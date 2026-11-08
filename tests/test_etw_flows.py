"""The net_flows logic on synthetic event streams: decoding (NE 4.3), endpoint classification (NE 4.4),
server flow, tick, gaps and their kinds (NE 4.6), talkers, server_rx, the reorder buffer and the bounds.

Expected values are written here as literals or built by this file's own encoders, never read
back from the code under test; the live local address set is checked against psutil.net_if_addrs().
"""
from __future__ import annotations

import random
import socket
import struct
import threading

import psutil
import pytest

from lagdoctor.capture import netflows as nf
from lagdoctor.capture.netflows import FlowTable, Packet

GAME, OTHER = 4242, 999
LOCAL, SERVER, VOICE = "192.168.0.225", "203.0.113.7", "198.51.100.20"
LOCALS = frozenset({LOCAL, "127.0.0.1", "::1"})


def rx(t, pid=GAME, server=SERVER, port=27015, size=1200, lport=50000):
    return (t, Packet(43, "udp", "recv", pid, size, LOCAL, server, lport, port))


def tx(t, pid=GAME, server=SERVER, port=27015, size=100, lport=50000):
    return (t, Packet(42, "udp", "send", pid, size, server, LOCAL, port, lport))


def times(hz, start, stop, offset=0.0):
    k0 = int(start * hz)
    return [k / hz + offset for k in range(k0, int(stop * hz)) if start <= k / hz < stop]


def stream(*, hz=64, dur=20.0, rx_gap=None, tx_gap=None, burst=(), tx_hz=None, **kw):
    """rx at hz and tx at tx_hz (5 ms later), leaving out rx inside rx_gap and tx inside tx_gap (open
    intervals), plus extra rx packets at the `burst` times."""
    ev = [rx(t, **kw) for t in times(hz, 0, dur) if not (rx_gap and rx_gap[0] < t < rx_gap[1])]
    ev += [tx(t, **kw) for t in times(tx_hz or hz, 0, dur, 0.005) if not (tx_gap and tx_gap[0] < t < tx_gap[1])]
    ev += [rx(t, **kw) for t in burst]
    return sorted(ev, key=lambda e: e[0])


def stretched(hz, dur, at, interval):
    """rx and tx at hz, but the rx packet after `at` (and every rx after it) comes late, so exactly one
    rx inter-arrival is `interval` seconds."""
    shift = interval - 1.0 / hz
    ev = [rx(t if t <= at else t + shift) for t in times(hz, 0, dur)]
    ev += [tx(t) for t in times(hz, 0, dur, 0.005)]
    return sorted(ev, key=lambda e: e[0])


def run(events, game=(GAME,), local=LOCALS, end=None):
    return nf.analyze(events, game, local, end)


def feed(events, lag, end=None):
    """Hand events to a FlowTable the way ETW does: each one `lag(t, pkt)` seconds after its timestamp,
    with the table's clock advanced to the arrival first. Returns the finished table."""
    arrivals = sorted((t + lag(t, pkt), i, t, pkt) for i, (t, pkt) in enumerate(events))
    table = FlowTable({GAME}, LOCALS)
    for arrival, _, t, pkt in arrivals:
        table.advance(arrival)
        table.add(t, pkt)
    table.finish(end)
    return table


def held(lo, hi, late_at=None, late_by=None, rx=True, tx=True):
    """A lag: 0.5 s for every packet, except the chosen directions with lo <= t < hi, which arrive at
    `late_at` (all at once) or `late_by` seconds after their timestamp, as after a consumer stall."""
    def lag(t, pkt):
        if lo <= t < hi and (rx if pkt.kind == "recv" else tx):
            return late_at - t if late_at is not None else late_by
        return 0.5
    return lag


# ---------------------------------------------------------------- decoding (NE 4.3) ---

def _v4(pid, size, daddr, saddr, dport, sport, extra=b"", tail=(0, 0)):
    return (struct.pack("<II", pid, size) + socket.inet_aton(daddr) + socket.inet_aton(saddr)
            + struct.pack(">HH", dport, sport) + extra + struct.pack("<II", *tail))


def _v6(pid, size, daddr, saddr, dport, sport):
    return (struct.pack("<II", pid, size) + socket.inet_pton(socket.AF_INET6, daddr)
            + socket.inet_pton(socket.AF_INET6, saddr) + struct.pack(">HH", dport, sport) + struct.pack("<II", 7, 8))


