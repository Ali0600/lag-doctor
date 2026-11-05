"""The game-server flow tracker ("net_flows"): Microsoft-Windows-Kernel-Network over real-time ETW.

Sources: docs/research/network-and-etw.md (NE) section 4 for the provider, events and payload
layouts, NE 4.6 for how the server flow and its gaps are found, NE 5 for the session.

- Events are filtered by the PID in their payload: a PID scope filter does not reach a
  kernel-registered provider (NE 4.5).
- Which payload address is the remote one is unverified for Kernel-Network (NE 4.4), so each
  event is classified by the local address set instead: the end that is not one of this PC's
  addresses is the remote one. The elevated self-test (one UDP and one TCP DNS query) settles
  the field semantics; until it passes, `layout` is "unverified".
- Times come from EventHeader.TimeStamp (raw QPC ticks, see winapi/etw.py), never from when an
  event arrived: ETW delivers in batches when buffers flush (NE 5.4). A 2 s reorder buffer puts
  them in order before any inter-arrival is computed (NE 4.6.8). A packet that comes later than
  that never enters the timeline, so a gap whose span holds one is not a measured silence: it is
  dropped (a late receive) or its kind becomes "unknown" (a late send), and the status says so.
- A server that goes quiet for good leaves no packet to close its last gap, so the end of the
  capture closes it: an "open" gap, when our own packets to it went on for at least a second
  after its last one (the server stopped answering, not the game hanging up).

The flow logic is the `FlowTable` class, fed `(t, Packet)` tuples, so it is testable without ETW.
"""
from __future__ import annotations

import bisect
import heapq
import ipaddress
import math
import os
import secrets
import socket
import statistics
import struct
import threading
from collections import OrderedDict, deque
from typing import Any, Callable, Iterable, NamedTuple

import psutil

from lagdoctor import clock
from lagdoctor.capture.base import Collector
from lagdoctor.winapi import etw

# NE 4.1: provider identity on this machine.
KERNEL_NETWORK = "7dd42a49-5329-4832-8dfd-43d979153a88"
KW_IPV4, KW_IPV6 = 0x10, 0x20
LEVEL_INFORMATIONAL = 4
SESSION_NAME = "lag-doctor-net"                 # DESIGN.md net_flows; CLAUDE.md
TOOL = "ETW Microsoft-Windows-Kernel-Network"
NEEDS_RIGHTS = "needs admin rights or Performance Log Users"

# NE 4.2: the events decoded here -> (protocol, kind, address family). All are version 0.
EVENTS: dict[int, tuple[str, str, int]] = {
    10: ("tcp", "send", 4), 26: ("tcp", "send", 6),
    11: ("tcp", "recv", 4), 27: ("tcp", "recv", 6),
    12: ("tcp", "connect", 4), 28: ("tcp", "connect", 6),
    14: ("tcp", "retransmit", 4), 30: ("tcp", "retransmit", 6),
    15: ("tcp", "accept", 4), 31: ("tcp", "accept", 6),
    42: ("udp", "send", 4), 58: ("udp", "send", 6),
    43: ("udp", "recv", 4), 59: ("udp", "recv", 6),
}
EXPECTED_VERSION = 0
# NE 4.3 layout table: UserDataLength per event id.
EXPECTED_LENGTH: dict[int, int] = {11: 28, 14: 28, 42: 28, 43: 28, 10: 36, 12: 44, 15: 44,
                                   27: 52, 30: 52, 58: 52, 59: 52, 26: 60, 28: 68, 31: 68}
# NE 4.3: PID@0 and size@4 (little-endian); daddr@8 then saddr (4 or 16 bytes, network order);
# dport then sport, big-endian (win:Port), at 16 for IPv4 and 40 for IPv6.
_PID_SIZE = struct.Struct("<II")
_PORTS = struct.Struct(">HH")
_V4MAPPED = b"\x00" * 10 + b"\xff\xff"

# NE 4.6, all [inferred] starting points there: tune against recordings, never silently.
REORDER_S = 2.0                 # 4.6.8: reorder buffer before inter-arrival is computed
WINDOW_S = 10                   # 4.6.3-4: server pick and nominal tick over a sliding 10 s
MIN_PPS = 10.0                  # 4.6.3: the server flow is bidirectional, rx and tx >= 10 packets/s
HISTORY = 512                   # 4.6.2: last 512 timestamps per flow
GAP_FACTOR, GAP_FLOOR_S = 3.0, 0.050   # 4.6.5: gap = inter-arrival > max(3 x nominal, nominal + 50 ms)
BURST_PACKETS, BURST_FRACTION = 3, 0.25  # 4.6.6: >= 3 packets within 0.25 x nominal = delayed, then flushed
LOCAL_OVERLAP = 0.5             # a tx silence must cover half the rx gap to call the gap local
MIN_INTERVALS = 8               # fewer inter-arrivals than this: no nominal tick yet
SKIP_PORTS = frozenset({53})    # 4.6.3: DNS is never the game server
# An open gap at the end needs our own packets to the server to go on this long after its last one;
# a game that hangs up stops sending with it [inferred, like the rest of 4.6].
OPEN_TX_S = 1.0

# Bounds: nothing grows per event without a cap.
MAX_FLOWS = 256
MAX_TALKERS = 2048
MAX_PENDING = 50_000
MAX_GAPS = 5_000
MAX_ROWS = 86_400               # server_rx rows: a day at 1 Hz
MAX_SERVERS = 64
MAX_PROBE = 64
TOP_TALKERS = 8

LAG_BUCKET_S, LAG_BUCKETS = 0.1, 300    # delivery-lag histogram: 100 ms steps up to 30 s, then one overflow

