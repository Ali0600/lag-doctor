"""scripts/release.py and .github/workflows/release.yml: Stage 5's draft release of the MSI main's CI checked.

The real run is the workflow on GitHub (its check job on a pull request that changes the release, its draft job when
dispatched on main). These tests drive both steps against a made-up GitHub behind the script's `gh` seam: every
refusal on its own cause, the draft read back and deleted when it reads wrong, `create` refused anywhere but a
dispatch of main on GitHub's runner, and the workflow's triggers and token rights.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from tests.test_ci_triggers import IGNORED
from tests.test_msi import UPGRADE_CODE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release as rel  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
REPO = "someone/lag-doctor"
RUN_SHA = "a" * 40
MAIN_SHA = "b" * 40
VERSION = rel.project_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
TAG = f"v{VERSION}"
MSI = f"lag-doctor-{VERSION}-x64.msi"
RUN_URL = "https://github.com/someone/lag-doctor/actions/runs/7"
DOCS_FILE = IGNORED[0].replace("**", "notes.md")                 # a file under the folder ci.yml's push ignores
DISPATCH = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main"}


def msi_bytes(size: int = rel.MIN_MSI_BYTES + 10) -> bytes:
    return rel.OLE_MAGIC + bytes(range(256)) * ((size - len(rel.OLE_MAGIC)) // 256 + 1)


def payload(**over) -> dict:
    return {"msi": MSI, "version": VERSION, "upgrade_code": UPGRADE_CODE, "manufacturer": "lag-doctor",
            "product_code": "{00000000-0000-0000-0000-000000000001}", "files": 249, **over}


class World:
    """A made-up GitHub for `gh`: main's newest CI run, main's commit, the compare between them, the releases and
    tags, the run's artifact, and the releases `gh release create` makes. Every call is recorded."""

    def __init__(self, tmp: Path):
        self.calls: list[list[str]] = []
        self.run = {"databaseId": 7, "headSha": RUN_SHA, "status": "completed", "conclusion": "success",
                    "event": "push", "headBranch": "main", "url": RUN_URL}
        self.passed_run = dict(self.run)          # what `--status success` lists
        self.files: dict[str, bytes] = {}         # the repository's files at the run's commit, else this tree's
        self.reads: list[tuple[str, str]] = []
        self.may_push = True                      # whether the token sees drafts (the draft job's) or not (check's)
        self.main = RUN_SHA
        self.compare = {"status": "ahead", "behind_by": 0, "total_commits": 1, "files": [{"filename": DOCS_FILE}]}
        self.releases: list[dict] = []
        self.tags: list[str] = []
        self.artifact = {MSI: msi_bytes(), "payload.json": json.dumps(payload()).encode(),
                         f"lag-doctor-{VERSION}-x64.wixpdb": b"pdb"}
        self.made: dict | None = None
        self.tamper: dict = {}                    # what the made release reads back as instead
        self.tag_on_create = False                # GitHub making the tag with the draft
        self.upload_fails = False                 # gh makes the draft, then an asset's upload fails
        self.fail_asset_download = False
        self.published_meanwhile = False          # the owner publishes the draft while it is read back
        self.twin_on_create = False               # a second release with the tag appears with the draft
        self.deleted: list[int] = []

    def __call__(self, args: list[str]) -> bytes:
        self.calls.append(list(args))
        if args[:2] == ["run", "list"]:
            if "--status" in args:
                assert args[args.index("--status") + 1] == "success"
                return json.dumps([self.passed_run]).encode()
            return json.dumps([self.run] if self.run else []).encode()
        if args[:2] == ["run", "download"]:
            folder = Path(args[args.index("--dir") + 1])
            for name, data in self.artifact.items():
                (folder / name).write_bytes(data)
            return b""
        if args[:2] == ["release", "create"]:
            files = [Path(a) for a in args[3:5]]
            self.made = {"id": 41, "tag_name": args[2], "name": args[args.index("--title") + 1],
                         "draft": "--draft" in args, "prerelease": "--prerelease" in args,
                         "target_commitish": args[args.index("--target") + 1], "html_url": "https://example.test/r",
                         "assets": [{"id": 50 + i, "name": f.name, "data": f.read_bytes()} for i, f in enumerate(files)]}
            self.made.update(self.tamper)
            self.releases.append(self.made)
            if self.tag_on_create:
                self.tags.append(args[2])
            if self.twin_on_create:
                self.releases.append({**self.made, "id": 42})
            if self.upload_fails:
                self.made["assets"] = self.made["assets"][:1]
                raise rel.Refused("`gh release create v0.1.0 ...` failed with exit code 1: upload failed")
            return b""
        if args[0] == "api":
            return self.api(args[1:])
        raise AssertionError(f"an unexpected gh call: {args}")

    def api(self, args: list[str]) -> bytes:
        if args[:2] == ["-X", "DELETE"] and args[2].startswith(f"repos/{REPO}/git/refs/tags/"):
            self.tags.remove(args[2].rsplit("/", 1)[1])
            return b""
        if args[:2] == ["-X", "DELETE"] and args[2].startswith(f"repos/{REPO}/releases/"):
            self.deleted.append(int(args[2].rsplit("/", 1)[1]))
            return b""
        if args[:2] == ["-H", "Accept: application/octet-stream"]:
            if self.fail_asset_download:
                raise rel.Refused("`gh api -H Accept: application/octet-stream ...` took over 600 s and was stopped")
            if self.published_meanwhile and self.made["draft"]:
                self.made["draft"] = False
                self.tags.append(self.made["tag_name"])
            asset = int(args[2].rsplit("/", 1)[1])
            return next(a["data"] for r in self.releases for a in r["assets"] if a["id"] == asset)
        if args[:2] == ["-H", rel.RAW]:
            path, ref = re.fullmatch(rf"repos/{re.escape(REPO)}/contents/(.+)\?ref=([0-9a-f]{{40}})", args[2]).groups()
            self.reads.append((path, ref))
            return self.files[path] if path in self.files else (ROOT / path).read_bytes()
        if args[0] == "--paginate" and args[1].startswith(f"repos/{REPO}/releases"):
            tag = re.search(r'select\(\.tag_name == "([^"]+)"\)', args[3]).group(1)
            return b"".join(json.dumps({**r, "assets": [{k: v for k, v in a.items() if k != "data"}
                                                        for a in r["assets"]]}).encode() + b"\n"
                            for r in self.releases if r["tag_name"] == tag and (self.may_push or not r["draft"]))
        if args[0] == f"repos/{REPO}/git/ref/heads/main":
            return json.dumps({"object": {"sha": self.main}}).encode()
        if args[0] == f"repos/{REPO}/compare/{RUN_SHA}...{MAIN_SHA}":
            return json.dumps(self.compare).encode()
        if args[0].startswith(f"repos/{REPO}/git/matching-refs/tags/"):
            prefix = args[0].rsplit("/", 1)[1]
            return json.dumps([{"ref": f"refs/tags/{t}"} for t in self.tags if t.startswith(prefix)]).encode()
        raise AssertionError(f"an unexpected gh api call: {args}")

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["release", "create"] or "-X" in c]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def staged(world: World, tmp_path: Path, strict: bool = True) -> Path:
    """gather as the check job runs it (a token that sees no draft); the draft job's token, which does, after."""
    out = tmp_path / "release"
    world.may_push = False
    _, world.noted = rel.gather(out, REPO, gh=world, strict=strict)
    world.may_push = True
    return out


