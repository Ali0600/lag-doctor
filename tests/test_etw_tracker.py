"""The net_flows collector (FlowTracker): how it starts, degrades, filters, times and reports.

Most tests drive it through a fake session that hands synthetic Kernel-Network events to the
tracker's own callback; one runs it over a real private ETW session (NE 5.6) end to end, and one
makes the real unelevated attempt that must come back "unavailable".
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time
import uuid

import pytest

from lagdoctor import clock
from lagdoctor.capture import netflows as nf
from lagdoctor.capture.netflows import FlowTracker
from lagdoctor.winapi import etw
from tests.loopback_dns import LoopbackDns

GAME, OTHER = 4242, 999
LOCAL, SERVER = "192.168.0.225", "203.0.113.7"
LOCALS = frozenset({LOCAL, "127.0.0.1", "::1"})
RESULT_KEYS = {"server", "talkers", "talkers_scope", "gaps", "server_rx", "layout", "health"}


def _v4(pid, size, daddr, saddr, dport, sport):
    """Kernel-Network UDPv4 payload (NE 4.3): 28 bytes, addresses in network order, ports big-endian."""
    return (struct.pack("<II", pid, size) + socket.inet_aton(daddr) + socket.inet_aton(saddr)
            + struct.pack(">HH", dport, sport) + struct.pack("<II", 0, 0))


def _ticks(origin, t):
    return round((origin + t) * clock.qpc_frequency())


def _event(origin, t, event_id, payload, header_pid=4, version=0):
    return etw.EtwEvent(_ticks(origin, t), nf.KERNEL_NETWORK, event_id, version, event_id, 4, 11, 0x10,
                        header_pid, 1, payload)


def _game_stream(origin, *, dur=20.0, gap=(10.0, 10.3), burst=19, pid=GAME, header_pid=4):
    """64 Hz rx (43) and tx (42) between the game and SERVER, rx silent inside `gap`, then a burst."""
    out = []
    for k in range(int(dur * 64)):
        t = k / 64
        if not (gap[0] < t < gap[1]):
            out.append(_event(origin, t, 43, _v4(pid, 1200, LOCAL, SERVER, 50000, 27015), header_pid))
        out.append(_event(origin, t + 0.005, 42, _v4(pid, 100, SERVER, LOCAL, 27015, 50000), header_pid))
    out += [_event(origin, gap[1] + j * 0.0005, 43, _v4(pid, 1200, LOCAL, SERVER, 50000, 27015), header_pid)
            for j in range(burst)]
    return sorted(out, key=lambda e: e.timestamp)


class FakeSession:
    """Stands in for etw.RealtimeSession: it hands events to the tracker's own callback.

    `preload` is delivered inside start(), before the tracker's first tick, like a backlog. Events
    handed over later must be fresh: anything older than the tracker's 2 s reorder window is late."""

    def __init__(self, name, providers, on_event, *, start_error=None, health=None, preload=()):
        self.name, self.providers, self.on_event = name, providers, on_event
        self.start_error, self._health, self.preload = start_error, health or {}, list(preload)
        self.started = self.stopped = False

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        self.feed(self.preload)

    def stop(self):
        self.stopped = True

    def health(self):
        return {"events": 0, "lost": 0, "callback_errors": 0, "pump_alive": self.started and not self.stopped,
                "process_trace_rc": None, **self._health}

    def feed(self, events):
        for ev in events:
            self.on_event(ev)


def _factory(made, **kw):
    def make(name, providers, on_event):
        s = FakeSession(name, providers, on_event, **kw)
        made.append(s)
        return s
    return make


def _tracker(made, game=(GAME,), **kw):
    session_kw = {k: kw.pop(k) for k in ("start_error", "health", "preload") if k in kw}
    kw.setdefault("selftest", False)
    kw.setdefault("local_addrs", lambda: LOCALS)
    return FlowTracker(game, session_factory=_factory(made, **session_kw), **kw)


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ---------------------------------------------------------------- starting ---

