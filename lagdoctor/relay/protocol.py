"""The relay's messages: one JSON object per pipe message, each checked where it arrives.

The relay runs with administrator rights and the recorder in the player's own account, so each side checks what the
other sends: the relay accepts exactly two messages from the recorder (`start`, with the capture clock's origin, and
`stop`) and never takes a PresentMon or ETW argument from the pipe; the recorder checks every field's type and range
before a value reaches the capture, so a broken message ends the link with a reason instead of a traceback or a
wrong number. Every message fits one pipe message of at most MAX_MESSAGE bytes: rows and events are packed into as
many messages as they need (`pack`).

From the relay:
  hello   {v, pid, game: [pid, create_time], frames: bool, network: bool}
  rows    {cols: [column ...], rows: [[value ...] ...]}     the game's frame rows, FRAME_COLUMNS present in them
  events  {ev: [[qpc, id, version, hex payload] ...]}         the game's and the recorder's Kernel-Network events
  frames  {state, detail, tool, final: bool, info: {...}|null}   PresentMon's status; final once its run is over
  net     {started: bool|null, why: str, health: {...}, totals: [[pid, rx, tx] ...], final: bool}
  end     {}                                                   nothing more will come
  error   {why}                                                the relay stops: why, in plain words
From the recorder:
  start   {origin: float}      the capture clock's origin (QPC seconds): start PresentMon and the ETW session
  stop    {}                   stop them, send what is left, then `end`
"""
from __future__ import annotations

import json
import math
import re
import secrets
from typing import Any, Iterable

VERSION = 1
MAX_MESSAGE = 65536                     # bytes per pipe message, both ways (the pipe's own cap)
PIPE_PREFIX = "lagdoctor-relay-"
_PIPE_NAME = re.compile(re.escape(PIPE_PREFIX) + r"[0-9a-f]{32}")
MAX_PID = 0xFFFFFFFF
MAX_QPC = (1 << 63) - 1
MAX_TEXT = 4000                         # a status detail or reason
MAX_SHORT = 200                         # a tool name, a frame column's text
MAX_EVENT_BYTES = 128                   # the largest Kernel-Network payload decoded is 68 bytes (NE 4.3)
MAX_TOTALS = 64                         # per-program byte totals per message, the largest first
MAX_PLAIN_ITEMS = 1000                  # a list or object inside info or health
MAX_PLAIN_DEPTH = 4
STATES = ("ok", "partial", "failed", "unavailable", "off")

# Exit codes of the relay process: what the recorder reads when the relay ended before the pipe could say why.
EXIT_OK = 0
EXIT_CRASH = 1             # anything the relay did not expect (pythonw shows no traceback anywhere)
EXIT_BAD_ARGS = 2          # a command line lag-doctor does not build
EXIT_TARGET = 3            # the game or the recorder is not running as approved, or runs in another Windows session
EXIT_PIPE = 4              # its pipe could not be made: the name was taken, or Windows refused its owner
EXIT_NO_CLIENT = 5         # the recorder did not connect in time
EXIT_NOT_READY = 6         # PresentMon is missing or failed its check (watch's own EXIT_NOT_READY is 6 too)
EXIT_PROTOCOL = 7          # the recorder sent something the protocol does not allow

# The PresentMon columns the recorder reads (presentmon.frame_row and PresentMonSource._add), with the type each
# holds after presentmon.convert(): None stands for PresentMon's "NA" in every column but the required ones.
FRAME_COLUMNS: dict[str, type] = {
    "ProcessID": int, "SwapChainAddress": int, "TimeInQPC": int, "MsBetweenPresents": float,
    "MsCPUBusy": float, "MsCPUWait": float, "MsGPULatency": float, "MsGPUBusy": float, "MsGPUWait": float,
    "MsUntilDisplayed": float, "MsBetweenDisplayChange": float, "MsAllInputToPhotonLatency": float,
    "MsAnimationError": float, "PresentMode": str, "PresentRuntime": str, "SyncInterval": int,
    "AllowsTearing": int, "FrameType": str, "HybridPresent": int,
}
FRAME_REQUIRED = ("ProcessID", "SwapChainAddress", "TimeInQPC", "MsBetweenPresents")


