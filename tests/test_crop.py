"""analysis/crop.py: a recording cut to the part that is analysed (M5, docs/LAUNCHER-PLAN.md "Measuring the right
part"). Times stay the recording's own; every table, list of times and total is cut, clipped, counted again or
dropped; a cut copy is never saved."""
from __future__ import annotations

import ast
import gzip
import json
import math
import re
from pathlib import Path

import pytest

from lagdoctor.analysis import crop as crop_mod
from lagdoctor.analysis import rules
from lagdoctor.analysis.crop import EMPTY_IN_SPAN, MIN_SPAN_S, NET_FLOWS, check_span, crop
from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.capture import session as sess
from lagdoctor.model import F, G, N, S, Capture, SourceStatus
from lagdoctor.report import labels
from tests import synth

ROOT = Path(__file__).resolve().parents[1]
LOST = {"gateway": [20.0, 70.0], "internet": [30.0, 40.5, 80.0], "server": []}


def _rtt(lost: list[float], ms: float):
    return lambda t: None if t in lost else ms


def rich_capture() -> Capture:
    """100 s with every table, other programs, marks and every net_flows key the collectors write."""
    fr = synth.frames_table([1000.0 / 60] * 5700)                          # 1.0 s to about 96 s
    sys_ = synth.system_table(100.0, cpu_total_pct=10.0, game_focused=1)
    gpu = synth.gpu_table(100.0, **{G.UTIL_PCT: 50.0, G.POWER_CAP_PCT: None})
    net = synth.network_table(100.0, gw_rtt_ms=_rtt(LOST["gateway"], 3.0), inet_rtt_ms=_rtt(LOST["internet"], 24.0))
    others = [[t, [[42, "app.exe", 5.0, 0.0, 0.0, 1, 0.0]]] for t in sys_[S.T]]
    nf = {
        "targets": {"gateway": "192.168.0.1", "internet": "198.51.100.1", "server": None},
        "probes": {"gateway": {"sent": 200, "lost": 2, "from_t": 0.5},
                   "internet": {"sent": 200, "lost": 3, "from_t": 0.5},
                   "server": {"sent": 0, "lost": 0, "from_t": None}},
        "lost": {role: list(ts) for role, ts in LOST.items()},
        "wifi_scans": [15.0, 55.0, 95.0],
        "route": {"vpn": False},
        "downloads": [{"t": 12.0, "source": "steam", "mbps": 80.0}, {"t": 60.0, "source": "steam", "mbps": 40.0}],
        "download_sources": ["steam"],
        "server": {"ip": "203.0.113.7", "port": 27015, "proto": "udp", "pid": 1, "rx_packets": 9000,
                   "tx_packets": 4000, "rx_bytes": 1_800_000, "tx_bytes": 400_000, "tick_ms": 15.6,
                   "first_rx_t": 5.0, "last_rx_t": 95.0},
        "talkers": [{"pid": 7, "name": "steam.exe", "rx_bytes": 900_000_000, "tx_bytes": 1_000_000}],
        "talkers_scope": "all",
        "game_wire_bytes": [1_800_000, 400_000],
        "gaps": [[38.0, 4000.0, "loss"], [55.0, 500.0, "local"], [79.0, 3000.0, "delay"], [90.0, 9000.0, "open"]],
        "server_rx": [[float(k), 60] for k in range(1, 101)],
        "layout": "verified",
        "health": {"events": 123, "consumer_stopped_t": None},
    }
    cap = synth.capture(fr, sys_, duration=100.0, gpu=gpu, network=net, others=others,
                        marks=[10.0, 50.0, 99.5, 100.6], net_flows=nf)
    cap.sources = synth.sources(("frames", "system", "gpu", "network", "net_flows"))
    return cap


def _times(table: dict) -> list[float]:
    return [float(t) for t in table["t"]]


# ------------------------------------------------------------------------------ the cut ---