def test_the_tracker_asks_for_kernel_network_ipv4_and_ipv6_in_its_fixed_session():
    made = []
    tr = _tracker(made)
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
    finally:
        tr.stop()
    s = made[0]
    assert s.name == "lag-doctor-net"
    assert s.providers == [("7dd42a49-5329-4832-8dfd-43d979153a88", 4, 0x30)]   # NE 4.1, DESIGN net_flows
    assert s.stopped


def test_unelevated_the_real_session_is_unavailable_with_the_reason():
    name = f"lagdoctor-test-{uuid.uuid4().hex[:12]}"      # never the real lag-doctor-net, even when elevated
    tr = FlowTracker({GAME}, selftest=False,
                     session_factory=lambda _n, providers, cb: etw.RealtimeSession(name, providers, cb))
    tr.start(clock.now())
    try:
        assert _wait(lambda: tr.status().state != "off")
        st = tr.status()
        if st.state == "ok":                                # elevated: the real path ran; nothing more to judge here
            return
        assert st.state == "unavailable", st
        # A group member gets the group's own reason: the group may not enable Kernel-Network (measured, E1).
        from lagdoctor.winapi import elevation
        assert st.detail == (nf.GROUP_REFUSED if elevation.in_performance_log_users() else nf.NEEDS_RIGHTS)
        assert "--elevate" in st.detail and st.tool == "ETW Microsoft-Windows-Kernel-Network"
    finally:
        started = time.monotonic()
        tr.stop()
        assert time.monotonic() - started < 5.0
    r = tr.result()
    assert set(r) == RESULT_KEYS
    # Nothing was listened to, so the lists are not measured (None), never "measured, none seen" ([]).
    assert r["server"] is None and r["talkers"] is None and r["gaps"] is None and r["server_rx"] is None
    assert r["layout"] == "unverified"
    assert r["health"]["pump_alive"] is False and r["health"]["lost"] is None   # not measured, never 0
    assert etw.query_session(name) == 4201


def test_any_other_start_error_is_failed_not_unavailable():
    made = []
    tr = _tracker(made, start_error=OSError(None, "StartTraceW: no system resources", None, 1450))
    tr.start(clock.now())
    try:
        assert _wait(lambda: tr.status().state != "off")
    finally:
        tr.stop()
    st = tr.status()
    assert st.state == "failed" and "1450" in st.detail, st


def test_result_and_stop_are_safe_before_start():
    tr = _tracker([])
    tr.stop()
    r = tr.result()
    assert set(r) == RESULT_KEYS and r["server"] is None and r["layout"] == "unverified"
    assert r["talkers"] is None and r["gaps"] is None and r["server_rx"] is None


def _not_member():
    raise PermissionError(5, "Access is denied")


@pytest.mark.parametrize("member,expect", [(lambda: True, nf.GROUP_REFUSED), (lambda: False, nf.NEEDS_RIGHTS),
                                           (_not_member, nf.NEEDS_RIGHTS)])
def test_a_refused_start_says_what_would_help(monkeypatch, member, expect):
    # A member of Performance Log Users may start the session but not enable Kernel-Network (E1): the reason must
    # not send them back to join the group they are in.
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "in_performance_log_users", member)
    tr = _tracker([], start_error=OSError(None, "EnableTraceEx2: Access is denied.", None, 5))
    tr.start(clock.now())
    try:
        assert _wait(lambda: tr.status().state != "off")
    finally:
        tr.stop()
    st = tr.status()
    assert st.state == "unavailable" and st.detail == expect
    assert "--elevate" in st.detail


def test_a_session_that_could_not_start_reports_its_lists_as_not_measured():
    made = []
    tr = _tracker(made, start_error=OSError(None, "StartTraceW: access denied", None, 5))
    tr.start(clock.now())
    try:
        assert _wait(lambda: tr.status().state != "off")
    finally:
        tr.stop()
    assert tr.status().state == "unavailable"
    r = tr.result()
    assert r["talkers"] is None and r["gaps"] is None and r["server_rx"] is None and r["server"] is None
    good = _tracker([])                                      # the control: a session that ran and saw nothing
    good.start(clock.now())
    try:
        assert _wait(lambda: good.status().state == "ok")
    finally:
        good.stop()
    r = good.result()
    assert r["talkers"] == [] and r["gaps"] == [] and r["server_rx"] == []


