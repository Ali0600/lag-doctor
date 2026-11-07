"""What the tests download: PresentMon's pinned release and its gold ETW captures.

Neither is in the repo. `uv run python -m lagdoctor setup` downloads PresentMon (lag-doctor's own download, checked
against lag-doctor's pin) and `uv run python -m tests.downloads fetch-gold` fetches the captures. The fixtures in
conftest.py look for them where those commands put them and check the exe against the pin below, the tests' own: a
changed pin in lagdoctor/setup.py cannot make the tests trust another file.

A PC that never downloaded them skips those tests with the reason. CI downloads both and names them in
LAGDOCTOR_REQUIRE; there a missing one fails those tests instead of skipping them, since a broken download step
would otherwise look like a PC that never downloaded and CI would stay green without running them. Tally counts what
became of those tests, after the run and in CI's job summary.

    uv run python -m tests.downloads fetch-gold          # fetch the gold captures (never replaces a folder)
    uv run python -m tests.downloads check NAME [NAME]   # exit 1 unless each is downloaded and as pinned
    uv run python -m tests.downloads ci-outputs          # CI: the folders and cache keys, into $GITHUB_OUTPUT
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]

PRESENTMON_VERSION = "2.6.0"
PRESENTMON_SHA256 = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"

# Tests/Gold of Intel PresentMon v2.6.0 (MIT licence); the tag is a lightweight tag on this commit.
GOLD_REPO = "https://github.com/GameTechDev/PresentMon.git"
GOLD_COMMIT = "e13fce6acdb55a808fd8318175a56863e532d95f"
GOLD_PATH = "Tests/Gold"
GOLD_FILES = tuple(f"test_case_{n}.{ext}" for n in range(6) for ext in ("etl", "csv"))

REQUIRE_ENV = "LAGDOCTOR_REQUIRE"
DOWNLOADS = {"presentmon": "PresentMon's pinned release", "presentmon_gold": "PresentMon's gold ETW captures"}
# The conftest fixtures that hand them out: a test using one, directly or through another fixture, needs it.
FIXTURES = {"presentmon_exe": "presentmon", "gold_etl_dir": "presentmon_gold"}
# The code `lagdoctor setup --download` runs. CI's cache key for the exe hashes it, so a change to it misses the
# cache and the real download runs again.
DOWNLOAD_CODE = ("lagdoctor/setup.py", "lagdoctor/paths.py", "lagdoctor/cli.py")


class Missing(Exception):
    """Not downloaded: the test skips, unless LAGDOCTOR_REQUIRE names the download."""


class Broken(Exception):
    """Downloaded, but not what the tests pin: the test fails."""


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _localappdata(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def presentmon_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where `lagdoctor setup` puts PresentMon (never under LAGDOCTOR_HOME, which every test redirects)."""
    return (_localappdata(_env(environ)) / "lag-doctor" / "presentmon" / PRESENTMON_VERSION
            / f"PresentMon-{PRESENTMON_VERSION}-x64.exe")


def gold_dir(environ: Mapping[str, str] | None = None) -> Path:
    """LAGDOCTOR_TESTDATA\\presentmon-gold, by default %LOCALAPPDATA%\\lag-doctor\\testdata\\presentmon-gold."""
    environ = _env(environ)
    base = environ.get("LAGDOCTOR_TESTDATA") or _localappdata(environ) / "lag-doctor" / "testdata"
    return Path(base) / "presentmon-gold"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def presentmon(environ: Mapping[str, str] | None = None) -> Path:
    """The pinned exe. Missing when it was never downloaded, Broken when its bytes are not the pinned release's."""
    exe = presentmon_path(environ)
    if not exe.is_file():
        raise Missing(f"PresentMon not downloaded ({exe}); run `uv run python -m lagdoctor setup`")
    digest = sha256(exe)
    if digest != PRESENTMON_SHA256:
        raise Broken(f"{exe} has SHA-256 {digest}, expected the pinned PresentMon {PRESENTMON_VERSION} release")
    return exe


def gold(environ: Mapping[str, str] | None = None) -> Path:
    """The gold captures' folder. Missing when none of the files is there, Broken when only some are."""
    folder = gold_dir(environ)
    absent = [name for name in GOLD_FILES if not (folder / name).is_file()]
    if len(absent) == len(GOLD_FILES):
        raise Missing(f"gold ETL captures not found in {folder}; run `uv run python -m tests.downloads fetch-gold`")
    if absent:
        raise Broken(f"the gold captures in {folder} lack {', '.join(absent)}; delete the folder and run "
                     "`uv run python -m tests.downloads fetch-gold` again")
    return folder


FIND = {"presentmon": presentmon, "presentmon_gold": gold}


