"""The machine-wide trace lock (lagdoctor/capture/tracelock.py), live on this PC under names of the tests' own.

Unelevated (the laptop) a token may set only its own account as a mutex's owner, so the locks these tests hold are
owned by it, and trusted only where a test says so (MINE). Elevated (CI's runner) a lock owned by Administrators is held
too, and read busy with the real trusted set. tests/conftest.py gives every test its own tracelock.NAME, in the Global\\
namespace, and fails one that reaches the real name. Each name is gone with its last handle, so nothing outlives a test.
"""
from __future__ import annotations

import ast
import ctypes
import re
import threading
import uuid
from ctypes import wintypes
from pathlib import Path

import pytest

from lagdoctor.capture import tracelock
from lagdoctor.relay import protocol
from lagdoctor.winapi import elevation, pipe
from lagdoctor.winapi import service as scm
from tests import conftest

ME = elevation.token_user_sid()
MINE = {ME}                                       # a trusted set that takes this account's own locks for lag-doctor's
QUERY = elevation.READ_CONTROL | elevation.MUTANT_QUERY_STATE     # what everyone may ask of the mutex
FULL = "0x1f0001"                                 # MUTEX_ALL_ACCESS, as Windows writes GA back in SDDL
DACL_INFORMATION = 0x4                            # winnt.h: DACL_SECURITY_INFORMATION


def _state() -> tuple[int, bool, bool] | None:
    """The mutex under this test's name, read through a handle with everyone's rights; None when there is none."""
    h = elevation.open_mutex(tracelock.NAME, QUERY)
    if h is None:
        return None
    try:
        return elevation.mutex_state(h)
    finally:
        elevation.release_named_mutex(h)


def _shipped_name() -> str:
    """tracelock.NAME as its module assigns it (the fixture has moved the attribute)."""
    tree = ast.parse(Path(tracelock.__file__).read_text(encoding="utf-8"))
    (value,) = [n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "NAME" for t in n.targets)]
    return value


def test_the_lock_trusts_system_administrators_and_the_capture_service_and_tests_never_use_its_name():
    assert tracelock.trusted_owners() == {"S-1-5-18", "S-1-5-32-544", scm.service_sid(protocol.SERVICE_NAME)}
    assert _shipped_name() == conftest.REAL_TRACE_LOCK == "Global\\lag-doctor-trace"
    assert re.fullmatch(r"Global\\lagdoctor-test-[0-9a-f]{32}", tracelock.NAME), tracelock.NAME


def test_a_new_lock_is_held_owned_as_asked_and_everyone_else_may_only_read_it():
    with tracelock.TraceLock(ME, trusted=MINE) as lock:
        assert lock.acquire() == tracelock.HELD and lock.held
        h = elevation.open_mutex(tracelock.NAME, QUERY)
        try:
            assert elevation.mutex_owner(h) == ME
            count, _mine, abandoned = elevation.mutex_state(h)
            assert count <= 0 and not abandoned                       # a thread holds it
            dacl = pipe.descriptor_of(h, DACL_INFORMATION)
            with pytest.raises(OSError) as no_wait:                   # everyone's rights cannot take it
                elevation.take_mutex(h)
            assert no_wait.value.winerror == elevation.ERROR_ACCESS_DENIED
        finally:
            elevation.release_named_mutex(h)
    assert dacl.startswith("D:P"), dacl
    aces = dict((who, rights) for rights, who in re.findall(r"\(A;;([^;]*);;;([^)]*)\)", dacl))
    assert aces == {"SY": FULL, "BA": FULL, scm.service_sid(protocol.SERVICE_NAME): FULL, ME: FULL,
                    "WD": "CCRC"}, dacl                               # CCRC: 0x1 and READ_CONTROL, no SYNCHRONIZE
    assert _state() is None                                           # let go: gone with its last handle


