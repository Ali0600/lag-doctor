"""Frame timing from PresentMon 2.6.0, run as a child process whose CSV arrives on a pipe.

Where each rule comes from (PM = docs/research/presentmon.md; DESIGN = docs/DESIGN.md, "Collector
specifics -> frames"):
- the command and its flags (PM 10a, DESIGN); the PID is validated first because PresentMon reads it
  with wcstoul, so 0 or junk means "every process" and -1 means none (PM 5);
- the parser (PM 10 "Parser rules") and the mapping to the F columns (DESIGN);
- the stop protocol (PM 7.4): CTRL_BREAK to the child's own process group when we share its console,
  else a second PresentMon that stops the ETW session by name, and only then a kill followed by the same
  clean-up, because TerminateProcess leaves the session running until reboot (PM 7.2);
- start failures classified by stderr text, because exit code 6 has several meanings (PM verification
  log; MainThread.cpp#L336-L382 at v2.6.0).
"""
from __future__ import annotations

import codecs
import collections
import contextlib
import ctypes
import os
import re
import signal
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import psutil

from lagdoctor import clock, setup
from lagdoctor.capture.base import Collector
from lagdoctor.model import F, SeriesBuilder

VERSION = setup.VERSION
TOOL = f"PresentMon {VERSION}"
SESSION_NAME = "lagdoctor"
MAX_PID = 0xFFFFFFFF
# lag-doctor's flags (PM 10a); the live-only ones are added for a live capture.
FLAGS = ("--output_stdout", "--no_console_stats", "--qpc_time", "--track_frame_type",
         "--track_pc_latency", "--track_hybrid_present")
LIVE_ONLY = ("--session_name", SESSION_NAME, "--stop_existing_session", "--terminate_on_proc_exit")
KNOWN_EXIT_CODES = frozenset(range(8))   # 0-7, PM 5 "Exit codes"; anything else is a crash


def check_pid(pid: object) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= MAX_PID:
        raise ValueError(f"not a process id: {pid!r} (PresentMon would read 0 or junk as every process)")
    return pid


def live_args(pid: int) -> list[str]:
    return ["--process_id", str(check_pid(pid)), *FLAGS, *LIVE_ONLY]


def replay_args(etl_file: Path | str) -> list[str]:
    return ["--etl_file", str(etl_file), *FLAGS]


def terminate_args() -> list[str]:
    return ["--terminate_existing_session", "--session_name", SESSION_NAME]


# ------------------------------------------------------------------ bytes -> lines ---

MAX_LINE_CHARS = 16 * 1024    # a real row is about 330 characters


class LineSplitter:
    """Bytes in any chunking -> text lines without CR/LF (PM 10 rule 1).

    The first bytes pick the encoding: FF FE is UTF-16LE (stdout redirected to an empty file), EF BB BF
    is UTF-8 (--output_file); anything else is the narrow text PresentMon writes into a pipe, which is
    exactly Latin-1 (PM 4). A line longer than MAX_LINE_CHARS, or one cut off by the end of the stream,
    is counted in `dropped` instead of being buffered without bound or half-parsed.
    """

    def __init__(self) -> None:
        self.encoding: str | None = None
        self.dropped = 0
        self._head = b""
        self._decoder: codecs.IncrementalDecoder | None = None
        self._pending = ""
        self._skipping = False

    def _start(self, data: bytes) -> bytes:
        if data.startswith(b"\xff\xfe"):
            self.encoding, data = "utf-16-le", data[2:]
        elif data.startswith(b"\xef\xbb\xbf"):
            self.encoding, data = "utf-8", data[3:]
        else:
            self.encoding = "latin-1"
        self._decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        return data

    def feed(self, chunk: bytes) -> list[str]:
        if self._decoder is None:
            self._head += chunk
            if len(self._head) < 3:            # not enough bytes to tell a BOM yet
                return []
            chunk, self._head = self._start(self._head), b""
        return self._split(self._decoder.decode(chunk))

    def close(self) -> list[str]:
        if self._decoder is None:
            if not self._head:
                return []
            data, self._head = self._start(self._head), b""
            lines = self._split(self._decoder.decode(data, final=True))
        else:
            lines = self._split(self._decoder.decode(b"", final=True))
        if self._pending or self._skipping:    # no newline at the end: the writer was cut off
            self.dropped += 1
        self._pending, self._skipping = "", False
        return lines

    def take_dropped(self) -> int:
        n, self.dropped = self.dropped, 0
        return n

    def _split(self, text: str) -> list[str]:
        parts = text.split("\n")
        tail = parts.pop()
        out = []
        for piece in parts:
            if self._skipping:                 # this piece ends an overlong line
                self._skipping = False
                continue
            line, self._pending = self._pending + piece, ""
            if line.endswith("\r"):
                line = line[:-1]
            if len(line) > MAX_LINE_CHARS:
                self.dropped += 1
                continue
            out.append(line)
        if self._skipping:
            return out
        self._pending += tail
        if len(self._pending) > MAX_LINE_CHARS + 1:    # + its CR, still waiting for the LF
            self.dropped += 1
            self._pending, self._skipping = "", True
        return out


