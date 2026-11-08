"""One capture session: every collector on one clock, a live status line, then one Capture.

The session owns no measurement itself. It decides which collectors to run,
starts them with the same origin, watches for the end (duration, Ctrl+C, the
game exiting), stops them in parallel so one slow stop cannot stretch the
others, and copies each collector's result() into the Capture exactly as the
table in docs/DESIGN.md says. A collector that crashes or cannot start leaves
its status in Capture.sources; the session carries on with the rest.

Once anything has started, everything is stopped, whatever happens. On the main
thread Ctrl+C never raises inside the session: the first press stops the
recording and later ones are only counted (CtrlC), so a second press while the
collectors stop cannot lose the capture.

lag-doctor's own processes go into target["tool_pids"] (handle-free snapshots,
once the collectors have started their children and again just before they are
stopped), so the analysis never blames them for the lag: this process and what
it started, and each tool root it was given (the relay) with what that started.

With a relay (relay/client.py), frame timing and the game-server tracker run in
it, with administrator rights; their collectors here are fed from its pipe.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from lagdoctor import __version__, clock
from lagdoctor.capture.base import Collector
from lagdoctor.model import Capture, SourceStatus

STATUS_PERIOD_S = 1.0
STATUS_MIN_WIDTH = 20       # a narrower console still gets the elapsed time and the frame rate
STOP_TIMEOUT_S = {"frames": 25.0}          # PresentMon's stop can take two 10 s waits (DESIGN: Collector contract)
DEFAULT_STOP_TIMEOUT_S = 5.0
STOPPING_MESSAGE = "Stopping and saving the capture (Ctrl+C will not lose it now)..."
STILL_STOPPING_MESSAGE = ("Still stopping and saving the capture; Ctrl+C will not lose it. "
                          "(To abandon it, close this window.)")
# One capture at a time per Windows session. "Local\" is the namespace of this sign-in session, shared by
# every account and elevation running in it, so an unelevated watch and an elevated capture child meet here.
CAPTURE_LOCK_NAME = "Local\\lag-doctor-capture"

# Which result() keys go where (docs/DESIGN.md, "What each collector's result() returns").
NETWORK_FLOW_KEYS = ("targets", "probes", "lost", "wifi_scans", "route", "downloads", "download_sources")
FLOW_KEYS = ("server", "talkers", "talkers_scope", "gaps", "server_rx", "layout", "health")
# lag-doctor's own processes are first listed at the first status tick this far in, once the collectors have
# had a status period to start their children (PresentMon, nvidia-smi), and again just before they stop.
TOOL_SIGHT_S = 0.5
MAX_TOOL_PIDS = 64                         # bounded: lag-doctor itself starts only a handful
# Frame timing is on, the game window was in front this long, and not one frame came: say so at once, while
# the player can still stop and fix it, not in the report afterwards. A minimised game draws nothing, so the
# time counts only while the game is in front.
NO_FRAMES_WARN_S = 10.0
FRAMES_FAILED = ("failed", "unavailable")   # PresentMon's states that mean no more frames will come


@dataclass
class SessionOptions:
    duration_s: float | None = None        # None: until Ctrl+C, the stop event, or the game exits
    network: bool = True
    server_ip: str | None = None
    hotkey: str | None = "ctrl+alt+l"
    presentmon_exe: Path | None = None
    frames: bool = True                    # False when the user chose to run without ETW rights
    steam_log: str | Path | None = None    # None: look it up in this account's HKCU; "": none; a path: that file
    # lag-doctor's processes outside this one, as (pid, create_time): the relay. Each counts, with every process it
    # started (its PresentMon), while that exact process still runs.
    tool_procs: tuple[tuple[int, int], ...] = ()
    # A relay/client.RelayLink: frame timing and the game-server tracker come through it (whichever it runs).
    relay: Any = None


def request_stop(stop: threading.Event, reason: str) -> None:
    """Ask a session to stop, saying why (the first reason given wins; it becomes target["stop_reason"])."""
    if not stop.is_set() and not getattr(stop, "reason", ""):
        stop.reason = reason
    stop.set()


class CtrlC:
    """Ctrl+C while a capture runs. Python can only set a signal handler on the main thread; elsewhere this
    does nothing (and Ctrl+C never reaches other threads anyway).

    The first press asks the recording to stop, through `stop`. Once the stop has begun (`stopping`), every
    press is only counted and answered, so a second Ctrl+C can never lose the capture. Leaving the block
    puts the previous handler back; presses counted meanwhile are not replayed.
    """

    def __init__(self, stop: threading.Event, say: Callable[[str], None] | None = None) -> None:
        self.stop = stop
        self.say = say
        self.presses = 0
        self.stopping = False
        self.installed = False
        self._old: Any = None

    def __enter__(self) -> "CtrlC":
        if threading.current_thread() is threading.main_thread():
            self._old = signal.signal(signal.SIGINT, self._on_sigint)
            self.installed = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self.installed:
            # None = the old handler was not set from Python; Python's own default is the best match.
            signal.signal(signal.SIGINT, self._old if self._old is not None else signal.default_int_handler)
            self.installed = False

    def _on_sigint(self, signum: int, frame: object) -> None:
        self.presses += 1
        if not self.stopping and not self.stop.is_set():
            request_stop(self.stop, "Ctrl+C")
            return
        if self.say is not None:
            # Not printed here: the handler may have interrupted a print, and a nested write would raise.
            threading.Thread(target=self.say, args=(STILL_STOPPING_MESSAGE,), name="lagdoctor-ctrl-c",
                             daemon=True).start()


class CaptureLock:
    """One capture at a time in this Windows session: a named mutex held for the whole recording."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name if name is not None else CAPTURE_LOCK_NAME
        self._handle: int | None = None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another recording holds it.
        Raises OSError when Windows could not say (so the caller refuses rather than guesses)."""
        from lagdoctor.winapi.elevation import hold_named_mutex
        if self._handle is None:
            self._handle = hold_named_mutex(self.name)
        return self._handle is not None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def release(self) -> None:
        from lagdoctor.winapi.elevation import release_named_mutex
        handle, self._handle = self._handle, None
        release_named_mutex(handle)

    def __enter__(self) -> "CaptureLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class Session:
    """Run one capture. Tests inject collectors, the inventory, the liveness check and the process snapshot."""

    def __init__(self, target: dict[str, Any], opts: SessionOptions,
                 collectors: list[Collector] | None = None,
                 inventory: dict[str, Any] | None = None,
                 alive: Callable[[], bool] | None = None,
                 hotkey_factory: Callable[..., Any] | None = None,
                 printer: Callable[[str], None] | None = None,
                 processes: Callable[[], dict[int, Any]] | None = None) -> None:
        self.target = dict(target)
        self.opts = opts
        self.inventory = dict(inventory) if inventory is not None else None
        self.collectors = collectors
        self.alive = alive
        self.hotkey_factory = hotkey_factory
        self.printer = printer or (lambda line: print(line, end="", flush=True))
        self.processes = processes
        self.marks: list[float] = []
        self.notes: list[str] = []
        self._marks_lock = threading.Lock()
        self.origin = 0.0
        self.stop_reason = ""
        self._printer_broken = False
        self._tools: dict[int, None] = {}          # lag-doctor's own PIDs seen so far (an ordered set)
        self._tools_listed = False
        self._tools_error = ""
        self._first_sighting_done = False
        self._frames_warned = False                # the "frame timing is not working" line is said once
        self._front_without_frames = 0.0           # seconds the game was in front with no frame yet
        self._frames_checked_at: float | None = None

    # ------------------------------------------------------------------ run --
    def run(self, stop: threading.Event | None = None, ctrl_c: CtrlC | None = None) -> Capture:
        """Record until the duration, `stop`, Ctrl+C or the game's exit; then stop everything and assemble.

        `ctrl_c`: the caller's own CtrlC (entered, made with this `stop`), to keep the protection past the
        return, e.g. over the save. Without it the session installs one for the length of run().
        """
        stop = stop if stop is not None else threading.Event()
        if ctrl_c is not None and ctrl_c.stop is not stop:
            raise ValueError("ctrl_c must be made with the same stop event")
        guard = ctrl_c if ctrl_c is not None else CtrlC(stop, lambda text: self._print(text + "\n"))
        with (contextlib.nullcontext(guard) if ctrl_c is not None else guard):
            if self.inventory is None:
                self.inventory = _collect_inventory(self.target)
            for section, why in sorted((self.inventory.get("errors") or {}).items()):
                self.notes.append(f"System details: {section} could not be read ({why}).")
            if self.collectors is None:
                self.collectors = default_collectors(self.target, self.inventory, self.opts)
            if self.alive is None:
                self.alive = _default_alive(self.target)

            self.origin = clock.now()
            started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            hotkey = None
            # From the first start on, whatever happens (an exception, a KeyboardInterrupt), everything stops.
            try:
                self._start_collectors()
                hotkey = self._start_hotkey()
                self._watch(stop)
            finally:
                guard.stopping = True
                self._print(STOPPING_MESSAGE + "\n")
                if hotkey is not None:
                    self._stop_hotkey(hotkey)
                self._sight_tools()                   # before the stop ends PresentMon and nvidia-smi
                self._stop_all()
            end = clock.now()
            return self._assemble(started_at, end - self.origin)

    def _start_collectors(self) -> None:
        for c in self.collectors:
            try:
                c.start(self.origin)
            except Exception as exc:  # noqa: BLE001 - one collector must not stop the capture
                c.set_status("failed", f"could not start: {type(exc).__name__}: {exc}")

    def _start_hotkey(self):
        if not self.opts.hotkey:
            return None
        factory = self.hotkey_factory
        if factory is None:
            from lagdoctor.winapi.hotkey import HotkeyListener
            factory = HotkeyListener
        try:
            listener = factory(self.opts.hotkey, self._on_mark)
            started = listener.start()
        except Exception as exc:  # noqa: BLE001 - a bad key (the CLI checks it first) must not end the capture
            self.notes.append(f"The lag key {self.opts.hotkey} could not be used ({type(exc).__name__}: {exc}); "
                              "no marks were recorded.")
            return None
        if started:
            return listener
        why = getattr(listener, "error", "") or "unknown reason"
        self.notes.append(f"The lag key {self.opts.hotkey} could not be registered ({why}); no marks were recorded.")
        return None

    def _stop_hotkey(self, listener) -> None:
        try:
            listener.stop()
        except Exception as exc:  # noqa: BLE001 - the collectors must still be stopped
            self.notes.append(f"The lag key could not be released cleanly ({type(exc).__name__}: {exc}).")

    def _sight_tools(self) -> None:
        """Add the lag-doctor processes running now (one handle-free snapshot) to target["tool_pids"]."""
        try:
            procs = self.processes() if self.processes is not None else _snapshot()
            for pid in tool_pids(procs, self.target, self.opts.tool_procs):
                if len(self._tools) < MAX_TOOL_PIDS:
                    self._tools.setdefault(pid)
            self._tools_listed = True
        except Exception as exc:  # noqa: BLE001 - the capture goes on; the analysis is told below
            self._tools_error = self._tools_error or f"{type(exc).__name__}: {exc}"

    def _print(self, text: str) -> None:
        """The status line and messages. A console that went away (its window closed) must not end the capture."""
        if self._printer_broken:
            return
        try:
            self.printer(text)
        except Exception as exc:  # noqa: BLE001
            self._printer_broken = True
            self.notes.append(f"The status line stopped: the console could not be written to ({type(exc).__name__}).")

    def _on_mark(self, when: float) -> None:
        with self._marks_lock:
            self.marks.append(round(when - self.origin, 3))

    def _watch(self, stop: threading.Event) -> None:
        try:
            while True:
                elapsed = clock.now() - self.origin
                if stop.is_set():
                    self.stop_reason = getattr(stop, "reason", "") or "stopped"
                    return
                if self.opts.duration_s is not None and elapsed >= self.opts.duration_s:
                    self.stop_reason = "duration"
                    return
                if not self.alive():
                    self.stop_reason = "game exited"
                    self.notes.append(f"The game exited after {elapsed:.0f} s, so the capture ended there.")
                    return
                if not self._first_sighting_done and elapsed >= TOOL_SIGHT_S:
                    self._first_sighting_done = True
                    self._sight_tools()
                self._route_server()
                live = {c.name: c.live() for c in self.collectors}
                self._check_frames(live, elapsed)
                frames = self._by_name("frames")
                if frames is not None and frames.ended:
                    live["frames"] = {}             # PresentMon is gone: no frame rate frozen at its last value
                self._print("\r" + status_line(elapsed, live, console_width()))
                wait = STATUS_PERIOD_S
                if self.opts.duration_s is not None:
                    wait = max(0.0, min(wait, self.opts.duration_s - elapsed))
                stop.wait(wait)
        except KeyboardInterrupt:
            self.stop_reason = "Ctrl+C"
        finally:
            self._print("\n")

    def _check_frames(self, live: dict[str, dict[str, Any]], elapsed: float) -> None:
        """Say once, on its own line, that frame timing is not working: PresentMon failed or ended mid-recording
        (crashed, or its trace session stopped from outside: "partial" once frames had come), or the game was in
        front for NO_FRAMES_WARN_S without one frame. An end in "ok" is the game's own exit, not a fault. A game that
        freezes in front sends no frames either, so frames that stop while PresentMon still runs are never a fault:
        that is a stutter, measured when it ends. Everything else goes on being recorded."""
        c = self._by_name("frames")
        if c is None or self._frames_warned:
            return
        step = 0.0 if self._frames_checked_at is None else max(0.0, elapsed - self._frames_checked_at)
        self._frames_checked_at = elapsed
        st = c.status()
        frames = (live.get("frames") or {}).get("frames")
        if st.state in FRAMES_FAILED or (c.ended and st.state != "ok"):
            why = (st.detail or "PresentMon stopped").rstrip(". ")       # its reason already names PresentMon
        elif isinstance(frames, int) and frames > 0:
            return
        else:
            if (live.get("system") or {}).get("game_focused") == 1:
                self._front_without_frames += step
            if self._front_without_frames < NO_FRAMES_WARN_S:
                return
            why = (f"the game was in front for {self._front_without_frames:.0f} s and PresentMon saw no frame "
                   "from it")
        self._frames_warned = True
        self._print(f"\nFrame timing is not working: {why}. Everything else is still being recorded; stop with "
                    "Ctrl+C if you need frame timing.\n")

    def _route_server(self) -> None:
        """When the flow tracker finds the game server, the network sampler starts pinging it too."""
        flows = self._by_name("net_flows")
        net = self._by_name("network")
        if flows is None or net is None or not hasattr(net, "set_server"):
            return
        ip = flows.live().get("server_ip")
        if ip and ip != getattr(self, "_server_routed", None):
            self._server_routed = ip
            try:
                net.set_server(ip)
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"Could not start pinging the game server {ip}: {exc}")

    def _stop_all(self) -> None:
        """Stop every collector at once; each stop is bounded by its own timeout."""
        threads = []
        for c in self.collectors or []:
            timeout = STOP_TIMEOUT_S.get(c.name, DEFAULT_STOP_TIMEOUT_S)
            t = threading.Thread(target=_stop_quietly, args=(c, timeout), name=f"stop-{c.name}", daemon=True)
            t.start()
            threads.append((c, t, timeout))
        for c, t, timeout in threads:
            t.join(timeout + 2.0)
            if t.is_alive():
                c.set_status("partial", f"{c.name} did not stop within {timeout:.0f} s; its data may be cut short")

    def _by_name(self, name: str) -> Collector | None:
        return next((c for c in self.collectors or [] if c.name == name), None)

    # ------------------------------------------------------------- assemble --
    def _assemble(self, started_at: str, duration_s: float) -> Capture:
        sources: dict[str, SourceStatus] = {}
        results: dict[str, dict[str, Any]] = {}
        for c in self.collectors or []:
            try:
                results[c.name] = c.result() or {}
            except Exception as exc:  # noqa: BLE001
                results[c.name] = {}
                c.set_status("failed", f"its results could not be read: {type(exc).__name__}: {exc}")
            sources[c.name] = c.status()
        if not self.opts.frames and "frames" not in sources:
            # the report shows it beside "frame timing (PresentMon):", so it never repeats those words
            sources["frames"] = SourceStatus("off", "it needs admin rights or Performance Log Users (see "
                                                    "`lagdoctor setup`)")
        if not self.opts.network:            # switched off on purpose: say so, not "not recorded" as if it failed
            for name in ("network", "net_flows"):
                sources.setdefault(name, SourceStatus("off", "turned off for this recording (--no-network)"))

        target = dict(self.target)
        inventory = dict(self.inventory or {})
        net_flows: dict[str, Any] = {}

        fr = results.get("frames", {})
        if fr:
            pm = dict(fr.get("presentmon") or {})
            for k in ("swap_chains", "main_chain"):
                if k in fr:
                    pm[k] = fr[k]
            target["presentmon"] = pm
        sysr = results.get("system", {})
        if sysr.get("game_adapter_luid"):
            target["adapter_luid"] = sysr["game_adapter_luid"]
        if sysr.get("game_pids"):
            target["pids"] = sorted(sysr["game_pids"])
        gpur = results.get("gpu", {})
        if gpur.get("device"):
            inventory["nvidia"] = gpur["device"]
        netr = results.get("network", {})
        if netr.get("interface"):
            inventory["network"] = netr["interface"]
        for k in NETWORK_FLOW_KEYS:
            if k in netr:
                net_flows[k] = netr[k]
        flowr = results.get("net_flows", {})
        for k in FLOW_KEYS:
            if k in flowr:
                net_flows[k] = flowr[k]
        if self.stop_reason:
            target["stop_reason"] = self.stop_reason
        if self._tools_listed:
            target["tool_pids"] = sorted(self._tools)
        elif self._tools_error:
            self.notes.append(f"lag-doctor could not list its own processes ({self._tools_error}), so the analysis "
                              "may name them among the other programs.")

        with self._marks_lock:
            marks = sorted(m for m in self.marks if 0 <= m <= duration_s + 1.0)
        return Capture(
            started_at=started_at, clock_origin=self.origin, duration_s=round(duration_s, 3),
            target=target, sources=sources, inventory=inventory,
            frames=fr.get("columns") or {}, system=sysr.get("columns") or {}, gpu=gpur.get("columns") or {},
            network=netr.get("columns") or {}, others=sysr.get("others") or [], net_flows=net_flows,
            marks=marks, notes=list(self.notes), tool_version=__version__)


def _stop_quietly(c: Collector, timeout: float) -> None:
    try:
        c.stop(timeout)
    except TypeError:                 # a collector whose stop() takes no timeout
        c.stop()
    except Exception as exc:  # noqa: BLE001 - stop must never raise into the session
        c.set_status("partial", f"error while stopping: {type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ status --

def console_width() -> int:
    """The console's width in columns now (a window can be resized while it records); 80 without a console."""
    try:
        return max(STATUS_MIN_WIDTH, shutil.get_terminal_size((80, 24)).columns)
    except (OSError, ValueError):
        return 80


