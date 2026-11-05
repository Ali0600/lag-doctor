"""Plain-English labels shared by the console and HTML reports."""
from __future__ import annotations

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
    "ram_pressure": "low on RAM",
    "power_plan": "power-saving mode",
    "refresh_rate": "screen refresh rate below its maximum",
    "vsync_judder": "VSync judder",
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
    "ram_pressure": "RAM",
    "power_plan": "power plan",
    "refresh_rate": "screen refresh rate",
    "vsync_judder": "VSync judder",
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


def severity_text(kind: str, severity: float) -> str:
    if kind == "stutter":
        return f"{severity:.0f} ms frame"
    if kind == "fps_drop":
        return f"{severity:.0f}% below normal"
    if kind == "pacing":
        return f"{severity:.1f} ms swings"
    if kind == "net_spike":
        return f"{severity:.0f} ms ping"
    if kind == "net_loss":
        return f"{severity:.0f}% lost"
    if kind == "net_gap":
        return f"{severity:.0f} ms silence"
    return ""


def cause_text(cause: str) -> str:
    return CAUSES.get(cause, cause.replace("_", " "))


def topic_text(cause: str) -> str:
    return TOPICS.get(cause, cause.replace("_", " "))


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
