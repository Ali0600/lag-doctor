"""Choose the game process to watch, and follow it, without ever opening a handle to it.

Process identity comes from NtQuerySystemInformation snapshots (winapi/ntquery.py): a process is the pair
(pid, create_time), because Windows reuses PIDs. GPU use comes from the GPU Engine performance counters,
whose instance names carry the PID and the adapter LUID (docs/research/windows-telemetry.md (WT) 2.2).

`auto` mode picks the process with the most 3D-engine use among those that own a visible, uncloaked
top-level window. The window matters: CUDA compute is booked to the 3D engine too (WT verify log 1, 2.2),
so a busy 3D engine alone does not make a process a game; this machine's ML training job is exactly that.

Play (docs/LAUNCHER-PLAN.md, M6) has a store start the game, so it waits for it instead: `wait_for_target` watches
for a new process whose program is under the game's install folder and that draws, and `accept_target`'s folder
rule is what the relay checks that choice against.
"""
from __future__ import annotations

import math
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from lagdoctor.capture import gamefiles
from lagdoctor.winapi import folders as winfolders
from lagdoctor.winapi import ntquery, storage, windows
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


def _own_pid() -> int:
    """lag-doctor's own process id. Its own seam, so a test's made-up process table names this process by a made-up
    number too, and a real number that happens to be one of the table's made-up ones never makes that one lag-doctor."""
    return os.getpid()


def self_and_ancestors(procs: dict[int, ProcInfo], me: int | None = None) -> set[int]:
    """Our own PID and the processes that started us (the venv launcher, the shell, the terminal ...)."""
    me = _own_pid() if me is None else me
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


def is_alive(pid: int, create_time: int, procs: dict[int, ProcInfo] | None = None) -> bool:
    """True while that exact process runs (a reused PID does not count). Raises OSError if Windows cannot say.
    `procs`: a snapshot already taken (the session's of this tick); default, a new one."""
    _check_identity(pid, create_time)
    p = (_snapshot() if procs is None else procs).get(pid)
    return p is not None and p.create_time == create_time


def game_pids(pid: int, create_time: int) -> set[int]:
    """The target and its descendants, from one snapshot; empty if the target is gone."""
    _check_identity(pid, create_time)
    return descendants(_snapshot(), pid, create_time)


# ---------------------------------------------------------------- the game's folder (Play, M6) ---

MAX_FOLDER_CHARS = 260
MAX_SHOWN_CHARS = 80            # of a refused value that a message quotes: the files that give it are any size
# A plain local drive path: a letter, a colon and one separator. Never a share, a device path or "C:relative".
_LOCAL_DRIVE = re.compile(r"[A-Za-z]:[\\/](?![\\/])")
_SEPARATORS = re.compile(r"[\\/]+")
_BAD_NAME_CHARS = frozenset('<>:"|?*')


def _shown(value: object) -> str:
    """A refused value as a message quotes it: its repr, cut to MAX_SHOWN_CHARS."""
    text = repr(value)
    return text if len(text) <= MAX_SHOWN_CHARS else text[:MAX_SHOWN_CHARS - 3] + "..."


def _windows_folder() -> str:
    """The Windows folder as Windows itself gives it (winfolders.windows_folder), looked up when called (the tests
    replace this). Never the SystemRoot variable: an environment is the caller's to set, and the elevated relay checks
    the folder forms its UAC prompt approved by this. ValueError when Windows cannot say: then no folder is checked."""
    try:
        return winfolders.windows_folder()
    except OSError as exc:
        raise ValueError(f"the Windows folder could not be read ({exc.strerror or exc}), so no game's folder can be "
                         "checked") from None


def fold_path(path: str) -> str:
    """The form two Windows paths are compared in: each run of slashes one backslash, none at the end, case-folded.

    Steam stores its folder as c:/program files (x86)/steam, while Windows gives a game's image path as
    C:\\Program Files (x86)\\Steam\\... (research note, section 14). Windows compares names through its own
    upper-case table; casefold agrees with it on the names game folders use, and where the two differ (ß against
    ss) it can only take a look-alike name for the folder's own.
    """
    out = _SEPARATORS.sub(lambda m: "\\", path).casefold()
    return out[:-1] if out.endswith("\\") else out


def check_folder(folder: object) -> str:
    """A game's install folder as the folder rule takes it, in Windows' own form (backslashes, none at the end, the
    drive letter in upper case); ValueError says why not.

    The library files that name it are untrusted (every local account can write them, research note section 5) and
    the rule takes in every program under it, so it must be a plain local drive path below the drive's root and
    outside the Windows folder, with no . or .. part (a test of the text would otherwise reach past it), and no name
    that Windows would change (a trailing dot or space) or refuse (<>:"|?* and control characters).
    """
    if not isinstance(folder, str):
        raise ValueError(f"not a folder path: {_shown(folder)}")
    if not 3 <= len(folder) <= MAX_FOLDER_CHARS or any(ord(c) < 32 for c in folder):
        raise ValueError(f"not a folder path of 3 to {MAX_FOLDER_CHARS} printable characters: {_shown(folder)}")
    if not _LOCAL_DRIVE.match(folder):
        raise ValueError(f"{folder} is not a folder on a local drive (like C:\\Games\\Name)")
    parts = [p for p in _SEPARATORS.split(folder[3:]) if p]
    if not parts:
        raise ValueError(f"{folder} is a whole drive, not a game's folder")
    for part in parts:
        if part in (".", ".."):
            raise ValueError(f"{folder} has a {part} part, so it may name another folder than it seems to")
        if part[-1] in ". " or any(c in _BAD_NAME_CHARS for c in part):
            raise ValueError(f"{folder} has a name Windows would not keep as written ({part!r})")
    clean = folder[0].upper() + ":\\" + "\\".join(parts)
    system = fold_path(_windows_folder())
    if fold_path(clean) == system or fold_path(clean).startswith(system + "\\"):
        raise ValueError(f"{clean} is in the Windows folder, where no game is installed")
    return clean