# ---------------------------------------------------------------- the pipeline ---

def test_synthetic_kernel_network_events_give_the_server_its_gap_and_the_talkers():
    made = []
    origin = clock.now() - 21.0                              # the 20 s stream is a backlog at start
    backlog = _game_stream(origin) + [_event(origin, 5.0 + i * 0.1, 43,
                                             _v4(OTHER, 64000, LOCAL, "198.51.100.5", 61000, 443)) for i in range(10)]
    tr = _tracker(made, names=lambda pids: {GAME: "game.exe", OTHER: "steam.exe"}, preload=backlog)
    tr.start(origin)
    try:
        assert _wait(lambda: tr.live().get("server_ip") == SERVER), tr.live()
        assert tr.live()["server"] == f"{SERVER}:27015" and tr.live()["server_pps"] == 64
    finally:
        tr.stop()
    r = tr.result()
    assert set(r) == RESULT_KEYS
    assert r["gaps"] == [[pytest.approx(10.0, abs=1e-6), pytest.approx(300.0, abs=1e-3), "delay"]]
    s = r["server"]
    assert (s["ip"], s["port"], s["proto"], s["pid"]) == (SERVER, 27015, "udp", GAME)
    assert s["rx_packets"] == 1280 and s["tx_packets"] == 1280
    assert s["tick_ms"] == pytest.approx(15.625, abs=0.001)
    assert [t["pid"] for t in r["talkers"]] == [GAME, OTHER]          # 1.66 MB before 640 kB
    assert r["talkers"][1] == {"pid": OTHER, "name": "steam.exe", "rx_bytes": 640_000, "tx_bytes": 0}
    assert r["talkers"][0]["name"] == "game.exe"
    assert r["talkers_scope"] == "all"                # its own session sees every program (no relay)
    assert r["server_rx"][0] == [1.0, 64]
    assert r["layout"] == "unverified"
    assert tr.status().state == "ok" and "unverified" in tr.status().detail
    assert r["health"]["layout_mismatch"] == {} and r["health"]["bad_time"] == 0


@pytest.mark.parametrize("payload_pid, header_pid, found", [(GAME, 4, True), (OTHER, GAME, False)])
def test_the_payload_pid_decides_which_process_a_packet_belongs_to(payload_pid, header_pid, found):
    made = []
    origin = clock.now() - 21.0
    tr = _tracker(made, preload=_game_stream(origin, pid=payload_pid, header_pid=header_pid))
    tr.start(origin)
    try:
        assert _wait(lambda: made and made[0].started)
    finally:
        tr.stop()
    r = tr.result()
    assert (r["server"] is not None) is found
    assert {t["pid"] for t in r["talkers"]} == {payload_pid}


def test_an_event_with_an_unexpected_layout_is_counted_and_never_decoded():
    made = []
    origin = clock.now() - 21.0
    odd = [_event(origin, 6.0, 43, _v4(GAME, 1, LOCAL, SERVER, 1, 2), version=1),
           _event(origin, 6.1, 43, _v4(GAME, 1, LOCAL, SERVER, 1, 2) + b"\x00"),
           _event(origin, 6.2, 13, b"\x00" * 28)]                    # TCP close: not decoded here, not an error
    tr = _tracker(made, preload=_game_stream(origin, dur=5.0) + odd)
    tr.start(origin)
    try:
        assert _wait(lambda: made and made[0].started)
    finally:
        tr.stop()
    r = tr.result()
    assert r["health"]["layout_mismatch"] == {"43": 2}
    assert r["health"]["ignored"] == 1
    assert r["layout"] == "unverified"
    st = tr.status()
    assert st.state == "partial" and "unknown layout" in st.detail and "43" in st.detail


