"""The recorder's side of the capture service (relay/client.via_service, cli._via_service): run against the service's
own host on a thread here (tests/test_service.py's running_host), never against this PC's service.

The SCM's side is a stand-in (`scm`): StartServiceW and the service's process id are what the test says, and the
service's "process" is this test's own, since its host runs here. The pipe the client opens is the host's, under its
test name. tests/conftest.py keeps every other test, and every process a test starts, away from the real service
(LAGDOCTOR_NO_SERVICE, and _no_real_capture_service); these tests lift the first for themselves only.
"""
from __future__ import annotations

import argparse
import os

import pytest

from lagdoctor import cli
from lagdoctor.relay import client, protocol
from lagdoctor.winapi import pipe
from lagdoctor.winapi import service as winsvc
from tests import conftest
from tests import test_relay as tr
from tests import test_service as ts
from tests.test_relay import game  # noqa: F401 - a fixture: a running 'game' in this session


class StandInSCM:
    """The service control manager as the client sees it: start, the service's state and pid, its SID."""

    def __init__(self, pid: int, owner: str, start_error: int | None = None) -> None:
        self.pid, self.owner, self.start_error = pid, owner, start_error
        self.calls: list[str] = []

    def start(self, name: str) -> bool:
        self.calls.append(f"start {name}")
        if self.start_error is not None:
            raise OSError(None, "refused", None, self.start_error)
        return True

    def query(self, name: str) -> tuple[str, int]:
        self.calls.append(f"query {name}")
        return "running", self.pid

    def service_sid(self, name: str) -> str:
        return self.owner


@pytest.fixture
def service_on(monkeypatch):
    monkeypatch.delenv(client.SERVICE_OFF_ENV, raising=False)


def to_host(name: str):
    """pipe.connect, sent to the host's test pipe whatever name the client asks for."""
    return lambda _name, timeout_s, max_message=pipe.MAX_MESSAGE: pipe.connect(name, timeout_s, max_message=max_message)


def recorder() -> tuple[int, int]:
    me = tr._me()
    return me.pid, me.create_time


def test_a_recording_through_the_service_needs_no_prompt_and_its_link_names_the_service(game, service_on):  # noqa: F811
    with ts.running_host() as (host, name, _stop, _out):
        scm = StandInSCM(os.getpid(), tr._default_owner())
        before = client._filetime_now()
        link = client.via_service(game, frames=True, network=True, recorder=recorder(), scm=scm, connect=to_host(name))
        try:
            assert link.pid == os.getpid() and link.game == (game["pid"], game["create_time"])
            assert link.frames and link.network and link.folders is None
            assert link.service is True                  # no prompt was raised for it
            assert link.tool_procs() == ((os.getpid(), tr._me().create_time),)   # the service: lag-doctor's own
            assert before <= link.since <= client._filetime_now()
            link.stop()
            assert link.ended.wait(30.0) and link.why is None, link.why
        finally:
            assert link.close() is None                 # no process of this recording's to wait for
    assert scm.calls[0] == f"start {protocol.SERVICE_NAME}" and host.served == 1


def test_plays_link_through_the_service_judges_games_from_the_ask_and_passes_over_the_service(service_on):
    with ts.running_host() as (host, name, _stop, _out):
        scm = StandInSCM(os.getpid(), tr._default_owner())
        link = client.via_service(None, frames=False, network=True, recorder=recorder(), folders=(r"D:\Games\Blue",),
                                  wait_s=30.0, scm=scm, connect=to_host(name))
        try:
            assert link.folders == (r"D:\Games\Blue",) and link.game is None
            assert link.own() == (recorder(), (os.getpid(), tr._me().create_time))   # the service's own `me`
            assert link.since > link.create_time         # the ask, not the service's start
            assert link.aim_by is not None
        finally:
            link.close()


