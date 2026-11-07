"""winapi/etw.py against this machine: struct layouts, orphan cleanup, the unelevated refusal, and a
private in-process session round trip through the real consumer (NE 5.6), all unelevated.

Every session gets a unique name and is stopped in `finally`.
"""
from __future__ import annotations

import atexit
import ctypes
import gc
import os
import socket
import struct
import sys
import uuid
import weakref
from ctypes import wintypes

import pytest

from lagdoctor import clock
from lagdoctor.capture import netflows
from lagdoctor.winapi import etw

KERNEL_NETWORK = "7dd42a49-5329-4832-8dfd-43d979153a88"     # NE 4.1


def _name() -> str:
    return f"lagdoctor-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def provider():
    p = etw.Provider(str(uuid.uuid4()))
    try:
        yield p
    finally:
        p.close()


def _udp_v4_recv(pid: int, size: int, daddr: str, saddr: str, dport: int, sport: int, seq: int,
                 connid: int) -> bytes:
    """A payload shaped exactly like Kernel-Network event 43 (NE 4.3: 28 bytes, ports big-endian)."""
    return (struct.pack("<II", pid, size) + socket.inet_aton(daddr) + socket.inet_aton(saddr)
            + struct.pack(">HH", dport, sport) + struct.pack("<II", seq, connid))


def _write_43(prov: etw.Provider, payload: bytes) -> int:
    return prov.write(43, payload, version=0, level=4, opcode=43, task=11, keyword=0x10)


# ---------------------------------------------------------------- layouts ---

@pytest.mark.parametrize("cls, size", [
    (etw.GUID, 16), (etw.WNODE_HEADER, 48), (etw.EVENT_TRACE_PROPERTIES, 120), (etw.EVENT_TRACE_HEADER, 48),
    (etw.EVENT_TRACE, 88), (etw.TIME_ZONE_INFORMATION, 172), (etw.TRACE_LOGFILE_HEADER, 280),
    (etw.EVENT_TRACE_LOGFILEW, 448), (etw.EVENT_DESCRIPTOR, 16), (etw.EVENT_HEADER, 80),
    (etw.ETW_BUFFER_CONTEXT, 4), (etw.EVENT_RECORD, 112), (etw.EVENT_HEADER_EXTENDED_DATA_ITEM, 16),
    (etw.ENABLE_TRACE_PARAMETERS, 48), (etw.EVENT_FILTER_DESCRIPTOR, 16), (etw.EVENT_DATA_DESCRIPTOR, 16),
])
def test_struct_sizes_are_the_x64_sizes_in_ne_5_1(cls, size):
    assert ctypes.sizeof(cls) == size


@pytest.mark.parametrize("cls, field, offset", [
    (etw.EVENT_TRACE_PROPERTIES, "LoggerNameOffset", 116), (etw.EVENT_TRACE_PROPERTIES, "RealTimeBuffersLost", 100),
    (etw.EVENT_TRACE_LOGFILEW, "ProcessTraceMode", 28), (etw.EVENT_TRACE_LOGFILEW, "EventRecordCallback", 424),
    (etw.TRACE_LOGFILE_HEADER, "PerfFreq", 256), (etw.EVENT_HEADER, "TimeStamp", 16),
    (etw.EVENT_HEADER, "ProviderId", 24), (etw.EVENT_HEADER, "EventDescriptor", 40),
    (etw.EVENT_RECORD, "UserDataLength", 86), (etw.EVENT_RECORD, "UserData", 96),
])
def test_key_offsets_are_the_ones_in_ne_5_1(cls, field, offset):
    assert getattr(cls, field).offset == offset


def test_the_import_time_layout_check_reports_every_mismatch():
    assert etw.layout_mismatches(etw.LAYOUTS) == []
    bad = etw.layout_mismatches([(etw.EVENT_RECORD, 111, {"UserData": 95, "UserDataLength": 86}),
                                 (etw.GUID, 17, {})])
    assert len(bad) == 3, bad
    assert any("sizeof(EVENT_RECORD) = 112, expected 111" in b for b in bad)
    assert any("EVENT_RECORD.UserData at 96, expected 95" in b for b in bad)
    assert any("sizeof(GUID) = 16, expected 17" in b for b in bad)


def test_the_record_decoder_check_catches_a_wrong_width_and_an_overlap():
    assert etw.record_reads_mismatches(etw._RECORD_READS) == []
    assert len(etw.record_reads_mismatches([("UserDataLength", "I")])) == 1          # a 2-byte field read as 4
    assert len(etw.record_reads_mismatches([("UserData", "Q"), ("UserDataLength", "H")])) == 1  # out of order


# ---------------------------------------------------------------- controller ---