def test_timestamps_off_the_qpc_clock_are_refused():
    made = []
    tr = _tracker(made)
    origin = clock.now() - 21.0
    tr.start(origin)
    try:
        assert _wait(lambda: made and made[0].started)
        payload = _v4(GAME, 1200, LOCAL, SERVER, 50000, 27015)
        filetime = etw.EtwEvent(134_000_000_000_000_000, nf.KERNEL_NETWORK, 43, 0, 43, 4, 11, 0x10, 4, 1, payload)
        zero = filetime._replace(timestamp=0)
        made[0].feed([filetime, zero])
    finally:
        tr.stop()
    r = tr.result()
    assert r["health"]["bad_time"] == 2 and r["health"]["game_events"] == 0
    assert tr.status().state == "partial" and "QPC" in tr.status().detail


def _feed_live(session, tr, seconds):
    """Hand over 64 Hz rx/tx events stamped with the capture clock as it runs (like ETW, minus the
    batching). Returns how many rx events were handed over."""
    k = 0
    start = tr.now()
    sent = 0
    while k / 64 < seconds:
        t = start + k / 64
        if t > tr.now() + 0.1:
            time.sleep(0.02)
            continue
        session.feed([_event(tr.origin, t, 43, _v4(GAME, 1200, LOCAL, SERVER, 50000, 27015)),
                      _event(tr.origin, t + 0.005, 42, _v4(GAME, 100, SERVER, LOCAL, 27015, 50000))])
        sent += 1
        k += 1
    return sent


def test_game_pids_can_be_a_callable_polled_while_running():
    made = []
    pids: set[int] = set()
    tr = _tracker(made, game=lambda: set(pids))
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        _feed_live(made[0], tr, 1.5)                                  # the game's PID is not known yet
        pids.add(GAME)
        assert _wait(lambda: tr._table.is_game(GAME), timeout=3.0)
        after = _feed_live(made[0], tr, 2.5)
    finally:
        tr.stop()
    r = tr.result()
    assert r["server"] is not None
    assert r["server"]["rx_packets"] == after                        # only what came after the PID joined
    assert r["talkers"][0]["rx_bytes"] > after * 1200                # the early packets still count as traffic


def test_game_pids_are_validated():
    tr = FlowTracker({0, -5, True, GAME, 2**33, "7", 0xFFFFFFFF}, selftest=False)
    assert tr._pid_source == frozenset({GAME, 0xFFFFFFFF})


def test_a_stale_local_address_set_is_reread_early():
    # The PC gets a new address mid-capture (DHCP, VPN): events from it have neither end "local"
    # until the set is re-read, which must happen within about a second, not at the 10 s refresh.
    made = []
    current = {"127.0.0.1"}
    tr = _tracker(made, local_addrs=lambda: set(current))
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        current.add(LOCAL)
        _feed_live(made[0], tr, 3.0)
    finally:
        tr.stop()
    r = tr.result()
    assert r["health"]["unknown_endpoints"] > 0
    assert r["server"] is not None and r["server"]["rx_packets"] > 64


def test_a_consumer_that_dies_or_drops_events_is_reported(monkeypatch):
    monkeypatch.setattr(nf, "HEALTH_EVERY_S", 0.1)
    made = []
    tr = _tracker(made, health={"pump_alive": False, "process_trace_rc": 4201, "lost": 5})
    tr.start(clock.now())
    try:
        assert _wait(lambda: tr.status().state == "partial", timeout=3.0)
        detail = tr.status().detail
        assert "consumer stopped" in detail and "4201" in detail and "dropped 5 events" in detail
    finally:
        tr.stop()
    assert "dropped 5 events" in tr.status().detail                  # still said after stop


class DyingSession(FakeSession):
    """A consumer whose ProcessTrace returns by itself `after` seconds in (its session stopped from outside)."""

    after = 0.8

    def start(self):
        super().start()
        self.started_at = time.monotonic()

    def health(self):
        h = super().health()
        if h["pump_alive"] and time.monotonic() - self.started_at >= self.after:
            h.update(pump_alive=False, process_trace_rc=0)
        return h


