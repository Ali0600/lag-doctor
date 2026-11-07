"""tests/downloads.py: the tests' pins, the skip-or-fail switch CI turns on, the gold fetch, CI's cache keys, the tally.

The fixtures are driven through a child pytest that loads the real conftest.py, so what is tested is what a test run
does. The gold fetch runs against a local stand-in for PresentMon's repo: no network.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests import downloads as dl

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"


# --------------------------------------------------------------- the finders ---

def test_the_tests_pin_the_v2_6_0_release_and_tag():
    # The release record and the tag (tests/fixtures/presentmon/README.md); independent of lagdoctor/setup.py.
    assert dl.PRESENTMON_SHA256 == "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"
    assert dl.GOLD_COMMIT == "e13fce6acdb55a808fd8318175a56863e532d95f"
    assert dl.GOLD_REPO == "https://github.com/GameTechDev/PresentMon.git" and dl.GOLD_PATH == "Tests/Gold"


def test_the_folders_are_where_setup_and_fetch_gold_put_them():
    env = {"LOCALAPPDATA": r"C:\L"}
    assert dl.presentmon_path(env) == Path(r"C:\L\lag-doctor\presentmon\2.6.0\PresentMon-2.6.0-x64.exe")
    assert dl.gold_dir(env) == Path(r"C:\L\lag-doctor\testdata\presentmon-gold")
    assert dl.gold_dir({**env, "LAGDOCTOR_TESTDATA": r"D:\T"}) == Path(r"D:\T\presentmon-gold")


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_missing_exe_is_missing_and_says_how_to_get_it(tmp_path):
    with pytest.raises(dl.Missing, match="lagdoctor setup"):
        dl.presentmon({"LOCALAPPDATA": str(tmp_path)})


def test_an_exe_with_other_bytes_is_broken_and_one_with_the_pinned_bytes_is_returned(tmp_path, monkeypatch):
    env = {"LOCALAPPDATA": str(tmp_path)}
    exe = put(dl.presentmon_path(env), b"not presentmon")
    with pytest.raises(dl.Broken, match="expected the pinned PresentMon 2.6.0"):
        dl.presentmon(env)
    monkeypatch.setattr(dl, "PRESENTMON_SHA256", dl.sha256(exe))
    assert dl.presentmon(env) == exe


def test_gold_is_missing_with_none_of_its_files_broken_with_some_and_found_with_all(tmp_path):
    env = {"LAGDOCTOR_TESTDATA": str(tmp_path)}
    folder = dl.gold_dir(env)
    folder.mkdir(parents=True)
    put(folder / "SOURCE.txt", b"not a capture")
    with pytest.raises(dl.Missing, match="fetch-gold"):
        dl.gold(env)
    for name in dl.GOLD_FILES[:-1]:
        put(folder / name, b"x")
    with pytest.raises(dl.Broken, match=re.escape(dl.GOLD_FILES[-1])):
        dl.gold(env)
    put(folder / dl.GOLD_FILES[-1], b"x")
    assert dl.gold(env) == folder


def test_the_gold_files_are_the_six_captures_and_their_expected_csvs():
    assert sorted(dl.GOLD_FILES) == sorted(f"test_case_{n}.{e}" for n in range(6) for e in ("etl", "csv"))


# ------------------------------------------------------- skip, or fail in CI ---

def test_nothing_required_unless_named():
    assert dl.required({}) == set() and dl.required({dl.REQUIRE_ENV: " , "}) == set()
    assert dl.required({dl.REQUIRE_ENV: "presentmon, presentmon_gold"}) == {"presentmon", "presentmon_gold"}


def test_a_misspelt_requirement_is_a_usage_error():
    with pytest.raises(pytest.UsageError, match="presentmon-gold"):
        dl.required({dl.REQUIRE_ENV: "presentmon,presentmon-gold"})


def outcome(name: str, env: dict) -> tuple[str, str]:
    """What for_fixture did: ("skip" or "fail", its message). Caught here, so a stray skip fails the test rather
    than skipping it (pytest.raises(pytest.fail.Exception) lets a skip through)."""
    try:
        dl.for_fixture(name, env)
    except pytest.skip.Exception as exc:
        return "skip", str(exc)
    except pytest.fail.Exception as exc:
        return "fail", str(exc)
    return "returned", ""


def test_a_missing_download_skips_unless_required_then_fails(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    kind, why = outcome("presentmon", env)
    assert kind == "skip" and "PresentMon not downloaded" in why, (kind, why)
    kind, why = outcome("presentmon_gold", {**env, dl.REQUIRE_ENV: "presentmon"})       # only what is named fails
    assert kind == "skip" and "gold ETL captures not found" in why, (kind, why)
    kind, why = outcome("presentmon", {**env, dl.REQUIRE_ENV: "presentmon"})
    assert kind == "fail" and "PresentMon not downloaded" in why and dl.REQUIRE_ENV in why, (kind, why)


def test_a_broken_download_fails_even_when_not_required(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    put(dl.presentmon_path(env), b"not presentmon")
    kind, why = outcome("presentmon", env)
    assert kind == "fail" and "SHA-256" in why, (kind, why)


# ------------------------------------------- the fixtures, through pytest itself ---

PROBES = '''\
import pytest


@pytest.fixture
def replay(presentmon_exe):
    return presentmon_exe


def test_exe(presentmon_exe):
    assert presentmon_exe.name == "PresentMon-2.6.0-x64.exe"


def test_gold(gold_etl_dir):
    assert (gold_etl_dir / "test_case_0.etl").is_file()


def test_through_a_fixture(replay):
    assert replay.is_file()


def test_plain():
    pass
'''
# Settings a child run must not inherit from this one (CI sets several of them).
CLEARED = (dl.REQUIRE_ENV, "LAGDOCTOR_TESTDATA", "LAGDOCTOR_HOME", "LAGDOCTOR_ABSENT", "GITHUB_STEP_SUMMARY",
           "PYTEST_ADDOPTS", "PYTEST_PLUGINS")


def run_probes(tmp_path: Path, **env: str) -> tuple[int, dict[str, tuple[str, str]], str, str]:
    """Run PROBES in a child pytest that loads the real conftest.py, with %LOCALAPPDATA% in tmp_path unless `env`
    says otherwise. Returns (exit code, {probe: (outcome, message)}, output, what it added to the job summary)."""
    probe_dir, local = tmp_path / "probe", tmp_path / "local"
    probe_dir.mkdir()
    local.mkdir(exist_ok=True)
    (probe_dir / "test_probe.py").write_text(PROBES, encoding="utf-8")
    summary, report = tmp_path / "summary.md", tmp_path / "report.xml"
    # The child's own temp folders stay in tmp_path, as test_redfirst.py does for its runs. In the shared
    # %TEMP%\pytest-of-<user>, each child run adds a numbered folder and pushes this suite's out of the three pytest
    # keeps, so the next pytest run deletes the suite's whole folder. Measured on CI (2026-11-07): with the child
    # folders there, the timing-sensitive ETW test that runs after the suite was about 2 s slower and failed 3 times
    # in 16 runs; with them here, 12 of 12 passed at its usual 3.3 s.
    temp = tmp_path / "temp"
    temp.mkdir()
    child_env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    child_env.update({"PYTHONPATH": str(ROOT), "LOCALAPPDATA": str(local), "GITHUB_STEP_SUMMARY": str(summary),
                      "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)}, **env)
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider", "-q",
                           f"--junitxml={report}", str(probe_dir)], cwd=probe_dir, env=child_env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 4 or list(temp.glob("pytest-of-*")), "the child run kept its temp folders elsewhere"
    outcomes = {}
    if report.is_file():
        for case in ET.parse(report).getroot().iter("testcase"):
            kind, message = "passed", ""
            for child in case:
                if child.tag in ("skipped", "error", "failure"):
                    kind, message = child.tag, child.get("message", "")
            outcomes[case.get("name")] = (kind, message)
    text = summary.read_text(encoding="utf-8") if summary.is_file() else ""
    return done.returncode, outcomes, done.stdout + done.stderr, text


TALLY = "tests use PresentMon's download or its gold captures"


def test_the_tally_counts_one_test_in_the_singular():
    tally = dl.Tally({})
    tally.needing = {"t.py::a"}
    tally.outcomes = {"t.py::a": ("passed", "")}
    assert tally.headline() == "1 test uses PresentMon's download or its gold captures: 1 passed"
    tally.needing = {"t.py::a", "t.py::b"}
    assert tally.headline() == f"2 {TALLY}: 1 passed, 1 not run"


def test_on_a_pc_without_the_downloads_their_tests_skip_and_are_counted(tmp_path):
    code, outcomes, out, summary = run_probes(tmp_path)
    assert code == 0, out
    assert outcomes["test_exe"][0] == outcomes["test_through_a_fixture"][0] == "skipped", outcomes
    assert "PresentMon not downloaded" in outcomes["test_exe"][1]
    assert outcomes["test_gold"][0] == "skipped" and "gold ETL captures not found" in outcomes["test_gold"][1]
    assert outcomes["test_plain"] == ("passed", "")
    # Counted by fixture, also through another fixture; the plain test is not one of them.
    assert f"3 {TALLY}: 3 skipped" in out, out
    assert f"3 {TALLY}: 3 skipped." in summary and "### PresentMon tests (Python " in summary, summary
    assert "skipped: `test_probe.py::test_exe`: PresentMon not downloaded" in summary, summary


def test_when_the_run_requires_them_a_missing_download_fails_its_tests(tmp_path):
    code, outcomes, out, summary = run_probes(tmp_path, **{dl.REQUIRE_ENV: "presentmon,presentmon_gold"})
    assert code == 1, out
    for probe in ("test_exe", "test_gold", "test_through_a_fixture"):
        assert outcomes[probe][0] == "error" and dl.REQUIRE_ENV in outcomes[probe][1], outcomes
    assert outcomes["test_plain"] == ("passed", "")
    assert f"3 {TALLY}: 3 error" in out, out
    assert f"{dl.REQUIRE_ENV} = `presentmon,presentmon_gold`" in summary, summary
    assert "error: `test_probe.py::test_gold`: " in summary and "gold ETL captures not found" in summary, summary


def test_an_exe_that_is_not_the_pinned_release_fails_its_tests_even_when_not_required(tmp_path):
    put(tmp_path / "local" / "lag-doctor" / "presentmon" / "2.6.0" / "PresentMon-2.6.0-x64.exe", b"\0" * 980_320)
    code, outcomes, out, _ = run_probes(tmp_path)
    assert code == 1, out
    assert outcomes["test_exe"][0] == "error" and "SHA-256" in outcomes["test_exe"][1], outcomes
    assert outcomes["test_gold"][0] == "skipped"


def test_a_misspelt_requirement_stops_the_run_before_any_test(tmp_path):
    code, outcomes, out, summary = run_probes(tmp_path, **{dl.REQUIRE_ENV: "presentmon,presentmon-gold"})
    assert code == 4, out                                     # pytest's usage error
    assert "presentmon-gold" in out and not outcomes and not summary, out


def test_with_the_real_downloads_required_their_tests_run(tmp_path, presentmon_exe, gold_etl_dir):
    # The control: the same probes pass when the files are really there, so the failures above are the switch's.
    shutil.copyfile(presentmon_exe, put(tmp_path / "local" / "lag-doctor" / "presentmon" / "2.6.0" /
                                        presentmon_exe.name, b""))
    code, outcomes, out, summary = run_probes(tmp_path, LAGDOCTOR_TESTDATA=str(gold_etl_dir.parent),
                                              **{dl.REQUIRE_ENV: "presentmon,presentmon_gold"})
    assert code == 0, out
    assert {probe: kind for probe, (kind, _) in outcomes.items()} == dict.fromkeys(outcomes, "passed"), outcomes
    assert f"3 {TALLY}: 3 passed" in out and f"3 {TALLY}: 3 passed." in summary, out


# ------------------------------------------------------------- the gold fetch ---

@pytest.fixture
def git_env(tmp_path):
    """git with only this config: autocrlf on (as a Windows install has it), so a fetch must turn it off itself."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[core]\n\tautocrlf = true\n[user]\n\tname = t\n\temail = t@example.com\n"
                   "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    return {**os.environ, "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}