TICK_S = 0.25
PIDS_EVERY_S, NAMES_EVERY_S, LOCAL_EVERY_S, HEALTH_EVERY_S = 1.0, 5.0, 10.0, 10.0
PUMP_EVERY_S = 1.0              # how often the consumer thread is checked, so its death is timed
SELFTEST_WAIT_S = 4.0           # flush timer 1 s (NE 5.3) plus slack
SELFTEST_SERVER = "1.1.1.1"     # the default internet anchor (DESIGN network); answers DNS on UDP and TCP


# ---------------------------------------------------------------- decoding ---

class Packet(NamedTuple):
    event_id: int
    proto: str          # "udp" | "tcp"
    kind: str           # "send" | "recv" | "retransmit" | "connect" | "accept"
    pid: int            # the payload PID: the process that owns the socket
    size: int
    daddr: str
    saddr: str
    dport: int
    sport: int


class LayoutError(ValueError):
    """An event whose version or length differs from NE 4.3: its fields are not guessed."""

    def __init__(self, event_id: int, version: int, length: int) -> None:
        super().__init__(f"Kernel-Network event {event_id} v{version} with {length} bytes "
                         f"(expected v{EXPECTED_VERSION}, {EXPECTED_LENGTH.get(event_id)} bytes)")
        self.event_id, self.version, self.length = event_id, version, length


def layout_ok(event_id: int, version: int, length: int) -> bool:
    return version == EXPECTED_VERSION and EXPECTED_LENGTH.get(event_id) == length


def _v6(raw: bytes) -> str:
    # An IPv4-mapped IPv6 address is the same host as the IPv4 one: one flow, one spelling.
    if raw[:12] == _V4MAPPED:
        return socket.inet_ntop(socket.AF_INET, raw[12:])
    return socket.inet_ntop(socket.AF_INET6, raw)


def normalize_ip(text: str) -> str | None:
    """The spelling decode() produces for an address (inet_pton -> inet_ntop), or None if it is not one."""
    ip = text.split("%", 1)[0]
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            raw = socket.inet_pton(family, ip)
        except OSError:
            continue
        return _v6(raw) if family == socket.AF_INET6 else socket.inet_ntop(family, raw)
    return None


def decode(event_id: int, version: int, data: bytes) -> Packet | None:
    """Decode one Kernel-Network payload per NE 4.3. None for events not decoded here; raises
    LayoutError when the version or length is not the one the layout table was proven for."""
    spec = EVENTS.get(event_id)
    if spec is None:
        return None
    if not layout_ok(event_id, version, len(data)):
        raise LayoutError(event_id, version, len(data))
    proto, kind, family = spec
    pid, size = _PID_SIZE.unpack_from(data, 0)
    if family == 4:
        daddr = socket.inet_ntop(socket.AF_INET, data[8:12])
        saddr = socket.inet_ntop(socket.AF_INET, data[12:16])
        dport, sport = _PORTS.unpack_from(data, 16)
    else:
        daddr, saddr = _v6(data[8:24]), _v6(data[24:40])
        dport, sport = _PORTS.unpack_from(data, 40)
    return Packet(event_id, proto, kind, pid, size, daddr, saddr, dport, sport)


def local_addresses() -> frozenset[str]:
    """This PC's interface addresses (psutil.net_if_addrs: no process handles) plus loopback."""
    out = {"127.0.0.1", "::1"}
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family in (socket.AF_INET, socket.AF_INET6):
                ip = normalize_ip(a.address)
                if ip:
                    out.add(ip)
    return frozenset(out)


def endpoints(pkt: Packet, local: frozenset[str]) -> tuple[str, int, str, int] | None:
    """(local_ip, local_port, remote_ip, remote_port): the remote end is the one that is not ours
    (NE 4.4). None when both ends are local (loopback) or neither is (a stale address set)."""
    d_local, s_local = pkt.daddr in local, pkt.saddr in local
    if d_local and not s_local:
        return pkt.daddr, pkt.dport, pkt.saddr, pkt.sport
    if s_local and not d_local:
        return pkt.saddr, pkt.sport, pkt.daddr, pkt.dport
    return None


def can_be_server(ip: str, port: int) -> bool:
    """Multicast, broadcast, unspecified addresses and DNS are never the game server."""
    if port in SKIP_PORTS or port == 0:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_multicast or addr.is_unspecified or str(addr) == "255.255.255.255")


# ---------------------------------------------------------------- flow logic ---

def gap_threshold(nominal_s: float) -> float:
    """NE 4.6.5: an rx inter-arrival longer than this is a gap."""
    return max(GAP_FACTOR * nominal_s, nominal_s + GAP_FLOOR_S)


def median_interval(times: Iterable[float], until: float, window_s: float = WINDOW_S) -> float | None:
    """Median inter-arrival of the (sorted) timestamps in (until - window_s, until]: the nominal tick
    (NE 4.6.4). None with fewer than MIN_INTERVALS intervals or a zero median."""
    recent = [t for t in times if until - window_s < t <= until]
    gaps = [b - a for a, b in zip(recent, recent[1:])]
    if len(gaps) < MIN_INTERVALS:
        return None
    m = statistics.median(gaps)
    return m if m > 0 else None


def tx_paused(tx_times: Iterable[float], start: float, end: float, tx_tick: float | None, now: float) -> bool:
    """Did our own tx stream to the server stop during the rx gap [start, end]? (NE 4.6.6 "local")

    True when a silence between consecutive tx packets is itself a gap by the tx stream's own rule
    and covers at least LOCAL_OVERLAP of the rx gap. A tx stream with no known tick never counts.
    `now` stands in for the next tx packet when none has arrived yet."""
    if not tx_tick or end <= start:
        return False
    limit = gap_threshold(tx_tick)
    times = sorted(tx_times)
    before = [t for t in times if t <= start]
    if not before:
        return False                       # no tx before the gap: the stream was not running
    points = [before[-1]] + [t for t in times if start < t < end]
    after = [t for t in times if t >= end]
    points.append(after[0] if after else max(now, end))
    for a, b in zip(points, points[1:]):
        overlap = min(b, end) - max(a, start)
        if b - a > limit and overlap >= LOCAL_OVERLAP * (end - start):
            return True
    return False


