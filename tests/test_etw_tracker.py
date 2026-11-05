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

GAME, OTHER = 4242, 999
LOCAL, SERVER = "192.168.0.225", "203.0.113.7"
LOCALS = frozenset({LOCAL, "127.0.0.1", "::1"})
RESULT_KEYS = {"server", "talkers", "gaps", "server_rx", "layout", "health"}


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
        assert "needs admin rights or Performance Log Users" in st.detail
        assert st.tool == "ETW Microsoft-Windows-Kernel-Network"
    finally:
        started = time.monotonic()
        tr.stop()
        assert time.monotonic() - started < 5.0
    r = tr.result()
    assert set(r) == RESULT_KEYS
    assert r["server"] is None and r["talkers"] == [] and r["gaps"] == [] and r["server_rx"] == []
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


# ---------------------------------------------------------------- the self-test glue ---

class _LoopbackDns:
    """Answers one DNS query over UDP and one over TCP on the same loopback port number."""

    def __init__(self):
        for _ in range(10):
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.bind(("127.0.0.1", 0))
            self.port = self.tcp.getsockname()[1]
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.udp.bind(("127.0.0.1", self.port))
                break
            except OSError:
                self.tcp.close()
                self.udp.close()
        else:
            raise RuntimeError("no free port for both UDP and TCP")
        self.tcp.listen(1)
        self.tcp.settimeout(5)
        self.udp.settimeout(5)
        self.threads = [threading.Thread(target=self._udp, daemon=True), threading.Thread(target=self._tcp, daemon=True)]
        for th in self.threads:
            th.start()

    @staticmethod
    def _answer(q):
        return q[:2] + struct.pack(">HHHHH", 0x8180, 1, 0, 0, 0) + q[12:]

    def _udp(self):
        try:
            q, addr = self.udp.recvfrom(512)
            self.udp.sendto(self._answer(q), addr)
        except OSError:
            pass

    def _tcp(self):
        try:
            conn, _ = self.tcp.accept()
            with conn:
                n = struct.unpack(">H", conn.recv(2))[0]
                a = self._answer(conn.recv(n))
                conn.sendall(struct.pack(">H", len(a)) + a)
        except OSError:
            pass

    def close(self):
        for th in self.threads:
            th.join(5)
        self.tcp.close()
        self.udp.close()


@pytest.mark.parametrize("mismatch", [False, True])
def test_the_selftest_marks_the_layout_verified_only_with_every_event_and_no_mismatch(mismatch):
    dns = _LoopbackDns()
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
    recv = _v4(GAME, 1200, "127.0.0.1", SERVER, 50000, 27015)
    send = _v4(GAME, 100, SERVER, "127.0.0.1", 27015, 50000)
    try:
        tr.start(clock.now())
        assert _wait(lambda: sessions and sessions[0].health()["running"]), tr.status()
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
    assert r["server"] is not None, r
    assert (r["server"]["ip"], r["server"]["port"]) == (SERVER, 27015)
    assert 14.0 < r["server"]["tick_ms"] < 17.5
    # If this writer itself stalls, both streams pause together: that shows up as a "local" gap,
    # which is the right verdict for it, so only the planted one must be a delay.
    delays = [g for g in r["gaps"] if g[2] == "delay"]
    assert len(delays) == 1 and all(g[2] == "local" for g in r["gaps"] if g not in delays), r["gaps"]
    assert 280.0 < delays[0][1] < 340.0
    assert r["talkers"][0]["pid"] == GAME and r["talkers"][0]["name"] == "game.exe"