def git(env, cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, encoding="utf-8",
                          timeout=60).stdout.strip()


def gold_bytes(name: str, version: str) -> bytes:
    if name.endswith(".etl"):
        return b"\x00ETL\r\n\x01" + name.encode() + version.encode()       # binary: never converted
    return f"Application,ProcessID\n{name},{version}\n".encode()           # text with LF endings


def make_upstream(root: Path, env, skip: str = "") -> tuple[str, str]:
    """A stand-in for PresentMon's repo: a first commit with the gold folder (less `skip`), files elsewhere, then a
    second commit that changes every gold file. Returns (its URL, the first commit)."""
    root.mkdir()
    git(env, root, "init", "--quiet")
    git(env, root, "config", "uploadpack.allowFilter", "true")
    git(env, root, "config", "uploadpack.allowAnySHA1InWant", "true")
    names = [n for n in (*dl.GOLD_FILES, "test_case_0_v1.csv") if n != skip]
    for name in names:
        put(root / dl.GOLD_PATH / name, gold_bytes(name, "1"))
    put(root / "README.md", b"# upstream\n")
    put(root / "PresentMon" / "big.bin", b"\x00" * 4096)
    git(env, root, "add", "-A")
    git(env, root, "commit", "--quiet", "-m", "one")
    first = git(env, root, "rev-parse", "HEAD")
    for name in names:
        put(root / dl.GOLD_PATH / name, gold_bytes(name, "2"))
    git(env, root, "commit", "--quiet", "-am", "two")
    return root.as_uri(), first