def folder_reason(image: str | None, folder: str) -> str | None:
    """Why the program at `image` (a process's image path, as ntquery.image_path gives it) is not from under
    `folder` (as check_folder returns it), in plain words; None when it is.

    Windows gives an image path in NT form when no drive letter names its volume (one mounted on a folder, say).
    Such a path cannot match a folder named by its drive letter, and the reason says so.
    """
    if not image:
        return "Windows did not give the path of its program"
    if not _LOCAL_DRIVE.match(image):
        if image.startswith("\\\\"):
            return f"its program runs from a network share ({image})"
        return f"Windows gave its program's path as {image}, with no drive letter, so it cannot be matched to a folder"
    parts = _SEPARATORS.split(image)
    if "." in parts or ".." in parts:
        return f"its program's path ({image}) has a . or .. part"
    if not fold_path(image).startswith(fold_path(folder) + "\\"):
        return f"its program ({image}) is not in the game's folder ({folder})"
    return None


def in_folder(image: str | None, folder: str) -> bool:
    """True when the program at `image` is under `folder` (folder_reason gives the why when it is not)."""
    return folder_reason(image, folder) is None


MAX_FORMS = 4           # forms of one folder the rule takes: the folder itself and its resolved path, with room
_SHORT_NAME = re.compile(r"~[0-9]")     # an 8.3 short name's tilde and number (PROGRA~2), never a long name's ~


DRIVE_NOT_THERE = 1             # GetDriveTypeW's DRIVE_NO_ROOT_DIR: no disk at that root now (an unplugged one, say)


def _disk_problem(root: str) -> str | None:
    """Why the drive or volume at `root` ("C:\\" or "\\\\?\\Volume{...}\\") is no local fixed or removable disk, as the
    rest of a sentence that starts with a path on it; None when it is one. GetDriveTypeW asks the root alone, so
    nothing on a network drive is opened to find out: a sleeping server could hold the call for its whole timeout."""
    kind = storage.drive_type(root)
    if storage.judged_drive_type(kind):
        return None
    if kind == DRIVE_NOT_THERE:
        return f"is on {root[:2]}, where no disk is connected now" if _LOCAL_DRIVE.match(root) \
            else "is on a volume that is not connected now"
    return "is not on a local fixed or removable disk"


def disk_problem(folder: str) -> str | None:
    """_disk_problem for `folder`'s drive (`folder` as check_folder gives it), as the rest of a sentence about it."""
    return _disk_problem(folder[:3])


def on_local_disk(folder: str) -> bool:
    """True when `folder` (as check_folder gives it) is on a local fixed or removable disk, asked of its drive letter
    alone (disk_problem). A link along the path can still lead elsewhere; local_problem follows each one."""
    return disk_problem(folder) is None


MAX_LINKS = 8                   # junctions and symbolic links one path may lead through (Windows itself stops at 63)
MAX_LINK_CHARS = 1024           # a link's target as read: no folder a game sits under needs more
_NAME_SURROGATES = frozenset({stat.IO_REPARSE_TAG_MOUNT_POINT, stat.IO_REPARSE_TAG_SYMLINK})
_VOLUME_ROOT = re.compile(r"\\\\\?\\Volume\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}\\", re.ASCII)


class NotLocal(OSError):
    """A path that leads off the local disks, refused before anything was opened through it; strerror says why, in
    plain words (local_problem's answer). The store readers' file access raises it (lagdoctor/stores)."""


def _lstat(path: str) -> os.stat_result:
    """os.lstat, looked up when called (the tests replace this, never os.lstat itself)."""
    return os.lstat(path)


def _readlink(path: str) -> str:
    """os.readlink, looked up when called (the tests replace this, never os.readlink itself)."""
    return os.readlink(path)


def _names(text: str) -> list[str]:
    """The names of a path below its root, with its . and .. parts worked out from the text, as Windows does."""
    out: list[str] = []
    for part in _SEPARATORS.split(text):
        if part == "..":
            if out:
                out.pop()
        elif part and part != ".":
            out.append(part)
    return out


