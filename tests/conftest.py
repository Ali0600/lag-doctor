"""Shared test fixtures.

Builders: put fixtures only your own tests use in your own test module, not here.
"""
from __future__ import annotations

import hashlib
import os
import uuid
import winreg
from pathlib import Path

import pytest

from lagdoctor.fixes.registry import RegistryBackend
from tests import capabilities

SANDBOX_PARENT = r"Software\LagDoctorTest"

# The real PresentMon download, outside LAGDOCTOR_HOME (which every test redirects).
PRESENTMON_VERSION = "2.6.0"
PRESENTMON_SHA256 = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


@pytest.fixture(scope="session")
def presentmon_exe() -> Path:
    """The verified PresentMon 2.6.0 exe. Skips (with the reason) when it was never downloaded."""
    exe = (_localappdata() / "lag-doctor" / "presentmon" / PRESENTMON_VERSION
           / f"PresentMon-{PRESENTMON_VERSION}-x64.exe")
    if not exe.is_file():
        pytest.skip(f"PresentMon not downloaded ({exe}); run `uv run python -m lagdoctor setup`")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    assert digest == PRESENTMON_SHA256, f"{exe} has SHA-256 {digest}, expected the pinned 2.6.0 release"
    return exe


@pytest.fixture(scope="session")
def gold_etl_dir() -> Path:
    """PresentMon's own gold ETW captures (Tests/Gold of v2.6.0), for --etl_file replays without admin."""
    d = Path(os.environ.get("LAGDOCTOR_TESTDATA") or _localappdata() / "lag-doctor" / "testdata") / "presentmon-gold"
    if not (d / "test_case_0.etl").is_file():
        pytest.skip(f"gold ETL captures not found in {d} (see {d / 'SOURCE.txt'} for where they come from)")
    return d


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


def pytest_collection_modifyitems(config, items):
    capabilities.apply_absent(items, os.environ)