def required(environ: Mapping[str, str] | None = None) -> set[str]:
    """The downloads LAGDOCTOR_REQUIRE names. An unknown name is a usage error, never ignored."""
    names = {n.strip() for n in _env(environ).get(REQUIRE_ENV, "").split(",") if n.strip()}
    unknown = sorted(names - DOWNLOADS.keys())
    if unknown:
        raise pytest.UsageError(f"{REQUIRE_ENV} names unknown downloads {unknown}; known: {sorted(DOWNLOADS)}")
    return names


def for_fixture(name: str, environ: Mapping[str, str] | None = None) -> Path:
    """The download `name` for a fixture. Missing: the test skips, or fails when LAGDOCTOR_REQUIRE names it. Broken:
    the test fails."""
    environ = _env(environ)
    try:
        return FIND[name](environ)
    except Missing as exc:
        if name in required(environ):
            pytest.fail(f"{exc}; this run requires it ({REQUIRE_ENV})", pytrace=False)
        pytest.skip(str(exc))
    except Broken as exc:
        pytest.fail(str(exc), pytrace=False)


# ------------------------------------------------------------------ fetching ---

def source_text(repo: str, commit: str) -> str:
    return (f"Gold test captures from Intel PresentMon v{PRESENTMON_VERSION} (MIT licence),\n"
            f"{GOLD_PATH} of {repo}\n"
            f"commit {commit}\n"
            "Used by lag-doctor's tests to replay real ETW data through PresentMon --etl_file without admin rights.\n"
            "Fetched by `uv run python -m tests.downloads fetch-gold`.\n")