def test_fetch_gold_takes_the_pinned_commits_gold_folder_byte_for_byte(tmp_path, git_env):
    url, first = make_upstream(tmp_path / "upstream", git_env)
    dest = dl.fetch_gold(tmp_path / "data" / "presentmon-gold", repo=url, commit=first, env=git_env)
    got = {p.name: p.read_bytes() for p in dest.iterdir()}
    assert sorted(got) == sorted([*dl.GOLD_FILES, "test_case_0_v1.csv", "SOURCE.txt"])     # that folder only
    for name in [*dl.GOLD_FILES, "test_case_0_v1.csv"]:
        assert got[name] == gold_bytes(name, "1"), name       # the first commit's bytes, LF kept, not the tip's
    assert f"commit {first}" in got["SOURCE.txt"].decode("utf-8")
    assert sorted(p.name for p in dest.parent.iterdir()) == ["presentmon-gold"]           # no staging left


def test_fetch_gold_refuses_a_folder_that_is_there_before_fetching_anything(tmp_path, git_env):
    dest = put(tmp_path / "presentmon-gold" / "mine.txt", b"keep").parent
    with pytest.raises(FileExistsError, match="already there"):
        # An unreachable repo: a refusal that came only after a fetch would be a git error instead.
        dl.fetch_gold(dest, repo=(tmp_path / "no-such-repo").as_uri(), commit="0" * 40, env=git_env)
    assert [p.name for p in dest.iterdir()] == ["mine.txt"] and (dest / "mine.txt").read_bytes() == b"keep"