def gap_kind(burst: int, local: bool) -> str:
    """NE 4.6.6. A local stall wins: when the PC itself stalls, queued packets also arrive in a burst."""
    if local:
        return "local"
    return "delay" if burst >= BURST_PACKETS else "loss"


def top_talkers(per_pid: dict[int, list[int]], names: dict[int, str | None], n: int = TOP_TALKERS) -> list[dict]:
    """[{pid, name, rx_bytes, tx_bytes}] for the n processes that moved the most bytes."""
    ranked = sorted(per_pid.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
    return [{"pid": pid, "name": names.get(pid), "rx_bytes": row[0], "tx_bytes": row[1]}
            for pid, row in ranked[:n] if row[0] + row[1] > 0]


class _Flow:
    __slots__ = ("key", "local", "serverable", "rx_packets", "tx_packets", "rx_bytes", "tx_bytes",
                 "rx_times", "tx_times", "bin_sec", "bin_rx", "bin_tx", "bin_rx_bytes", "first_sec",
                 "first_rx", "last_rx", "last_tx", "rx_tick", "tx_tick", "server_s", "late_rx", "late_tx",
                 "gap_starts", "gap_log")

    def __init__(self, key: tuple, local: tuple[str, int], first_sec: int) -> None:
        self.key, self.local = key, local
        self.serverable = can_be_server(key[2], key[3])
        self.rx_packets = self.tx_packets = self.rx_bytes = self.tx_bytes = 0
        self.rx_times: deque[float] = deque(maxlen=HISTORY)
        self.tx_times: deque[float] = deque(maxlen=HISTORY)
        self.bin_sec: list[int | None] = [None] * WINDOW_S     # ring of per-second counts
        self.bin_rx, self.bin_tx, self.bin_rx_bytes = [0] * WINDOW_S, [0] * WINDOW_S, [0] * WINDOW_S
        self.first_sec = first_sec
        self.first_rx: float | None = None
        self.last_rx: float | None = None
        self.last_tx: float | None = None
        self.rx_tick: float | None = None
        self.tx_tick: float | None = None
        self.server_s = 0
        # Newest packet that came too late for the timeline, per direction (-inf: none).
        self.late_rx = self.late_tx = -math.inf
        # Recorded gaps of this flow in time order (they never overlap): start times, and [start, end, record].
        self.gap_starts: list[float] = []
        self.gap_log: list[list] = []

    def _slot(self, sec: int) -> int:
        i = sec % WINDOW_S
        if self.bin_sec[i] != sec:
            self.bin_sec[i] = sec
            self.bin_rx[i] = self.bin_tx[i] = self.bin_rx_bytes[i] = 0
        return i

    def window(self, sec: int) -> tuple[int, int, int]:
        """(rx packets, tx packets, rx bytes) in the WINDOW_S seconds ending with second `sec`."""
        rx = tx = rxb = 0
        for i, s in enumerate(self.bin_sec):
            if s is not None and sec - WINDOW_S < s <= sec:
                rx, tx, rxb = rx + self.bin_rx[i], tx + self.bin_tx[i], rxb + self.bin_rx_bytes[i]
        return rx, tx, rxb

    def rx_in(self, sec: int) -> int:
        i = sec % WINDOW_S
        return self.bin_rx[i] if self.bin_sec[i] == sec else 0


class _Gap:
    __slots__ = ("flow", "start", "end", "burst_until", "deadline", "burst", "phantom", "unknown")

    def __init__(self, flow: _Flow, start: float, end: float) -> None:
        self.flow, self.start, self.end = flow, start, end
        self.burst_until = end + BURST_FRACTION * flow.rx_tick
        tx_wait = gap_threshold(flow.tx_tick) if flow.tx_tick else 0.0
        self.deadline = end + max(BURST_FRACTION * flow.rx_tick, tx_wait)
        self.burst = 1                       # the packet that ended the gap
        self.phantom = False                 # a late receive fell inside: the server did not go quiet
        self.unknown = False                 # a late send fell inside: "local" cannot be told


class FlowTable:
    """Per-process byte counts and per-flow UDP timing for the game, fed `(t, Packet)` in arrival order.

    `add()` buffers the game's UDP packets; `advance(now)` finalises everything older than
    now - REORDER_S in timestamp order; `finish(end)` finalises the rest and, given the capture's
    end, closes a silence still open then. Each completed second re-picks the server flow,
    re-estimates its tick and appends one server_rx row.

    counts["late"] counts packets that came after their time was finalised; counts["gaps_late"]
    the gaps left out because a late receive fell inside them.
    """

    def __init__(self, game_pids: Iterable[int] = (), local: Iterable[str] = (), *,
                 reorder_s: float = REORDER_S) -> None:
        self.reorder_s = reorder_s
        self._game = frozenset(game_pids)
        self._local = frozenset(local)
        self._flows: OrderedDict[tuple, _Flow] = OrderedDict()
        self._heap: list[tuple[float, int, _Flow, bool, int]] = []
        self._seq = 0
        self._final_t = -math.inf          # everything at or before this time is finalised
        self._next_sec: int | None = None  # next whole-second boundary to close
        self._last_t = -math.inf           # newest finalised packet
        self._server: _Flow | None = None
        self._last_server: _Flow | None = None   # the most recent server, kept after it stops being one
        self._servers: dict[tuple, _Flow] = {}
        self._pending: list[_Gap] = []
        self._talkers: dict[int, list[int]] = {}
        self.gaps: list[list] = []
        self.server_rx: list[list] = []
        self.counts = {"game_events": 0, "local_endpoints": 0, "unknown_endpoints": 0, "late": 0,
                       "flows_evicted": 0, "pending_overflow": 0, "gaps_dropped": 0, "gaps_late": 0,
                       "talkers_dropped": 0, "server_changes": 0, "retransmits": 0}

    # ---- inputs ---------------------------------------------------------
    def set_game_pids(self, pids: Iterable[int]) -> None:
        self._game = frozenset(pids)

    def set_local(self, local: Iterable[str]) -> None:
        self._local = frozenset(local)

    def is_game(self, pid: int) -> bool:
        return pid in self._game

    def count(self, pid: int, kind: str, size: int) -> None:
        """Bytes per process, for the top talkers (every process, not just the game)."""
        if kind == "retransmit":
            if pid in self._game:
                self.counts["retransmits"] += 1
            return
        if kind not in ("send", "recv"):
            return
        row = self._talkers.get(pid)
        if row is None:
            if len(self._talkers) >= MAX_TALKERS:
                self.counts["talkers_dropped"] += 1
                return
            row = self._talkers[pid] = [0, 0]
        row[0 if kind == "recv" else 1] += size

    def add(self, t: float, pkt: Packet) -> None:
        self.count(pkt.pid, pkt.kind, pkt.size)
        if pkt.proto != "udp" or pkt.kind not in ("send", "recv") or pkt.pid not in self._game:
            return
        self.counts["game_events"] += 1
        ends = endpoints(pkt, self._local)
        if ends is None:
            both = pkt.daddr in self._local and pkt.saddr in self._local
            self.counts["local_endpoints" if both else "unknown_endpoints"] += 1
            return
        local_ip, local_port, remote_ip, remote_port = ends
        flow = self._flow((pkt.pid, "udp", remote_ip, remote_port), (local_ip, local_port), t)
        rx = pkt.kind == "recv"
        if t <= self._final_t:               # older than what is already finalised
            self.counts["late"] += 1
            self._totals(flow, rx, pkt.size)
            self._late(flow, rx, t)
            return
        heapq.heappush(self._heap, (t, self._seq, flow, rx, pkt.size))
        self._seq += 1
        if len(self._heap) > MAX_PENDING:
            self.counts["pending_overflow"] += 1
            self._finalize_until(self._heap[0][0])

    def advance(self, now: float) -> None:
        self._finalize_until(now - self.reorder_s)

    def finish(self, end: float | None = None) -> None:
        """Finalise every buffered packet and settle every pending gap (end of capture). `end` is
        when the capture stopped listening (capture clock): a server silent since before then by
        more than a gap gets an "open" gap. None (a replay) closes nothing."""
        if self._heap:
            self._finalize_until(max(t for t, *_ in self._heap))
        self._settle(math.inf)
        if end is not None:
            self._open_gap(end)

    # ---- outputs --------------------------------------------------------
    @property
    def server(self) -> _Flow | None:
        return self._server

    def server_flow(self) -> _Flow | None:
        """The flow that was the server for the most seconds (ties: more rx packets)."""
        if not self._servers:
            return None
        return max(self._servers.values(), key=lambda f: (f.server_s, f.rx_packets))

    def result(self, names: dict[int, str | None] | None = None) -> dict[str, Any]:
        f = self.server_flow()
        server = None
        if f is not None:
            server = {"ip": f.key[2], "port": f.key[3], "proto": f.key[1], "pid": f.key[0],
                      "rx_packets": f.rx_packets, "tx_packets": f.tx_packets,
                      "rx_bytes": f.rx_bytes, "tx_bytes": f.tx_bytes,
                      "tick_ms": round(f.rx_tick * 1000.0, 3) if f.rx_tick else None,
                      # when its packets came, so a reader can tell how much of the capture they cover
                      "first_rx_t": None if f.first_rx is None else round(f.first_rx, 6),
                      "last_rx_t": None if f.last_rx is None else round(f.last_rx, 6)}
        return {"server": server, "talkers": top_talkers(self._talkers, names or {}),
                "gaps": [list(g) for g in self.gaps], "server_rx": [list(r) for r in self.server_rx]}

    def talker_pids(self, n: int = TOP_TALKERS) -> list[int]:
        return [row["pid"] for row in top_talkers(self._talkers, {}, n)]

    # ---- internals ------------------------------------------------------
    def _flow(self, key: tuple, local: tuple[str, int], t: float) -> _Flow:
        flow = self._flows.get(key)
        if flow is None:
            flow = self._servers.get(key) or _Flow(key, local, math.floor(t))
            self._flows[key] = flow
            while len(self._flows) > MAX_FLOWS:
                for k, old in self._flows.items():       # least recently active first
                    if old is not self._server and old is not flow:
                        del self._flows[k]
                        self.counts["flows_evicted"] += 1
                        break
                else:
                    break
        self._flows.move_to_end(key)
        return flow

    @staticmethod
    def _totals(flow: _Flow, rx: bool, size: int) -> None:
        if rx:
            flow.rx_packets += 1
            flow.rx_bytes += size
        else:
            flow.tx_packets += 1
            flow.tx_bytes += size

    def _finalize_until(self, limit: float) -> None:
        heap = self._heap
        while heap and heap[0][0] <= limit:
            t, _, flow, rx, size = heapq.heappop(heap)
            if self._next_sec is None:
                self._next_sec = math.floor(t) + 1     # the first packet opens its second
            else:
                self._roll_to(t)
            self._apply(t, flow, rx, size)
        if self._next_sec is not None and limit != math.inf:
            self._roll_to(limit)
        if limit > self._final_t:
            self._final_t = limit
        self._settle(limit)

    def _roll_to(self, t: float) -> None:
        """Close every whole second up to t. With no server and no packet for a whole window there
        is nothing to pick or report, so an idle stretch is skipped instead of ticked through."""
        while self._next_sec <= t:
            if self._server is None and self._next_sec - self._last_t > WINDOW_S + 1:
                self._next_sec = math.floor(t) + 1
                return
            self._tick(self._next_sec)
            self._next_sec += 1

    def _apply(self, t: float, flow: _Flow, rx: bool, size: int) -> None:
        self._totals(flow, rx, size)
        sec = math.floor(t)
        i = flow._slot(sec)
        if rx:
            flow.bin_rx[i] += 1
            flow.bin_rx_bytes[i] += size
            prev, flow.last_rx = flow.last_rx, t
            if flow.first_rx is None:
                flow.first_rx = t
            flow.rx_times.append(t)
            for g in self._pending:
                if g.flow is flow and g.end <= t <= g.burst_until:
                    g.burst += 1
            if flow is self._server and prev is not None and flow.rx_tick and t - prev > gap_threshold(flow.rx_tick):
                # A late packet newer than `prev` lies inside this span (it arrived after prev was final,
                # so before t could be): the server did not go quiet, the delivery did.
                if flow.late_rx > prev:
                    self.counts["gaps_late"] += 1
                else:
                    g = _Gap(flow, prev, t)
                    g.unknown = flow.late_tx > prev
                    self._pending.append(g)
        else:
            flow.bin_tx[i] += 1
            flow.tx_times.append(t)
            flow.last_tx = t
        self._last_t = t
        if self._pending:
            self._settle(t)

    def _settle(self, now: float) -> None:
        keep = []
        for g in self._pending:
            if now < g.deadline:
                keep.append(g)
                continue
            if g.phantom:
                self.counts["gaps_late"] += 1
                continue
            local = tx_paused(g.flow.tx_times, g.start, g.end, g.flow.tx_tick, min(now, g.deadline))
            kind = gap_kind(g.burst, local)
            if g.unknown and kind == "local":
                kind = "unknown"            # the missing sends may only have been late
            self._record(g.flow, g.start, g.end, kind)
        self._pending = keep

    def _record(self, flow: _Flow, start: float, end: float, kind: str) -> None:
        if len(self.gaps) >= MAX_GAPS:
            self.counts["gaps_dropped"] += 1
            return
        rec = [round(start, 6), round((end - start) * 1000.0, 3), kind]
        self.gaps.append(rec)
        flow.gap_starts.append(start)
        flow.gap_log.append([start, end, rec, True])

    def _late(self, flow: _Flow, rx: bool, t: float) -> None:
        """A packet that came after its time was finalised (NE 4.6.8's window was too short for it).
        It is not in the timeline, so a gap whose span holds it was not a measured silence."""
        if rx:
            flow.late_rx = max(flow.late_rx, t)
        else:
            flow.late_tx = max(flow.late_tx, t)
        for g in self._pending:
            if g.flow is not flow:
                continue
            if g.start < t < g.end:
                if rx:
                    g.phantom = True
                else:
                    g.unknown = True
            elif rx and g.end <= t <= g.burst_until:
                g.burst += 1                # part of the burst after the gap, as it would have been on time
        # A recorded gap of this flow around t: the flow's gaps never overlap, so it is the last one starting before t.
        i = bisect.bisect_left(flow.gap_starts, t) - 1
        if i < 0:
            return
        entry = flow.gap_log[i]
        start, end, rec, kept = entry
        if not kept or not start < t < end:
            return
        if rx:
            entry[3] = False
            self.counts["gaps_late"] += 1
            for k in range(len(self.gaps) - 1, -1, -1):
                if self.gaps[k] is rec:
                    del self.gaps[k]
                    break
        elif rec[2] == "local":
            rec[2] = "unknown"

    def _open_gap(self, end: float) -> None:
        """The most recent server went quiet for good: no packet closed its last silence, so the end does
        (CC-1). Only when our own packets to it went on after its last one; when they stopped too, the
        game itself hung up (left the match), which is no lost connection."""
        flow = self._last_server
        if flow is None or flow.last_rx is None or not flow.rx_tick:
            return
        # Its newest packet, late or not: the stop delivers whatever was still buffered, so none came after.
        last = max(flow.last_rx, flow.late_rx)
        if end - last <= gap_threshold(flow.rx_tick):
            return
        sent = max(flow.last_tx if flow.last_tx is not None else -math.inf, flow.late_tx)
        if sent - last < OPEN_TX_S:
            return
        self._record(flow, last, end, "open")

    def _tick(self, boundary: int) -> None:
        """Close the second [boundary - 1, boundary): pick the server, re-estimate its tick, add a row."""
        sec = boundary - 1
        best, best_key = None, None
        for flow in self._flows.values():
            if not flow.serverable or flow.key[0] not in self._game:
                continue
            span = min(WINDOW_S, sec - flow.first_sec + 1)
            if span <= 0:
                continue
            rx_n, tx_n, rx_b = flow.window(sec)
            if rx_n < MIN_PPS * span or tx_n < MIN_PPS * span:
                continue
            key = (rx_n, rx_b, flow.key)
            if best_key is None or key > best_key:
                best, best_key = flow, key
        if best is not self._server:
            if best is not None and self._server is not None:
                self.counts["server_changes"] += 1
            self._server = best
        if best is None:
            return
        self._last_server = best
        best.rx_tick = median_interval(best.rx_times, boundary)
        best.tx_tick = median_interval(best.tx_times, boundary)
        best.server_s += 1
        if best.key not in self._servers and len(self._servers) < MAX_SERVERS:
            self._servers[best.key] = best
        if len(self.server_rx) < MAX_ROWS:
            self.server_rx.append([float(boundary), best.rx_in(sec)])


def analyze(events: Iterable[tuple[float, Packet]], game_pids: Iterable[int], local: Iterable[str],
            end: float | None = None) -> dict:
    """Run decoded `(t, Packet)` tuples through a FlowTable and return its result (tests, replays).
    `end`: when the recording stopped, to close a silence still open then (None: close nothing)."""
    table = FlowTable(game_pids, local)
    for t, pkt in events:
        table.add(t, pkt)
    table.finish(end)
    return table.result()


# ---------------------------------------------------------------- self-test (NE 4.4) ---

def _dns_query(txid: int, name: str = "example.com") -> bytes:
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)        # recursion desired, one question
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)                  # type A, class IN


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


