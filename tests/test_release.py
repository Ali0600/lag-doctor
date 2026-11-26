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
        self.main = RUN_SHA
        self.compare = {"status": "ahead", "behind_by": 0, "total_commits": 1, "files": [{"filename": DOCS_FILE}]}
        self.releases: list[dict] = []
        self.tags: list[str] = []
        self.artifact = {MSI: msi_bytes(), "payload.json": json.dumps(payload()).encode(),
                         f"lag-doctor-{VERSION}-x64.wixpdb": b"pdb"}
        self.made: dict | None = None
        self.tamper: dict = {}                    # what the made release reads back as instead
        self.tag_on_create = False                # GitHub making the tag with the draft
        self.deleted: list[int] = []

    def __call__(self, args: list[str]) -> bytes:
        self.calls.append(list(args))
        if args[:2] == ["run", "list"]:
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
            asset = int(args[2].rsplit("/", 1)[1])
            return next(a["data"] for r in self.releases for a in r["assets"] if a["id"] == asset)
        if args[0] == "--paginate" and args[1].startswith(f"repos/{REPO}/releases"):
            tag = re.search(r'select\(\.tag_name == "([^"]+)"\)', args[3]).group(1)
            return b"".join(json.dumps({**r, "assets": [{k: v for k, v in a.items() if k != "data"}
                                                        for a in r["assets"]]}).encode() + b"\n"
                            for r in self.releases if r["tag_name"] == tag)
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


def staged(world: World, tmp_path: Path) -> Path:
    out = tmp_path / "release"
    rel.gather(out, REPO, gh=world)
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


@pytest.mark.parametrize("what", ["release", "draft", "tag"])
def test_gather_refuses_a_version_whose_release_or_tag_exists(world, tmp_path, what):
    if what == "tag":
        world.tags.append(TAG)
    else:
        world.releases.append({"id": 3, "tag_name": TAG, "draft": what == "draft", "assets": []})
    with pytest.raises(rel.Refused, match="raise the version"):
        staged(world, tmp_path)
    assert not any(c[:2] == ["run", "download"] for c in world.calls)


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
    ({"draft": False}, "draft"),
    ({"prerelease": False}, "prerelease"),
    ({"target_commitish": "main"}, "target_commitish"),
    ({"name": "other"}, "its name"),
])
def test_a_draft_that_reads_back_wrong_is_deleted(world, tmp_path, tamper, why):
    out = staged(world, tmp_path)
    world.tamper = tamper
    with pytest.raises(rel.Refused, match=f"deleted.*{why}"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41]


def test_a_draft_whose_msi_downloads_different_is_deleted(world, tmp_path):
    out = staged(world, tmp_path)
    world.tamper = {"assets": [{"id": 50, "name": MSI, "data": msi_bytes() + b"x"},
                               {"id": 51, "name": rel.SUMS, "data": (out / rel.SUMS).read_bytes()}]}
    with pytest.raises(rel.Refused, match=f"the {MSI} downloaded from it"):
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


def test_a_draft_that_made_its_tag_is_deleted_with_the_tag(world, tmp_path):
    out = staged(world, tmp_path)
    world.tag_on_create = True
    with pytest.raises(rel.Refused, match="not private"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41] and world.tags == []


def test_a_draft_deleted_for_another_reason_leaves_the_tags_alone(world, tmp_path):
    out = staged(world, tmp_path)
    world.tags.append(f"{TAG}-other")
    world.tamper = {"draft": False}
    with pytest.raises(rel.Refused, match="deleted"):
        rel.create(out, REPO, gh=world)
    assert world.deleted == [41] and world.tags == [f"{TAG}-other"]
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


def test_every_action_is_pinned_and_no_event_text_reaches_a_shell():
    text = code(workflow())
    uses = re.findall(r"uses: (\S+)", text)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert "github.event." not in text and "persist-credentials: false" in text
    assert text.count("persist-credentials: false") == text.count("actions/checkout@")
