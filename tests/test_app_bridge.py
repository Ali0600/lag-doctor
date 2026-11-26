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


def test_a_cover_is_asked_for_by_its_games_id_on_the_art_host_alone():
    gid = "g-0123456789abcdef"
    assert bridge.art_url(gid) == "https://art.lagdoctor.example/g-0123456789abcdef"
    assert bridge.art_id(bridge.art_url(gid)) == gid
    with pytest.raises(ValueError):
        bridge.art_url("../sessions")
    assert bridge.ART_FILTER == bridge.ART_ORIGIN + "/*" and bridge.ART_HOST != bridge.APP_HOST


@pytest.mark.parametrize("uri", [
    "https://art.lagdoctor.example/g-0123456789ABCDEF",          # an id is lower-case hex
    "https://art.lagdoctor.example/g-0123456789abcde",
    "https://art.lagdoctor.example/g-0123456789abcdef/",
    "https://art.lagdoctor.example/g-0123456789abcdef?x=1", "https://art.lagdoctor.example/g-0123456789abcdef#top",
    "https://art.lagdoctor.example:443/g-0123456789abcdef", "https://user@art.lagdoctor.example/g-0123456789abcdef",
    "http://art.lagdoctor.example/g-0123456789abcdef", "https://ART.lagdoctor.example/g-0123456789abcdef",
    "https://app.lagdoctor.example/g-0123456789abcdef", "https://art.lagdoctor.example.evil/g-0123456789abcdef",
    "https://art.lagdoctor.example/sessions/g-0123456789abcdef", "https://art.lagdoctor.example/",
    "https://art.lagdoctor.example/..%2Fg-0123456789abcdef", "https://art.lagdoctor.example:bad/x",
    "https://art.lagdoctor.example/g-0123456789abcdef" + "0" * 200, None, 5, "",
])
def test_anything_else_names_no_cover(uri):
    assert bridge.art_id(uri) is None


def msg(**fields) -> str:
    return json.dumps(fields)