def test_stopping_or_querying_a_session_that_does_not_exist_returns_4201():
    name = _name()
    assert etw.stop_session(name) == etw.ERROR_WMI_INSTANCE_NOT_FOUND == 4201
    assert etw.query_session(name) == 4201


@pytest.mark.parametrize("bad", ["", "x" * 1024, "nul\x00name", None, 7])
def test_bad_session_names_are_refused(bad):
    with pytest.raises(ValueError):
        etw.stop_session(bad)


@pytest.mark.parametrize("providers", [[], [("not-a-guid", 4, 0)], [(KERNEL_NETWORK, 256, 0)],
                                       [(KERNEL_NETWORK, 4, -1)], [(KERNEL_NETWORK, 4, 1 << 64)]])
def test_bad_providers_are_refused(providers):
    with pytest.raises(ValueError):
        etw.RealtimeSession(_name(), providers, lambda e: None)


@pytest.mark.needs("unelevated")
def test_unelevated_real_time_start_is_access_denied_and_leaves_nothing_behind():
    name = _name()
    session = etw.RealtimeSession(name, [(KERNEL_NETWORK, 4, 0x30)], lambda e: None)
    callbacks = atexit._ncallbacks()
    try:
        session.start()
    except OSError as exc:
        assert exc.winerror == etw.ERROR_ACCESS_DENIED, exc        # NE 5.6: unelevated StartTraceW -> 5
        assert isinstance(exc, PermissionError) and "StartTraceW" in str(exc)
        h = session.health()
        assert h["running"] is False and h["pump_alive"] is False and h["lost"] is None
        assert h["orphan_stop_rc"] == 4201                         # it tried the orphan cleanup first
        assert atexit._ncallbacks() == callbacks                   # nothing registered for a session that never ran
    else:                                                          # an elevated run: a real session must work too
        try:
            h = session.health()
            assert h["running"] and h["query_rc"] == 0 and h["pump_alive"]
        finally:
            session.stop()
    finally:
        session.stop()
    assert etw.query_session(name) == 4201
    session.stop()                                                 # idempotent


def test_stop_and_health_are_safe_before_start():
    session = etw.RealtimeSession(_name(), [(KERNEL_NETWORK, 4, 0x30)], lambda e: None)
    h = session.health()
    assert h["events"] == 0 and h["pump_alive"] is False and h["lost"] is None and h["process_trace_rc"] is None
    session.stop()
    session.stop()


# ---------------------------------------------------------------- private round trip ---

def test_private_round_trip_delivers_kernel_network_shaped_events_byte_exact(provider):
    got: list[etw.EtwEvent] = []
    name = _name()
    session = etw.RealtimeSession(name, [(provider.guid, 4, 0x10)], got.append, private=True)
    n = 2000
    try:
        session.start()
        before = clock.qpc_now()
        failed = sum(_write_43(provider, _udp_v4_recv(4242, 1200, "192.168.0.225", "203.0.113.7", 50000, 27015,
                                                      i, 0xC0FFEE)) != 0 for i in range(n))
        after = clock.qpc_now()
        assert failed == 0
    finally:
        session.stop()
    h = session.health()
    assert len(got) == h["events"] == n, h
    assert h["lost"] == 0 and h["buffers_lost"] == 0 and h["callback_errors"] == 0
    assert h["process_trace_rc"] == 0 and h["pump_alive"] is False
    assert h["skipped"] >= 1                                       # the file's own header event is not delivered
    assert all(e.provider == provider.guid for e in got)
    # Raw QPC timestamps: inside the window our own QPC reads bracket, in order, on the same frequency.
    assert h["clock"] == (etw.CLOCK_QPC, clock.qpc_frequency())
    stamps = [e.timestamp for e in got]
    assert before <= stamps[0] and stamps[-1] <= after
    assert stamps == sorted(stamps)
    first = got[0]
    assert (first.id, first.version, first.opcode, first.level, first.task, first.keyword) == (43, 0, 43, 4, 11, 0x10)
    assert first.process_id == os.getpid()
    assert [struct.unpack_from("<I", e.data, 20)[0] for e in got] == list(range(n))   # seqnum, in order
    # Decoded through the flow tracker's NE 4.3 decoder: every field as written.
    pkt = netflows.decode(first.id, first.version, first.data)
    assert pkt == netflows.Packet(43, "udp", "recv", 4242, 1200, "192.168.0.225", "203.0.113.7", 50000, 27015)
    assert len(first.data) == 28


