"""A game that played offline (DESIGN.md, "A game that played offline"): when the network trace shows that the game
received nothing over the network, its ping spikes and lost probes are no lag of its own, and every network rule is
cleared for that one reason, which the reports list once."""
from __future__ import annotations

import copy

import pytest

from lagdoctor.analysis import crop, events, rules
from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.model import SourceStatus
from lagdoctor.report import html, labels
from tests import synth
from tests.test_analysis_rules import run

# Declared here, never read off the rules: a new network rule must be added on purpose, and one that loses its
# category fails the membership test below.
NETWORK_CAUSES = {"wifi_weak", "wifi_roaming", "wifi_scan_spikes", "bufferbloat", "isp_latency", "server_lag",
                  "packet_loss", "vpn_route", "bandwidth_hog"}
HEALTH = {"lost": 0, "buffers_lost": 0, "consumer_stopped_t": None, "early_dropped": 0, "unknown_endpoints": 0,
          "wire_unplaced": 0, "game_unrelayed_rx": 0, "pid_errors": 0, "relay": {"events_lost": 0, "rows_lost": 0}}
NOTHING = "received nothing over the network"


def offline(cap, received=0, sent=6500, **health):
    """`cap` as a whole trace leaves a game that received `received` bytes over the network and sent `sent`."""
    cap.sources["net_flows"] = SourceStatus("ok", "Kernel-Network flows; address layout verified by the self-test")
    cap.net_flows.update(layout="verified", game_wire_bytes=[received, sent],
                         health=dict(copy.deepcopy(HEALTH), **health))
    return cap


def noisy(duration_s=120.0):
    """A LagLab recording whose internet ping spiked and lost four rounds in a row: network lag, if it is online."""
    return synth.laglab("smooth", duration_s=duration_s, net_spikes=[(30.0, 32.0, 80.0)],
                        net_losses=[60.2, 60.7, 61.2, 61.7]).cap


def test_a_game_that_received_nothing_meets_no_network_lag_and_every_network_rule_is_cleared_for_that():
    online = diagnose(offline(noisy(), received=1))           # the control: one byte received
    assert {"net_spike", "net_loss"} <= {e.kind for e in online.events} and online.lag_type == "network"
    d = diagnose(offline(noisy()))
    assert not [e for e in d.events if e.kind.startswith("net_")]
    assert d.lag_type != "network" and "network" not in d.headline.lower()
    assert all(NOTHING in d.cleared.get(c, "") for c in NETWORK_CAUSES), d.cleared
    assert len({d.cleared[c] for c in NETWORK_CAUSES}) == 1                  # one reason, said once in the reports
    assert not NETWORK_CAUSES & set(d.unchecked) and not [f for f in d.findings if f.category == "network"]
    # Nothing else moves: the other findings and causes are the online game's own
    assert [f.title for f in d.findings] == [f.title for f in online.findings if f.category != "network"]
    assert {c: w for c, w in d.cleared.items() if c not in NETWORK_CAUSES} == \
        {c: w for c, w in online.cleared.items() if c not in NETWORK_CAUSES}


@pytest.mark.parametrize("label, change", [
    ("the trace read partial", lambda c: c.sources.update(net_flows=SourceStatus("partial", "ETW dropped 3 events"))),
    ("the trace did not run", lambda c: c.sources.update(net_flows=SourceStatus("unavailable", "needs ETW rights"))),
    ("its address layout was not proven", lambda c: c.net_flows.update(layout="unverified")),
    ("the game's process was not known", lambda c: c.target.update(pid=None)),
    ("a process number that is True", lambda c: c.target.update(pid=True)),
    ("a process number of 0", lambda c: c.target.update(pid=0)),
    ("one byte received", lambda c: c.net_flows["game_wire_bytes"].__setitem__(0, 1)),
    ("a received count of 0.0", lambda c: c.net_flows["game_wire_bytes"].__setitem__(0, 0.0)),
    ("a received count of False", lambda c: c.net_flows["game_wire_bytes"].__setitem__(0, False)),
    ("a received count of None", lambda c: c.net_flows["game_wire_bytes"].__setitem__(0, None)),
    ("no counts, as in an older capture", lambda c: c.net_flows.pop("game_wire_bytes")),
    ("three counts", lambda c: c.net_flows["game_wire_bytes"].append(0)),
    ("ETW lost events", lambda c: c.net_flows["health"].update(lost=2)),
    ("ETW's losses not read", lambda c: c.net_flows["health"].update(lost=None)),
    ("ETW's lost events not reported", lambda c: c.net_flows["health"].pop("lost")),
    ("ETW's lost buffers not reported", lambda c: c.net_flows["health"].pop("buffers_lost")),
    ("ETW lost buffers", lambda c: c.net_flows["health"].update(buffers_lost=1)),
    ("the consumer stopped by itself", lambda c: c.net_flows["health"].update(consumer_stopped_t=40.0)),
    ("events from before the trace were dropped", lambda c: c.net_flows["health"].update(early_dropped=5)),
    ("a game packet whose ends could not be told", lambda c: c.net_flows["health"].update(unknown_endpoints=1)),
    ("such packets not counted", lambda c: c.net_flows["health"].pop("unknown_endpoints")),
    ("a TCP packet of the game's that could not be placed", lambda c: c.net_flows["health"].update(wire_unplaced=1)),
    ("a capture from before those were counted", lambda c: c.net_flows["health"].pop("wire_unplaced")),
    ("a child the relay did not pass on received bytes", lambda c: c.net_flows["health"].update(game_unrelayed_rx=700)),
    ("the relay's totals for the game not given", lambda c: c.net_flows["health"].pop("game_unrelayed_rx")),
    ("the relay's totals may have been cut", lambda c: c.net_flows["health"].update(game_unrelayed_rx=None)),
    ("the game's process tree could not be read", lambda c: c.net_flows["health"].update(pid_errors=1)),
    ("the relay lost events", lambda c: c.net_flows["health"]["relay"].update(events_lost=1)),
    ("a relay record that is none", lambda c: c.net_flows["health"].update(relay=None)),
])
def test_offline_takes_a_whole_trace_that_saw_the_game_receive_nothing(label, change):
    cap = offline(synth.laglab("smooth", duration_s=30.0).cap)
    assert events.game_offline(cap)                     # the control: the bytes it sent do not count
    change(cap)
    assert not events.game_offline(cap), label