def selftest_traffic(server_ip: str, udp_port: int = 53, tcp_port: int = 53, timeout: float = 2.0) -> dict:
    """Send one DNS query over UDP and one over TCP from known local ports (NE 4.4)."""
    family = socket.AF_INET6 if ":" in server_ip else socket.AF_INET
    out: dict[str, Any] = {"server": server_ip, "udp_port": None, "tcp_port": None,
                           "udp_reply": False, "tcp_reply": False, "errors": []}
    query = _dns_query(secrets.randbits(16))
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as u:
            u.settimeout(timeout)
            u.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", 0))
            out["udp_port"] = u.getsockname()[1]
            u.sendto(query, (server_ip, udp_port))
            data, _ = u.recvfrom(4096)
            out["udp_reply"] = data[:2] == query[:2]
    except OSError as exc:
        out["errors"].append(f"udp: {exc}")
    try:
        with socket.create_connection((server_ip, tcp_port), timeout=timeout) as c:
            out["tcp_port"] = c.getsockname()[1]
            c.sendall(struct.pack(">H", len(query)) + query)
            head = _recv_exact(c, 4)                                    # length prefix, then the txid
            out["tcp_reply"] = len(head) == 4 and head[2:4] == query[:2]
    except OSError as exc:
        out["errors"].append(f"tcp: {exc}")
    return out


