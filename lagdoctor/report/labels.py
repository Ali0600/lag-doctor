"""Plain-English labels shared by the console and HTML reports."""
from __future__ import annotations

from typing import Any

from lagdoctor.report.charts import number

LAG_TYPES = {
    "none": "No lag found",
    "low_fps": "Low frame rate",
    "stutter": "Stutter",
    "pacing": "Uneven frame pacing",
    "network": "Network lag",
    "mixed": "Several problems",
    "unknown": "Not enough data",
}

CONFIDENCE = {"high": "likely", "medium": "possible", "low": "worth checking"}

EVENT_KINDS = {
    "stutter": "Stutter",
    "fps_drop": "FPS drop",
    "pacing": "Uneven pacing",
    "net_spike": "Ping spike",
    "net_loss": "Packet loss",
    "net_gap": "Server went quiet",
    "mark": "You pressed the lag key",
}

# Per-event causes (analysis/attribute.py) and session findings (analysis/rules.py).
CAUSES = {
    "game_cpu_spike": "the game's own CPU work",
    "asset_streaming": "loading from disk",
    "memory_paging": "out of RAM (paging to disk)",
    "background_cpu": "another program using the CPU",
    "background_disk": "another program using the disk",
    "background_gpu": "another program using the GPU",
    "driver_dpc": "a driver holding up the CPU",
    "cpu_throttle": "the CPU slowing down (heat or power)",
    "gpu_throttle": "the GPU slowing down (heat or power)",
    "vram_overflow": "video memory full",
    "gpu_spike": "a moment of heavy GPU work",
    "game_stall": "the game waiting on itself",
    "wrong_gpu": "running on the wrong GPU",
    "hybrid_copy": "frames copied between GPUs",
    "gpu_limit": "GPU at its limit",
    "cpu_limit": "CPU at its limit",
    "gpu_contention": "another program using the game's GPU",
    "ram_pressure": "low on memory (RAM or the page file)",
    "power_plan": "power-saving mode",
    "refresh_rate": "screen refresh rate below its maximum",
    "vsync_judder": "VSync judder",
    "dropped_frames": "frames dropped (the game runs faster than the screen)",
    "uneven_pacing": "uneven frame pacing",
    "windowed_composition": "windowed mode adding delay",
    "game_dvr": "background recording on",
    "game_mode_off": "Game Mode off",
    "background_hogs": "background programs",
    "hdd_streaming": "game on a hard drive",
    "wifi_weak": "weak Wi-Fi signal",
    "wifi_roaming": "Wi-Fi switching access points",
    "wifi_scan_spikes": "Wi-Fi background scans",
    "bufferbloat": "connection congested (bufferbloat)",
    "isp_latency": "internet provider latency",
    "server_lag": "game server or route",
    "packet_loss": "packet loss",
    "bandwidth_hog": "a download using your bandwidth",
    "not_focused": "the game not being the active window",
    "frame_cap": "a frame-rate cap",
    "shader_compile": "shader compilation",
    "vpn_route": "traffic going through a VPN",
}

# The neutral name of what each cause is about, for lists that are not problems: "checked and fine"
# and "could not check". CAUSES reads as a problem ("Game Mode off"), which in "checked and fine"
# would say the opposite of what was found.
TOPICS = {
    "game_cpu_spike": "the game's own CPU work",
    "asset_streaming": "loading from disk",
    "memory_paging": "paging to disk",
    "background_cpu": "other programs' CPU use",
    "background_disk": "other programs' disk use",
    "background_gpu": "other programs' GPU use",
    "driver_dpc": "driver delays (DPC and interrupts)",
    "cpu_throttle": "CPU heat and power limits",
    "gpu_throttle": "GPU heat and power limits",
    "vram_overflow": "video memory",
    "gpu_spike": "moments of heavy GPU work",
    "game_stall": "the game waiting on itself",
    "wrong_gpu": "which GPU the game runs on",
    "hybrid_copy": "copies between GPUs",
    "gpu_limit": "GPU load",
    "cpu_limit": "CPU load",
    "gpu_contention": "other programs on the game's GPU",
    "ram_pressure": "RAM and the page file",
    "power_plan": "power plan",
    "refresh_rate": "screen refresh rate",
    "vsync_judder": "VSync judder",
    "dropped_frames": "dropped frames",
    "uneven_pacing": "frame pacing",
    "windowed_composition": "windowed-mode presentation",
    "game_dvr": "background recording",
    "game_mode_off": "Game Mode",
    "background_hogs": "background programs",
    "hdd_streaming": "the game's drive",
    "wifi_weak": "Wi-Fi signal",
    "wifi_roaming": "Wi-Fi access point changes",
    "wifi_scan_spikes": "Wi-Fi background scans",
    "bufferbloat": "the connection under load (bufferbloat)",
    "isp_latency": "internet provider latency",
    "server_lag": "the game server and its route",
    "packet_loss": "packet loss",
    "bandwidth_hog": "downloads competing for bandwidth",
    "not_focused": "whether the game had focus",
    "frame_cap": "frame-rate cap",
    "shader_compile": "shader compilation",
    "vpn_route": "VPN routing",
}

