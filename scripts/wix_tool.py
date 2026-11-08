"""Install the pinned WiX Toolset .NET tool into a folder, from a package checked against its SHA-512 first.

Usage:  uv run python scripts/wix_tool.py DEST

CI's msi job runs this before scripts/msi_build.py. The `wix` package comes from nuget.org's flat container, must
match WIX_SIZE and WIX_SHA512 (nuget.org's own catalog entry for it, re-read on 2026-11-08), and only then is
installed with `dotnet tool install` from a NuGet configuration whose one source is the folder holding that package,
so nothing else is fetched. WiX v7 runs no command but help, version and eula until its EULA is accepted
(docs/DECISIONS.md, 2026-11-08: the owner accepts it per build, which msi_build.py's --accept-eula does).

Prints the installed wix.exe and its `--version`. Exit codes: 0 installed; 1 a check failed; 2 cannot start (no
dotnet, DEST not empty).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WIX_VERSION = "7.0.1"
WIX_URL = f"https://api.nuget.org/v3-flatcontainer/wix/{WIX_VERSION}/wix.{WIX_VERSION}.nupkg"
WIX_SIZE = 4_586_487
WIX_SHA512 = ("9c72a17d435e0c642f2ba6d78424a49526fcd743cc2d04bd1e053d1ecae2dd12"
              "d63faa6201c79577490e37756677f5a8d7df7f5498e64bf996018ebe17b37d70")
EULA_ID = "wix" + WIX_VERSION.split(".")[0]           # WiX computes it the same way: "wix" + its major version
RETRY_WAITS_S = (0, 20, 60)


def nuget_config(feed: Path) -> str:
    """A NuGet configuration whose only package source is `feed`."""
    return ('<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n    <clear />\n'
            f'    <add key="pinned" value={quoteattr(str(feed))} />\n  </packageSources>\n</configuration>\n')


def package_problem(size: int, sha512: str) -> str | None:
    if size != WIX_SIZE or sha512.lower() != WIX_SHA512:
        return f"the package is {size} bytes with SHA-512 {sha512}; the pin is {WIX_SIZE} bytes, {WIX_SHA512}"
    return None


def download(dest: Path) -> None:
    from lagdoctor import setup
    last = None
    for wait in RETRY_WAITS_S:
        time.sleep(wait)
        part = dest.with_name(dest.name + ".part")
        try:
            with open(part, "wb") as f:
                setup.fetch(WIX_URL, f, WIX_SIZE)
            os.replace(part, dest)
            return
        except (OSError, setup.SetupError) as exc:
            last = exc
            print(f"::warning::the WiX download failed: {exc}", flush=True)
        finally:
            part.unlink(missing_ok=True)
    raise SystemExit(f"the WiX package could not be downloaded from {WIX_URL}: {last}")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    dest = Path(argv[0]).resolve()
    dotnet = shutil.which("dotnet")
    if not dotnet:
        print("dotnet is not on PATH", file=sys.stderr)
        return 2
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} is not empty", file=sys.stderr)
        return 2
    feed, tool = dest / "feed", dest / "tool"
    feed.mkdir(parents=True)
    nupkg = feed / f"wix.{WIX_VERSION}.nupkg"
    download(nupkg)
    data = nupkg.read_bytes()
    problem = package_problem(len(data), hashlib.sha512(data).hexdigest())
    if problem:
        print(problem, file=sys.stderr)
        nupkg.unlink()
        return 1
    config = dest / "nuget.config"
    config.write_text(nuget_config(feed), encoding="utf-8")
    p = subprocess.run([dotnet, "tool", "install", "wix", "--version", WIX_VERSION, "--tool-path", str(tool),
                        "--configfile", str(config)], timeout=600)
    wix = tool / "wix.exe"
    if p.returncode != 0 or not wix.is_file():
        print(f"dotnet tool install failed with exit code {p.returncode}", file=sys.stderr)
        return 1
    v = subprocess.run([str(wix), "--version"], capture_output=True, text=True, timeout=120)
    said = (v.stdout + v.stderr).strip()
    if v.returncode != 0 or not said.startswith(WIX_VERSION):
        print(f"{wix} --version said {said!r} (exit code {v.returncode}), not {WIX_VERSION}", file=sys.stderr)
        return 1
    print(f"WiX {said} at {wix}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"wix={wix}\neula={EULA_ID}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