def test_the_tables_keep_their_rows_in_the_span_with_their_own_times():
    cap = rich_capture()
    part = crop(cap, 40.0, 80.0)
    for name in ("system", "gpu", "network"):
        whole, cut = getattr(cap, name), getattr(part, name)
        assert _times(cut) == [t for t in _times(whole) if 40.0 <= t <= 80.0] and _times(cut)
        assert set(cut) == set(whole)                      # every column kept, aligned to t
        assert all(len(col) == len(cut["t"]) for col in cut.values())
    assert part.system[S.CPU_TOTAL_PCT][0] == 10.0 and min(_times(part.system)) == 40.0
    assert [row[0] for row in part.others] == _times(part.system)
    assert part.marks == [50.0]
    assert part.duration_s == 40.0
    assert part.span == {"from_s": 40.0, "to_s": 80.0, "recorded_s": 100.0, "how": "set"}
    assert part.started_at == cap.started_at and part.clock_origin == cap.clock_origin
    assert part.inventory == cap.inventory and part.inventory is not cap.inventory
    assert part.target == cap.target and part.target is not cap.target


def test_every_column_key_is_kept_even_where_its_cut_is_empty():
    """attribute.cap_in_throttle tells a capture from before the power-cap split by the key alone."""
    cap = rich_capture()
    part = crop(cap, 40.0, 80.0)
    assert G.POWER_CAP_PCT in part.gpu and part.gpu[G.POWER_CAP_PCT] == [None] * len(part.gpu["t"])


def test_crop_never_changes_the_capture_it_is_given():
    cap = rich_capture()
    before = json.dumps(cap.to_json(), sort_keys=True)
    part = crop(cap, 40.0, 80.0, how="key")
    part.net_flows["targets"]["gateway"] = "changed"
    part.target["pid"] = 99
    part.sources["frames"].detail = "changed"
    assert json.dumps(cap.to_json(), sort_keys=True) == before
    assert cap.span is None and part.span["how"] == "key"


def test_a_part_that_runs_to_the_end_keeps_a_press_just_after_it():
    """The recording keeps a lag-key press up to 1 s after it stopped (capture/session.py), and so does a part that
    ends where the recording ends; one that ends earlier keeps only its own."""
    cap = rich_capture()
    assert crop(cap, 60.0, 100.0).marks == [99.5, 100.6]
    assert crop(cap, 60.0, 500.0).marks == [99.5, 100.6]
    assert crop(cap, 60.0, 99.0).marks == []


@pytest.mark.parametrize("t0,t1,said", [
    (float("nan"), 50.0, "numbers of seconds"),
    (10.0, float("inf"), "numbers of seconds"),
    (True, 50.0, "numbers of seconds"),
    ("10", 50.0, "numbers of seconds"),
    (-1.0, 50.0, "before the recording began"),
    (100.0, 150.0, "the recording ended at 1m 40s"),
    (50.0, 50.0, "not after its start"),
    (60.0, 40.0, "not after its start"),
    (95.0, 120.0, "at least 10 s"),
    (20.0, 29.9, "at least 10 s"),
])
def test_check_span_refuses_what_is_not_a_part_of_the_recording(t0, t1, said):
    with pytest.raises(ValueError, match=re.escape(said)):
        check_span(rich_capture(), t0, t1)
    with pytest.raises(ValueError):
        crop(rich_capture(), t0, t1)


def test_check_span_ends_a_span_past_the_recording_at_its_end():
    assert check_span(rich_capture(), 90.0, 500.0) == (90.0, 100.0)
    assert check_span(rich_capture(), 0.0, MIN_SPAN_S) == (0.0, MIN_SPAN_S)


def test_a_part_of_exactly_the_minimum_is_taken_and_a_shorter_one_says_how_short():
    """To the millisecond: 16.4 - 6.4 is 9.999999999999998 in floats. A refused length is rounded down, never up to
    the minimum it is refused against."""
    assert check_span(rich_capture(), 6.4, 16.4) == (6.4, 16.4)
    with pytest.raises(ValueError, match=re.escape("it would be 9.9 s long, and a part needs at least 10 s")):
        check_span(rich_capture(), 20.0, 29.96)