@pytest.mark.parametrize("error, installed, words", [(winsvc.ERROR_SERVICE_DOES_NOT_EXIST, False, "not installed"),
                                                      (5, True, "could not be started")])
def test_a_service_that_is_not_installed_or_may_not_be_started_sends_the_recording_to_the_prompt(
        game, service_on, error, installed, words):  # noqa: F811
    scm = StandInSCM(os.getpid(), "S-1-5-80-1", start_error=error)
    with pytest.raises(client.ServiceUnavailable) as exc:
        client.via_service(game, frames=True, network=True, recorder=recorder(), scm=scm,
                           connect=lambda *a, **k: pytest.fail("no pipe is opened"))
    assert exc.value.installed is installed and words in exc.value.why


@pytest.mark.parametrize("who", ["another process", "another owner"])
def test_a_pipe_the_service_did_not_make_is_never_used(game, service_on, who):  # noqa: F811
    with ts.running_host() as (host, name, _stop, _out):
        scm = (StandInSCM(4242, tr._default_owner()) if who == "another process"
               else StandInSCM(os.getpid(), "S-1-5-19"))
        with pytest.raises(client.ServiceUnavailable) as exc:
            client.via_service(game, frames=True, network=True, recorder=recorder(), scm=scm, connect=to_host(name))
        assert exc.value.installed and host.served == 0
        assert ("another program" if who == "another process" else "not owned by the service") in exc.value.why


def test_a_running_service_whose_pipe_never_appears_is_no_busy_one(game, service_on):  # noqa: F811
    def absent(name, timeout_s, max_message=pipe.MAX_MESSAGE):
        exc = TimeoutError("no pipe")
        exc.attempts, exc.errors = 9, {2: 9}                 # ERROR_FILE_NOT_FOUND each time: nobody made it
        raise exc

    with pytest.raises(client.ServiceUnavailable) as exc:
        client.via_service(game, frames=True, network=True, recorder=recorder(),
                           scm=StandInSCM(os.getpid(), "S-1-5-80-1"), connect=absent, start_s=1.0)
    assert "never appeared" in exc.value.why and exc.value.installed


class StoppedOnce(StandInSCM):
    """A service that stopped itself (its idle end) as the first StartServiceW reached it: running after the second."""

    def query(self, name):
        self.calls.append(f"query {name}")
        starts = sum(c.startswith("start") for c in self.calls)
        return ("stopped", 0) if starts < 2 else ("running", self.pid)


def test_a_service_that_stopped_as_it_was_asked_is_started_again(game, service_on):  # noqa: F811
    with ts.running_host() as (host, name, _stop, _out):
        scm = StoppedOnce(os.getpid(), tr._default_owner())
        link = client.via_service(game, frames=True, network=True, recorder=recorder(), scm=scm, connect=to_host(name))
        link.close()
    assert sum(c.startswith("start") for c in scm.calls) == 2


def test_what_the_service_refuses_is_said_and_the_prompt_comes_instead(game, service_on):  # noqa: F811
    def unreadable(srv):
        raise OSError("no token")

    with ts.running_host(caller=unreadable) as (host, name, _stop, _out):
        with pytest.raises(client.ServiceUnavailable) as exc:
            client.via_service(game, frames=True, network=True, recorder=recorder(),
                               scm=StandInSCM(os.getpid(), tr._default_owner()), connect=to_host(name))
    assert exc.value.installed and "did not record for this window" in exc.value.why and "no token" in exc.value.why


def test_a_service_recording_for_another_window_is_left_alone(game, service_on):  # noqa: F811
    with ts.running_host() as (host, name, _stop, _out):
        with pipe.connect(name, 10.0) as other:
            other.write(ts.ask_bytes(game), 5.0)
            ts.messages(other, "hello")                       # it records for that window now
            with pytest.raises(client.ServiceBusy):
                client.via_service(game, frames=True, network=True, recorder=recorder(),
                                   scm=StandInSCM(os.getpid(), tr._default_owner()), connect=to_host(name),
                                   start_s=1.0)
            other.write(protocol.stop(), 5.0)
            ts.messages(other, "end")