def test_decode_udp_v4_receive_reads_the_ne_4_3_offsets():
    data = _v4(4242, 1200, "192.168.0.225", "203.0.113.7", 50000, 27015, tail=(5, 0xC0FFEE))
    assert len(data) == 28
    assert nf.decode(43, 0, data) == Packet(43, "udp", "recv", 4242, 1200, "192.168.0.225", "203.0.113.7",
                                            50000, 27015)
    assert nf.decode(42, 0, data).kind == "send"


def test_decode_ports_are_big_endian():
    data = _v4(1, 0, "10.0.0.1", "10.0.0.2", 0x1234, 0xABCD)
    pkt = nf.decode(43, 0, data)
    assert (pkt.dport, pkt.sport) == (0x1234, 0xABCD)


def test_decode_udp_v6_and_an_ipv4_mapped_address():
    data = _v6(77, 64, "2001:db8::1", "::ffff:203.0.113.7", 3074, 27016)
    assert len(data) == 52
    pkt = nf.decode(59, 0, data)
    assert pkt == Packet(59, "udp", "recv", 77, 64, "2001:db8::1", "203.0.113.7", 3074, 27016)


def test_decode_tcp_send_and_connect_have_their_own_lengths():
    send = _v4(5, 300, "1.2.3.4", "10.0.0.2", 443, 61000, extra=struct.pack("<II", 111, 222))      # startime, endtime
    assert len(send) == 36
    assert nf.decode(10, 0, send) == Packet(10, "tcp", "send", 5, 300, "1.2.3.4", "10.0.0.2", 443, 61000)
    connect = _v4(5, 0, "1.2.3.4", "10.0.0.2", 443, 61000, extra=struct.pack("<HHHHIHH", 1460, 1, 0, 1, 65535, 8, 8))
    assert len(connect) == 44
    assert nf.decode(12, 0, connect).kind == "connect"
    assert nf.decode(14, 0, _v4(5, 300, "1.2.3.4", "10.0.0.2", 443, 61000)).kind == "retransmit"


@pytest.mark.parametrize("event_id, version, length", [(43, 1, 28), (43, 0, 27), (43, 0, 29), (59, 0, 28),
                                                       (10, 0, 28), (12, 0, 36)])
def test_decode_refuses_a_version_or_length_it_was_not_proven_for(event_id, version, length):
    with pytest.raises(nf.LayoutError):
        nf.decode(event_id, version, b"\x00" * length)
    assert not nf.layout_ok(event_id, version, length)


@pytest.mark.parametrize("event_id", [13, 16, 17, 18, 49, 1000])
def test_events_not_decoded_here_return_none(event_id):
    assert nf.decode(event_id, 0, b"\x00" * 28) is None


@pytest.mark.parametrize("text, want", [("fe80::1%8", "fe80::1"), ("::ffff:1.2.3.4", "1.2.3.4"),
                                        ("10.0.0.1", "10.0.0.1"), ("2001:DB8::1", "2001:db8::1"), ("junk", None)])
def test_normalize_ip_spells_addresses_the_way_decode_does(text, want):
    assert nf.normalize_ip(text) == want


def test_the_local_address_set_has_loopback_and_this_pcs_addresses():
    local = nf.local_addresses()
    assert {"127.0.0.1", "::1"} <= local
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))             # picks a source address; sends nothing
        source = probe.getsockname()[0]
    except OSError:
        pytest.skip("no IPv4 route on this machine")
    finally:
        probe.close()
    assert source in local


def _set_from_psutil() -> set[str]:
    """The set the tracker built from psutil.net_if_addrs() before lag-doctor read the adapters itself."""
    out = {"127.0.0.1", "::1"}
    for addrs in psutil.net_if_addrs().values():
        out |= {nf.normalize_ip(a.address) for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)}
    return out - {None}


def test_the_local_address_set_is_the_one_psutil_gave():
    before = _set_from_psutil()
    local = nf.local_addresses()
    after = _set_from_psutil()
    assert before & after <= local, sorted((before & after) - local)
    assert local <= before | after, sorted(local - (before | after))     # spelled as decode() spells, no zones


def test_the_local_address_set_spells_each_adapter_address_as_decode_does(monkeypatch):
    adapters = [{"alias": "Wi-Fi", "if_index": 8,
                 "addresses": ["192.168.0.40", "fe80::1C2D:0:0:7%8", "::ffff:192.0.2.9"]},
                {"alias": "Ethernet 2", "if_index": 12, "addresses": []}]
    monkeypatch.setattr(nf.iphlp, "adapter_addresses", lambda: adapters)
    assert nf.local_addresses() == {"127.0.0.1", "::1", "192.168.0.40", "fe80::1c2d:0:0:7", "192.0.2.9"}