# ------------------------------------------------------------------ lines -> rows ---

HEADER_PREFIX = "Application,ProcessID,"        # every PresentMon mode starts its header so (PM 10 rule 2)
REQUIRED = ("Application", "ProcessID", "SwapChainAddress", "TimeInQPC", "MsBetweenPresents")
INT_COLS = frozenset({"ProcessID", "SyncInterval", "PresentFlags", "AllowsTearing", "HybridPresent",
                      "VidPnSourceId", "LayerIndex", "PresentId", "FrameId", "TimeInQPC", "CPUStartQPC"})
STR_COLS = frozenset({"Application", "PresentRuntime", "PresentMode", "FrameType", "TimeInDateTime",
                      "CPUStartDateTime"})
# Numbers lag-doctor reads: anything else in them means the columns shifted, so the row is refused.
STRICT_FLOATS = frozenset({"MsBetweenPresents", "MsBetweenDisplayChange", "MsUntilDisplayed", "MsBetweenAppStart",
                           "MsCPUBusy", "MsCPUWait", "MsGPULatency", "MsGPUTime", "MsGPUBusy", "MsGPUWait",
                           "MsAnimationError", "MsAllInputToPhotonLatency"})

_PID = re.compile(r"[0-9]{1,10}")
_ADDRESS = re.compile(r"0x[0-9A-Fa-f]{1,16}")       # "0x%llX" (CsvOutput.cpp#L1131)
_INT = re.compile(r"-?[0-9]{1,20}")
_FLOAT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_PRINTF_NAN = re.compile(r"-?(?:nan(?:\([a-z]*\))?|inf(?:inity)?)", re.IGNORECASE)


def convert(column: str, value: str) -> Any:
    """One CSV field as a Python value (PM 10 rule 4); ValueError when it cannot be right."""
    if value == "NA":
        return None
    if column == "SwapChainAddress":
        if not _ADDRESS.fullmatch(value):
            raise ValueError(f"SwapChainAddress {value!r}")
        return int(value, 16)
    if column == "ProcessID":
        if not _PID.fullmatch(value) or int(value) > MAX_PID:
            raise ValueError(f"ProcessID {value!r}")
        return int(value)
    if column in STR_COLS:
        return value
    if column in INT_COLS:
        if not _INT.fullmatch(value):
            raise ValueError(f"{column} {value!r}")
        return int(value)
    if _FLOAT.fullmatch(value):
        return float(value)
    if _PRINTF_NAN.fullmatch(value):
        return None                             # printf's spelling of a value PresentMon did not have
    if column in STRICT_FLOATS:
        raise ValueError(f"{column} {value!r}")
    return value                                # a column lag-doctor does not read stays text