class ProtocolError(ValueError):
    """A message the protocol does not allow; its text says what, in plain words."""


def pipe_name() -> str:
    """A fresh, unguessable pipe name for one relay."""
    return PIPE_PREFIX + secrets.token_hex(16)


def is_pipe_name(name: object) -> bool:
    return isinstance(name, str) and _PIPE_NAME.fullmatch(name) is not None


# ------------------------------------------------------------------------------------------- encoding ---

def encode(message: dict[str, Any]) -> bytes:
    """One message as the bytes of one pipe message; ValueError when it would not fit."""
    data = json.dumps(message, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_MESSAGE:
        raise ValueError(f"a {message.get('t')!r} message of {len(data)} bytes is over the {MAX_MESSAGE}-byte cap")
    return data


def pack(head: dict[str, Any], key: str, items: Iterable[Any]) -> tuple[list[bytes], int]:
    """`head` plus `key: [items]`, split into as few messages as fit MAX_MESSAGE each, in order; and how many items
    were left out because one alone would not fit."""
    prefix = json.dumps({**head, key: []}, separators=(",", ":"))
    if not prefix.endswith("[]}"):
        raise ValueError("the packed key must come last")
    prefix, suffix = prefix[:-2], "]}"
    room = MAX_MESSAGE - len(prefix) - len(suffix)
    out: list[bytes] = []
    chunk: list[str] = []
    used = 0
    dropped = 0
    for item in items:
        text = json.dumps(item, separators=(",", ":"))
        if len(text) > room:
            dropped += 1
            continue
        if chunk and used + 1 + len(text) > room:
            out.append((prefix + ",".join(chunk) + suffix).encode("ascii"))
            chunk, used = [], 0
        used += len(text) + (1 if chunk else 0)
        chunk.append(text)
    if chunk:
        out.append((prefix + ",".join(chunk) + suffix).encode("ascii"))
    return out, dropped


def hello(pid: int, game: tuple[int, int], frames: bool, network: bool) -> bytes:
    return encode({"t": "hello", "v": VERSION, "pid": pid, "game": list(game), "frames": frames, "network": network})


def row_values(row: dict[str, Any]) -> tuple[tuple[str, ...], list[Any]]:
    """A parsed PresentMon row as (its FRAME_COLUMNS, their values in that order). Columns the row's header lacks
    stay absent, so the recorder can tell an absent column from PresentMon's "NA" (frame_row does)."""
    cols = tuple(c for c in FRAME_COLUMNS if c in row)
    return cols, [row[c] for c in cols]


def rows_messages(cols: tuple[str, ...], rows: Iterable[list[Any]]) -> tuple[list[bytes], int]:
    return pack({"t": "rows", "cols": list(cols)}, "rows", rows)


def events_messages(events: Iterable[tuple[int, int, int, bytes]]) -> tuple[list[bytes], int]:
    return pack({"t": "events"}, "ev", ([ts, eid, ver, data.hex()] for ts, eid, ver, data in events))


def frames_status(state: str, detail: str, tool: str, final: bool = False,
                  info: dict[str, Any] | None = None) -> bytes:
    return encode({"t": "frames", "state": state, "detail": _cut(detail, MAX_TEXT), "tool": _cut(tool, MAX_SHORT),
                   "final": final, "info": info})


def net_status(started: bool | None, why: str, health: dict[str, Any], totals: list[list[int]],
               final: bool = False) -> bytes:
    return encode({"t": "net", "started": started, "why": _cut(why, MAX_TEXT), "health": health,
                   "totals": totals[:MAX_TOTALS], "final": final})


def end() -> bytes:
    return encode({"t": "end"})


def error(why: str) -> bytes:
    return encode({"t": "error", "why": _cut(why, MAX_TEXT)})


def start(origin: float) -> bytes:
    return encode({"t": "start", "origin": float(origin)})


def stop() -> bytes:
    return encode({"t": "stop"})


def _cut(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ------------------------------------------------------------------------------------------- decoding ---

def _load(data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_MESSAGE:
        raise ProtocolError("a message that is empty or over the size cap")
    try:
        msg = json.loads(bytes(data).decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError("a message that is not JSON") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("a message without a type")
    return msg


def _keys(msg: dict[str, Any], *keys: str) -> None:
    if set(msg) != {"t", *keys}:
        raise ProtocolError(f"a {msg['t']!r} message with other fields than {', '.join(keys) or 'none'}")


def _int(value: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ProtocolError(f"{what} is not a whole number from {lo} to {hi}")
    return value


def _bool(value: Any, what: str, none: bool = False) -> bool | None:
    if value is None and none:
        return None
    if not isinstance(value, bool):
        raise ProtocolError(f"{what} is not true or false")
    return value


def _text(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ProtocolError(f"{what} is not text of at most {limit} characters")
    return value


def _plain(value: Any, what: str, depth: int = 0) -> Any:
    """JSON data the relay reports about itself (PresentMon's info, the ETW session's health): kept as it is, within
    bounds, and only what JSON can hold."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, MAX_TEXT, what)
    if depth >= MAX_PLAIN_DEPTH:
        raise ProtocolError(f"{what} is nested too deep")
    if isinstance(value, list):
        if len(value) > MAX_PLAIN_ITEMS:
            raise ProtocolError(f"{what} holds too many items")
        return [_plain(v, what, depth + 1) for v in value]
    if isinstance(value, dict):
        if len(value) > MAX_PLAIN_ITEMS or not all(isinstance(k, str) and len(k) <= MAX_SHORT for k in value):
            raise ProtocolError(f"{what} has too many or odd keys")
        return {k: _plain(v, what, depth + 1) for k, v in value.items()}
    raise ProtocolError(f"{what} holds a value JSON cannot")


def frame_value(column: str, value: Any) -> Any:
    """One forwarded frame value checked against its column's type; ProtocolError otherwise."""
    kind = FRAME_COLUMNS[column]
    if value is None:
        if column in FRAME_REQUIRED:
            raise ProtocolError(f"the frame column {column} is empty")
        return None
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(f"the frame column {column} is not a number")
        value = float(value)                    # a huge whole number overflows: from_relay says so
        if not math.isfinite(value):
            raise ProtocolError(f"the frame column {column} is not a finite number")
        return value
    if kind is int:
        hi = MAX_PID if column == "ProcessID" else MAX_QPC if column == "TimeInQPC" else (1 << 64) - 1
        lo = 1 if column == "ProcessID" else 0 if column == "SwapChainAddress" else -(1 << 63)
        return _int(value, lo, hi, f"the frame column {column}")
    return _text(value, MAX_SHORT, f"the frame column {column}")


def _rows(msg: dict[str, Any]) -> dict[str, Any]:
    _keys(msg, "cols", "rows")
    cols = msg["cols"]
    if (not isinstance(cols, list) or not all(isinstance(c, str) and c in FRAME_COLUMNS for c in cols)
            or len(set(cols)) != len(cols) or not set(FRAME_REQUIRED) <= set(cols)):
        raise ProtocolError("rows with columns the recorder does not know, repeated, or lacking a required one")
    rows = msg["rows"]
    if not isinstance(rows, list):
        raise ProtocolError("rows that are not a list")
    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(cols):
            raise ProtocolError("a frame row whose length differs from its columns")
        out.append({c: frame_value(c, v) for c, v in zip(cols, row)})
    return {"t": "rows", "rows": out}


def _events(msg: dict[str, Any]) -> dict[str, Any]:
    _keys(msg, "ev")
    if not isinstance(msg["ev"], list):
        raise ProtocolError("events that are not a list")
    out = []
    for ev in msg["ev"]:
        if not isinstance(ev, list) or len(ev) != 4:
            raise ProtocolError("an event that is not [time, id, version, payload]")
        ts, eid, ver, payload = ev
        if not isinstance(payload, str) or len(payload) > 2 * MAX_EVENT_BYTES or not re.fullmatch(r"(?:[0-9a-f]{2})*",
                                                                                                     payload):
            raise ProtocolError("an event payload that is not short lower-case hex")
        out.append((_int(ts, 0, MAX_QPC, "an event's time"), _int(eid, 0, 0xFFFF, "an event id"),
                    _int(ver, 0, 0xFF, "an event version"), bytes.fromhex(payload)))
    return {"t": "events", "ev": out}


def _totals(value: Any) -> list[tuple[int, int, int]]:
    if not isinstance(value, list) or len(value) > MAX_TOTALS:
        raise ProtocolError("per-program totals that are not a short list")
    out = []
    for item in value:
        if not isinstance(item, list) or len(item) != 3:
            raise ProtocolError("a per-program total that is not [pid, received, sent]")
        out.append((_int(item[0], 1, MAX_PID, "a program's pid"), _int(item[1], 0, MAX_QPC, "bytes received"),
                    _int(item[2], 0, MAX_QPC, "bytes sent")))
    return out


def _readable(check):
    """A checker whose every failure is a ProtocolError: a value Python cannot convert (a 400-digit number overflows
    float) must end the link with a reason, never kill the thread that reads it."""
    def checked(data: bytes) -> dict[str, Any]:
        try:
            return check(data)
        except ProtocolError:
            raise
        except (ValueError, OverflowError, TypeError, RecursionError) as exc:
            raise ProtocolError(f"a message that cannot be read ({type(exc).__name__})") from None
    checked.__doc__, checked.__name__ = check.__doc__, check.__name__
    return checked


@_readable
def from_relay(data: bytes) -> dict[str, Any]:
    """A message from the relay, checked; ProtocolError when it is not one the protocol allows."""
    msg = _load(data)
    t = msg["t"]
    if t == "hello":
        _keys(msg, "v", "pid", "game", "frames", "network")
        game = msg["game"]
        if not isinstance(game, list) or len(game) != 2:
            raise ProtocolError("hello names no game")
        return {"t": t, "v": _int(msg["v"], 0, 1 << 16, "the version"), "pid": _int(msg["pid"], 1, MAX_PID, "its pid"),
                "game": (_int(game[0], 1, MAX_PID, "the game's pid"), _int(game[1], 0, MAX_QPC, "its create time")),
                "frames": _bool(msg["frames"], "frames"), "network": _bool(msg["network"], "network")}
    if t == "rows":
        return _rows(msg)
    if t == "events":
        return _events(msg)
    if t == "frames":
        _keys(msg, "state", "detail", "tool", "final", "info")
        if msg["state"] not in STATES:
            raise ProtocolError("a frame timing state the recorder does not know")
        info = _plain(msg["info"], "PresentMon's details")
        if info is not None and not isinstance(info, dict):
            raise ProtocolError("PresentMon's details are not an object")
        return {"t": t, "state": msg["state"], "detail": _text(msg["detail"], MAX_TEXT, "the detail"),
                "tool": _text(msg["tool"], MAX_SHORT, "the tool"), "final": _bool(msg["final"], "final"), "info": info}
    if t == "net":
        _keys(msg, "started", "why", "health", "totals", "final")
        health = _plain(msg["health"], "the ETW session's health")
        if not isinstance(health, dict):
            raise ProtocolError("the ETW session's health is not an object")
        return {"t": t, "started": _bool(msg["started"], "started", none=True),
                "why": _text(msg["why"], MAX_TEXT, "the reason"), "health": health, "totals": _totals(msg["totals"]),
                "final": _bool(msg["final"], "final")}
    if t == "end":
        _keys(msg)
        return {"t": t}
    if t == "error":
        _keys(msg, "why")
        return {"t": t, "why": _text(msg["why"], MAX_TEXT, "the reason")}
    raise ProtocolError(f"a message of a type the recorder does not know ({_cut(t, 40)!r})")


@_readable
def from_recorder(data: bytes) -> dict[str, Any]:
    """A message from the recorder, checked: only `start` with a finite origin, or `stop`."""
    msg = _load(data)
    t = msg["t"]
    if t == "start":
        _keys(msg, "origin")
        origin = msg["origin"]
        if isinstance(origin, bool) or not isinstance(origin, (int, float)) or not math.isfinite(origin) or origin < 0:
            raise ProtocolError("a start without a clock origin")
        return {"t": t, "origin": float(origin)}
    if t == "stop":
        _keys(msg)
        return {"t": t}
    raise ProtocolError("a message the relay does not take")
