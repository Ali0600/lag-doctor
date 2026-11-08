"""Choose the game process to watch, and follow it, without ever opening a handle to it.

Process identity comes from NtQuerySystemInformation snapshots (winapi/ntquery.py): a process is the pair
(pid, create_time), because Windows reuses PIDs. GPU use comes from the GPU Engine performance counters,
whose instance names carry the PID and the adapter LUID (docs/research/windows-telemetry.md (WT) 2.2).

`auto` mode picks the process with the most 3D-engine use among those that own a visible, uncloaked
top-level window. The window matters: CUDA compute is booked to the 3D engine too (WT verify log 1, 2.2),
so a busy 3D engine alone does not make a process a game; this machine's ML training job is exactly that.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from lagdoctor.winapi import ntquery, windows
from lagdoctor.winapi.ntquery import ProcInfo, Snapshotter
from lagdoctor.winapi.pdh import PdhQuery

GPU_ENGINE_UTIL = r"\GPU Engine(*)\Utilization Percentage"

SAMPLE_PERIOD_S = 0.25
MAX_SAMPLE_S = 30.0
# Auto mode needs at least this much 3D-engine use (% of the engine, mean over the sample) before it calls a
# windowed process the game, and treats candidates within TIE_PCT of the best as tied (ties go to the
# foreground window). Both are [inferred] starting points: a game renders even in its menus, an idle
# desktop app sits near 0 %. Tune against real captures, never silently.
AUTO_MIN_3D_PCT = 2.0
TIE_PCT = 1.0
MAX_CANDIDATES = 10

# Programs that draw windows with the GPU but are never the game (lower-case image names). The tool itself
# is excluded by PID (it is python.exe, and so is LagLab, the test game).
IGNORE_NAMES = frozenset({
    # Windows shell and system UI
    "dwm.exe", "explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe",
    "searchapp.exe", "searchui.exe", "textinputhost.exe", "applicationframehost.exe", "lockapp.exe",
    "systemsettings.exe", "widgets.exe", "widgetboard.exe", "taskmgr.exe", "sihost.exe", "ctfmon.exe",
    "csrss.exe", "winlogon.exe", "fontdrvhost.exe", "rundll32.exe", "phoneexperiencehost.exe",
    "securityhealthsystray.exe", "screenclippinghost.exe", "snippingtool.exe", "magnify.exe", "mstsc.exe",
    # consoles and terminals
    "conhost.exe", "openconsole.exe", "windowsterminal.exe", "wt.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    # browsers and web views
    "chrome.exe", "msedge.exe", "msedgewebview2.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe",
    "vivaldi.exe", "arc.exe", "iexplore.exe",
    # launchers and stores (the game they start is a separate process)
    "steam.exe", "steamwebhelper.exe", "steamservice.exe", "epicgameslauncher.exe", "epicwebhelper.exe",
    "battle.net.exe", "eadesktop.exe", "upc.exe", "ubisoftconnect.exe", "galaxyclient.exe",
    "riotclientservices.exe", "riotclientux.exe", "xboxpcapp.exe", "gamingservices.exe",
    # chat, capture, overlays, OEM and vendor tools
    "discord.exe", "obs64.exe", "obs32.exe", "obs.exe", "medal.exe", "nvidia overlay.exe", "nvidia app.exe",
    "nvidia share.exe", "nvcontainer.exe", "gamebar.exe", "gamebarftserver.exe", "overwolf.exe",
    "armourycrate.exe", "armourycrate.usersessionhelper.exe", "teams.exe", "ms-teams.exe", "slack.exe",
    "zoom.exe", "spotify.exe", "whatsapp.exe", "telegram.exe",
    # editors and assistants that render with the GPU
    "code.exe", "cursor.exe", "devenv.exe", "claude.exe",
})
# A console window is drawn by conhost, and Windows reports its client program as the owner: it never makes
# that program a game (a training job started from a console window would otherwise "own" a window).
CONSOLE_CLASSES = frozenset({"ConsoleWindowClass", "PseudoConsoleWindow"})


class TargetNotFound(Exception):
    """No process could be chosen; the message says why. `candidates` lists what was considered."""

    def __init__(self, reason: str, candidates: list[dict] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.candidates = candidates or []


# ---------------------------------------------------------------- GPU counter instance names ---

def luid_text(high: int, low: int) -> str:
    """The contract's LUID form: "0xHHHHHHHH_0xLLLLLLLL", lower-case, HighPart first (DESIGN.md, dxgi)."""
    return f"0x{high & 0xFFFFFFFF:08x}_0x{low & 0xFFFFFFFF:08x}"