def test_a_part_of_a_part_is_refused():
    """A cut copy's duration_s is its part's length, never the recording's end."""
    with pytest.raises(ValueError, match="a part of a recording already"):
        check_span(crop(rich_capture(), 40.0, 80.0), 50.0, 70.0)


def test_a_time_too_large_for_a_float_is_junk_not_a_crash():
    """A file can hold an integer no float can: it reads as a damaged time, as everywhere else in the analysis."""
    cap = rich_capture()
    cap.system["t"][0] = 10 ** 400
    assert crop(cap, 40.0, 80.0).system["t"][0] == 40.0
    cap.duration_s = 10 ** 400
    with pytest.raises(ValueError, match="length is not known"):
        check_span(cap, 10.0, 50.0)


@pytest.mark.parametrize("length", [float("nan"), 0.0, -5.0, None])
def test_a_recording_of_unknown_length_cannot_be_cut(length):
    cap = rich_capture()
    cap.duration_s = length
    with pytest.raises(ValueError, match="length is not known"):
        check_span(cap, 10.0, 50.0)


def test_crop_refuses_a_way_a_span_is_not_set():
    with pytest.raises(ValueError):
        crop(rich_capture(), 40.0, 80.0, how="guess")      # a guessed start is only ever proposed, never analysed


# ------------------------------------------------------------------------------ frames ---

def _two_chains() -> Capture:
    a = synth.frames_table([1000.0 / 60] * 3000, chain="0xA", display_ms=1000.0 / 60)      # 1 s to 51 s, shown
    b = synth.frames_table([1000.0 / 30] * 400, t0=30.0, chain="0xB")         # 30 s to 43.3 s
    fr = {k: a[k] + b[k] for k in a}
    return synth.capture(fr, None, duration=60.0)


def test_each_chains_first_frame_whose_interval_began_before_the_span_is_left_out():
    cap = _two_chains()
    part = crop(cap, 20.0, 40.0)
    inside = [(t, c) for t, c in zip(cap.frames[F.T], cap.frames[F.SWAP_CHAIN]) if 20.0 <= t <= 40.0]
    firsts = {c: min(t for t, cc in inside if cc == c) for c in ("0xA", "0xB")}
    kept = list(zip(part.frames[F.T], part.frames[F.SWAP_CHAIN]))
    assert (firsts["0xA"], "0xA") not in kept and len(kept) == len(inside) - 1   # 0xB starts inside: its first stays
    assert (firsts["0xB"], "0xB") in kept
    assert all(len(col) == len(kept) for col in part.frames.values())


def test_every_chain_that_began_before_the_part_loses_its_first_frame():
    cap = _two_chains()                                  # at 35 s both chains have presented already
    inside = [t for t in cap.frames[F.T] if 35.0 <= t <= 45.0]
    assert len(crop(cap, 35.0, 45.0).frames[F.T]) == len(inside) - 2


def test_a_chain_shown_long_after_the_screen_last_changed_loses_its_first_frame():
    """MsBetweenDisplayChange counts from the frame shown before, which can come long before the part, even for a chain
    whose first present is in it."""
    cap = _two_chains()
    first_b = cap.frames[F.SWAP_CHAIN].index("0xB")
    cap.frames[F.DISPLAY_MS][first_b] = 15000.0                     # the screen last changed 15 s earlier
    part = crop(cap, 20.005, 40.0)
    assert (cap.frames[F.T][first_b], "0xB") not in list(zip(part.frames[F.T], part.frames[F.SWAP_CHAIN]))