SELFTEST_EVENTS = (("udp", "send"), ("udp", "recv"), ("tcp", "send"), ("tcp", "recv"))  # 42, 43, 10, 11


def judge_selftest(packets: Iterable[Packet], *, pid: int, udp_port: int | None, tcp_port: int | None,
                   remote_ip: str, remote_port: int = 53) -> dict:
    """Which payload field holds the remote end in UDP/TCP send and receive events (NE 4.4)?

    Verified only when every one of the four was seen and always pointed the same way."""
    remote = (normalize_ip(remote_ip) or remote_ip, remote_port)
    seen: dict[tuple[str, str], set[str]] = {k: set() for k in SELFTEST_EVENTS}
    for p in packets:
        key = (p.proto, p.kind)
        local_port = udp_port if p.proto == "udp" else tcp_port
        if p.pid != pid or key not in seen or local_port is None:
            continue
        if (p.daddr, p.dport) == remote and p.sport == local_port:
            seen[key].add("daddr")
        elif (p.saddr, p.sport) == remote and p.dport == local_port:
            seen[key].add("saddr")
    fields = {f"{proto}_{kind}": (next(iter(s)) if len(s) == 1 else ("ambiguous" if s else None))
              for (proto, kind), s in seen.items()}
    missing = [k for k, v in fields.items() if v is None]
    mixed = [k for k, v in fields.items() if v == "ambiguous"]
    verified = not missing and not mixed
    reason = ("every event pointed one way" if verified else
              "; ".join(filter(None, [f"not seen: {', '.join(missing)}" if missing else "",
                                      f"both fields matched: {', '.join(mixed)}" if mixed else ""])))
    return {"verified": verified, "fields": fields, "reason": reason}