def _hex32(text: str) -> int:
    if not text.lower().startswith("0x") or not 3 <= len(text) <= 10:
        raise ValueError(text)
    return int(text, 16)


def parse_engine(name: str) -> tuple[int, str, int, int, str] | None:
    """'pid_656_luid_0x00000000_0x00010F83_phys_0_eng_0_engtype_3D' -> (656, '0x00000000_0x00010f83', 0, 0, '3D').

    WT 2.2: split at most 9 times, because the engine type can be empty, hold '_' (OFA_0) or a space
    (GDI Render). PDH returns the LUID in upper case here; it is normalised. None for anything else.
    """
    parts = name.split("_", 9)
    if (len(parts) != 10 or parts[0] != "pid" or parts[2] != "luid" or parts[5] != "phys" or parts[7] != "eng"
            or not parts[9].startswith("engtype_")):
        return None
    try:
        return (int(parts[1]), luid_text(_hex32(parts[3]), _hex32(parts[4])), int(parts[6]), int(parts[8]),
                parts[9][len("engtype_"):])
    except ValueError:
        return None


def parse_adapter_memory(name: str) -> tuple[str, int] | None:
    """'luid_0x00000000_0x00010F83_phys_0' -> ('0x00000000_0x00010f83', 0)  (\\GPU Adapter Memory(*))."""
    parts = name.split("_")
    if len(parts) != 5 or parts[0] != "luid" or parts[3] != "phys":
        return None
    try:
        return luid_text(_hex32(parts[1]), _hex32(parts[2])), int(parts[4])
    except ValueError:
        return None


def parse_process_memory(name: str) -> tuple[int, str, int] | None:
    """'pid_656_luid_0x00000000_0x00010F83_phys_0' -> (656, '0x00000000_0x00010f83', 0)  (\\GPU Process Memory(*))."""
    parts = name.split("_")
    if len(parts) != 7 or parts[0] != "pid" or parts[2] != "luid" or parts[5] != "phys":
        return None
    try:
        return int(parts[1]), luid_text(_hex32(parts[3]), _hex32(parts[4])), int(parts[6])
    except ValueError:
        return None


def is_3d(engtype: str) -> bool:
    return engtype.lower() == "3d"


def sample_3d(seconds: float, *, query_factory: Callable[[list[str]], PdhQuery] = PdhQuery,
              sleep: Callable[[float], None] = time.sleep) -> dict[tuple[int, str], float]:
    """(pid, luid) -> that process's busiest 3D engine on that adapter, in %, averaged over `seconds`.

    Per-sample values stay unclamped and only the average is clamped to 100 (single 250 ms samples reached
    3,652 %, WT verify log 13). Raises OSError when the GPU Engine counters gave no reading at all.
    """
    n = max(1, round(seconds / SAMPLE_PERIOD_S))
    sums: dict[tuple[int, str], float] = {}
    valid = 0
    q = query_factory([GPU_ENGINE_UTIL])
    try:
        q.collect()                                  # rate counter: the first collect only sets the baseline
        for _ in range(n):
            sleep(SAMPLE_PERIOD_S)
            try:
                q.collect()
            except OSError:
                continue
            values = q.read(GPU_ENGINE_UTIL)
            if not values:
                continue
            valid += 1
            busiest: dict[tuple[int, str], float] = {}
            for inst, v in values.items():
                parsed = parse_engine(inst) if v is not None else None
                if parsed is None or not is_3d(parsed[4]):
                    continue
                key = (parsed[0], parsed[1])
                busiest[key] = max(busiest.get(key, 0.0), v)
            for key, v in busiest.items():
                sums[key] = sums.get(key, 0.0) + v
    finally:
        q.close()
    if valid == 0:
        status = q.last_status.get(GPU_ENGINE_UTIL)
        raise OSError(f"the GPU Engine counters gave no reading in {seconds:.1f} s"
                      + (f" (last status 0x{status:08X})" if status is not None else ""))
    return {key: min(100.0, s / valid) for key, s in sums.items()}


