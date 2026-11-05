"""The terminal summary: plain ASCII, wrapped to the console width.

ASCII because a redirected or old-codepage console cannot print every Unicode
character, and a report that crashes on an en dash helps nobody.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from lagdoctor.model import Capture, Diagnosis
from lagdoctor.report import labels
from lagdoctor.report.charts import fmt_clock

_PUNCT = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
          "…": "...", "→": "->", "×": "x", "•": "-", "·": "|", "≤": "<=",
          "≥": ">=", " ": " "}


def ascii_text(text: object) -> str:
    s = "" if text is None else str(text)
    for k, v in _PUNCT.items():
        s = s.replace(k, v)
    return s.encode("ascii", "replace").decode("ascii")


def _num(v, pattern="{:.0f}", suffix=""):
    return "-" if v is None else pattern.format(v) + suffix


def render(d: Diagnosis, cap: Capture, report_path: Path | None = None, width: int = 100) -> str:
    width = max(60, width)
    out: list[str] = []

    def para(text: str, indent: str = "", sub: str | None = None) -> None:
        out.extend(textwrap.wrap(ascii_text(text), width, initial_indent=indent,
                                 subsequent_indent=sub if sub is not None else indent) or [indent.rstrip()])

    game = cap.target.get("name") or "your game"
    para(f"lag-doctor report: {game} ({fmt_clock(cap.duration_s)} recorded, {cap.started_at[:16].replace('T', ' ')})")
    out.append("")
    lag = labels.LAG_TYPES.get(d.lag_type, d.lag_type).upper()
    para(f"VERDICT [{lag}] {d.headline}")
    out.append("")
    s = d.stats
    bound = s.get("bound")
    para(" | ".join([
        f"avg {_num(s.get('avg_fps'))} fps",
        f"1% low {_num(s.get('low1_fps'))} fps",
        f"{_num(s.get('stutters_per_min'), '{:.1f}')} stutters/min",
        f"worst frame {_num(s.get('worst_frame_ms'), suffix=' ms')}",
        f"limited by {labels.BOUND_SHORT.get(bound, bound) if bound else '-'}",
        f"ping {_num(s.get('ping_inet_ms'), suffix=' ms')}",
        f"loss {_num(s.get('loss_pct'), '{:.1f}', '%')}",
    ]), indent="  ")
    out.append("")
    if d.findings:
        out.append("What is causing it (most likely first):")
        for i, f in enumerate(d.findings[:3], 1):
            conf = labels.CONFIDENCE.get(f.confidence, f.confidence)
            para(f"{i}. {f.title} [{conf}]", indent="  ", sub="     ")
            for ev in f.evidence[:3]:
                para(f"- {ev.text}", indent="     ", sub="       ")
            for fx in f.fixes[:2]:
                if fx.fix_id:
                    para(f"> Fix it: {fx.title}", indent="     ", sub="       ")
                    out.append(f"       run: lagdoctor fix {ascii_text(fx.fix_id)}")   # never wrapped: it gets copied
                else:
                    para(f"> Try: {fx.title}" + (f": {fx.detail}" if fx.detail else ""),
                         indent="     ", sub="       ")
        if len(d.findings) > 3:
            out.append(f"  ... and {len(d.findings) - 3} more in the full report.")
    else:
        para("Nothing stood out. If the game felt laggy, record again and press the lag key the moment it "
             "happens.", indent="  ")
    if d.cleared:
        out.append("")
        para("Checked and fine: " + "; ".join(labels.cause_text(c) for c in sorted(d.cleared)), indent="", sub="  ")
    missing = [f"{src} ({st.detail or st.state})" for src, st in sorted(cap.sources.items()) if st.state != "ok"]
    if missing:
        out.append("")
        para("Not measured, so not ruled out: " + "; ".join(missing), indent="", sub="  ")
    if d.unchecked:
        out.append("")
        para("Could not check: " + "; ".join(f"{labels.cause_text(c)} ({why})" for c, why in sorted(d.unchecked.items())),
             indent="", sub="  ")
    if report_path:
        out.append("")
        para(f"Full report: {report_path}")
    return "\n".join(line.rstrip() for line in out) + "\n"