def test_a_trace_of_its_own_with_no_relay_still_tells_an_offline_game():
    cap = offline(synth.laglab("smooth", duration_s=30.0).cap)
    cap.net_flows["health"].pop("relay")
    cap.net_flows["health"].pop("early_dropped")
    assert events.game_offline(cap)


def test_the_network_rules_are_the_ones_an_offline_game_clears():
    assert {r.cause for r in rules.RULES if r.category == "network"} == NETWORK_CAUSES


def test_each_network_rule_is_cleared_before_its_sources_are_asked_for_and_no_other_rule_is():
    cap = offline(synth.laglab("smooth", duration_s=30.0).cap)
    cap.sources.pop("network")                          # no pings at all: still no network lag for this game
    for cause in sorted(NETWORK_CAUSES):
        res = run(cause, cap)
        assert isinstance(res, rules.Cleared) and NOTHING + " during the capture" in res.text, (cause, res)
    for r in rules.RULES:
        if r.cause not in NETWORK_CAUSES:
            assert NOTHING not in getattr(run(r.cause, cap), "text", ""), r.cause
    cap.net_flows["game_wire_bytes"][0] = 1                                 # the control: it received a byte
    for cause in sorted(NETWORK_CAUSES):
        assert NOTHING not in getattr(run(cause, cap), "text", ""), cause


@pytest.mark.parametrize("wire, part", [
    ([0, 6500], [0, None]), ([0, 0], [0, 0]), ([7, 0], [None, 0]), ([7, 9], None), ([False, 0], [None, 0]),
    ([0.0, 3], None), (None, None)])
def test_a_measured_part_keeps_only_the_whole_recordings_counts_of_zero(wire, part):
    assert crop._zeros(wire) == part


def test_an_offline_recording_cut_to_a_part_stays_offline_and_says_the_part():
    part = crop.crop(offline(noisy()), 20.0, 80.0)
    assert part.net_flows["game_wire_bytes"] == [0, None] and events.game_offline(part)
    d = diagnose(part)
    assert not [e for e in d.events if e.kind.startswith("net_")]
    assert all(NOTHING + " during the measured part" in d.cleared[c] for c in NETWORK_CAUSES), d.cleared
    online = crop.crop(offline(noisy(), received=1), 20.0, 80.0)        # the control
    assert online.net_flows["game_wire_bytes"] is None and not events.game_offline(online)


def test_causes_ruled_out_for_one_reason_share_a_line_and_the_lines_go_in_order():
    cleared = {"power_plan": "a plan of its own", "bufferbloat": "one reason", "packet_loss": "one reason",
               "game_dvr": ["not text"], "refresh_rate": 5}       # two reasons that are not text: a line each
    t = labels.topic_text
    # The preconditions that make order a test: the shared pair and the lines are not already given in order
    assert [t("bufferbloat"), t("packet_loss")] != sorted([t("bufferbloat"), t("packet_loss")], key=str.casefold)
    pair = ", ".join(sorted([t("bufferbloat"), t("packet_loss")], key=str.casefold))
    want = sorted([(t("power_plan"), "a plan of its own"), (pair, "one reason"), (t("game_dvr"), ["not text"]),
                   (t("refresh_rate"), 5)], key=lambda g: g[0].casefold())
    assert [g[0] for g in want] != [t("power_plan"), pair, t("game_dvr"), t("refresh_rate")]
    assert labels.group_cleared(cleared) == want


def test_the_report_says_an_offline_games_reason_once_for_all_its_network_rules():
    cap = offline(noisy())
    page = html.render(diagnose(cap), cap)
    assert page.count(NOTHING) == 1
    online = offline(noisy(), received=1)
    assert NOTHING not in html.render(diagnose(online), online)
