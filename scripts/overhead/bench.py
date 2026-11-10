"""What lag-doctor's recorder costs a game, measured on LagLab: one long LagLab run with the recorder attached and
detached in turns.

  uv run python scripts/overhead/bench.py cycles --suite NAME [--suite NAME ...] --out DIR [--runs N]
  uv run python scripts/overhead/bench.py summarize-cycles --out DIR

LagLab logs the start of every frame itself, so its frame times do not depend on lag-doctor. After a warm-up (a
laptop's boost window ends inside it), windows of equal length alternate: A with nothing attached, and B,
lag-doctor's recorder (recorder.py: the real target lookup, inventory, session and collectors, without the console),
or in the pm- suites P (PresentMon alone) and F (everything, frame timing included). The order flips every cycle
(rotates, with three conditions), so slow drift in the background cancels out. Over each window, two handle-free
process snapshots give the CPU of LagLab, of the recorder's whole process tree and of the whole machine
(GetSystemTimes), so a window with heavy background load can be seen. No process is ever opened.

The defaults are the runs behind docs/research/overhead.md, section 4.1: 380 s of LagLab, a 60 s warm-up and 10 s
windows, about 6.5 minutes a suite. Run it on a quiet PC: the recorder's own CPU nearly doubled with 13-15 of the 16
logical CPUs busy with other work. The pm- suites need ETW rights (an administrator terminal), and their recorder runs
PresentMon in its own process, as recordings did before the relay, so they do not measure the relay's own cost. Each
window goes to DIR/cycles.jsonl as it ends; summarize-cycles pairs them and writes DIR/cycles-summary.json.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import statistics
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
NVSMI = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"

SUITES = {
    # LagLab uncapped on the Intel GPU: the most frames per second, so the most sensitive to per-frame costs.
    "intel-smooth": {"laglab": ["--mode", "smooth", "--fps-cap", "0"], "env": {}, "conds": "AB"},
    # A CPU-bound main thread (6 ms of busy work a frame): sensitive to CPU time taken from the game.
    "intel-cpu": {"laglab": ["--mode", "cpu", "--target-ms", "6"], "env": {}, "conds": "AB"},
    # Light, capped at 144 fps, on the NVIDIA GPU (where a hybrid laptop's games usually render): NVML runs too.
    "nvidia-144": {"laglab": ["--mode", "smooth", "--fps-cap", "144"], "env": {"SHIM_MCCOMPAT": "0x800000001"},
                   "conds": "AB"},
    # ETW rights only (PresentMon needs them): A none, P PresentMon alone, F everything.
    "pm-intel-smooth": {"laglab": ["--mode", "smooth", "--fps-cap", "0"], "env": {}, "conds": "APF"},
    "pm-nvidia-144": {"laglab": ["--mode", "smooth", "--fps-cap", "144"], "env": {"SHIM_MCCOMPAT": "0x800000001"},
                      "conds": "APF"},
}
# recorder.py's arguments per condition; None: nothing attached. With --frames the recorder runs the PresentMon that
# lag-doctor itself would (setup.expected_exe), which cannot_run checks first.
RECORDER_ARGS: dict[str, list[str] | None] = {"A": None, "B": [], "P": ["--frames", "--only", "frames"],
                                              "F": ["--frames"]}
# An attached window whose recording had begun less than this before it is left out with its pair: the session's own
# start-up would fall inside it.
MIN_ORIGIN_LEAD_S = 1.0
ETW_SESSIONS = ("lagdoctor", "lag-doctor-net")     # PresentMon's session, the game-server tracker's
METRICS = ("fps", "mean_ms", "median_ms", "p99_ms", "p999_ms", "max_ms", "stutter2x_per_min", "stutter4x_per_min",
           "over_10ms", "over_20ms")
# Compared with A, pair by pair: (metric, True for a change in % of A's value, False for a plain difference).
PAIRED = (("mean_ms", True), ("fps", True), ("p99_ms", False), ("p999_ms", False), ("max_ms", False),
          ("stutter2x_per_min", False), ("stutter4x_per_min", False), ("over_10ms", False))

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def system_times() -> tuple[float, float, float]:
    idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    f = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7  # noqa: E731
    return f(idle), f(kern), f(user)


def host_state() -> dict:
    """What else could move the numbers: mains power, the power plan, a locked desktop, the NVIDIA GPU's state."""
    out: dict = {}
    ps = _PowerStatus()
    if _k32.GetSystemPowerStatus(ctypes.byref(ps)):
        out["ac"] = {0: False, 1: True}.get(ps.ACLineStatus)
        out["battery_pct"] = ps.BatteryLifePercent if ps.BatteryLifePercent != 255 else None
    try:
        from lagdoctor.winapi import power
        out["scheme"] = power.active_scheme()[1]
    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
        out["scheme"] = f"? {exc}"
    _u32.OpenInputDesktop.restype = wintypes.HANDLE
    h = _u32.OpenInputDesktop(0, False, 0x0100)
    out["desktop_locked"] = not h
    if h:
        _u32.CloseDesktop(wintypes.HANDLE(h))
    try:
        r = subprocess.run([str(NVSMI), "--query-gpu=pstate,utilization.gpu,power.draw,temperature.gpu",
                            "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        out["nvidia"] = r.stdout.strip()
    except Exception as exc:  # noqa: BLE001 - no NVIDIA GPU, or no driver
        out["nvidia"] = f"? {exc}"
    return out


_snapshotter = None


def snapshot() -> dict:
    """pid -> process record for every process, from NtQuerySystemInformation: no handle to any process."""
    global _snapshotter
    from lagdoctor.winapi import ntquery
    if _snapshotter is None:
        _snapshotter = ntquery.Snapshotter()
    return _snapshotter.snapshot()


def tree(procs: dict, pid: int) -> list:
    """The process `pid` and every descendant, by parent PID, a child never older than its parent (so a process whose
    parent PID was reused by the recorder is not counted)."""
    root = procs.get(pid)
    if root is None:
        return []
    out, frontier = [root], [root]
    while frontier:
        nxt = []
        for parent in frontier:
            for p in procs.values():
                if p.ppid == parent.pid and p.create_time >= parent.create_time and p.pid != parent.pid \
                        and p not in out:
                    nxt.append(p)
        out.extend(nxt)
        frontier = nxt
    return out


def cpu_s(p) -> float:
    return (p.user_100ns + p.kernel_100ns) / 1e7


def wait_file(path: Path, timeout: float, proc: subprocess.Popen) -> None:
    deadline = time.perf_counter() + timeout
    while not path.exists():
        if proc.poll() is not None:
            raise RuntimeError(f"LagLab exited with {proc.returncode} before writing {path.name}")
        if time.perf_counter() > deadline:
            raise TimeoutError(f"{path.name} did not appear within {timeout:.0f} s")
        time.sleep(0.05)


def sleep_until(t: float) -> None:
    while True:
        left = t - time.perf_counter()
        if left <= 0:
            return
        time.sleep(min(left, 0.25))


def rolling_median(xs: list[float], k: int = 31) -> list[float]:
    h = k // 2
    return [statistics.median(xs[max(0, i - h): i + h + 1]) for i in range(len(xs))]


def frame_metrics(frames: list[float], ws: float, we: float) -> dict:
    """Frame-time figures for the frames that start inside [ws, we] (LagLab's frame start times, in seconds)."""
    f = [x for x in frames if ws <= x <= we]
    d = [(b - a) * 1000.0 for a, b in zip(f, f[1:])]
    if len(d) < 50:
        return {"frames": len(d), "error": "too few frames in the window"}
    s = sorted(d)
    q = lambda p: s[min(len(s) - 1, int(p * len(s)))]  # noqa: E731
    med = rolling_median(d) if len(d) <= 20000 else None
    minutes = (f[-1] - f[0]) / 60.0
    out = {"frames": len(d), "fps": len(d) / (f[-1] - f[0]), "mean_ms": statistics.fmean(d),
           "median_ms": q(0.5), "p90_ms": q(0.9), "p99_ms": q(0.99), "p999_ms": q(0.999), "max_ms": s[-1],
           "sd_ms": statistics.pstdev(d)}
    if med is not None:
        out["stutter2x_per_min"] = sum(1 for x, m in zip(d, med) if x > 2 * m) / minutes
        out["stutter4x_per_min"] = sum(1 for x, m in zip(d, med) if x > 4 * m) / minutes
    else:  # uncapped runs can hold 50k+ frames: a coarser local median keeps this fast
        blocks = [statistics.median(d[i:i + 31]) for i in range(0, len(d), 31)]
        out["stutter2x_per_min"] = sum(1 for i, x in enumerate(d) if x > 2 * blocks[i // 31]) / minutes
        out["stutter4x_per_min"] = sum(1 for i, x in enumerate(d) if x > 4 * blocks[i // 31]) / minutes
    out["over_10ms"] = sum(1 for x in d if x > 10.0)
    out["over_20ms"] = sum(1 for x in d if x > 20.0)
    return out


def summarize_recorder(rs: dict) -> dict:
    """The recorder's own stats file, cut to what the summary reads: its CPU per thread name over the window."""
    w = rs.get("window") or {}
    out = {k: rs.get(k) for k in ("find_target_s", "inventory_s", "adapter_luid", "collectors", "origin",
                                  "sampler_cost", "presentmon", "error")}
    out["sources"] = {k: v[:2] for k, v in (rs.get("sources") or {}).items()}
    if "start" in w and "end" in w:
        a, b = w["start"], w["end"]
        wall = b["at"] - a["at"]
        out["window_wall_s"] = wall
        out["proc_cpu_pct"] = 100 * (b["proc_cpu_s"] - a["proc_cpu_s"]) / wall
        out["ws_mb_end"] = b["ws_bytes"] / 2**20
        out["private_mb_end"] = b["private_bytes"] / 2**20
        out["private_mb_growth"] = (b["private_bytes"] - a["private_bytes"]) / 2**20
        by_name: dict[str, float] = {}
        for tid, tb in b["threads"].items():
            ta = a["threads"].get(tid)
            c0 = ta["cpu_s"] if ta and ta["create_time"] == tb["create_time"] else 0.0
            name = tb["name"] or ta and ta["name"] or "(native, unnamed)"
            if name.startswith("lagdoctor-ping-"):
                name = "lagdoctor-ping-* (all workers)"
            by_name[name] = by_name.get(name, 0.0) + 100 * (tb["cpu_s"] - c0) / wall
        out["threads_cpu_pct"] = dict(sorted(by_name.items(), key=lambda kv: -kv[1]))
    else:
        out["window_error"] = {k: v for k, v in w.items() if k.endswith("error")} or "no window snapshots"
    return out


def etw_stats(name: str) -> dict:
    """A running ETW session's counters by name (ControlTraceW QUERY; needs ETW rights)."""
    from lagdoctor.winapi import etw
    props = etw._Properties()
    rc = int(etw._advapi32.ControlTraceW(0, name, props.ptr(), etw.EVENT_TRACE_CONTROL_QUERY))
    if rc != 0:
        return {"rc": rc}
    out = props.stats()
    out["buffer_kb"] = int(props.p.BufferSize)
    out["flush_timer_s"] = int(props.p.FlushTimer)
    return out


def measure(ws: float, we: float, game_pid: int, launcher_pid: int | None, etw_names: tuple = ()) -> dict:
    """CPU of the machine, LagLab and the recorder's process tree over [ws, we], from two handle-free snapshots."""
    sleep_until(ws)
    e0 = {n: etw_stats(n) for n in etw_names}
    s0, p0 = system_times(), snapshot()
    t0 = time.perf_counter()
    sleep_until(we)
    s1, p1 = system_times(), snapshot()
    wall = time.perf_counter() - t0
    e1 = {n: etw_stats(n) for n in etw_names}
    busy = ((s1[1] - s0[1]) - (s1[0] - s0[0]) + (s1[2] - s0[2])) / wall
    acct: dict = {"wall_s": wall, "machine_busy_cpus": busy, "late_s": t0 - ws}
    g0, g1 = p0.get(game_pid), p1.get(game_pid)
    acct["laglab_cpu_pct"] = 100 * (cpu_s(g1) - cpu_s(g0)) / wall if g0 and g1 else None
    acct["recorder_cpu_pct"] = 0.0
    if launcher_pid is not None:
        start_cpu = {(p.pid, p.create_time): cpu_s(p) for p in tree(p0, launcher_pid)}
        procs = []
        for p in tree(p1, launcher_pid):
            c0 = start_cpu.get((p.pid, p.create_time), 0.0)
            procs.append({"pid": p.pid, "name": p.name, "cpu_pct": 100 * (cpu_s(p) - c0) / wall,
                          "ws_mb": p.ws_bytes / 2**20, "private_mb": p.private_bytes / 2**20,
                          "new_in_window": (p.pid, p.create_time) not in start_cpu})
        acct["recorder_tree"] = procs
        acct["recorder_cpu_pct"] = sum(x["cpu_pct"] for x in procs)
    acct["background_cpus"] = busy - (acct["laglab_cpu_pct"] or 0) / 100 - acct["recorder_cpu_pct"] / 100
    if etw_names:
        acct["etw"] = {}
        for n in etw_names:
            a, b = e0.get(n, {}), e1.get(n, {})
            if "buffers_written" in a and "buffers_written" in b:
                acct["etw"][n] = {"buffers_per_s": (b["buffers_written"] - a["buffers_written"]) / wall,
                                  "kb_per_s_upper": (b["buffers_written"] - a["buffers_written"]) * b["buffer_kb"]
                                  / wall, "events_lost": b["events_lost"] - a["events_lost"],
                                  "buffers_lost": b["buffers_lost"] - a["buffers_lost"], "buffer_kb": b["buffer_kb"],
                                  "flush_timer_s": b["flush_timer_s"], "buffers": b["buffers"]}
            else:
                acct["etw"][n] = {"start": a, "end": b}
    return acct


def order_for(conds: str, i: int) -> str:
    """The order of cycle i's windows: AB then BA (ABBA blocks) for two conditions, a rotation for three."""
    if len(conds) == 2:
        return conds if i % 2 == 0 else conds[::-1]
    k = i % len(conds)
    return conds[k:] + conds[:k]


def run_cycles(suite: str, idx: int, out: Path, duration: float, warm: float, win: float, startup: float,
               gap: float) -> list[dict]:
    """One long LagLab run; the recorder is attached and detached in turns: windows A (none) and B/P/F
    (attached) of equal length alternate, the order flipping each cycle, so slow background drift cancels."""
    spec = SUITES[suite]
    conds = spec["conds"]
    tag = f"{suite}-cyc{idx:02d}"
    pf, log = out / f"{tag}.pid.json", out / f"{tag}.laglab.json"
    env = os.environ.copy()
    env.update(spec["env"])
    windows: list[dict] = []
    host0 = host_state()
    with open(out / f"{tag}.laglab.out", "w", encoding="utf-8") as lag_out:
        lag = subprocess.Popen([PY, "-m", "laglab", *spec["laglab"], "--duration", str(duration),
                                "--pid-file", str(pf), "--log", str(log)], env=env, stdout=lag_out,
                               stderr=subprocess.STDOUT)
        live: subprocess.Popen | None = None
        try:
            wait_file(pf, 30.0, lag)
            first = time.perf_counter()
            game_pid = json.loads(pf.read_text(encoding="utf-8"))["pid"]
            end = first + duration - 2.0
            t = first + warm
            k = 0
            while True:
                order = order_for(conds, k)
                need = sum(win if c == "A" else startup + win + 1.5 + gap for c in order)
                if t + need > end:
                    break
                for cond in order:
                    w: dict = {"suite": suite, "run": idx, "cycle": k, "cond": cond, "game_pid": game_pid}
                    if RECORDER_ARGS[cond] is None:
                        w.update(ws=t, we=t + win)
                        w["acct"] = measure(t, t + win, game_pid, None)
                        t = t + win
                    else:
                        wtag = f"{tag}-k{k:02d}-{cond}"
                        cap, st = out / f"{wtag}.ldcap.json.gz", out / f"{wtag}.rec.json"
                        ws, we = t + startup, t + startup + win
                        w.update(ws=ws, we=we, capture=str(cap), stats=str(st))
                        with open(out / f"{wtag}.rec.out", "w", encoding="utf-8") as rec_out:
                            live = subprocess.Popen(
                                [PY, str(HERE / "recorder.py"), "--pid", str(game_pid), "--capture", str(cap),
                                 "--stats", str(st), "--win-start", repr(ws), "--win-end", repr(we),
                                 "--stop-at", repr(we), *RECORDER_ARGS[cond]],
                                stdout=rec_out, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                            w["acct"] = measure(ws, we, game_pid, live.pid,
                                                ETW_SESSIONS if cond in "PF" else ())
                            live.wait(timeout=60)
                            w["recorder_exit"] = live.returncode
                            live = None
                        if st.exists():
                            rs = json.loads(st.read_text(encoding="utf-8"))
                            w["recorder"] = summarize_recorder(rs)
                            origin = rs.get("origin")
                            w["origin_lead_s"] = (ws - origin) if origin else None
                        t = time.perf_counter() + gap
                    windows.append(w)
                    print(f"    cycle {k} {cond}: recorder {w['acct']['recorder_cpu_pct']:.2f}%  "
                          f"background {w['acct']['background_cpus']:.2f} CPUs  late {w['acct']['late_s']:.3f} s",
                          flush=True)
                k += 1
            lag.wait(timeout=duration + 60)
        finally:
            for p in (lag, live):
                if p is not None and p.poll() is None:
                    p.kill()
    data = json.loads(log.read_text(encoding="utf-8"))
    for w in windows:
        w["renderer"] = data.get("gl_renderer")
        w["metrics"] = frame_metrics(data["frames"], w["ws"], w["we"])
        w["host0"] = host0
    return windows


def cannot_run(suite: str) -> str | None:
    """Why `suite` cannot run here, or None: the pm- suites need ETW rights and lag-doctor's PresentMon."""
    if not any(c in "PF" for c in SUITES[suite]["conds"]):
        return None
    from lagdoctor import setup
    from lagdoctor.winapi import elevation
    if not elevation.can_trace():
        return "needs ETW rights (an administrator terminal)"
    exe = setup.expected_exe()
    if not exe.is_file():
        return f"PresentMon not found at {exe}"
    return None


def cmd_cycles(args: argparse.Namespace) -> int:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    rc = 0
    for suite in args.suite:
        why = cannot_run(suite)
        if why:
            print(f"{suite}: {why}; skipped", flush=True)
            rc = 2
            continue
        for i in range(args.runs):
            print(f"{time.strftime('%H:%M:%S')} {suite} run {i + 1}/{args.runs} ({args.duration:.0f} s of LagLab)",
                  flush=True)
            try:
                ws = run_cycles(suite, i, out, args.duration, args.warm, args.win, args.startup, args.gap)
            except Exception as exc:  # noqa: BLE001 - a failed run is recorded and the rest go on
                ws = [{"suite": suite, "run": i, "error": f"{type(exc).__name__}: {exc}"}]
                print(f"    failed: {ws[0]['error']}", flush=True)
                rc = rc or 1
            with open(out / "cycles.jsonl", "a", encoding="utf-8") as fh:
                for w in ws:
                    fh.write(json.dumps(w, default=str) + "\n")
            time.sleep(args.gap)
    return rc


def boot_ci(xs: list[float], n: int = 10000, seed: int = 7) -> tuple[float, float]:
    """A 95 % bootstrap interval for the median of xs (seeded, so a summary reads the same every time)."""
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(xs, k=len(xs))) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n) - 1]


def why_left_out(r: dict) -> str | None:
    """Why a window cannot be used, or None. An attached window also needs its recorder to have exited cleanly and
    its recording to have begun at least MIN_ORIGIN_LEAD_S before the window: else it measured no recorder, or one
    still starting."""
    if "error" in r:
        return r["error"]
    if "error" in (r.get("metrics") or {}):
        return r["metrics"]["error"]
    if r.get("cond") != "A":
        if r.get("recorder_exit") != 0:
            return f"recorder exit {r.get('recorder_exit')}"
        lead = r.get("origin_lead_s")
        if lead is None or lead < MIN_ORIGIN_LEAD_S:
            return f"origin lead {lead}"
    return None


def summarize_cycles(recs: list[dict]) -> dict:
    """Per suite: each condition's medians, and each attached condition against A, cycle by cycle (median change and a
    bootstrap interval), plus the noise floor between consecutive A windows."""
    report: dict = {}
    for suite in dict.fromkeys(r["suite"] for r in recs):
        rs = [r for r in recs if r["suite"] == suite]
        conds = SUITES[suite]["conds"]
        reasons = [(r, why_left_out(r)) for r in rs]
        good = [r for r, why in reasons if why is None]
        key = lambda r: (r["run"], r["cycle"])  # noqa: E731
        by = {c: {key(r): r for r in good if r["cond"] == c} for c in conds}
        s: dict = {"windows": len(rs),
                   "excluded": [(r.get("run"), r.get("cycle"), r.get("cond"), why) for r, why in reasons if why],
                   "renderers": sorted({r.get("renderer") or "?" for r in good})}
        for c in conds:
            ws_ = list(by[c].values())
            if not ws_:
                continue
            col = lambda k: [r["metrics"][k] for r in ws_ if k in r["metrics"]]  # noqa: E731
            s[c] = {k: round(statistics.median(col(k)), 4) for k in METRICS if col(k)}
            s[c]["n"] = len(ws_)
            s[c]["recorder_cpu_pct_median"] = round(statistics.median([r["acct"]["recorder_cpu_pct"] for r in ws_]), 3)
            s[c]["background_cpus"] = [round(r["acct"]["background_cpus"], 2) for r in ws_]
            s[c]["laglab_cpu_pct_median"] = round(statistics.median([r["acct"]["laglab_cpu_pct"] or 0 for r in ws_]), 1)
            recs_c = [r["recorder"] for r in ws_ if r.get("recorder")]
            if recs_c:
                names = dict.fromkeys(n for x in recs_c for n in x.get("threads_cpu_pct", {}))
                s[c]["threads_cpu_pct_median"] = {n: round(statistics.median(
                    [x.get("threads_cpu_pct", {}).get(n, 0.0) for x in recs_c]), 3) for n in names}
                proc = [x["proc_cpu_pct"] for x in recs_c if "proc_cpu_pct" in x]
                if proc:
                    s[c]["recorder_proc_cpu_pct_median"] = round(statistics.median(proc), 3)
                private = [x["private_mb_end"] for x in recs_c if "private_mb_end" in x]
                if private:
                    s[c]["private_mb_end_median"] = round(statistics.median(private), 1)
                ws_mb = [x["ws_mb_end"] for x in recs_c if "ws_mb_end" in x]
                if ws_mb:
                    s[c]["ws_mb_end_median"] = round(statistics.median(ws_mb), 1)
                trees = [r["acct"].get("recorder_tree") or [] for r in ws_]
                pm = [sum(p["cpu_pct"] for p in t if "presentmon" in p["name"].lower()) for t in trees]
                if any(pm):
                    s[c]["presentmon_cpu_pct_median"] = round(statistics.median(pm), 3)
                    s[c]["presentmon_ws_mb_median"] = round(statistics.median(
                        [sum(p["ws_mb"] for p in t if "presentmon" in p["name"].lower()) for t in trees]), 1)
                s[c]["sampler_cost_examples"] = [x.get("sampler_cost") for x in recs_c][:3]
                s[c]["collectors"] = recs_c[0].get("collectors")
                s[c]["sources"] = recs_c[0].get("sources")
        base = by.get("A", {})
        for c in conds:
            if c == "A":
                continue
            ks = sorted(set(base) & set(by[c]))
            if len(ks) < 2:
                continue
            pairs = [(base[k_], by[c][k_]) for k_ in ks]
            d: dict = {"pairs": len(pairs)}
            for k_, rel in PAIRED:
                if not all(k_ in a["metrics"] and k_ in b["metrics"] for a, b in pairs):
                    continue
                xs = [(b["metrics"][k_] - a["metrics"][k_]) / a["metrics"][k_] * 100 if rel
                      else b["metrics"][k_] - a["metrics"][k_] for a, b in pairs]
                lo, hi = boot_ci(xs)
                d[k_ + ("_change_pct" if rel else "_change")] = {
                    "median": round(statistics.median(xs), 4), "ci95": [round(lo, 4), round(hi, 4)],
                    "each": [round(x, 3) for x in xs]}
            bg = [b["acct"]["background_cpus"] - a["acct"]["background_cpus"] for a, b in pairs]
            d["background_cpus_change_each"] = [round(x, 2) for x in bg]
            s[f"{c}_vs_A"] = d
        aks = sorted(base)
        if len(aks) >= 3:
            aa = [(base[j]["metrics"]["mean_ms"] - base[i]["metrics"]["mean_ms"]) / base[i]["metrics"]["mean_ms"] * 100
                  for i, j in zip(aks, aks[1:])]
            s["A_vs_next_A_mean_ms_change_pct"] = {"median_abs": round(statistics.median([abs(x) for x in aa]), 4),
                                                   "each": [round(x, 3) for x in aa]}
        report[suite] = s
    return report


def cmd_summarize_cycles(args: argparse.Namespace) -> int:
    path = args.out / "cycles.jsonl"
    if not path.is_file():
        print(f"no cycles.jsonl in {args.out}: run cycles first", file=sys.stderr)
        return 2
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    text = json.dumps(summarize_cycles(recs), indent=1)
    (args.out / "cycles-summary.json").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycles", help="run the suites, appending each window to OUT/cycles.jsonl")
    c.add_argument("--suite", action="append", choices=sorted(SUITES), required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--runs", type=int, default=1, help="LagLab runs per suite")
    c.add_argument("--duration", type=float, default=380.0, help="seconds of LagLab per run (default 380)")
    c.add_argument("--warm", type=float, default=60.0,
                   help="seconds after the first frame before the first window (default 60: a laptop's boost "
                        "window must end first)")
    c.add_argument("--win", type=float, default=10.0, help="window length, every condition (default 10)")
    c.add_argument("--startup", type=float, default=6.5, help="time the recorder gets to start before its window")
    c.add_argument("--gap", type=float, default=2.0, help="pause after the recorder exits")
    sc = sub.add_parser("summarize-cycles", help="pair OUT/cycles.jsonl into OUT/cycles-summary.json")
    sc.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    return {"cycles": cmd_cycles, "summarize-cycles": cmd_summarize_cycles}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