def status_line(elapsed: float, live: dict[str, dict[str, Any]], width: int | None = None) -> str:
    """One short line, refreshed once a second. Unknown values show as '-'; a source this recording does not
    run at all (frame timing off, --no-network) as 'off', so a dash never stands for something never coming.
    With `width`, whole readouts are dropped from the end until it fits width - 1 columns, and it is padded to them:
    a line as wide as the console wraps, "\\r" then rewrites only its last row, and the screen scrolls a row a second
    (95 characters once, in a 71-column window). The keys are in the "Recording." line printed once before it."""
    m, s = divmod(int(elapsed), 60)
    parts = [f"{m}:{s:02d}"]
    fps = (live.get("frames") or {}).get("fps")
    if "frames" not in live:
        parts.append("fps off")
    else:
        parts.append(f"{fps:.0f} fps" if isinstance(fps, (int, float)) else "fps -")
    sysl = live.get("system") or {}
    cpu, gpu = sysl.get("game_top_thread_pct"), sysl.get("game_gpu_pct")
    parts.append(f"main thread {cpu:.0f}%" if isinstance(cpu, (int, float)) else "main thread -")
    parts.append(f"GPU {gpu:.0f}%" if isinstance(gpu, (int, float)) else "GPU -")
    ping = (live.get("network") or {}).get("inet_rtt_ms")
    if "network" not in live:
        parts.append("ping off")
    else:
        parts.append(f"ping {ping:.0f} ms" if isinstance(ping, (int, float)) else "ping -")
    if not width:
        return " | ".join(parts)
    room = max(1, width - 1)
    while len(parts) > 1 and len(" | ".join(parts)) > room:
        parts.pop()
    return " | ".join(parts)[:room].ljust(room)


