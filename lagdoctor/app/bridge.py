"""The bridge: the only way the page reaches Python, and its policy as pure functions.

The window trusts a web message only from lag-doctor's own page (origin_ok), lets the view navigate only once, to that
page (navigation_allowed), and turns each message into a Request with every field checked (parse) before anything
acts on it. The page names sessions and games only by the ids the app gave it; library.py resolves an id against its
own listing, never by joining it to a path. tests/test_app_bridge.py holds each of these, with negative cases.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

APP_HOST = "app.lagdoctor.example"          # RFC 6761 name, one host for the bundled files (research note, section 3)
APP_ORIGIN = "https://" + APP_HOST
ENTRY = APP_ORIGIN + "/index.html"
MAX_MESSAGE_CHARS = 16 * 1024              # a request is a few dozen characters; anything longer is refused unread
MAX_ID = 2 ** 31 - 1

# What the page may ask for, and the fields each request carries besides its id: {kind: {field: checker}}.
SESSION_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[\w.-]{1,64}")      # a session folder's name (capture.session.session_dir)
GAME_ID = re.compile(r"g-[0-9a-f]{16}")


def _session(v: Any) -> bool:
    return isinstance(v, str) and SESSION_ID.fullmatch(v) is not None and ".." not in v


def _game(v: Any) -> bool:
    return isinstance(v, str) and GAME_ID.fullmatch(v) is not None


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_ID


REQUESTS = {
    "library": {},
    "game": {"game": _game},
    "results": {"session": _session},
    "moment": {"session": _session, "event": _count},
    "compare": {"a": _session, "b": _session},
    "settings": {},
    "open_report": {"session": _session},
    "open_folder": {},
    "test_sound": {},
}
# Messages that are not requests: the page is ready, or it let go of a shared buffer the app sent it.
NOTICES = {"ready": {}, "frames_done": {"token": _count}}


@dataclass(frozen=True)
class Request:
    kind: str
    id: int = 0
    args: dict[str, Any] = field(default_factory=dict)


def origin_ok(source: Any) -> bool:
    """True only for a document of lag-doctor's own host: https, that exact host, no port, no user info."""
    if not isinstance(source, str) or not source:
        return False
    try:
        parts = urllib.parse.urlsplit(source)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == APP_HOST and port is None
            and parts.username is None and parts.password is None)


def navigation_allowed(uri: Any, first: bool) -> bool:
    """Only the first navigation of a view, and only to the entry page; everything later is refused."""
    if not first or not origin_ok(uri):
        return False
    parts = urllib.parse.urlsplit(uri)
    return parts.path == "/index.html" and not parts.query and not parts.fragment


def parse(text: Any) -> Request | None:
    """The Request (or notice) a web message's JSON holds, or None for anything else: too long, not JSON, an
    unknown type or kind, a field missing, an extra field, or a field of the wrong shape."""
    if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS:
        return None
    try:
        msg = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "request":
        name = msg.get("kind")
        spec = REQUESTS.get(name) if isinstance(name, str) else None
        if spec is None or not _count(msg.get("id")) or msg.get("id") == 0:
            return None
        extra = set(msg) - {"type", "kind", "id"} - set(spec)
    elif isinstance(kind, str) and kind in NOTICES:
        name, spec = kind, NOTICES[kind]
        extra = set(msg) - {"type"} - set(spec)
    else:
        return None
    if extra or any(not check(msg.get(key)) for key, check in spec.items()):
        return None
    return Request(kind=name, id=msg.get("id", 0) if kind == "request" else 0,
                   args={key: msg[key] for key in spec})


def reply(request_id: int, data: Any = None, error: str | None = None) -> dict[str, Any]:
    """The answer to a request: its data, or a plain sentence saying why there is none."""
    if error is not None:
        return {"type": "reply", "id": request_id, "ok": False, "error": error}
    return {"type": "reply", "id": request_id, "ok": True, "data": data}