def test_a_second_lock_is_busy_while_the_first_holds_it_and_holds_it_once_it_is_let_go():
    first, second = tracelock.TraceLock(ME, trusted=MINE), tracelock.TraceLock(ME, trusted=MINE)
    assert first.acquire() == tracelock.HELD
    try:
        assert second.acquire() == tracelock.BUSY and not second.held
        assert tracelock.check(trusted=MINE) == tracelock.BUSY
        # With the real trusted set, this account's own lock is no lag-doctor recording: the protection is lost.
        assert tracelock.TraceLock(ME).acquire() == tracelock.TAKEN and tracelock.check() == tracelock.TAKEN
    finally:
        first.release()
    assert tracelock.check(trusted=MINE) == tracelock.FREE and _state() is None
    assert second.acquire() == tracelock.HELD                          # asked again, not its old answer
    second.release()
    assert first.acquire() == tracelock.HELD                           # a lock let go may be taken again
    first.release()
    assert _state() is None


def test_the_lock_is_held_by_a_thread_of_its_own_so_the_thread_that_took_it_may_end():
    lock = tracelock.TraceLock(ME, trusted=MINE)
    t = threading.Thread(target=lock.acquire)
    t.start()
    t.join(10)
    try:
        assert lock.held and tracelock.check(trusted=MINE) == tracelock.BUSY   # a mutex its thread left reads free
    finally:
        lock.release()
    assert tracelock.check(trusted=MINE) == tracelock.FREE


def test_a_handle_left_open_keeps_the_name_but_not_the_lock():
    lock = tracelock.TraceLock(ME, trusted=MINE)
    assert lock.acquire() == tracelock.HELD
    kept = elevation.open_mutex(tracelock.NAME, QUERY)                  # any account may keep one: everyone may read
    try:
        lock.release()
        assert _state() == (1, False, False)                           # still there, held by nobody
        assert tracelock.check(trusted=MINE) == tracelock.FREE
        again = tracelock.TraceLock(ME, trusted=MINE)
        assert again.acquire() == tracelock.HELD
        again.release()
    finally:
        elevation.release_named_mutex(kept)


def test_a_holder_that_ended_holding_the_lock_never_blocks_the_next_recording():
    left = {}

    def ends_holding_it() -> None:
        left["handle"], left["made"] = elevation.create_mutex(tracelock.NAME, tracelock.descriptor(ME), own=True)

    t = threading.Thread(target=ends_holding_it)
    t.start()
    t.join(10)
    try:
        assert left["made"] and _state() == (1, False, True)            # free, and abandoned
        assert tracelock.check(trusted=MINE) == tracelock.FREE
        lock = tracelock.TraceLock(ME, trusted=MINE)
        assert lock.acquire() == tracelock.HELD
        assert tracelock.check(trusted=MINE) == tracelock.BUSY
        lock.release()
    finally:
        elevation.release_named_mutex(left.get("handle"))


def test_a_name_another_kind_of_object_holds_is_taken():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    event = k32.CreateEventW(None, True, False, tracelock.NAME)
    assert event, ctypes.get_last_error()
    try:
        lock = tracelock.TraceLock(ME, trusted=MINE)
        assert lock.acquire() == tracelock.TAKEN and not lock.held
        assert tracelock.check(trusted=MINE) == tracelock.TAKEN
    finally:
        k32.CloseHandle(event)


def test_a_mutex_whose_dacl_shuts_lag_doctor_out_is_taken():
    # A squatter's mutex, made first: its owner (this token) may only wait on it, and nobody may read it.
    squatter, made = elevation.create_mutex(tracelock.NAME, f"O:{ME}D:P(A;;0x100000;;;OW)", own=False)
    try:
        assert made
        lock = tracelock.TraceLock(ME, trusted=MINE)
        assert lock.acquire() == tracelock.TAKEN and not lock.held
        assert tracelock.check(trusted=MINE) == tracelock.TAKEN
    finally:
        elevation.release_named_mutex(squatter)


def test_an_existing_lock_is_asked_only_to_be_waited_on_and_read():
    """Never MUTEX_ALL_ACCESS, which CreateMutexW asks of an existing mutex: a DACL that grants this account only
    SYNCHRONIZE and READ_CONTROL (OW: its owner gets no more either) still lets it take a lock nobody holds."""
    other, made = elevation.create_mutex(tracelock.NAME, f"O:{ME}D:P(A;;0x120000;;;OW)(A;;0x120000;;;{ME})",
                                         own=False)
    try:
        assert made
        lock = tracelock.TraceLock(ME, trusted=MINE)
        assert lock.acquire() == tracelock.HELD
        lock.release()
    finally:
        elevation.release_named_mutex(other)


