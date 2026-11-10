"""Plain-English labels shared by the console and HTML reports."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from lagdoctor.report.charts import fmt_elapsed, number

# A reason the capture's own sources carry can open with what the list already says it is about ("frame timing
# (PresentMon): frame timing was skipped: ..."); earlier builds wrote this prefix for every frame source that was off.
_SAID_ALREADY = re.compile(r"^\s*frame timing was skipped:\s*", re.IGNORECASE)
# Where a reason's first clause ends: its details (a path, a command, an error text) follow.
_CLAUSE_END = re.compile(r" \(|: |; |\. ")
SHORT_REASON_FALLBACK = "see Not measured"
GAME_NAME_MAX = 80                    # a window title longer than this is cut, never wrapped into the headline

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
    "cpu_throttle": "the CPU running below full speed",      # its clock alone can name it: no heat or power claim
    "gpu_throttle": "the GPU slowing down (heat or power)",
    "vram_overflow": "video memory over 90% full",
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
    "gpu_limit": "the GPU as the frame-rate limit",
    "cpu_limit": "the CPU as the frame-rate limit",
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
# What the headroom sentence (rules.headroom_text) rests on, said wherever it is shown. The busiest-thread figure is
# the lead thread's; in a capture saved before that column, the busiest of the game's threads at each sample, which
# reads high when several are busy. Work a game does per second does not grow with the frame rate either: both make
# the CPU's figure lean low, while work that moves between threads every few seconds can read low and lean it high
# (DESIGN, Headroom).
HEADROOM_NOTE = ("A rough estimate for this game at these settings, from its busiest thread and the GPU's time per "
                 "frame, not a benchmark: record again with lighter graphics and compare the two to check it.")

# A capture's sources (Capture.sources keys) in words, for the reports and the rules' "needs X" reasons.
SOURCES = {
    "frames": "frame timing (PresentMon)", "system": "the system sampler", "gpu": "NVIDIA telemetry (NVML)",
    "network": "the ping and Wi-Fi monitor", "net_flows": "the game's own network traffic (ETW)",
}

# Every column a chart can show (model.F, S, G, N), as (label, unit): the app's charts name a series only through this
# table. The columns left out are names, ids and addresses, never drawn as a line (NOT_CHARTED). Each model column but
# "t" is in exactly one of the two (tests/test_app_views.py).
COLUMNS = {
    # frames (PresentMon)
    "frame_ms": ("time between frames", "ms"),
    "cpu_busy_ms": ("CPU time per frame", "ms"),
    "cpu_wait_ms": ("CPU waiting per frame", "ms"),
    "gpu_latency_ms": ("CPU start to GPU start", "ms"),
    "gpu_busy_ms": ("GPU time per frame", "ms"),
    "gpu_wait_ms": ("GPU idle per frame", "ms"),
    "displayed": ("frame shown (1) or dropped (0)", ""),
    "display_ms": ("time each frame stayed on screen", "ms"),
    "display_latency_ms": ("CPU start to on screen", "ms"),
    "input_latency_ms": ("input to on screen", "ms"),
    "anim_error_ms": ("animation timing error", "ms"),
    "sync_interval": ("VSync interval (0 = off)", ""),
    "allows_tearing": ("tearing allowed (1) or not (0)", ""),
    "hybrid": ("copied between GPUs (1) or not (0)", ""),
    # system sampler
    "cpu_total_pct": ("CPU use, all cores", "%"),
    "cpu_max_core_pct": ("busiest CPU core", "%"),
    "cpu_perf_pct": ("CPU speed against its base clock", "%"),
    "cpu_perf_limit_pct": ("CPU speed limit (100 = none)", "%"),
    "thermal_limit_pct": ("CPU heat limit (100 = none)", "%"),
    "dpc_pct": ("driver work (DPC), busiest core", "%"),
    "interrupt_pct": ("interrupts, busiest core", "%"),
    "game_cpu_pct": ("the game's CPU use (100 = one core)", "%"),
    "game_top_thread_pct": ("the game's busiest thread at each sample (100 = one core)", "%"),
    "game_lead_thread_pct": ("the game's lead thread, the busiest of the second before (100 = one core)", "%"),
    "game_threads": ("the game's threads", ""),
    "game_ws_mb": ("the game's memory in use", "MB"),
    "game_private_mb": ("the game's private memory", "MB"),
    "game_page_faults_ps": ("the game's page faults", "/s"),
    "game_io_read_bps": ("the game's disk and file reads", "B/s"),
    "game_io_write_bps": ("the game's disk and file writes", "B/s"),
    "game_io_ops_ps": ("the game's read and write calls", "/s"),
    "game_focused": ("the game in front (1) or not (0)", ""),
    "game_gpu_pct": ("the game's GPU use", "%"),
    "gpu_pct": ("GPU use, all programs", "%"),
    "gpu_other_pct": ("GPU use by other programs", "%"),
    "vram_used_mb": ("video memory in use", "MB"),
    "game_vram_mb": ("the game's video memory", "MB"),
    "game_shared_mb": ("the game's data spilled into system RAM", "MB"),
    "ram_avail_mb": ("free RAM", "MB"),
    "commit_pct": ("memory committed", "%"),
    "hard_faults_ps": ("reads from disk for memory", "/s"),
    "disk_busy_pct": ("busiest disk", "%"),
    "disk_read_bps": ("disk reads, all disks", "B/s"),
    "disk_read_ms": ("disk read time", "ms"),
    "disk_queue": ("disk queue", ""),
    # NVIDIA (NVML)
    "nv_util_pct": ("NVIDIA GPU use", "%"),
    "nv_mem_used_mb": ("NVIDIA video memory in use", "MB"),
    "nv_temp_c": ("NVIDIA GPU temperature", "°C"),
    "nv_power_w": ("NVIDIA GPU power", "W"),
    "nv_power_limit_w": ("NVIDIA GPU power limit", "W"),
    "nv_clock_mhz": ("NVIDIA GPU clock", "MHz"),
    "nv_clock_max_mhz": ("NVIDIA GPU top clock", "MHz"),
    "nv_pstate": ("NVIDIA performance state (0 = fastest)", ""),
    "nv_throttle_pct": ("NVIDIA slowed by heat or power", "%"),
    "nv_power_cap_pct": ("NVIDIA at its power cap", "%"),
    # network
    "gw_rtt_ms": ("ping to your router", "ms"),
    "inet_rtt_ms": ("ping to the internet", "ms"),
    "server_rtt_ms": ("ping to the game server", "ms"),
    "wifi_signal_pct": ("Wi-Fi signal", "%"),
    "wifi_rssi_dbm": ("Wi-Fi signal strength", "dBm"),
    "wifi_channel": ("Wi-Fi channel", ""),
    "wifi_rx_mbps": ("Wi-Fi receive rate", "Mbit/s"),
    "wifi_tx_mbps": ("Wi-Fi send rate", "Mbit/s"),
    "wifi_retries_ps": ("Wi-Fi retries", "/s"),
    "nic_rx_bps": ("network download, all programs", "B/s"),
    "nic_tx_bps": ("network upload, all programs", "B/s"),
}
NOT_CHARTED = frozenset({"present_mode", "runtime", "swap_chain", "frame_type", "game_top_thread_id", "nv_reasons",
                         "wifi_bssid"})


def column_text(column: str) -> str:
    """A chart series' name in words, with its unit: "time between frames (ms)"."""
    label, unit = COLUMNS.get(column, (column.replace("_", " "), ""))
    return f"{label} ({unit})" if unit else label


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