def test_a_consumer_that_stops_by_itself_keeps_the_flows_partial_after_stop_and_says_when():
    made = []
    tr = FlowTracker((GAME,), selftest=False, local_addrs=lambda: LOCALS,
                     session_factory=lambda n, p, cb: made.append(DyingSession(n, p, cb)) or made[-1])
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        time.sleep(2.5)
    finally:
        tr.stop()
    st = tr.status()
    assert st.state == "partial" and "ETW consumer stopped by itself" in st.detail, st
    assert "ProcessTrace returned 0" in st.detail
    died = tr.result()["health"]["consumer_stopped_t"]
    assert died is not None and 0.8 <= died <= 2.3, died               # found within a check (1 s) of dying


def test_a_consumer_found_stopped_at_the_end_had_stopped_by_itself(monkeypatch):
    # It dies after the last periodic check: the look taken just before our own stop is what sees it.
    monkeypatch.setattr(nf, "PUMP_EVERY_S", 5.0)
    monkeypatch.setattr(DyingSession, "after", 0.3)
    made = []
    tr = FlowTracker((GAME,), selftest=False, local_addrs=lambda: LOCALS,
                     session_factory=lambda n, p, cb: made.append(DyingSession(n, p, cb)) or made[-1])
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        time.sleep(0.8)
    finally:
        tr.stop()
    st = tr.status()
    assert st.state == "partial" and "ETW consumer stopped by itself" in st.detail, st


class StoppedByName(FakeSession):
    """A consumer that ends when `end()` is called: lag-doctor stopping lag-doctor-net by name (cli, on close)."""

    def end(self):
        self.ended = True

    def health(self):
        h = super().health()
        if getattr(self, "ended", False):
            h.update(pump_alive=False, process_trace_rc=0)
        return h


def _run_until_ended(say_first, monkeypatch):
    """Start a tracker, end its consumer from outside (saying first, from another thread, that the stop is
    ours, when say_first), let consumer checks run, stop it, and return it."""
    monkeypatch.setattr(nf, "PUMP_EVERY_S", 0.1)
    made = []
    tr = FlowTracker((GAME,), selftest=False, local_addrs=lambda: LOCALS,
                     session_factory=lambda n, p, cb: made.append(StoppedByName(n, p, cb)) or made[-1])
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        time.sleep(0.3)
        if say_first:
            t = threading.Thread(target=nf.expect_external_stop)      # the console handler's own thread
            t.start()
            t.join()
        made[0].end()
        time.sleep(0.6)                                                # several consumer checks see it ended
    finally:
        tr.stop()
    return tr


def test_a_consumer_ended_by_lag_doctors_own_stop_by_name_is_a_stop_it_asked_for(monkeypatch):
    tr = _run_until_ended(True, monkeypatch)
    st = tr.status()
    assert st.state == "ok" and "stopped by itself" not in st.detail, st
    assert tr.result()["health"]["consumer_stopped_t"] is None
    control = _run_until_ended(False, monkeypatch)                     # the same end, not announced
    assert "ETW consumer stopped by itself" in control.status().detail
    assert control.result()["health"]["consumer_stopped_t"] is not None


def test_a_stop_by_name_announced_before_the_session_started_does_not_excuse_its_consumer(monkeypatch):
    nf.expect_external_stop()                                          # an earlier capture in this process
    time.sleep(0.05)
    monkeypatch.setattr(nf, "PUMP_EVERY_S", 0.1)
    monkeypatch.setattr(DyingSession, "after", 0.3)
    made = []
    tr = FlowTracker((GAME,), selftest=False, local_addrs=lambda: LOCALS,
                     session_factory=lambda n, p, cb: made.append(DyingSession(n, p, cb)) or made[-1])
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        time.sleep(0.8)
    finally:
        tr.stop()
    assert "ETW consumer stopped by itself" in tr.status().detail


