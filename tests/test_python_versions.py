"""Which Python lag-doctor promises, tests and gives a checkout: the one its installer ships (docs/DECISIONS.md,
2026-11-08).

The MSI ships python.org's embeddable runtime, pinned in scripts/runtime_pin.py. CI's test matrix must hold that minor
version; a checkout's .python-version must name it, so uv builds a new venv on it rather than on the first Python it
finds (a new worktree here got uv's own 3.12, which CI never ran); and the oldest version pyproject.toml's
requires-python promises must be the oldest CI tests. When runtime_pin.py moves to a new minor version, these fail
until the matrix and .python-version follow it.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_pin  # noqa: E402


def minor(version: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version)
    assert m, f"{version!r} is not a Python version"
    return int(m[1]), int(m[2])


def matrix() -> list[tuple[int, int]]:
    """The minor versions of the test job's `python:` matrix in ci.yml."""
    lines = re.findall(r"^        python: \[([^\]\n]*)\]$", CI.read_text(encoding="utf-8"), re.M)
    assert len(lines) == 1, f"ci.yml has {len(lines)} `python:` matrix lines, not 1"
    entries = [e.strip() for e in lines[0].split(",")]
    assert entries and all(re.fullmatch(r'"\d+\.\d+"', e) for e in entries), (
        f"each matrix entry is a quoted minor version such as \"3.13\": {entries}")
    return [minor(e.strip('"')) for e in entries]


def shipped() -> tuple[int, int]:
    return minor(runtime_pin.EMBED_VERSION)


def test_ci_tests_the_python_the_installer_ships():
    assert shipped() in matrix(), (
        f"the installer ships Python {runtime_pin.EMBED_VERSION}; add \"{'.'.join(map(str, shipped()))}\" to ci.yml's "
        f"matrix {matrix()} in the same change that moved scripts/runtime_pin.py")


def test_a_checkout_builds_its_venv_on_the_python_the_installer_ships():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert minor(pinned) == shipped() and pinned.count(".") == 1, (
        f".python-version says {pinned!r}; the installer ships {runtime_pin.EMBED_VERSION}")


def test_the_oldest_python_promised_is_the_oldest_ci_tests():
    promise = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    m = re.fullmatch(r">=(\d+\.\d+)", promise.replace(" ", ""))
    assert m, f"requires-python {promise!r} is not a plain lower bound"
    assert minor(m[1]) == min(matrix()), f"requires-python {promise!r}, but CI's oldest is {min(matrix())}"


def test_the_version_reader_refuses_what_it_cannot_read():
    assert minor("3.13.17") == minor("3.13") == (3, 13)
    for bad in ("3", "3.13.17.1", "3.13t", "py3.13", ""):
        with pytest.raises(AssertionError):
            minor(bad)