def test_fetch_gold_leaves_nothing_when_the_commit_lacks_a_capture(tmp_path, git_env):
    url, first = make_upstream(tmp_path / "upstream", git_env, skip="test_case_3.etl")
    data = tmp_path / "data"
    with pytest.raises(RuntimeError, match="lacks test_case_3.etl"):
        dl.fetch_gold(data / "presentmon-gold", repo=url, commit=first, env=git_env)
    assert not data.exists() or not list(data.iterdir())


def test_fetch_gold_leaves_nothing_when_the_copy_fails_halfway(tmp_path, git_env, monkeypatch):
    url, first = make_upstream(tmp_path / "upstream", git_env)
    real_copy, copied = shutil.copyfile, []

    def copy_three_then_fail(src, dst):
        if len(copied) == 3:
            raise OSError(28, "There is not enough space on the disk")
        copied.append(dst)
        return real_copy(src, dst)

    monkeypatch.setattr(dl.shutil, "copyfile", copy_three_then_fail)
    data = tmp_path / "data"
    with pytest.raises(OSError, match="not enough space"):
        dl.fetch_gold(data / "presentmon-gold", repo=url, commit=first, env=git_env)
    assert len(copied) == 3 and list(data.iterdir()) == []     # the three copied files went with their folder


# ------------------------------------------------------------------------ CI ---