# ------------------------------------------------------------------------------------------ what CI checked ---

def test_the_ignored_paths_are_read_from_ci_yml_as_written_there():
    assert rel.push_ignored((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")) == IGNORED


@pytest.mark.parametrize("pattern", ["**/*.md", f"!{IGNORED[0][:-3]}/keep.md", f"/{IGNORED[0]}"])
def test_an_ignored_pattern_the_script_cannot_read_refuses(pattern):
    text = f'on:\n  push:\n    branches: [main]\n    paths-ignore:\n      - "{IGNORED[0]}"\n      - "{pattern}"\n'
    with pytest.raises(rel.Refused, match="cannot read"):
        rel.push_ignored(text)


def test_a_ci_yml_without_an_ignored_list_refuses():
    with pytest.raises(rel.Refused, match="no paths-ignore"):
        rel.push_ignored("on:\n  push:\n    branches: [main]\n  pull_request:\n")


def test_ignored_covers_a_folder_s_files_and_the_named_files_only():
    folder = IGNORED[0][:-3]
    assert rel.ignored(f"{folder}/a/b.md", IGNORED) and rel.ignored(IGNORED[1], IGNORED)
    assert not rel.ignored(f"{folder}x/a.md", IGNORED)
    assert not rel.ignored(f"sub/{IGNORED[1]}", IGNORED)
    assert not rel.ignored("README.md", IGNORED)


@pytest.mark.parametrize("change, why", [
    ({"status": "in_progress", "conclusion": None}, "has not finished"),
    ({"conclusion": "failure"}, "not success"),
    ({"event": "pull_request"}, "not a push to main"),
    ({"headBranch": "other"}, "not a push to main"),
    ({"headSha": "abc"}, "reads wrong"),
])
def test_main_s_newest_run_must_be_a_finished_passing_push(change, why):
    run = {"databaseId": 7, "headSha": RUN_SHA, "status": "completed", "conclusion": "success", "event": "push",
           "headBranch": "main", "url": RUN_URL}
    assert rel.newest_run([run]) == run
    with pytest.raises(rel.Refused, match=why):
        rel.newest_run([{**run, **change}])


def test_no_run_refuses():
    with pytest.raises(rel.Refused, match="no ci.yml push run"):
        rel.newest_run([])


def test_later_commits_may_change_only_ignored_paths():
    docs = {"status": "ahead", "behind_by": 0, "total_commits": 2, "files": [{"filename": DOCS_FILE},
                                                                           {"filename": IGNORED[1]}]}
    assert rel.later_code(docs, IGNORED) == []
    code = {**docs, "files": [{"filename": DOCS_FILE}, {"filename": "lagdoctor/cli.py"}]}
    assert rel.later_code(code, IGNORED) == ["lagdoctor/cli.py"]
    moved = {**docs, "files": [{"filename": DOCS_FILE, "previous_filename": "scripts/x.py", "status": "renamed"}]}
    assert rel.later_code(moved, IGNORED) == ["scripts/x.py"]


@pytest.mark.parametrize("change, why", [
    ({"status": "diverged", "behind_by": 1}, "not ahead"),
    ({"status": "behind"}, "not ahead"),
    ({"files": [{"filename": f"{IGNORED[0][:-3]}/{i}.md"} for i in range(rel.COMPARE_MAX_FILES)]}, "too much"),
    ({"total_commits": rel.COMPARE_MAX_COMMITS}, "too much"),
])
def test_a_compare_that_cannot_show_main_s_changes_refuses(change, why):
    base = {"status": "ahead", "behind_by": 0, "total_commits": 1, "files": [{"filename": DOCS_FILE}]}
    with pytest.raises(rel.Refused, match=why):
        rel.later_code({**base, **change}, IGNORED)


def test_the_upgrade_code_and_version_come_from_the_repo_s_own_files():
    assert rel.upgrade_code((ROOT / "installer" / "lag-doctor.wxs").read_text(encoding="utf-8")) == UPGRADE_CODE
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)
    with pytest.raises(rel.Refused, match="not X.Y.Z"):
        rel.project_version('[project]\nversion = "0.2.0rc1"\n')


# ---------------------------------------------------------------------------------------------- the artifact ---

def artifact(tmp_path: Path, files: dict[str, bytes]) -> Path:
    folder = tmp_path / "artifact"
    folder.mkdir()
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


def test_the_artifact_s_one_msi_is_taken_when_its_payload_json_describes_it(tmp_path):
    folder = artifact(tmp_path, {MSI: msi_bytes(), "payload.json": json.dumps(payload()).encode(), "x.wixpdb": b""})
    assert rel.check_artifact(folder, VERSION, UPGRADE_CODE) == folder / MSI


@pytest.mark.parametrize("files, why", [
    ({MSI: msi_bytes(), "other.msi": msi_bytes()}, "not just"),
    ({"lag-doctor-9.9.9-x64.msi": msi_bytes()}, "not just"),
    ({MSI: msi_bytes()}, "payload.json cannot be read"),
    ({MSI: msi_bytes(), "payload.json": b"{"}, "payload.json cannot be read"),
    ({MSI: msi_bytes(), "payload.json": json.dumps(payload(version="9.9.9")).encode()}, "version"),
    ({MSI: msi_bytes(), "payload.json": json.dumps(payload(msi="x.msi")).encode()}, "its msi"),
    ({MSI: msi_bytes(), "payload.json": json.dumps(payload(upgrade_code="{1}")).encode()}, "upgrade_code"),
    ({MSI: msi_bytes(), "payload.json": json.dumps(payload(manufacturer="x")).encode()}, "manufacturer"),
    ({MSI: b"PK\x03\x04" + msi_bytes()[4:], "payload.json": json.dumps(payload()).encode()}, "does not start"),
    ({MSI: msi_bytes()[:rel.MIN_MSI_BYTES - 1], "payload.json": json.dumps(payload()).encode()}, "bytes, outside"),
])
def test_an_artifact_that_is_not_the_msi_to_release_refuses(tmp_path, files, why):
    with pytest.raises(rel.Refused, match=why):
        rel.check_artifact(artifact(tmp_path, files), VERSION, UPGRADE_CODE)


# ------------------------------------------------------------------------------------------------- gather ---

def test_gather_stages_the_tested_msi_with_its_hash_and_notes_and_writes_nothing_on_github(world, tmp_path):
    out = staged(world, tmp_path)
    digest = hashlib.sha256(world.artifact[MSI]).hexdigest()
    assert sorted(p.name for p in out.iterdir()) == sorted([MSI, rel.SUMS, rel.NOTES, rel.FACTS])
    assert (out / MSI).read_bytes() == world.artifact[MSI]
    assert (out / rel.SUMS).read_bytes() == f"{digest}  {MSI}\n".encode()
    facts = json.loads((out / rel.FACTS).read_text(encoding="utf-8"))
    assert facts == {"repo": REPO, "tag": TAG, "version": VERSION, "target": RUN_SHA, "title": rel.title(VERSION),
                     "msi": MSI, "size": len(world.artifact[MSI]), "sha256": digest, "run_id": 7, "run_url": RUN_URL}
    text = (out / rel.NOTES).read_text(encoding="utf-8")
    assert digest.upper() in text and RUN_URL in text and RUN_SHA[:12] in text and "TODO" not in text
    assert f"(Get-FileHash .\\{MSI} -Algorithm SHA256).Hash" in text
    assert world.writes() == []
    download = next(c for c in world.calls if c[:2] == ["run", "download"])
    assert download[2] == "7" and download[download.index("--name") + 1] == rel.ARTIFACT


def test_gather_takes_main_ahead_by_ignored_paths_only(world, tmp_path):
    world.main = MAIN_SHA
    assert staged(world, tmp_path).joinpath(MSI).is_file()


def test_gather_refuses_when_main_changed_code_after_the_run(world, tmp_path):
    world.main = MAIN_SHA
    world.compare["files"].append({"filename": "lagdoctor/cli.py"})
    with pytest.raises(rel.Refused, match="lagdoctor/cli.py"):
        staged(world, tmp_path)
    assert not any(c[:2] == ["run", "download"] for c in world.calls)


@pytest.mark.parametrize("what", ["release", "tag"])
def test_gather_refuses_a_version_whose_release_or_tag_exists(world, tmp_path, what):
    if what == "tag":
        world.tags.append(TAG)
    else:
        world.releases.append({"id": 3, "tag_name": TAG, "draft": False, "assets": []})
    with pytest.raises(rel.Refused, match="raise the version"):
        staged(world, tmp_path)
    assert not any(c[:2] == ["run", "download"] for c in world.calls)


def test_a_draft_holding_the_tag_passes_the_check_job_and_the_draft_job_refuses_it(world, tmp_path):
    world.releases.append({"id": 3, "tag_name": TAG, "draft": True, "assets": []})
    out = staged(world, tmp_path)                 # the check job's token sees no draft
    with pytest.raises(rel.Refused, match="a draft release already has the tag"):
        rel.create(out, REPO, gh=world)
    assert world.writes() == []


def test_gather_judges_the_artifact_by_main_s_files_at_the_run_s_commit_not_the_checkout(world, tmp_path):
    world.files["pyproject.toml"] = b'[project]\nname = "lag-doctor"\nversion = "9.9.9"\n'
    with pytest.raises(rel.Refused, match="not just lag-doctor-9.9.9-x64.msi"):
        staged(world, tmp_path)
    assert {p for p, _ in world.reads} == {".github/workflows/ci.yml", "pyproject.toml", "installer/lag-doctor.wxs"}
    assert {ref for _, ref in world.reads} == {RUN_SHA}


def test_gather_refuses_a_compare_that_lists_no_files(world, tmp_path):
    world.main = MAIN_SHA
    del world.compare["files"]
    with pytest.raises(rel.Refused, match="lists no files"):
        staged(world, tmp_path)


@pytest.mark.parametrize("setup, why", [
    (lambda w: w.run.update(status="in_progress", conclusion=None), "has not finished"),
    (lambda w: w.run.update(conclusion="failure"), "not success"),
    (lambda w: (setattr(w, "main", MAIN_SHA), w.compare["files"].append({"filename": "lagdoctor/cli.py"})),
     "main changed"),
    (lambda w: w.tags.append(TAG), "the tag v"),
])
def test_a_pull_request_s_check_notes_what_only_main_s_state_decides(world, tmp_path, setup, why):
    setup(world)
    with pytest.raises(rel.Refused, match=why):
        staged(world, tmp_path)
    world.calls.clear()
    out = staged(world, tmp_path / "pr", strict=False)
    assert (out / MSI).is_file() and len(world.noted) == 1 and why in world.noted[0]
    assert world.writes() == []


def test_a_pull_request_s_check_still_refuses_an_artifact_that_is_not_the_msi(world, tmp_path):
    world.artifact[MSI] = b"PK" + msi_bytes()[2:]
    with pytest.raises(rel.Refused, match="does not start"):
        staged(world, tmp_path, strict=False)


STAGED = {"tag": TAG, "msi": MSI, "size": 1, "sha256": "0" * 64, "target": RUN_SHA, "run_url": RUN_URL,
          "html_url": "https://example.test/r"}


@pytest.fixture
def main_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    for name, value in DISPATCH.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_main_s_gather_is_lenient_only_for_a_pull_request(main_env, tmp_path):
    seen = []

    def fake_gather(out, repo, strict):
        seen.append(strict)
        return STAGED, []
    main_env.setattr(rel, "gather", fake_gather)
    for event in ("pull_request", "workflow_dispatch", "push", ""):
        main_env.setenv("GITHUB_EVENT_NAME", event)
        assert rel.main(["gather", str(tmp_path / "release")]) == 0
    assert seen == [False, True, True, True]


@pytest.mark.parametrize("step", ["gather", "create"])
def test_main_exits_1_on_a_refusal_and_0_once_done(main_env, tmp_path, step):
    def refused(*args, **kwargs):
        raise rel.Refused("the artifact is not the MSI to release")
    main_env.setattr(rel, step, refused)
    assert rel.main([step, str(tmp_path / "a")]) == 1
    main_env.setattr(rel, step, lambda *a, **k: (STAGED, []) if step == "gather" else STAGED)
    assert rel.main([step, str(tmp_path / "b")]) == 0


def test_a_tag_that_only_starts_like_the_version_is_not_taken(world, tmp_path):
    world.tags.append(f"{TAG}1")
    assert staged(world, tmp_path).joinpath(MSI).is_file()


def test_gather_refuses_a_folder_that_holds_anything(world, tmp_path):
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "x").write_text("x")
    with pytest.raises(rel.Refused, match="not empty"):
        staged(world, tmp_path)
    assert world.calls == []