def test_private_session_file_is_deleted_after_stop(provider):
    session = etw.RealtimeSession(_name(), [(provider.guid, 4, 0)], lambda e: None, private=True)
    try:
        session.start()
        path = session._etl_path
        _write_43(provider, b"\x00" * 28)
        assert os.path.exists(path)
    finally:
        session.stop()
    try:
        assert not os.path.exists(path)
        assert session.health()["file"] is None
    finally:
        if os.path.exists(path):                                   # a broken stop must not litter %TEMP%
            os.remove(path)


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetProcessHandleCount.restype = wintypes.BOOL


def _own_handle_count() -> int:
    n = wintypes.DWORD()
    assert _kernel32.GetProcessHandleCount(_kernel32.GetCurrentProcess(), ctypes.byref(n))
    return n.value


def test_stopping_closes_the_consumer_handle(provider):
    # Measured: skipping CloseTrace leaks 2 OS handles per session (20 over 10), and the .etl file is
    # still deletable, so only our own process's handle count shows it.
    def one_session():
        s = etw.RealtimeSession(_name(), [(provider.guid, 4, 0)], lambda e: None, private=True)
        try:
            s.start()
            _write_43(provider, b"\x00" * 28)
        finally:
            s.stop()

    one_session()                                                  # warm-up: DLLs and caches load once
    before = _own_handle_count()
    for _ in range(10):
        one_session()
    assert _own_handle_count() - before < 10


def test_an_exception_in_the_callback_is_counted_and_the_trace_goes_on(provider, monkeypatch):
    escaped = []
    monkeypatch.setattr(sys, "unraisablehook", escaped.append)     # what an escaping ctypes exception hits
    calls = []

    def flaky(ev: etw.EtwEvent) -> None:
        calls.append(ev)
        if len(calls) <= 3:
            raise RuntimeError("decoder bug")

    session = etw.RealtimeSession(_name(), [(provider.guid, 4, 0)], flaky, private=True)
    try:
        session.start()
        for i in range(50):
            _write_43(provider, _udp_v4_recv(1, i, "10.0.0.1", "10.0.0.2", 1, 2, i, 0))
    finally:
        session.stop()
    h = session.health()
    assert len(calls) == 50                                        # events after the failures still arrived
    assert h["callback_errors"] == 3 and h["first_error"] == "RuntimeError: decoder bug"
    assert h["process_trace_rc"] == 0
    assert escaped == []


def test_start_stops_an_orphan_of_the_same_name_before_starting(provider, monkeypatch):
    calls = []
    real = etw.stop_session

    def spy(name):
        calls.append((name, session.health()["running"]))
        return real(name)

    monkeypatch.setattr(etw, "stop_session", spy)
    name = _name()
    session = etw.RealtimeSession(name, [(provider.guid, 4, 0)], lambda e: None, private=True)
    try:
        session.start()
        assert calls == [(name, False)]                            # by name, before StartTraceW
        assert session.health()["orphan_stop_rc"] == 4201
    finally:
        session.stop()


def test_a_name_clash_stops_by_name_and_retries_once_then_reports_183(provider, monkeypatch):
    names = []
    real = etw.stop_session
    monkeypatch.setattr(etw, "stop_session", lambda name: names.append(name) or real(name))
    name = _name()
    first_events: list[etw.EtwEvent] = []
    first = etw.RealtimeSession(name, [(provider.guid, 4, 0)], first_events.append, private=True)
    other = etw.Provider(str(uuid.uuid4()))
    try:
        first.start()
        names.clear()
        second = etw.RealtimeSession(name, [(other.guid, 4, 0)], lambda e: None, private=True)
        with pytest.raises(OSError) as info:
            second.start()
        # A private session cannot be stopped by name, so the retry meets the same clash.
        assert info.value.winerror == etw.ERROR_ALREADY_EXISTS
        assert names == [name, name]                               # orphan cleanup, then once more before the retry
        assert second.health()["running"] is False
        _write_43(provider, b"\x00" * 28)                          # the first session is untouched
    finally:
        first.stop()
        other.close()
    assert len(first_events) == 1


def test_a_stopped_session_is_no_longer_held_by_its_atexit_hook(provider):
    # atexit._ncallbacks() never shrinks (unregister leaves an empty slot), so watch the session's
    # lifetime instead: once stopped and dropped, nothing may still reference it.
    session = etw.RealtimeSession(_name(), [(provider.guid, 4, 0)], lambda e: None, private=True)
    session.start()
    ref = weakref.ref(session)
    pump = session._pump
    del session
    gc.collect()
    running = ref()
    try:
        assert running is not None, "a started session must stay reachable until it is stopped"
    finally:
        if running is not None:
            running.stop()
    del running
    pump.join(5)
    gc.collect()
    assert ref() is None, "the stopped session is still referenced (atexit hook not unregistered?)"
