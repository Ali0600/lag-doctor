"""The terminal summary: plain ASCII, wrapped to the console width.

ASCII because a redirected or old-codepage console cannot print every Unicode
character, and a report that crashes on an en dash helps nobody. Control
characters are shown as '?': a process name or a shared capture must never send
an escape sequence to the terminal (SEC-8). Each part is built on its own, so a
damaged capture that breaks one part still gets the rest of the summary (A15).
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, Callable

from lagdoctor.model import Capture, Diagnosis
from lagdoctor.report import labels
from lagdoctor.report.charts import fmt_elapsed, number

# "89 °C" reads "89 C". Invisible ones are escapes: a literal no-break space looks like a space, and was lost as one.
_PUNCT = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
          "…": "...", "→": "->", "×": "x", "•": "-", "·": "|", "≤": "<=",
          "≥": ">=", "°": "", "\u00a0": " ", "\ufeff": ""}
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")     # every C0 control and DEL but the newline (as cli.say)
MAX_MARKS = 5                # lines of matched marks before "... and N more"


def ascii_text(text: object) -> str:
    s = "" if text is None else str(text)
    for k, v in _PUNCT.items():
        s = s.replace(k, v)
    return _CONTROL.sub("?", s.encode("ascii", "replace").decode("ascii"))


def _num(v, pattern="{:.0f}", suffix=""):
    v = number(v)                # junk, NaN or a corrupt 1e308 reads as not measured
    return "-" if v is None else pattern.format(v) + suffix


def _word(table: dict, key: Any) -> str:
    """table[key] for a string key, the key itself when the table lacks it; "-" for anything else (a damaged
    value is never a lookup)."""
    return table.get(key, key) if isinstance(key, str) else "-"


class _Out:
    def __init__(self, width: int) -> None:
        self.width, self.lines = width, []

    def para(self, text: str, indent: str = "", sub: str | None = None) -> None:
        # never at a hyphen or inside a word: a path broke at "lag-" once, and a broken path cannot be copied
        self.lines.extend(textwrap.wrap(ascii_text(text), self.width, initial_indent=indent,
                                        subsequent_indent=sub if sub is not None else indent,
                                        break_on_hyphens=False, break_long_words=False) or [indent.rstrip()])

    def add(self, line: str = "") -> None:
        self.lines.append(ascii_text(line))


def render(d: Diagnosis, cap: Capture, report_path: Path | None = None, width: int = 100,
           checkup: bool = False) -> str:
    """The summary. checkup=True is `lagdoctor check`: settings only, no game was recorded."""
    out = _Out(max(60, width))
    parts: list[Callable[[], None]] = [lambda: _header(out, d, cap, checkup), lambda: _findings(out, d, checkup)]
    if not checkup:
        parts.append(lambda: _marks(out, d))
    parts += [lambda: _cleared(out, d), lambda: _missing(out, d, cap, checkup)]
    for part in parts:
        try:
            part()
        except Exception as exc:  # noqa: BLE001 - one broken part must not lose the summary; it says so
            out.add(f"(Part of this summary could not be shown from this capture: {type(exc).__name__}.)")
    if report_path:
        out.add()
        out.add("Full report:")
        out.add(f"  {report_path}")             # on a line of its own, never wrapped: it gets copied
    return "\n".join(line.rstrip() for line in out.lines) + "\n"


def _header(out: _Out, d: Diagnosis, cap: Capture, checkup: bool) -> None:
    started = cap.started_at if isinstance(cap.started_at, str) else ""
    when = started[:16].replace("T", " ")
    if checkup:
        out.para(f"lag-doctor settings check-up (no game recorded, {when})")
        out.add()
        return
    part = labels.measured_part(d.stats)
    analysed = f"{fmt_elapsed(part['to_s'] - part['from_s'])} analysed, " if part else ""
    out.para(f"lag-doctor report: {labels.game_name(cap.target)} ({fmt_elapsed(cap.duration_s)} recorded, "
             f"{analysed}started {when})")
    if part:
        out.para(labels.part_note(part, len(labels.outside_marks(cap.marks, part))))
    out.add()
    lag = _word(labels.LAG_TYPES, d.lag_type).upper()
    out.para(f"VERDICT [{lag}] {d.headline}")
    out.add()
    s = d.stats if isinstance(d.stats, dict) else {}
    bound = s.get("bound")
    out.para(" | ".join([
        f"avg {_num(s.get('avg_fps'))} fps",
        f"1% low {_num(s.get('low1_fps'))} fps",
        f"{_num(s.get('stutters_per_min'), '{:.1f}')} stutters/min",
        f"worst frame {_num(s.get('worst_frame_ms'), suffix=' ms')}",
        f"limited by {_word(labels.BOUND_SHORT, bound) if bound else '-'}",
        f"ping {_num(s.get('ping_inet_ms'), suffix=' ms')}",
        f"loss {_num(s.get('loss_pct'), '{:.1f}', '%')}",
    ]), indent="  ")
    out.add()
    room = _headroom(d)
    if room:     # how close the next limit is, beside "limited by": shown once here, so never among the findings
        out.para("Headroom: " + " ".join(room), indent="  ", sub="  ")
        out.add()


def _headroom(d: Diagnosis) -> list[str]:
    """The headroom sentence and its note (Diagnosis.stats "headroom", "headroom_note"), or nothing."""
    s = d.stats if isinstance(d.stats, dict) else {}
    room = s.get("headroom")
    if not isinstance(room, str) or not room.strip():
        return []
    note = s.get("headroom_note")
    return [room] + ([note] if isinstance(note, str) and note.strip() else [])


def _findings(out: _Out, d: Diagnosis, checkup: bool) -> None:
    if d.findings:
        out.add("Settings that can cost performance (most likely first):" if checkup else
                "What is causing it (most likely first):")
        shown_above = set(_headroom(d))
        for i, f in enumerate(d.findings[:3], 1):
            conf = _word(labels.CONFIDENCE, f.confidence)
            out.para(f"{i}. {f.title} [{conf}]", indent="  ", sub="     ")
            for ev in [x for x in f.evidence if not (isinstance(x.text, str) and x.text in shown_above)][:3]:
                out.para(f"- {ev.text}", indent="     ", sub="       ")
            for fx in f.fixes[:2]:
                if fx.fix_id:
                    out.para(f"> Fix it: {fx.title}", indent="     ", sub="       ")
                    out.add(f"       run: lagdoctor fix {ascii_text(fx.fix_id)}")   # never wrapped: it gets copied
                else:
                    out.para(f"> Try: {fx.title}" + (f": {fx.detail}" if fx.detail else ""),
                             indent="     ", sub="       ")
        if len(d.findings) > 3:
            out.add(f"  ... and {len(d.findings) - 3} more in the full report.")
    elif checkup:
        out.para("Nothing in the settings stood out.", indent="  ")
    else:
        out.para("Nothing stood out. If the game felt laggy, record again and press the lag key the moment it "
                 "happens.", indent="  ")


def _marks(out: _Out, d: Diagnosis) -> None:
    """CC-5: the user's lag-key presses and what was found at each, also when frame timing was not measured."""
    rows = labels.marks(d.stats, d.events)
    if not rows:
        return
    frames_ok = str((d.measured or {}).get("frames", "")).startswith(("ok", "partial"))
    out.add()
    out.para(labels.marks_summary(rows, frames_ok), sub="  ")
    matched = [(t, found) for t, found, _ in rows if found]
    for t, found in matched[:MAX_MARKS]:
        out.para(f"- {fmt_elapsed(t)}: " + "; ".join(labels.event_text(ev) for ev in found[:3]), indent="  ",
                 sub="    ")
    if len(matched) > MAX_MARKS:
        out.add(f"  ... and {len(matched) - MAX_MARKS} more in the full report.")
    moved = [(t, causes) for t, found, causes in rows if not found and causes]
    for t, causes in moved[:MAX_MARKS]:
        out.para(f"- {fmt_elapsed(t)}: no lag event; meanwhile " + ", ".join(labels.cause_text(c) for c in causes),
                 indent="  ", sub="    ")
    quiet = [fmt_elapsed(t) for t, found, causes in rows if not found and not causes]
    if quiet:
        out.para("- nothing at " + ", ".join(quiet[:12]) + (f" and {len(quiet) - 12} more" if len(quiet) > 12 else ""),
                 indent="  ", sub="    ")


def _cleared(out: _Out, d: Diagnosis) -> None:
    if d.cleared:
        out.add()
        out.para("Checked and fine: " + "; ".join(labels.sorted_topics(d.cleared)), indent="", sub="  ")


def _missing(out: _Out, d: Diagnosis, cap: Capture, checkup: bool) -> None:
    if checkup:
        if d.unchecked:
            out.add()
            out.para(f"Not checked without a recording ({len(d.unchecked)} more causes, such as frame rate, stutter, "
                     f"CPU and GPU load, and the network): run `lagdoctor watch` while you play.", indent="", sub="  ")
        return
    missing = labels.not_measured(cap.sources, d.measured)
    if missing:
        out.add()
        out.add("Not measured, so not ruled out:")
        for name, why in missing:
            out.para(f"- {name}: {why}", indent="  ", sub="    ")
    if d.unchecked:
        out.add()
        out.add("Could not check:")
        for why, topics in labels.group_unchecked(d.unchecked):
            out.para(f"- {', '.join(topics)}: {why}", indent="  ", sub="    ")
