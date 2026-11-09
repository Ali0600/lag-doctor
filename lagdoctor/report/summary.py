"""The small summary of an analysed session, summary.json, written beside its diagnosis at analysis time.

The app's library lists every recorded game from these, so it never loads a capture to draw a tile
(docs/LAUNCHER-PLAN.md, M1). It holds who and when (game, exe, start, tool version, kind) and the numbers the game page
and Compare show (the verdict's figures and the conditions a recording ran under, the game's own files among them).
A session saved before summaries existed has none; the library then reads its folder's name
(lagdoctor/app/library.py). Keys added to the schema later are optional: an older summary reads back with them None.
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
# What the game's folders held (capture/gamefiles.py), added after the schema's first build: a summary written
# before them has none of these keys, and reads back with each None.
GAME_FILE_KEYS = ("game_exe", "drop_ins", "unreal_config")
CHECKED, NOT_CHECKED, NOT_UNREAL = "checked", "not_checked", "not_unreal"
MAX_LISTED = 30                       # files a list keeps; the rest are counted
SHA_CHARS = 16                        # of a file's SHA-256: enough to tell two copies apart
NO_EXE = labels.NO_EXE


def _text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = "".join(ch if ch.isprintable() else " " for ch in v).strip()
    return v[:MAX_TEXT] or None


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _sha(v: Any) -> str | None:
    return v.lower()[:SHA_CHARS] if isinstance(v, str) and len(v) >= SHA_CHARS and all(
        ch in "0123456789abcdefABCDEF" for ch in v) else None


def _not_checked(game: dict, errors: dict, section: str) -> dict[str, Any]:
    why = labels.error_text(errors.get(f"game.{section}"))
    return {"state": NOT_CHECKED, "why": _text(why) or (NO_EXE if not game.get("exe") else None), "files": [],
            "more": 0}


def _listed(files: list, more: int) -> dict[str, Any]:
    """[[name, sha, bytes, modified], ...] of the first MAX_LISTED good entries, and how many more there were."""
    rows = []
    for f in files:
        name = _text(f.get("name")) if isinstance(f, dict) else None
        if name is None:
            continue
        if len(rows) >= MAX_LISTED:
            more += 1
            continue
        rows.append([name, _sha(f.get("sha256")), labels.byte_count(f.get("bytes")), _text(f.get("modified"))])
    return {"state": CHECKED, "why": None, "files": rows, "more": more}


def game_files(inv: dict) -> dict[str, Any]:
    """The summary's game_exe, drop_ins and unreal_config from a capture's inventory; each None when the capture
    is older than the folder check."""
    game = inv.get("game") if isinstance(inv.get("game"), dict) else {}
    errors = inv.get("errors") if isinstance(inv.get("errors"), dict) else {}
    out: dict[str, Any] = dict.fromkeys(GAME_FILE_KEYS)
    if "exe_bytes" in game:
        if labels.byte_count(game.get("exe_bytes")) is None:
            nc = _not_checked(game, errors, "exe_file")
            out["game_exe"] = {"state": NOT_CHECKED, "why": nc["why"], "bytes": None, "modified": None}
        else:
            out["game_exe"] = {"state": CHECKED, "why": None, "bytes": labels.byte_count(game["exe_bytes"]),
                               "modified": _text(game.get("exe_modified"))}
    if "drop_ins" not in game:
        return out
    drop, ue = game.get("drop_ins"), game.get("unreal")
    ue = ue if isinstance(ue, dict) else None
    if isinstance(drop, dict) and isinstance(drop.get("files"), list):
        # An Unreal game's ~mods paks are drop-in files too.
        is_ue = ue is not None and ue.get("found") is True and isinstance(ue.get("mods"), list)
        out["drop_ins"] = _listed(drop["files"] + (ue["mods"] if is_ue else []),
                                  _count(drop.get("more")) + (_count(ue.get("mods_more")) if is_ue else 0))
    else:
        out["drop_ins"] = _not_checked(game, errors, "drop_ins")
    if ue is None or not isinstance(ue.get("config"), list):
        out["unreal_config"] = _not_checked(game, errors, "unreal")
    elif ue.get("found") is not True:
        out["unreal_config"] = {"state": NOT_UNREAL, "why": None, "files": [], "more": 0}
    else:
        out["unreal_config"] = _listed(ue["config"], _count(ue.get("config_more")))
    return out


def _read_files(v: Any, states: tuple[str, ...]) -> dict[str, Any] | None:
    """A drop_ins or unreal_config field as read back: None unless every part has its type."""
    if not isinstance(v, dict) or v.get("state") not in states or not isinstance(v.get("files"), list) \
            or len(v["files"]) > MAX_LISTED:
        return None
    rows = []
    for row in v["files"]:
        if not isinstance(row, list) or len(row) != 4 or _text(row[0]) is None:
            return None
        rows.append([_text(row[0]), _sha(row[1]), labels.byte_count(row[2]), _text(row[3])])
    return {"state": v["state"], "why": _text(v.get("why")), "files": rows, "more": _count(v.get("more"))}


def _read_exe(v: Any) -> dict[str, Any] | None:
    if not isinstance(v, dict) or v.get("state") not in (CHECKED, NOT_CHECKED):
        return None
    return {"state": v["state"], "why": _text(v.get("why")), "bytes": labels.byte_count(v.get("bytes")),
            "modified": _text(v.get("modified"))}


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
        **game_files(inv),
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
    out["game_exe"] = _read_exe(data.get("game_exe"))
    out["drop_ins"] = _read_files(data.get("drop_ins"), (CHECKED, NOT_CHECKED))
    out["unreal_config"] = _read_files(data.get("unreal_config"), (CHECKED, NOT_CHECKED, NOT_UNREAL))
    return out
