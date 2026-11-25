"""lag-doctor's capture service: the relay core (lagdoctor/relay/server.py) as a LocalService Windows service, so a
recording needs no UAC prompt (docs/INSTALLER-PLAN.md, Stage 4; DESIGN.md, "The capture service").

The MSI installs it as `python.exe -B -m lagdoctor.service` from Program Files, on demand, with its privileges
trimmed to SeChangeNotifyPrivilege and an unrestricted service SID (E3), and lets interactive users start it and
nothing more (E9, DECISIONS 2026-11-25). Where the elevated relay takes what it records from the command line the UAC
prompt approved, the service takes it from its caller, and so judges the caller itself, on its own reads:

1. Its pipe has a fixed name (protocol.SERVICE_PIPE), one instance (one recording on the PC at a time), remote clients
   refused, interactive users alone let in at medium integrity or above, and its own service SID as the owner, which
   no other process can set: a client checks that owner and that the pipe's server is the process the SCM names.
2. A caller says what it asks for in its first message (protocol `ask`) within FIRST_ASK_S, or is let go: any
   interactive user may connect, and would otherwise hold the one instance.
3. Who the caller is comes from its own token through the pipe (pipe.PipeServer.caller), never from what it says:
   its account and Windows session. The pipe's own client session, and the session of the caller's process in the
   service's handle-free snapshot, must both be that session.
4. The game is judged as the relay judges it, in the caller's session: the running game by target.accept_target, or
   for Play each process the caller names by the folder rule (target.check_proposed), started after the ask. The
   service runs only the PresentMon installed beside it, after its check (setup.verify).
5. Of the game's tree, only the game and the processes whose program lies under the game's own folder count as the
   game's (GameFolderTree): nothing tells the service another process's account without opening it (E9), and a
   descendant from elsewhere may be another account's. Every other program's bytes count only in the totals for the
   caller's session, as the relay's do.
6. One caller holds the service for at most protocol.SERVICE_HOLD_S, counted from when its ask was read: every wait
   before the recording starts and the recording itself end by then (server.Relay's `until`), said in words, so a
   forgotten or hostile hold cannot keep every other window from the service. The recorder stops itself before that
   (relay/client.HOLD_MARGIN_S), so an honest recording ends cleanly with its frames.
7. Last of its checks, it takes the machine-wide trace lock (capture/tracelock.py) as its own SID, and lets it go once
   the recording has ended: while another lag-doctor recording on this PC holds it (an elevated relay or watch in
   another Windows session), the caller is refused, since starting the trace sessions would stop that recording's.
8. When the recording ends the service waits for the next caller, and stops itself after IDLE_S without one; the next
   recording starts it again.

It writes no file and prints nothing: a service has nowhere to print, and what a caller is refused is said on the
pipe (`error`, then `end`).
"""
from __future__ import annotations

import contextlib
import ntpath
import os
import threading
import time
from typing import Any, Callable

from lagdoctor.relay import protocol, server

INTERACTIVE = "S-1-5-4"            # NT AUTHORITY\INTERACTIVE: the pipe's only clients
FIRST_ASK_S = 5.0                  # a caller says what it asks for within this, or is let go
IDLE_S = 120.0                     # no caller for this long: the service stops itself
WAIT_CLIENT_S = 0.5                # one wait for a caller; the stop request is looked at between two
READER_GONE_S = 5.0                # after a recording, how long its pipe reader has to leave (its reads wait 1 s)
FILETIME_AT_1970 = 116_444_736_000_000_000    # 100 ns intervals from 1601-01-01 to 1970-01-01
MAX_KNOWN_PROGRAMS = 4096          # GameFolderTree's remembered answers, one per process


def filetime_now() -> int:
    """Now in the units of a process's create time (ProcInfo.create_time: FILETIME, 100 ns since 1601)."""
    return time.time_ns() // 100 + FILETIME_AT_1970