class CsvParser:
    """PresentMon CSV lines -> rows keyed by column name (PM 10 rules 2-4).

    Every header line rebuilds the column map (a second header appears when recording restarts, PM 1.4).
    `malformed` counts rows it refused (field count off, a value that cannot be right, no usable header
    yet); `rejoined` counts rows whose Application name contained commas.
    """

    def __init__(self) -> None:
        self.columns: list[str] = []
        self.missing: list[str] = []            # required columns the current header lacks
        self.headers = 0
        self.rows = 0
        self.malformed = 0
        self.rejoined = 0

    def feed(self, line: str) -> dict[str, Any] | None:
        if not line:
            return None
        if line.startswith(HEADER_PREFIX):
            self.columns = line.split(",")
            self.headers += 1
            self.missing = [c for c in REQUIRED if c not in self.columns]
            return None
        if not self.columns or self.missing:
            self.malformed += 1
            return None
        fields = line.split(",")                # nothing is quoted (PM 10 rule 3)
        extra = len(fields) - len(self.columns)
        if extra > 0:
            # Only Application (column 0) may hold commas. The rejoined row stands only if the two fields
            # after it still convert as ProcessID and SwapChainAddress, i.e. look like a PID and 0x...
            # (convert() below; those are columns 1 and 2 in every PresentMon mode, PM 10 rule 3).
            fields = [",".join(fields[:extra + 1]), *fields[extra + 1:]]
        if len(fields) != len(self.columns):
            self.malformed += 1
            return None
        try:
            row = {c: convert(c, v) for c, v in zip(self.columns, fields)}
        except ValueError:
            self.malformed += 1
            return None
        if any(row[c] is None for c in REQUIRED):
            self.malformed += 1
            return None
        self.rows += 1
        self.rejoined += extra > 0
        return row


def parse(chunks: Iterable[bytes], parser: CsvParser | None = None) -> Iterator[dict[str, Any]]:
    """Rows from PresentMon's raw output bytes in any chunking. Pure: no I/O, no clock."""
    parser = parser if parser is not None else CsvParser()
    splitter = LineSplitter()
    for chunk in chunks:
        for line in splitter.feed(chunk):
            row = parser.feed(line)
            if row is not None:
                yield row
        parser.malformed += splitter.take_dropped()
    for line in splitter.close():
        row = parser.feed(line)
        if row is not None:
            yield row
    parser.malformed += splitter.take_dropped()


def frame_row(row: dict[str, Any], origin: float, chain: str) -> dict[str, Any]:
    """One parsed row as one row of F columns (DESIGN.md, frames)."""
    frame_type = row.get("FrameType")
    app = frame_type is None or frame_type == "Application"

    def ms(column: str, zero_is_missing: bool = False, app_only: bool = False) -> float | None:
        v = row.get(column)
        if not isinstance(v, float) or (app_only and not app) or (zero_is_missing and v == 0.0):
            return None
        return v

    # PresentMon prints 0 for a missing CPU busy/wait (WriteMetricOrZero, PM 2.3), and generated frames
    # carry no CPU timing and no GPU busy of their own (PM 8.4).
    cpu_busy = ms("MsCPUBusy", zero_is_missing=True, app_only=True)
    until = ms("MsUntilDisplayed")
    return {
        F.T: clock.qpc_to_seconds(row["TimeInQPC"]) - origin,
        F.FRAME_MS: ms("MsBetweenPresents"),
        F.CPU_BUSY_MS: cpu_busy,
        F.CPU_WAIT_MS: ms("MsCPUWait", zero_is_missing=True, app_only=True),
        F.GPU_LATENCY_MS: ms("MsGPULatency"),
        F.GPU_BUSY_MS: ms("MsGPUBusy", app_only=True),
        F.GPU_WAIT_MS: ms("MsGPUWait"),
        F.DISPLAYED: (0 if until is None else 1) if "MsUntilDisplayed" in row else None,
        F.DISPLAY_MS: ms("MsBetweenDisplayChange"),
        F.DISPLAY_LATENCY_MS: cpu_busy + until if cpu_busy is not None and until is not None else None,
        F.INPUT_LATENCY_MS: ms("MsAllInputToPhotonLatency"),
        F.ANIM_ERROR_MS: ms("MsAnimationError"),
        F.PRESENT_MODE: row.get("PresentMode"),
        F.RUNTIME: row.get("PresentRuntime"),
        F.SYNC_INTERVAL: row.get("SyncInterval"),
        F.ALLOWS_TEARING: row.get("AllowsTearing"),
        F.SWAP_CHAIN: chain,
        F.FRAME_TYPE: frame_type,
        F.HYBRID: row.get("HybridPresent"),
    }