# How the measured part of a recording was set (analysis/crop.py HOWS, which a test holds equal to these keys).
PART_HOW = {"key": "measured from your start key", "set": "the part you chose"}
PART_SLACK_S = 1e-3                     # rounding a saved diagnosis's seconds may leave


def measured_part(stats: Any) -> dict[str, Any] | None:
    """The measured part a diagnosis covers (Diagnosis.stats["span"], analysis/crop.py) as {from_s, to_s, recorded_s,
    how}, every value checked; None for a diagnosis of the whole recording, or a record that does not add up (a saved
    diagnosis can hold anything)."""
    span = stats.get("span") if isinstance(stats, dict) else None
    if not isinstance(span, dict):
        return None
    lo, hi, rec = (number(span.get(k)) for k in ("from_s", "to_s", "recorded_s"))
    how = span.get("how")
    if lo is None or hi is None or rec is None or not isinstance(how, str) or how not in PART_HOW \
            or not 0.0 <= lo < hi <= rec + PART_SLACK_S:
        return None
    return {"from_s": lo, "to_s": hi, "recorded_s": rec, "how": how}


def outside_marks(marks: Any, part: dict[str, Any] | None) -> list[float]:
    """The lag-key presses (a capture's `marks`) outside the measured part, which its analysis never saw: the capture
    keeps them all. A part that runs to the recording's end keeps the presses up to 1 s past it, as the recording
    does (capture/session.py)."""
    if part is None:
        return []
    lo, hi = part["from_s"], part["to_s"]
    late = hi + 1.0 if hi >= part["recorded_s"] - PART_SLACK_S else hi
    return [t for t in (number(m) for m in (marks if isinstance(marks, list) else [])) if t is not None
            and not lo <= t <= late]


