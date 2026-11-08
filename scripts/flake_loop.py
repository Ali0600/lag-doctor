"""Throwaway (never merged): run one test many times on a runner and count how it ended.

    python scripts/flake_loop.py MODE N [BUSY]

MODE alone: the test as its own pytest process, N times. stress: the same while BUSY processes spin at normal
priority (default: half the CPUs).
suite: the whole suite N times, the test in it (nothing deselected); only the test's own outcome is counted.

Each run's outcome is read from pytest's JUnit XML for that test. A run is FAILED when the test failed, PASSED when
it passed; anything else (no such test in the XML, an exit code other than 0/1, a skip) is an instrument fault,
counted apart, and makes this script exit 1. A failed run prints the end of its output. The tally goes to the job
summary.
"""
from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

TEST = os.environ["TEST"]
FILE, NAME = TEST.split("::")


def _burn(stop_at: float) -> None:
    x = 0
    while time.time() < stop_at:
        x += 1


def _outcome(xml_path: str) -> str:
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        return f"no-xml ({exc})"
    for case in root.iter("testcase"):
        if case.get("name") == NAME:
            if case.find("failure") is not None or case.find("error") is not None:
                return "failed"
            if case.find("skipped") is not None:
                return "skipped"
            return "passed"
    return "absent"


def main() -> int:
    mode, n = sys.argv[1], int(sys.argv[2])
    busy = int(sys.argv[3]) if len(sys.argv) > 3 else max(1, (os.cpu_count() or 2) // 2)
    burners = []
    if mode == "stress":
        stop_at = time.time() + 3600
        for _ in range(busy):
            p = multiprocessing.Process(target=_burn, args=(stop_at,), daemon=True)
            p.start()
            burners.append(p)
    tally = {"passed": 0, "failed": 0}
    faults = []
    durations = []
    tmp = tempfile.mkdtemp(prefix="flake-loop-")
    try:
        for i in range(1, n + 1):
            xml = os.path.join(tmp, f"run-{i}.xml")
            args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
            args += [TEST] if mode in ("alone", "stress") else []
            started = time.perf_counter()
            proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
            took = time.perf_counter() - started
            durations.append(took)
            got = _outcome(xml)
            ok_rc = proc.returncode in (0, 1) if mode == "suite" else proc.returncode == (0 if got == "passed" else 1)
            if got in tally and ok_rc:
                tally[got] += 1
            else:
                faults.append(f"run {i}: outcome {got}, pytest exit {proc.returncode}")
            line = f"{mode} run {i}/{n}: {got} (exit {proc.returncode}, {took:.1f} s)"
            print(line, flush=True)
            if got != "passed":
                lines = proc.stdout.splitlines()
                # The test's own failure block (its header is "_ name _"), not the suite's other output: the
                # failing line and every "E" line under it.
                heads = [k for k, s in enumerate(lines) if s.startswith("_") and NAME in s]
                if heads:
                    block = lines[heads[0]:heads[0] + 200]
                    end = next((k for k, s in enumerate(block[1:], 1) if s.startswith(("_", "=")) and "_ " in s
                                or s.startswith("====")), len(block))
                    keep = [s for s in block[:end] if s.startswith((">", "E "))]
                    print("\n".join(keep[:40]), flush=True)
                else:
                    print("\n".join(lines[-40:]), flush=True)
    finally:
        for p in burners:
            p.terminate()
    label = f"{mode} ({busy} of {os.cpu_count()} CPUs busy)" if mode == "stress" else mode
    summary = (f"**{label}**: {tally['failed']} failed, {tally['passed']} passed of {n} runs "
               f"(instrument faults {len(faults)}); per run {min(durations):.1f}-{max(durations):.1f} s, "
               f"Python {sys.version.split()[0]}")
    print(summary, flush=True)
    for f in faults:
        print("FAULT", f, flush=True)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n\n" + "".join(f"- fault: {f}\n" for f in faults))
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main())
