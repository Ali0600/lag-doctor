"""Where lag-doctor keeps its files.

Everything lives under %LOCALAPPDATA%\\lag-doctor unless LAGDOCTOR_HOME points
elsewhere (tests point it at a temporary folder, so they never touch the real
fix journal or captures).

private_copy() tells when Windows keeps what this process saves there in another
app's private folder instead, out of sight of the user's other windows.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Where Windows keeps a packaged (MSIX) app's private copy of AppData: %LOCALAPPDATA%\Packages\<app>\LocalCache\
# followed by Local, Roaming or LocalLow.
_PRIVATE_COPY = re.compile(r"\\Packages\\([^\\]+)\\LocalCache\\", re.IGNORECASE)


@dataclass(frozen=True)
class PrivateCopy:
    app: str        # the packaged app's family name, e.g. "Claude_pzs8sxrjxfjjc"
    folder: str     # the folder the files really land in, without the \\?\ prefix


def home() -> Path:
    override = os.environ.get("LAGDOCTOR_HOME")
    if override:
        root = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "lag-doctor"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_dir() -> Path:
    """One sub-folder per capture session: the capture file and its HTML report."""
    d = home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweeps_dir() -> Path:
    """One sub-folder per settings sweep: its state (sweep.json), a copy of the settings file as it was, and its runs'
    recordings. Apart from sessions_dir, so the app's library lists only ordinary recordings."""
    d = home() / "sweeps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def folder_stem(name: object) -> str:
    """The game's part of a session folder's name, from the game's exe name: "ff7rebirth_.exe" is "ff7rebirth_".
    Letters, digits, '-', '_' and '.' are kept, anything else is '_'; the extension goes; at most 40 characters;
    "game" when nothing is left. capture.session.session_dir names folders with it, and the app groups a game's
    sessions by it."""
    text = name if isinstance(name, str) and name else "game"
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)
    return stem.rsplit(".", 1)[0].strip("._")[:40] or "game"


def journal_path() -> Path:
    """The fix journal: every change lag-doctor made, with the value it replaced."""
    return home() / "fix-journal.json"


def presentmon_dir(version: str) -> Path:
    return home() / "presentmon" / version


def private_copy(folder: Path | None = None) -> PrivateCopy | None:
    r"""The other app's private folder that new files written in `folder` (default: home()) land in, or None.

    A program started by a packaged (MSIX) app can have what it writes in %LOCALAPPDATA% and %APPDATA% kept in
    that app's private folder, %LOCALAPPDATA%\Packages\<app>\LocalCache\...: it and its own child processes
    see those files at the usual path, but the user's other windows and an elevated window never do. Measured
    here on 2026-11-05: a shell that the Claude desktop app started has no package identity of its own
    (GetCurrentPackageFullName answers APPMODEL_ERROR_NO_PACKAGE), yet a file it makes in %LOCALAPPDATA% lands
    in ...\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local; %TEMP% and the user's home folder are not moved.
    So this asks Windows where a new file really lands, never whether the process belongs to a package.
    Raises OSError when no file can be made in `folder`.
    """
    from lagdoctor.winapi import storage
    asked = os.path.abspath(str(home() if folder is None else folder))
    return redirected(asked, storage.new_file_path(asked))


def private_copy_advice(copy: PrivateCopy) -> str:
    """Where the files go and what to do about it, in plain words. When this very process is the packaged app (a
    Microsoft Store Python), another terminal cannot help: only another Python can."""
    from lagdoctor.winapi import storage
    try:
        own = storage.own_package_family()
    except OSError:
        own = None
    if own and own.lower() == copy.app.lower():
        return (f"lag-doctor runs on a packaged Python ({copy.app}), which keeps every file it saves in its own "
                f"private folder ({copy.folder}), where your browser and other programs never look. Make "
                "lag-doctor's environment with a Python from python.org, or one that uv installs, instead.")
    return (f"Windows keeps the files this window saves in a private folder of the app {copy.app} ({copy.folder}), "
            "and your other windows, the administrator recording window included, cannot see that folder. Open "
            "PowerShell from the Start menu and run lag-doctor there.")


def redirected(asked: str, landed: str) -> PrivateCopy | None:
    """Pure: the private copy that `landed` (a new file's real path) is in, when the folder `asked` for is not
    itself inside one."""
    m = _PRIVATE_COPY.search(landed)
    if m is None or _PRIVATE_COPY.search(asked.replace("/", "\\")):
        return None
    folder = landed.rsplit("\\", 1)[0]
    if folder[:8].upper() == "\\\\?\\UNC\\":         # \\?\UNC\server\share\... is shown as \\server\share\...
        folder = "\\\\" + folder[8:]
    elif folder.startswith("\\\\?\\"):
        folder = folder[4:]
    return PrivateCopy(app=m.group(1), folder=folder)