def part_length(part: dict[str, Any]) -> str:
    """A measured part's length as the reports show it: the difference of its two ends as they are shown (each
    rounded to the second), so "from 13s to 47s" never reads as 35 s long."""
    return fmt_elapsed(round(part["to_s"]) - round(part["from_s"]))


def part_note(part: dict[str, Any], outside_marks: int = 0) -> str:
    """What the reports say about a measured part: 'Analysed 10m, from 3m 12s to 13m 12s of the recording (the part you
    chose); the rest is not counted.'"""
    lo, hi = part["from_s"], part["to_s"]
    text = (f"Analysed {part_length(part)}, from {fmt_elapsed(lo)} to {fmt_elapsed(hi)} of the recording "
            f"({PART_HOW[part['how']]}); the rest is not counted")
    if outside_marks > 0:
        text += f", nor the {outside_marks} lag-key press{'es' if outside_marks != 1 else ''} in it"
    return text + "."


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
    out = []
    for src, st in sorted(sources.items(), key=lambda kv: str(kv[0])):
        if getattr(st, "state", None) != "ok":
            out.append((source_text(str(src)), _why(getattr(st, "detail", None) or getattr(st, "state", None))))
        elif isinstance(measured.get(src), str) and measured[src] != "ok":    # ran, but the analysis had nothing of it
            out.append((source_text(str(src)), _why(measured[src])))
    out += [(source_text(str(src)), _why(why)) for src, why in sorted(measured.items(), key=lambda kv: str(kv[0]))
            if why != "ok" and src not in sources]
    return out


def _why(value: Any) -> str:
    return reason_text(value) if isinstance(value, str) and value.strip() else "not measured"


def reason_text(why: str) -> str:
    """A source's reason as the Not measured list shows it, beside the source's name: without an opening that repeats
    it ("frame timing (PresentMon): frame timing was skipped: PresentMon ... is not installed")."""
    return _SAID_ALREADY.sub("", why, count=1).strip() or why


def short_reason(why: Any) -> str:
    """A reason's first clause, for a sentence such as the verdict: no path, command or error text, which the Not
    measured list gives once in full. "PresentMon 2.6.0 is not installed (C:\\...). Run `lagdoctor setup` ..." is
    "PresentMon 2.6.0 is not installed"; a first clause that still holds a path or a command is no short reason."""
    if not isinstance(why, str) or not why.strip():
        return SHORT_REASON_FALLBACK
    text = _SAID_ALREADY.sub("", why.strip(), count=1)
    m = _CLAUSE_END.search(text)
    text = (text[:m.start()] if m else text).strip().rstrip(".;:,")
    if not text or "`" in text or "\\" in text or re.search(r"[A-Za-z]:/", text) or len(text) > 120:
        return SHORT_REASON_FALLBACK
    return text