# ---------------------------------------------------------------- endpoints (NE 4.4) ---

def test_the_remote_end_is_the_one_that_is_not_ours_in_either_field():
    recv_d_local = Packet(43, "udp", "recv", 1, 0, LOCAL, SERVER, 50000, 27015)
    recv_s_local = Packet(43, "udp", "recv", 1, 0, SERVER, LOCAL, 27015, 50000)
    assert nf.endpoints(recv_d_local, LOCALS) == (LOCAL, 50000, SERVER, 27015)
    assert nf.endpoints(recv_s_local, LOCALS) == (LOCAL, 50000, SERVER, 27015)
    assert nf.endpoints(Packet(43, "udp", "recv", 1, 0, "127.0.0.1", LOCAL, 1, 2), LOCALS) is None   # both ours
    assert nf.endpoints(Packet(43, "udp", "recv", 1, 0, VOICE, SERVER, 1, 2), LOCALS) is None        # neither


@pytest.mark.parametrize("ip, port, ok", [(SERVER, 27015, True), ("192.168.0.40", 7777, True), (SERVER, 53, False),
                                          ("239.255.255.250", 1900, False), ("255.255.255.255", 6112, False),
                                          ("ff02::1", 5353, False), ("0.0.0.0", 1, False), (SERVER, 0, False)])
def test_who_can_be_a_game_server(ip, port, ok):
    assert nf.can_be_server(ip, port) is ok


# ---------------------------------------------------------------- tick and gap rules ---

def test_gap_threshold_takes_the_larger_of_three_ticks_and_tick_plus_50_ms():
    assert nf.gap_threshold(0.015625) == pytest.approx(0.065625)   # 64 Hz: tick + 50 ms wins
    assert nf.gap_threshold(0.050) == pytest.approx(0.150)         # 20 Hz: 3 ticks wins


def test_median_interval_needs_eight_intervals_inside_the_window():
    nine = [i * 0.01 for i in range(9)]                             # 8 intervals of 10 ms
    assert nf.median_interval(nine, until=0.08) == pytest.approx(0.01)
    assert nf.median_interval(nine[:8], until=0.08) is None         # 7 intervals
    old_and_new = [i * 0.5 for i in range(9)] + [20 + i * 0.02 for i in range(9)]
    assert nf.median_interval(old_and_new, until=20.16) == pytest.approx(0.02)   # older than 10 s: ignored


def test_tx_paused_needs_a_running_tx_stream():
    assert nf.tx_paused([], 10.0, 10.3, 0.015625, 11.0) is False                    # nothing sent before
    assert nf.tx_paused([9.99], 10.0, 10.3, None, 11.0) is False                    # no tx tick known
    assert nf.tx_paused([9.99, 10.31], 10.0, 10.3, 0.015625, 11.0) is True          # silent through the gap


# ---------------------------------------------------------------- the three gap kinds ---

def test_a_64hz_server_with_a_300ms_gap_then_a_burst_is_a_delay():
    burst = [10.300 + j * 0.0005 for j in range(19)]
    r = run(stream(rx_gap=(10.0, 10.3), burst=burst))
    assert r["gaps"] == [[10.0, 300.0, "delay"]]
    s = r["server"]
    assert (s["ip"], s["port"], s["proto"], s["pid"]) == (SERVER, 27015, "udp", GAME)
    assert s["tick_ms"] == 15.625
    assert (s["rx_packets"], s["tx_packets"]) == (1280, 1280)
    assert (s["rx_bytes"], s["tx_bytes"]) == (1280 * 1200, 1280 * 100)


def test_the_same_gap_with_no_burst_is_loss():
    r = run(stream(rx_gap=(10.0, 10.3)))
    assert r["gaps"] == [[10.0, 312.5, "loss"]]                     # last packet 10.0, next 10.3125


def test_a_gap_where_our_own_tx_paused_too_is_local():
    r = run(stream(rx_gap=(10.0, 10.3), tx_gap=(10.0, 10.3)))
    assert r["gaps"] == [[10.0, 312.5, "local"]]


def test_local_wins_over_a_burst():
    burst = [10.300 + j * 0.0005 for j in range(19)]
    r = run(stream(rx_gap=(10.0, 10.3), tx_gap=(10.0, 10.3), burst=burst))
    assert [g[2] for g in r["gaps"]] == ["local"]