# --------------------------------------------------------------- defaults --

def default_collectors(target: dict[str, Any], inventory: dict[str, Any], opts: SessionOptions) -> list[Collector]:
    """The collectors a real capture runs, per docs/DESIGN.md."""
    from lagdoctor.capture import target as target_mod
    from lagdoctor.capture.sysmon import SystemSampler

    out: list[Collector] = [SystemSampler(target)]
    relay = opts.relay
    if opts.frames and relay is not None and relay.frames:
        out.append(relay.frames_collector(target["pid"]))
    elif opts.frames:
        from lagdoctor.capture.presentmon import PresentMonSource
        # PresentMon's own --terminate_on_proc_exit cannot fire if anti-cheat strips its handle, so it is
        # also told when the game ends, by the same handle-free check the session uses.
        out.append(PresentMonSource(target["pid"], exe=opts.presentmon_exe, is_alive=_default_alive(target)))
    adapter = _game_adapter(target, inventory)
    if _has_nvidia(inventory) and (adapter is None or adapter.get("vendor") == "nvidia"):
        from lagdoctor.capture.gpumon import NvidiaSampler
        out.append(NvidiaSampler(adapter))           # None = adapter unknown yet: watch the NVIDIA GPU anyway
    if opts.network:
        from lagdoctor.capture.netflows import FlowTracker
        from lagdoctor.capture.netmon import NetworkSampler
        pid, created = target["pid"], target.get("create_time")
        out.append(NetworkSampler(server_ip=opts.server_ip, steam_log=opts.steam_log))
        # Polled once a second, so child processes the game starts later join its traffic.
        out.append(FlowTracker(lambda: target_mod.game_pids(pid, created),
                               relay=relay if relay is not None and relay.network else None))
    return out