def test_a_part_from_the_recordings_own_start_keeps_every_frame():
    """PresentMon's first row measures from a present before the recording began; the whole recording's analysis
    keeps it, and so does a part from t = 0."""
    fr = synth.frames_table([500.0] + [1000.0 / 60] * 1200, t0=-0.3)    # the first frame began 0.3 s before t = 0
    cap = synth.capture(fr, None, duration=30.0)
    assert cap.frames[F.T][0] - cap.frames[F.FRAME_MS][0] / 1000.0 < 0
    assert crop(cap, 0.0, 20.0).frames[F.T] == [t for t in cap.frames[F.T] if t <= 20.0]


def test_a_wait_carried_by_the_first_shown_frame_is_left_out_too():
    """A game faster than the screen: the first present after a 5 s load was never shown (no display change), and the
    next one, 3 ms later, was, so its MsBetweenDisplayChange counts from the last frame shown before the load."""
    hz = 1000.0 / 60
    ms = [hz] * 600 + [5000.0, 3.0] + [hz] * 1800
    shown = [1] * 600 + [0, 1] + [1] * 1800
    disp = [hz] * 600 + [None, 5000.0 + 3.0 + hz / 2] + [hz] * 1800
    cap = synth.capture(synth.frames_table(ms, display_ms=disp, displayed=shown), None, hz=60.0, duration=50.0)
    cap.sources = synth.sources(("frames",))
    wait_end = cap.frames[F.T][600]
    d = diagnose(crop(cap, wait_end - 0.5, 50.0))
    assert d.stats["worst_frame_ms"] < 20.0 and d.stats["low1_fps"] > 55.0
    assert diagnose(crop(cap, wait_end + 0.001, 50.0)).stats["worst_frame_ms"] < 20.0     # a start at the load's end


def test_a_long_wait_before_the_span_is_never_its_first_frame():
    """A start set at the end of a loading screen: the first frame after it measured the whole wait."""
    ms = [1000.0 / 60] * 600 + [5000.0] + [1000.0 / 60] * 1800                # a 5 s frame lands at about 16 s
    cap = synth.capture(synth.frames_table(ms, display_ms=ms, displayed=1), None, duration=50.0)
    cap.sources = synth.sources(("frames",))
    long_at = cap.frames[F.T][600]
    d = diagnose(crop(cap, long_at - 0.5, 50.0))
    assert d.stats["worst_frame_ms"] < 20.0


# ---------------------------------------------------------------------------- sources ---

def _before(table: dict, t1: float) -> dict:
    return {k: [v for t, v in zip(table["t"], col) if t < t1] for k, col in table.items()}


def test_a_source_with_rows_only_outside_the_span_says_so():
    cap = rich_capture()
    cap.system = _before(cap.system, 30.0)
    cap.sources["gpu"] = SourceStatus("off", "turned off")
    part = crop(cap, 40.0, 80.0)
    assert part.sources["system"].state == "unavailable" and part.sources["system"].detail == EMPTY_IN_SPAN
    assert part.sources["frames"].state == "ok" and part.sources["gpu"].state == "off"
    d = diagnose(part)
    assert d.measured["system"] == EMPTY_IN_SPAN
    assert any(why.endswith("which recorded nothing in the measured part (see Not measured)")
               for why in d.unchecked.values())
    assert not any("system sampler, which was not recorded" in why for why in d.unchecked.values())


def test_a_source_keeps_its_own_reason_when_it_failed_and_its_state_when_it_never_recorded():
    """Only a source that recorded, and recorded nothing in the part, is told apart: a failed one keeps its failure,
    and an "ok" one with no rows at all stays as the analysis already reads it ("it ran but recorded nothing")."""
    cap = rich_capture()
    cap.gpu = _before(cap.gpu, 30.0)
    cap.sources["gpu"] = SourceStatus("failed", "NVML stopped answering")
    cap.network = {k: [] for k in cap.network}
    part = crop(cap, 40.0, 80.0)
    assert (part.sources["gpu"].state, part.sources["gpu"].detail) == ("failed", "NVML stopped answering")
    assert part.sources["network"].state == "ok"


