"""Stage 5's preview release (docs/INSTALLER-PLAN.md): the MSI main's CI checked, on a DRAFT GitHub Release.

Usage:  python3 -I scripts/release.py gather OUT_DIR       (on GitHub's runner, with its own Python: stdlib only)
        python3 -I scripts/release.py create OUT_DIR

Nothing is built here. `gather` takes main's newest CI run (.github/workflows/ci.yml): it must be a push run that
concluded success, and every commit on main after it may change only what ci.yml's push trigger ignores, so the MSI
it built, installed, ran and uninstalled (the msi job) is main's code. It downloads that run's `lag-doctor-msi`
artifact, checks it against main's own files at the run's commit (one MSI, named and versioned as its payload.json
and pyproject.toml say, lag-doctor's upgrade code), refuses a version whose tag or release exists, hashes the MSI and
writes OUT_DIR: the MSI, SHA256SUMS, notes.md and release.json. A pull request that changes the release runs it with
a read-only token, which sees no draft, and with what only main's state decides noted rather than refused.

`create` makes the draft from OUT_DIR, and only on GitHub's runner in a run dispatched on main
(.github/workflows/release.yml's draft job, the one with a token that may write): the tag v<version> at that commit,
marked a pre-release, the MSI and SHA256SUMS as its assets. It reads the draft back, downloads the MSI from it and
compares the hash, and checks the tag does not exist yet: a draft is seen only by those who may push, and GitHub
creates its tag when someone publishes it. Publishing is the owner's. If anything fails once the create has started,
the draft is deleted while it is still a draft; a tag is never deleted.

Exit codes: 0 done; 1 a check refused the release; 2 refused to start (not on GitHub's runner, not a dispatch of main).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from stage2 import guard  # noqa: E402

PRODUCT = "lag-doctor"
ARTIFACT = "lag-doctor-msi"
SUMS = "SHA256SUMS"
NOTES = "notes.md"
FACTS = "release.json"
CI_WORKFLOW = "ci.yml"
# What `gh api .../compare` lists at most: past these, the list is cut short, so it cannot show that a later commit
# changed only ignored paths (GitHub's REST reference, "Compare two commits").
COMPARE_MAX_FILES = 300
COMPARE_MAX_COMMITS = 250
# An MSI is an OLE compound file; these are the first 8 bytes of every one.
OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
MIN_MSI_BYTES = 1 << 20
MAX_ASSET_BYTES = 2 << 30                                # GitHub's limit on one release asset: under 2 GiB
GH_TIMEOUT_S = 120
DOWNLOAD_TIMEOUT_S = 600
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.\.?$)[A-Za-z0-9_.-]+")   # an owner, then a name (not . or ..)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
RAW = "Accept: application/vnd.github.raw+json"          # the contents API's media type for a file's own bytes

Gh = Callable[[list[str]], bytes]
_run = subprocess.run                                    # run_gh's own seam, so a test never patches subprocess itself


class Refused(Exception):
    """A check refused the release, or a gh call failed; the message says what, if anything, was left."""


# ------------------------------------------------------------------------------------------------- gh ---

def slow(args: list[str]) -> bool:
    """The gh calls that move the MSI: the artifact's download, the draft's upload, and the asset's download."""
    return args[:2] in (["run", "download"], ["release", "create"]) or "Accept: application/octet-stream" in args