# ------------------------------------------------------------------------------------------------- create ---

@pytest.mark.parametrize("change, why", [
    ({"GITHUB_ACTIONS": ""}, "GitHub's runner"),
    ({"RUNNER_ENVIRONMENT": "self-hosted"}, "GitHub's runner"),
    ({"GITHUB_EVENT_NAME": "pull_request"}, "workflow_dispatch run of main"),
    ({"GITHUB_EVENT_NAME": "push"}, "workflow_dispatch run of main"),
    ({"GITHUB_REF": "refs/heads/other"}, "workflow_dispatch run of main"),
    ({"GITHUB_REF": "refs/tags/v0.1.0"}, "workflow_dispatch run of main"),
])
def test_create_runs_only_in_a_dispatch_of_main_on_github_s_runner(change, why):
    assert rel.may_create(DISPATCH) is None
    assert why in rel.may_create({**DISPATCH, **change})


def test_create_makes_a_draft_pre_release_at_the_run_s_commit_and_reads_it_back(world, tmp_path):
    out = staged(world, tmp_path)
    made = rel.create(out, REPO, gh=world)
    (call,) = [c for c in world.calls if c[:2] == ["release", "create"]]
    assert call[2] == TAG and [Path(a).name for a in call[3:5]] == [MSI, rel.SUMS]
    assert {"--draft", "--prerelease", "--latest=false"} <= set(call)
    assert call[call.index("--target") + 1] == RUN_SHA
    assert Path(call[call.index("--notes-file") + 1]) == out / rel.NOTES
    assert made["release_id"] == 41 and world.deleted == [] and world.tags == []
    assert any(c[:3] == ["api", "-H", "Accept: application/octet-stream"] for c in world.calls)


