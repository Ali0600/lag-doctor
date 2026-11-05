"""End to end: run each LagLab scenario, capture it for real, diagnose it, and judge the diagnosis.

    cd <repo>; uv run python scripts/e2e.py [--modes smooth,hitch,...] [--duration 25] [--nvidia]

The judge (scripts/e2e_truth.py) compares the diagnosis with LagLab's own
ground-truth log, so the expected answer never comes from lag-doctor itself.
Frame timing needs ETW rights: without them this script relaunches itself
once through a UAC prompt, runs there, and prints the verdicts back here.
Nothing on the PC is changed; only LagLab, PresentMon and the samplers run.
Each mode records under the one-recording lock (session.CaptureLock), as
`lagdoctor watch` does: the trace sessions have fixed names, so a run beside a
watch would end that watch's tracing. A busy lock stops the run before LagLab
starts.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_truth import EXPECTATIONS, judge  # noqa: E402

from lagdoctor import paths  # noqa: E402

ORDER = ["smooth", "gpu", "cpu", "hitch", "hitch-io", "hitch-wait", "hog", "pace"]
NETWORK_MODES = {"smooth"}          # one run exercises the real ping + Kernel-Network ETW path
PID_FILE_TIMEOUT_S = 30.0
NVIDIA_SHIM = {"SHIM_MCCOMPAT": "0x800000001"}   # moves an OpenGL app to the NVIDIA GPU (docs/learnings.md)


class Busy(Exception):
    """Another lag-doctor recording holds the one-recording lock, or Windows could not say."""


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--modes", default=",".join(ORDER), help="comma-separated LagLab modes")
    ap.add_argument("--duration", type=float, default=25.0, help="LagLab seconds per mode")
    ap.add_argument("--nvidia", action="store_true", help="run LagLab on the NVIDIA GPU (heavy for the gpu mode)")
    ap.add_argument("--out", type=Path, help="results folder (default: a new one under the sessions folder)")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.mode_list = [m.strip() for m in a.modes.split(",") if m.strip()]
    unknown = [m for m in a.mode_list if m not in EXPECTATIONS]
    if unknown:
        ap.error(f"unknown mode(s) {unknown}; known: {ORDER}")
    if not a.mode_list:                       # also: an empty --modes would be an empty argument when elevated
        ap.error(f"no modes given; known: {ORDER}")
    return a


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    from lagdoctor.winapi import elevation
    # Absolute: the elevated run works in the repo folder, and LAGDOCTOR_HOME may be relative.
    out = (args.out or (paths.sessions_dir() / f"e2e-{time.strftime('%Y%m%d-%H%M%S')}")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not elevation.can_trace():
        if args.child:
            print("The elevated run still cannot start ETW traces; giving up.")
            return 3
        print("Frame timing needs admin rights: approve the UAC prompt. The run happens in a new window;")
        print(f"results come back here. ({len(args.mode_list)} modes, about {len(args.mode_list) * (args.duration + 15):.0f} s)")
        child = [sys.executable, str(Path(__file__).resolve()), "--modes", ",".join(args.mode_list),
                 "--duration", repr(args.duration), "--out", str(out), "--child"] + (["--nvidia"] if args.nvidia else [])
        try:
            code = elevation.relaunch_elevated(child, cwd=str(ROOT))
        except PermissionError:
            print("The UAC prompt was declined; nothing ran.")
            return 3
        except (OSError, ValueError) as exc:
            print(f"The elevated run could not be started ({exc}); nothing ran.")
            return 3
        return print_summary(out, code)
    results = []
    for m in args.mode_list:
        try:
            results.append(run_mode(m, args, out))
        except Busy as exc:
            print(f"\n{exc} The run stops here: {m} and the modes after it did not run.", flush=True)
            results.append({"mode": m, "ok": False, "error": str(exc)})
            (out / "e2e-results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
            break
        except Exception as exc:  # noqa: BLE001 - one broken mode must not hide the others' results
            import traceback
            results.append({"mode": m, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()})
            print(results[-1]["traceback"], flush=True)
        # Written after every mode, so a crash later still leaves these results for the parent window.
        (out / "e2e-results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return print_summary(out, 0)


def take_capture_lock():
    """The one-recording lock, held; Busy (a plain sentence) when another recording runs or Windows cannot say."""
    from lagdoctor.capture.session import CaptureLock
    lock = CaptureLock()
    try:
        if lock.acquire():
            return lock
    except OSError as exc:
        raise Busy(f"Could not check whether another lag-doctor recording is running ({exc}).") from None
    raise Busy("Another lag-doctor recording is running (a `lagdoctor watch`, or its administrator window); "
               "stop it first.")


def run_mode(mode: str, args: argparse.Namespace, out: Path) -> dict:
    from lagdoctor import setup as pm_setup
    from lagdoctor.analysis.diagnose import diagnose
    from lagdoctor.capture.session import Session, SessionOptions
    from lagdoctor.capture.target import find_target
    from lagdoctor.report import html

    print(f"\n=== {mode} ===", flush=True)
    lock = take_capture_lock()                # before LagLab starts: a busy lock leaves nothing running
    try:
        d = out / mode
        d.mkdir(exist_ok=True)
        pid_file, log_file = d / "laglab-pid.json", d / "laglab-log.json"
        for f in (pid_file, log_file):
            f.unlink(missing_ok=True)
        env = dict(os.environ, **(NVIDIA_SHIM if args.nvidia else {}))
        lab = subprocess.Popen([sys.executable, "-m", "laglab", "--mode", mode, "--duration", repr(args.duration),
                                "--pid-file", str(pid_file), "--log", str(log_file)], cwd=str(ROOT), env=env)
        try:
            deadline = time.monotonic() + PID_FILE_TIMEOUT_S
            while not pid_file.is_file():
                if lab.poll() is not None or time.monotonic() > deadline:
                    return {"mode": mode, "ok": False, "error": f"LagLab never started (exit {lab.poll()})"}
                time.sleep(0.1)
            game_pid = json.loads(pid_file.read_text(encoding="utf-8"))["pid"]
            target = find_target(pid=game_pid)
            opts = SessionOptions(duration_s=args.duration + 5.0, network=mode in NETWORK_MODES, hotkey=None,
                                  presentmon_exe=pm_setup.ensure_presentmon(allow_download=False), frames=True)
            cap = Session(target, opts, printer=lambda line: None).run()      # ends when LagLab exits
            lab.wait(timeout=30)
        finally:
            if lab.poll() is None:
                lab.kill()
    finally:
        lock.release()
    log = json.loads(log_file.read_text(encoding="utf-8"))
    cap_path = cap.save(d / "capture.ldcap.json.gz")
    diag = diagnose(cap)
    (d / "diagnosis.json").write_text(json.dumps(diag.to_json(), indent=1), encoding="utf-8")
    html.write(diag, cap, d / "report.html", capture_path=cap_path)
    discrete = any(a.get("integrated") is False and not a.get("software") for a in cap.inventory.get("adapters") or [])
    verdict = judge(mode, log, cap.clock_origin, diag, has_discrete_gpu=discrete)
    result = {
        "mode": mode, "ok": verdict.ok, "renderer": log.get("gl_renderer"),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in verdict.checks],
        "findings": [f.cause for f in diag.findings], "bound": diag.stats.get("bound"),
        "sources": {k: [v.state, v.detail] for k, v in cap.sources.items()},
        "frames": diag.stats.get("frames"), "hog_survivors": log.get("hog_survivors"),
    }
    if log.get("hog_survivors"):
        result["ok"] = False
        result["checks"].append({"name": "no hog processes left behind", "ok": False,
                                 "detail": f"survivors: {log['hog_survivors']}"})
    for c in result["checks"]:
        print(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}", flush=True)
    return result


def print_summary(out: Path, child_code: int) -> int:
    f = out / "e2e-results.json"
    if not f.is_file():
        print(f"No results were written (exit code {child_code}). Folder: {out}")
        return 1
    results = json.loads(f.read_text(encoding="utf-8"))
    print(f"\n{'mode':11} {'verdict':8} {'bound':9} findings / failed checks")
    for r in results:
        failed = [c["name"] for c in r.get("checks", []) if not c["ok"]]
        print(f"{r['mode']:11} {'PASS' if r['ok'] else 'FAIL':8} {str(r.get('bound')):9} "
              f"{', '.join(r.get('findings') or []) or '-'}" + (f"   FAILED: {'; '.join(failed)}" if failed else "")
              + (f"   ERROR: {r['error']}" if r.get("error") else ""))
    passed = sum(1 for r in results if r["ok"])
    print(f"\n{passed}/{len(results)} modes passed. Captures and reports: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
