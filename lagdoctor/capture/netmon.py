"""The network collector: ping rounds, Wi-Fi quality, NIC throughput, route, Wi-Fi scans, Steam downloads.

One row per probe round (0.5 s; docs/DECISIONS.md "Network probes in rounds"). Each round pings the
router, the internet anchor (1.1.1.1 unless told otherwise) and the game server once it is known.
Every target has its own small pool of worker threads: IcmpSendEcho2 blocks until a reply or its
timeout, and a lost probe can block for about a second (docs/research/network-and-etw.md, NE 1.3), so a
stuck probe must never hold up the next round. A round's row is written once all its probes have
answered or their deadline has passed; a reply after 1000 ms counts as lost.

Bookkeeping keeps "not probed" apart from "lost": a probe that was never sent (no worker free, the
target not known yet, the ping call itself failing) is not counted at all, `from_t` says when probing
each target began, and `lost` lists the round times of the probes that really went unanswered.
"""
from __future__ import annotations

import collections
import datetime
import ipaddress
import queue
import re
import threading
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lagdoctor import clock
from lagdoctor.capture.base import Collector
from lagdoctor.model import N, SeriesBuilder
from lagdoctor.winapi import icmp, iphlp, wlan

TARGETS = ("gateway", "internet", "server")
COLUMN = {"gateway": N.GW_RTT_MS, "internet": N.INET_RTT_MS, "server": N.SERVER_RTT_MS}

DEADLINE_GRACE_S = 0.25        # how long past the ICMP timeout a round waits for its stragglers
JOIN_BUDGET_S = 2.0            # workers get this long, in total, to finish at stop
MAX_EVENTS = 4096              # bound on wifi_scans and downloads
MAX_APS = 64                   # distinct access points labelled per session

_STEAM_RATE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Current download rate: "
                         r"(\d{1,7}(?:\.\d{1,6})?) Mbps\s*$")      # NE 6.1, e.g. "Current download rate: 98.213 Mbps"
_LOCAL_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/](?![\\/])")


# ------------------------------------------------------------------ pure helpers ---

def band_of(freq_mhz: int | None) -> str | None:
    """Wi-Fi band from the link's centre frequency (MHz, NE 3.3)."""
    if freq_mhz is None:
        return None
    if 2400 <= freq_mhz < 2500:
        return "2.4 GHz"
    if 5150 <= freq_mhz < 5925:
        return "5 GHz"
    if 5925 <= freq_mhz <= 7125:
        return "6 GHz"
    return None


def kind_of(if_type: int | None) -> str | None:
    if if_type is None:
        return None
    if if_type == iphlp.IF_TYPE_IEEE80211:
        return "wifi"
    if if_type == iphlp.IF_TYPE_ETHERNET_CSMACD:
        return "ethernet"
    return "other"


class Rate:
    """Per-second rate of a cumulative counter. None for the first reading and after the counter goes back."""

    def __init__(self) -> None:
        self.value: int | None = None
        self.t: float | None = None

    def update(self, value: int, t: float) -> float | None:
        prev_value, prev_t = self.value, self.t
        self.value, self.t = value, t
        if prev_value is None or prev_t is None or t <= prev_t or value < prev_value:
            return None
        return (value - prev_value) / (t - prev_t)

    def reset(self) -> None:
        self.value = self.t = None


def parse_steam_rate(line: str) -> tuple[float | None, float] | None:
    """A "Current download rate" line -> (its local-time stamp as epoch seconds or None, Mbit/s)."""
    m = _STEAM_RATE.match(line)
    if not m:
        return None
    try:
        stamp = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()   # Steam logs local time
    except (ValueError, OverflowError, OSError):
        stamp = None
    return stamp, float(m.group(2))


def steam_log_path(steam_path: object, kind: int) -> Path | None:
    """<SteamPath>\\logs\\content_log.txt, only for a plain local drive path (never UNC or a device)."""
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(steam_path, str):
        return None
    if not 3 <= len(steam_path) <= 260 or any(ord(c) < 32 for c in steam_path):
        return None
    if not _LOCAL_DRIVE_PATH.match(steam_path):
        return None
    return Path(steam_path) / "logs" / "content_log.txt"