# -------------------------------------------------------------------------- net_flows ---

def test_every_net_flows_key_the_collectors_write_has_a_decision():
    """A key a collector adds goes red here until crop says what a part does with it (cut, clip, count again,
    drop or keep)."""
    assert set(NET_FLOWS) == set(sess.NETWORK_FLOW_KEYS) | set(sess.FLOW_KEYS)
    assert set(NET_FLOWS.values()) == {"kept", "cut", "clipped", "counted again", "dropped"}


def test_net_flows_lists_of_times_are_cut_and_totals_dropped():
    cap = rich_capture()
    cap.net_flows["from_a_newer_build"] = 12345
    nf = crop(cap, 40.0, 80.0).net_flows
    assert nf["lost"] == {"gateway": [70.0], "internet": [40.5, 80.0], "server": []}
    assert nf["wifi_scans"] == [55.0]
    assert nf["downloads"] == [{"t": 60.0, "source": "steam", "mbps": 40.0}]
    assert [r[0] for r in nf["server_rx"]] == [float(k) for k in range(41, 81)]
    assert nf["talkers"] is None and nf["game_wire_bytes"] is None
    assert nf["from_a_newer_build"] is None
    for key in ("targets", "route", "download_sources", "talkers_scope", "layout", "health"):
        assert nf[key] == cap.net_flows[key]
    server = nf["server"]
    assert all(server[k] is None for k in ("rx_packets", "tx_packets", "rx_bytes", "tx_bytes"))
    assert (server["first_rx_t"], server["last_rx_t"]) == (40.0, 80.0)
    assert (server["ip"], server["tick_ms"]) == ("203.0.113.7", 15.6)


def test_not_recorded_stays_not_recorded_and_nothing_in_the_span_stays_a_list():
    cap = rich_capture()
    cap.net_flows.update(wifi_scans=None, downloads=None, gaps=None, talkers=None, server_rx=None)
    nf = crop(cap, 40.0, 80.0).net_flows
    assert nf["wifi_scans"] is None and nf["downloads"] is None and nf["gaps"] is None and nf["server_rx"] is None
    cap.net_flows.update(wifi_scans=[5.0], downloads=[])
    nf = crop(cap, 40.0, 80.0).net_flows
    assert nf["wifi_scans"] == [] and nf["downloads"] == []


def test_a_server_flow_recorded_outside_the_span_takes_its_times_from_the_parts_own_bins():
    """The recorded flow (2 s to 30 s) lies outside the part, but packets came in every second's bin of it."""
    cap = rich_capture()
    cap.net_flows["server"].update(first_rx_t=2.0, last_rx_t=30.0)
    server = crop(cap, 40.0, 80.0).net_flows["server"]
    assert (server["first_rx_t"], server["last_rx_t"]) == (40.0, 80.0) and "in_part" not in server


def test_the_probes_are_counted_again_over_the_span():
    """Exact: every probe the network collector counts as sent was answered (its round's row) or lost (`lost`)."""
    nf = crop(rich_capture(), 40.0, 80.0).net_flows["probes"]
    rows = 81                                          # rounds every 0.5 s from 40.0 to 80.0
    assert nf["gateway"] == {"sent": rows, "lost": 1, "from_t": 0.5}
    assert nf["internet"] == {"sent": rows, "lost": 2, "from_t": 0.5}
    assert nf["server"] == {"sent": 0, "lost": 0, "from_t": None}


def test_probes_keyed_by_address_are_counted_again_too():
    cap = rich_capture()
    cap.net_flows["probes"] = {"198.51.100.1": cap.net_flows["probes"]["internet"]}
    cap.net_flows["lost"] = {"198.51.100.1": LOST["internet"]}
    assert crop(cap, 40.0, 80.0).net_flows["probes"] == {"198.51.100.1": {"sent": 81, "lost": 2, "from_t": 0.5}}