def game_name(target: Any) -> str:
    """The game as a player knows it: its window's title when the capture has a sane one, else its exe name, else
    "your game". A title is the name the game shows; "ff7rebirth_.exe" is only a file."""
    target = target if isinstance(target, dict) else {}
    for key in ("window_title", "name"):
        v = target.get(key)
        if isinstance(v, str):
            s = " ".join(v.split())           # control characters stay for each report to show safely (SEC-8)
            if s:
                return s if len(s) <= GAME_NAME_MAX else s[:GAME_NAME_MAX - 3].rstrip() + "..."
    return "your game"


def group_unchecked(unchecked: dict[str, str]) -> list[tuple[str, list[str]]]:
    """(reason, [topic, ...]) with causes that share a reason listed together, biggest group first.

    A reason's hint after the first ';' (e.g. "press the lag key ...") is dropped, so causes blocked by the
    same missing source land in one group instead of one line each.
    """
    groups: dict[str, list[str]] = {}
    for cause, why in unchecked.items():
        key = (why or "not checked").split(";", 1)[0].strip()
        groups.setdefault(key, []).append(topic_text(cause))
    out = []
    for r, topics in groups.items():
        if len(topics) > 1 and r.startswith(NEEDS):   # "these need frame timing ...", for a list of them
            r = "these need " + r[len(NEEDS):]
        out.append((r, sorted(topics, key=str.casefold)))
    return sorted(out, key=lambda g: (-len(g[1]), g[0]))


def sorted_topics(causes: Any) -> list[str]:
    """The topics of these causes in alphabetical order, whatever their case ("CPU heat" with "copies", not before)."""
    return sorted((topic_text(c) for c in causes), key=str.casefold)


NEEDS = "needs "                      # how a reason that a missing source blocked begins (rules._Env.need)


def needs_source(source: str, in_part: bool = False) -> str:
    """Why a cause could not be checked when a source it needs was not measured. The source's own reason, path and
    command included, is in the Not measured list, once; repeating it here chained colons ("needs X, which was not
    measured: frame timing was skipped: ..."). `in_part`: the source recorded, but nothing in the measured part."""
    if in_part:
        return f"{NEEDS}{source_text(source)}, which recorded nothing in the measured part (see Not measured)"
    return f"{NEEDS}{source_text(source)}, which was not recorded (see Not measured)"


# ------------------------------------------------------------------ the game's files --

MAX_BYTES = 1 << 53                    # a file size past this is no size (a float holds every integer below it)


def byte_count(v: Any) -> int | None:
    """v as a file size: a whole number of bytes from 0 to MAX_BYTES, else None (charts.number stops at 1e7)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_BYTES else None


def size_text(n: Any) -> str | None:
    """1089 -> "1.1 KB"; bytes in powers of 1024, as Explorer shows them."""
    n = byte_count(n)
    if n is None:
        return None
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("1 byte" if n == 1 else f"{n:.0f} bytes") if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return None


def date_text(iso: Any) -> str | None:
    """"2026-11-06T12:20:52-08:00" -> "6 Nov 2026, 12:20", in the time zone the text names."""
    if not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return f"{dt.day} {dt:%b %Y, %H:%M}"


def file_text(name: Any, size: Any, iso: Any) -> str:
    """"dxgi.dll (1.1 MB, 6 Nov 2026, 12:20)"."""
    details = [x for x in (size_text(size), date_text(iso)) if x]
    return f"{name} ({', '.join(details)})" if details else str(name)


NO_EXE = "the game's file was not known"
_OS_ERROR = "OSError: "                 # how the inventory's errors name a folder read that failed (inventory._section)


def error_text(why: Any) -> str | None:
    """An inventory section's reason without the error type the inventory puts first: the folder check's own
    reasons are sentences ("a folder could not be read: Access is denied")."""
    if not isinstance(why, str) or not why.strip():
        return None
    why = why.strip()
    return why[len(_OS_ERROR):] if why.startswith(_OS_ERROR) else why
