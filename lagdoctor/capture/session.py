"""One capture session: every collector on one clock, a live status line, then one Capture.

The session owns no measurement itself. It decides which collectors to run,
starts them with the same origin, watches for the end (duration, Ctrl+C, the
game exiting), stops them in parallel so one slow stop cannot stretch the
others, and copies each collector's result() into the Capture exactly as the
table in docs/DESIGN.md says. A collector that crashes or cannot start leaves
its status in Capture.sources; the session carries on with the rest.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from lagdoctor import __version__, clock
from lagdoctor.capture.base import Collector
from lagdoctor.model import Capture, SourceStatus

STATUS_PERIOD_S = 1.0
STOP_TIMEOUT_S = {"frames": 25.0}          # PresentMon's stop can take two 10 s waits (DESIGN: Collector contract)
DEFAULT_STOP_TIMEOUT_S = 5.0

# Which result() keys go where (docs/DESIGN.md, "What each collector's result() returns").
NETWORK_FLOW_KEYS = ("targets", "probes", "lost", "wifi_scans", "route", "downloads")
FLOW_KEYS = ("server", "talkers", "gaps", "server_rx", "layout", "health")


@dataclass
class SessionOptions:
    duration_s: float | None = None        # None: until Ctrl+C, the stop event, or the game exits
    network: bool = True
    server_ip: str | None = None
    hotkey: str | None = "ctrl+alt+l"
    presentmon_exe: Path | None = None
    frames: bool = True                    # False when the user chose to run without ETW rights


class Session:
    """Run one capture. Tests inject collectors, the inventory and the liveness check."""

    def __init__(self, target: dict[str, Any], opts: SessionOptions,
                 collectors: list[Collector] | None = None,
                 inventory: dict[str, Any] | None = None,
                 alive: Callable[[], bool] | None = None,
                 hotkey_factory: Callable[..., Any] | None = None,
                 printer: Callable[[str], None] | None = None) -> None:
        self.target = dict(target)
        self.opts = opts
        self.inventory = dict(inventory) if inventory is not None else None
        self.collectors = collectors
        self.alive = alive
        self.hotkey_factory = hotkey_factory
        self.printer = printer or (lambda line: print(line, end="", flush=True))
        self.marks: list[float] = []
        self.notes: list[str] = []
        self._marks_lock = threading.Lock()
        self.origin = 0.0
        self.stop_reason = ""

    # ------------------------------------------------------------------ run --
    def run(self, stop: threading.Event | None = None) -> Capture:
        stop = stop or threading.Event()
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
        for c in self.collectors:
            try:
                c.start(self.origin)
            except Exception as exc:  # noqa: BLE001 - one collector must not stop the capture
                c.set_status("failed", f"could not start: {type(exc).__name__}: {exc}")
        hotkey = self._start_hotkey()
        try:
            self._watch(stop)
        finally:
            if hotkey is not None:
                hotkey.stop()
            self._stop_all()
        end = clock.now()
        return self._assemble(started_at, end - self.origin)

    def _start_hotkey(self):
        if not self.opts.hotkey:
            return None
        factory = self.hotkey_factory
        if factory is None:
            from lagdoctor.winapi.hotkey import HotkeyListener
            factory = HotkeyListener
        listener = factory(self.opts.hotkey, self._on_mark)
        if listener.start():
            return listener
        why = getattr(listener, "error", "") or "unknown reason"
        self.notes.append(f"The lag key {self.opts.hotkey} could not be registered ({why}); no marks were recorded.")
        return None

    def _on_mark(self, when: float) -> None:
        with self._marks_lock:
            self.marks.append(round(when - self.origin, 3))

    def _watch(self, stop: threading.Event) -> None:
        try:
            while True:
                elapsed = clock.now() - self.origin
                if stop.is_set():
                    self.stop_reason = "stopped"
                    return
                if self.opts.duration_s is not None and elapsed >= self.opts.duration_s:
                    self.stop_reason = "duration"
                    return
                if not self.alive():
                    self.stop_reason = "game exited"
                    self.notes.append(f"The game exited after {elapsed:.0f} s, so the capture ended there.")
                    return
                self._route_server()
                self.printer("\r" + status_line(elapsed, {c.name: c.live() for c in self.collectors},
                                                self.opts.hotkey) + " ")
                wait = STATUS_PERIOD_S
                if self.opts.duration_s is not None:
                    wait = max(0.0, min(wait, self.opts.duration_s - elapsed))
                stop.wait(wait)
        except KeyboardInterrupt:
            self.stop_reason = "Ctrl+C"
        finally:
            self.printer("\n")

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
            sources["frames"] = SourceStatus("off", "frame timing was skipped: it needs admin rights or "
                                                    "Performance Log Users (see `lagdoctor setup`)")

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

def status_line(elapsed: float, live: dict[str, dict[str, Any]], hotkey: str | None) -> str:
    """One short line, refreshed once a second. Unknown values show as '-'."""
    m, s = divmod(int(elapsed), 60)
    parts = [f"{m}:{s:02d}"]
    fr = live.get("frames") or {}
    fps = fr.get("fps")
    parts.append(f"{fps:.0f} fps" if isinstance(fps, (int, float)) else "fps -")
    sysl = live.get("system") or {}
    cpu, gpu = sysl.get("game_top_thread_pct"), sysl.get("game_gpu_pct")
    parts.append(f"main thread {cpu:.0f}%" if isinstance(cpu, (int, float)) else "main thread -")
    parts.append(f"GPU {gpu:.0f}%" if isinstance(gpu, (int, float)) else "GPU -")
    net = live.get("network") or {}
    ping = net.get("inet_rtt_ms")
    parts.append(f"ping {ping:.0f} ms" if isinstance(ping, (int, float)) else "ping -")
    keys = "Ctrl+C stops"
    if hotkey:
        keys = f"{hotkey.title()} = it lagged now, " + keys
    return " | ".join(parts) + f"   ({keys})"


# --------------------------------------------------------------- defaults --

def default_collectors(target: dict[str, Any], inventory: dict[str, Any], opts: SessionOptions) -> list[Collector]:
    """The collectors a real capture runs, per docs/DESIGN.md."""
    from lagdoctor.capture import target as target_mod
    from lagdoctor.capture.sysmon import SystemSampler

    out: list[Collector] = [SystemSampler(target)]
    if opts.frames:
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
        out.append(NetworkSampler(server_ip=opts.server_ip))
        # Polled once a second, so child processes the game starts later join its traffic.
        out.append(FlowTracker(lambda: target_mod.game_pids(pid, created)))
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