def fetch_gold(dest: Path, repo: str = GOLD_REPO, commit: str = GOLD_COMMIT,
               env: Mapping[str, str] | None = None) -> Path:
    """Put GOLD_PATH of `commit` into `dest`: one commit, no other folder's files, every byte as committed (no
    line-ending conversion). Refuses a `dest` that is already there; on failure, nothing is left in its place.

    `commit` must be a full commit id: anything else (a tag, a branch, a short id) fails the check after checkout.
    """
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} is already there; delete it first to fetch again")
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lagdoctor-gold-", ignore_cleanup_errors=True) as tmp:
        clone = Path(tmp)

        def run(*args: str) -> str:
            # No credential helper: a wrong URL must fail, not open a sign-in window.
            done = subprocess.run([git, "-c", "credential.helper=", "-C", str(clone), *args],
                                  env={**_env(env), "GIT_TERMINAL_PROMPT": "0"}, stdin=subprocess.DEVNULL,
                                  capture_output=True, encoding="utf-8", errors="replace", timeout=600)
            if done.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)} failed with exit code {done.returncode}: "
                                   f"{done.stderr.strip()}")
            return done.stdout.strip()

        run("init", "--quiet")
        run("config", "core.autocrlf", "false")          # PresentMon's expected CSVs keep their own line endings
        run("remote", "add", "origin", repo)
        run("sparse-checkout", "set", "--no-cone", f"/{GOLD_PATH}/")
        run("fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", commit)
        run("checkout", "--quiet", "--detach", commit)
        head = run("rev-parse", "HEAD")
        if head != commit:
            raise RuntimeError(f"checked out {head}, not the pinned commit {commit}")
        src = clone / GOLD_PATH
        absent = [name for name in GOLD_FILES if not (src / name).is_file()]
        if absent:
            raise RuntimeError(f"{GOLD_PATH} of commit {commit} lacks {', '.join(absent)}")
        # Copied into a fresh folder beside dest and renamed into place, so dest is either complete or absent.
        staging = Path(tempfile.mkdtemp(prefix=".lagdoctor-gold-", dir=dest.parent))
        try:
            for f in sorted(src.iterdir()):
                if f.is_file():
                    shutil.copyfile(f, staging / f.name)
            (staging / "SOURCE.txt").write_text(source_text(repo, commit), encoding="utf-8", newline="\n")
            os.rename(staging, dest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return dest


# ------------------------------------------------------------------------ CI ---

def ci_outputs(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Where CI's steps find each download, and the cache key for each (the exe's also hashes DOWNLOAD_CODE, the
    captures' also hashes this file, which decides how they are fetched)."""
    code = hashlib.sha256()
    for rel in DOWNLOAD_CODE:
        code.update(rel.encode("utf-8") + b"\0" + (ROOT / rel).read_bytes() + b"\0")
    this = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "presentmon_dir": str(presentmon_path(environ).parent),
        "presentmon_key": f"presentmon-{PRESENTMON_VERSION}-{PRESENTMON_SHA256[:16]}-code-{code.hexdigest()[:16]}",
        "gold_dir": str(gold_dir(environ)),
        "gold_key": f"presentmon-gold-{GOLD_COMMIT[:12]}-{this[:16]}",
    }


def _skip_reason(report) -> str:
    longrepr = report.longrepr
    text = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
    return text.removeprefix("Skipped: ")


def _failure(report) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    text = crash.message if crash is not None else (report.longreprtext.strip().splitlines() or [""])[-1]
    return " ".join(text.split())[:300]


class Tally:
    """What became of the tests that use a download: a line at the end of the run and, when GITHUB_STEP_SUMMARY is
    set (CI), a section of the job summary, so every run shows how many of them ran."""

    ORDER = ("passed", "failed", "error", "skipped")

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = _env(environ)
        self.needing: set[str] = set()
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.collect_only = False

    def pytest_collection_finish(self, session) -> None:
        # After deselection, so what --deselect or -k removed is not counted as never run.
        self.collect_only = bool(session.config.option.collectonly)
        self.needing = {item.nodeid for item in session.items
                        if FIXTURES.keys() & set(getattr(item, "fixturenames", ()))}

    def pytest_runtest_logreport(self, report) -> None:
        if report.nodeid not in self.needing:
            return
        if report.skipped:
            self.outcomes[report.nodeid] = ("skipped", _skip_reason(report))
        elif report.failed:
            self.outcomes[report.nodeid] = ("failed" if report.when == "call" else "error", _failure(report))
        elif report.when == "call":
            self.outcomes[report.nodeid] = ("passed", "")

    def headline(self) -> str:
        counts = Counter(outcome for outcome, _ in self.outcomes.values())
        parts = [f"{counts[o]} {o}" for o in self.ORDER if counts[o]]
        not_run = len(self.needing - self.outcomes.keys())
        if not_run:
            parts.append(f"{not_run} not run")
        n = len(self.needing)
        uses = "test uses" if n == 1 else "tests use"
        return f"{n} {uses} PresentMon's download or its gold captures: {', '.join(parts)}"

    def others(self) -> list[tuple[str, str, str]]:
        """(outcome, test id, why) for every one that did not pass, failures first."""
        rank = {o: i for i, o in enumerate(self.ORDER)}
        return sorted(((o, nodeid, why) for nodeid, (o, why) in self.outcomes.items() if o != "passed"),
                      key=lambda x: (rank[x[0]], x[1]))

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if not self.needing or self.collect_only:
            return
        terminalreporter.write_sep("-", "tests that use a download (tests/downloads.py)")
        terminalreporter.write_line(self.headline())
        for outcome, nodeid, why in self.others():
            terminalreporter.write_line(f"  {outcome}: {nodeid}: {why}")
        summary = self.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(self.markdown())

    def markdown(self) -> str:
        names = sorted(required(self.environ))
        lines = [f"### PresentMon tests (Python {platform.python_version()})", "", f"{self.headline()}.", ""]
        if names:
            lines += [f"{REQUIRE_ENV} = `{','.join(names)}`: a missing download fails these tests here instead of "
                      "skipping them.", ""]
        lines += [f"- {outcome}: `{nodeid}`: {why}" for outcome, nodeid, why in self.others()]
        return "\n".join(lines).rstrip("\n") + "\n\n"


# ----------------------------------------------------------------------- CLI ---

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.downloads",
                                description="Get or check what lag-doctor's tests download.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-gold", help=f"fetch {GOLD_PATH} of PresentMon v{PRESENTMON_VERSION} into {gold_dir()}")
    check = sub.add_parser("check", help="exit 1 unless each named download is there and as pinned")
    check.add_argument("names", nargs="+", choices=sorted(DOWNLOADS))
    sub.add_parser("ci-outputs", help="for CI: append the folders and cache keys to $GITHUB_OUTPUT")
    args = p.parse_args(argv)

    if args.cmd == "fetch-gold":
        try:
            dest = fetch_gold(gold_dir())
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"The gold captures were not fetched: {exc}", file=sys.stderr)
            return 1
        print(f"Fetched {GOLD_PATH} of commit {GOLD_COMMIT} into {dest}")
        return 0

    if args.cmd == "check":
        ok = True
        for name in args.names:
            try:
                where = FIND[name]()
            except (Missing, Broken) as exc:
                print(f"{name}: {exc}", file=sys.stderr)
                ok = False
            else:
                print(f"{name}: {where} is {DOWNLOADS[name]}, as the tests pin it")
        return 0 if ok else 1

    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        print("GITHUB_OUTPUT is not set: ci-outputs is for GitHub Actions", file=sys.stderr)
        return 2
    lines = [f"{key}={value}" for key, value in ci_outputs().items()]
    with open(out, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
