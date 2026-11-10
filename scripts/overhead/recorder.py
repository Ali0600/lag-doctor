"""lag-doctor's recorder for the overhead benchmark (bench.py starts it): the real target lookup, inventory, session
and collectors, without the CLI (no console status line, no hotkey).

Runs as its own process: the "lag-doctor" whose cost is measured. It attaches to --pid, records until --stop-at or
until that process exits, saves the capture, and writes its own per-thread CPU over [--win-start, --win-end]
(absolute time.perf_counter seconds: QueryPerformanceCounter, the same clock in every process) to --stats.

Thread CPU comes from a handle-free NtQuerySystemInformation snapshot of this process; the Python thread names come
from threading.enumerate() (native_id), so each collector's thread can be named. With --frames it runs PresentMon
itself, in this process (ETW rights needed), as recordings did before the relay.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


def own_threads(snapshotter) -> dict:
    me = os.getpid()
    procs = snapshotter.snapshot(threads_for=(me,))
    p = procs[me]
    names = {t.native_id: t.name for t in threading.enumerate() if t.native_id}
    return {
        "at": time.perf_counter(),
        "proc_cpu_s": (p.user_100ns + p.kernel_100ns) / 1e7,
        "ws_bytes": p.ws_bytes,
        "private_bytes": p.private_bytes,
        "threads": {t.tid: {"cpu_s": (t.user_100ns + t.kernel_100ns) / 1e7, "create_time": t.create_time,
                            "name": names.get(t.tid, "")} for t in (p.threads or [])},
    }


def window_watcher(snapshotter, win_start: float, win_end: float, out: dict) -> None:
    for key, when in (("start", win_start), ("end", win_end)):
        while True:
            left = when - time.perf_counter()
            if left <= 0:
                break
            time.sleep(min(left, 0.5))
        try:
            out[key] = own_threads(snapshotter)
        except Exception as exc:  # noqa: BLE001 - the stats say what failed
            out[key + "_error"] = f"{type(exc).__name__}: {exc}"


def build_collectors(target: dict[str, Any], inventory: dict[str, Any], *, frames: bool,
                     presentmon_exe: Path | None, network: bool, only: str) -> tuple[Any, list]:
    """lag-doctor's own session options and default collectors for this target, cut to `only` (comma-separated
    collector names; empty keeps them all). Raises ValueError for a name this recording would not have: a misspelt
    name would otherwise measure a recorder with nothing in it."""
    from lagdoctor.capture import session as sess
    opts = sess.SessionOptions(duration_s=None, network=network, hotkey=None, frames=frames,
                               presentmon_exe=presentmon_exe)
    collectors = sess.default_collectors(target, inventory, opts)
    keep = [n.strip() for n in only.split(",") if n.strip()]
    if keep:
        names = [c.name for c in collectors]
        unknown = [n for n in keep if n not in names]
        if unknown:
            raise ValueError(f"no collector named {', '.join(unknown)} in this recording "
                             f"(it has {', '.join(names) or 'none'})")
        collectors = [c for c in collectors if c.name in keep]
    return opts, collectors


def _write(path: Path, stats: dict) -> None:
    path.write_text(json.dumps(stats, indent=1, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--stats", type=Path, required=True)
    ap.add_argument("--win-start", type=float, required=True)
    ap.add_argument("--win-end", type=float, required=True)
    ap.add_argument("--frames", action="store_true", help="run PresentMon too (needs ETW rights)")
    ap.add_argument("--presentmon-exe", type=Path, help="default: the one lag-doctor itself would run")
    ap.add_argument("--only", default="", help="comma-separated collector names to keep (default: all)")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--no-lock", action="store_true",
                    help="do not take lag-doctor's capture lock: the caller holds it (bench.py does, for its whole run)")
    ap.add_argument("--stop-at", type=float, help="absolute perf_counter time to stop the recording (default: "
                                                  "when the target exits)")
    args = ap.parse_args(argv)

    from lagdoctor.capture import session as sess
    from lagdoctor.capture import target as tgt
    from lagdoctor.winapi import ntquery

    stats: dict = {"pid": os.getpid(), "argv": sys.argv[1:] if argv is None else list(argv),
                   "t_launch": time.perf_counter()}
    lock = None if args.no_lock else sess.CaptureLock()
    if lock is not None:
        try:
            held = lock.acquire()
        except OSError as exc:
            stats["error"] = f"could not take lag-doctor's capture lock: {exc}"
            _write(args.stats, stats)
            return 3
        if not held:
            stats["error"] = "another lag-doctor recording holds the capture lock"
            _write(args.stats, stats)
            return 3
    try:
        t0 = time.perf_counter()
        target = tgt.find_target(pid=args.pid)          # watch's default: a 2 s GPU Engine sample
        t1 = time.perf_counter()
        inventory = sess._collect_inventory(target)
        t2 = time.perf_counter()
        try:
            opts, collectors = build_collectors(target, inventory, frames=args.frames,
                                                presentmon_exe=args.presentmon_exe, network=not args.no_network,
                                                only=args.only)
        except ValueError as exc:
            stats["error"] = str(exc)
            return 2
        stats.update(find_target_s=round(t1 - t0, 3), inventory_s=round(t2 - t1, 3),
                     adapter_luid=target.get("adapter_luid"), collectors=[c.name for c in collectors])

        window: dict = {}
        snap = ntquery.Snapshotter()
        watcher = threading.Thread(target=window_watcher, args=(snap, args.win_start, args.win_end, window),
                                   name="bench-window", daemon=True)
        watcher.start()
        s = sess.Session(target, opts, collectors=collectors, inventory=inventory, printer=lambda line: None)
        stop = threading.Event()
        if args.stop_at is not None:
            def stopper() -> None:
                while (left := args.stop_at - time.perf_counter()) > 0:
                    time.sleep(min(left, 0.25))
                sess.request_stop(stop, "benchmark window over")
            threading.Thread(target=stopper, name="bench-stopper", daemon=True).start()
        cap = s.run(stop)
        stats["origin"] = s.origin
        stats["t_end"] = time.perf_counter()
        watcher.join(timeout=2.0)
        stats["window"] = window
        stats["sources"] = {k: [v.state, v.detail, v.tool] for k, v in cap.sources.items()}
        for c in collectors:
            res = None
            try:
                res = c.result()
            except Exception:  # noqa: BLE001 - a collector that cannot report has nothing to add here
                pass
            if isinstance(res, dict) and "cost" in res:
                stats["sampler_cost"] = res["cost"]
            if isinstance(res, dict) and "presentmon" in res:
                pm = res["presentmon"] or {}
                stats["presentmon"] = {k: pm.get(k) for k in ("rows", "malformed", "rejoined", "exit_code",
                                                               "stop_path")}
        stats["tool_pids"] = cap.target.get("tool_pids")
        cap.save(args.capture)
        stats["saved"] = str(args.capture)
    finally:
        if lock is not None:
            lock.release()
        _write(args.stats, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