def test_each_request_kind_parses_with_exactly_its_fields():
    sid = "20261104-194500-b_quest__b"
    cases = {"library": {}, "game": {"game": "g-0123456789abcdef"}, "results": {"session": sid},
             "moment": {"session": sid, "event": 7}, "compare": {"a": sid, "b": "20261101-201500-demoracer"},
             "settings": {}, "open_report": {"session": sid}, "open_folder": {}, "test_sound": {}, "record": {},
             "play": {"game": "g-0123456789abcdef"}, "wait": {"game": "g-0123456789abcdef"}, "pick_game": {},
             "add_game": {"draft": "d-0123456789abcdef", "name": "Moss Garden"},
             "remove_game": {"game": "g-0123456789abcdef"},
             "rename_game": {"game": "g-0123456789abcdef", "name": "Moss Garden"},
             "delete_recordings": {"game": "g-0123456789abcdef", "sessions": [sid, "20261101-201500-demoracer"]},
             "stop_recording": {}, "cancel_recording": {}, "recording": {},
             "plan_fix": {"session": sid, "finding": 0, "fix": 1}, "apply_fix": {"plan": "p-0123456789abcdef"},
             "changes": {}, "undo_change": {"entry": "0123456789ab", "force": False},
             "undo_all": {"entries": ["0123456789ab", "ba9876543210"]}}
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
    msg(type="request", id=1, kind="results", session="20261104-194500-a."),        # Windows drops the last dot
    msg(type="request", id=1, kind="game", game="demoracer"),              # a name, not an id
    msg(type="request", id=1, kind="game", game="g-0123456789ABCDEF"),
    msg(type="request", id=1, kind="moment", session="20261104-194500-a", event=-1),
    msg(type="request", id=1, kind="moment", session="20261104-194500-a", event="1"),
    msg(type="request", id=1, kind="rename_game", game="g-0123456789abcdef"),                # no name
    msg(type="request", id=1, kind="rename_game", game="g-0123456789abcdef", name=None),
    msg(type="request", id=1, kind="rename_game", game="g-0123456789abcdef", name="x" * (bridge.MAX_NAME_CHARS + 1)),
    msg(type="request", id=1, kind="rename_game", game="D:\\Games\\moss.exe", name="Moss"),
    msg(type="request", id=1, kind="delete_recordings", game="g-0123456789abcdef", sessions=[]),   # none
    msg(type="request", id=1, kind="delete_recordings", game="g-0123456789abcdef",
        sessions=["20261104-194500-a", "20261104-194500-a"]),                                 # one twice
    msg(type="request", id=1, kind="delete_recordings", game="g-0123456789abcdef",
        sessions=[f"20261104-194500-a{i}" for i in range(bridge.MAX_DELETE + 1)]),            # past the cap
    msg(type="request", id=1, kind="delete_recordings", game="g-0123456789abcdef", sessions="20261104-194500-a"),
    msg(type="request", id=1, kind="delete_recordings", game="g-0123456789abcdef", sessions=["..\\..\\Windows"]),
    msg(type="request", id=1, kind="delete_recordings", game="g-0123456789abcdef",
        sessions=["20261104-194500-a", "C:\\lag-doctor\\sessions\\20261104-194500-a"]),
    msg(type="request", id=1, kind="delete_recordings", sessions=["20261104-194500-a"]),      # no game
    msg(type="request", id=1, kind="delete_recordings", game="demoracer", sessions=["20261104-194500-a"]),
    msg(type="request", id=1, kind="plan_fix", session="20261104-194500-a", finding=0),          # no fix
    msg(type="request", id=1, kind="plan_fix", session="20261104-194500-a", finding=-1, fix=0),
    msg(type="request", id=1, kind="plan_fix", session="20261104-194500-a", finding=0, fix=True),
    msg(type="request", id=1, kind="plan_fix", session="..\\x", finding=0, fix=0),
    msg(type="request", id=1, kind="plan_fix", session="20261104-194500-a", finding=0, fix=0,    # never its params
        fix_id="power_plan", params={"scheme": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"}),
    msg(type="request", id=1, kind="apply_fix", plan="d-0123456789abcdef"),        # another kind of token
    msg(type="request", id=1, kind="apply_fix", plan="p-0123456789ABCDEF"),
    msg(type="request", id=1, kind="apply_fix", plan="p-0123456789abcdef0"),
    msg(type="request", id=1, kind="apply_fix"),
    msg(type="request", id=1, kind="changes", entry="0123456789ab"),
    msg(type="request", id=1, kind="undo_change", entry="0123456789ab"),           # force must be said
    msg(type="request", id=1, kind="undo_change", entry="0123456789ab", force=1),  # a number is no yes
    msg(type="request", id=1, kind="undo_change", entry="0123456789ab", force="true"),
    msg(type="request", id=1, kind="undo_change", entry="0123456789AB", force=False),
    msg(type="request", id=1, kind="undo_change", entry="0123456789a", force=False),
    msg(type="request", id=1, kind="undo_change", entry="power_plan", force=False),   # a fix's id, not a change's
    msg(type="request", id=1, kind="undo_change", entry=["0123456789ab"], force=False),
    msg(type="request", id=1, kind="undo_all"),
    msg(type="request", id=1, kind="undo_all", entries=[]),                      # one change at least
    msg(type="request", id=1, kind="undo_all", entries="0123456789ab"),          # a list of them, never one
    msg(type="request", id=1, kind="undo_all", entries=["0123456789ab", "0123456789ab"]),    # none twice
    msg(type="request", id=1, kind="undo_all", entries=["0123456789ab", "power_plan"]),
    msg(type="request", id=1, kind="undo_all", entries=["0123456789ab"], force=True),         # never put back anyway
    msg(type="request", id=1, kind="undo_all", entries=[f"{i:012x}" for i in range(bridge.MAX_UNDO_ALL + 1)]),
    msg(type="frames_done"),                                               # a notice without its token
    msg(type="frames_done", token=1, extra=2),
    msg(type="ready", x=1),
    msg(type="reply", id=1, ok=True),                                      # the app's own kind of message
    msg(type="request", id=1, kind="library") + " " * bridge.MAX_MESSAGE_CHARS,     # too long to read
    "[" * 100_000,                                                         # nested past the parser's depth
])
def test_anything_else_is_refused(text):
    assert bridge.parse(text) is None


def test_a_delete_names_as_many_recordings_as_its_cap_within_one_message():
    most = [f"20261104-194500-{'a' * 60}{i:04d}" for i in range(bridge.MAX_DELETE)]      # the longest names there are
    text = msg(type="request", id=bridge.MAX_ID, kind="delete_recordings", game="g-0123456789abcdef", sessions=most)
    assert len(text) <= bridge.MAX_MESSAGE_CHARS and bridge.parse(text).args["sessions"] == most


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


def test_a_journal_entrys_id_is_what_the_journal_gives_and_nothing_else(tmp_path):
    from lagdoctor.fixes.journal import Journal
    e = Journal(tmp_path / "j.json").begin("power_plan", "the active power plan", {}, "a", "b")
    assert bridge.entry_ok(e.id)
    back, newline = chr(92), chr(10)
    for bad in (None, 12, "", "0123456789a", "0123456789abc", "0123456789aG", ".." + back + "0123456789",
                "0123456789ab" + newline, ["0123456789ab"]):
        assert not bridge.entry_ok(bad), bad