def _fold(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").casefold()


def read_image(image: Callable[[int], str | None], pid: int) -> str | None:
    """A process's program path through `image` (ntquery.image_path, no handle), or None when it cannot be read."""
    try:
        return image(pid)
    except OSError:
        return None


def game_folder(path: str | None) -> str:
    """The folder of the game's program as Play's folder rule takes a folder (target.check_folder): a local drive's
    folder, below the drive's root and outside the Windows folder. ValueError says why the service records no program
    from there: a game named at C:\\Windows\\explorer.exe, or at a drive's root, would make the whole folder its own."""
    from lagdoctor.capture import target
    if not path:
        raise ValueError("Windows did not give the path of the game's program")
    return target.check_folder(ntpath.dirname(path))


class GameFolderTree:
    """The capture service's rule for the game's tree (Relay's `tree`): the game itself, and each process descended
    from it whose program lies under the folder the game's own program is in (game_folder), both paths read without
    a handle (`image`, ntquery.image_path). A path that cannot be read counts as elsewhere, and a game whose folder
    fails the rule keeps its tree to itself. `known`: (game, its folder) as the service checked it already. Each
    process's answer is kept by (pid, create time), so a reused pid is judged again."""

    def __init__(self, image: Callable[[int], str | None],
                 known: tuple[tuple[int, int], str] | None = None) -> None:
        self.image = image
        self._folder: tuple[tuple[int, int], str | None] | None = (
            None if known is None else (known[0], _fold(known[1])))
        self._known: dict[tuple[int, int], bool] = {}

    def _game_folder(self, game: tuple[int, int]) -> str | None:
        if self._folder is None or self._folder[0] != game:
            try:
                folder = _fold(game_folder(read_image(self.image, game[0])))
            except ValueError:
                folder = None
            self._folder = (game, folder)
            self._known.clear()
        return self._folder[1]

    def __call__(self, procs: dict, game: tuple[int, int], pids: set[int]) -> set[int]:
        folder = self._game_folder(game)
        keep = {pid for pid in pids if pid == game[0]}
        if folder is None:
            return keep
        for pid in pids:
            p = procs.get(pid)
            if pid == game[0] or p is None:
                continue
            key = (pid, p.create_time)
            under = self._known.get(key)
            if under is None:
                path = read_image(self.image, pid)
                under = bool(path) and _fold(path).startswith(folder + "\\")
                if len(self._known) >= MAX_KNOWN_PROGRAMS:
                    self._known.clear()
                self._known[key] = under
            if under:
                keep.add(pid)
        return keep


class Refused(Exception):
    """A caller the service will not record for: `why` in plain words, said on the pipe."""


class Host:
    """The service's work: callers one at a time on its pipe, each judged, then recorded through the relay core.

    `parts`: what the relay core runs (server.Parts; the defaults are the real ones). `presentmon_exe`: the installed
    PresentMon (None: frame timing is refused). `owner`: the pipe's owner in SDDL ("O:<SID>"); the default is this
    service's own SID, which only its process holds, and "" leaves Windows' default (tests). `caller`: who the
    connected client is (default: from its token, pipe.PipeServer.caller). `hold_s`: how long one caller may hold the
    service, from its ask to its recording's end (protocol.SERVICE_HOLD_S). `trace_lock`: () -> the machine-wide trace
    lock for one recording, not yet acquired (default a tracelock.TraceLock owned by this service's SID, as its pipe
    is)."""

    def __init__(self, parts: server.Parts | None = None, *, presentmon_exe=None, pipe_name: str = protocol.SERVICE_PIPE,
                 owner: str | None = None, idle_s: float = IDLE_S, first_ask_s: float = FIRST_ASK_S,
                 caller: Callable[[Any], Any] | None = None, hold_s: float = protocol.SERVICE_HOLD_S,
                 trace_lock: Callable[[], Any] | None = None) -> None:
        from lagdoctor.capture import tracelock
        from lagdoctor.winapi import service
        self.parts = parts or server.Parts()
        if self.parts.snapshot is None:
            self.parts.snapshot = server._snapshot_fn()
        self.presentmon_exe = presentmon_exe
        self.pipe_name = pipe_name
        self.owner = f"O:{service.service_sid(protocol.SERVICE_NAME)}" if owner is None else owner
        self.trace_lock = trace_lock or (lambda: tracelock.TraceLock(service.service_sid(protocol.SERVICE_NAME)))
        self._trace = None                     # the trace lock the caller now recorded for holds
        self.idle_s, self.first_ask_s, self.hold_s = idle_s, first_ask_s, hold_s
        self.caller = caller or (lambda srv: srv.caller())
        self.served = 0                        # callers recorded for (tests read it)
        self._connected_at = 0                 # when the caller now on the pipe connected (FILETIME)
        self.refused: list[str] = []           # why each refused caller was refused, newest last (at most 100)

    def serve(self, stop: threading.Event) -> int:
        """Take callers until `stop` is set or none came for idle_s: protocol.EXIT_OK. EXIT_PIPE when the pipe
        could not be made (another process holds the name)."""
        from lagdoctor.winapi import pipe
        sddl = self.owner + pipe.client_dacl([INTERACTIVE])
        try:
            srv = pipe.PipeServer(self.pipe_name, sddl, max_message=protocol.MAX_MESSAGE)
        except OSError:
            return protocol.EXIT_PIPE
        # A stop from the SCM ends a recording in progress too: closing the pipe ends every call on it, so the relay
        # sees its caller gone, stops PresentMon and the network trace, and returns.
        threading.Thread(target=lambda: stop.wait() and srv.close(), name="service-stop", daemon=True).start()
        with srv:
            last = time.monotonic()
            while not stop.is_set():
                try:
                    came = srv.wait_client(WAIT_CLIENT_S)
                except OSError:
                    if stop.is_set():                  # closed by the stop: the service is going
                        break
                    raise
                if not came:
                    if time.monotonic() - last >= self.idle_s:
                        break
                    continue
                self._connected_at = filetime_now()
                try:
                    if not self._one(srv):
                        return protocol.EXIT_CRASH     # a recording's reader would not leave: take no other caller
                finally:
                    with contextlib.suppress(OSError):  # the caller left, or the stop closed the pipe
                        srv.disconnect()
                    last = time.monotonic()
        return protocol.EXIT_OK

    # ---- one caller -----------------------------------------------------------------------------------
    def _say_no(self, srv, why: str) -> None:
        """Say why, then `end`, and give the caller GOODBYE_S to read them and hang up: a disconnect drops what the
        caller has not read yet."""
        why = "".join(c if c.isprintable() else "?" for c in why)   # a program's name can hold control characters
        self.refused = (self.refused + [why])[-100:]
        try:
            srv.write(protocol.error(why), server.WRITE_TIMEOUT_S)
            srv.write(protocol.end(), server.WRITE_TIMEOUT_S)
        except (OSError, TimeoutError, ValueError):
            return
        deadline = time.monotonic() + server.GOODBYE_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                srv.read(left)                         # anything more it sends is not taken
            except (OSError, TimeoutError, ValueError):
                return                                 # it hung up (or took too long): either way, done

    def _one(self, srv) -> bool:
        """One caller, judged and recorded for or refused. False when its recording's pipe reader did not leave, so
        no other caller may be taken (its messages could reach this recording)."""
        try:
            data = srv.read(self.first_ask_s)
        except (TimeoutError, OSError):
            return True                                # said nothing in time, or left: let go
        until = time.monotonic() + self.hold_s         # the hold counts from the ask, its judging included
        try:
            relay = self._judge(srv, data, until)
        except Refused as no:
            self._say_no(srv, str(no))                 # the trace lock is _judge's last check: never held here
            return True
        except Exception as exc:  # noqa: BLE001 - one caller's surprise must not end the service for the next
            self._let_go_of_the_trace()                # the relay core could not be made once it was held
            self._say_no(srv, f"the capture service could not judge what was asked ({type(exc).__name__}: {exc})")
            return True
        self.served += 1
        try:
            relay.serve()
        except Exception as exc:  # noqa: BLE001 - say why on the pipe if it is still there; the next caller is served
            relay.fail(f"the capture service stopped this recording on an error it did not expect "
                       f"({type(exc).__name__}: {exc})")
            relay._goodbye()                           # so the words are read before the pipe is disconnected
        finally:
            left = relay.close(READER_GONE_S)
            self._let_go_of_the_trace()                # its sources stopped in serve(): another may trace now
        return left

    def _hold_the_trace(self) -> None:
        """The machine-wide trace lock for the caller's recording, as the last of _judge's checks: Refused while another
        lag-doctor recording on this PC holds it, or when Windows cannot say; held otherwise until the recording ends
        (TAKEN, which another program holds, goes on: there is nothing to hold)."""
        from lagdoctor.capture import tracelock
        lock = self.trace_lock()
        try:
            got = lock.acquire()
        except OSError as exc:
            raise Refused(f"the capture service could not check whether another recording runs on this PC ({exc})"
                          ) from None
        if got == tracelock.BUSY:
            raise Refused("another lag-doctor recording on this PC, in another Windows session, is timing frames or "
                          "tracing the network, and recording now would stop it")
        self._trace = lock

    def _let_go_of_the_trace(self) -> None:
        lock, self._trace = self._trace, None
        if lock is not None:
            lock.release()

    def _judge(self, srv, data: bytes, until: float) -> server.Relay:
        """The caller and what it asks for, judged; the relay that will record it, which ends at `until` (its hold's
        end, time.monotonic) at the latest. Refused with why when not."""
        from lagdoctor.capture import target
        from lagdoctor.winapi import pipe
        try:
            ask = protocol.from_caller(data)
        except protocol.ProtocolError as exc:
            raise Refused(f"the recording window asked for something the capture service does not take ({exc})")
        if ask["v"] != protocol.VERSION:
            raise Refused("the recording window and the installed capture service are different versions of "
                          "lag-doctor, so it did not record through it")
        try:
            who = self.caller(srv)
            pid, pipe_session = srv.client_pid(), srv.client_session()
        except OSError as exc:
            raise Refused(f"the capture service could not tell who asked ({exc})")
        except pipe.StillImpersonating:
            os._exit(protocol.EXIT_CRASH)                  # never go on acting as the caller
        since = filetime_now()
        try:
            procs = self.parts.snapshot()
        except OSError as exc:
            raise Refused(f"the capture service could not see the running programs ({exc})")
        rec, me = procs.get(pid), procs.get(self.parts.me)
        if rec is None or me is None:
            raise Refused("the capture service could not find the recording window, or itself, among the running "
                          "programs")
        if rec.create_time > self._connected_at:
            raise Refused("the program holding the recording window's number started after the connection was made, "
                          "so it is not the one that connected")
        if who.session != pipe_session or rec.session_id != who.session:
            raise Refused("the recording window's Windows session is not the one its account signed in to")
        folders, tree = None, GameFolderTree(self.parts.image)
        if ask["game"] is not None:
            why = target.accept_target(procs, *ask["game"], session=who.session, approved=ask["game"])
            if why is not None:
                raise Refused(why)
            if ask["game"][0] == pid:
                raise Refused("the recording window is not the game")
            try:
                folder = game_folder(read_image(self.parts.image, ask["game"][0]))
            except ValueError as exc:
                raise Refused(f"the capture service records no program from there ({exc})") from None
            try:                                       # the path was read after the snapshot: still the same game?
                again = self.parts.snapshot().get(ask["game"][0])
            except OSError as exc:
                raise Refused(f"the capture service could not see the running programs ({exc})") from None
            if again is None or again.create_time != ask["game"][1]:
                raise Refused("the game exited while the capture service checked it")
            tree = GameFolderTree(self.parts.image, known=(ask["game"], folder))
        else:
            try:
                folders = target.check_forms(list(ask["folders"]))
            except ValueError as exc:
                raise Refused(f"the capture service does not wait on that folder ({exc})") from None
        exe = None
        if ask["frames"]:
            if self.presentmon_exe is None:
                raise Refused("the capture service has no PresentMon beside it, so it cannot time frames")
            try:
                self.parts.verify(self.presentmon_exe)
            except Exception:  # noqa: BLE001 - SetupError, or anything that stops the check: never run it unchecked
                raise Refused("the capture service's PresentMon failed its check, so it was not run") from None
            exe = self.presentmon_exe
        cfg = server.Config(pipe=self.pipe_name, recorder=(pid, rec.create_time), sid=who.sid, game=ask["game"],
                            frames=ask["frames"], network=ask["network"], presentmon_exe=exe,
                            folders=folders, wait_s=ask["wait_s"])
        self._hold_the_trace()                         # last: no check after it may refuse the caller
        return server.Relay(cfg, srv, who.session, self.parts, since=since, me=(me.pid, me.create_time), tree=tree,
                            until=until)
