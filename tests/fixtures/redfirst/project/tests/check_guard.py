"""Checks of the stand-in guard, run by the red-first runner under test (tests/test_redfirst.py).

Named check_*.py so the real suite never collects them. Some misbehave on purpose; each is named by the
spec that needs it.
"""
from __future__ import annotations

import os
import py_compile
import subprocess
import sys
import time
from pathlib import Path

from lagdoctor import guard

# A process started by a check that hangs: says its own id and the check's (written whole, then renamed), then
# outlives the check unless whoever ends the check's process ends everything under it too.
HANG_CHILD = """\
import json, os, pathlib, sys, time
marker = pathlib.Path(sys.argv[1])
part = marker.with_name(marker.name + ".part")
part.write_text(json.dumps({"check": int(sys.argv[2]), "child": os.getpid()}), encoding="utf-8")
os.replace(part, marker)
time.sleep(300)
"""


def test_pid_zero_is_refused():
    assert guard.refuses(0)


def test_a_normal_pid_is_allowed():
    assert not guard.refuses(1234)


def test_always_red():
    raise AssertionError("red on purpose: a baseline that is not green")


def test_blocks_while_sabotaged():
    # The hard-kill test kills the runner here. Under the sabotage this says where the guard was loaded
    # from (written whole, then renamed, so the marker never shows half a path), then waits to be killed.
    if guard.refuses(0):
        return
    marker = Path(os.environ["REDFIRST_FIXTURE_MARKER"])
    part = marker.with_name(marker.name + ".part")
    part.write_text(guard.__file__, encoding="utf-8")
    os.replace(part, marker)
    time.sleep(60)
    raise AssertionError("not killed within 60 s")


def test_hangs_while_sabotaged():
    # The timeout test: under the sabotage this starts a process of its own and waits far past the runner's timeout.
    # That process's output goes nowhere, so only the process itself outlives a kill that misses it.
    if guard.refuses(0):
        return
    subprocess.Popen([sys.executable, "-c", HANG_CHILD, os.environ["REDFIRST_FIXTURE_MARKER"], str(os.getpid())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(120)
    raise AssertionError("not ended within 120 s")


def test_always_hangs():
    time.sleep(120)
    raise AssertionError("not ended within 120 s: a baseline that never finishes")


def test_reads_the_real_tree():
    # Reads the scratch project's own guard by its real path instead of the copy's.
    real = Path(os.environ["REDFIRST_FIXTURE_REAL"]) / "lagdoctor" / "guard.py"
    assert "def refuses" in real.read_text(encoding="utf-8")
    assert guard.refuses(0)


def test_lists_the_real_root_again():
    # As when a file is saved in the real checkout's root mid-run, with that root on sys.path (the venv's editable
    # install puts it there): the next import lookup and metadata scan list the folder again.
    import importlib.metadata
    real = os.environ["REDFIRST_FIXTURE_REAL"]
    st = os.stat(real)
    os.utime(real, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    try:
        import no_such_module_anywhere  # noqa: F401
    except ImportError:
        pass
    list(importlib.metadata.distributions())
    assert guard.refuses(0)


def test_exits_without_a_report():
    os._exit(0)          # no atexit handlers run, so the runner's probe never reports


def test_fakes_a_red_without_a_report(capsys):
    # Under the sabotage: a FAILED line and exit code 1, as a real red looks, but no probe report.
    if guard.refuses(0):
        return
    with capsys.disabled():
        print("FAILED tests/check_guard.py::test_fakes_a_red_without_a_report - faked", flush=True)
    os._exit(1)


def test_compiles_the_guard():
    # Leaves bytecode next to the guard in the copy, as a child started with -I would.
    py_compile.compile(guard.__file__, doraise=True)
    assert guard.refuses(0)


def test_writes_into_its_own_tree():
    Path(__file__).with_name("left_behind.txt").write_text("a test wrote into the tree\n", encoding="utf-8")
    assert guard.refuses(0)
