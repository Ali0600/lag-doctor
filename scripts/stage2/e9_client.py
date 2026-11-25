"""E9's caller: what it may do to the service, and who the service says it is.

    "<install>\\python.exe" "<install>\\stage2\\e9_client.py" OUT SERVICE PIPE PHASE

drive.py runs it from the service layout under Program Files, as the throwaway standard account (PHASE "before",
then "after" the service's descriptor grants interactive users SERVICE_START) and as the job's own administrator
("admin"). It records its own token; which of start, stop, change-config and query-status it may open the service
with; in "after", StartServiceW itself; and, except in "before", the pipe: the server's pid against the one the SCM
reports, the pipe's owner, and the service's reply, which says whom it saw (the token it read through the pipe and
what WTSEnumerateProcessesExW said of this process). The result goes into OUT, a new file. Exit code 0 when it ran
to the end, whatever it found; 1 when it crashed (the traceback is in OUT).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

RIGHTS = {"start": 0x0010, "stop": 0x0020, "change_config": 0x0002, "query_status": 0x0004}
CONNECT_S = 30.0
MAX_MESSAGE = 1 << 20


def run(service: str, pipe_name: str, phase: str) -> dict:
    from lagdoctor.winapi import pipe
    from stage2 import scm, tokeninfo
    own = tokeninfo.snapshot()
    out: dict = {"phase": phase, "pid": os.getpid(), "own": own,
                 "rights": {k: scm.open_error(service, v) for k, v in RIGHTS.items()}}
    if phase == "after":
        try:
            scm.start(service, [])
            out["start_error"] = None
        except OSError as exc:
            out["start_error"] = exc.winerror
    if phase == "before":
        return out
    out["status"] = scm.wait_for(service, {"running"}, CONNECT_S)
    t0 = time.monotonic()
    with pipe.connect(pipe_name, CONNECT_S, max_message=MAX_MESSAGE) as client:
        out["connect_s"] = round(time.monotonic() - t0, 3)
        out["server_pid"] = client.server_pid()
        out["server_session"] = client.server_session()
        out["owner_sid"] = client.owner_sid()
        client.write(json.dumps({"pid": os.getpid(), "phase": phase}).encode(), 10.0)
        out["reply"] = json.loads(client.read(10.0))
    out["scm_pid"] = scm.status(service)["pid"]
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[3] not in ("before", "after", "admin"):
        print(__doc__, file=sys.stderr)
        return 2
    target, service, pipe_name, phase = argv
    code = 0
    try:
        data = run(service, pipe_name, phase)
    except Exception:  # noqa: BLE001 - the result must say what broke
        data, code = {"phase": phase, "pid": os.getpid(), "crashed": traceback.format_exc()}, 1
    with open(target, "x", encoding="utf-8") as f:                   # a new file only, never an overwrite
        f.write(json.dumps(data, indent=1, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