def test_probe_counts_without_the_lost_probes_times_cannot_be_split_and_become_none():
    cap = rich_capture()
    del cap.net_flows["lost"]
    assert crop(cap, 40.0, 80.0).net_flows["probes"] is None


def test_the_loss_figure_covers_only_the_span():
    cap = rich_capture()
    d_whole, d_part = diagnose(cap), diagnose(crop(cap, 40.0, 80.0))
    assert d_whole.stats["loss_pct"] == pytest.approx(100.0 * 3 / 200)
    assert d_part.stats["loss_pct"] == pytest.approx(100.0 * 2 / 81)


def test_a_part_says_why_each_programs_traffic_is_not_known():
    """Crop drops the per-program byte totals, so bandwidth_hog cannot clear a part by them, and says why (never "it
    ran but recorded nothing", which reads as the ETW session failing)."""
    d = diagnose(crop(rich_capture(), 62.0, 98.0))            # no download logged in the part
    assert d.unchecked["bandwidth_hog"].startswith("each program's traffic was counted over the whole recording, so "
                                                   "it is not known for the measured part")


def test_a_part_after_the_etw_consumer_stopped_names_when_it_stopped():
    cap = rich_capture()
    cap.net_flows["health"]["consumer_stopped_t"] = 50.0
    cap.net_flows.update(talkers=[], downloads=[])
    why = diagnose(crop(cap, 60.0, 98.0)).unchecked["bandwidth_hog"]
    assert why.startswith("each program's traffic was counted only until the ETW consumer stopped at 50 s, so it is "
                          "not known for the measured part")
    assert "it ran but recorded nothing" not in why


def test_the_game_servers_silences_are_clipped_to_the_span():
    """One that began before the part keeps when it began, as a fourth field, for the words."""
    gaps = crop(rich_capture(), 40.0, 80.0).net_flows["gaps"]
    assert gaps == [[40.0, 2000.0, "loss", 38.0], [55.0, 500.0, "local"], [79.0, 1000.0, "delay"]]


def _servers(cap: Capture, *, first: float, last: float, bins: dict[int, int], gaps: list) -> Capture:
    """The recorded server flow (the server for the most seconds) from `first` to `last`, and the packet bins and
    silences of every server, as the tracker writes them (capture/netflows.py)."""
    cap.net_flows["server"].update(first_rx_t=first, last_rx_t=last)
    cap.net_flows["server_rx"] = [[float(k), bins.get(k, 0)] for k in range(1, 101)]
    cap.net_flows["gaps"] = gaps
    return cap


def test_a_part_over_another_servers_match_keeps_a_server_and_its_silences():
    """Server A served from 6 s to 25 s, then server B, the one recorded, from 30 s: a part over A's match still has a
    game server, with its packets' times from the part's own bins, and A's silences."""
    cap = _servers(rich_capture(), first=30.0, last=95.0, bins={k: 50 for k in range(6, 26)} |
                   {k: 60 for k in range(30, 96)}, gaps=[[12.0, 500.0, "delay"], [18.0, 600.0, "delay"]])
    part = crop(cap, 5.0, 28.0)
    server = part.net_flows["server"]
    assert server is not None and "in_part" not in server
    assert (server["first_rx_t"], server["last_rx_t"]) == (5.0, 25.0)
    d = diagnose(part)
    assert "TCP-only" not in d.unchecked.get("server_lag", "")
    assert any("went quiet 2 times" in x.text for f in d.findings if f.cause == "server_lag" for x in f.evidence)


def test_a_part_with_nothing_of_a_server_says_so_and_never_guesses_a_tcp_game():
    cap = _servers(rich_capture(), first=30.0, last=95.0, bins={k: 60 for k in range(30, 96)}, gaps=[])
    part = crop(cap, 5.0, 28.0)
    assert part.net_flows["server"]["in_part"] is False
    assert diagnose(part).unchecked["server_lag"] == rules.OUTSIDE_THE_PART


