"""The relay's messages (lagdoctor/relay/protocol.py): what each side accepts, how rows and events are packed, and that
the relay forwards every frame column the recorder reads. Pure: no pipe, no process."""
from __future__ import annotations

import json
import math

import pytest

from lagdoctor.capture import netflows as nf
from lagdoctor.capture import presentmon as pm
from lagdoctor.relay import protocol as rp

ROW = {"Application": "game.exe", "ProcessID": 4242, "SwapChainAddress": 0x2A70D2CAC00, "TimeInQPC": 1234567890123,
       "MsBetweenPresents": 16.667, "MsCPUBusy": 3.25, "MsCPUWait": 13.0, "MsGPULatency": 1.5, "MsGPUBusy": 6.0,
       "MsGPUWait": 0.0, "MsUntilDisplayed": None, "MsBetweenDisplayChange": 16.6, "MsAllInputToPhotonLatency": None,
       "MsAnimationError": 0.01, "PresentMode": "Hardware: Independent Flip", "PresentRuntime": "DXGI",
       "SyncInterval": 0, "AllowsTearing": 1, "FrameType": "Application", "HybridPresent": 0, "LayerIndex": 0}


class Reads(dict):
    """A row that remembers which columns were read."""

    def __init__(self, *a):
        super().__init__(*a)
        self.read: set[str] = set()

    def __getitem__(self, k):
        self.read.add(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.read.add(k)
        return super().get(k, default)

    def __contains__(self, k):
        self.read.add(k)
        return super().__contains__(k)


def test_the_relay_forwards_exactly_the_columns_the_recorder_reads():
    row = Reads(ROW)
    pm.frame_row(row, 0.0, "0x1")
    src = pm.PresentMonSource(4242)
    src._add(row)                                          # the pid filter and the chain key read their columns too
    assert row.read == set(rp.FRAME_COLUMNS), sorted(row.read ^ set(rp.FRAME_COLUMNS))


def test_a_row_through_the_protocol_is_the_row_the_recorder_would_have_parsed():
    cols, values = rp.row_values(ROW)
    assert "Application" not in cols and "LayerIndex" not in cols          # what the recorder never reads stays put
    (msg,), dropped = rp.rows_messages(cols, [values])
    got = rp.from_relay(msg)["rows"][0]
    assert dropped == 0 and got == {c: ROW[c] for c in rp.FRAME_COLUMNS}
    assert pm.frame_row(got, 5.0, "0x1") == pm.frame_row(ROW, 5.0, "0x1")
    # A column the header lacks stays absent, which frame_row tells apart from PresentMon's "NA" (None).
    no_display = {k: v for k, v in ROW.items() if k != "MsUntilDisplayed"}
    cols, values = rp.row_values(no_display)
    got = rp.from_relay(rp.rows_messages(cols, [values])[0][0])["rows"][0]
    assert "MsUntilDisplayed" not in got
    assert pm.frame_row(got, 0.0, "0x1")["displayed"] is None and pm.frame_row(ROW, 0.0, "0x1")["displayed"] == 0


def test_pack_keeps_every_item_once_in_order_and_under_the_cap():
    items = [[i, "x" * (i % 700)] for i in range(2000)]
    messages, dropped = rp.pack({"t": "rows", "cols": ["a"]}, "rows", items)
    assert dropped == 0 and len(messages) > 1 and all(len(m) <= rp.MAX_MESSAGE for m in messages)
    back = [item for m in messages for item in json.loads(m)["rows"]]
    assert back == items
    huge, dropped = rp.pack({"t": "rows", "cols": ["a"]}, "rows", [[1], ["y" * rp.MAX_MESSAGE], [2]])
    assert dropped == 1 and [i for m in huge for i in json.loads(m)["rows"]] == [[1], [2]]
    full = [["z" * 100]] * 2000
    messages, _ = rp.pack({"t": "rows", "cols": ["a"]}, "rows", full)
    assert max(len(m) for m in messages) > rp.MAX_MESSAGE - 200           # packed close to the cap, not one per item


def test_events_round_trip_byte_exact():
    data = bytes(range(28))
    (msg,), dropped = rp.events_messages([(123456789, 42, 0, data), (5, 11, 0, b"")])
    assert dropped == 0 and rp.from_relay(msg)["ev"] == [(123456789, 42, 0, data), (5, 11, 0, b"")]


@pytest.mark.parametrize("bad", [
    b"", b"not json", b"[1, 2]", b'{"t": 5}', '{"t":"start","origin":1.0}'.encode("utf-16"),
    b'{"t":"start"}', b'{"t":"start","origin":"1"}', b'{"t":"start","origin":true}', b'{"t":"start","origin":-1}',
    b'{"t":"start","origin":NaN}', b'{"t":"start","origin":Infinity}', b'{"t":"start","origin":1,"pid":4}',
    b'{"t":"stop","now":1}', b'{"t":"hello"}', b'{"t":"rows","cols":[],"rows":[]}', b'{"t":"STOP"}',
    b'{"t":"start","origin":1.0' + b" " * rp.MAX_MESSAGE + b"}"])
def test_the_relay_takes_only_start_and_stop_from_the_recorder(bad):
    with pytest.raises(rp.ProtocolError):
        rp.from_recorder(bad)


def test_start_and_stop_round_trip():
    assert rp.from_recorder(rp.start(12.5)) == {"t": "start", "origin": 12.5}
    assert rp.from_recorder(rp.stop()) == {"t": "stop"}


def _rows(cols, *rows):
    return json.dumps({"t": "rows", "cols": cols, "rows": [list(r) for r in rows]}).encode()


REQ = ["ProcessID", "SwapChainAddress", "TimeInQPC", "MsBetweenPresents"]


@pytest.mark.parametrize("bad", [
    _rows(REQ + ["Application"], (1, 2, 3, 4.0, "x")),              # a column the recorder does not read
    _rows(REQ[:3], (1, 2, 3)),                                       # a required one missing
    _rows(REQ + ["ProcessID"], (1, 2, 3, 4.0, 1)),                   # repeated
    _rows(REQ, (1, 2, 3)),                                           # a row shorter than its columns
    _rows(REQ, (0, 2, 3, 4.0)), _rows(REQ, (2 ** 32, 2, 3, 4.0)),    # a pid out of range
    _rows(REQ, (True, 2, 3, 4.0)), _rows(REQ, (1, -1, 3, 4.0)),      # bool as a number; a negative address
    _rows(REQ, (1, 2, 3, None)), _rows(REQ, (1, 2, 3, "16.6")),      # the frame time empty or text
    _rows(REQ + ["PresentMode"], (1, 2, 3, 4.0, "x" * 201)),         # an overlong text
    _rows(REQ + ["SyncInterval"], (1, 2, 3, 4.0, 1.5)),              # a fraction in a whole-number column
    json.dumps({"t": "events", "ev": [[1, 42, 0, "ABCD"]]}).encode(),          # upper-case hex
    json.dumps({"t": "events", "ev": [[1, 42, 0, "abc"]]}).encode(),           # odd length
    json.dumps({"t": "events", "ev": [[1, 42, 0, "00" * 129]]}).encode(),      # longer than any payload
    json.dumps({"t": "events", "ev": [[1, 70000, 0, ""]]}).encode(),           # an id out of range
    json.dumps({"t": "events", "ev": [[-1, 42, 0, ""]]}).encode(),             # a time before zero
    json.dumps({"t": "net", "started": True, "why": "", "health": {}, "totals": [[1, -5, 0]],
                "final": False}).encode(),
    json.dumps({"t": "net", "started": True, "why": "", "health": {},
                "totals": [[1, 1, 1]] * (rp.MAX_TOTALS + 1), "final": False}).encode(),
    json.dumps({"t": "net", "started": "yes", "why": "", "health": {}, "totals": [], "final": False}).encode(),
    json.dumps({"t": "net", "started": True, "why": "", "health": [], "totals": [], "final": False}).encode(),
    json.dumps({"t": "frames", "state": "great", "detail": "", "tool": "", "final": False, "info": None}).encode(),
    json.dumps({"t": "frames", "state": "ok", "detail": "", "tool": "", "final": 1, "info": None}).encode(),
    json.dumps({"t": "frames", "state": "ok", "detail": "", "tool": "", "final": True, "info": [1]}).encode(),
    json.dumps({"t": "hello", "v": 1, "pid": 5, "game": [5], "frames": True, "network": True}).encode(),
    json.dumps({"t": "end", "more": 1}).encode(), json.dumps({"t": "start", "origin": 1.0}).encode()])
def test_the_recorder_refuses_what_the_protocol_does_not_allow(bad):
    with pytest.raises(rp.ProtocolError):
        rp.from_relay(bad)


def test_relay_messages_round_trip():
    assert rp.from_relay(rp.hello(77, (80, 430), True, False)) == {
        "t": "hello", "v": rp.VERSION, "pid": 77, "game": (80, 430), "frames": True, "network": False}
    st = rp.from_relay(rp.frames_status("partial", "x" * (rp.MAX_TEXT + 50), "PresentMon 2.6.0", True,
                                        {"exit_code": 0, "stderr_tail": ["a", "b"]}))
    assert st["state"] == "partial" and len(st["detail"]) == rp.MAX_TEXT and st["detail"].endswith("...")
    assert st["final"] is True and st["info"] == {"exit_code": 0, "stderr_tail": ["a", "b"]}
    net = rp.from_relay(rp.net_status(None, "", {"events": 5, "relay": {"layout_mismatch": {"42": 1}}},
                                      [[9, 1, 2]] * (rp.MAX_TOTALS + 10)))
    assert net["started"] is None and len(net["totals"]) == rp.MAX_TOTALS and net["totals"][0] == (9, 1, 2)
    assert rp.from_relay(rp.end()) == {"t": "end"} and rp.from_relay(rp.error("why")) == {"t": "error", "why": "why"}


def test_a_status_that_does_not_fit_one_message_is_refused_at_the_relay():
    with pytest.raises(ValueError, match="over the"):
        rp.net_status(True, "", {"k": "v" * rp.MAX_MESSAGE}, [])


def test_pipe_names_are_unguessable_and_checked():
    names = {rp.pipe_name() for _ in range(50)}
    assert len(names) == 50 and all(rp.is_pipe_name(n) for n in names)
    for bad in ("lagdoctor-relay-", "lagdoctor-relay-" + "0" * 31, "lagdoctor-relay-" + "G" * 32,
                "x" + rp.pipe_name(), rp.pipe_name() + "\\..", None, 5):
        assert not rp.is_pipe_name(bad), bad


def test_every_kernel_network_payload_the_relay_forwards_fits_its_cap():
    assert max(nf.EXPECTED_LENGTH.values()) <= rp.MAX_EVENT_BYTES


def test_the_frame_column_types_are_what_the_parser_produces():
    for col, kind in rp.FRAME_COLUMNS.items():
        if col in pm.INT_COLS or col in ("ProcessID", "SwapChainAddress"):
            assert kind is int, col
        elif col in pm.STR_COLS:
            assert kind is str, col
        else:
            assert kind is float and col in pm.STRICT_FLOATS, col
    assert set(rp.FRAME_REQUIRED) == set(pm.REQUIRED) - {"Application"}
    assert rp.frame_value("MsGPUBusy", 7) == 7.0 and isinstance(rp.frame_value("MsGPUBusy", 7), float)
    assert math.isinf(rp.frame_value("MsGPUBusy", float("inf")))          # what the parser itself can produce