def _game_adapter(target: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any] | None:
    luid = target.get("adapter_luid")
    return next((a for a in inventory.get("adapters") or [] if luid and a.get("luid") == luid), None)


def _has_nvidia(inventory: dict[str, Any]) -> bool:
    return any(a.get("vendor") == "nvidia" for a in inventory.get("adapters") or [])


def _collect_inventory(target: dict[str, Any]) -> dict[str, Any]:
    from lagdoctor.capture import inventory
    names: Iterable[str] | None = None
    try:
        from lagdoctor.winapi.ntquery import Snapshotter
        names = sorted({p.name for p in Snapshotter().snapshot().values() if p.name})
    except Exception:  # noqa: BLE001 - OEM tool names are a nice-to-have
        names = None
    return inventory.collect(target, process_names=names)


_snapshot_lock = threading.Lock()
_snapshotter: Any = None


def _snapshot() -> dict[int, Any]:
    """Every process, from one NtQuerySystemInformation call: no process is opened (CLAUDE.md)."""
    global _snapshotter
    from lagdoctor.winapi.ntquery import Snapshotter
    with _snapshot_lock:
        if _snapshotter is None:
            _snapshotter = Snapshotter()
    return _snapshotter.snapshot()


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def own_processes(procs: dict[int, Any] | None = None, *, me: int | None = None,
                  image_path: Callable[[int], str | None] | None = None,
                  launcher: str | None = None) -> list[tuple[int, int]]:
    """(pid, create_time) of this process and of the venv launcher that started it, handle-free. A venv's
    python.exe is a launcher that waits while the base interpreter runs lag-doctor; sys.executable names it.
    OSError when Windows cannot list the processes."""
    from lagdoctor.winapi import ntquery
    procs = _snapshot() if procs is None else procs
    me = os.getpid() if me is None else me
    image_path = image_path or ntquery.image_path
    launcher = sys.executable if launcher is None else launcher

    def image(pid: int) -> str | None:
        try:
            return image_path(pid)
        except OSError:
            return None                          # unknown: never taken for the launcher
    p = procs.get(me)
    if p is None:
        return []
    out = [(me, p.create_time)]
    parent = procs.get(p.ppid)
    if parent is not None and parent.pid != me and parent.create_time <= p.create_time and launcher:
        theirs, mine = image(parent.pid), image(me)
        if theirs and _same_file(theirs, launcher) and not (mine and _same_file(mine, launcher)):
            out.append((parent.pid, parent.create_time))
    return out