def main_chain_of(chains: dict[str, dict]) -> str | None:
    """The swap chain with the most displayed frames (then the most frames)."""
    if not chains:
        return None
    return max(chains, key=lambda k: (chains[k]["displayed"], chains[k]["frames"]))


# ------------------------------------------------------------- starting and stopping ---

RIGHTS_DETAIL = ("PresentMon needs admin rights or membership of Performance Log Users to trace frames "
                 "(access denied); frame timing was not measured")


def classify_start_failure(exit_code: int | None, stderr: str) -> tuple[str, str, bool]:
    """(state, detail, retry) for a PresentMon that ended before writing its CSV header.

    With --stop_existing_session a leftover session shows up as the warning "... is already running and
    it will be stopped", and a failed restart as "error code 183." (MainThread.cpp#L336-L370).
    """
    low = stderr.lower()
    if "access denied" in low:
        return "unavailable", RIGHTS_DETAIL, False
    if "already running" in low or "error code 183" in low:
        return "failed", f"a trace session named {SESSION_NAME!r} is already running and could not be replaced", True
    if "file not found" in low or "path not found" in low:
        return "failed", "PresentMon could not open its input (file not found)", False
    message = next((ln.strip() for ln in stderr.splitlines() if ln.lower().startswith("error:")), "")
    if exit_code == 1:
        return "failed", f"PresentMon rejected its command line: {message or 'no message'}", False
    return "failed", f"PresentMon ended with exit code {exit_code}: {message or 'no message'}", False


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetConsoleCP.argtypes = []
_kernel32.GetConsoleCP.restype = wintypes.UINT


def has_console() -> bool:
    """GetConsoleCP() is 0 without a console; GetConsoleWindow() is NULL even with a hidden one (PM 7.4)."""
    return _kernel32.GetConsoleCP() != 0


def spawn(argv: list[str], console: bool) -> subprocess.Popen:
    """Start PresentMon in its own process group so CTRL_BREAK reaches it alone (PM 7.4 step 1).

    Without a console of our own the child would open a window, so it gets CREATE_NO_WINDOW; it then
    sits in a console of its own that CTRL_BREAK cannot reach, and is stopped through its session.
    """
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | (0 if console else subprocess.CREATE_NO_WINDOW)
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=flags)


def _discard(stream) -> None:
    try:
        for _ in iter(lambda: stream.read1(1 << 16), b""):
            pass
    except (OSError, ValueError):
        pass