def test_a_server_lost_before_the_part_is_named_with_when_it_was_lost():
    """The server's last packet came at 38 s and the game kept sending (an "open" gap to the end): a part from 40 s
    says it had stopped answering before the part, at 38 s, never at the part's start."""
    cap = _servers(rich_capture(), first=5.0, last=38.0, bins={k: 60 for k in range(5, 39)},
                   gaps=[[38.0, 62000.0, "open"]])
    part = crop(cap, 40.0, 80.0)
    assert part.net_flows["gaps"] == [[40.0, 40000.0, "open", 38.0]]
    assert "in_part" not in part.net_flows["server"]
    d = diagnose(part)
    assert "had stopped answering before the measured part, at 38s" in d.headline and "at 40s" not in d.headline
    texts = [x.text for f in d.findings for x in f.evidence]
    assert any("before the measured part, at 38 s" in t for t in texts)
    assert "the game server stopped answering at 38s" in diagnose(cap).headline       # the whole recording's own


# ---------------------------------------------------------------------- the analysis ---

def test_the_startup_is_left_out_of_the_verdict():
    cap = synth.startup_then_smooth(startup_s=60.0, total_s=180.0)
    whole = diagnose(cap)
    assert whole.lag_type == "stutter" and whole.stats["stutters"] >= 15 and "span" not in whole.stats
    part = diagnose(crop(cap, 70.0, 180.0))
    assert part.lag_type == "none" and part.stats["stutters"] == 0
    assert part.stats["duration_s"] == 110.0
    assert part.stats["span"] == {"from_s": 70.0, "to_s": 180.0, "recorded_s": 180.0, "how": "set"}
    assert labels.measured_part(part.stats) == part.stats["span"]


def test_a_cropped_diagnosis_keeps_the_recordings_times():
    cap = synth.startup_then_smooth(startup_s=60.0, total_s=180.0)
    d = diagnose(crop(cap, 30.0, 180.0))
    stutters = [ev for ev in d.events if ev.kind == "stutter"]
    assert stutters and all(30.0 <= ev.t_start and ev.t_end <= 60.5 for ev in stutters)


def test_shader_compile_counts_its_first_third_from_the_parts_start():
    """Stutters from the game's own work that bunch up at the start of the part are early in it, wherever the part
    starts in the recording (here 150 s in, past a third of its 150 s length)."""
    cap = synth.startup_then_smooth(startup_s=200.0, total_s=300.0)
    d = diagnose(crop(cap, 150.0, 300.0))
    found = [f for f in d.findings if f.cause == "shader_compile"]
    assert found, [(f.cause, f.confidence) for f in d.findings]
    assert any("first third of the measured part" in x.text for x in found[0].evidence)


def _shown(d) -> list[str]:
    """Every text a diagnosis shows the player: the headline, each finding's title, summary and evidence, and the
    cleared and unchecked reasons."""
    return [d.headline] + [t for f in d.findings for t in [f.title, f.summary] + [x.text for x in f.evidence]] + \
        list(d.cleared.values()) + list(d.unchecked.values())


_CAPTURE_WORDS = re.compile(r"\b(?:in|of|during|over|through|throughout|across|for) the (?:\d+ s )?"
                            r"(?:capture|recording|session)\b", re.IGNORECASE)


def _wifi_capture() -> Capture:
    cap = rich_capture()
    cap.inventory["network"] = {"kind": "wifi"}
    return cap


def _pacing_capture() -> Capture:
    return synth.laglab("pace", duration_s=60.0).cap


@pytest.mark.parametrize("make,t0,t1", [
    (lambda: synth.startup_then_smooth(startup_s=200.0, total_s=300.0), 150.0, 300.0),   # shader_compile, early
    (_pacing_capture, 20.0, 60.0),                                                     # the headline's pacing share
    (_wifi_capture, 60.0, 90.0),                                                       # no Wi-Fi scan in the part
])
def test_the_words_name_the_measured_part_once_the_capture_is_cut(make, t0, t1):
    whole, part = _shown(diagnose(make())), _shown(diagnose(crop(make(), t0, t1)))
    assert any("the measured part" in t for t in part)
    assert [t for t in part if _CAPTURE_WORDS.search(t)] == []
    assert not any("the measured part" in t for t in whole)


