"""A Windows service written in Python, for the Stage 2 experiments (docs/INSTALLER-PLAN.md, Stage 2).

The service control manager runs it as
    "<install>\\python.exe" "<install>\\stage2\\service_host.py" <mode>
where <install> holds python.org's embeddable runtime, lag-doctor's code and the pinned PresentMon (drive.py
builds it). It talks to the SCM through ctypes only: StartServiceCtrlDispatcherW, RegisterServiceCtrlHandlerExW
and SetServiceStatus (E3c's question). The arguments StartServiceW passes follow the mode:
- e3c RESULT: how the runtime came up: sys.path as it was before any import, sys.flags, the interpreter's paths
  and the token.
- e3 RESULT GAME_PID SECONDS DESCRIPTORS: E3 under this service's own token: the token; each trace provider
  enabled into a session of its own; Windows' own access check (authz) on the descriptors E1 read on this
  machine, judged for this token; then lag-doctor's own frame-timing and network collectors on GAME_PID until
  the game exits or SECONDS pass.
- e3b FOLDER PIPE IDLE (on the command line, after the mode): serves \\\\.\\pipe\\PIPE and stops itself after IDLE
  seconds without a client; each start writes FOLDER\\e3b-<pid>.json.
Each mode writes its result as JSON into a new file (a crash writes the traceback instead) and then reports
SERVICE_STOPPED with a service-specific exit code: 0 the mode finished, 1 it crashed, 2 bad arguments, 3 the
result could not be written.
"""
import sys

SYS_PATH_AT_START = list(sys.path)          # before any import below can change it
SITE_AT_START = "site" in sys.modules

import ctypes  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import statistics  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402
from ctypes import wintypes  # noqa: E402
from pathlib import Path  # noqa: E402

INSTALL = Path(__file__).resolve().parents[1]
# drive.py's RESULTS: the one folder the service may write. A crash before any mode runs lands here too, since a
# service has no console and the SCM keeps nothing it printed.
RESULTS = Path(r"C:\lagdoctor-stage2-results")
try:
    from stage2 import tokeninfo
    IMPORT_NOTE = None
except ImportError as _exc:                  # keep the run diagnosable; the snapshot above shows why
    sys.path.insert(0, str(INSTALL))
    from stage2 import tokeninfo
    IMPORT_NOTE = f"stage2 was not importable from sys.path as the runtime set it up ({_exc}); {INSTALL} was added"


def _scripts_on_path() -> None:
    """etw_access.py sits in <install>\\scripts, as it does in the checkout (it adds its parent to sys.path itself)."""
    scripts = str(INSTALL / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)

# winsvc.h / winnt.h / winerror.h (Windows SDK 10.0.19041)
SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 0x1, 0x4
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_SHUTDOWN = 1, 4, 5
NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED, ERROR_SERVICE_SPECIFIC_ERROR = 0, 120, 1066
FINISHED, CRASHED, BAD_ARGS, NOT_WRITTEN = 0, 1, 2, 3
NOT_A_SERVICE = 4                           # the process exit code when no SCM started it

SERVICE_MAIN_FUNCTION = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
HANDLER_FUNCTION_EX = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.c_void_p)


class SERVICE_TABLE_ENTRYW(ctypes.Structure):
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", SERVICE_MAIN_FUNCTION)]


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


assert ctypes.sizeof(SERVICE_TABLE_ENTRYW) == 16
assert ctypes.sizeof(SERVICE_STATUS) == 28

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
_advapi32.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
_advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER_FUNCTION_EX, ctypes.c_void_p]
_advapi32.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
_advapi32.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
_advapi32.SetServiceStatus.restype = wintypes.BOOL


# ------------------------------------------------------------------------------------------- the modes ---

def _status(collector) -> dict:
    s = collector.status()
    return {"state": s.state, "detail": s.detail, "tool": s.tool}