# What limited the frame rate (analysis/frames.bound verdicts): a short form for a stat tile,
# and a phrase for sentences.
BOUND_SHORT = {
    "gpu": "GPU", "cpu_main": "CPU (1 thread)", "cpu_all": "CPU", "waiting": "waiting",
    "vsync": "VSync", "capped": "FPS cap", "mixed": "CPU + GPU", "unknown": "-",
}
BOUND = {
    "gpu": "the graphics card (GPU)",
    "cpu_main": "one busy game thread on the CPU",
    "cpu_all": "the CPU, on every core",
    "waiting": "neither the CPU nor the GPU: the game is waiting on something",
    "vsync": "VSync: the screen's refresh rate",
    "capped": "a frame-rate cap",
    "mixed": "both the CPU and the GPU",
    "unknown": "not measured",
}

# A capture's sources (Capture.sources keys) in words, for the reports and the rules' "needs X" reasons.
SOURCES = {
    "frames": "frame timing (PresentMon)", "system": "the system sampler", "gpu": "NVIDIA telemetry (NVML)",
    "network": "the ping and Wi-Fi monitor", "net_flows": "the game's own network traffic (ETW)",
}

# The power-mode slider's positions (inventory power.slider) and the plan types (power.personality) that have no
# slider: Windows shows it only on Balanced and plans derived from it (winapi/power.py).
POWER_MODES = ("best_efficiency", "balanced", "best_performance")
PLAN_TYPES_WITHOUT_POWER_MODE = ("high_performance", "power_saver")


def severity_text(kind: str, severity: float, detail: dict | None = None) -> str:
    """An event's severity in words. A pacing event flagged by its animation error alone (events.py's
    detail["criterion"] "anim") carries that error as its severity, not a swing (A11). A severity that is not a
    sane number (a damaged capture, A15) reads "-"."""
    if number(severity) is None:
        return "-"
    if kind == "stutter":
        return f"{severity:.0f} ms frame"
    if kind == "fps_drop":
        return f"{severity:.0f}% below normal"
    if kind == "pacing":
        anim = isinstance(detail, dict) and detail.get("criterion") == "anim"
        return f"{severity:.1f} ms animation error" if anim else f"{severity:.1f} ms swings"
    if kind == "net_spike":
        return f"{severity:.0f} ms ping"
    if kind == "net_loss":
        return f"{severity:.0f}% lost"
    if kind == "net_gap":
        return f"{severity:.0f} ms silence"
    return ""


def event_text(ev) -> str:
    """'Stutter (160 ms frame)': an event's kind and how bad it was."""
    kind = ev.kind if isinstance(ev.kind, str) else "?"
    sev = severity_text(kind, ev.severity, ev.detail)
    return EVENT_KINDS.get(kind, kind) + (f" ({sev})" if sev else "")


def marks(stats: dict, events: list) -> list[tuple[float, list, list[str]]]:
    """CC-5: (time, [the lag events in its window], [signals that moved]) for each of the user's lag-key presses,
    from Diagnosis.stats["marks"] (analysis/diagnose.mark_list); entries a damaged file spoiled are left out."""
    raw = stats.get("marks") if isinstance(stats, dict) else None
    by_id = {ev.id: ev for ev in events or [] if isinstance(getattr(ev, "id", None), int)}
    out = []
    for m in raw if isinstance(raw, list) else []:
        t = number(m.get("t")) if isinstance(m, dict) else None
        if t is None:
            continue
        ids = m.get("events") if isinstance(m.get("events"), list) else []
        found = [by_id[i] for i in ids if isinstance(i, int) and not isinstance(i, bool) and i in by_id]
        causes = [c for c in m.get("causes") or [] if isinstance(c, str)] if isinstance(m.get("causes"), list) else []
        out.append((t, found, causes))
    return sorted(out, key=lambda r: r[0])