# ---------------------------------------------------------------- thresholds pinned either side ---

@pytest.mark.parametrize("hz, interval, gaps", [(64, 0.0650, 0), (64, 0.0662, 1),     # tick + 50 ms = 65.625 ms
                                                (20, 0.1490, 0), (20, 0.1510, 1)])    # 3 ticks = 150 ms
def test_a_gap_is_an_interval_past_the_threshold_and_not_before(hz, interval, gaps):
    r = run(stretched(hz, 20.0, at=10.0, interval=interval))
    assert len(r["gaps"]) == gaps, r["gaps"]
    if gaps:
        assert r["gaps"][0][1] == pytest.approx(interval * 1000, abs=0.01)


@pytest.mark.parametrize("burst, kind", [((10.300, 10.301), "loss"),                  # 2 packets: not a burst
                                         ((10.300, 10.301, 10.302), "delay"),         # 3 packets
                                         ((10.300, 10.301, 10.3038), "delay"),        # third inside 0.25 x 15.625 ms
                                         ((10.300, 10.301, 10.3040), "loss")])        # third just outside
def test_a_burst_is_three_packets_within_a_quarter_tick(burst, kind):
    r = run(stream(rx_gap=(10.0, 10.3125), burst=burst))
    assert [g[2] for g in r["gaps"]] == [kind]
    assert r["gaps"][0][1] == pytest.approx(300.0)


# tx packets sit at k/64 + 5 ms: ... 10.03625, [10.051875 ... 10.22375], 10.239375 ... The open intervals below
# drop the bracketed ones, leaving a tx silence from 10.03625 to 10.239375 (203.1 ms, 65 % of the 312.5 ms rx
# gap) or to 10.16125 (125 ms, 40 %). Both silences are tx gaps in their own right (> 65.625 ms).
@pytest.mark.parametrize("tx_gap, kind", [((10.037, 10.239), "local"), ((10.037, 10.161), "loss")])
def test_local_needs_the_tx_silence_to_cover_half_the_gap(tx_gap, kind):
    r = run(stream(rx_gap=(10.0, 10.3), tx_gap=tx_gap))
    assert [g[2] for g in r["gaps"]] == [kind]


# ---------------------------------------------------------------- picking the server ---

def test_the_server_is_the_bidirectional_udp_flow_with_the_most_rx():
    voice = stream(hz=50, server=VOICE, port=3478)
    game = stream(hz=64)
    r = run(sorted(voice + game, key=lambda e: e[0]))
    assert (r["server"]["ip"], r["server"]["port"]) == (SERVER, 27015)


def test_dns_one_way_tcp_multicast_and_other_processes_are_never_the_server():
    events = stream(hz=64, port=53)                                         # DNS, both ways
    events += [rx(t, server="198.51.100.9", port=9000) for t in times(128, 0, 20)]   # one way
    events += [(t, Packet(11, "tcp", "recv", GAME, 1400, LOCAL, SERVER, 50001, 443)) for t in times(64, 0, 20)]
    events += [(t, Packet(10, "tcp", "send", GAME, 100, SERVER, LOCAL, 443, 50001)) for t in times(64, 0, 20)]
    events += stream(hz=64, server="239.255.255.250", port=1900)            # multicast
    events += stream(hz=64, pid=OTHER)                                      # not the game
    r = run(sorted(events, key=lambda e: e[0]))
    assert r["server"] is None and r["server_rx"] == [] and r["gaps"] == []
    assert OTHER in {t["pid"] for t in r["talkers"]}                        # still counted as a talker


@pytest.mark.parametrize("tx_hz, picked", [(10.0, True), (9.5, False)])
def test_the_server_needs_ten_packets_a_second_each_way(tx_hz, picked):
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in stream(hz=64, tx_hz=tx_hz, dur=20.0):
        table.add(t, pkt)
    table.advance(17.0)                                                     # steady state: a full 10 s window
    assert (table.server is not None) is picked


def test_the_server_is_judged_over_the_last_ten_seconds():
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in stream(hz=64, dur=20.0):                                  # the server goes quiet at t = 20
        table.add(t, pkt)
    table.advance(30.0)                                                     # closes second 27: 2 s of data left in its window
    assert table.server is not None
    table.advance(31.0)                                                     # closes second 28: 1 s left, 64 < 100
    assert table.server is None