def test_a_wifi_channel_kept_through_the_part_is_said_of_the_part():
    """The recording changed channel before the part; the part stayed on one, and says so of the part, not of the
    whole recording the report draws."""
    cap = _wifi_capture()
    cap.network[N.WIFI_CHANNEL] = [6 if t < 30.0 else 36 for t in cap.network["t"]]
    assert "throughout the measured part" in diagnose(crop(cap, 40.0, 80.0)).cleared["wifi_roaming"]


def test_the_whole_recordings_words_still_name_the_capture():
    whole = _shown(diagnose(synth.startup_then_smooth(startup_s=60.0, total_s=180.0)))
    assert any("of the capture" in t for t in whole)


_WHOLE = re.compile(r"\b(?:in|of|during|over|through|throughout|across|for) the (?:\{\}\s*)?"
                    r"(?:capture|recording|session)\b", re.IGNORECASE)


def _texts(source: str) -> list[tuple[int, str]]:
    """Every string a module's source can show, docstrings left out: each plain constant, and each f-string whole
    with "{}" where a value goes, so "the {dur}capture" reads as one text."""
    tree = ast.parse(source)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    inside = {id(part) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for part in ast.walk(n) if part is not n}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) and id(n) not in inside:
            out.append((n.lineno, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and id(n) not in inside:
            out.append((n.lineno, n.value))
    return out


@pytest.mark.parametrize("module", ["lagdoctor/analysis/rules.py", "lagdoctor/analysis/diagnose.py"])
def test_no_text_the_analysis_shows_names_the_whole_capture(module):
    """A share or a time "of the capture" on a measured part would claim the whole recording: such text names
    {e.part}, which reads "the capture" until the capture is cut."""
    hits = [(line, text) for line, text in _texts((ROOT / module).read_text(encoding="utf-8"))
            if _WHOLE.search(text)]
    assert hits == []


@pytest.mark.parametrize("line", [
    "f'no Wi-Fi background scan happened during the {dur}capture (they come'",   # once two separate constants
    "'Possible shader-compilation stutter early in the session'",
    "f'stayed on Wi-Fi channel {ch} throughout the capture'",
    "f'{share:.0f}% Of The Capture'",
])
def test_the_scan_sees_each_way_a_text_can_name_the_whole_capture(line):
    assert [text for _, text in _texts(f"def f(e, dur, ch, share):\n    return {line}\n") if _WHOLE.search(text)]


def test_the_reports_ways_a_part_is_set_are_crops():
    assert set(labels.PART_HOW) == set(crop_mod.HOWS)


# --------------------------------------------------------------------------- the file ---

def test_a_cut_copy_is_never_saved(tmp_path):
    part = crop(rich_capture(), 40.0, 80.0)
    with pytest.raises(ValueError, match="never saved"):
        part.save(tmp_path / "part.ldcap.json.gz")
    assert list(tmp_path.iterdir()) == []
    assert "span" not in part.to_json()


def test_a_file_never_says_it_holds_a_part(tmp_path):
    """A capture file holding a span (made by hand) loads as the whole recording, the key named in its notes."""
    data = dict(rich_capture().to_json(), span={"from_s": 40.0, "to_s": 80.0, "recorded_s": 100.0, "how": "set"})
    p = tmp_path / "c.ldcap.json.gz"
    with gzip.open(p, "wb") as f:
        f.write(json.dumps(data).encode("utf-8"))
    cap = Capture.load(p)
    assert cap.span is None and any("span" in n for n in cap.notes)
    assert math.isclose(cap.duration_s, 100.0)
