"""Shared test fixtures.

Builders: put fixtures only your own tests use in your own test module, not here.
"""
from __future__ import annotations

import os
import uuid
import winreg
from pathlib import Path

import pytest

from lagdoctor.fixes.registry import RegistryBackend
from tests import capabilities, downloads

SANDBOX_PARENT = r"Software\LagDoctorTest"


@pytest.fixture(scope="session")
def presentmon_exe() -> Path:
    """The PresentMon 2.6.0 exe, checked against the tests' pin (tests/downloads.py). Skips with the reason when it
    was never downloaded, unless LAGDOCTOR_REQUIRE names it; fails when its bytes are not the pinned release's."""
    return downloads.for_fixture("presentmon")


@pytest.fixture(scope="session")
def gold_etl_dir() -> Path:
    """PresentMon's own gold ETW captures (Tests/Gold of v2.6.0), for --etl_file replays without admin. Skips like
    presentmon_exe; fetch them with `uv run python -m tests.downloads fetch-gold`."""
    return downloads.for_fixture("presentmon_gold")


@pytest.fixture(scope="session")
def webview2_idl() -> Path:
    """WebView2.idl of Microsoft's WebView2 SDK 1.0.4314.35, checked against the tests' pin, for the vtable slots and
    IIDs that lagdoctor/winapi/webview2.py calls. Skips like presentmon_exe; fetch it with `uv run python -m
    tests.downloads fetch-webview2-idl`."""
    return downloads.for_fixture("webview2_idl")


@pytest.fixture
def reg_sandbox():
    """A RegistryBackend whose every path lands under HKCU\\Software\\LagDoctorTest\\<uuid>.

    The whole sandbox (and the parent key, once empty) is deleted afterwards, and
    the deletion is checked with an independent winreg open.
    """
    backend = RegistryBackend(prefix=f"{SANDBOX_PARENT}\\{uuid.uuid4().hex}")
    yield backend
    backend.delete_tree()
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, backend.prefix)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, SANDBOX_PARENT)   # only succeeds when empty
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test may touch the real %LOCALAPPDATA%\\lag-doctor (journal, captures)."""
    monkeypatch.setenv("LAGDOCTOR_HOME", str(tmp_path / "lagdoctor-home"))


def pytest_configure(config):
    config.addinivalue_line("markers", "needs(*names): what the test needs from the machine; it skips when "
                            f"{capabilities.ABSENT_ENV} lists one of them (tests/capabilities.py)")
    downloads.required(os.environ)                  # a misspelt LAGDOCTOR_REQUIRE stops the run before it starts
    for line in downloads.TALLIES:                  # a line each in the summary (downloads.TALLIES)
        config.pluginmanager.register(downloads.Tally(os.environ, line), f"lagdoctor-download-tally-{line}")


def pytest_collection_modifyitems(config, items):
    capabilities.apply_absent(items, os.environ)