def test_a_server_flow_stays_the_server_in_the_result_after_it_ends():
    r = run(stream(hz=64, dur=20.0) + [rx(25.0), tx(25.0)])
    assert r["server"]["ip"] == SERVER


def test_only_the_current_server_gets_gaps_and_the_longest_server_is_reported():
    # B (twice A's rate, from 2 s) out-counts A over the 10 s window from second 4 on: A is the server
    # for 4 s, B for 15. A's gap at 15 s is then not the server's gap.
    a = stream(hz=64, dur=20.0, rx_gap=(15.0, 15.3))
    b = [e for e in stream(hz=128, dur=20.0, server=VOICE, port=7777) if e[0] >= 2.0]
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in sorted(a + b, key=lambda e: e[0]):
        table.add(t, pkt)
    table.finish()
    seconds = {f.key[3]: f.server_s for f in table._servers.values()}
    assert seconds == {27015: 4, 7777: 15}
    r = table.result()
    assert (r["server"]["ip"], r["server"]["port"]) == (VOICE, 7777)
    assert r["gaps"] == []


def test_a_process_that_leaves_the_game_stops_being_the_server():
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in stream(hz=64, dur=20.0):
        table.add(t, pkt)
    table.advance(8.0)
    assert table.server is not None
    table.set_game_pids(set())
    table.advance(9.0)
    assert table.server is None


# ---------------------------------------------------------------- server_rx ---

def test_server_rx_is_one_row_per_second_holding_that_seconds_rx_count():
    r = run(stream(rx_gap=(10.0, 10.3)))
    rows = dict((t, n) for t, n in r["server_rx"])
    assert len(r["server_rx"]) == 19                                        # seconds 0-18 closed; 19 is still open
    assert rows[1.0] == 64 and rows[10.0] == 64                             # stamped at the end of each second
    assert rows[11.0] == 64 - 19                                            # second 10 lost 19 packets


def test_server_rx_rows_only_exist_while_there_is_a_server():
    r = run(stream(hz=64, pid=OTHER))
    assert r["server_rx"] == []


# ---------------------------------------------------------------- reorder buffer ---

def test_batches_delivered_late_and_shuffled_are_put_back_in_order():
    events = stream(hz=64, dur=20.0)
    rng = random.Random(7)
    table = FlowTable({GAME}, LOCALS)
    for sec in range(20):                                                   # ETW flushes about once a second
        batch = [e for e in events if sec <= e[0] < sec + 1]
        rng.shuffle(batch)
        for t, pkt in batch:
            table.add(t, pkt)
        table.advance(sec + 1.5)
    table.finish()
    assert table.counts["late"] == 0
    r = table.result()
    assert r["gaps"] == [] and r["server"]["tick_ms"] == 15.625


@pytest.mark.parametrize("delay, late", [(1.9, 0), (2.1, 1)])
def test_the_reorder_buffer_holds_two_seconds(delay, late):
    arrivals = []
    for t, pkt in stream(hz=64, dur=20.0):
        lag = delay if (pkt.kind == "recv" and t == 10.0) else 0.5
        arrivals.append((t + lag, t, pkt))
    table = FlowTable({GAME}, LOCALS)
    for arrival, t, pkt in sorted(arrivals, key=lambda a: a[0]):
        table.advance(arrival)
        table.add(t, pkt)
    table.finish()
    assert table.counts["late"] == late
    assert table.result()["server"]["rx_packets"] == 1280                   # a late packet still counts as traffic


# ---------------------------------------------------------------- packets later than the reorder window ---
# A steady 64 Hz flow with packets 0.5 s late, except a 0.5 s stretch [10.0, 10.5) held back by a consumer
# stall. The rx silence they leave, from 9.984375 to 10.5, never happened: no gap may come of it.

@pytest.mark.parametrize("rx, tx", [(True, True), (True, False)])        # held together ("local"), rx alone ("loss")
def test_a_stall_that_delivers_packets_after_their_gap_was_recorded_leaves_no_gap(rx, tx):
    table = feed(stream(hz=64, dur=20.0), held(10.0, 10.5, late_by=2.6, rx=rx, tx=tx))
    assert table.counts["late"] == 32 * (rx + tx)                            # rx 10.0 ... 10.484375 (+ tx 5 ms later)
    assert table.result()["gaps"] == []
    assert table.counts["gaps_late"] == 1