def test_ci_outputs_name_the_folders_the_fixtures_read():
    out = dl.ci_outputs({"LOCALAPPDATA": r"C:\L"})
    assert out["presentmon_dir"] == r"C:\L\lag-doctor\presentmon\2.6.0"
    assert out["gold_dir"] == r"C:\L\lag-doctor\testdata\presentmon-gold"
    assert out["presentmon_key"].startswith("presentmon-2.6.0-b2a706bc6ad47574-code-")
    assert out["gold_key"].startswith("presentmon-gold-e13fce6acdb5-")
    # What `lagdoctor setup --download` runs: its command (cli), where it installs (paths), the download and checks.
    assert set(dl.DOWNLOAD_CODE) == {"lagdoctor/setup.py", "lagdoctor/paths.py", "lagdoctor/cli.py"}


def test_a_change_to_any_of_the_download_code_misses_the_exe_cache(tmp_path, monkeypatch):
    for rel in dl.DOWNLOAD_CODE:
        put(tmp_path / rel, (ROOT / rel).read_bytes())
    monkeypatch.setattr(dl, "ROOT", tmp_path)
    before = dl.ci_outputs({"LOCALAPPDATA": r"C:\L"})
    for rel in dl.DOWNLOAD_CODE:                         # each file on its own, so none is left out of the hash
        original = (tmp_path / rel).read_bytes()
        (tmp_path / rel).write_bytes(original + b"\n# changed\n")
        after = dl.ci_outputs({"LOCALAPPDATA": r"C:\L"})
        assert after["presentmon_key"] != before["presentmon_key"], rel
        assert after["gold_key"] == before["gold_key"], rel
        (tmp_path / rel).write_bytes(original)
    assert dl.ci_outputs({"LOCALAPPDATA": r"C:\L"}) == before
    (tmp_path / dl.DOWNLOAD_CODE[0]).unlink()
    with pytest.raises(FileNotFoundError):              # a renamed file stops CI rather than weakening the key
        dl.ci_outputs({"LOCALAPPDATA": r"C:\L"})


def test_ci_fetches_checks_and_requires_every_download_and_uses_only_real_outputs():
    text = CI.read_text(encoding="utf-8")
    required = re.search(rf"^\s*{dl.REQUIRE_ENV}:\s*(\S+)\s*$", text, re.M)
    assert required and set(required.group(1).split(",")) == set(dl.DOWNLOADS)
    checked = re.search(r"python -m tests\.downloads check ([a-z_ ]+)$", text, re.M)
    assert checked and set(checked.group(1).split()) == set(dl.DOWNLOADS)
    assert re.search(r"python -m lagdoctor setup --download$", text, re.M)
    assert re.search(r"python -m tests\.downloads fetch-gold$", text, re.M)
    used = set(re.findall(r"steps\.downloads\.outputs\.(\w+)", text))
    assert used == set(dl.ci_outputs({"LOCALAPPDATA": r"C:\L"}))