def _exited_within(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def stop_child(proc: subprocess.Popen, *, console: bool, terminate_session: Callable[[float], object] | None,
               ctrl_break_wait: float = 10.0, session_cmd_timeout: float = 3.0, session_stop_wait: float = 3.0,
               kill_wait: float = 1.0) -> str:
    """Stop PresentMon without leaking its ETW session (PM 7.4) and return the path that worked.

    "exited": it had already ended. "ctrl_break": it shut down on CTRL_BREAK. "terminate_session": a second
    PresentMon stopped the session by name and this one then ended normally. "kill": TerminateProcess, and
    the session was then stopped by name as clean-up. terminate_session(timeout) is None for an ETL replay,
    which has no session. Keep draining both pipes meanwhile: a full pipe blocks PresentMon's exit.
    """
    if proc.poll() is not None:
        return "exited"
    if console:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:                        # no console after all (WinError 6): use the session path
            if proc.poll() is not None:
                return "exited"
        else:
            if _exited_within(proc, ctrl_break_wait):
                return "ctrl_break"
    if terminate_session is not None:
        terminate_session(session_cmd_timeout)
        if _exited_within(proc, session_stop_wait):
            return "terminate_session"
    proc.kill()
    _exited_within(proc, kill_wait)
    if terminate_session is not None:
        terminate_session(session_cmd_timeout)  # the kill left the session running: remove it
    return "kill"


class _Unavailable(Exception):
    pass


# --------------------------------------------------------------------- the collector ---

class PresentMonSource(Collector):
    """Collector "frames": PresentMon on one game process, or on an ETL file (tests; no rights needed).

    result() (DESIGN.md result() table): `columns` (F, every swap chain of the target), `swap_chains`
    {"0x…": {frames, displayed, runtime}}, `main_chain`, and `presentmon` {version, exe, args (without
    the exe), exit_code, stop_path, rows, malformed, rejoined, headers, stderr_tail}. A replay without
    a pid keeps every process and keys its chains "<pid>:0x…", since an address is only unique within
    one process (PM 10 rule 6).

    The game is watched with is_alive() (default: its PID is still listed by psutil.pids()), because
    --terminate_on_proc_exit never fires if anti-cheat strips PresentMon's handle (PM 8.1).
    A live PresentMon that ends by itself with exit code 0 while the game still runs had its trace session
    stopped from outside (PM 5, 7.1: e.g. another capture's --stop_existing_session), so its frames stop
    there: the capture is "partial" and says when. The game is given EXITED_GRACE_S to vanish first, since
    --terminate_on_proc_exit can beat our own liveness check.
    live() gives {frames, fps (busiest chain over its last second of frame times), malformed}.
    stop() takes at most about 20 s on its worst path (CTRL_BREAK, session stop, kill + clean-up), which
    with the base class's 5 s join stays inside DESIGN's 25 s.
    """

    name = "frames"
    CTRL_BREAK_WAIT_S = 10.0       # DESIGN: the second instance comes in after 10 s
    SESSION_CMD_TIMEOUT_S = 3.0    # --terminate_existing_session itself takes about 0.05 s (PM 6.2)
    SESSION_STOP_WAIT_S = 3.0      # PresentMon ends once ProcessTrace returns (ConsumerThread.cpp)
    KILL_WAIT_S = 1.0
    WATCH_INTERVAL_S = 1.0
    EXIT_WAIT_S = 5.0              # after stdout ends, how long the process may take to exit
    EXITED_GRACE_S = 1.5           # PresentMon ended by itself: how long the game gets to be seen gone too
    ALIVE_POLL_S = 0.25
    STDERR_LINES = 20
    LINE_LIMIT_BYTES = 64 * 1024
    PUBLISH_EVERY_S = 0.25

    def __init__(self, pid: int | None, exe: Path | str | None = None, etl_file: Path | str | None = None, *,
                 is_alive: Callable[[], bool] | None = None) -> None:
        super().__init__()
        # Absolute paths: the exe that runs must be the file that was verified (a relative name would go
        # through CreateProcess's search order), and an ETL path must never read as an option.
        self.etl_file = Path(os.path.abspath(etl_file)) if etl_file is not None else None
        self.pid = check_pid(pid) if self.etl_file is None or pid is not None else None
        self.exe = Path(os.path.abspath(exe)) if exe is not None else None
        self._is_alive = is_alive
        self._console = True
        self._lock = threading.Lock()           # what result() and live() read
        self._proc_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._series = SeriesBuilder(F.ALL)
        self._chains: dict[str, dict[str, Any]] = {}
        self._recent: dict[str, collections.deque] = {}
        self._parser = CsvParser()
        self._stderr_head: list[str] = []
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=self.STDERR_LINES)
        self._proc: subprocess.Popen | None = None
        self._handled: subprocess.Popen | None = None
        self.child_pid: int | None = None
        self._args: list[str] = []
        self._exe_used: str | None = None
        self._exit_code: int | None = None
        self._stop_path: str | None = None
        self._failure: tuple[str, str] | None = None
        self._notes: list[str] = []
        self._row_errors = 0
        self._last_publish = 0.0
        self._last_frame_t: float | None = None
        self._alone: tuple[float, float | None] | None = None   # (t, last frame t) when it ended by itself

    # ---- collector interface ---------------------------------------------------------
    def run(self) -> None:
        self._console = has_console()
        self.set_status("ok", "starting PresentMon", tool=TOOL)
        retried = False
        try:
            while True:
                outcome = self._capture_once()
                if outcome == "retry" and not retried and not self.stopping:
                    retried = True
                    self._failure = None
                    self._note(f"a trace session named {SESSION_NAME!r} was already running; it was stopped "
                               "and the capture started again")
                    self._terminate_session(self.SESSION_CMD_TIMEOUT_S)
                    continue
                break
        except _Unavailable as exc:
            self.set_status("unavailable", str(exc), tool=TOOL)
            return
        self._settle(outcome)

    def on_stop(self) -> None:
        self._stop_child()

    def result(self) -> dict[str, Any]:
        with self._lock:
            chains = {k: {"frames": c["frames"], "displayed": c["displayed"],
                          "runtime": c["runtimes"].most_common(1)[0][0] if c["runtimes"] else None}
                      for k, c in self._chains.items()}
            info = {"version": VERSION, "exe": self._exe_used, "args": list(self._args),
                    "exit_code": self._exit_code, "stop_path": self._stop_path, "rows": len(self._series),
                    "malformed": self._parser.malformed, "rejoined": self._parser.rejoined,
                    "headers": self._parser.headers, "stderr_tail": list(self._stderr_tail)}
            columns = self._series.build()
        return {"columns": columns, "swap_chains": chains, "main_chain": main_chain_of(chains), "presentmon": info}

    # ---- one PresentMon run ------------------------------------------------------------
    @contextlib.contextmanager
    def _exe_guard(self) -> Iterator[list[str]]:
        """The command prefix for PresentMon: the pinned exe, locked against writers while it starts."""
        path = self.exe if self.exe is not None else setup.installed_exe()
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(setup.pinned(path))
        except setup.NotInstalled:
            raise _Unavailable(f"PresentMon {VERSION} is not installed ({path}); run `lagdoctor setup`") from None
        except setup.SetupError as exc:
            raise _Unavailable(f"PresentMon was not run because it failed its integrity check: {exc}. "
                               "Run `lagdoctor setup` to replace it") from None
        with stack:
            self._exe_used = str(path)
            yield [str(path)]

    def _capture_once(self) -> str:
        args = replay_args(self.etl_file) if self.etl_file is not None else live_args(self.pid)
        with self._exe_guard() as prefix:
            with self._proc_lock:
                if self.stopping:
                    return "stopped"
                try:
                    proc = spawn(prefix + args, self._console)
                except OSError as exc:
                    raise _Unavailable(f"PresentMon could not be started: {exc}") from None
                self._proc = proc
        self.child_pid = proc.pid
        with self._lock:
            self._args = args
            self._exit_code = self._stop_path = None
            self._stderr_head.clear()
            self._stderr_tail.clear()
        self._alone = None
        self._last_publish = 0.0       # a new run's first frames show at once, not held back by the last run's
        headers_before = self._parser.headers
        stderr_thread = threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True,
                                         name="lagdoctor-frames-stderr")
        stderr_thread.start()
        done = threading.Event()
        watcher = None
        if self.etl_file is None:
            watcher = threading.Thread(target=self._watch_game, args=(done,), daemon=True,
                                       name="lagdoctor-frames-watch")
            watcher.start()
        try:
            self._read_stdout(proc.stdout)
            # Its output ended though nobody asked it to stop: told apart from the game's own exit in _settle.
            if self.etl_file is None and proc is not self._handled and not self.stopping:
                self._alone = (self.now(), self._last_frame_t)
        except BaseException:
            # Keep the pipe drained while PresentMon is stopped below: a full pipe blocks its shutdown (PM 4).
            threading.Thread(target=_discard, args=(proc.stdout,), daemon=True).start()
            raise
        finally:
            done.set()
            if not _exited_within(proc, self.EXIT_WAIT_S):
                self._stop_child()             # stdout ended but the process lives on: stop it properly
            code = proc.returncode if _exited_within(proc, self.EXIT_WAIT_S) else None
            stderr_thread.join(self.EXIT_WAIT_S)
            if watcher is not None:
                watcher.join(self.EXIT_WAIT_S)
            if self._stop_lock.acquire(timeout=30.0):   # let a stop in progress (kill + clean-up) finish
                self._stop_lock.release()
            with self._lock:
                self._exit_code = code
                if self._stop_path is None:
                    self._stop_path = "exited"
            if code is not None and not stderr_thread.is_alive():
                proc.stdout.close()
                proc.stderr.close()
        stopped_by_us = proc is self._handled and self._stop_path != "exited"
        if self._parser.headers == headers_before and code != 0 and not stopped_by_us:
            with self._lock:
                stderr = "\n".join(self._stderr_head)
            state, detail, retry = classify_start_failure(code, stderr)
            self._failure = (state, detail)
            return "retry" if retry else "failed"
        if self.etl_file is None and self._stop_path == "exited" and code is not None and code not in KNOWN_EXIT_CODES:
            self._note(f"PresentMon crashed (exit code 0x{code:08X}); its trace session was stopped by name")
            self._terminate_session(self.SESSION_CMD_TIMEOUT_S)
        return "ran"

    def _read_stdout(self, stream) -> None:
        chunks = iter(lambda: stream.readline(self.LINE_LIMIT_BYTES), b"")
        for row in parse(chunks, self._parser):
            try:
                self._add(row)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the drain
                self._row_errors += 1
                if self._row_errors == 1:
                    self._note(f"a row could not be stored: {type(exc).__name__}: {exc}")
        self._publish(force=True)

    def _add(self, row: dict[str, Any]) -> None:
        pid = row["ProcessID"]
        if self.pid is not None and pid != self.pid:
            return
        address = f"0x{row['SwapChainAddress']:X}"
        key = address if self.pid is not None else f"{pid}:{address}"
        fr = frame_row(row, self.origin, key)
        with self._lock:
            self._series.add(**fr)
            if self._last_frame_t is None or fr[F.T] > self._last_frame_t:
                self._last_frame_t = fr[F.T]
            chain = self._chains.get(key)
            if chain is None:
                chain = self._chains[key] = {"frames": 0, "displayed": 0, "runtimes": collections.Counter()}
                self._recent[key] = collections.deque(maxlen=1024)
            chain["frames"] += 1
            chain["displayed"] += fr[F.DISPLAYED] == 1
            chain["runtimes"][fr[F.RUNTIME]] += 1
            self._recent[key].append(fr[F.T])
        self._publish()

    def _publish(self, force: bool = False) -> None:
        now = clock.now()
        if not force and now - self._last_publish < self.PUBLISH_EVERY_S:
            return
        self._last_publish = now
        with self._lock:
            frames = len(self._series)
            main = max(self._chains, key=lambda k: self._chains[k]["frames"]) if self._chains else None
            recent = list(self._recent[main]) if main else []
            malformed = self._parser.malformed
        fps = None
        if recent:
            newest = max(recent)
            fps = float(sum(1 for t in recent if t > newest - 1.0))
        self.publish(frames=frames, fps=fps, malformed=malformed)

    def _drain_stderr(self, stream) -> None:
        splitter = LineSplitter()
        try:
            for chunk in iter(lambda: stream.read1(4096), b""):
                for line in splitter.feed(chunk):
                    self._add_stderr(line)
        except (OSError, ValueError):
            pass                                # the pipe was closed under us: nothing more to read
        for line in splitter.close():
            self._add_stderr(line)

    def _add_stderr(self, line: str) -> None:
        line = line[:300]
        with self._lock:
            if len(self._stderr_head) < self.STDERR_LINES:
                self._stderr_head.append(line)
            self._stderr_tail.append(line)

    def _probe_alive(self) -> bool | None:
        """Is the game still running? None when the check itself failed."""
        is_alive = self._is_alive or (lambda: self.pid in psutil.pids())
        try:
            return bool(is_alive())
        except Exception:  # noqa: BLE001 - a broken check must not end the capture
            return None

    def _watch_game(self, done: threading.Event) -> None:
        while not done.wait(self.WATCH_INTERVAL_S):
            if self._probe_alive() is False:
                self._note("the game exited, so the capture was stopped")
                self._stop_child()
                return

    def _game_after_exit(self) -> str:
        """After PresentMon ended by itself: "exited" if the game is gone within EXITED_GRACE_S (its
        --terminate_on_proc_exit beat our snapshot), else "running", or "unknown" if no check worked."""
        deadline = clock.now() + self.EXITED_GRACE_S
        seen = None
        while True:
            alive = self._probe_alive()
            if alive is False:
                return "exited"
            seen = seen or alive
            left = deadline - clock.now()
            if left <= 0:
                return "running" if seen else "unknown"
            time.sleep(min(self.ALIVE_POLL_S, left))

    def _stop_child(self) -> None:
        if not self._stop_lock.acquire(timeout=30.0):
            return
        try:
            with self._proc_lock:
                proc = self._proc
            if proc is None or proc is self._handled:
                return
            self._handled = proc
            session = None if self.etl_file is not None else self._terminate_session
            path = stop_child(proc, console=self._console, terminate_session=session,
                              ctrl_break_wait=self.CTRL_BREAK_WAIT_S, session_cmd_timeout=self.SESSION_CMD_TIMEOUT_S,
                              session_stop_wait=self.SESSION_STOP_WAIT_S, kill_wait=self.KILL_WAIT_S)
            with self._lock:
                self._stop_path = path
        finally:
            self._stop_lock.release()

    def _terminate_session(self, timeout: float) -> int | None:
        """A second PresentMon stops the session by name: 0 stopped, 7 none (PM 7.4 step 3)."""
        try:
            with self._exe_guard() as prefix:
                r = subprocess.run(prefix + terminate_args(), stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError, _Unavailable) as exc:
            self._note(f"PresentMon could not be run to stop its trace session: {exc}")
            return None
        return r.returncode

    def _note(self, text: str) -> None:
        with self._lock:
            if text not in self._notes and len(self._notes) < 10:
                self._notes.append(text)

    def _settle(self, outcome: str) -> None:
        if outcome == "stopped":
            self.set_status("off", "stopped before PresentMon started", tool=TOOL)
            return
        if self._failure is not None:
            self.set_status(*self._failure, tool=TOOL)
            return
        with self._lock:
            rows, chains = len(self._series), len(self._chains)
            code, path, notes = self._exit_code, self._stop_path, list(self._notes)
            stderr = list(self._stderr_head) + [ln for ln in self._stderr_tail if ln not in self._stderr_head]
        target = f"PID {self.pid}" if self.pid is not None else "the ETL file"
        problems = []
        if self._parser.missing:
            problems.append(f"PresentMon's CSV header lacks {', '.join(self._parser.missing)}, so the rows "
                            "under it could not be used")
        if rows == 0:
            if problems:
                why = problems[0]
            elif self._parser.headers == 0:
                why = "PresentMon ran but wrote no CSV"
            elif self.etl_file is not None:
                why = f"PresentMon ran but no frames came from {target}"
            else:
                why = f"PresentMon ran but no frames came from {target} (is it the process that draws the game?)"
            self.set_status("failed", "; ".join([why, *notes]), tool=TOOL)
            return
        if path == "kill":
            problems.append("PresentMon had to be killed, so its last frames may be missing")
        elif code is None:
            problems.append("PresentMon did not exit")
        elif code != 0:
            problems.append(f"PresentMon ended with exit code {code}")
        elif self._alone is not None and path == "exited":
            ended_t, last_t = self._alone
            game = self._game_after_exit()
            last = f"t={last_t:.1f} s" if last_t is not None else "its start"
            if game == "exited":
                if "the game exited, so the capture was stopped" not in notes:
                    notes.append("the game exited, so the capture was stopped")
            elif game == "running":
                problems.append(f"PresentMon stopped on its own at t={ended_t:.1f} s while the game kept running "
                                f"(most likely its {SESSION_NAME!r} trace session was stopped from outside, e.g. by "
                                f"another capture); no frames after {last}")
            else:
                problems.append(f"PresentMon stopped on its own at t={ended_t:.1f} s and whether the game was still "
                                f"running could not be checked; no frames after {last}")
        if self._parser.malformed:
            problems.append(f"{self._parser.malformed} malformed CSV row(s) skipped")
        if self._row_errors:
            problems.append(f"{self._row_errors} row(s) could not be stored")
        problems += [ln.strip() for ln in stderr if re.search(r"were lost|overflowed present events", ln)]
        if any("is already running and it will be stopped" in ln for ln in stderr):
            notes.append(f"an earlier {SESSION_NAME!r} trace session was still running and was replaced")
        detail = "; ".join([f"{rows} frames from {chains} swap chain(s) of {target}", *problems, *notes])
        self.set_status("partial" if problems else "ok", detail, tool=TOOL)