def test_late_packets_that_land_while_their_gap_is_pending_leave_no_gap():
    # The gap is created when 10.5 is final (arrival 12.5) and settles at 10.5656 (arrival 12.5656): 12.52 is between.
    table = feed(stream(hz=64, dur=20.0), held(10.0, 10.5, late_at=12.52))
    assert table.counts["late"] == 64 and table.result()["gaps"] == [] and table.counts["gaps_late"] == 1


def test_late_packets_that_land_before_their_gap_closes_leave_no_gap():
    # At 12.3 only t <= 10.3 is final: those are late, the rest of the stretch is on time, and the gap they
    # leave (9.984375 to 10.3125) is refused when 10.3125 is finalised, since a late receive lies inside it.
    table = feed(stream(hz=64, dur=20.0), held(10.0, 10.5, late_at=12.3))
    assert table.counts["late"] == 2 * 20 - 1                                # rx 10.0 ... 10.296875, tx up to 10.286
    assert table.result()["gaps"] == [] and table.counts["gaps_late"] == 1


# The rx gap [10.0, 10.3125] is created when 10.3125 is final (arrival 12.3125) and settles at 10.378125 (12.378).
# Sends held back until after it settled, while it was pending, or before it was created (then only those at
# or before 10.2 are late: 13 of the 19).
@pytest.mark.parametrize("held_back, late", [({"late_by": 2.6}, 19), ({"late_at": 12.35}, 19), ({"late_at": 12.2}, 13)])
def test_a_real_gap_whose_missing_sends_were_only_late_is_unknown_not_local(held_back, late):
    # The server really went quiet (no rx in the open interval), but our own sends there came late: without
    # them the gap reads as our tx pausing too ("local"), which is not known.
    on_time = run(stream(rx_gap=(10.0, 10.3), tx_gap=(10.0, 10.3)))
    assert on_time["gaps"] == [[10.0, 312.5, "local"]]                       # the control: sends really missing
    table = feed(stream(rx_gap=(10.0, 10.3)), held(10.0, 10.3, rx=False, **held_back))
    assert table.result()["gaps"] == [[10.0, 312.5, "unknown"]]
    assert table.counts["late"] == late and table.counts["gaps_late"] == 0


def test_a_burst_that_comes_late_still_counts_as_the_burst():
    # Burst packets 2-19 arrive at 12.33, after the gap opened (10.3 final at 12.3) and before it settles (12.3656).
    burst = [10.300 + j * 0.0005 for j in range(19)]
    on_time = feed(stream(rx_gap=(10.0, 10.3), burst=burst), held(0, 0))
    assert on_time.result()["gaps"] == [[10.0, 300.0, "delay"]]
    table = feed(stream(rx_gap=(10.0, 10.3), burst=burst), held(10.3001, 10.31, late_at=12.33, tx=False))
    assert table.counts["late"] == 18
    assert table.result()["gaps"] == [[10.0, 300.0, "delay"]]


# ---------------------------------------------------------------- a server that never comes back ---

def test_a_server_that_goes_quiet_for_good_leaves_an_open_gap_at_the_end():
    # rx stops after 9.984375 while our own sends go on: the server stopped answering (a lost connection).
    r = run(stream(rx_gap=(9.99, 20.5)), end=20.0)
    assert r["gaps"] == [[9.984375, 10015.625, "open"]]
    assert r["server"]["last_rx_t"] == 9.984375 and r["server"]["first_rx_t"] == 0.0
    assert run(stream(rx_gap=(9.99, 20.5)))["gaps"] == []                   # no end given (a replay): nothing closed


def test_no_open_gap_when_the_game_stopped_sending_too():
    # Both directions stop together: the game hung up (left the match), the server did not vanish.
    assert run(stream(rx_gap=(9.99, 20.5), tx_gap=(9.99, 20.5)), end=20.0)["gaps"] == []


# Our last send after the server's last packet (9.984375): 10.895625 (0.911 s) and 11.09875 (1.114 s).
@pytest.mark.parametrize("tx_stop, gaps", [(10.9, 0), (11.1, 1)])
def test_an_open_gap_needs_our_sends_to_go_on_a_second_after_the_last_packet(tx_stop, gaps):
    r = run(stream(rx_gap=(9.99, 20.5), tx_gap=(tx_stop, 20.5)), end=20.0)
    assert len(r["gaps"]) == gaps and all(g[2] == "open" for g in r["gaps"]), r["gaps"]


def test_no_open_gap_for_a_silence_shorter_than_a_gap():
    # The capture ends 15.6 ms after the last packet (our sends go on past it): not a gap by the 65.6 ms rule.
    assert run(stream(rx_gap=(9.99, 20.5)), end=10.0)["gaps"] == []