def _part(fn, *args):
    """One part of a mode's result: what fn returns, or its traceback, so one failure keeps the other parts."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - the result must say what broke
        return {"error": traceback.format_exc()}


def provider_probe() -> dict:
    """Each provider PresentMon and the tracker use (scripts/etw_access.py PROVIDERS), enabled into a real-time
    session of its own under this token: {"enabled": True} or the refusal with the call that refused."""
    _scripts_on_path()
    import etw_access
    from lagdoctor.winapi import etw
    rows = {}
    for i, (provider, (guid, need)) in enumerate(etw_access.PROVIDERS.items()):
        session = etw.RealtimeSession(f"lagdoctor-e3-probe-{i}", [(guid, 4, 0)], lambda _event: None)
        row = {"guid": guid, "needed_for": need}
        try:
            session.start()
            row["enabled"] = True
        except OSError as exc:
            row.update(enabled=False, winerror=exc.winerror, error=str(exc))
        finally:
            session.stop()
        rows[provider] = row
    return rows


def authz_with_own_token(descriptors: Path) -> dict:
    """E1's method with this real token: Windows' own access check on the descriptors E1 read on this machine."""
    _scripts_on_path()
    import etw_access
    data = json.loads(descriptors.read_text(encoding="utf-8"))
    rows = {}
    with etw_access.Authz({"service": (None, ())}) as authz:
        for provider, row in data["providers"].items():
            if not isinstance(row, dict) or not isinstance(row.get("sddl"), str):
                rows[provider] = {"error": "E1 read no descriptor for it"}
                continue
            sd = etw_access.descriptor_from_sddl(row["sddl"])
            rows[provider] = {"enable": authz.granted("service", sd, etw_access.TRACELOG_GUID_ENABLE),
                              "realtime_session": authz.granted("service", sd, etw_access.REALTIME_SESSION)}
    return rows


def capture(game_pid: int, seconds: float, stop: threading.Event) -> dict:
    """lag-doctor's own collectors, as the capture service will run them: PresentMon on the game's PID and the
    Kernel-Network tracker, until PresentMon's capture ends by itself (the game exited) or `seconds` pass."""
    from lagdoctor import clock, setup
    from lagdoctor.capture.netflows import FlowTracker
    from lagdoctor.capture.presentmon import PresentMonSource
    from lagdoctor.model import F
    origin = clock.now()
    frames = PresentMonSource(game_pid, exe=INSTALL / setup.EXE_NAME)
    flows = FlowTracker([game_pid])
    frames.start(origin)
    flows.start(origin)
    deadline = origin + seconds
    while not stop.is_set() and not frames.ended and clock.now() < deadline:
        stop.wait(0.2)
    ended_by_itself = frames.ended
    ended_qpc_s = clock.now()
    flows.stop()
    frames.stop()
    fr, nf = frames.result(), flows.result()
    frame_ms = [v for v in fr["columns"].get(F.FRAME_MS, []) if isinstance(v, (int, float))]
    return {"origin_qpc_s": origin, "ended_by_itself": ended_by_itself, "ended_qpc_s": ended_qpc_s,
            "frames": {"status": _status(frames), "presentmon": fr["presentmon"], "swap_chains": fr["swap_chains"],
                       "main_chain": fr["main_chain"],
                       "frame_ms_median": statistics.median(frame_ms) if frame_ms else None},
            "net_flows": {"status": _status(flows), "layout": nf["layout"], "health": nf["health"],
                          "talkers": nf["talkers"]}}


def _runtime() -> dict:
    return {"sys_path_at_start": SYS_PATH_AT_START, "site_imported_at_start": SITE_AT_START,
            "import_note": IMPORT_NOTE, "executable": sys.executable, "prefix": sys.prefix,
            "base_prefix": sys.base_prefix, "install": str(INSTALL), "version": sys.version, "cwd": os.getcwd(),
            "flags": {k: getattr(sys.flags, k) for k in ("isolated", "ignore_environment", "no_user_site",
                                                         "no_site", "safe_path")}}


def mode_e3c(name: str, args: list[str], stop: threading.Event) -> dict:
    return {"runtime": _runtime(), "token": _part(tokeninfo.snapshot)}


def mode_e3(name: str, args: list[str], stop: threading.Event) -> dict:
    _result, game_pid, seconds, descriptors = args
    return {"game_pid": int(game_pid), "runtime": _runtime(), "token": _part(tokeninfo.snapshot),
            "providers": _part(provider_probe), "authz": _part(authz_with_own_token, Path(descriptors)),
            "capture": _part(capture, int(game_pid), float(seconds), stop)}


def mode_e3b(name: str, args: list[str], stop: threading.Event) -> dict:
    """Serve the pipe until IDLE seconds pass without a client (then stop by itself) or the SCM asks to stop."""
    from lagdoctor import clock
    from stage2 import pipes
    _folder, pipe_name, idle = args
    started = clock.now()
    token = tokeninfo.snapshot()
    # Full access for SYSTEM and this service's own SID (which a write-restricted token needs to write its own
    # pipe), read and write for administrators and interactive users.
    sddl = ("D:P(A;;GA;;;SY)" + "".join(f"(A;;GA;;;{s})" for s in tokeninfo.service_sids(token["groups"]))
            + "(A;;GRGW;;;BA)(A;;GRGW;;;IU)")
    out = {"started_qpc_s": started, "runtime": _runtime(), "token": token, "pipe_sddl": sddl, "clients": []}
    idle_s, last = float(idle), started

    def reply(message: bytes) -> bytes:
        return json.dumps({"pid": os.getpid(), "started_qpc_s": started, "served_qpc_s": clock.now()}).encode()

    with pipes.PipeServer(f"\\\\.\\pipe\\{pipe_name}", sddl) as server:
        out["pipe_ready_qpc_s"] = clock.now()
        while not stop.is_set():
            if server.wait_client(0.25):
                client, served = server.client_pid(), clock.now()
                message = server.serve_one(reply)
                out["clients"].append({"pid": client, "qpc_s": served, "message": message.decode("ascii", "replace")[:100]})
                last = clock.now()
            elif clock.now() - last >= idle_s:
                out["stopped_by"] = "idle"
                break
        else:
            out["stopped_by"] = "stop request"
    out["stopped_qpc_s"] = clock.now()
    return out


