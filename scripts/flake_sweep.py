"""Throwaway (never merged): the tracker test with its ETW session started late, by 0 to 0.95 s.

    python scripts/flake_sweep.py REPEATS [test file]

A runner that is slow to start the session moves the stream against the capture clock's whole seconds. This makes
that happen on purpose: a copy of the test (as it is in this checkout) sleeps `delay` seconds at the top of its session
factory, for each delay from 0 to 0.95 s in 0.05 s steps, REPEATS times each. Prints the outcome per delay; the tally
goes to the job summary. Exits 1 only when the instrument broke (no result for a case).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

NAME = "test_the_tracker_over_a_real_private_etw_session_finds_the_server_and_a_delay_gap"
DELAYS = [round(k * 0.05, 2) for k in range(20)]


def main() -> int:
    repeats = int(sys.argv[1])
    src = Path(sys.argv[2] if len(sys.argv) > 2 else "tests/test_etw_tracker.py").read_text(encoding="utf-8")
    head = f"def {NAME}():\n"
    anchor = "    def factory(_name, _providers, on_event):\n"
    assert src.count(head) == 1 and src.count(anchor) == 1, "the test's shape changed"
    params = [d for d in DELAYS for _ in range(repeats)]
    ids = [f"d{d:.2f}r{i}" for d in DELAYS for i in range(repeats)]
    src = src.replace(head, f"@pytest.mark.parametrize('delay', {params!r}, ids={ids!r})\ndef {NAME}(delay):\n")
    src = src.replace(anchor, anchor + "        time.sleep(delay)\n")
    tmp = Path(tempfile.mkdtemp(prefix="flake-sweep-"))
    (tmp / "test_sweep.py").write_bytes(src.encode("utf-8"))
    xml = tmp / "sweep.xml"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}",
                           "test_sweep.py", "-k", NAME], cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    by_delay: dict[float, list[str]] = defaultdict(list)
    first_error: dict[float, str] = {}
    for case in ET.parse(xml).getroot().iter("testcase"):
        name = case.get("name", "")
        if not name.startswith(NAME + "["):
            continue
        delay = float(name[len(NAME) + 1:].split("]")[0][1:].split("r")[0])
        bad = case.find("failure") if case.find("failure") is not None else case.find("error")
        by_delay[delay].append("failed" if bad is not None else "passed")
        if bad is not None and delay not in first_error:
            first_error[delay] = (bad.get("message") or "").splitlines()[0][:120]
    total = sum(len(v) for v in by_delay.values())
    failed = sum(v.count("failed") for v in by_delay.values())
    lines = [f"| session started late by | runs | failed | first failure |", "|---|---|---|---|"]
    for d in DELAYS:
        got = by_delay.get(d, [])
        lines.append(f"| {d:.2f} s | {len(got)} | {got.count('failed')} | {first_error.get(d, '')} |")
    summary = (f"**phase sweep**: {failed} failed of {total} runs (20 delays x {repeats}); pytest exit "
               f"{proc.returncode}; Python {sys.version.split()[0]}")
    print(summary)
    print("\n".join(lines))
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(summary + chr(10) * 2 + chr(10).join(lines) + chr(10) * 2)
    return 0 if total == len(params) and proc.returncode in (0, 1) else 1


if __name__ == "__main__":
    sys.exit(main())
