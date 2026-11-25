"""The machine-wide trace lock: one lag-doctor recording at a time on this PC runs frame timing or the network trace.

The hazard. PresentMon's trace session ("lagdoctor", capture/presentmon.py) and the network trace's ("lag-doctor-net",
capture/netflows.py) have fixed names, and starting either stops a running session of that name first: PresentMon runs
with --stop_existing_session, and etw.RealtimeSession.start stops one to clear the orphan a crash leaves. Any token that
may start trace sessions may also stop one by name, whoever started it. The capture lock (session.CaptureLock, in the
Local\\ namespace) keeps two recordings of one Windows session apart, but not the capture service, an elevated relay
and an elevated watch in different Windows sessions: each would stop the other's recording.

The rule. Every lag-doctor that starts those sessions itself holds this lock, a mutex named NAME in the Global\\
namespace, from before the first one starts until both have stopped: the elevated relay (relay/server.main), the
capture service for each caller it records (service.Host), and an elevated watch that records in its own window
(cli.cmd_watch). The lock is the mutex's ownership, never its mere existence: a program that keeps a handle to the name
open keeps nothing locked, and a holder that crashed left the mutex abandoned, which reads free. Ownership belongs to a
thread, so a held lock is owned by a thread of its own that lives until release().

- The mutex is owned by SYSTEM, Administrators or the capture service's own SID (trusted_owners). Windows lets a token
  set only an owner it holds, so a standard account cannot make one (it is refused with error 1307, measured by
  tests/test_tracelock.py). Its DACL lets those and its owner wait on it, which is how a thread takes a mutex, and lets
  everyone else read its owner and whether it is held (OTHERS), never take it: a standard account cannot hold
  recordings off this way. Trusting Administrators trusts every elevated program, not only lag-doctor; an elevated
  program can stop the trace sessions directly anyway.
- BUSY: the name is a mutex with a trusted owner, held: another lag-doctor recording on this PC is tracing.
- TAKEN: the name is held by something lag-doctor cannot trust (another owner, a DACL that shuts lag-doctor out,
  another kind of object). The protection is lost and the recording goes on; the recorder says so once.
- A token that may set none of the trusted owners (an unelevated member of Performance Log Users, which may trace) only
  checks (check()): it honours the lock but cannot hold one. The recorder also checks before it asks the capture
  service and before it raises the UAC prompt.
"""
from __future__ import annotations

import threading
from typing import Iterable

from lagdoctor.winapi import elevation

NAME = "Global\\lag-doctor-trace"     # read when the lock is used, so tests can move it (tests/conftest.py)
FREE, HELD, BUSY, TAKEN = "free", "held", "busy", "taken"
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
EVERYONE = "S-1-1-0"
# What everyone but the trusted owners may do with the mutex: read its owner and whether a thread holds it. Never
# SYNCHRONIZE: a wait is how a thread takes a mutex, so an account granted it could hold every recording off.
OTHERS = elevation.READ_CONTROL | elevation.MUTANT_QUERY_STATE
# A name lag-doctor cannot use: a DACL that refuses it the rights it asks for, or another kind of object.
_SQUATTED = (elevation.ERROR_ACCESS_DENIED, elevation.ERROR_INVALID_HANDLE)
WAIT_S = 10.0                         # the holder thread's calls return at once: a Windows that hangs ends acquire here


def trusted_owners() -> frozenset[str]:
    """The owners that make a held mutex of this name another lag-doctor recording: SYSTEM, Administrators and the
    capture service's own SID (NT SERVICE\\<protocol.SERVICE_NAME>), each set only by a token that holds it."""
    from lagdoctor.relay import protocol
    from lagdoctor.winapi import service
    return frozenset((SYSTEM, ADMINISTRATORS, service.service_sid(protocol.SERVICE_NAME)))


def descriptor(owner: str) -> str:
    """The mutex's descriptor, owned by `owner`: every trusted owner, and `owner`, may do anything with it; everyone
    else only OTHERS. ValueError unless `owner` is a SID."""
    from lagdoctor.winapi import pipe
    if not pipe.is_sid(owner):
        raise ValueError(f"not a SID: {owner!r}")
    full = sorted(trusted_owners() | {owner})
    return f"O:{owner}D:P" + "".join(f"(A;;GA;;;{sid})" for sid in full) + f"(A;;0x{OTHERS:x};;;{EVERYONE})"