def _rooted(path: str) -> tuple[str, list[str]] | None:
    """A path as (root, the names below it): a drive's root ("C:\\") or a volume's ("\\\\?\\Volume{...}\\"), read
    through the \\\\?\\ and \\??\\ prefixes a link's target carries. None for any other path: a share (\\\\server\\...
    or \\\\?\\UNC\\...), a device, or a path with no root."""
    for prefix in ("\\\\?\\", "\\??\\"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            m = _VOLUME_ROOT.match("\\\\?\\" + path)
            if m:
                return m.group(0), _names(("\\\\?\\" + path)[m.end():])
            break
    if not _LOCAL_DRIVE.match(path):
        return None
    return path[0].upper() + ":\\", _names(path[3:])


def _link_target(text: str, root: str, names: list[str]) -> tuple[str, list[str]] | None:
    """Where the link at `root` + `names` points, given its target as readlink gives it, as (root, names); None when
    that is not a path on a local drive. A target with no root is read against the link's own folder, and one that
    starts with a single backslash against the link's drive, as Windows reads a symbolic link's relative target."""
    if text.startswith("\\??\\") or (text[0] in "\\/" and text[1:2] in ("\\", "/")) or _LOCAL_DRIVE.match(text):
        return _rooted(text)                # a share in either slash is no local path: _rooted refuses it
    if text[0] in "\\/":
        return root, _names(text)
    if len(text) > 1 and text[1] == ":":
        return None                         # "D:name": from a drive's current folder, which a link never has
    return root, _names("\\".join([*names[:-1], text]))


def _cut(text: str) -> str:
    """A path or a link's target as a message names it: as written, cut to MAX_SHOWN_CHARS."""
    return text[:MAX_SHOWN_CHARS - 3] + "..." if len(text) > MAX_SHOWN_CHARS else text


def _line(text: str) -> str:
    """local_problem's answer as one line of plain text: each character that cannot be shown becomes a ?."""
    return "".join(c if c.isprintable() else "?" for c in text)


def local_problem(path: object) -> str | None:
    """Why `path` cannot be opened without leaving the local disks, in plain words; None when it can, or when one of
    its parts does not exist (nothing past it can be opened at all, through a link or not).

    on_local_disk asks a path's drive letter alone, and a junction or a symbolic link anywhere along the path can
    still lead onto a network share (where a sleeping server holds a call for its whole timeout) or onto a drive that
    is no local disk. So each part of the path is looked at in turn, from its root down, without following it (an
    lstat), and a link is read (readlink), never opened: the walk goes on at its target, whose drive or volume must be
    a local fixed or removable disk too. A part kept online only is refused as well (opening it would download it),
    and so is a path that leads through more than MAX_LINKS links. Anything but text without control characters is
    no path."""
    if not isinstance(path, str):
        return _line(f"{_shown(path)} is not a path on a local drive")
    split = None if any(ord(c) < 32 for c in path) else _rooted(path)
    if split is None:
        return _line(f"{_cut(path)} is not a path on a local drive")
    root, todo = split
    why = _disk_problem(root)
    if why is not None:
        return _line(f"{path} {why}")
    done: list[str] = []
    links = 0
    while todo:
        done.append(todo.pop(0))
        here = root + "\\".join(done)
        try:
            st = _lstat(here)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return _line(f"{here} could not be looked at ({exc.strerror or exc})")
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & gamefiles.ONLINE_ONLY:
            return _line(f"{here} is {gamefiles.WHY_ONLINE}")
        if not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT or getattr(st, "st_reparse_tag", 0) not in _NAME_SURROGATES:
            continue                        # a plain folder or file (or a reparse point that names no other place)
        links += 1
        if links > MAX_LINKS:
            return _line(f"{path} leads through more than {MAX_LINKS} links, so it was not opened")
        try:
            text = _readlink(here)
        except OSError as exc:
            return _line(f"{here} is a link that could not be read ({exc.strerror or exc})")
        if not isinstance(text, str) or not text or len(text) > MAX_LINK_CHARS or any(ord(c) < 32 for c in text):
            return _line(f"{here} is a link to {_shown(text)}, which is not a folder's path")
        to = _link_target(text, root, done)
        if to is None:
            return _line(f"{here} is a link to {_cut(text)}, which is not a path on a local drive")
        why = None if to[0] == root else _disk_problem(to[0])
        if why is not None:
            return _line(f"{here} is a link to {_cut(text)}, which {why}")
        root, done, todo = to[0], [], to[1] + todo
    return None


def _real_path(path: str) -> str:
    """os.path.realpath, looked up when called (the tests replace this, never the os.path function itself)."""
    return os.path.realpath(path)


def folder_forms(folder: object) -> tuple[str, ...]:
    """The forms of a game's install folder that its programs are matched against: the folder as check_folder returns
    it, then its real path when that names another plain local folder. Windows names a program by the path it was
    opened through with junctions and subst drives resolved, so a library moved with a junction matches only its real
    path. ValueError when the folder, or a junction or link along its path, leads off the local fixed and removable
    disks (local_problem, which reads every link before anything is opened through it): a sleeping server could
    hold the call for its whole timeout.

    It opens the folder (never a process), so it runs with the player's own rights, in the recorder or the app before
    the UAC prompt, and the prompt approves the forms it gives. The elevated relay takes them from its command line:
    it never resolves a path the player can change after the approval.
    """
    folder = check_folder(folder)
    why = local_problem(folder)
    if why is not None:
        raise ValueError(why)
    forms = [folder]
    try:
        real = check_folder(_real_path(folder))
    except (OSError, ValueError):
        real = None
    if real is not None and fold_path(real) != fold_path(folder) and on_local_disk(real):
        forms.append(real)
    return tuple(forms)


def check_forms(folders: object) -> tuple[str, ...]:
    """A folder's forms as the folder rule takes them (folder_forms' tuple, each checked again); ValueError if not."""
    if isinstance(folders, str) or not isinstance(folders, (tuple, list)) or not 1 <= len(folders) <= MAX_FORMS:
        raise ValueError(f"not 1 to {MAX_FORMS} forms of a folder: {folders!r}")
    forms = tuple(check_folder(f) for f in folders)
    if len({fold_path(f) for f in forms}) != len(forms):
        raise ValueError(f"the forms of a folder name one folder twice: {_shown(forms)}")
    return forms


def program_reason(image: str | None, folders: tuple[str, ...], *, resolve: bool = True) -> str | None:
    """Why the program at `image` is under none of `folders` (check_forms' tuple), in plain words; None when it is
    under one. The folder rule's one test: the wait judges each process by it, and accept_target the game proposed.

    A path Windows gives through an 8.3 short name (C:\\PROGRA~2\\...), as when a store or a stub starts the game
    through one, is first written out long (winapi.folders.long_path reads folder entries and opens no file). Not with
    `resolve` False, as the relay asks it: it would read those folders with administrator rights, and a link someone
    swapped in along the way could lead it onto a share, so there the path is compared as Windows gave it.
    """
    if resolve and image and "~" in image and _LOCAL_DRIVE.match(image):
        image = winfolders.long_path(image) or image
    first = None
    for folder in folders:
        why = folder_reason(image, folder)
        if why is None:
            return None
        first = first or why
    if not resolve and image and _SHORT_NAME.search(image):
        return f"{first} (Windows gave that path with 8.3 short names, which the relay compares as written)"
    return first


def running_from(folders: tuple[str, ...], procs: dict[int, ProcInfo] | None = None) -> dict[str, list[dict]]:
    """The processes in lag-doctor's Windows session whose program is under one of `folders`, read now, opening no
    process: {"running": [{pid, create_time, name, exe}], "unreadable": [{pid, create_time, name}]}, the second the
    processes whose path Windows did not give, which may be the game too. Play's pre-flight asks it before the link
    is opened, since a store starts nothing for a game that already runs, and the wait sees only processes started
    after it began."""
    folders = check_forms(folders)
    procs = _snapshot() if procs is None else procs
    me = procs.get(_own_pid())
    if me is None:
        raise OSError("lag-doctor's own process is not in Windows' process list")
    mine = self_and_ancestors(procs)
    out: dict[str, list[dict]] = {"running": [], "unreadable": []}
    for p in procs.values():
        if p.session_id != me.session_id or p.pid in mine or p.pid in (0, 4):
            continue
        who = {"pid": p.pid, "create_time": p.create_time, "name": p.name}
        try:
            image = ntquery.image_path(p.pid)
        except OSError:
            out["unreadable"].append(who)
            continue
        if program_reason(image, folders) is None:
            out["running"].append({**who, "exe": image})
    return out


def accept_target(procs: dict[int, ProcInfo], pid: int, create_time: int, *, session: int,
                  approved: tuple[int, int] | None = None, folders: tuple[str, ...] | None = None,
                  since: int | None = None, image: str | None = None, own: tuple[tuple[int, int], ...] = (),
                  resolve: bool = True) -> str | None:
    """Why the relay must not record this process, in plain words; None when it may.

    Called with the same arguments by both sides, so they cannot drift: the recorder before it proposes the game,
    the relay on its own snapshot. The process must be running as exactly that one process, in `session`: the
    Windows session of the recorder and of the relay. Then, in one of two modes:
    - `approved`: the running game's pid and create time, which the UAC prompt approved (M3);
    - `folders` and `since` (Play, M6): the prompt came before the game existed and approved its install folder's
      forms (folder_forms), so the game must have started after `since` (the relay's own create time) and run a
      program under one of them (program_reason, which the folder wait judges by too). `image` is that program's
      path, which each side reads for itself, without a handle (ntquery.image_path). `own`: lag-doctor's own
      processes as (pid, create_time), the recorder and the relay, whose trees are never the game. `resolve` False
      (the relay, and the recorder asking as the relay will): the path is compared as Windows gave it (program_reason).
    """
    _check_identity(pid, create_time)
    if (approved is None) == (folders is None):
        raise ValueError("pass exactly one of approved= or folders=")
    if folders is not None:
        folders = check_forms(folders)
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError(f"not a process create time: {since!r}")
    elif since is not None or image is not None:
        raise ValueError("since= and image= go with folders=")
    roots = _own_roots(own, folders, resolve)
    p = procs.get(pid)
    if p is None or p.create_time != create_time:
        return f"process {pid} is no longer running (its game exited, or Windows gave its number to another program)"
    if p.session_id != session:
        return (f"process {pid} ({p.name or 'unnamed'}) runs in another Windows session ({p.session_id}, not "
                f"{session}): only a game in the player's own session is recorded")
    if approved is not None:
        if (pid, create_time) != tuple(approved):
            return f"process {pid} ({p.name or 'unnamed'}) is not the game the UAC prompt approved"
        return None
    if any(pid in descendants(procs, *root) for root in roots):
        return f"process {pid} ({p.name or 'unnamed'}) is one of lag-doctor's own processes, never the game"
    if create_time <= since:
        return (f"process {pid} ({p.name or 'unnamed'}) started before the UAC prompt was approved, so it is not "
                "the game started after it")
    why = program_reason(image, folders, resolve=resolve)
    if why is not None:
        return f"process {pid} ({p.name or 'unnamed'}) is not the game: {why}"
    return None


def _own_roots(own: object, folders: tuple[str, ...] | None, resolve: object) -> tuple[tuple[int, int], ...]:
    """accept_target's `own` as checked (pid, create_time) pairs. It and `resolve` go with folders= alone: a game the
    prompt approved by its pid was never looked for, so neither has anything to judge there."""
    if not isinstance(resolve, bool):
        raise ValueError(f"resolve= is not true or false: {_shown(resolve)}")
    if isinstance(own, (str, bytes)) or not isinstance(own, (tuple, list)):
        raise ValueError(f"not a list of lag-doctor's own processes: {_shown(own)}")
    if folders is None and (own or not resolve):
        raise ValueError("own= and resolve= go with folders=")
    return _roots(own)


def _roots(own: tuple | list) -> tuple[tuple[int, int], ...]:
    for root in own:
        if not isinstance(root, (tuple, list)) or len(root) != 2:
            raise ValueError(f"not a process as (pid, create time): {_shown(root)}")
        _check_identity(*root)
    return tuple((root[0], root[1]) for root in own)


def _listed(procs: dict[int, ProcInfo], who: tuple[int, int], session: int) -> bool:
    p = procs.get(who[0])
    return p is not None and p.create_time == who[1] and p.session_id == session


def check_proposed(snapshot: Callable[[], dict[int, ProcInfo]], image_of: Callable[[int], str | None], pid: int,
                   create_time: int, *, session: int, folders: tuple[str, ...], since: int,
                   own: tuple[tuple[int, int], ...], alive: tuple[tuple[int, int], ...] = (),
                   tries: int | None = None) -> tuple[str | None, bool]:
    """Play's folder rule on a game proposed by its pid and create time, as the relay judges it and as the recorder
    asks first, with the same arguments so the two cannot drift: (why it may not be recorded, whether asking again
    about it may change that), or (None, False) when it may.

    The program's path is read between two snapshots (`snapshot`, `image_of`: ntquery's, or a test's), and both must
    list the process with that create time: a number never names two processes at once, so the path read is that
    process's own, never that of one that held the number before or after it. `alive`: processes that must run in
    `session` at the end of the check, so throughout it, or nothing is taken (the relay names the recorder: a proposal
    from a pipe handle that outlived it is never taken). A failed read is tried again, TRY_PAUSE_S apart, up to
    `tries` times; past that the game could not be checked, which a later proposal may change (`tries` defaults to
    IMAGE_TRIES, the wait's own). The path is compared as Windows gave it (accept_target's resolve=False): the relay
    reads no folder with administrator rights. The reason is one line of printable text (a program's name can hold
    characters that turn the text around), the same on both sides."""
    why, retry = _proposed(snapshot, image_of, pid, create_time, session=session, folders=folders, since=since,
                           own=own, alive=alive, tries=tries)
    return (None, False) if why is None else ("".join(c if c.isprintable() else "?" for c in why), retry)


def _proposed(snapshot, image_of, pid, create_time, *, session, folders, since, own, alive, tries):
    _check_identity(pid, create_time)
    why = f"process {pid} could not be checked"
    for attempt in range(max(1, IMAGE_TRIES if tries is None else tries)):
        if attempt:
            time.sleep(TRY_PAUSE_S)
        try:
            before = snapshot()
            if not _listed(before, (pid, create_time), session):     # accept_target says which: gone, or elsewhere
                return accept_target(before, pid, create_time, session=session, approved=(pid, create_time)), False
            image = image_of(pid)
            after = snapshot()
        except OSError as exc:
            why = f"process {pid} could not be checked ({exc.strerror or exc})"
            continue
        if not all(_listed(after, who, session) for who in alive):  # at the end of the check: so throughout it
            return "the recording window is no longer running", False
        # accept_target refuses a process `after` does not list, as the second snapshot must.
        return accept_target(after, pid, create_time, session=session, folders=folders, since=since, image=image,
                             own=own, resolve=False), False
    return why, True


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
            # What to do next is the caller's to say (the console names --game and --pid once, for every refusal).
            raise TargetNotFound(
                f"no process with a visible window used the 3D engine (at least {AUTO_MIN_3D_PCT:g} % over "
                f"{sample_s:g} s).{hint}", listed)
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


# ---------------------------------------------------------------- the folder wait (Play, M6) ---

WAIT_DEADLINE_S = 300.0         # how long the wait gives a store to start the game (a feel number: Steam may first
                                # download an update, or wait for an answer in its own window)
MAX_WAIT_S = 3600.0
WAIT_POLL_S = 0.5               # how often it looks for new processes and their windows
DRAW_SAMPLE_S = 1.0             # one GPU sample, taken only while a process from the folder owns a window
DRAW_SAMPLES = 2                # samples in a row of at least AUTO_MIN_3D_PCT before that process is the game
GPU_FAILS_TO_WINDOWS = 3        # samples in a row without a reading before a window from the folder is enough
IMAGE_TRIES = 3                 # failed reads of a process's image path before it is counted as unreadable
ROUND_FAILS = 3                 # rounds in a row whose processes or windows could not be read before it gives up
MAX_READS_PER_ROUND = 512       # new processes whose image path one round reads; any more wait for the next round
SINCE_MARGIN = 20_000_000       # 2 s in create-time units: by default a process started just before the wait counts
MAX_NAMES = 3                   # programs from the folder that a message names
# How long a chosen process must keep running before Play names it to the relay, so a launcher that draws and then
# leaves re-arms the wait instead (docs/LAUNCHER-PLAN.md, M6: a feel number the owner has not judged yet). The re-arm
# itself is the recorder's (watch --wait-folder); the relay's wait for the game leaves room for it (server.GAME_SLACK_S).
REARM_HOLD_S = 20.0
TRY_PAUSE_S = 0.1               # check_proposed: between two tries of a read that failed
_FILETIME_1970 = 116_444_736_000_000_000   # 1970-01-01 as a create time (100 ns since 1601)


class WaitCancelled(Exception):
    """The folder wait ended because its `cancel` was set, or `pick` chose nothing."""


def filetime_now() -> int:
    """The system time in the units of a process's create time (100 ns since 1601, UTC)."""
    return time.time_ns() // 100 + _FILETIME_1970


def _duration_words(seconds: float) -> str:
    if seconds >= 120:
        return f"{seconds / 60:g} minutes"
    return f"{seconds:g} second" + ("" if seconds == 1 else "s")


def _named(names: list[str]) -> str:
    return ", ".join(names[:MAX_NAMES]) + (" and others" if len(names) > MAX_NAMES else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _deadline_reason(folder: str, seconds: float, furthest: int, names: list[list[str]], unreadable: int,
                     driveless: int, samples: int, failed: int, short: int = 0) -> str:
    """Why the wait ended without a game: how far the folder's programs got, then what could not be measured."""
    took = _duration_words(seconds)
    if furthest == 0:
        out = (f"no program from the game's folder ({folder}) started within {took}. The store may still be "
               "downloading an update or waiting for an answer in its own window, or the game started from "
               "another folder")
    elif furthest == 1:
        out = f"{_named(names[0])} started from the game's folder but showed no window within {took}"
    elif furthest == 2:
        out = (f"a window from the game's folder ({_named(names[1])}) never drew within {took}: it may be showing a "
               "message instead, such as a missing runtime")
    else:
        out = (f"a window from the game's folder ({_named(names[2])}) drew only in short bursts within {took}, never "
               f"for {DRAW_SAMPLES} samples in a row")
    if unreadable:
        out += (f". Windows could not give the path of {_plural(unreadable, 'program')} that started, so they could "
                "not be checked")
    if driveless:
        out += (f". Windows gave {_plural(driveless, 'program')} that started a path with no drive letter, which no "
                "folder can be matched to")
    if short:
        out += (f". Windows gave {_plural(short, 'program')} that started a path in 8.3 short names, which a "
                "recording with administrator rights compares as written, so no folder matched them")
    if failed:
        out += f". The GPU counters gave no reading on {failed} of {_plural(samples, 'sample')}"
    return out


def _busiest(cands: list[Candidate]) -> Candidate:
    if all(c.gpu_pct is not None for c in cands):
        return choose_auto(cands, min_pct=0.0) or cands[0]
    return max(cands, key=lambda c: (c.foreground, c.area, -c.pid))


def wait_for_target(folder: str, *, folders: tuple[str, ...] | None = None, since: int | None = None,
                    deadline_s: float = WAIT_DEADLINE_S, cancel: threading.Event | None = None,
                    on_status: Callable[[str, dict], None] | None = None,
                    exclude: Iterable[tuple[int, int]] = (),
                    pick: Callable[[list[Candidate]], Candidate | None] | None = None,
                    own: Iterable[tuple[int, int]] = (), resolve: bool = True,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], object] | None = None) -> dict:
    """Wait, opening no process, for the game a store starts from its install folder. Returns find_target's dict
    with how "folder", plus `folder`, `folders` (the forms matched against) and `judged_by` ("3d", or "window"
    once the GPU counters failed). Raises TargetNotFound saying how far the wait got by `deadline_s`, WaitCancelled,
    ValueError for an argument, and OSError when Windows' process list or windows cannot be read ROUND_FAILS rounds
    in a row.

    `folders` are the folder's forms (folder_forms); by default the wait works them out itself, once it has said
    "waiting". Play passes the forms the UAC prompt approved, so the relay's folder rule (accept_target) judges the
    game by the same forms, through the same program_reason.

    A process counts once it started after `since` (a create time; by default SINCE_MARGIN before the call, so take
    it before the store's link is opened), runs in this Windows session from a program under one of the forms (its
    image path, read once per process, without a handle; a failed read is tried again, up to IMAGE_TRIES times), and
    is neither lag-doctor's own nor in `exclude` (processes the caller already tried). lag-doctor's own are this
    process and the ones that started it, and with `own` ((pid, create_time) roots: Play passes the recorder and the
    relay, RelayLink.own) every process in those trees too, which the relay's folder rule refuses (accept_target's
    own=). `resolve` False compares a program's path as Windows gave it, never writing an 8.3 short name out long, as
    the relay does (program_reason), so the wait never picks a game the relay refuses for it. It is the game once it owns a
    window that counts for auto mode, both before and after each sample, and draws at least AUTO_MIN_3D_PCT of a 3D
    engine on DRAW_SAMPLES samples in a row; the GPU is sampled only while such a window exists. Stubs, anti-cheat
    bootstrappers and store relaunches never draw, or exit, so they are passed over for as long as they run (Unreal's
    stub runs beside its game all session). A sample with no reading changes no count: it is no evidence either way,
    and the deadline's message says how many there were. When several draw, `pick` chooses one of them (None
    cancels); by default the busiest does.

    `on_status(state, info)` hears each stage once: "waiting"; "started" and "window", with the pid and name of the
    first process to get there; "short-name", with the pid and name of the first whose path Windows gave in 8.3
    short names, which with `resolve` False matches no form (it may be the game, which the relay would refuse
    too); and "windows-only", with why, when the GPU counters give no reading on
    GPU_FAILS_TO_WINDOWS samples in a row (each still takes a sample's time). From then on a window from the folder
    that stays for DRAW_SAMPLES rounds is enough.
    """
    folder = check_folder(folder)
    if isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float)) or not math.isfinite(deadline_s) \
            or not 0 < deadline_s <= MAX_WAIT_S:
        raise ValueError(f"deadline_s must be more than 0 and at most {MAX_WAIT_S:.0f} seconds")
    if since is None:
        since = filetime_now() - SINCE_MARGIN
    elif isinstance(since, bool) or not isinstance(since, int) or since < 0:
        raise ValueError(f"not a process create time: {since!r}")
    given = check_forms(folders) if folders is not None else None
    skip = set()
    for pid, create_time in exclude:
        _check_identity(pid, create_time)
        skip.add((pid, create_time))
    if not isinstance(resolve, bool):
        raise ValueError(f"resolve= is not true or false: {_shown(resolve)}")
    roots = _roots(tuple(own))                  # text given as `own` fails there too: its letters are no processes
    pause = sleep if sleep is not None else (cancel.wait if cancel is not None else time.sleep)

    told: set[str] = set()

    def tell(state: str, **info) -> None:
        if state not in told:
            told.add(state)
            if on_status is not None:
                on_status(state, info)

    def reached(stage: int, procs_there: list[ProcInfo]) -> None:
        nonlocal furthest
        furthest = max(furthest, stage)
        for p in procs_there:
            if p.name not in names[stage - 1]:
                names[stage - 1].append(p.name)

    end = clock() + deadline_s
    tell("waiting")
    forms = given if given is not None else folder_forms(folder)
    images: dict[tuple[int, int], tuple[str | None, bool]] = {}   # (pid, create time) -> (image path, from folder)
    tries: dict[tuple[int, int], int] = {}                        # failed reads of a process not yet answered
    streak: dict[tuple[int, int], int] = {}                       # samples in a row that a process drew
    names: list[list[str]] = [[], [], []]   # the folder's programs that started, had a window, and drew
    furthest = 0                            # 0 nothing from the folder, 1 it started, 2 it had a window, 3 it drew
    unreadable = driveless = short = 0
    samples = failed = gpu_fails = round_fails = 0
    by_windows = False
    listed: list[dict] = []
    while True:
        if cancel is not None and cancel.is_set():
            raise WaitCancelled("the wait for the game was cancelled")
        if clock() >= end:
            raise TargetNotFound(_deadline_reason(folder, deadline_s, furthest, names, unreadable, driveless,
                                                  samples, failed, short), listed)
        try:
            procs = _snapshot()
            wins = windows_by_pid(windows.top_windows())
        except OSError as exc:
            round_fails += 1
            if round_fails >= ROUND_FAILS:
                raise OSError(f"Windows' process list or windows could not be read {round_fails} times in a row "
                              f"({exc})") from exc
            pause(WAIT_POLL_S)
            continue
        round_fails = 0
        me = procs.get(_own_pid())
        if me is None:
            raise OSError("lag-doctor's own process is not in Windows' process list")
        mine = self_and_ancestors(procs)
        for root in roots:
            mine |= descendants(procs, *root)
        reads = 0
        for p in procs.values():
            key = (p.pid, p.create_time)
            if key in images or p.create_time <= since or p.pid in mine or p.pid in (0, 4):
                continue
            if reads >= MAX_READS_PER_ROUND:
                break
            reads += 1
            try:
                image = ntquery.image_path(p.pid)
            except OSError:
                tries[key] = tries.get(key, 0) + 1
                if tries[key] >= IMAGE_TRIES:
                    images[key] = (None, False)
                    unreadable += 1
                continue
            images[key] = (image, program_reason(image, forms, resolve=resolve) is None)
            if image and image[:8].lower() == "\\device\\":
                driveless += 1              # a volume no drive letter or folder names (ntquery could not map it)
            elif not resolve and not images[key][1] and image and _SHORT_NAME.search(image):
                short += 1                  # compared as written, it matches no form, yet it may be the game
                tell("short-name", pid=p.pid, name=p.name)
        for table in (images, tries):
            for key in [k for k in table if k[0] not in procs or procs[k[0]].create_time != k[1]]:
                del table[key]              # gone: Windows may give its number to another program
        # lag-doctor's own processes were never read, so they are never from the folder.
        ours = [p for p in procs.values() if p.name.lower() not in IGNORE_NAMES
                and images.get((p.pid, p.create_time), (None, False))[1]
                and p.session_id == me.session_id and (p.pid, p.create_time) not in skip]
        if ours:
            reached(1, ours)
            tell("started", pid=ours[0].pid, name=ours[0].name)
        fg = windows.foreground_pid()
        windowed = [p for p in ours if p.pid in wins]
        if not windowed:
            streak.clear()
            listed = [_public(_candidate(p, None, wins, fg)) for p in ours][:MAX_CANDIDATES]
            pause(WAIT_POLL_S)
            continue
        reached(2, windowed)
        tell("window", pid=windowed[0].pid, name=windowed[0].name)
        gpu = None
        began = clock()
        if by_windows:
            pause(DRAW_SAMPLE_S)
        else:
            samples += 1
            try:
                gpu = _gpu_by_pid(sample_3d(DRAW_SAMPLE_S))
                gpu_fails = 0
            except OSError as exc:
                failed += 1
                gpu_fails += 1
                rest = DRAW_SAMPLE_S - (clock() - began)
                if rest > 0:
                    pause(rest)             # a sample that fails at once still takes a sample's time
                if gpu_fails >= GPU_FAILS_TO_WINDOWS:
                    by_windows = True
                    tell("windows-only", why=str(exc))
        try:
            after = _snapshot()             # the sample took a while: what still runs, and still owns its window
            wins_after = windows_by_pid(windows.top_windows())
        except OSError:
            continue                        # the next round reads them again, and counts its own failures
        cands = []
        for p in windowed:
            key = (p.pid, p.create_time)
            q = after.get(p.pid)
            if q is None or q.create_time != p.create_time or p.pid not in wins_after:
                streak.pop(key, None)       # it exited, or its window went away: drawing starts over
                continue
            if by_windows:
                streak[key] = streak.get(key, 0) + 1
            elif gpu is not None:
                drew = gpu.get(p.pid, (0.0, None))[0] >= AUTO_MIN_3D_PCT
                streak[key] = streak.get(key, 0) + 1 if drew else 0
            cands.append(_candidate(p, gpu, wins_after, fg))
        kept = {(c.pid, c.create_time) for c in cands}
        for key in [k for k in streak if k not in kept]:
            del streak[key]
        drawing = [p for p in windowed if streak.get((p.pid, p.create_time), 0) > 0]
        if drawing:
            reached(3, drawing)
        cands.sort(key=lambda c: (-streak.get((c.pid, c.create_time), 0), -(c.gpu_pct or 0.0), c.pid))
        listed = [_public(c) for c in cands][:MAX_CANDIDATES]
        ready = [c for c in cands if streak.get((c.pid, c.create_time), 0) >= DRAW_SAMPLES]
        if not ready:
            continue
        if cancel is not None and cancel.is_set():
            raise WaitCancelled("the wait for the game was cancelled")
        chosen = ready[0] if len(ready) == 1 else (pick(list(ready)) if pick is not None else _busiest(ready))
        if chosen is None:
            raise WaitCancelled("no game was chosen among the programs from its folder that draw")
        if chosen not in ready:
            raise ValueError("pick must return one of the candidates it was given")
        window = _main_window(wins_after.get(chosen.pid, []))
        listed = [_public(chosen)] + [c for c in listed if c["pid"] != chosen.pid]
        return {
            "pid": chosen.pid,
            "create_time": chosen.create_time,
            "name": chosen.name,
            "exe": images[(chosen.pid, chosen.create_time)][0],
            "window_title": window["title"] if window else None,
            "monitor_device": window["monitor_device"] if window else None,
            "adapter_luid": chosen.adapter if chosen.gpu_pct else None,
            "how": "folder",
            "candidates": listed[:MAX_CANDIDATES],
            "folder": folder,
            "folders": forms,
            "judged_by": "window" if by_windows else "3d",
        }


