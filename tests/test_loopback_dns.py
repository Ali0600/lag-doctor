"""The loopback DNS server of the self-test's tests (tests/loopback_dns.py), on the real network stack: it finds one
port number free for both UDP and TCP wherever the OS's port-0 hint stands, and says why when it cannot."""
from __future__ import annotations

import socket

import pytest

from lagdoctor.capture import netflows as nf
from tests.loopback_dns import DYNAMIC, HOST, LoopbackDns, bind_udp_and_tcp

HELD = 2000     # numbers held for UDP after the TCP hint: more than the TCP-excluded blocks its walk skips


def _next_tcp_port() -> int:
    """The number Windows gives the next TCP socket bound to port 0 (closed again at once)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _hold_udp_after(port: int, count: int) -> tuple[list[socket.socket], set[int]]:
    """UDP held on the `count` numbers after `port`, wrapping inside DYNAMIC as the hint does: (the sockets, every
    one of those numbers, now closed to UDP here, counting those that already were: excluded, or in use)."""
    held, closed = [], set()
    for k in range(1, count + 1):
        number = DYNAMIC[(port - DYNAMIC.start + k) % len(DYNAMIC)]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((HOST, number))
            held.append(s)
        except OSError:
            s.close()
        closed.add(number)
    return held, closed


def test_a_port_is_found_when_the_next_tcp_ports_are_all_closed_to_udp():
    """Windows hands port-0 numbers out in sequence, and its excluded ranges differ per protocol, so the next ten TCP
    numbers can all be closed to UDP: there the old stand-ins' ten tries (TCP's next number, then UDP on it) all
    failed. Made here on purpose, with UDP held on the numbers after the hint, and the hint checked to be inside."""
    held: list[socket.socket] = []
    try:
        for _ in range(3):                  # the hint jumps elsewhere now and then: a jump here voids the setup
            for s in held:
                s.close()
            held, closed = _hold_udp_after(_next_tcp_port(), HELD)
            ahead = [_next_tcp_port() for _ in range(10)]
            if set(ahead) <= closed:
                break
        else:
            pytest.fail(f"the TCP hint never stayed inside the numbers held for UDP: its next ten were {ahead}")
        dns = LoopbackDns()
        try:
            out = nf.selftest_traffic(HOST, dns.port, dns.port, timeout=3.0)
        finally:
            dns.close()
    finally:
        for s in held:
            s.close()
    assert dns.port not in closed
    assert out["errors"] == [] and out["udp_reply"] and out["tcp_reply"], out


def test_each_refused_try_is_named_with_its_protocol_and_error_and_leaves_nothing_bound():
    udp_only, tcp_side = bind_udp_and_tcp()             # a number held for UDP alone ...
    tcp_side.close()
    udp_side, tcp_only = bind_udp_and_tcp()             # ... and one held for TCP alone
    udp_side.close()
    a, b = udp_only.getsockname()[1], tcp_only.getsockname()[1]
    try:
        with pytest.raises(RuntimeError) as err:
            bind_udp_and_tcp(candidates=[a, b, a], tries=2)
        again = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            again.bind((HOST, b))                       # the try on b bound UDP before TCP refused: closed again
        finally:
            again.close()
    finally:
        udp_only.close()
        tcp_only.close()
    assert str(err.value) == f"no port free for both UDP and TCP in 2 tries: {a}: UDP error 10048, {b}: TCP error 10048"