def test_an_administrators_owner_is_refused_to_this_token_unless_it_is_elevated():
    """What lag-doctor's elevated processes hold. Unelevated (this laptop's UAC-filtered token) Windows refuses that
    owner with error 1307 and makes nothing; elevated (CI's runner) the lock is held, and read busy."""
    lock = tracelock.TraceLock(tracelock.ADMINISTRATORS)
    if not elevation.is_elevated():
        with pytest.raises(OSError) as refused:
            lock.acquire()
        assert refused.value.winerror == elevation.ERROR_INVALID_OWNER and not lock.held
        assert _state() is None
        return
    try:
        assert lock.acquire() == tracelock.HELD
        assert tracelock.TraceLock(tracelock.ADMINISTRATORS).acquire() == tracelock.BUSY
        assert tracelock.check() == tracelock.BUSY
    finally:
        lock.release()
    assert tracelock.check() == tracelock.FREE


def test_check_never_makes_the_lock_nor_takes_it():
    assert tracelock.check(trusted=MINE) == tracelock.FREE and _state() is None
    lock = tracelock.TraceLock(ME, trusted=MINE)
    assert lock.acquire() == tracelock.HELD
    try:
        assert [tracelock.check(trusted=MINE) for _ in range(3)] == [tracelock.BUSY] * 3
    finally:
        lock.release()


def test_any_other_answer_from_windows_is_raised_so_the_caller_refuses(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(None, "a device attached to the system is not functioning", None, 31)

    monkeypatch.setattr(elevation, "create_mutex", broken)
    monkeypatch.setattr(elevation, "open_mutex", broken)
    lock = tracelock.TraceLock(ME, trusted=MINE)
    with pytest.raises(OSError):
        lock.acquire()
    assert not lock.held
    with pytest.raises(OSError):
        tracelock.check(trusted=MINE)


def test_an_owner_that_is_not_a_sid_never_reaches_a_descriptor():
    for bad in ("S-1-5-18)(A;;GA;;;WD", "BA", "", None):
        with pytest.raises(ValueError):
            tracelock.TraceLock(bad)


def test_release_is_safe_twice_and_before_acquire_and_a_with_block_lets_go():
    lock = tracelock.TraceLock(ME, trusted=MINE)
    lock.release()
    with lock:
        assert lock.acquire() == tracelock.HELD
        assert lock.acquire() == tracelock.HELD                       # holds nothing more
    assert tracelock.check(trusted=MINE) == tracelock.FREE and not lock.held
    lock.release()


def test_the_guard_keeps_tests_from_this_pcs_trace_lock(monkeypatch):
    reached: list[str] = []

    def made(sa, name, flags, access):              # one layer below the guard: a sabotaged guard reaches these
        reached.append(name)
        ctypes.set_last_error(elevation.ERROR_INVALID_HANDLE)
        return None

    def opened(access, inherit, name):
        reached.append(name)
        ctypes.set_last_error(elevation.ERROR_FILE_NOT_FOUND)
        return None

    monkeypatch.setattr(elevation, "_CreateMutexExW", made)
    monkeypatch.setattr(elevation, "_OpenMutexW", opened)
    guard = conftest.trace_lock_guard(monkeypatch)
    caught = next(guard)
    with pytest.raises(OSError):
        elevation.create_mutex(conftest.REAL_TRACE_LOCK, tracelock.descriptor(ME), own=True)
    assert elevation.open_mutex("Local\\LAG-DOCTOR-TRACE", QUERY) is None    # in any namespace, in any case
    assert len(caught) == 2 and reached == []
    other = f"Global\\lagdoctor-test-{uuid.uuid4().hex}"
    assert elevation.open_mutex(other, QUERY) is None                         # another name goes through
    assert reached == [other]
    with pytest.raises(AssertionError):
        next(guard)