def check(name: str | None = None, trusted: Iterable[str] | None = None) -> str:
    """FREE, BUSY or TAKEN for the lock now, read-only: it never makes the mutex and never takes it, so a token that may
    set no trusted owner can honour the lock. `name`: default NAME; `trusted`: the owners to trust (default
    trusted_owners()). OSError when Windows cannot say, so the caller refuses."""
    name = NAME if name is None else name
    trusted = trusted_owners() if trusted is None else frozenset(trusted)
    try:
        handle = elevation.open_mutex(name, OTHERS)
    except OSError as exc:
        if exc.winerror in _SQUATTED:
            return TAKEN
        raise
    if handle is None:
        return FREE
    try:
        if elevation.mutex_owner(handle) not in trusted:
            return TAKEN
        count, _mine, _abandoned = elevation.mutex_state(handle)
        return BUSY if count <= 0 else FREE
    finally:
        elevation.release_named_mutex(handle)


class TraceLock:
    """The lock for one recording, for a token that may set `owner` (a SID): Administrators for the elevated relay and
    an elevated watch, the capture service's own SID for the service. `name`: default NAME, read when acquired;
    `trusted`: the owners to trust (default trusted_owners()). Use acquire(), then release() (or a with-block)."""

    def __init__(self, owner: str, name: str | None = None, trusted: Iterable[str] | None = None) -> None:
        self.sddl = descriptor(owner)
        self.owner, self.name = owner, name
        self.trusted = None if trusted is None else frozenset(trusted)
        self.state: str | None = None          # what acquire() last answered; None before it and after release()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._let_go: threading.Event | None = None

    @property
    def held(self) -> bool:
        return self.state == HELD

    def acquire(self) -> str:
        """HELD (this lock holds the mutex until release()), BUSY or TAKEN (module docstring); OSError for anything
        else, so the caller refuses. Once held, a second call holds nothing more; after BUSY or TAKEN it asks again."""
        with self._lock:
            if self.state == HELD:
                return HELD
            name = NAME if self.name is None else self.name
            trusted = trusted_owners() if self.trusted is None else self.trusted
            out: dict = {}
            ready, let_go = threading.Event(), threading.Event()
            thread = threading.Thread(target=self._hold, args=(name, trusted, out, ready, let_go),
                                      name="lagdoctor-trace-lock", daemon=True)
            thread.start()
            if not ready.wait(WAIT_S):
                let_go.set()                   # should it get there after all, it lets go at once
                raise OSError(f"Windows did not answer about {name} within {WAIT_S:g} s")
            if "error" in out:
                raise out["error"]
            self.state = out["state"]
            if self.state == HELD:
                self._thread, self._let_go = thread, let_go
            return self.state

    def _hold(self, name: str, trusted: frozenset[str], out: dict, ready: threading.Event,
              let_go: threading.Event) -> None:
        """The holder thread: takes the mutex, says how it went, and while it holds it, waits for release()."""
        handle = None
        try:
            handle, out["state"] = self._take(name, trusted)
        except BaseException as exc:  # noqa: BLE001 - acquire() raises it on the caller's thread
            out["error"] = exc
        held = out.get("state") == HELD
        if handle and not held:
            elevation.release_named_mutex(handle)
        ready.set()
        if not held:
            return
        let_go.wait()
        try:
            elevation.release_mutex(handle)    # this thread holds it: let it go before its handle closes
        finally:
            elevation.release_named_mutex(handle)

    def _take(self, name: str, trusted: frozenset[str]) -> tuple[int | None, str]:
        try:
            handle, made = elevation.create_mutex(name, self.sddl, own=True)
        except OSError as exc:
            if exc.winerror in _SQUATTED:
                return None, TAKEN
            raise
        if made:
            return handle, HELD
        try:
            if elevation.mutex_owner(handle) not in trusted:
                return handle, TAKEN
            # Held by nobody (its holder let go, or ended holding it), it is this recording's now.
            return handle, (HELD if elevation.take_mutex(handle) else BUSY)
        except BaseException:
            elevation.release_named_mutex(handle)
            raise

    def release(self) -> None:
        """Let the mutex go and wait for that. Safe to call twice, and before acquire()."""
        with self._lock:
            thread, let_go = self._thread, self._let_go
            self._thread = self._let_go = None
            self.state = None
        if let_go is not None:
            let_go.set()
        if thread is not None:
            thread.join(WAIT_S)

    def __enter__(self) -> "TraceLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
