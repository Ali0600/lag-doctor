"""The 'it lagged now' hotkey: parsing, registration, delivery, clean release."""
from __future__ import annotations

import threading

import pytest

from lagdoctor.winapi import hotkey
from lagdoctor.winapi.hotkey import HotkeyListener, parse_hotkey

# An unlikely combination, so the test never fights the user's own shortcuts.
TEST_KEY = "ctrl+alt+shift+f24"


@pytest.mark.parametrize("text, expected", [
    ("ctrl+alt+l", (hotkey.MOD_CONTROL | hotkey.MOD_ALT, ord("L"))),
    ("Shift+F11", (hotkey.MOD_SHIFT, 0x7A)),
    ("scrolllock", (0, 0x91)),
    ("ctrl+9", (hotkey.MOD_CONTROL, ord("9"))),
])
def test_parse_hotkey_literal_points(text, expected):
    assert parse_hotkey(text) == expected


@pytest.mark.parametrize("bad", ["", "ctrl+", "hyper+l", "ctrl+f25", "ctrl+volumeup"])
def test_parse_hotkey_rejects_unclear_input(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_listener_registers_delivers_presses_and_releases_the_key():
    presses: list[float] = []
    got = threading.Event()

    def on_press(t: float) -> None:
        presses.append(t)
        got.set()

    first = HotkeyListener(TEST_KEY, on_press)
    assert first.start(), first.error
    try:
        assert first._simulate_press()
        assert got.wait(5), "the press never reached the callback"
        assert len(presses) == 1
    finally:
        first.stop()
    assert first._thread is None and not first.registered
    # Released: a new listener can take the same key.
    again = HotkeyListener(TEST_KEY, lambda t: None)
    try:
        assert again.start(), again.error
    finally:
        again.stop()


def test_a_key_already_taken_is_reported_not_raised():
    owner = HotkeyListener(TEST_KEY, lambda t: None)
    assert owner.start(), owner.error
    try:
        rival = HotkeyListener(TEST_KEY, lambda t: None)
        assert rival.start() is False
        assert "already used" in rival.error
        rival.stop()
    finally:
        owner.stop()


def test_a_crashing_callback_does_not_stop_the_listener():
    calls = []
    second = threading.Event()

    def flaky(t: float) -> None:
        calls.append(t)
        if len(calls) == 1:
            raise RuntimeError("boom")
        second.set()

    lst = HotkeyListener(TEST_KEY, flaky)
    assert lst.start(), lst.error
    try:
        lst._simulate_press()
        lst._simulate_press()
        assert second.wait(5)
    finally:
        lst.stop()