# ---------------------------------------------------------------- process trees ---

def descendants(procs: dict[int, ProcInfo], pid: int, create_time: int) -> set[int]:
    """The target and every process whose parent chain leads to it; empty if the target is not running.

    A process joins only if it started after its parent: its ppid may name a PID that has since been
    reused by a newer process, which is not its parent.
    """
    root = procs.get(pid)
    if root is None or root.create_time != create_time:
        return set()
    children: dict[int, list[ProcInfo]] = {}
    for p in procs.values():
        if p.pid != p.ppid:
            children.setdefault(p.ppid, []).append(p)
    out = {pid}
    stack = [root]
    while stack:
        parent = stack.pop()
        for child in children.get(parent.pid, ()):
            if child.pid in out or child.create_time < parent.create_time:
                continue
            out.add(child.pid)
            stack.append(child)
    return out


def self_and_ancestors(procs: dict[int, ProcInfo], me: int | None = None) -> set[int]:
    """Our own PID and the processes that started us (the venv launcher, the shell, the terminal ...)."""
    me = os.getpid() if me is None else me
    out = {me}
    cur = procs.get(me)
    while cur is not None and cur.ppid not in out:
        parent = procs.get(cur.ppid)
        if parent is None or parent.create_time > cur.create_time:
            break
        out.add(parent.pid)
        cur = parent
    return out


_shared_lock = threading.Lock()
_shared: Snapshotter | None = None


def _snapshot(threads_for=()) -> dict[int, ProcInfo]:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Snapshotter()
    return _shared.snapshot(threads_for)


def _check_identity(pid: int, create_time: int) -> None:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xFFFFFFFF:
        raise ValueError(f"not a process id: {pid!r}")
    if isinstance(create_time, bool) or not isinstance(create_time, int) or create_time < 0:
        raise ValueError(f"not a process create time: {create_time!r}")


def is_alive(pid: int, create_time: int) -> bool:
    """True while that exact process runs (a reused PID does not count). Raises OSError if Windows cannot say."""
    _check_identity(pid, create_time)
    p = _snapshot().get(pid)
    return p is not None and p.create_time == create_time


def game_pids(pid: int, create_time: int) -> set[int]:
    """The target and its descendants, from one snapshot; empty if the target is gone."""
    _check_identity(pid, create_time)
    return descendants(_snapshot(), pid, create_time)


def accept_target(procs: dict[int, ProcInfo], pid: int, create_time: int, *, session: int,
                  approved: tuple[int, int]) -> str | None:
    """Why the relay must not record this process, in plain words; None when it may.

    Called with the same arguments by both sides, so they cannot drift: the recorder before it asks for the UAC
    prompt, the relay on its own snapshot once the prompt has approved `approved` (the running game's pid and create
    time). The process must be running as exactly that one process, in `session`: the Windows session of the
    recorder and of the relay. (Play, later, adds a rule for a game started after the relay from an approved folder.)
    """
    _check_identity(pid, create_time)
    p = procs.get(pid)
    if p is None or p.create_time != create_time:
        return f"process {pid} is no longer running (its game exited, or Windows gave its number to another program)"
    if p.session_id != session:
        return (f"process {pid} ({p.name or 'unnamed'}) runs in another Windows session ({p.session_id}, not "
                f"{session}): only a game in the player's own session is recorded")
    if (pid, create_time) != tuple(approved):
        return f"process {pid} ({p.name or 'unnamed'}) is not the game the UAC prompt approved"
    return None


