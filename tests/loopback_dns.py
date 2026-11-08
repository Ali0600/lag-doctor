"""A loopback DNS server for the network self-test's tests: one port number for both UDP and TCP, as DNS has on 53.

The number is drawn at random from the dynamic ports, never taken from the OS's port-0 choice. Windows hands port-0
numbers out in sequence, from one hint shared by every process, and excludes port ranges per protocol: on GitHub's
runners, Docker's NAT holds 200 UDP ports in a row, at a new place on each runner, that TCP may still use (9 of 10
runners, measured 2026-11-08). With the hint there, the next ten TCP numbers are all closed to UDP, and the old
stand-ins' ten tries (TCP's next number, then UDP on it) all failed: at 1.2 % of the hint's positions, which is the
"no free port for both UDP and TCP" CI saw on 2026-11-07 and 2026-11-08. Random tries do not follow one another,
and 2.3 % of dynamic ports were refused there (15 % on a laptop where Hyper-V holds blocks): 64 refusals in a row
would take a chance of 0.15 to the 64th power, about 2e-53, at worst.
"""
from __future__ import annotations

import itertools
import random
import socket
import struct
import threading
from collections.abc import Iterable, Iterator

HOST = "127.0.0.1"
DYNAMIC = range(49152, 65536)       # Windows' dynamic ports, by default (IANA's range)
TRIES = 64
_RNG = random.SystemRandom()


def random_ports() -> Iterator[int]:
    """Port numbers drawn from DYNAMIC, each independently of the ones before."""
    while True:
        yield _RNG.choice(DYNAMIC)


def bind_udp_and_tcp(host: str = HOST, candidates: Iterable[int] | None = None,
                     tries: int = TRIES) -> tuple[socket.socket, socket.socket]:
    """A UDP and a TCP socket bound to one port number on `host`, tried over `tries` candidates (random_ports() by
    default). A try that is refused closes both of its sockets. When every try is refused, the RuntimeError names
    each port with the protocol that refused it and the Windows error (10013: an excluded port, 10048: in use)."""
    refused = []
    for port in itertools.islice(random_ports() if candidates is None else candidates, tries):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proto = "UDP"
        try:
            udp.bind((host, port))
            proto = "TCP"
            tcp.bind((host, port))
            return udp, tcp
        except OSError as exc:
            udp.close()
            tcp.close()
            refused.append(f"{port}: {proto} error {getattr(exc, 'winerror', None) or exc.errno}")
    raise RuntimeError(f"no port free for both UDP and TCP in {len(refused)} tries: {', '.join(refused)}")


class LoopbackDns:
    """Answers one DNS query over UDP and one over TCP on `port` (bad_txid: with the query's id changed), and
    records the client port each came from (`udp_client`, `tcp_client`)."""

    def __init__(self, bad_txid: bool = False) -> None:
        self.bad, self.udp_client, self.tcp_client = bad_txid, None, None
        self.udp, self.tcp = bind_udp_and_tcp()
        self.port = self.udp.getsockname()[1]
        self.tcp.listen(1)
        self.tcp.settimeout(5)
        self.udp.settimeout(5)
        self.threads = [threading.Thread(target=self._serve_udp, daemon=True),
                        threading.Thread(target=self._serve_tcp, daemon=True)]
        for th in self.threads:
            th.start()

    def _answer(self, query: bytes) -> bytes:
        txid = struct.unpack(">H", query[:2])[0] ^ (1 if self.bad else 0)
        return struct.pack(">HHHHHH", txid, 0x8180, 1, 0, 0, 0) + query[12:]

    def _serve_udp(self) -> None:
        try:
            data, addr = self.udp.recvfrom(512)
            self.udp_client = addr[1]
            self.udp.sendto(self._answer(data), addr)
        except OSError:
            pass

    def _serve_tcp(self) -> None:
        try:
            conn, addr = self.tcp.accept()
            with conn:
                self.tcp_client = addr[1]
                n = struct.unpack(">H", conn.recv(2))[0]
                reply = self._answer(conn.recv(n))
                conn.sendall(struct.pack(">H", len(reply)) + reply)
        except OSError:
            pass

    def close(self) -> None:
        for th in self.threads:
            th.join(5)
        self.tcp.close()
        self.udp.close()
