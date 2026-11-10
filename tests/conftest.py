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


# The only keys a test may register for real (tests/test_hotkey.py): combinations nobody presses by chance.
TEST_KEYS = ("ctrl+alt+shift+f23", "ctrl+alt+shift+f24")


class QuietWorld:
    """What _no_real_keys_or_sounds caught (`caught`, each a failure) and the chimes a console `watch` asked for
    (`played`, by name: cli.console_sound's stand-in)."""

    def __init__(self) -> None:
        self.caught: list[str] = []
        self.played: list[str] = []
        self.sound_ok = True


@pytest.fixture(autouse=True)
def _no_real_keys_or_sounds(monkeypatch):
    """No test registers a key the owner may use (Ctrl+Alt+L, Ctrl+Alt+S: their own watch or sweep may hold it), and
    none makes a sound. Since `watch` measures from a start key by default, a test that reaches the real listener
    would. A real registration or sound is caught and fails the test at its end: the code under test swallows what a
    refusal raises (a key that cannot register is only a note, and sounds.Player.play never raises). A console
    `watch` (cli._watch_here) gets a stand-in player that only counts. A test's own patch overrides these."""
    import winsound

    from lagdoctor import cli
    from lagdoctor.winapi import hotkey
    world = QuietWorld()
    real_start = hotkey.HotkeyListener.start

    def start(self, *args, **kwargs):
        if self.hotkey not in TEST_KEYS:
            world.caught.append(f"the real key {self.hotkey!r} was registered")
            self.error = "a test may not register a real key"
            return False
        return real_start(self, *args, **kwargs)

    def play(name: str) -> bool:
        world.played.append(name)
        return world.sound_ok

    monkeypatch.setattr(hotkey.HotkeyListener, "start", start)
    monkeypatch.setattr(winsound, "PlaySound",
                        lambda *args, **kwargs: world.caught.append(f"a sound played: {args[:1]}"))
    monkeypatch.setattr(cli, "console_sound", lambda: play)
    yield world
    assert not world.caught, "; ".join(world.caught)


def pytest_configure(config):
    config.addinivalue_line("markers", "needs(*names): what the test needs from the machine; it skips when "
                            f"{capabilities.ABSENT_ENV} lists one of them (tests/capabilities.py)")
    downloads.required(os.environ)                  # a misspelt LAGDOCTOR_REQUIRE stops the run before it starts
    for line in downloads.TALLIES:                  # a line each in the summary (downloads.TALLIES)
        config.pluginmanager.register(downloads.Tally(os.environ, line), f"lagdoctor-download-tally-{line}")


def pytest_collection_modifyitems(config, items):
    capabilities.apply_absent(items, os.environ)