# mode -> (function, number of arguments, where they come from). A trigger start passes no arguments of ours to
# ServiceMain, so e3b takes its own from the service's command line (the binPath sc.exe set); e3 and e3c take
# theirs from StartServiceW, since the game's PID is known only at start.
MODES = {"e3c": (mode_e3c, 1, "start"), "e3": (mode_e3, 4, "start"), "e3b": (mode_e3b, 3, "command line")}


def _target(mode: str, args: list[str]) -> Path:
    return Path(args[0]) / f"e3b-{os.getpid()}.json" if mode == "e3b" else Path(args[0])


def run_mode(mode: str, name: str, start_args: list[str], command_args: list[str], stop: threading.Event) -> int:
    entry = MODES.get(mode)
    if entry is None:
        return BAD_ARGS
    args = start_args if entry[2] == "start" else command_args
    if len(args) != entry[1]:
        return BAD_ARGS
    code = FINISHED
    try:
        data = entry[0](name, args, stop)
    except Exception:  # noqa: BLE001 - a crash is a result too
        data, code = {"crashed": traceback.format_exc()}, CRASHED
    data = {"mode": mode, "service": name, "pid": os.getpid(), "servicemain_args": start_args,
            "command_line_args": command_args, **data}
    try:
        text = json.dumps(data, indent=1, default=str)
        with open(_target(mode, args), "x", encoding="utf-8") as f:     # a new file only, never an overwrite
            f.write(text)
    except OSError:
        return NOT_WRITTEN
    return code


# ------------------------------------------------------------------------------- the SCM plumbing ---

class _Service:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.stop = threading.Event()
        self._handle = None
        self._lock = threading.Lock()
        self._checkpoint = 0
        self.main_cb = SERVICE_MAIN_FUNCTION(self._main)          # kept referenced for the process's life
        self._handler_cb = HANDLER_FUNCTION_EX(self._handler)

    def report(self, state: int, exit_code: int = 0, wait_hint_ms: int = 0) -> None:
        with self._lock:
            pending = state in (SERVICE_START_PENDING, SERVICE_STOP_PENDING)
            self._checkpoint = self._checkpoint + 1 if pending else 0
            accepted = 0 if state in (SERVICE_START_PENDING, SERVICE_STOPPED) else SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
            st = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accepted,
                                ERROR_SERVICE_SPECIFIC_ERROR if exit_code else NO_ERROR, exit_code,
                                self._checkpoint, wait_hint_ms)
            _advapi32.SetServiceStatus(self._handle, ctypes.byref(st))

    def _handler(self, control, _event_type, _data, _context) -> int:
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            self.stop.set()
            self.report(SERVICE_STOP_PENDING, wait_hint_ms=30_000)
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    def _main(self, argc, argv) -> None:
        args = [argv[i] for i in range(argc)]                  # argv[0] is the service's own name
        self._handle = _advapi32.RegisterServiceCtrlHandlerExW(args[0] if args else "", self._handler_cb, None)
        if not self._handle:
            return                                             # nothing can be reported without it
        code = CRASHED
        try:
            self.report(SERVICE_START_PENDING, wait_hint_ms=10_000)
            self.report(SERVICE_RUNNING)
            code = run_mode(self.mode, args[0], args[1:], sys.argv[2:], self.stop)
        finally:
            self.report(SERVICE_STOPPED, exit_code=code)


def main() -> int:
    service = _Service(sys.argv[1] if len(sys.argv) > 1 else "")
    table = (SERVICE_TABLE_ENTRYW * 2)()                       # zeroed: the second entry ends the table
    table[0].lpServiceName = "lagdoctor-stage2"                # ignored for an own-process service, never NULL
    table[0].lpServiceProc = service.main_cb
    if not _advapi32.StartServiceCtrlDispatcherW(table):
        print(f"Not started by the service control manager (error {ctypes.get_last_error()}): this program runs "
              "only as a service.", file=sys.stderr)
        return NOT_A_SERVICE
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        try:
            with open(RESULTS / f"crash-{os.getpid()}.txt", "x", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except OSError:
            pass
        raise
    raise SystemExit(code)
