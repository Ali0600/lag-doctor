"""What the machine running the tests lacks: declared by whoever runs the suite, never detected.

GitHub's Windows runners lack some of the owner's laptop: no GPU, no monitor, no Wi-Fi, no ping replies from the
internet. The CI workflow lists what is absent in LAGDOCTOR_ABSENT (comma-separated names from CAPABILITIES), and
a test marked ``@pytest.mark.needs(name)`` then skips with the reason. Unset, as on the laptop, nothing skips this
way. Detecting the absence instead (a failed ping, an empty adapter list) would also skip the test when a regression
causes the same symptom, so the list is declared, and an unknown name is an error rather than a silent no-op.
"""
from __future__ import annotations

import pytest

ABSENT_ENV = "LAGDOCTOR_ABSENT"
CAPABILITIES = {
    "internet_icmp": "router or internet host that answers ping",
    "gpu": "hardware GPU with 3D engines",
    "display": "real monitor",
    "wlan": "Wi-Fi service (wlanapi.dll)",
    "unelevated": "unelevated token (the runner's account is an administrator with UAC off)",
}


def absent_capabilities(environ) -> set[str]:
    """The capability names ``environ`` declares absent; an unknown name is a usage error."""
    names = {n.strip() for n in environ.get(ABSENT_ENV, "").split(",") if n.strip()}
    unknown = sorted(names - CAPABILITIES.keys())
    if unknown:
        raise pytest.UsageError(f"{ABSENT_ENV} names unknown capabilities {unknown}; known: {sorted(CAPABILITIES)}")
    return names


def skip_reason(needs, absent) -> str | None:
    """Why a test that needs ``needs`` must skip when ``absent`` is missing, or None when it can run."""
    unknown = sorted(set(needs) - CAPABILITIES.keys())
    if unknown:
        raise pytest.UsageError(f"@pytest.mark.needs names unknown capabilities {unknown}; known: {sorted(CAPABILITIES)}")
    missing = [n for n in needs if n in absent]
    if not missing:
        return None
    return "this machine has no " + " and no ".join(CAPABILITIES[n] for n in missing) + f" ({ABSENT_ENV})"


def apply_absent(items, environ) -> None:
    """Add a skip marker, with its reason, to every collected item that needs something declared absent."""
    absent = absent_capabilities(environ)
    for item in items:
        reason = skip_reason([name for mark in item.iter_markers("needs") for name in mark.args], absent)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
