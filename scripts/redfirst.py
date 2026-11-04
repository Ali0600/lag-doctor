"""Red-first check: prove each test can fail by sabotaging the code it guards.

Usage:  uv run python scripts/redfirst.py path/to/spec.json

The spec is a JSON list of mutations:
  {"name": "...", "file": "lagdoctor/x.py", "old": "exact text", "new": "replacement",
   "tests": ["tests/test_x.py::test_name", ...]}

For each mutation, independently: the `old` text must occur exactly once; the
file is byte-copied, mutated, and its hash must change; the named tests run
with bytecode writing off; the original bytes are put back and the hash must
match the original, or everything stops. A mutation is CAUGHT only when pytest
ran tests and reported failures (exit code 1). A collection or import error
(exit code 2+) means the sabotage broke loading, which proves nothing, so it is
reported as BROKE-LOAD, not CAUGHT. Exit status is 0 only if every mutation was
caught.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(tests: list[str]) -> tuple[int, str, list[str]]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rf", *tests],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    out = proc.stdout + proc.stderr
    failed = re.findall(r"^FAILED (\S+)", out, flags=re.M)
    summary = next((ln for ln in reversed(out.splitlines()) if re.search(r"\d+ (passed|failed|error)", ln)), "")
    return proc.returncode, summary.strip("= "), failed


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    results = []
    for m in spec:
        path = ROOT / m["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        count = text.count(m["old"])
        if count != 1:
            results.append((m["name"], "BAD-SPEC", f"pattern found {count} times in {m['file']}", []))
            continue
        base_code, base_summary, _ = _run(m["tests"])
        if base_code != 0:
            results.append((m["name"], "BAD-BASE", f"tests not green before sabotage: {base_summary}", []))
            continue
        try:
            path.write_bytes(text.replace(m["old"], m["new"]).encode("utf-8"))
            if _sha(path.read_bytes()) == _sha(original):
                results.append((m["name"], "NOT-APPLIED", "file unchanged after mutation", []))
                continue
            code, summary, failed = _run(m["tests"])
        finally:
            path.write_bytes(original)
            if _sha(path.read_bytes()) != _sha(original):
                print(f"STOP: could not restore {path}; its original bytes are lost from disk", file=sys.stderr)
                return 3
        verdict = "CAUGHT" if code == 1 and failed else ("SURVIVED" if code == 0 else "BROKE-LOAD")
        results.append((m["name"], verdict, f"{summary} (baseline: {base_summary})", failed))

    width = max(len(r[0]) for r in results)
    for name, verdict, info, failed in results:
        print(f"{verdict:11} {name:{width}}  {info}")
        for f in failed:
            print(f"{'':11} {'':{width}}    red: {f}")
    return 0 if all(r[1] == "CAUGHT" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
