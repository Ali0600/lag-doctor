"""The frame path's offline costs, no ETW rights needed (docs/research/overhead.md, section 3.4).

1. PresentMon replaying each of its own gold ETL captures with lag-doctor's flags: PresentMon's CPU time per CSV row
   it printed (GetProcessTimes on our own child's handle; never the game's).
2. lag-doctor's side per frame: the real parser and PresentMonSource._add() over the biggest replay's rows, repeated
   to --rows rows: CPU per row, and private memory per stored frame.

  uv run python scripts/overhead/pm_parse_bench.py [--rows N] [--json OUT.json]

PresentMon and the gold captures are the tests' pinned downloads (lag-doctor's setup, and the tests' fetch-gold).
CPU per row roughly doubles on a busy PC (33 us quiet, 62-66 us with 11 of 16 logical CPUs busy), so note the load.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import io
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from lagdoctor.capture import presentmon as pm
from lagdoctor.winapi import ntquery
from tests import downloads

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4


def proc_cpu_s(handle: int) -> float:
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    if not _k32.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k),
                                ctypes.byref(u)):
        raise ctypes.WinError(ctypes.get_last_error())
    f = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7  # noqa: E731
    return f(k) + f(u)


def private_mb() -> float:
    p = ntquery.Snapshotter().snapshot()[os.getpid()]
    return p.private_bytes / 2**20


def replay(exe: Path, case: Path) -> dict:
    argv = [str(exe), *pm.replay_args(case)]
    t0 = time.perf_counter()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _err = proc.communicate(timeout=120)
    wall = time.perf_counter() - t0
    cpu = proc_cpu_s(proc._handle)            # our own child, still referenced: its handle is open
    lines = out.decode("latin-1").splitlines()
    rows = max(0, len(lines) - 1)
    return {"case": case.name, "etl_mb": round(case.stat().st_size / 2**20, 2), "rows": rows,
            "presentmon_cpu_s": round(cpu, 3), "wall_s": round(wall, 3),
            "cpu_ms_per_row": round(1000 * cpu / rows, 4) if rows else None, "exit": proc.returncode,
            "stdout": out}


def parse_cost(data: bytes, rows: int, read_mb: Callable[[], float] = private_mb) -> dict:
    """lag-doctor's reader over `data` (PresentMon's header and `rows` rows). The caller holds `data` for the whole
    call, so the input is in both memory readings and cancels out: a first version freed it between them, and frames
    read as 425-485 B each instead of about 0.7 KB."""
    src = pm.PresentMonSource(None, etl_file="x.etl")
    src.origin = 0.0
    gc.collect()
    m0 = read_mb()
    t0, c0 = time.perf_counter(), time.process_time()
    stream = io.BytesIO(data)
    chunks = iter(lambda: stream.readline(pm.PresentMonSource.LINE_LIMIT_BYTES), b"")
    kept = 0
    for row in pm.parse(chunks, src._parser):
        src._add(row)
        kept += 1
    cpu = time.process_time() - c0
    wall = time.perf_counter() - t0
    del stream
    gc.collect()
    m1 = read_mb()
    stored = len(src._series)
    return {"rows_fed": rows, "rows_parsed": kept, "frames_stored": stored, "cpu_s": round(cpu, 3),
            "wall_s": round(wall, 3), "us_per_row": round(1e6 * cpu / max(kept, 1), 2),
            "private_mb_growth": round(m1 - m0, 1),
            "bytes_per_stored_frame": round((m1 - m0) * 2**20 / stored) if stored else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", type=int, default=300_000, help="rows to parse: the biggest replay's, repeated")
    ap.add_argument("--json", type=Path, help="also write every result here")
    args = ap.parse_args(argv)
    try:
        exe, gold = downloads.presentmon(), downloads.gold()
    except (downloads.Missing, downloads.Broken) as exc:
        print(f"pm_parse_bench: {exc}", file=sys.stderr)
        return 2
    results: dict = {"presentmon": [], "parse": None}
    biggest: tuple[int, bytes] | None = None
    for case in sorted(gold.glob("test_case_*.etl")):
        r = replay(exe, case)
        out = r.pop("stdout")
        results["presentmon"].append(r)
        print(json.dumps(r), flush=True)
        if r["exit"] == 0 and (biggest is None or r["rows"] > biggest[0]):
            biggest = (r["rows"], out)
    if biggest is None or not biggest[0]:
        print("pm_parse_bench: no replay printed any rows", file=sys.stderr)
        return 1
    lines = biggest[1].splitlines(keepends=True)
    body = lines[1:]
    repeat = max(1, args.rows // len(body))
    data = lines[0] + b"".join(body) * repeat
    results["parse"] = parse_cost(data, len(body) * repeat)
    print(json.dumps(results["parse"]), flush=True)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
