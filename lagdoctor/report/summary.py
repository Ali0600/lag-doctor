"""The small summary of an analysed session, summary.json, written beside its diagnosis at analysis time.

The app's library lists every recorded game from these, so it never loads a capture to draw a tile
(docs/LAUNCHER-PLAN.md, M1). It holds who and when (game, exe, start, tool version, kind) and the numbers the game page
and Compare show (the verdict's figures and the conditions a recording ran under). A session saved before summaries
existed has none; the library then reads its folder's name (lagdoctor/app/library.py).
"""
from __future__ import annotations

import json
import ntpath
from pathlib import Path
from typing import Any

from lagdoctor import __version__
from lagdoctor.model import Capture, Diagnosis
from lagdoctor.report import labels
from lagdoctor.report.charts import number

SUMMARY_NAME = "summary.json"
SCHEMA = "lagdoctor.summary/1"
KINDS = ("watch", "analyze")         # a recording, or a saved capture analysed again (analyze)
MAX_BYTES = 64 * 1024                 # a summary is a few hundred bytes: anything larger is not one
MAX_TEXT = 300                        # longest text field kept (a game's own window title can be anything)
NUMBERS = ("duration_s", "frames", "avg_fps", "low1_fps", "stutters_per_min", "worst_frame_ms", "refresh_hz")
TEXTS = ("kind", "tool_version", "capture_tool_version", "game", "exe", "exe_name", "started_at", "lag_type",
         "headline", "top_cause", "frames_source", "power_plan", "power_mode", "launcher")
BOOLS = ("frames_measured",)


def _text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = "".join(ch if ch.isprintable() else " " for ch in v).strip()
    return v[:MAX_TEXT] or None


def build(d: Diagnosis, cap: Capture, kind: str) -> dict[str, Any]:
    """The summary of one analysed capture. `kind` is "watch" (a recording) or "analyze"."""
    if kind not in KINDS:
        raise ValueError(f"unknown session kind {kind!r}")
    target = cap.target if isinstance(cap.target, dict) else {}
    stats = d.stats if isinstance(d.stats, dict) else {}
    inv = cap.inventory if isinstance(cap.inventory, dict) else {}
    power = inv.get("power") if isinstance(inv.get("power"), dict) else {}
    game = inv.get("game") if isinstance(inv.get("game"), dict) else {}
    exe = _text(target.get("exe"))
    frames_src = cap.sources.get("frames") if isinstance(cap.sources, dict) else None
    top = d.findings[0] if d.findings else None
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": kind,
        "tool_version": __version__,
        "capture_tool_version": _text(cap.tool_version),
        "game": _text(labels.game_name(target)),
        "exe": exe,
        "exe_name": _text(target.get("name")) or (_text(ntpath.basename(exe)) if exe else None),
        "started_at": _text(cap.started_at),
        "duration_s": number(cap.duration_s),
        "lag_type": _text(d.lag_type),
        "headline": _text(d.headline),
        "frames": number(stats.get("frames")),
        "avg_fps": number(stats.get("avg_fps")),
        "low1_fps": number(stats.get("low1_fps")),
        "stutters_per_min": number(stats.get("stutters_per_min")),
        "worst_frame_ms": number(stats.get("worst_frame_ms")),
        "top_cause": _text(top.cause) if top else None,
        "frames_source": _text(stats.get("frames_source")),
        "frames_measured": getattr(frames_src, "state", None) in ("ok", "partial"),
        "refresh_hz": number(stats.get("refresh_hz")),
        "power_plan": _text(power.get("scheme_name")),
        "power_mode": _text(labels.power_mode(power)),
        "launcher": _text(game.get("launcher")),
    }
    return out


def read(folder: Path) -> dict[str, Any] | None:
    """The summary in `folder`, every field checked (one of the wrong type reads as None), or None when there is
    none, it is too large, or it is not a summary of this schema."""
    path = Path(folder) / SUMMARY_NAME
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("kind") not in KINDS:
        return None
    out: dict[str, Any] = {"schema": SCHEMA}
    for key in TEXTS:
        out[key] = _text(data.get(key))
    for key in NUMBERS:
        out[key] = number(data.get(key))
    for key in BOOLS:
        out[key] = data.get(key) if isinstance(data.get(key), bool) else None
    return out
