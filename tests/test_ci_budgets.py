"""CI's checks print every verdict before a job's cap stops it (docs/DECISIONS.md, 2026-11-08).

scripts/wheel_check.py and scripts/msi_check.py print their verdicts only once every check has returned. Each now runs
its commands under one budget, taken when its checks start (BUDGET_S): a command's timeout is cut to what is left, and
once the budget is spent the rest report "not run". So a hang, such as a sabotage that makes lagdoctor never return,
ends in printed verdicts rather than in GitHub cancelling the job with nothing printed. These tests hold each budget,
plus what the script spends outside it and what its job's earlier steps take, under the job's timeout-minutes, and
prove the cut is applied. The test step itself stops a hung test well before its job's cap.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
sys.path.insert(0, str(ROOT / "scripts"))

import msi_check as mc  # noqa: E402
import wheel_check as wc  # noqa: E402
from stage2 import guard  # noqa: E402

SCRIPTS = {"wheel": wc, "msi": mc}
# What each job's steps before its check take when they succeed: the worst measured on CI plus a margin, written here.
# wheel: checkout, setup-uv, uv sync and uv build took 20-25 s in all (2026-11-08). msi: up to 3 min 14 s, 142 s of
# it PresentMon's download retrying (run 42478440132); a download that succeeds only on its third try waits 80 s more.
BEFORE_S = {"wheel": 180, "msi": 480}
# What each script spends outside its budget: git's list of tracked files, the capture it makes in-process, reading and
# hashing what it checks and the temp folder's cleanup; for the MSI also the uninstall, which keeps its own timeout.
OUTSIDE_S = {"wheel": wc.GIT_TIMEOUT_S + 60, "msi": wc.GIT_TIMEOUT_S + 60 + mc.MSIEXEC_TIMEOUT_S}
# How long the whole check took on CI (2026-11-08): a budget must leave a slow runner room.
NORMAL_S = {"wheel": 7, "msi": 85}


def ci_text() -> str:
    return CI.read_text(encoding="utf-8")


def job_cap_s(job: str) -> int:
    m = re.search(rf"^  {job}:\n(?:(?:    [^\n]*|[ \t]*)\n)*?    timeout-minutes: (\d+)$", ci_text(), re.M)
    assert m, f"ci.yml's {job} job sets no timeout-minutes"
    return int(m.group(1)) * 60


def fake_subprocess(calls: list) -> types.SimpleNamespace:
    """A stand-in for the subprocess module that records each timeout and starts nothing."""
    def run(argv, **kw):
        calls.append(kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    return types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
                                 SubprocessError=subprocess.SubprocessError, list2cmdline=subprocess.list2cmdline)


# ---------------------------------------------------------------------------------------------- the arithmetic ---

@pytest.mark.parametrize("job", sorted(SCRIPTS))
def test_a_checks_budget_and_what_comes_before_it_fit_its_jobs_cap(job):
    worst = BEFORE_S[job] + SCRIPTS[job].BUDGET_S + OUTSIDE_S[job]
    assert worst <= job_cap_s(job), (
        f"the {job} job's earlier steps ({BEFORE_S[job]} s), its check's budget ({SCRIPTS[job].BUDGET_S} s) and what "
        f"the check does outside it ({OUTSIDE_S[job]} s) add up to {worst} s, past the job's {job_cap_s(job)} s cap: "
        f"a hang would end with no verdict printed")


@pytest.mark.parametrize("job", sorted(SCRIPTS))
def test_a_budget_leaves_a_slow_runner_room(job):
    assert SCRIPTS[job].BUDGET_S >= 5 * NORMAL_S[job], SCRIPTS[job].BUDGET_S


def test_the_test_step_stops_a_hung_test_before_its_jobs_cap():
    m = re.search(r"^      - name: Tests\n        timeout-minutes: (\d+)\n        run: [^\n]*"
                  r"-o faulthandler_timeout=(\d+)\b", ci_text(), re.M)
    assert m, "the Tests step sets no timeout-minutes, or its pytest no faulthandler_timeout"
    step_s, stacks_s = int(m.group(1)) * 60, int(m.group(2))
    assert stacks_s < step_s < job_cap_s("test"), (stacks_s, step_s, job_cap_s("test"))


def test_the_msi_logs_are_kept_whatever_ended_the_job():
    m = re.search(r"^      - name: Keep the install and uninstall logs\n        if: (\$\{\{[^\n]*\}\})$",
                  ci_text(), re.M)
    assert m and m.group(1) == "${{ always() }}", m and m.group(1)


# ----------------------------------------------------------------------------------------------- the mechanism ---

def test_a_wheel_check_command_gets_only_what_is_left_of_the_budget(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(wc, "subprocess", fake_subprocess(calls))
    monkeypatch.setattr(wc, "_deadline", None)
    wc.run(["x"], tmp_path, {})
    monkeypatch.setattr(wc, "_deadline", time.monotonic() + 5)
    wc.run(["x"], tmp_path, {})
    assert calls[0] == wc.TIMEOUT_S and 0 < calls[1] <= 5, calls


def test_a_spent_wheel_check_budget_starts_nothing_and_says_so(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(wc, "subprocess", fake_subprocess(calls))
    monkeypatch.setattr(wc, "_deadline", time.monotonic() - 1)
    code, out, err = wc.run(["x"], tmp_path, {})
    assert calls == [] and code is None and "not run" in err and str(wc.BUDGET_S) in err, (calls, code, err)


def test_an_msi_check_command_gets_only_what_is_left_and_a_spent_budget_starts_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mc, "subprocess", fake_subprocess(calls))
    monkeypatch.setattr(mc, "_deadline", None)
    mc.run(["x"], tmp_path)
    monkeypatch.setattr(mc, "_deadline", time.monotonic() + 5)
    mc.run(["x"], tmp_path)
    assert calls[0] == mc.COMMAND_TIMEOUT_S and 0 < calls[1] <= 5, calls
    monkeypatch.setattr(mc, "_deadline", time.monotonic() - 1)
    code, out = mc.run(["x"], tmp_path)
    assert len(calls) == 2 and code is None and "not run" in out, (calls, code, out)


def test_the_uninstall_runs_on_its_own_timeout_when_the_budget_is_spent(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mc, "subprocess", fake_subprocess(calls))
    monkeypatch.setattr(mc, "_deadline", time.monotonic() - 1)
    code, out = mc.msiexec("/i", tmp_path / "x.msi", tmp_path / "install.log")
    assert calls == [] and code is None, (calls, code, out)
    mc.msiexec("/x", tmp_path / "x.msi", tmp_path / "uninstall.log")
    assert calls == [mc.MSIEXEC_TIMEOUT_S], calls


class Stop(Exception):
    pass


def test_both_checks_start_their_budget_before_anything_else(monkeypatch, tmp_path):
    monkeypatch.setattr(wc, "_deadline", None)
    with pytest.raises(wc.CannotStart):
        wc.run_checks(tmp_path)                          # no wheel here: it stops at once
    assert wc._deadline is not None and wc._deadline - time.monotonic() > wc.BUDGET_S - 30

    def refuse(*args, **kwargs):
        raise Stop
    monkeypatch.setattr(guard, "require_throwaway_runner", refuse)   # stops before anything is read or changed
    monkeypatch.setattr(mc, "_deadline", None)
    with pytest.raises(Stop):
        mc.run_checks(tmp_path / "x.msi", tmp_path / "x.zip", tmp_path / "pm.exe", tmp_path / "logs")
    assert mc._deadline is not None and mc._deadline - time.monotonic() > mc.BUDGET_S - 30