def test_the_open_gap_starts_at_the_newest_packet_even_a_late_one():
    # The server's last half second came 2.6 s late, then nothing: it went quiet after 10.484375, not 9.984375.
    table = feed(stream(rx_gap=(10.49, 20.5)), held(10.0, 10.5, late_by=2.6, tx=False), end=20.0)
    assert table.result()["gaps"] == [[10.484375, 9515.625, "open"]]


def test_an_open_gap_belongs_to_the_most_recent_server_only():
    # A (port 27015) answers until 14 s, though our sends to it go on; the game moved to B (port 7777), which
    # takes over as the server and runs to the end. A was the server for longer, but B is the one that counts.
    a = stream(hz=64, dur=20.0, rx_gap=(13.99, 20.5))
    b = [e for e in stream(hz=64, dur=20.0, server=VOICE, port=7777) if e[0] >= 12.0]
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in sorted(a + b, key=lambda e: e[0]):
        table.add(t, pkt)
    table.finish(20.0)
    seconds = {f.key[3]: f.server_s for f in table._servers.values()}
    assert seconds[27015] > seconds[7777] > 0 and table._last_server.key[3] == 7777
    assert table.result()["gaps"] == []


# ---------------------------------------------------------------- talkers ---

def test_top_talkers_are_the_eight_busiest_processes_by_bytes_with_names():
    per_pid = {pid: [pid * 10, pid] for pid in range(1, 11)}
    per_pid[50] = [0, 0]
    rows = nf.top_talkers(per_pid, {10: "game.exe", 9: "steam.exe"})
    assert [r["pid"] for r in rows] == [10, 9, 8, 7, 6, 5, 4, 3]
    assert rows[0] == {"pid": 10, "name": "game.exe", "rx_bytes": 100, "tx_bytes": 10}
    assert rows[2]["name"] is None


def test_talkers_count_every_process_both_ways():
    table = FlowTable({GAME}, LOCALS)
    table.add(1.0, Packet(43, "udp", "recv", OTHER, 500, LOCAL, SERVER, 1, 2))
    table.add(1.1, Packet(42, "udp", "send", OTHER, 70, SERVER, LOCAL, 2, 1))
    table.add(1.2, Packet(11, "tcp", "recv", 31337, 1400, LOCAL, SERVER, 3, 443))
    table.add(1.3, Packet(14, "tcp", "retransmit", 31337, 1400, SERVER, LOCAL, 443, 3))   # not new bytes
    table.finish()
    rows = {r["pid"]: r for r in table.result()["talkers"]}
    assert (rows[OTHER]["rx_bytes"], rows[OTHER]["tx_bytes"]) == (500, 70)
    assert (rows[31337]["rx_bytes"], rows[31337]["tx_bytes"]) == (1400, 0)


# ---------------------------------------------------------------- bounds ---

def test_the_flow_table_is_capped_and_never_evicts_the_server(monkeypatch):
    monkeypatch.setattr(nf, "MAX_FLOWS", 4)
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in stream(hz=64, dur=5.0):
        table.add(t, pkt)
    table.advance(4.0)
    assert table.server is not None
    for i in range(10):
        table.add(4.5 + i * 0.01, rx(4.5 + i * 0.01, server=f"198.51.100.{i + 1}", port=1000 + i)[1])
    assert len(table._flows) == 4 and table.counts["flows_evicted"] == 7
    assert table.server.key in table._flows


def test_talkers_are_capped(monkeypatch):
    monkeypatch.setattr(nf, "MAX_TALKERS", 3)
    table = FlowTable({GAME}, LOCALS)
    for pid in range(1, 7):
        table.count(pid, "recv", 10)
    assert len(table._talkers) == 3 and table.counts["talkers_dropped"] == 3


def test_the_reorder_buffer_is_capped(monkeypatch):
    monkeypatch.setattr(nf, "MAX_PENDING", 100)
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in stream(hz=64, dur=10.0):                                  # 1280 packets, never advanced
        table.add(t, pkt)
    assert len(table._heap) <= 101 and table.counts["pending_overflow"] > 0


def test_gaps_are_capped(monkeypatch):
    monkeypatch.setattr(nf, "MAX_GAPS", 1)
    table = FlowTable({GAME}, LOCALS)
    for t, pkt in stream(rx_gap=(5.0, 5.3), dur=20.0):
        if not (12.0 < t < 12.3 and pkt.kind == "recv"):         # a second gap
            table.add(t, pkt)
    table.finish()
    assert len(table.gaps) == 1 and table.counts["gaps_dropped"] == 1