@pytest.mark.parametrize("tamper, why", [
    ({"prerelease": False}, "prerelease"),
    ({"name": "other"}, "its name"),
])
def test_a_draft_that_reads_back_wrong_is_deleted(world, tmp_path, tamper, why):
    out = staged(world, tmp_path)
    world.tamper = tamper
    with pytest.raises(rel.Refused, match=f"{why}.*the draft was deleted"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


@pytest.mark.parametrize("tamper, why", [
    ({"draft": False}, "its draft is False"),
    ({"target_commitish": "main"}, "its target_commitish is 'main'"),
])
def test_a_release_that_is_not_a_draft_at_the_run_s_commit_is_named_and_never_deleted(world, tmp_path, tamper, why):
    out = staged(world, tmp_path)
    world.tamper = tamper
    with pytest.raises(rel.Refused, match=f"{why}.*left as they are"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == []


def test_a_draft_the_owner_publishes_during_the_readback_is_never_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.published_meanwhile = True
    with pytest.raises(rel.Refused, match="not private.*left as they are.*tag .* exists and was left"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [] and world.tags == [TAG] and world.writes()[-1][:2] == ["release", "create"]


def test_two_releases_with_the_tag_after_the_create_are_both_left(world, tmp_path):
    out = staged(world, tmp_path)
    world.twin_on_create = True
    with pytest.raises(rel.Refused, match="2 releases have the tag.*2 releases with the tag .* left as they are"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == []


def test_a_draft_whose_upload_failed_is_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.upload_fails = True
    with pytest.raises(rel.Refused, match="upload failed; the draft was deleted"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


def test_a_draft_whose_readback_download_failed_is_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.fail_asset_download = True
    with pytest.raises(rel.Refused, match="was stopped; the draft was deleted"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


def test_a_failed_cleanup_says_where_to_look(world, tmp_path):
    out = staged(world, tmp_path)
    world.upload_fails = True
    real = world.api

    def no_list(args):
        if args[0] == "--paginate" and world.made is not None:
            raise rel.Refused("`gh api --paginate ...` failed with exit code 1: HTTP 502")
        return real(args)
    world.api = no_list
    with pytest.raises(rel.Refused, match="upload failed; looking for what it left failed too.*releases page"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == []


def test_a_draft_whose_msi_downloads_different_is_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.tamper = {"assets": [{"id": 50, "name": MSI, "data": msi_bytes() + b"x"},
                               {"id": 51, "name": rel.SUMS, "data": (out / rel.SUMS).read_bytes()}]}
    with pytest.raises(rel.Refused, match=f"the {MSI} downloaded from it"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


def test_a_draft_whose_sums_download_different_is_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.tamper = {"assets": [{"id": 50, "name": MSI, "data": (out / MSI).read_bytes()},
                               {"id": 51, "name": rel.SUMS, "data": b"0" * 64 + f"  {MSI}\n".encode()}]}
    with pytest.raises(rel.Refused, match=f"the {rel.SUMS} downloaded from it.*the draft was deleted"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


def test_a_draft_with_another_asset_is_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.tamper = {"assets": [{"id": 50, "name": MSI, "data": (out / MSI).read_bytes()},
                               {"id": 51, "name": rel.SUMS, "data": (out / rel.SUMS).read_bytes()},
                               {"id": 52, "name": "extra.zip", "data": b""}]}
    with pytest.raises(rel.Refused, match="its assets are"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


def test_a_draft_that_made_its_tag_is_deleted_and_the_tag_named_but_kept(world, tmp_path):
    out = staged(world, tmp_path)
    world.tag_on_create = True
    with pytest.raises(rel.Refused, match="not private.*the draft was deleted; the tag .* exists and was left"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41] and world.tags == [TAG]
    assert not any("git/refs" in " ".join(c) for c in world.writes())


def test_create_refuses_a_tag_a_draft_took_since_the_gather(world, tmp_path):
    out = staged(world, tmp_path)
    world.releases.append({"id": 3, "tag_name": TAG, "draft": True, "assets": []})
    with pytest.raises(rel.Refused, match="draft release already has"):
        rel.create(out, REPO, gh=world)
    assert world.writes() == []


@pytest.mark.parametrize("spoil, why", [
    (lambda out: (out / MSI).write_bytes(msi_bytes() + b"x"), "SHA-256 is not the one gathered"),
    (lambda out: (out / MSI).unlink(), "missing"),
    (lambda out: (out / rel.SUMS).write_bytes(b"0" * 64 + b"  x.msi\n"), "SHA256SUMS is not"),
    (lambda out: (out / rel.FACTS).write_text("{", encoding="utf-8"), "cannot be read"),
])
def test_create_refuses_a_staged_folder_that_changed(world, tmp_path, spoil, why):
    out = staged(world, tmp_path)
    spoil(out)
    with pytest.raises(rel.Refused, match=why):
        rel.create(out, REPO, gh=world)
    assert world.writes() == []


@pytest.mark.parametrize("key, value, why", [
    ("tag", "v9.9.9", "its tag"),
    ("title", "lag-doctor", "its title"),
    ("repo", "else/where", "its repo"),
    ("target", "main", "not a commit"),
    ("version", "0.1", "not X.Y.Z"),
])
def test_create_refuses_release_json_that_reads_wrong(world, tmp_path, key, value, why):
    out = staged(world, tmp_path)
    facts = json.loads((out / rel.FACTS).read_text(encoding="utf-8"))
    (out / rel.FACTS).write_text(json.dumps({**facts, key: value}), encoding="utf-8")
    with pytest.raises(rel.Refused, match=why):
        rel.create(out, REPO, gh=world)
    assert world.writes() == []


def test_main_refuses_create_off_github_s_runner_before_any_gh_call(monkeypatch, tmp_path):
    for name in DISPATCH:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)

    def no_gh(args, timeout_s=0):
        raise AssertionError(f"gh was called: {args}")
    monkeypatch.setattr(rel, "run_gh", no_gh)
    monkeypatch.setattr(rel, "create", lambda *a, **k: pytest.fail("create ran"))
    assert rel.main(["create", str(tmp_path / "release")]) == rel.guard.REFUSED_EXIT


def test_main_refuses_a_repository_that_is_not_owner_and_name(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOSITORY", "../x")
    monkeypatch.setattr(rel, "gather", lambda *a, **k: pytest.fail("gather ran"))
    assert rel.main(["gather", str(tmp_path / "release")]) == rel.guard.REFUSED_EXIT


# ----------------------------------------------------------------------------------------------- the workflow ---

def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job(text: str, name: str) -> str:
    m = re.search(rf"^  {name}:\n((?:    [^\n]*\n|[ \t]*\n)+)", text, re.M)
    assert m, f"release.yml has no {name} job"
    return m.group(1)


def code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_release_starts_only_by_hand_or_for_a_pull_request_check():
    m = re.search(r"^on:\n((?:  [^\n]*\n|[ \t]*\n)+)", code(workflow()) + "\n", re.M)
    assert m and re.findall(r"^  (\w+):", m.group(1), re.M) == ["workflow_dispatch", "pull_request"]


def test_only_the_draft_job_may_write_and_only_in_a_dispatch_of_main():
    text = code(workflow())
    assert re.search(r"^permissions: \{\}$", text, re.M), "the workflow's default token must have no rights"
    assert re.findall(r"^  (\w+):\n", text.split("\njobs:\n", 1)[1], re.M) == ["check", "draft"]
    check, draft = job(text, "check"), job(text, "draft")
    assert re.search(r"^    permissions:\n      contents: read\n      actions: read\n    \S", check, re.M)
    assert re.search(r"^    permissions:\n      contents: write\n    \S", draft, re.M)
    assert "    needs: check\n" in draft
    assert ("    if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'\n") in draft
    assert "release.py create" not in check and "release.py gather" not in draft
    assert len(re.findall(r"release\.py create", text)) == 1 and len(re.findall(r"release\.py gather", text)) == 1


def test_the_jobs_run_the_runner_s_own_python_and_only_github_s_own_actions():
    text = code(workflow())
    for name in ("check", "draft"):
        owners = re.findall(r"uses: ([\w.-]+)/", job(text, name))
        assert owners and set(owners) == {"actions"}, f"the {name} job runs an action not GitHub's own: {owners}"
    runs = re.findall(r'^        run: python3 -I scripts/release\.py (\w+) "\$RUNNER_TEMP/release"$', text, re.M)
    assert runs == ["gather", "create"]


def test_run_gh_gives_the_msi_s_moves_the_long_timeout_and_turns_a_timeout_into_a_refusal(monkeypatch):
    seen = []

    def fake_run(argv, capture_output, timeout):
        seen.append((argv[1:3], timeout))
        raise rel.subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(rel, "_run", fake_run)
    for args in (["run", "download", "7"], ["release", "create", TAG],
                 ["api", "-H", "Accept: application/octet-stream", "repos/x/y/releases/assets/1"], ["run", "list"]):
        with pytest.raises(rel.Refused, match="was stopped"):
            rel.run_gh(args)
    assert [t for _, t in seen] == [rel.DOWNLOAD_TIMEOUT_S] * 3 + [rel.GH_TIMEOUT_S]


def test_every_action_is_pinned_and_no_event_text_reaches_a_shell():
    text = code(workflow())
    uses = re.findall(r"uses: (\S+)", text)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert "github.event." not in text and "persist-credentials: false" in text
    assert text.count("persist-credentials: false") == text.count("actions/checkout@")