def run_gh(args: list[str]) -> bytes:
    """`gh` with these arguments (no shell); its stdout, or Refused naming the command and its stderr (or its
    timeout, past which it is killed)."""
    timeout_s = DOWNLOAD_TIMEOUT_S if slow(args) else GH_TIMEOUT_S
    try:
        p = _run(["gh", *args], capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise Refused(f"`gh {' '.join(args[:3])} ...` took over {timeout_s} s and was stopped") from None
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()[-400:]
        raise Refused(f"`gh {' '.join(args[:3])} ...` failed with exit code {p.returncode}: {err}")
    return p.stdout


def gh_json(gh: Gh, args: list[str]):
    return json.loads(gh(args).decode("utf-8"))


# ----------------------------------------------------------------------------------------- what CI checked ---

def push_ignored(ci_text: str) -> list[str]:
    """The paths ci.yml's push trigger ignores (its `paths-ignore`), each a folder's `<dir>/**` or one file."""
    m = re.search(r"^  push:\n(?:    [^\n]*\n)*?    paths-ignore:\n((?:      - \"[^\"\n]+\"\n|[ \t]*#[^\n]*\n)+)",
                  ci_text, re.M)
    if not m:
        raise Refused("ci.yml's push trigger has no paths-ignore list to read")
    pats = re.findall(r'^      - "([^"\n]+)"$', m.group(1), re.M)
    odd = [p for p in pats if "*" in p.removesuffix("/**") or p.startswith(("/", "!"))]
    if not pats or odd:
        raise Refused(f"ci.yml's paths-ignore holds patterns this script cannot read: {odd or pats}")
    return pats


def ignored(path: str, patterns: list[str]) -> bool:
    """Whether ci.yml's push trigger ignores a change to `path` (a folder's `/**` covers everything under it)."""
    return any(path.startswith(p[:-2]) if p.endswith("/**") else path == p for p in patterns)


def newest_run(runs: list[dict]) -> dict:
    """main's newest ci.yml push run, which must have finished and passed."""
    if len(runs) != 1:
        raise Refused(f"main has no ci.yml push run to release from ({len(runs)} found)")
    run = runs[0]
    if run.get("event") != "push" or run.get("headBranch") != "main":
        raise Refused(f"the newest run is a {run.get('event')!r} run of {run.get('headBranch')!r}, not a push to main")
    if run.get("status") != "completed":
        raise Refused(f"main's newest CI run has not finished ({run.get('status')}): wait for it, then run again")
    if run.get("conclusion") != "success":
        raise Refused(f"main's newest CI run ended {run.get('conclusion')!r}, not success: {run.get('url')}")
    if not SHA_RE.fullmatch(str(run.get("headSha"))) or not isinstance(run.get("databaseId"), int):
        raise Refused(f"the run's commit or id reads wrong: {run.get('headSha')!r}, {run.get('databaseId')!r}")
    return run


def later_code(compare: dict, patterns: list[str]) -> list[str]:
    """The files main changed after the run's commit that ci.yml's push trigger does not ignore (none may be)."""
    if compare.get("status") not in ("ahead", "identical") or compare.get("behind_by"):
        raise Refused(f"main is not ahead of the run's commit (GitHub says {compare.get('status')!r})")
    files, commits = compare.get("files"), compare.get("total_commits")
    if not isinstance(files, list) or not isinstance(commits, int):
        raise Refused("GitHub's compare of the run's commit and main lists no files or no commit count")
    if len(files) >= COMPARE_MAX_FILES or commits >= COMPARE_MAX_COMMITS:
        raise Refused("main changed too much after the run's commit for GitHub to list it all: let CI run on main")
    out = []
    for f in files:
        for name in (f.get("filename"), f.get("previous_filename")):
            if name is not None and not ignored(str(name), patterns):
                out.append(str(name))
    return sorted(set(out))


def upgrade_code(wxs_text: str) -> str:
    """The UpgradeCode installer/lag-doctor.wxs gives lag-doctor's Package (it never changes)."""
    root = ET.fromstring(wxs_text)
    found = [el.get("UpgradeCode") for el in root.iter() if el.tag.endswith("}Package") or el.tag == "Package"]
    if len(found) != 1 or not found[0]:
        raise Refused("installer/lag-doctor.wxs names no one Package UpgradeCode")
    return found[0]


def project_version(pyproject_text: str) -> str:
    version = tomllib.loads(pyproject_text).get("project", {}).get("version", "")
    if not VERSION_RE.fullmatch(str(version)):
        raise Refused(f"pyproject.toml's version {version!r} is not X.Y.Z, which an MSI needs")
    return str(version)


def check_artifact(folder: Path, version: str, code: str) -> Path:
    """The one MSI in the downloaded artifact, once it is the one its payload.json describes."""
    msis = sorted(folder.glob("*.msi"))
    name = f"{PRODUCT}-{version}-x64.msi"
    if [m.name for m in msis] != [name]:
        raise Refused(f"the artifact holds {[m.name for m in msis]}, not just {name}")
    try:
        payload = json.loads((folder / "payload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"the artifact's payload.json cannot be read: {exc}") from None
    problems = [f"its {key} is {payload.get(key)!r}, not {want!r}"
                for key, want in (("msi", name), ("version", version), ("upgrade_code", code),
                                  ("manufacturer", PRODUCT)) if payload.get(key) != want]
    msi = msis[0]
    size = msi.stat().st_size
    with open(msi, "rb") as f:
        head = f.read(len(OLE_MAGIC))
    if head != OLE_MAGIC:
        problems.append("the MSI does not start as an MSI file does")
    if not MIN_MSI_BYTES <= size < MAX_ASSET_BYTES:
        problems.append(f"the MSI is {size:,} bytes, outside {MIN_MSI_BYTES:,} to {MAX_ASSET_BYTES:,}")
    if problems:
        raise Refused("the artifact is not the MSI to release: " + "; ".join(problems))
    return msi


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def sums_text(digest: str, name: str) -> str:
    """SHA256SUMS as `sha256sum --check` reads it: the hash, two spaces, the name."""
    return f"{digest}  {name}\n"


# ------------------------------------------------------------------------------------------ what is taken ---

def releases_with(gh: Gh, repo: str, tag: str) -> list[dict]:
    """Every release that has the tag `tag`: drafts too, when the token may push (no other token sees a draft)."""
    # `@json` makes each release one line of compact JSON, which gh prints as it is (a string), on every page.
    pages = gh(["api", "--paginate", f"repos/{repo}/releases?per_page=100", "--jq",
                f'.[] | select(.tag_name == "{tag}") | @json']).decode("utf-8")
    return [json.loads(line) for line in pages.splitlines() if line.strip()]


def tag_exists(gh: Gh, repo: str, tag: str) -> bool:
    refs = gh_json(gh, ["api", f"repos/{repo}/git/matching-refs/tags/{tag}"])
    return any(r.get("ref") == f"refs/tags/{tag}" for r in refs)


def taken(gh: Gh, repo: str, tag: str) -> list[str]:
    """Why `tag` cannot be this release's: a release has it already, or the tag exists."""
    out = [f"a {'draft ' if r.get('draft') else ''}release already has the tag {tag}"
           for r in releases_with(gh, repo, tag)]
    if tag_exists(gh, repo, tag):
        out.append(f"the tag {tag} exists")
    return out


# -------------------------------------------------------------------------------------------- the notes ---

def notes(version: str, msi: str, digest: str, sha: str, run_url: str, repo: str) -> str:
    """The release's text: what it is, how to check it, what Windows shows for an unsigned MSI, and how to remove it."""
    readme = f"https://github.com/{repo}/blob/{sha}/README.md"
    return f"""**A preview, and not signed yet.** lag-doctor {version} for 64-bit Windows 11 (Windows 10 is not tried \
yet): the MSI that CI built from commit `{sha[:12]}` and then installed, ran and uninstalled on GitHub's runner \
([that run]({run_url}), kept for 90 days).

## Install

1. Download `{msi}`.
2. Check it in PowerShell, in the folder you saved it to:
   ```powershell
   (Get-FileHash .\\{msi} -Algorithm SHA256).Hash
   ```
   It must print `{digest.upper()}`, the hash in `{SUMS}` too.
3. Open it. It shows one UAC prompt (on a standard account, for an administrator's password) and asks no questions, \
and installs lag-doctor under Program Files, `lagdoctor` on \
the system PATH, a Start-menu shortcut and the capture service, which records frame timing without a UAC prompt \
([what it installs]({readme}#install)).

## What Windows shows for an unsigned installer

It is not signed yet (an application for SignPath Foundation's free signing of open-source projects comes next), \
so Windows treats it as an app from an unknown publisher:

- **SmartScreen** may say "Windows protected your PC" when you open the download. Choose **More info**, then **Run \
anyway**. Its trust in an unsigned file is per file, so each new release starts again.
- **The UAC prompt** is yellow and gives the publisher as **Unknown**. Neither says where the file came from: step \
2's hash says only that your download is the file on this page.
- **Smart App Control** (Windows 11), when it is on, blocks unsigned code and has no way past that for one app, only \
turning it off. Whether it blocks this MSI has not been tried yet. If it does, leave it on: this preview is not for \
that PC.

## Remove it

Settings > Apps > Installed apps, or `msiexec /x {msi}`. CI checks that no file, PATH entry, Start-menu shortcut or \
Windows Installer key is left. Your recordings and the app's own folders stay in `%LOCALAPPDATA%\\lag-doctor`: delete \
that folder to remove them too.
"""


def title(version: str) -> str:
    return f"{PRODUCT} {version} (preview, unsigned)"


# ---------------------------------------------------------------------------------------------- gather ---

def file_at(gh: Gh, repo: str, path: str, sha: str) -> str:
    """A file of the repository as it is at commit `sha`, read through GitHub's API (its raw media type)."""
    return gh(["api", "-H", RAW, f"repos/{repo}/contents/{path}?ref={sha}"]).decode("utf-8")


def gather(out: Path, repo: str, gh: Gh = run_gh, strict: bool = True) -> tuple[dict, list[str]]:
    """Pick main's tested MSI and stage the release in `out` (an empty or new folder); changes nothing on GitHub.
    The artifact is judged against main's own files at the run's commit, never a checkout's, so a pull request that
    changes the version or the installer does not judge main's MSI by them. With strict=False (a pull request's
    check), what only main's state decides (its newest run not passed yet, code changed on main since, the version's
    tag taken) is noted instead of refused, from main's newest run that passed; every other check is as strict.
    Returns the staged facts and those notes."""
    if out.exists() and any(out.iterdir()):
        raise Refused(f"{out} is not empty")
    noted: list[str] = []

    def refuse(why: str) -> None:
        if strict:
            raise Refused(why)
        noted.append(why)

    runs = ["run", "list", "--repo", repo, "--workflow", CI_WORKFLOW, "--branch", "main", "--event", "push",
            "--limit", "1", "--json", "databaseId,headSha,status,conclusion,event,headBranch,url"]
    try:
        run = newest_run(gh_json(gh, runs))
    except Refused as exc:
        refuse(str(exc))
        run = newest_run(gh_json(gh, runs + ["--status", "success"]))
    sha = run["headSha"]
    patterns = push_ignored(file_at(gh, repo, f".github/workflows/{CI_WORKFLOW}", sha))
    main_sha = gh_json(gh, ["api", f"repos/{repo}/git/ref/heads/main"]).get("object", {}).get("sha")
    if not SHA_RE.fullmatch(str(main_sha)):
        raise Refused(f"main's commit reads wrong: {main_sha!r}")
    if main_sha != sha:
        code = later_code(gh_json(gh, ["api", f"repos/{repo}/compare/{sha}...{main_sha}"]), patterns)
        if code:
            refuse(f"main changed {code[:8]} after CI's run of {sha[:12]}: wait for CI to run on main")
    version = project_version(file_at(gh, repo, "pyproject.toml", sha))
    code = upgrade_code(file_at(gh, repo, "installer/lag-doctor.wxs", sha))
    tag = f"v{version}"
    problems = taken(gh, repo, tag)
    if problems:
        refuse("; ".join(problems) + ": raise the version in pyproject.toml for a new release")
    work = out / "artifact"
    work.mkdir(parents=True)
    gh(["run", "download", str(run["databaseId"]), "--repo", repo, "--name", ARTIFACT, "--dir", str(work)])
    msi = check_artifact(work, version, code)
    shutil.move(str(msi), str(out / msi.name))
    shutil.rmtree(work)
    digest = sha256_file(out / msi.name)
    facts = {"repo": repo, "tag": tag, "version": version, "target": sha, "title": title(version),
             "msi": msi.name, "size": (out / msi.name).stat().st_size, "sha256": digest,
             "run_id": run["databaseId"], "run_url": run["url"]}
    (out / SUMS).write_bytes(sums_text(digest, msi.name).encode("ascii"))
    (out / NOTES).write_bytes(notes(version, msi.name, digest, sha, run["url"], repo).encode("utf-8"))
    (out / FACTS).write_bytes((json.dumps(facts, indent=1) + "\n").encode("utf-8"))
    return facts, noted


# ---------------------------------------------------------------------------------------------- create ---

def may_create(environ: Mapping[str, str]) -> str | None:
    """Why `create` must not run here, or None: only GitHub's runner, in a workflow_dispatch run of main."""
    if not guard.on_throwaway_runner(environ):
        return "it runs only on GitHub's runner"
    if environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or environ.get("GITHUB_REF") != "refs/heads/main":
        return (f"it runs only in a workflow_dispatch run of main, not a {environ.get('GITHUB_EVENT_NAME')!r} run "
                f"of {environ.get('GITHUB_REF')!r}")
    return None


def load_staged(folder: Path, repo: str) -> dict:
    """release.json and the files it names, checked against each other before anything is made."""
    try:
        facts = json.loads((folder / FACTS).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{FACTS} cannot be read: {exc}") from None
    version = str(facts.get("version"))
    want = {"repo": repo, "tag": f"v{version}", "title": title(version), "msi": f"{PRODUCT}-{version}-x64.msi"}
    problems = [f"its {k} is {facts.get(k)!r}, not {v!r}" for k, v in want.items() if facts.get(k) != v]
    if not VERSION_RE.fullmatch(version):
        problems.append(f"its version {version!r} is not X.Y.Z")
    if not SHA_RE.fullmatch(str(facts.get("target"))):
        problems.append(f"its target {facts.get('target')!r} is not a commit")
    if problems:
        raise Refused(f"{FACTS} reads wrong: " + "; ".join(problems))
    msi = folder / facts["msi"]
    if not msi.is_file() or sha256_file(msi) != facts.get("sha256"):
        raise Refused(f"{facts['msi']} is missing or its SHA-256 is not the one gathered")
    if (folder / SUMS).read_bytes() != sums_text(facts["sha256"], facts["msi"]).encode("ascii"):
        raise Refused(f"{SUMS} is not the gathered hash")
    return facts


def readback_problems(release: dict, facts: dict, assets: Mapping[str, bytes]) -> list[str]:
    """What the made release has that it should not: anything but a draft pre-release of the staged files."""
    problems = [f"its {k} is {release.get(k)!r}, not {v!r}"
                for k, v in (("tag_name", facts["tag"]), ("name", facts["title"]), ("draft", True),
                             ("prerelease", True), ("target_commitish", facts["target"]))
                if release.get(k) != v]
    names = sorted(a.get("name") for a in release.get("assets", []))
    if names != sorted([facts["msi"], SUMS]):
        problems.append(f"its assets are {names}, not {sorted([facts['msi'], SUMS])}")
    msi = assets.get(facts["msi"])
    if msi is None or hashlib.sha256(msi).hexdigest() != facts["sha256"]:
        problems.append(f"the {facts['msi']} downloaded from it is not the gathered one")
    if assets.get(SUMS) != sums_text(facts["sha256"], facts["msi"]).encode("ascii"):
        problems.append(f"the {SUMS} downloaded from it is not the gathered one")
    return problems


def remove_draft(gh: Gh, repo: str, tag: str, target: str) -> str:
    """After a create that failed or read back wrong: delete the release with the tag only while it is the one
    release with it and still a draft at the target, read again just before. A published release and any tag stay:
    the owner may have published the draft or pushed the tag meanwhile. Says what was done or left."""
    try:
        found = releases_with(gh, repo, tag)
        if len(found) == 1 and found[0].get("draft") is True and found[0].get("target_commitish") == target:
            gh(["api", "-X", "DELETE", f"repos/{repo}/releases/{int(found[0]['id'])}"])
            said = "the draft was deleted"
        elif not found:
            said = "no release was left"
        else:
            said = f"{len(found)} releases with the tag {tag} were left as they are: look at the releases page"
        if tag_exists(gh, repo, tag):
            said += f"; the tag {tag} exists and was left: delete it on the tags page if no release should have it"
        return said
    except Refused as exc:
        return f"looking for what it left failed too ({exc}): look at the releases page for a draft {tag}"


def create(folder: Path, repo: str, gh: Gh = run_gh) -> dict:
    """Make the draft from the staged folder and read it back; whatever fails after the create starts, a draft it
    left is deleted (`remove_draft`)."""
    facts = load_staged(folder, repo)
    tag = facts["tag"]
    problems = taken(gh, repo, tag)
    if problems:
        raise Refused("; ".join(problems))
    try:
        gh(["release", "create", tag, str(folder / facts["msi"]), str(folder / SUMS), "--repo", repo, "--draft",
            "--prerelease", "--latest=false", "--target", facts["target"], "--title", facts["title"],
            "--notes-file", str(folder / NOTES)])
        found = releases_with(gh, repo, tag)
        if len(found) != 1:
            raise Refused(f"{len(found)} releases have the tag {tag} after the create, not 1")
        release = found[0]
        assets = {a.get("name"): gh(["api", "-H", "Accept: application/octet-stream",
                                     f"repos/{repo}/releases/assets/{int(a['id'])}"])
                  for a in release.get("assets", []) if a.get("name") in (facts["msi"], SUMS)}
        problems = readback_problems(release, facts, assets)
        if tag_exists(gh, repo, tag):           # absent just before the create
            problems.append(f"the tag {tag} exists already, so the draft is not private")
        if problems:
            raise Refused("the draft read back wrong: " + "; ".join(problems))
    except Refused as exc:
        raise Refused(f"{exc}; {remove_draft(gh, repo, tag, facts['target'])}") from None
    return {**facts, "release_id": release["id"], "html_url": release.get("html_url")}


# ------------------------------------------------------------------------------------------------ main ---

def summary(lines: list[str]) -> None:
    """Print the lines, and add them to the job's summary on GitHub's runner."""
    print("\n".join(lines))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=("gather", "create"))
    ap.add_argument("out", help="the folder the release is staged in")
    args = ap.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not REPO_RE.fullmatch(repo):
        print(f"GITHUB_REPOSITORY is {repo!r}, not owner/name: nothing was done.", file=sys.stderr)
        return guard.REFUSED_EXIT
    if args.step == "create":
        why = may_create(os.environ)
        if why:
            print(f"The draft release is not made here: {why}. Nothing was done.", file=sys.stderr)
            return guard.REFUSED_EXIT
    try:
        if args.step == "gather":
            # A pull request's check judges its code on main's state, which it cannot change: anything else is strict.
            f, noted = gather(Path(args.out), repo, strict=os.environ.get("GITHUB_EVENT_NAME") != "pull_request")
            summary([f"Staged {f['tag']}: {f['msi']} ({f['size']:,} bytes), SHA-256 {f['sha256']}",
                     f"from commit {f['target']}, built and checked by {f['run_url']}"]
                    + [f"A dispatch would refuse this now: {why}" for why in noted])
        else:
            f = create(Path(args.out), repo)
            summary([f"Draft release {f['tag']} made and read back: {f['html_url']}",
                     f"{f['msi']} SHA-256 {f['sha256']}; publishing it is the owner's."])
    except Refused as exc:
        print(f"The release was refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