def marks_summary(rows: list, frames_measured: bool) -> str:
    """'You marked 4 moments: 1 matched a stutter, FPS drop or network event; 3 showed nothing in what was measured.'
    plus what that means for the moments without an event (LC 5.2, 6). A moment with no event where the system
    sampler saw a signal move (attributed when frames were not measured) is counted apart."""
    n = len(rows)
    matched = sum(1 for _, found, _ in rows if found)
    moved = sum(1 for _, found, causes in rows if not found and causes)
    quiet = n - matched - moved
    parts = ([f"{matched} matched a stutter, FPS drop or network event"] if matched else []) + \
            ([f"{moved} had no lag event but a system signal moved"] if moved else []) + \
            ([f"{quiet} showed nothing in what was measured"] if quiet else [])
    text = f"You marked {n} moment{'' if n == 1 else 's'}: " + "; ".join(parts) + "."
    if n - matched:
        text += (" No stutter or FPS drop was measured at those moments." if frames_measured else
                 " Frame timing was not measured, so a stutter at those moments could not be seen.")
    return text


def cause_text(cause: str) -> str:
    return CAUSES.get(cause, cause.replace("_", " "))


def topic_text(cause: str) -> str:
    return TOPICS.get(cause, cause.replace("_", " "))


def source_text(source: str) -> str:
    return SOURCES.get(source, source.replace("_", " "))


def plan_has_power_mode(power: Any) -> bool | None:
    """Whether a capture's active plan has the power-mode slider: True for a Balanced type, False for a High
    performance or Power saver type, None when the type was not read (as in captures saved before it was)."""
    kind = power.get("personality") if isinstance(power, dict) else None
    if kind == "balanced":
        return True
    if kind in PLAN_TYPES_WITHOUT_POWER_MODE:
        return False
    return None


def power_mode(power: Any) -> str | None:
    """The slider position a capture holds ("best_efficiency", "balanced" or "best_performance"); None when it was
    not read, and on a plan type without the slider, whatever position the capture holds."""
    if not isinstance(power, dict) or plan_has_power_mode(power) is False:
        return None
    mode = power.get("slider")
    return mode if mode in POWER_MODES else None


def power_mode_text(power: Any) -> str:
    """The power mode in plain words, for the PC details."""
    if plan_has_power_mode(power) is False:
        return "no power mode on this plan"
    mode = power_mode(power)
    return f"power mode {mode.replace('_', ' ')}" if mode else "power mode ?"


def not_measured(sources: Any, measured: Any) -> list[tuple[str, str]]:
    """(source in words, why) for every source that did not run cleanly, for both reports: the capture's own
    list (Capture.sources), then the sources the analysis expected but the capture never recorded
    (Diagnosis.measured). An unknown source is still listed, by its id in words."""
    sources = sources if isinstance(sources, dict) else {}
    measured = measured if isinstance(measured, dict) else {}
    out = [(source_text(str(src)), _why(getattr(st, "detail", None) or getattr(st, "state", None)))
           for src, st in sorted(sources.items(), key=lambda kv: str(kv[0])) if getattr(st, "state", None) != "ok"]
    out += [(source_text(str(src)), _why(why)) for src, why in sorted(measured.items(), key=lambda kv: str(kv[0]))
            if why != "ok" and src not in sources]
    return out


def _why(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else "not measured"


def group_unchecked(unchecked: dict[str, str]) -> list[tuple[str, list[str]]]:
    """(reason, [topic, ...]) with causes that share a reason listed together, biggest group first.

    A reason's hint after the first ';' (e.g. "press the lag key ...") is dropped, so causes blocked by the
    same missing source land in one group instead of one line each.
    """
    groups: dict[str, list[str]] = {}
    for cause, why in unchecked.items():
        key = (why or "not checked").split(";", 1)[0].strip()
        groups.setdefault(key, []).append(topic_text(cause))
    return sorted(((r, sorted(t)) for r, t in groups.items()), key=lambda g: (-len(g[1]), g[0]))