def find_steam_log() -> Path | None:
    """Read-only: HKCU\\Software\\Valve\\Steam SteamPath (NE 6.1)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, "SteamPath")
    except OSError:
        return None
    return steam_log_path(value, kind)


def given_steam_log(value: object) -> Path | str | None:
    """NetworkSampler's steam_log argument: None (look it up in HKCU), "" (no Steam log), or that exact file,
    which must be a plain local drive path like the one HKCU gives (never UNC or a device). ValueError else."""
    if value is None or value == "":
        return value
    text = str(value) if isinstance(value, (str, Path)) else None
    if text is None or not 3 <= len(text) <= 260 or any(ord(c) < 32 for c in text) \
            or not _LOCAL_DRIVE_PATH.match(text):
        raise ValueError(f"not a local Steam log file path: {value!r}")
    return Path(text)


class LogTail:
    """New complete lines of a growing text file, at most max_bytes per poll.

    Starts at the end (what was logged before the capture is history), opens and closes the file on
    every poll so the writer can still rotate or delete it, and starts over on a replaced or truncated
    file. When it falls further behind than max_bytes it keeps only the newest bytes.
    """

    def __init__(self, path: Path, max_bytes: int = 65_536, max_line: int = 4_096) -> None:
        self.path = Path(path)
        self.max_bytes, self.max_line = max_bytes, max_line
        self.bytes_read = 0
        self.readable: bool | None = None   # did the last poll() see the file (and read it, if it had grown)?
        self._partial = b""
        self._skip_partial = False
        st = self._stat()
        self._ident = (st.st_dev, st.st_ino) if st else None
        self._pos = st.st_size if st else 0

    def _stat(self):
        try:
            return self.path.stat()
        except OSError:
            return None

    def poll(self) -> list[str]:
        self.bytes_read = 0
        st = self._stat()
        self.readable = st is not None
        if st is None:
            return []
        ident = (st.st_dev, st.st_ino)
        if ident != self._ident or st.st_size < self._pos:
            self._ident, self._pos, self._partial, self._skip_partial = ident, 0, b"", False
        available = st.st_size - self._pos
        if available <= 0:
            return []
        if available > self.max_bytes:
            self._pos = st.st_size - self.max_bytes
            self._partial, self._skip_partial = b"", True
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                data = f.read(self.max_bytes)
        except OSError:
            self.readable = False
            return []
        self._pos += len(data)
        self.bytes_read = len(data)
        parts = (self._partial + data).split(b"\n")
        tail = parts.pop()
        if self._skip_partial:
            if parts:
                parts.pop(0)                 # its start was skipped
                self._skip_partial = False
            else:
                tail = b""
        if len(tail) > self.max_line:        # an over-long line is dropped whole
            tail, self._skip_partial = b"", True
        self._partial = tail
        return [p.rstrip(b"\r").decode("utf-8", "replace") for p in parts]


# ------------------------------------------------------------------ the machine ---

class SystemNet:
    """The real machine, through lagdoctor.winapi. Tests pass a fake with the same methods."""

    def self_check(self, version: int) -> None:
        icmp.self_check(version)

    def pinger(self):
        return icmp.Pinger()

    def default_route(self) -> dict | None:
        return iphlp.default_route()

    def best_route(self, ip: str) -> dict | None:
        return iphlp.best_route(ip)

    def if_row(self, if_index: int) -> dict:
        return iphlp.if_row(if_index)

    def open_wlan(self) -> tuple[Any, str | None]:
        w = wlan.Wlan.open()
        return w, (None if w else wlan.Wlan.open_error)

    def steam_log(self) -> Path | None:
        return find_steam_log()


# ------------------------------------------------------------------ probe workers ---

class _Worker:
    def __init__(self, group: "_ProbeGroup", i: int) -> None:
        self.group = group
        self.busy = False
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._loop, name=f"lagdoctor-ping-{group.target}-{i}", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        g = self.group
        pinger, pinger_error = None, None
        try:
            pinger = g.make_pinger()
        except Exception as exc:  # noqa: BLE001 - reported with every job instead
            pinger_error = exc
        try:
            while True:
                job = self.inbox.get()
                if job is None:
                    return
                k, ip = job
                try:
                    if pinger is None:
                        raise pinger_error or OSError("no ICMP handle")
                    outcome: Any = pinger.ping(ip, g.timeout_ms)
                except Exception as exc:  # noqa: BLE001 - the call failed: not a lost probe, an error
                    outcome = exc
                g.results.put((g.target, k, outcome))
                self.busy = False
        finally:
            if pinger is not None:
                try:
                    pinger.close()
                except Exception:  # noqa: BLE001
                    pass


class _ProbeGroup:
    """The workers that ping one target."""

    def __init__(self, target: str, n: int, make_pinger, timeout_ms: int, results: queue.SimpleQueue) -> None:
        self.target, self.make_pinger, self.timeout_ms, self.results = target, make_pinger, timeout_ms, results
        self.workers = [_Worker(self, i) for i in range(n)]

    def submit(self, k: int, ip: str) -> bool:
        """Hand the probe to an idle worker. False when every worker is still waiting on an earlier probe."""
        for w in self.workers:
            if not w.busy:
                w.busy = True
                w.inbox.put((k, ip))
                return True
        return False

    def stop(self) -> None:
        for w in self.workers:
            w.inbox.put(None)


@dataclass
class _Round:
    k: int
    t: float
    deadline: float                                         # clock.now() after which missing replies are lost
    sent: dict[str, str] = field(default_factory=dict)      # target -> ip, for probes handed to a worker
    results: dict[str, Any] = field(default_factory=dict)   # target -> (status, rtt, from) or an exception
    row: dict[str, Any] = field(default_factory=dict)       # Wi-Fi and NIC values read this round


# ------------------------------------------------------------------ the collector ---

class NetworkSampler(Collector):
    """Collector "network" (docs/DESIGN.md, Collector specifics -> network).

    server_ip / internet_ip: hosts to ping besides the router (ValueError for anything else). steam_log:
    None looks the Steam log up in this user's HKCU; a path uses exactly that file (an elevated child is
    handed the user's own, since its HKCU may be another account's); "" reads no Steam log. The other
    keywords are the cadence (round, ICMP timeout, route/Steam/BSSID checks), workers per target, and
    `net`, the machine (SystemNet; tests pass a fake).

    result()["downloads"] is None when no download log was read at all (not measured, never "none
    seen"); "download_sources" names the logs that were read. Likewise result()["wifi_scans"] is None
    unless Wi-Fi scan notifications were registered (the status says why), and [] only when they were
    and no scan came.
    """

    name = "network"

    def __init__(self, server_ip: str | None = None, internet_ip: str = "1.1.1.1", *, round_s: float = 0.5,
                 timeout_ms: int = 1000, route_every_s: float = 5.0, steam_every_s: float = 2.0,
                 bssid_every_s: float = 15.0, workers: int = 3, steam_log: str | Path | None = None,
                 net: Any = None) -> None:
        super().__init__()
        if not round_s > 0 or not 1 <= timeout_ms <= icmp.MAX_TIMEOUT_MS or workers < 1:
            raise ValueError(f"bad cadence: round_s={round_s}, timeout_ms={timeout_ms}, workers={workers}")
        self._steam_arg = given_steam_log(steam_log)
        self.internet_ip = str(icmp.check_target(internet_ip))
        self.round_s, self.timeout_ms = round_s, timeout_ms
        self.route_every_s, self.steam_every_s, self.bssid_every_s = route_every_s, steam_every_s, bssid_every_s
        self.n_workers = workers
        self.net = net if net is not None else SystemNet()
        self._server_lock = threading.Lock()
        self._server: str | None = self._checked(server_ip)
        self._lock = threading.Lock()                       # series + bookkeeping, for result() during a run
        self._series = SeriesBuilder(N.ALL)
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._groups: dict[str, _ProbeGroup] = {}
        self._pending: collections.OrderedDict[int, _Round] = collections.OrderedDict()
        self._k = 0
        self._sent = dict.fromkeys(TARGETS, 0)
        self._lost = dict.fromkeys(TARGETS, 0)
        self._lost_t: dict[str, list[float]] = {t: [] for t in TARGETS}
        self._from_t: dict[str, float | None] = dict.fromkeys(TARGETS)
        self._errors = dict.fromkeys(TARGETS, 0)
        self._skipped = dict.fromkeys(TARGETS, 0)
        self._last_error: dict[str, str] = {}
        self._can_ping = {4: True, 6: True}
        self._problems: dict[str, str] = {}                 # area -> reason; makes the status "partial"
        self._failures: dict[str, tuple[int, str]] = {}     # read -> (times it failed, last error); same
        self._notes: dict[str, str] = {}                    # area -> remark that does not
        self._gateway: str | None = None
        self._route: dict[str, Any] = {"if_index": None, "description": None, "vpn": None, "toward": None}
        self._interface: dict[str, Any] = {"name": None, "kind": None, "band": None, "channel": None}
        self._nic: int | None = None
        self._nic_row: dict | None = None
        self._rx, self._tx, self._retries = Rate(), Rate(), Rate()
        self._wlan: Any = None
        self._wlan_tried = False
        self._wifi_guid: str | None = None
        self._scan_registered = False
        self._aps: dict[str, str] = {}
        self._scans: collections.deque[float] = collections.deque(maxlen=MAX_EVENTS)
        self._downloads: collections.deque[dict] = collections.deque(maxlen=MAX_EVENTS)
        self._steam: LogTail | None = None
        self._steam_path: Path | None = None
        self._sources: list[str] = []                       # download logs read at least once
        self._wall0 = self._cap0 = 0.0
        self._next_route = self._next_steam = self._next_bssid = 0.0

    # ---- public ---------------------------------------------------------------
    @staticmethod
    def _checked(ip: str | None) -> str | None:
        return None if ip is None else str(icmp.check_target(ip))

    def set_server(self, ip: str | None) -> None:
        """Start (or stop, with None) pinging the game server from the next round. Thread-safe.
        Raises ValueError for anything that is not a single host's IP address."""
        checked = self._checked(ip)
        with self._server_lock:
            self._server = checked

    def result(self) -> dict:
        with self._lock:
            return {
                "columns": self._series.build(),
                "targets": {"gateway": self._gateway, "internet": self.internet_ip, "server": self._server_ip()},
                "probes": {t: {"sent": self._sent[t], "lost": self._lost[t], "from_t": self._from_t[t]}
                           for t in TARGETS},
                "lost": {t: list(self._lost_t[t]) for t in TARGETS},
                # never registered: not watched, which must not read as "no scan happened"
                "wifi_scans": list(self._scans.copy()) if self._scan_registered else None,
                "route": dict(self._route),
                "interface": dict(self._interface),
                "downloads": [dict(d) for d in self._downloads] if self._sources else None,
                "download_sources": list(self._sources),
            }

    # ---- the loop ---------------------------------------------------------------
    def run(self) -> None:
        self._wall0, self._cap0 = time.time(), self.now()
        try:
            self._setup()
            for t in self.ticks(self.round_s):
                self._round(t)
            self._finish()
        finally:
            self._teardown()
        if self._thread is not None:      # None: stop() already gave up waiting and said so; keep that
            self._update_status()

    def _setup(self) -> None:
        for version in (4, 6):
            try:
                self.net.self_check(version)
            except Exception as exc:  # noqa: BLE001 - a broken layout or API means no pings of that kind
                self._can_ping[version] = False
                self._problems[f"icmp{version}"] = f"IPv{version} pings disabled, ICMP self-test failed: {exc}"
        if self._steam_arg is None:
            try:
                self._steam_path = self.net.steam_log()
            except Exception as exc:  # noqa: BLE001
                self._notes["steam"] = f"Steam download log not read: {exc}"
        elif self._steam_arg == "":
            self._notes["steam"] = "Steam download log not read: none was given"
        else:
            self._steam_path = self._steam_arg              # exactly this file, never this account's HKCU
        if self._steam_path is not None:
            self._steam = LogTail(self._steam_path)
        else:
            self._notes.setdefault("steam", "Steam download log not found")
        self._refresh_route()
        self._next_route = clock.now() + self.route_every_s
        self._update_status()

    def _round(self, t: float) -> None:
        rnd = _Round(self._k, t, deadline=clock.now() + self.timeout_ms / 1000.0 + DEADLINE_GRACE_S)
        self._k += 1
        for target, ip in self._targets_now():
            if self._group(target).submit(rnd.k, ip):
                rnd.sent[target] = ip
                if self._from_t[target] is None:
                    with self._lock:
                        self._from_t[target] = t
            else:
                self._skipped[target] += 1
        self._pending[rnd.k] = rnd
        now = clock.now()
        if now >= self._next_route:
            self._next_route = now + self.route_every_s
            self._refresh_route()
        rnd.row = self._read_link()
        if self._steam is not None and now >= self._next_steam:
            self._next_steam = now + self.steam_every_s
            self._read_steam()
        self._collect()
        self._finalize(force=False)
        self._update_status()

    def _finish(self) -> None:
        """After stop: give the probes still out until their deadline, then write the last rows."""
        last = max((r.deadline for r in self._pending.values()), default=0.0)
        while self._pending and clock.now() < last:
            self._collect()
            self._finalize(force=False)
            if self._pending:
                time.sleep(0.02)
        self._collect()
        self._finalize(force=True)

    def _teardown(self) -> None:
        for g in self._groups.values():
            g.stop()
        deadline = time.monotonic() + JOIN_BUDGET_S
        for g in self._groups.values():
            for w in g.workers:
                w.thread.join(max(0.0, deadline - time.monotonic()))
        if self._wlan is not None:
            self._wlan.close()          # unregisters the scan callback first (waits for a running one)

    # ---- probes ---------------------------------------------------------------
    def _server_ip(self) -> str | None:
        with self._server_lock:
            return self._server

    def _targets_now(self) -> list[tuple[str, str]]:
        out = []
        for target, ip in (("gateway", self._gateway), ("internet", self.internet_ip), ("server", self._server_ip())):
            if ip is None:
                continue
            if not self._can_ping[ipaddress.ip_address(ip.split("%")[0]).version]:
                continue
            out.append((target, ip))
        return out

    def _group(self, target: str) -> _ProbeGroup:
        g = self._groups.get(target)
        if g is None:
            g = self._groups[target] = _ProbeGroup(target, self.n_workers, self.net.pinger, self.timeout_ms,
                                                   self._results)
        return g

    def _collect(self) -> None:
        while True:
            try:
                target, k, outcome = self._results.get_nowait()
            except queue.Empty:
                return
            rnd = self._pending.get(k)
            if rnd is not None and target in rnd.sent:
                rnd.results[target] = outcome     # else: too late, that round's row already counts it lost

    def _finalize(self, force: bool) -> None:
        now = clock.now()
        while self._pending:
            rnd = next(iter(self._pending.values()))
            done = all(target in rnd.results for target in rnd.sent)
            if not (done or force or now >= rnd.deadline):
                return
            self._pending.popitem(last=False)
            self._write(rnd)

    def _write(self, rnd: _Round) -> None:
        row = dict(rnd.row)
        with self._lock:
            for target in rnd.sent:
                outcome = rnd.results.get(target)
                if isinstance(outcome, BaseException):
                    self._errors[target] += 1     # the ping call failed: nothing was measured
                    self._last_error[target] = f"{type(outcome).__name__}: {outcome}"
                    continue
                self._sent[target] += 1
                rtt = None
                if outcome is not None:
                    status, wall_ms, _frm = outcome
                    if status == icmp.IP_SUCCESS and wall_ms is not None and wall_ms <= self.timeout_ms:
                        rtt = wall_ms
                if rtt is None:
                    self._lost[target] += 1
                    self._lost_t[target].append(rnd.t)
                row[COLUMN[target]] = rtt
            self._series.add(t=rnd.t, **row)
        self.publish(t=rnd.t, gw_rtt_ms=row.get(N.GW_RTT_MS), inet_rtt_ms=row.get(N.INET_RTT_MS),
                     server_rtt_ms=row.get(N.SERVER_RTT_MS), wifi_signal_pct=row.get(N.WIFI_SIGNAL_PCT),
                     inet_lost=self._lost["internet"], inet_sent=self._sent["internet"])

    # ---- route, interface, Wi-Fi ----------------------------------------------------
    def _refresh_route(self) -> None:
        try:
            gw = self.net.default_route()
        except Exception as exc:  # noqa: BLE001
            self._failed("default route", exc)
            return                       # keep the last known router and adapter until the next check
        self._gateway = gw["next_hop"] if gw else None
        if gw is None:
            self._problems["gateway"] = "no default gateway: the router was not pinged while there was none"
        else:
            self._problems.pop("gateway", None)
        server = self._server_ip()
        toward, dest = ("server", server) if server else ("internet", self.internet_ip)
        best = None
        try:
            best = self.net.best_route(dest.split("%")[0])
            route = {"if_index": None, "description": None, "vpn": None, "toward": toward}
            if best:
                route["if_index"] = best["if_index"]
                egress = self.net.if_row(best["if_index"])
                route["description"], route["vpn"] = egress["description"], bool(egress["tunnel"])
            with self._lock:
                self._route = route
        except Exception as exc:  # noqa: BLE001 - keep the last known route
            self._failed(f"route toward the {toward}", exc)
        nic = gw["if_index"] if gw else (best["if_index"] if best else None)
        if nic != self._nic:
            self._use_nic(nic)
        elif self._wifi_guid is None and self._wlan is not None and self._nic_row is not None \
                and self._nic_row["type"] == iphlp.IF_TYPE_IEEE80211:
            self._match_wifi(self._nic_row)      # an earlier lookup failed: try again

    def _use_nic(self, nic: int | None) -> None:
        """Sample this adapter from now on (the physical link: with a VPN up, the one the tunnel rides on)."""
        self._rx.reset()
        self._tx.reset()
        self._retries.reset()
        self._wifi_guid = None
        interface = {"name": None, "kind": None, "band": None, "channel": None}
        row = None
        if nic is not None:
            try:
                row = self.net.if_row(nic)
                interface["name"], interface["kind"] = row["description"], kind_of(row["type"])
            except OSError as exc:
                self._failed("network adapter", exc)
                nic = None                       # so the next route check tries again
        self._nic, self._nic_row = nic, row
        with self._lock:
            self._interface = interface
        if row is not None and row["type"] == iphlp.IF_TYPE_IEEE80211:
            self._match_wifi(row)
        else:
            self._problems.pop("wifi", None)

    def _match_wifi(self, nic_row: dict) -> None:
        if self._wlan is None and not self._wlan_tried:
            self._wlan_tried = True
            try:
                self._wlan, why = self.net.open_wlan()
            except Exception as exc:  # noqa: BLE001
                self._wlan, why = None, str(exc)
            if self._wlan is None:
                self._problems["wifi"] = f"Wi-Fi signal and scans not measured: {why}"
                return
        if self._wlan is None:
            return
        try:
            found = self._wlan.interfaces()
        except Exception as exc:  # noqa: BLE001
            self._problems["wifi"] = f"Wi-Fi signal and scans not measured: {exc}"
            return
        match = [i for i in found if i["guid"] == nic_row["guid"]]
        if not match:
            connected = [i for i in found if i["connected"]]
            match = connected if len(connected) == 1 else []
        if not match:
            self._problems["wifi"] = ("Wi-Fi signal and scans not measured: the adapter in use is unknown to the "
                                      "WLAN service")
            return
        self._wifi_guid = match[0]["guid"]
        self._problems.pop("wifi", None)
        if not self._scan_registered:
            try:
                self._wlan.on_scan_complete(self._on_scan)
                self._scan_registered = True
                self._problems.pop("scans", None)
            except Exception as exc:  # noqa: BLE001
                self._problems["scans"] = f"Wi-Fi scans not recorded: their notifications could not be registered ({exc})"

    def _on_scan(self, t_clock: float, guid: str) -> None:
        # Runs on a WLAN service thread (NE 3.5): an append and nothing else, no locks.
        want = self._wifi_guid
        if want is not None and guid == want:
            self._scans.append(t_clock - self.origin)

    def _read_link(self) -> dict[str, Any]:
        row: dict[str, Any] = {}
        if self._nic is not None:
            try:
                r = self.net.if_row(self._nic)
                t = self.now()
                row[N.NIC_RX_BPS] = self._rx.update(r["in_octets"], t)      # bytes per second
                row[N.NIC_TX_BPS] = self._tx.update(r["out_octets"], t)
            except OSError as exc:
                self._failed("network adapter counters", exc)
        guid = self._wifi_guid
        if self._wlan is None or guid is None:
            return row
        try:
            q = self._wlan.quality(guid)
        except OSError as exc:
            q = None
            self._failed("Wi-Fi quality", exc)
        if q is not None:
            row.update({N.WIFI_SIGNAL_PCT: q["signal_pct"], N.WIFI_RSSI_DBM: q["rssi_dbm"],
                        N.WIFI_CHANNEL: q["channel"], N.WIFI_RX_MBPS: q["rx_mbps"], N.WIFI_TX_MBPS: q["tx_mbps"]})
            with self._lock:
                if q["channel"] is not None:
                    self._interface["channel"] = q["channel"]
                if q["freq_mhz"] is not None:
                    self._interface["band"] = band_of(q["freq_mhz"])
        try:
            retries = self._wlan.retries(guid)
            row[N.WIFI_RETRIES_PS] = self._retries.update(retries, self.now()) if retries is not None else None
        except OSError as exc:
            self._failed("Wi-Fi retry counter", exc)
        if clock.now() >= self._next_bssid:
            self._next_bssid = clock.now() + self.bssid_every_s
            row[N.WIFI_BSSID] = self._read_bssid(guid)
        return row

    def _failed(self, what: str, exc: BaseException) -> None:
        """A read that failed this round: its value is None, and the status says how often it happened."""
        count, _ = self._failures.get(what, (0, ""))
        self._failures[what] = (count + 1, f"{type(exc).__name__}: {exc}")

    def _read_bssid(self, guid: str) -> str | None:
        """A per-session label ("AP 1", "AP 2" ...) instead of the MAC: a change still shows roaming,
        and the capture never holds an address that locates the user (NE 3.6)."""
        try:
            mac = self._wlan.bssid(guid)
        except OSError:
            mac = None
        if mac is None:
            self._notes["bssid"] = ("access point (BSSID) not measured: it needs Windows location permission "
                                    "for desktop apps")
            return None
        self._notes.pop("bssid", None)
        label = self._aps.get(mac)
        if label is None:
            label = f"AP {len(self._aps) + 1}" if len(self._aps) < MAX_APS else f"AP {MAX_APS}+"
            if len(self._aps) < MAX_APS:
                self._aps[mac] = label
        return label

    # ---- Steam downloads ---------------------------------------------------------
    def _read_steam(self) -> None:
        read_t = self.now()
        lines = self._steam.poll()
        if self._steam.readable and "steam" not in self._sources:
            with self._lock:
                self._sources.append("steam")
            self._notes.pop("steam", None)
        elif not self._steam.readable and "steam" not in self._sources:
            self._notes["steam"] = f"Steam download log not read: {self._steam.path} could not be read"
        for line in lines:
            parsed = parse_steam_rate(line)
            if parsed is None:
                continue
            stamp, mbps = parsed
            entry = {"t": self._log_time(stamp, read_t), "source": "steam", "mbps": mbps}
            with self._lock:
                self._downloads.append(entry)

    def _log_time(self, stamp: float | None, read_t: float) -> float:
        """The log line's own time on the capture clock; the read time if the stamp is missing or odd."""
        if stamp is None:
            return read_t
        t = self._cap0 + (stamp - self._wall0)
        if not read_t - 300.0 <= t <= read_t + 2.0:
            return read_t
        return max(t, 0.0)

    # ---- status ---------------------------------------------------------------
    def _update_status(self) -> None:
        reasons = list(self._problems.values())
        for what, (count, last) in self._failures.items():
            reasons.append(f"{what} not read {count} time{'s' if count > 1 else ''} ({last})")
        for target in TARGETS:
            if self._errors[target]:
                reasons.append(f"{self._errors[target]} pings to the {target} failed to send "
                               f"({self._last_error.get(target, '?')})")
            if self._skipped[target]:
                reasons.append(f"{self._skipped[target]} pings to the {target} not sent: earlier ones still waiting")
        notes = list(self._notes.values())
        if not self._scan_registered and "wifi" not in self._problems and "scans" not in self._problems:
            # wifi_scans is None then; on a Wi-Fi link a failure above already says why
            notes.append("Wi-Fi scans not watched: " + ("the network adapter in use is not known" if self._nic is None
                                                        else "the link in use is not Wi-Fi"))
        detail = "; ".join(reasons + notes)
        self.set_status("partial" if reasons else "ok", detail, tool="IcmpSendEcho2, GetIfEntry2, Native Wifi")
