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