# ---------------------------------------------------------------- choosing ---

@dataclass
class Candidate:
    pid: int
    create_time: int
    name: str
    gpu_pct: float | None        # busiest 3D engine, mean over the sample; None when not sampled
    adapter: str | None          # the LUID of that engine
    has_window: bool
    foreground: bool
    area: int                    # largest qualifying window, in pixels


def choose_auto(cands: list[Candidate], min_pct: float = AUTO_MIN_3D_PCT, tie: float = TIE_PCT) -> Candidate | None:
    """The windowed candidate with the most 3D use; within `tie` points of the best, the foreground one wins."""
    eligible = [c for c in cands if c.has_window and c.gpu_pct is not None and c.gpu_pct >= min_pct]
    if not eligible:
        return None
    best = max(c.gpu_pct for c in eligible)
    close = [c for c in eligible if best - c.gpu_pct <= tie]
    pool = [c for c in close if c.foreground] or close
    return max(pool, key=lambda c: (c.gpu_pct, c.area, -c.pid))


def choose_by_name(cands: list[Candidate], tie: float = TIE_PCT) -> Candidate:
    """Several processes share the name: the most 3D use wins; then foreground, a window, window size, newest."""
    pool = cands
    sampled = [c for c in cands if c.gpu_pct is not None]
    if sampled:
        best = max(c.gpu_pct for c in sampled)
        pool = [c for c in sampled if best - c.gpu_pct <= tie]
    return max(pool, key=lambda c: (c.foreground, c.has_window, c.area, c.gpu_pct or 0.0, c.create_time))


def windows_by_pid(wins: list[dict]) -> dict[int, list[dict]]:
    """Windows that can make their owner a game: shown, non-zero size, and not a console window."""
    out: dict[int, list[dict]] = {}
    for w in wins:
        if w.get("cls") in CONSOLE_CLASSES or not w.get("area", 0) > 0:
            continue
        out.setdefault(w["pid"], []).append(w)
    return out


def _main_window(wins: list[dict]) -> dict | None:
    return max(wins, key=lambda w: (not w["minimized"], w["area"]), default=None)


def _gpu_by_pid(gpu: dict[tuple[int, str], float]) -> dict[int, tuple[float, str]]:
    out: dict[int, tuple[float, str]] = {}
    for (pid, luid), v in gpu.items():
        if pid not in out or v > out[pid][0]:
            out[pid] = (v, luid)
    return out


def _candidate(p: ProcInfo, gpu: dict[int, tuple[float, str]] | None, wins: dict[int, list[dict]],
               fg: int | None) -> Candidate:
    g = (gpu.get(p.pid, (0.0, None)) if gpu is not None else (None, None))
    mine = wins.get(p.pid, [])
    return Candidate(p.pid, p.create_time, p.name, g[0], g[1], bool(mine), fg == p.pid,
                     max((w["area"] for w in mine), default=0))


def _public(c: Candidate) -> dict:
    return {"pid": c.pid, "name": c.name, "gpu_pct": None if c.gpu_pct is None else round(c.gpu_pct, 2),
            "has_window": c.has_window}


def _matches(proc_name: str, wanted: str) -> bool:
    have = proc_name.lower()
    return have == wanted or (not wanted.endswith(".exe") and have == wanted + ".exe")