def _check_seconds(value: object, what: str, zero_ok: bool = False) -> float:
    """A length of time a caller gives: finite, more than 0 (or 0 itself with `zero_ok`), at most MAX_WAIT_S."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0 or (value == 0 and not zero_ok) or value > MAX_WAIT_S:
        lowest = "0 or more" if zero_ok else "more than 0"
        raise ValueError(f"{what} must be {lowest} and at most {MAX_WAIT_S:.0f} seconds")
    return float(value)


def _pause_of(sleep: Callable[[float], object] | None, cancel: threading.Event | None) -> Callable[[float], object]:
    """How a wait pauses: `sleep` when given, else on `cancel`, so a cancel wakes it at once, else time.sleep."""
    return sleep if sleep is not None else (cancel.wait if cancel is not None else time.sleep)


def _stop_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise WaitCancelled("the wait for the game was cancelled")


def hold_target(found: dict, *, hold_s: float = REARM_HOLD_S, cancel: threading.Event | None = None,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], object] | None = None) -> str | None:
    """Play's re-arm: watch the process the folder wait chose (its dict) for `hold_s` from now, opening no process.
    None when, on the first look at or past `hold_s`, it still runs as that process and owns a window that counts for
    auto mode, never having stopped drawing on the way; else why it is not the game, in plain words: it exited, it no
    longer owns such a window, or it drew less than AUTO_MIN_3D_PCT of a 3D engine on DRAW_SAMPLES samples in a row
    (a launcher that drew, then left or went quiet while the player clicked its Play button). A sample with no reading
    changes no count, as in the wait; once the wait judged by windows alone (`judged_by` "window"), so does the hold.
    Raises WaitCancelled, ValueError for an argument, and OSError when Windows' process list or windows cannot be read
    ROUND_FAILS rounds in a row."""
    pid, create_time = found["pid"], found["create_time"]
    _check_identity(pid, create_time)
    hold_s = _check_seconds(hold_s, "hold_s", zero_ok=True)
    name = found.get("name") or "a program from the game's folder"
    windows_only = found.get("judged_by") == "window"
    pause = _pause_of(sleep, cancel)
    began = clock()
    end = began + hold_s
    quiet = fails = 0

    def after() -> str:
        return _duration_words(float(max(1, round(clock() - began)))) + " after it was chosen"

    while True:
        _stop_if_cancelled(cancel)
        try:
            procs = _snapshot()
            shown = windows_by_pid(windows.top_windows())
        except OSError as exc:
            fails += 1
            if fails >= ROUND_FAILS:
                raise OSError(f"Windows' process list or windows could not be read {fails} times in a row "
                              f"({exc})") from exc
            pause(WAIT_POLL_S)
            continue
        fails = 0
        if not is_alive(pid, create_time, procs):
            return f"{name} (process {pid}) exited {after()}"
        if pid not in shown:
            return f"{name} (process {pid}) closed its window {after()}"
        if quiet >= DRAW_SAMPLES:
            return f"{name} (process {pid}) stopped drawing {after()}"
        if clock() >= end:
            return None
        if windows_only:
            pause(DRAW_SAMPLE_S)
            continue
        started = clock()
        try:
            gpu = _gpu_by_pid(sample_3d(DRAW_SAMPLE_S))
        except OSError:
            rest = DRAW_SAMPLE_S - (clock() - started)
            if rest > 0:
                pause(rest)                 # a sample that fails at once still takes a sample's time
            continue
        quiet = 0 if gpu.get(pid, (0.0, None))[0] >= AUTO_MIN_3D_PCT else quiet + 1


def wait_for_game(folder: str, *, folders: tuple[str, ...] | None = None, since: int | None = None,
                  deadline_s: float = WAIT_DEADLINE_S, own: Iterable[tuple[int, int]] = (), resolve: bool = True,
                  hold_s: float = REARM_HOLD_S, aim: Callable[[dict], tuple[str, bool] | None] | None = None,
                  cancel: threading.Event | None = None, on_status: Callable[[str, dict], None] | None = None,
                  pick: Callable[[list[Candidate]], Candidate | None] | None = None,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], object] | None = None) -> dict:
    """The folder wait with Play's re-arm (watch --wait-folder): wait_for_target, then hold_target on what it chose,
    then `aim` it; wait again whenever the hold fails or `aim` refuses. Returns wait_for_target's dict for the process
    that held, and that `aim` accepted. Raises what those two raise, and whatever `aim` raises.

    One deadline for all of it: each wait gets what is left of `deadline_s` (a `pick` that takes a while included),
    and none starts once it is spent: TargetNotFound then says why the last choice was not taken. A hold that begins
    just before the deadline still runs out (at most `hold_s` and one sample past it: the relay's own wait,
    server.GAME_SLACK_S, leaves room for that). `since` is taken once, before the first wait (by default
    SINCE_MARGIN before the call), so a game that starts while a launcher is held is found by the next wait, and the
    folder's forms are worked out once (or `folders` as given) and used by every wait.

    `aim(found)`: None when the game is accepted, or (why, retry) when it is refused; the process is then left out of
    later waits only when `retry` is False (asking again about it cannot change that). It raises TargetNotFound when
    no more games may be named, which then says why the ones before were not taken too. A failed hold never leaves a
    process out: a game that blocks its drawing for a moment while it loads, or makes its window again, must stay
    findable, and a launcher that keeps drawing on and off is only chosen again, inside the same deadline.

    `on_status(state, info)` hears each wait's states (wait_for_target's, "short-name" among them; each wait says
    "waiting" again), and every
    time it happens: "holding" {pid, name, seconds}, "rearm" {pid, name, why} and "refused" {pid, name, why, retry}.
    `own`, `resolve`, `cancel`, `pick`, `clock` and `sleep` as in wait_for_target."""
    deadline_s = _check_seconds(deadline_s, "deadline_s")
    _check_seconds(hold_s, "hold_s", zero_ok=True)
    since = filetime_now() - SINCE_MARGIN if since is None else since    # once, for every wait
    own = tuple(own)
    end = clock() + deadline_s
    exclude: list[tuple[int, int]] = []
    last: str | None = None

    def tell(state: str, **info) -> None:
        if on_status is not None:
            on_status(state, info)

    while True:
        left = end - clock()
        if left <= 0:
            raise TargetNotFound(f"{last or 'no game was found'}, and the {_duration_words(deadline_s)} the wait had "
                                 "are up")
        try:
            found = wait_for_target(folder, folders=folders, since=since, deadline_s=left, cancel=cancel,
                                    on_status=on_status, exclude=exclude, pick=pick, own=own, resolve=resolve,
                                    clock=clock, sleep=sleep)
        except TargetNotFound as exc:
            if last is None:
                raise
            raise TargetNotFound(f"{last}; after that, {exc.reason}", exc.candidates) from None
        folders = found["folders"]
        pid, name = found["pid"], found["name"]
        tell("holding", pid=pid, name=name, seconds=hold_s)
        why = hold_target(found, hold_s=hold_s, cancel=cancel, clock=clock, sleep=sleep)
        if why is not None:
            last = f"{why}, so it was taken for a launcher"
            tell("rearm", pid=pid, name=name, why=why)
            continue
        if aim is None:
            return found
        try:
            refused = aim(found)
        except TargetNotFound as exc:           # no more games may be named: why the ones before were not taken
            if last is None:
                raise
            raise TargetNotFound(f"{last}; after that, {exc.reason}", exc.candidates) from None
        if refused is None:
            return found
        why, retry = refused
        last = f"the relay refused {name} (process {pid}): {why}"
        tell("refused", pid=pid, name=name, why=why, retry=retry)
        if not retry:
            exclude.append((pid, found["create_time"]))
