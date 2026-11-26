"""The install check's caller of the capture service: run by msi_check.py as a throwaway standard account, with the
INSTALLED runtime, on GitHub's runner only.

    "<install>\\python.exe" msi_service_client.py OUT GAME_PID

With the runtime's ._pth, its own folder is not on sys.path and the install's is, so every lagdoctor module it uses is
the installed one (it records which). In order, it records:
- which of start, stop, change-config and query-status this account may open the service with;
- StartServiceW, as the recorder will call it, and the process id the SCM then names;
- the pipe: its server's process id and its owner (the service's own SID, which nothing else can set);
- an `ask` for the stand-in game (frame timing and the network trace), the service's answer, then a short recording
  and its end: the frames status, whether the network trace started, which processes' events came whole;
- the machine-wide trace lock under its real name while the service records (`lock_during`: its state, its owner, and
  what opening it to take it met) and once the recording has ended and the pipe is closed (`lock_after`).
The result goes into OUT, a new file. Exit code 0 when it ran to the end, whatever it found; 1 when it crashed (the
traceback is in OUT).
"""
import ctypes
import json
import os
import sys
import time
import traceback
from ctypes import wintypes

RIGHTS = {"start": 0x0010, "stop": 0x0020, "change_config": 0x0002, "query_status": 0x0004}
RECORD_S = 8.0                 # how long the recording runs before the stop
END_S = 45.0                   # the stop to the service's `end`: PresentMon's worst stop is 25 s
CONNECT_S = 30.0
LOCK_FREE_S = 10.0             # the recording's end to the trace lock read free: the service lets go as its relay ends


def open_error(name: str, access: int) -> int | None:
    """None when this account may open the service with `access` (the handle is closed at once), else the error."""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    adv.OpenSCManagerW.restype = ctypes.c_void_p
    adv.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
    adv.OpenServiceW.restype = ctypes.c_void_p
    adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    manager = adv.OpenSCManagerW(None, None, 0x0001)
    if not manager:
        return ctypes.get_last_error()
    try:
        handle = adv.OpenServiceW(manager, name, access)
        if not handle:
            return ctypes.get_last_error()
        adv.CloseServiceHandle(handle)
        return None
    finally:
        adv.CloseServiceHandle(manager)


def read_until(client, protocol, kinds: set[str], timeout: float, seen: list[dict]) -> dict | None:
    """Messages from the service, kept in `seen`, until one of `kinds` (returned) or `end`, or the time is up."""
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        try:
            msg = protocol.from_relay(client.read(left))
        except TimeoutError:
            return None
        seen.append(msg)
        if msg["t"] in kinds or msg["t"] == "end":
            return msg
    return None


def _read(what):
    """One read of the lock: what `what()` answers, or the error it raised as text, never a crash."""
    try:
        return what()
    except Exception as exc:  # noqa: BLE001 - the result must say what each read met
        return f"{type(exc).__name__}: {exc}"


def lock_now() -> dict:
    """This PC's machine-wide trace lock (lagdoctor/capture/tracelock.py, its real name) as this account sees it: its
    state, its owner (None: no lock of that name) and what opening it to wait on it, which is how a thread takes a
    mutex, met: the error number when Windows refused, None when it opened, "no lock" when there was none."""
    from lagdoctor.capture import tracelock
    from lagdoctor.winapi import elevation

    def owner():
        handle = elevation.open_mutex(tracelock.NAME, tracelock.OTHERS)
        if handle is None:
            return None
        try:
            return elevation.mutex_owner(handle)
        finally:
            elevation.release_named_mutex(handle)

    def take():
        try:
            handle = elevation.open_mutex(tracelock.NAME, elevation.SYNCHRONIZE)
        except OSError as exc:
            if exc.winerror is None:
                raise
            return exc.winerror
        if handle is None:
            return "no lock"
        elevation.release_named_mutex(handle)        # opened, never waited on: this account took nothing
        return None

    return {"state": _read(tracelock.check), "owner": _read(owner), "take": _read(take)}


def lock_after() -> str:
    """The lock's state after the recording, read until it is free for LOCK_FREE_S at most: the last answer."""
    from lagdoctor.capture import tracelock
    deadline = time.monotonic() + LOCK_FREE_S
    while (state := _read(tracelock.check)) != tracelock.FREE and time.monotonic() < deadline:
        time.sleep(0.1)
    return state


def run(game_pid: int) -> dict:
    import lagdoctor
    from lagdoctor import clock
    from lagdoctor.relay import protocol
    from lagdoctor.winapi import ntquery, pipe, service
    name = protocol.SERVICE_NAME
    out: dict = {"pid": os.getpid(), "lagdoctor": lagdoctor.__file__, "sys_path": list(sys.path),
                 "rights": {k: open_error(name, v) for k, v in RIGHTS.items()}}
    try:
        out["started_by_this"] = service.start(name)
        out["start_error"] = None
    except OSError as exc:
        out["start_error"] = exc.winerror
        return out
    deadline = time.monotonic() + CONNECT_S
    while (state := service.query(name))[0] != "running" and time.monotonic() < deadline:
        time.sleep(0.1)
    out["state"], out["scm_pid"] = state
    game = ntquery.Snapshotter().snapshot().get(game_pid)
    if game is None:
        out["why"] = "the stand-in game was not running"
        return out
    seen: list[dict] = []
    with pipe.connect(protocol.SERVICE_PIPE, CONNECT_S, max_message=protocol.MAX_MESSAGE) as client:
        out["server_pid"], out["owner_sid"] = client.server_pid(), client.owner_sid()
        client.write(protocol.ask((game.pid, game.create_time), None, None, True, True), 10.0)
        out["hello"] = read_until(client, protocol, {"hello", "error"}, 30.0, seen)
        if out["hello"] is not None and out["hello"]["t"] == "hello":
            client.write(protocol.start(clock.now()), 10.0)
            read_until(client, protocol, set(), RECORD_S, seen)
            out["lock_during"] = lock_now()          # the service is recording: it holds the lock
            client.write(protocol.stop(), 10.0)
            out["last"] = read_until(client, protocol, set(), END_S, seen)
    if "lock_during" in out:
        out["lock_after"] = lock_after()             # the pipe is closed: the service lets go once its relay ends
    events = [ev for m in seen if m["t"] == "events" for ev in m["ev"]]
    pids: dict[str, int] = {}
    for ev in events:
        pid = str(int.from_bytes(ev[3][:4], "little"))
        pids[pid] = pids.get(pid, 0) + 1
    frames = [m for m in seen if m["t"] == "frames"]
    nets = [m for m in seen if m["t"] == "net"]
    out.update(types=[m["t"] for m in seen], errors=[m["why"] for m in seen if m["t"] == "error"],
               event_pids=pids, frames_last=frames[-1] if frames else None,
               net_started=nets[-1]["started"] if nets else None,
               net_why=nets[-1]["why"] if nets else None,
               ended=bool(seen) and seen[-1]["t"] == "end")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print(__doc__, file=sys.stderr)
        return 2
    target, game_pid = argv[0], int(argv[1])
    code = 0
    try:
        data = run(game_pid)
    except Exception:  # noqa: BLE001 - the result must say what broke
        data, code = {"pid": os.getpid(), "crashed": traceback.format_exc()}, 1
    with open(target, "x", encoding="utf-8") as f:                   # a new file only, never an overwrite
        f.write(json.dumps(data, indent=1, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
