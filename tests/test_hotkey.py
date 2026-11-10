"""The 'it lagged now' hotkey: parsing, registration, delivery, clean release."""
from __future__ import annotations

import threading

import pytest

from lagdoctor.winapi import hotkey
from lagdoctor.winapi.hotkey import HotkeyListener, parse_hotkey

# Unlikely combinations, so the test never fights the user's own shortcuts (tests/conftest.py allows only these).
TEST_KEY = "ctrl+alt+shift+f24"
OTHER_KEY = "ctrl+alt+shift+f23"


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
        assert "already used" in rival.error and rival.taken is True and owner.taken is False
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


def test_two_listeners_hold_two_keys_at_once_and_each_press_reaches_only_its_own():
    # watch's lag key and start key (M5): one listener, one thread, per key, both under the same id.
    heard: dict[str, list[float]] = {TEST_KEY: [], OTHER_KEY: []}
    events = {TEST_KEY: threading.Event(), OTHER_KEY: threading.Event()}

    def on(key):
        def on_press(t: float) -> None:
            heard[key].append(t)
            events[key].set()
        return on_press
    a, b = HotkeyListener(TEST_KEY, on(TEST_KEY)), HotkeyListener(OTHER_KEY, on(OTHER_KEY))
    try:
        assert a.start(), a.error
        assert b.start(), b.error
        assert a._thread_id != b._thread_id
        assert b._simulate_press() and events[OTHER_KEY].wait(5)
        assert a._simulate_press() and events[TEST_KEY].wait(5)
        assert len(heard[TEST_KEY]) == len(heard[OTHER_KEY]) == 1
    finally:
        a.stop()
        b.stop()
    assert not a.registered and not b.registered


def test_the_suites_guard_refuses_a_real_key_and_catches_a_real_sound(_no_real_keys_or_sounds, tmp_path):
    # tests/conftest.py's tripwire: no test registers a key the owner may use or makes a sound; each is caught.
    from lagdoctor.app import sounds
    lst = HotkeyListener("ctrl+alt+s", lambda t: None)
    assert lst.start() is False and lst._thread is None              # refused before any thread or registration
    assert sounds.Player(tmp_path, log=lambda s: None).play("start") is True       # PlaySound is the guard's
    caught = list(_no_real_keys_or_sounds.caught)
    assert caught[0] == "a test tried to register the real key 'ctrl+alt+s'" and len(caught) == 2
    assert caught[1].startswith("a sound played")
    _no_real_keys_or_sounds.caught.clear()                            # this test's own: it ends clean
