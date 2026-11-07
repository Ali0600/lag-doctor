"""The Stage 2 experiments run only on GitHub's throwaway Windows runners, never on a PC someone uses.

They create services, write under Program Files and set service settings (docs/INSTALLER-PLAN.md, Stage 2), so
every entry point that changes anything calls require_throwaway_runner() before its first change. GitHub sets
GITHUB_ACTIONS=true and RUNNER_ENVIRONMENT=github-hosted on its own runners, and a workflow cannot overwrite
variables named GITHUB_* or RUNNER_* [verified: GitHub Actions "Variables" reference, 2026-11-07]. A self-hosted
runner says "self-hosted" and is refused as well: it may be someone's own PC. This guards against an accident (an
agent or a person starting a script on their own machine), not against someone who sets the variables on purpose.
"""
from __future__ import annotations

import os
from typing import Mapping

REFUSED_EXIT = 2


def on_throwaway_runner(environ: Mapping[str, str] | None = None) -> bool:
    """True only on a GitHub-hosted Actions runner."""
    env = os.environ if environ is None else environ
    return env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted"


def require_throwaway_runner(environ: Mapping[str, str] | None = None) -> None:
    """Exit with REFUSED_EXIT, having changed nothing, unless this is a GitHub-hosted runner."""
    if not on_throwaway_runner(environ):
        print("This experiment creates a Windows service and changes settings, so it runs only on GitHub's "
              "throwaway runners (GITHUB_ACTIONS=true and RUNNER_ENVIRONMENT=github-hosted). Nothing was changed.")
        raise SystemExit(REFUSED_EXIT)