def test_the_service_is_never_asked_while_it_is_turned_off(game, monkeypatch):  # noqa: F811
    monkeypatch.setenv(client.SERVICE_OFF_ENV, "1")
    scm = StandInSCM(os.getpid(), "S-1-5-80-1")
    with pytest.raises(client.ServiceUnavailable) as exc:
        client.via_service(game, frames=True, network=True, recorder=recorder(), scm=scm)
    assert not exc.value.installed and scm.calls == []
    assert os.environ.get(client.SERVICE_OFF_ENV) == "1"


def test_every_test_and_its_children_keep_away_from_the_real_service():
    assert os.environ.get(client.SERVICE_OFF_ENV) == "1"   # set by tests/conftest.py for this run and its children


# --------------------------------------------------------------------------------------------- watch ---

def _args(**over) -> argparse.Namespace:
    return argparse.Namespace(**{"elevate": False, "no_elevate": False, "no_network": False, **over})


def test_watch_records_through_the_service_before_it_would_ask_for_a_prompt(monkeypatch):
    from lagdoctor.winapi import elevation
    link = object()
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(cli, "_via_service", lambda *a, **k: (link, None))
    monkeypatch.setattr(cli, "elevation_choice", lambda *a, **k: pytest.fail("no prompt is offered"))
    got = cli._elevation(_args(), {"pid": 1, "create_time": 1}, None, "", False, say=lambda s: None, launcher=None)
    assert got == (link, True, "", None)


def test_watch_told_not_to_elevate_never_asks_the_service(monkeypatch):
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(cli, "_via_service", lambda *a, **k: pytest.fail("the service is not asked"))
    relay, frames, _why, code = cli._elevation(_args(no_elevate=True), {"pid": 1, "create_time": 1}, None, "", False,
                                               say=lambda s: None, launcher=None)
    assert relay is None and frames is False and code is None


@pytest.mark.parametrize("raised, code, said", [
    (client.ServiceBusy("it records for another window"), cli.EXIT_BUSY, "Not recorded"),
    (client.ServiceUnavailable("not installed", installed=False), None, None),
    (client.ServiceUnavailable("its pipe is someone else's"), None, "UAC prompt comes instead")])
def test_watch_says_what_the_service_did_and_goes_on_to_the_prompt_unless_it_is_busy(monkeypatch, raised, code, said):
    def refuse(*a, **k):
        raise raised

    monkeypatch.setattr(client, "via_service", refuse)
    lines: list[str] = []
    assert cli._via_service(_args(), {"pid": 1, "create_time": 1}, say=lines.append) == (None, code)
    assert (said is None and lines == []) or (said is not None and said in " ".join(lines)), lines


# ----------------------------------------------------------------------------------------- the guard ---

def test_the_guard_keeps_tests_from_starting_this_pcs_capture_service(monkeypatch):
    opened: list[str] = []

    def nothing_real(name, access):                      # one layer below the guard: a sabotaged guard reaches this
        opened.append(name)
        raise OSError(None, "stand-in", None, winsvc.ERROR_SERVICE_DOES_NOT_EXIST)

    monkeypatch.setattr(winsvc, "_open", nothing_real)
    guard = conftest.capture_service_guard(monkeypatch)
    caught = next(guard)
    with pytest.raises(OSError):
        winsvc.start(protocol.SERVICE_NAME)
    with pytest.raises(OSError):
        winsvc.query(protocol.SERVICE_NAME)
    assert len(caught) == 2 and opened == []
    with pytest.raises(OSError):
        winsvc.query("lagdoctor-test-other")             # another name passes through (to the stand-in here)
    assert opened == ["lagdoctor-test-other"]
    with pytest.raises(AssertionError):
        next(guard)
