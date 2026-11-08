"""The bridge's policy: which documents the app trusts, where its view may navigate, and what a message may ask.

The window's safety rests on these three answers (lagdoctor/app/bridge.py), so each has its negative cases here.
"""
from __future__ import annotations

import json

import pytest

from lagdoctor.app import bridge
from lagdoctor.app.main import App


@pytest.mark.parametrize("source", ["https://app.lagdoctor.example/index.html",
                                    "https://app.lagdoctor.example/",
                                    "https://APP.lagdoctor.example/index.html"])      # hostnames fold case
def test_the_apps_own_documents_pass_the_origin_check(source):
    assert bridge.origin_ok(source)


@pytest.mark.parametrize("source", [
    None, "", 42, "about:blank", "null",
    "http://app.lagdoctor.example/index.html",                  # not https
    "https://app.lagdoctor.example:443/index.html",             # any port, even the default
    "https://app.lagdoctor.example:8443/index.html",
    "https://user@app.lagdoctor.example/index.html",            # user info
    "https://user:pw@app.lagdoctor.example/index.html",
    "https://app.lagdoctor.example.evil.com/index.html",        # a host that only starts like it
    "https://evil.com/app.lagdoctor.example/index.html",
    "https://evilapp.lagdoctor.example/index.html",
    "https://lagdoctor.example/index.html",
    "file:///C:/Windows/win.ini", "data:text/html,hi", "javascript:alert(1)",
    "https://app.lagdoctor.example:bad/",                       # a port that is not a number
])
def test_anything_else_fails_the_origin_check(source):
    assert not bridge.origin_ok(source)


def test_only_the_first_navigation_and_only_to_the_entry_page_is_allowed():
    assert bridge.navigation_allowed(bridge.ENTRY, first=True)
    assert not bridge.navigation_allowed(bridge.ENTRY, first=False)              # a reload or a second page
    for uri in (bridge.APP_ORIGIN + "/other.html", bridge.ENTRY + "?x=1", bridge.ENTRY + "#top",
                bridge.APP_ORIGIN + "/", "https://example.com/index.html", "http://app.lagdoctor.example/index.html",
                bridge.APP_ORIGIN + "/vendor/../index.html", None, ""):
        assert not bridge.navigation_allowed(uri, first=True), uri


def msg(**fields) -> str:
    return json.dumps(fields)


def test_each_request_kind_parses_with_exactly_its_fields():
    sid = "20261104-194500-b_quest__b"
    cases = {"library": {}, "game": {"game": "g-0123456789abcdef"}, "results": {"session": sid},
             "moment": {"session": sid, "event": 7}, "compare": {"a": sid, "b": "20261101-201500-demoracer"},
             "settings": {}, "open_report": {"session": sid}, "open_folder": {}, "test_sound": {}}
    assert set(cases) == set(bridge.REQUESTS)
    for kind, args in cases.items():
        req = bridge.parse(msg(type="request", id=3, kind=kind, **args))
        assert req == bridge.Request(kind=kind, id=3, args=args), kind


@pytest.mark.parametrize("text", [
    None, 5, "", "not json", "[]", "null", '"library"',
    msg(type="request", id=1),                                             # no kind
    msg(type="request", kind="library"),                                   # no id
    msg(type="request", id=0, kind="library"),                             # id 0 is no request's
    msg(type="request", id=-1, kind="library"),
    msg(type="request", id=True, kind="library"),                          # a bool is no id
    msg(type="request", id=1.0, kind="library"),
    msg(type="request", id=2 ** 31, kind="library"),
    msg(type="request", id=1, kind="delete_everything"),                   # an unknown kind
    msg(type="request", id=1, kind="__init__"),
    msg(type="request", id=1, kind="library", extra=1),                    # an extra field
    msg(type="request", id=1, kind="results"),                             # a field missing
    msg(type="request", id=1, kind="results", session="..\\..\\Windows"),  # not a session's name
    msg(type="request", id=1, kind="results", session="20261104-194500-a/../../x"),
    msg(type="request", id=1, kind="results", session="20261104-194500-.."),
    msg(type="request", id=1, kind="results", session="C:\\Users\\x\\capture.ldcap.json.gz"),
    msg(type="request", id=1, kind="results", session=["20261104-194500-a"]),
    msg(type="request", id=1, kind="results", session="20261104-194500-" + "a" * 65),
    msg(type="request", id=1, kind="game", game="demoracer"),              # a name, not an id
    msg(type="request", id=1, kind="game", game="g-0123456789ABCDEF"),
    msg(type="request", id=1, kind="moment", session="20261104-194500-a", event=-1),
    msg(type="request", id=1, kind="moment", session="20261104-194500-a", event="1"),
    msg(type="frames_done"),                                               # a notice without its token
    msg(type="frames_done", token=1, extra=2),
    msg(type="ready", x=1),
    msg(type="reply", id=1, ok=True),                                      # the app's own kind of message
    msg(type="request", id=1, kind="library") + " " * bridge.MAX_MESSAGE_CHARS,     # too long to read
    "[" * 100_000,                                                         # nested past the parser's depth
])
def test_anything_else_is_refused(text):
    assert bridge.parse(text) is None


def test_the_two_notices_parse():
    assert bridge.parse(msg(type="ready")) == bridge.Request(kind="ready")
    assert bridge.parse(msg(type="frames_done", token=4)) == bridge.Request(kind="frames_done", args={"token": 4})


def test_every_request_kind_has_its_handler_and_every_handler_a_kind():
    # App.on_message answers a request with App.do_<kind>; a kind without one would only ever fail.
    handlers = {name[3:] for name in vars(App) if name.startswith("do_")}
    assert handlers == set(bridge.REQUESTS)


def test_a_reply_carries_either_its_data_or_its_error():
    assert bridge.reply(4, {"x": 1}) == {"type": "reply", "id": 4, "ok": True, "data": {"x": 1}}
    assert bridge.reply(4, error="gone") == {"type": "reply", "id": 4, "ok": False, "error": "gone"}