# ---------------------------------------------------------------- the collector ---

def _clean_pids(values: Iterable[Any]) -> frozenset[int]:
    return frozenset(p for p in values if isinstance(p, int) and not isinstance(p, bool) and 0 < p <= 0xFFFFFFFF)


class FlowTracker(Collector):
    """Collector "net_flows". `game_pids`: the game's PIDs, or a callable returning the current set
    (polled once a second, e.g. as children appear). Process names for the top talkers come from
    `names(pids) -> {pid: name}` (default: winapi.ntquery, which opens no process handles)."""

    name = "net_flows"

    def __init__(self, game_pids: Iterable[int] | Callable[[], Iterable[int]], *,
                 names: Callable[[Iterable[int]], dict[int, str]] | None = None,
                 selftest: bool = True, selftest_server: str = SELFTEST_SERVER, selftest_port: int = 53,
                 session_factory: Callable[..., Any] = etw.RealtimeSession,
                 local_addrs: Callable[[], Iterable[str]] = local_addresses) -> None:
        super().__init__()
        self._pid_source = game_pids if callable(game_pids) else _clean_pids(game_pids)
        self._names_fn = names
        self._names: dict[int, str | None] = {}
        self._names_note: str | None = None
        self._snapshotter = None
        self._selftest_on = selftest
        self._selftest_server = normalize_ip(selftest_server) or selftest_server
        self._selftest_port = selftest_port
        self.selftest_traffic: dict | None = None    # what the self-test sent, once it has
        self._session_factory = session_factory
        self._local_fn = local_addrs
        self._lock = threading.Lock()
        self._table = FlowTable()
        self._session: Any = None
        self._session_health: dict = {}
        self._own_pid = os.getpid()
        self._probe: deque[Packet] | None = None
        self._selftest: dict = {"verified": False, "reason": "not run"}
        self._ignored = 0
        self._layout_bad: dict[str, int] = {}
        self._bad_time = 0
        self._pid_errors = 0
        self._unknown_seen = 0           # unknown_endpoints count at the last address refresh
        self._started = False            # the ETW session started: only then does an empty list mean "none seen"
        self._pump_alive_t = 0.0         # last check that found the consumer thread running (capture clock)
        self._pump_dead: tuple[float, Any] | None = None   # (t, ProcessTrace rc) when it was found stopped by itself
        self._lag_max: float | None = None                   # delivery lag of the game's packets (now - timestamp)
        self._lag_hist = [0] * (LAG_BUCKETS + 1)

    # ---- collector contract --------------------------------------------
    def run(self) -> None:
        self._refresh_inputs(local=True)
        session = self._session_factory(SESSION_NAME, [(KERNEL_NETWORK, LEVEL_INFORMATIONAL, KW_IPV4 | KW_IPV6)],
                                        self._on_event)
        try:
            session.start()
        except OSError as exc:
            if getattr(exc, "winerror", None) == etw.ERROR_ACCESS_DENIED:
                self.set_status("unavailable", f"{NEEDS_RIGHTS} (StartTraceW: access denied)", TOOL)
            else:
                self.set_status("failed", f"could not start the ETW session {SESSION_NAME}: {exc}", TOOL)
            self._session_health = _safe_health(session)
            return
        self._session = session
        self._started = True
        self._pump_alive_t = self.now()
        self.set_status("ok", "Kernel-Network flows; address layout unverified (self-test pending)", TOOL)
        if self._selftest_on:
            threading.Thread(target=self._run_selftest, name="lagdoctor-net-selftest", daemon=True).start()
        else:
            self._selftest = {"verified": False, "reason": "self-test turned off"}
        due = {"pids": 0.0, "names": 0.0, "local": LOCAL_EVERY_S, "health": HEALTH_EVERY_S, "pump": 0.0}
        last_local = 0.0
        try:
            for t in self.ticks(TICK_S):
                with self._lock:
                    self._table.advance(t)
                    unknown = self._table.counts["unknown_endpoints"]
                if t >= due["pump"]:
                    due["pump"] = t + PUMP_EVERY_S
                    self._check_pump(session, t)
                if t >= due["pids"]:
                    due["pids"] = t + PIDS_EVERY_S
                    self._refresh_inputs(local=False)
                # An event with neither end local means the address set went stale (new DHCP lease,
                # VPN up): re-read it early, but at most once a second.
                stale = unknown > self._unknown_seen and t - last_local >= PIDS_EVERY_S
                if t >= due["local"] or stale:
                    due["local"], last_local = t + LOCAL_EVERY_S, t
                    self._unknown_seen = unknown
                    self._refresh_inputs(local=True)
                if t >= due["names"]:
                    due["names"] = t + NAMES_EVERY_S
                    self._resolve_names()
                if t >= due["health"]:
                    due["health"] = t + HEALTH_EVERY_S
                    self._session_health = _safe_health(session)
                    self._judge_health(self._session_health)
                self._publish()
        finally:
            end = self.now()
            self._check_pump(session, end)            # before our own stop: a stopped consumer stopped by itself
            try:
                session.stop()
            except Exception as exc:  # noqa: BLE001 - still settle what was captured
                self.set_status("partial", f"error while stopping the ETW session: {type(exc).__name__}: {exc}")
            self._session_health = _safe_health(session)
            if self._pump_dead is not None:
                # Packets were delivered until about when the consumer was last seen running, less the
                # reorder window they may still have been on their way in.
                end = max(0.0, self._pump_alive_t - self._table.reorder_s)
            with self._lock:
                self._table.finish(end)
            self._resolve_names()
            self._judge_health(self._session_health)
            self._publish()

    def result(self) -> dict[str, Any]:
        with self._lock:
            out = self._table.result(self._names)
            counts = dict(self._table.counts)
        if not self._started:
            # Nothing was listened to: "no talkers, no gaps" would read as measured and clean.
            out["talkers"] = out["gaps"] = out["server_rx"] = None
        out["layout"] = self.layout()
        out["health"] = self.health(counts)
        return out

    def layout(self) -> str:
        with self._lock:
            mismatches = bool(self._layout_bad)
        return "verified" if self._selftest.get("verified") and not mismatches else "unverified"

    def health(self, counts: dict | None = None) -> dict:
        h = dict(self._session_health)
        with self._lock:
            if counts is None:
                counts = dict(self._table.counts)
            bad = dict(self._layout_bad)
            lag_max, lag_p99 = self._lag_max, self._lag_p99()
        h.update(counts)
        h.update({"ignored": self._ignored, "layout_mismatch": bad, "bad_time": self._bad_time,
                  "selftest": dict(self._selftest), "names": self._names_note, "pid_errors": self._pid_errors,
                  "consumer_stopped_t": None if self._pump_dead is None else round(self._pump_dead[0], 3),
                  "delivery_lag_max_s": None if lag_max is None else round(lag_max, 3),
                  "delivery_lag_p99_s": lag_p99})
        return h

    def _lag_p99(self) -> float | None:
        """An upper bound of the 99th percentile of the game packets' delivery lag: the top of its 100 ms
        histogram step, or the largest lag past the last step (call with the lock held)."""
        n = sum(self._lag_hist)
        if not n:
            return None
        need, seen = 0.99 * n, 0
        for i, c in enumerate(self._lag_hist):
            seen += c
            if seen >= need:
                return round((i + 1) * LAG_BUCKET_S, 3) if i < LAG_BUCKETS else round(self._lag_max, 3)
        return round(self._lag_max, 3)

    # ---- consumer thread ------------------------------------------------
    def _on_event(self, ev: etw.EtwEvent) -> None:
        spec = EVENTS.get(ev.id)
        if spec is None:
            self._ignored += 1
            return
        data = ev.data
        if not layout_ok(ev.id, ev.version, len(data)):
            key = str(ev.id)
            with self._lock:
                self._layout_bad[key] = self._layout_bad.get(key, 0) + 1
            return
        t = clock.qpc_to_seconds(ev.timestamp) - self.origin
        if not -1.0 <= t <= self.now() + 5.0:          # not on the QPC clock: never guess a time
            self._bad_time += 1
            return
        pid, size = _PID_SIZE.unpack_from(data, 0)
        probing = self._probe is not None and pid == self._own_pid
        with self._lock:
            table = self._table
            if probing or (spec[0] == "udp" and table.is_game(pid)):
                pkt = decode(ev.id, ev.version, data)
                table.add(t, pkt)
                if probing and self._probe is not None:
                    self._probe.append(pkt)
                lag = self.now() - t            # how long ETW took to hand it over
                if self._lag_max is None or lag > self._lag_max:
                    self._lag_max = lag
                self._lag_hist[min(LAG_BUCKETS, max(0, int(lag / LAG_BUCKET_S)))] += 1
            else:
                table.count(pid, spec[1], size)

    # ---- helpers --------------------------------------------------------
    def _refresh_inputs(self, local: bool) -> None:
        src = self._pid_source
        if callable(src):
            try:
                pids = _clean_pids(src())
            except Exception:  # noqa: BLE001 - keep the last good set; count the failure
                self._pid_errors += 1
                pids = None
        else:
            pids = src
        addrs = None
        if local:
            try:
                addrs = frozenset(self._local_fn())
            except Exception:  # noqa: BLE001
                addrs = None
        with self._lock:
            if pids is not None:
                self._table.set_game_pids(pids)
            if addrs is not None:
                self._table.set_local(addrs)

    def _resolve_names(self) -> None:
        with self._lock:
            pids = [p for p in self._table.talker_pids() if p not in self._names]
            server = self._table.server
            if server is not None and server.key[0] not in self._names:
                pids.append(server.key[0])
        if not pids:
            return
        try:
            found = (self._names_fn or self._ntquery_names)(pids)
        except Exception as exc:  # noqa: BLE001 - names are a nicety; say why they are missing
            self._names_note = f"process names unavailable: {type(exc).__name__}: {exc}"
            return
        for pid in pids:
            self._names[pid] = found.get(pid)

    def _ntquery_names(self, pids: Iterable[int]) -> dict[int, str]:
        from lagdoctor.winapi import ntquery    # system snapshot, no process handles (DESIGN winapi)
        if self._snapshotter is None:
            self._snapshotter = ntquery.Snapshotter()
        wanted = set(pids)
        return {pid: info.name for pid, info in self._snapshotter.snapshot().items() if pid in wanted}

    def _check_pump(self, session: Any, t: float) -> None:
        """Time the consumer thread's state. Only our own stop() ends it on purpose, and that comes after
        the last check, so a stopped thread found here stopped by itself (4201, 998, or its session stopped
        from outside, NE 5.4)."""
        h = _safe_health(session)
        if h.get("pump_alive") is True:
            self._pump_alive_t = t
        elif h.get("pump_alive") is False and self._pump_dead is None:
            self._pump_dead = (t, h.get("process_trace_rc"))

    def _judge_health(self, h: dict) -> None:
        problems = []
        if self._pump_dead is not None:      # kept at the end too: the flows stop where the consumer did
            dead_t, rc = self._pump_dead
            problems.append(f"the ETW consumer stopped by itself by t={dead_t:.0f} s (last seen running at "
                            f"t={self._pump_alive_t:.0f} s; ProcessTrace returned {rc}), so no flows were "
                            "recorded after that")
        with self._lock:
            late, late_gaps = self._table.counts["late"], self._table.counts["gaps_late"]
            lag_max = self._lag_max
        if late:
            worst = f", up to {lag_max:.1f} s" if lag_max is not None else ""
            problems.append(f"{late} game packets arrived more than {self._table.reorder_s:g} s late (ETW delivery "
                            f"stalled{worst}), so gaps around them were not counted"
                            + (f" ({late_gaps} left out)" if late_gaps else ""))
        if h.get("lost") or h.get("buffers_lost"):
            problems.append(f"ETW dropped {h.get('lost') or 0} events and {h.get('buffers_lost') or 0} buffers, "
                            "so flow timing has holes")
        if h.get("callback_errors"):
            problems.append(f"{h['callback_errors']} events failed to decode ({h.get('first_error')})")
        with self._lock:
            bad = dict(self._layout_bad)
        if bad:
            ids = ", ".join(sorted(bad, key=int))
            problems.append(f"{sum(bad.values())} events had an unknown layout (ids {ids}): not decoded")
        if self._bad_time:
            problems.append(f"{self._bad_time} events had timestamps off the QPC clock: not used")
        if problems:
            self.set_status("partial", "; ".join(problems), TOOL)
        else:
            note = "verified by the self-test" if self.layout() == "verified" else \
                f"unverified ({self._selftest.get('reason')})"
            self.set_status("ok", f"Kernel-Network flows; address layout {note}", TOOL)

    def _publish(self) -> None:
        with self._lock:
            server = self._table.server
            rows = self._table.server_rx
            last = rows[-1][1] if rows and server is not None else None
            gaps = len(self._table.gaps)
        self.publish(server=f"{server.key[2]}:{server.key[3]}" if server else None,
                     server_ip=server.key[2] if server else None, server_pps=last, gaps=gaps)

    def _run_selftest(self) -> None:
        probe: deque[Packet] = deque(maxlen=MAX_PROBE)
        with self._lock:
            self._probe = probe
        try:
            traffic = selftest_traffic(self._selftest_server, self._selftest_port, self._selftest_port)
            self.selftest_traffic = traffic
            verdict: dict = {"verified": False, "reason": "no events seen"}
            deadline = clock.now() + SELFTEST_WAIT_S
            while not self.stopping:
                with self._lock:
                    packets = list(probe)
                verdict = judge_selftest(packets, pid=self._own_pid, udp_port=traffic["udp_port"],
                                         tcp_port=traffic["tcp_port"], remote_ip=self._selftest_server,
                                         remote_port=self._selftest_port)
                if verdict["verified"] or clock.now() >= deadline:
                    break
                self._stop_event.wait(0.2)
            verdict["traffic"] = traffic
            self._selftest = verdict
        except Exception as exc:  # noqa: BLE001 - a broken self-test leaves the layout unverified
            self._selftest = {"verified": False, "reason": f"self-test failed: {type(exc).__name__}: {exc}"}
        finally:
            with self._lock:
                self._probe = None


def _safe_health(session: Any) -> dict:
    try:
        return dict(session.health())
    except Exception as exc:  # noqa: BLE001
        return {"health_error": f"{type(exc).__name__}: {exc}"}
