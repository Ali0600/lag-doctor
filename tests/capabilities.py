"""What the machine running the tests lacks: declared by whoever runs the suite, never detected.

GitHub's Windows runners lack some of the owner's laptop: no GPU, no NVIDIA driver, no thermal sensors and no ping
replies from the network (measured on the first CI runs, 2026-11-07). Their High performance power plan, which has no
power-mode slider, needs no entry: the power tests hold on any plan. The CI
workflow lists what is absent in LAGDOCTOR_ABSENT (comma-separated names from CAPABILITIES), and
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
    "thermal_zone": "ACPI thermal-zone sensors",
    "nvidia": "NVIDIA GPU and driver (NVML and nvidia-smi)",
    # GitHub's windows-2025 runner has the runtime and opens the window (M0's probe, 2026-11-08), so CI declares
    # nothing; a PC without the runtime or a desktop (a service session) sets LAGDOCTOR_ABSENT=webview2.
    "webview2": "WebView2 Runtime and a desktop to open lag-doctor's window on",
    # The laptop and GitHub's windows-2025 runner both have the Windows SDK's headers, which tests/test_launch.py reads
    # Explorer's COM slots back from, so nothing declares it; a PC without the SDK sets LAGDOCTOR_ABSENT=windows_sdk.
    "windows_sdk": "Windows SDK C headers (Windows Kits 10 Include um)",
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