def tool_pids(procs: dict[int, Any], target: dict[str, Any], extra: Iterable[tuple[int, int]] = (), *,
              me: int | None = None, image_path: Callable[[int], str | None] | None = None,
              launcher: str | None = None) -> list[int]:
    """lag-doctor's own processes in one snapshot, ours first: own_processes(), each `extra` (pid, create_time)
    still running as that process (the relay), and the processes lag-doctor and those run (PresentMon, nvidia-smi),
    but never the game's own process tree, even when lag-doctor started the game (scripts/e2e.py runs LagLab)."""
    from lagdoctor.capture.target import descendants
    own = own_processes(procs, me=me, image_path=image_path, launcher=launcher)
    if not own:
        return []
    out = dict.fromkeys(pid for pid, _ in own)
    roots = [own[0]]
    for pid, created in extra:
        p = procs.get(pid)
        if p is not None and p.create_time == created:
            out.setdefault(pid)
            roots.append((pid, created))
    game, created = target.get("pid"), target.get("create_time")
    theirs = descendants(procs, game, created) if isinstance(game, int) and isinstance(created, int) else set()
    for root, root_created in roots:
        for pid in sorted(descendants(procs, root, root_created) - theirs):
            out.setdefault(pid)
    return list(out)


def _default_alive(target: dict[str, Any]) -> Callable[[], bool]:
    """Is the game still running? When Windows cannot answer one second, assume yes and ask again
    the next: ending (and possibly losing) a long capture over one failed snapshot would be worse."""
    from lagdoctor.capture.target import is_alive
    pid, created = target["pid"], target.get("create_time")

    def alive() -> bool:
        try:
            return is_alive(pid, created)
        except OSError:
            return True
    return alive


def session_dir(base: Path, target: dict[str, Any], when: datetime | None = None) -> Path:
    """A fresh folder per capture: <base>/<yyyymmdd-hhmmss>-<game>, never one that already exists."""
    when = when or datetime.now()
    if not isinstance(target.get("name"), str):
        target = dict(target, name=None)          # a capture from elsewhere may hold any JSON value here
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (target.get("name") or "game"))
    stem = stem.rsplit(".", 1)[0].strip("._")[:40] or "game"
    Path(base).mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        d = Path(base) / (f"{when:%Y%m%d-%H%M%S}-{stem}" + (f"-{n}" if n > 1 else ""))
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise FileExistsError(f"no free session folder name under {base}")