def find_target(name: str | None = None, pid: int | None = None, auto: bool = False,
                sample_s: float = 2.0) -> dict:
    """Pick the game by PID, by exe name or automatically. Raises TargetNotFound with the reason.

    Returns {pid, create_time, name, exe, window_title, monitor_device, adapter_luid, how, candidates}.
    `sample_s` is how long the GPU Engine counters are sampled (0 skips it, except in auto mode, which needs
    it); adapter_luid is the adapter whose 3D engine the target used most during that sample.
    """
    if sum((name is not None, pid is not None, bool(auto))) != 1:
        raise ValueError("pass exactly one of name=, pid= or auto=True")
    if isinstance(sample_s, bool) or not isinstance(sample_s, (int, float)) or not math.isfinite(sample_s) \
            or not 0 <= sample_s <= MAX_SAMPLE_S:
        raise ValueError(f"sample_s must be between 0 and {MAX_SAMPLE_S:.0f} seconds")
    if auto and sample_s <= 0:
        raise ValueError("auto mode needs a GPU sample (sample_s > 0)")

    procs = _snapshot()
    mine = self_and_ancestors(procs)
    if pid is not None:
        how = "pid"
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xFFFFFFFF:
            raise TargetNotFound(f"{pid!r} is not a process id")
        if pid == 4:
            raise TargetNotFound("PID 4 is the Windows kernel (System), not a game")
        if pid == os.getpid():
            raise TargetNotFound("that PID is lag-doctor itself")
        if pid not in procs:
            raise TargetNotFound(f"no process with PID {pid} is running")
        pool = [procs[pid]]
    elif name is not None:
        how = "name"
        if not isinstance(name, str) or not name.strip() or "\0" in name or len(name) > 260:
            raise TargetNotFound(f"{name!r} is not an exe name")
        wanted = os.path.basename(name.strip()).lower()
        pool = [p for p in procs.values() if p.pid not in mine and _matches(p.name, wanted)]
        if not pool:
            raise TargetNotFound(f"no running process is named {wanted}")
    else:
        how = "auto"
        pool = []

    gpu = None
    if sample_s > 0:
        try:
            gpu = _gpu_by_pid(sample_3d(sample_s))
        except OSError as exc:
            if auto:
                raise TargetNotFound(f"auto mode needs the GPU Engine counters, which are unavailable: {exc}")
            gpu = None
        procs = _snapshot()                    # the sample took a while: work from the processes alive now
    wins = windows_by_pid(windows.top_windows())
    fg = windows.foreground_pid()

    if how == "auto":
        cands = [_candidate(p, gpu, wins, fg) for p in procs.values()
                 if p.pid not in mine and p.pid not in (0, 4) and p.name.lower() not in IGNORE_NAMES
                 and (p.pid in wins or (gpu or {}).get(p.pid, (0.0,))[0] > 0)]
        cands.sort(key=lambda c: (c.gpu_pct or 0.0, c.has_window), reverse=True)
        chosen = choose_auto(cands)
        listed = [_public(c) for c in cands[:MAX_CANDIDATES]]
        if chosen is None:
            busiest = next((c for c in cands if not c.has_window and (c.gpu_pct or 0) >= AUTO_MIN_3D_PCT), None)
            hint = (f" The busiest 3D user, {busiest.name} (PID {busiest.pid}, {busiest.gpu_pct:.0f} %), has no "
                    f"visible window, so it is not taken for a game." if busiest else "")
            raise TargetNotFound(
                f"no process with a visible window used the 3D engine (at least {AUTO_MIN_3D_PCT:g} % over "
                f"{sample_s:g} s). Start the game first, or name it with --game NAME or --pid PID.{hint}",
                listed)
    else:
        alive = [procs[p.pid] for p in pool if p.pid in procs and procs[p.pid].create_time == p.create_time]
        if not alive:
            raise TargetNotFound(f"the process exited while it was being sampled ({pool[0].name}, PID {pool[0].pid})")
        cands = [_candidate(p, gpu, wins, fg) for p in alive]
        chosen = cands[0] if how == "pid" else choose_by_name(cands)
        cands.sort(key=lambda c: (c.pid != chosen.pid, -(c.gpu_pct or 0.0)))
        listed = [_public(c) for c in cands[:MAX_CANDIDATES]]

    try:
        exe = ntquery.image_path(chosen.pid)
    except OSError:
        exe = None
    window = _main_window(wins.get(chosen.pid, []))
    return {
        "pid": chosen.pid,
        "create_time": chosen.create_time,
        "name": chosen.name,
        "exe": exe,
        "window_title": window["title"] if window else None,
        "monitor_device": window["monitor_device"] if window else None,
        "adapter_luid": chosen.adapter if chosen.gpu_pct else None,
        "how": how,
        "candidates": listed,
    }
