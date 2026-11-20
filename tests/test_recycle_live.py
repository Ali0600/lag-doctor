"""The real move to the Recycle Bin (lagdoctor/winapi/recycle.py), on GitHub's runner only: anywhere else it would put
a folder in that PC's own Recycle Bin (scripts/stage2/guard.py says how the runner is told apart).

A child process moves a made-up recording's folder, so a dialog Windows raised could only hang that child, which the
timeout ends. The folder is then found in the Recycle Bin of its drive by the record Windows keeps beside each item
there ($I..., which holds the item's former path; the item itself is $R... of the same name), its file read back, and
both taken out again. tests/test_recycle.py checks everything else with the call replaced."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stage2 import guard  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MOVE = "import sys; from lagdoctor.winapi import recycle; recycle.to_recycle_bin(sys.argv[1], None)"
pytestmark = pytest.mark.skipif(not guard.on_throwaway_runner(),
                                reason="moves a folder into this PC's Recycle Bin: GitHub's runner only")


def _user_sid() -> str:
    """This account's SID, as `whoami /user` gives it: the name of its folder in each drive's Recycle Bin."""
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True,
                         timeout=60).stdout
    return out.strip().rsplit(",", 1)[-1].strip('"')


def test_a_folder_moved_to_the_recycle_bin_is_found_there_with_its_files(tmp_path):
    folder = tmp_path / "20261120-100000-moss_garden"
    folder.mkdir()
    marker = os.urandom(16).hex()
    (folder / "summary.json").write_text(marker, encoding="utf-8")
    path = os.path.abspath(folder)
    forms = {path.casefold(), os.path.realpath(path).casefold()}       # with any short name of the profile's spelled out
    moved = subprocess.run([sys.executable, "-c", MOVE, path], cwd=ROOT, capture_output=True, text=True,
                           timeout=120)                       # the child imports this tree's lagdoctor: cwd is first
    assert moved.returncode == 0, moved.stderr
    assert not os.path.lexists(path)
    bin_folder = Path(path[:2] + "\\$Recycle.Bin") / _user_sid()
    infos = [p for p in bin_folder.iterdir() if p.name.startswith("$I")
             and any(f in p.read_bytes().decode("utf-16-le", errors="ignore").casefold() for f in forms)]
    assert len(infos) == 1, sorted(p.name for p in bin_folder.iterdir())
    kept = bin_folder / ("$R" + infos[0].name[2:])
    try:
        assert (kept / "summary.json").read_text(encoding="utf-8") == marker
    finally:
        shutil.rmtree(kept, ignore_errors=True)
        infos[0].unlink(missing_ok=True)