def test_after_the_consumer_died_an_open_gap_ends_where_it_was_last_seen_running():
    # The server went quiet at 10 s while the game kept sending; then the consumer itself stopped. What came
    # after that was never seen, so the server's silence is only known up to the consumer's last sign of life.
    made = []
    origin = clock.now() - 21.0
    stream = [e for e in _game_stream(origin, burst=0) if not (e.id == 43 and e.timestamp >= _ticks(origin, 9.99))]
    tr = FlowTracker((GAME,), selftest=False, local_addrs=lambda: LOCALS,
                     session_factory=lambda n, p, cb: made.append(DyingSession(n, p, cb, preload=stream)) or made[-1])
    tr.start(origin)
    try:
        assert _wait(lambda: made and made[0].started)
        time.sleep(2.5)
    finally:
        tr.stop()
    r = tr.result()
    died = r["health"]["consumer_stopped_t"]
    assert died is not None, r["health"]
    (start, ms, kind), = r["gaps"]
    assert kind == "open" and start == pytest.approx(9.984375, abs=1e-6)
    assert start + ms / 1000.0 < died - 1.0                         # last seen running, less the reorder window


def test_game_packets_that_arrive_after_the_reorder_window_make_the_flows_partial():
    made = []
    tr = _tracker(made)
    tr.start(clock.now())
    try:
        assert _wait(lambda: made and made[0].started)
        _feed_live(made[0], tr, 3.0)
        old = tr.now() - 3.0                                         # a stalled buffer: 3 s old, past the 2 s window
        made[0].feed([_event(tr.origin, old + k / 64, 43, _v4(GAME, 1200, LOCAL, SERVER, 50000, 27015))
                      for k in range(10)])
        time.sleep(0.4)
    finally:
        tr.stop()
    r = tr.result()
    assert r["health"]["late"] == 10
    assert r["health"]["delivery_lag_max_s"] >= 3.0
    st = tr.status()
    assert st.state == "partial" and "10 game packets arrived more than 2 s late" in st.detail, st


def test_a_server_that_goes_quiet_for_good_ends_in_an_open_gap():
    # The server's packets stop at 10 s and never come back, while the game keeps sending to it.
    made = []
    origin = clock.now() - 21.0
    stream = [e for e in _game_stream(origin, burst=0) if not (e.id == 43 and e.timestamp >= _ticks(origin, 9.99))]
    tr = _tracker(made, preload=stream)
    tr.start(origin)
    try:
        assert _wait(lambda: "gaps" in tr.live()), tr.live()        # the first tick took in the backlog
    finally:
        tr.stop()
    assert tr.result()["server"]["ip"] == SERVER                    # it was the server, until it went quiet
    gaps = tr.result()["gaps"]
    assert len(gaps) == 1, gaps
    assert gaps[0][0] == pytest.approx(9.984375, abs=1e-6) and gaps[0][2] == "open", gaps
    assert gaps[0][1] >= 11_000.0                                   # to when the capture stopped, after t = 21 s


# ---------------------------------------------------------------- the self-test glue ---

@pytest.mark.parametrize("mismatch", [False, True])
def test_the_selftest_marks_the_layout_verified_only_with_every_event_and_no_mismatch(mismatch):
    dns = LoopbackDns()
    made = []
    me = os.getpid()
    tr = _tracker(made, selftest=True, selftest_server="127.0.0.1", selftest_port=dns.port)
    tr.start(clock.now())
    try:
        assert _wait(lambda: tr.selftest_traffic is not None), "the self-test sent nothing"
        traffic = tr.selftest_traffic
        assert traffic["udp_reply"] and traffic["tcp_reply"], traffic
        u, c, d = traffic["udp_port"], traffic["tcp_port"], dns.port
        now = tr.now()
        lo = "127.0.0.1"
        tcp = lambda pid, size, da, sa, dp, sp: struct.pack("<II", pid, size) + socket.inet_aton(da) + \
            socket.inet_aton(sa) + struct.pack(">HH", dp, sp) + struct.pack("<IIII", 0, 0, 0, 0)  # 36 bytes
        events = [_event(tr.origin, now, 42, _v4(me, 29, lo, lo, d, u)),          # UDP send: remote in daddr
                  _event(tr.origin, now, 43, _v4(me, 45, lo, lo, u, d)),          # UDP recv: remote in saddr
                  _event(tr.origin, now, 10, tcp(me, 31, lo, lo, d, c)),          # TCP send
                  _event(tr.origin, now, 11, _v4(me, 47, lo, lo, d, c))]          # TCP recv
        if mismatch:
            events.append(_event(tr.origin, now, 59, b"\x00" * 51))
        made[0].feed(events)
        assert _wait(lambda: tr.health()["selftest"].get("verified")), tr.health()["selftest"]
    finally:
        tr.stop()
        dns.close()
    r = tr.result()
    assert r["health"]["selftest"]["fields"] == {"udp_send": "daddr", "udp_recv": "saddr",
                                                 "tcp_send": "daddr", "tcp_recv": "daddr"}
    if mismatch:
        assert r["layout"] == "unverified" and tr.status().state == "partial"
    else:
        assert r["layout"] == "verified"
        assert tr.status().state == "ok" and "verified by the self-test" in tr.status().detail