# ---------------------------------------------------------------- self-test (NE 4.4) ---

def _probe(proto, kind, daddr, dport, saddr, sport, pid=100):
    return Packet({"udp": 42, "tcp": 10}[proto], proto, kind, pid, 40, daddr, saddr, dport, sport)


REMOTE = "1.1.1.1"


def _consistent(pid=100):
    return [_probe("udp", "send", REMOTE, 53, LOCAL, 5001, pid), _probe("udp", "recv", LOCAL, 5001, REMOTE, 53, pid),
            _probe("tcp", "send", REMOTE, 53, LOCAL, 6001, pid), _probe("tcp", "recv", REMOTE, 53, LOCAL, 6001, pid)]


def test_selftest_verdict_names_the_remote_field_per_protocol_and_direction():
    v = nf.judge_selftest(_consistent(), pid=100, udp_port=5001, tcp_port=6001, remote_ip=REMOTE)
    assert v["verified"] is True
    assert v["fields"] == {"udp_send": "daddr", "udp_recv": "saddr", "tcp_send": "daddr", "tcp_recv": "daddr"}


def test_selftest_is_unverified_when_an_event_is_missing():
    v = nf.judge_selftest(_consistent()[:3], pid=100, udp_port=5001, tcp_port=6001, remote_ip=REMOTE)
    assert v["verified"] is False and v["fields"]["tcp_recv"] is None and "tcp_recv" in v["reason"]


def test_selftest_is_unverified_when_one_event_points_both_ways():
    packets = _consistent() + [_probe("udp", "recv", REMOTE, 53, LOCAL, 5001)]
    v = nf.judge_selftest(packets, pid=100, udp_port=5001, tcp_port=6001, remote_ip=REMOTE)
    assert v["verified"] is False and v["fields"]["udp_recv"] == "ambiguous"


def test_selftest_ignores_other_processes_and_other_ports():
    noise = [_probe("udp", "recv", REMOTE, 53, LOCAL, 5001, pid=7), _probe("udp", "recv", REMOTE, 53, LOCAL, 5999)]
    v = nf.judge_selftest(_consistent() + noise, pid=100, udp_port=5001, tcp_port=6001, remote_ip=REMOTE)
    assert v["verified"] is True
    assert nf.judge_selftest(noise, pid=100, udp_port=5001, tcp_port=6001, remote_ip=REMOTE)["verified"] is False


class _FakeDns:
    """A loopback DNS answerer on one port number for both UDP and TCP (bad_txid: answer the wrong id)."""

    def __init__(self, bad_txid=False):
        self.bad, self.udp_client, self.tcp_client = bad_txid, None, None
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
        self.threads = [threading.Thread(target=self._serve_udp, daemon=True),
                        threading.Thread(target=self._serve_tcp, daemon=True)]
        for th in self.threads:
            th.start()

    def _answer(self, query):
        txid = struct.unpack(">H", query[:2])[0] ^ (1 if self.bad else 0)
        return struct.pack(">HHHHHH", txid, 0x8180, 1, 0, 0, 0) + query[12:]

    def _serve_udp(self):
        try:
            data, addr = self.udp.recvfrom(512)
            self.udp_client = addr[1]
            self.udp.sendto(self._answer(data), addr)
        except OSError:
            pass

    def _serve_tcp(self):
        try:
            conn, addr = self.tcp.accept()
            with conn:
                self.tcp_client = addr[1]
                n = struct.unpack(">H", conn.recv(2))[0]
                reply = self._answer(conn.recv(n))
                conn.sendall(struct.pack(">H", len(reply)) + reply)
        except OSError:
            pass

    def close(self):
        for th in self.threads:
            th.join(5)
        self.tcp.close()
        self.udp.close()


@pytest.mark.parametrize("bad_txid", [False, True])
def test_selftest_traffic_sends_one_udp_and_one_tcp_query_from_the_ports_it_reports(bad_txid):
    dns = _FakeDns(bad_txid)
    try:
        out = nf.selftest_traffic("127.0.0.1", dns.port, dns.port, timeout=3.0)
    finally:
        dns.close()
    assert out["errors"] == []
    assert (out["udp_port"], out["tcp_port"]) == (dns.udp_client, dns.tcp_client)
    assert out["udp_reply"] is (not bad_txid) and out["tcp_reply"] is (not bad_txid)