# ---------------------------------------------------------------- a real ETW consumer ---

def test_the_tracker_over_a_real_private_etw_session_finds_the_server_and_a_delay_gap():
    """Kernel-Network-shaped events written in real time by a provider of our own, through a private
    in-process session (the only kind an unelevated run may start, NE 5.6), into the tracker."""
    provider = etw.Provider(str(uuid.uuid4()))
    name = f"lagdoctor-test-{uuid.uuid4().hex[:12]}"
    sessions = []

    def factory(_name, _providers, on_event):
        sessions.append(etw.RealtimeSession(name, [(provider.guid, 4, 0x30)], on_event, private=True))
        return sessions[0]

    tr = FlowTracker({GAME}, selftest=False, session_factory=factory, names=lambda pids: {GAME: "game.exe"})
    # A private session cannot be real time: it hands every event over inside stop(), up to the whole run after it
    # was written, while the live loop finalises the timeline up to the reorder window before now. With the 2 s
    # window the first second of the stream came late and stayed out of the timeline, and whether the server was
    # known before the gap then hung on where whole seconds fell (a writer starting 0.07-0.17 s past one missed
    # the gap every time). A window longer than the run takes the stream in whole, as it was written.
    tr._table.reorder_s = 60.0
    recv = _v4(GAME, 1200, "127.0.0.1", SERVER, 50000, 27015)
    send = _v4(GAME, 100, SERVER, "127.0.0.1", 27015, 50000)
    try:
        tr.start(clock.now())
        # ok once start() has returned, the provider enabled: an event written before that is never logged
        assert _wait(lambda: tr.status().state != "off"), tr.status()
        assert tr.status().state == "ok", tr.status()
        start = time.perf_counter()
        for k in range(int(3.0 * 64)):
            target = start + k / 64
            while time.perf_counter() < target:
                time.sleep(0.0005)
            if not 1.5 <= k / 64 < 1.8:                     # the server goes quiet for 300 ms ...
                provider.write(43, recv, opcode=43, task=11, keyword=0x10)
            if k / 64 == 1.796875:                         # ... then the held packets arrive at once
                for _ in range(15):
                    provider.write(43, recv, opcode=43, task=11, keyword=0x10)
            provider.write(42, send, opcode=42, task=11, keyword=0x10)
    finally:
        tr.stop()
        provider.close()
    h = sessions[0].health()
    assert h["events"] >= 2 * 192 - 20 + 15 and h["lost"] == 0 and h["callback_errors"] == 0, h
    r = tr.result()
    # No packet came after the window, so every verdict below is on the whole stream, and nothing made it partial.
    assert r["health"]["late"] == 0 and tr.status().state == "ok", (r["health"]["late"], tr.status())
    assert r["server"] is not None, r
    assert (r["server"]["ip"], r["server"]["port"]) == (SERVER, 27015)
    assert 14.0 < r["server"]["tick_ms"] < 17.5
    # If this writer itself stalls, both streams pause together: that shows up as a "local" gap,
    # which is the right verdict for it, so only the planted one must be a delay.
    delays = [g for g in r["gaps"] if g[2] == "delay"]
    assert len(delays) == 1 and all(g[2] == "local" for g in r["gaps"] if g not in delays), r["gaps"]
    assert 280.0 < delays[0][1] < 340.0
    assert r["talkers"][0]["pid"] == GAME and r["talkers"][0]["name"] == "game.exe"
