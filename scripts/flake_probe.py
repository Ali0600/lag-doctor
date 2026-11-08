"""Throwaway (never merged). Probe: why the real-private-session tracker test sometimes finds no delay gap.

Replays the test's exact scenario N times and records, per run: when the tracker's live loop ticked (first and
last advance), the finalised time at stop (final_t), how many game packets were late, the first on-time packet,
the boundary at which the server was first picked, and the gap-ending packet's time. Prints one JSON line per run
and a summary. Usage: python scripts/flake_probe.py N [reorder_s or -] [writer delays, comma-separated]
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid

from lagdoctor import clock
from lagdoctor.capture import netflows as nf
from lagdoctor.capture.netflows import FlowTable, FlowTracker
from lagdoctor.winapi import etw
from tests.test_etw_tracker import GAME, SERVER, _v4, _wait


def one_run(reorder_s=None, delay=0.0):
    rec = {"advance": [], "ticks": [], "adds": []}
    orig = {n: getattr(FlowTable, n) for n in ("advance", "_tick", "finish", "add")}

    def advance(self, now):
        rec["advance"].append(now)
        return orig["advance"](self, now)

    def tick(self, boundary):
        orig["_tick"](self, boundary)
        rec["ticks"].append((boundary, self._server is not None, self._server.rx_tick if self._server else None))

    def finish(self, end=None):
        rec["final_t"] = self._final_t
        rec["late_at_finish"] = self.counts["late"]
        return orig["finish"](self, end)

    def add(self, t, pkt):
        if pkt.pid == GAME and pkt.proto == "udp":
            rec["adds"].append((t, pkt.kind, t <= self._final_t))
        return orig["add"](self, t, pkt)

    FlowTable.advance, FlowTable._tick, FlowTable.finish, FlowTable.add = advance, tick, finish, add
    try:
        provider = etw.Provider(str(uuid.uuid4()))
        name = f"lagdoctor-test-{uuid.uuid4().hex[:12]}"
        sessions = []

        def factory(_name, _providers, on_event):
            sessions.append(etw.RealtimeSession(name, [(provider.guid, 4, 0x30)], on_event, private=True))
            return sessions[0]

        tr = FlowTracker({GAME}, selftest=False, session_factory=factory, names=lambda pids: {GAME: "game.exe"})
        if reorder_s is not None:
            tr._table.reorder_s = reorder_s
        recv = _v4(GAME, 1200, "127.0.0.1", SERVER, 50000, 27015)
        send = _v4(GAME, 100, SERVER, "127.0.0.1", 27015, 50000)
        try:
            tr.start(clock.now())
            assert _wait(lambda: sessions and sessions[0].health()["running"]), tr.status()
            if delay:
                time.sleep(delay)
            start = time.perf_counter()
            rec["ws"] = start - tr.origin
            for k in range(int(3.0 * 64)):
                target = start + k / 64
                while time.perf_counter() < target:
                    time.sleep(0.0005)
                if not 1.5 <= k / 64 < 1.8:
                    provider.write(43, recv, opcode=43, task=11, keyword=0x10)
                if k / 64 == 1.796875:
                    for _ in range(15):
                        provider.write(43, recv, opcode=43, task=11, keyword=0x10)
                provider.write(42, send, opcode=42, task=11, keyword=0x10)
            rec["we"] = time.perf_counter() - tr.origin
        finally:
            tr.stop()
            provider.close()
        h = sessions[0].health()
        r = tr.result()
    finally:
        for n, f in orig.items():
            setattr(FlowTable, n, f)
    delays = [g for g in r["gaps"] if g[2] == "delay"]
    ok = (h["events"] >= 2 * 192 - 20 + 15 and h["lost"] == 0 and r["server"] is not None
          and len(delays) == 1 and all(g[2] == "local" for g in r["gaps"] if g not in delays)
          and 280.0 < delays[0][1] < 340.0)
    rx = sorted(t for t, kind, late in rec["adds"] if kind == "recv")
    # The gap-ending packet: the first rx after the longest rx inter-arrival.
    gaps_rx = [(b - a, b) for a, b in zip(rx, rx[1:])]
    gap_end = max(gaps_rx)[1] if gaps_rx else None
    on_time = [t for t, kind, late in rec["adds"] if not late]
    first_pick = next((b for b, picked, _ in rec["ticks"] if picked), None)
    out = {
        "pass": ok,
        "gaps": r["gaps"],
        "events": h["events"],
        "lost": h["lost"],
        "s_first_tick": round(rec["advance"][0], 4) if rec["advance"] else None,
        "last_tick": round(rec["advance"][-1], 4) if rec["advance"] else None,
        "ws": round(rec["ws"], 4),
        "dw": round(rec["ws"] - rec["advance"][0], 4) if rec["advance"] else None,
        "we": round(rec["we"], 4),
        "final_t": round(rec["final_t"], 4),
        "late": r["health"]["late"],
        "first_on_time": round(min(on_time), 4) if on_time else None,
        "first_pick_boundary": first_pick,
        "gap_end": round(gap_end, 4) if gap_end is not None else None,
        "ws_frac": round(rec["ws"] % 1.0, 4),
        "status": tr.status().state,
        "ticks": rec["ticks"][:4],
    }
    # The mechanism's prediction: the gap is found iff the server was picked at a boundary at or before the
    # gap-ending packet.
    out["predicted_pass"] = first_pick is not None and gap_end is not None and first_pick <= gap_end
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    reorder = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    delays = [float(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else [0.0]
    results = []
    for i in range(n):
        d = delays[i % len(delays)]
        out = one_run(reorder, d)
        out["delay"] = d
        results.append(out)
        print(json.dumps(out), flush=True)
    fails = sum(not r["pass"] for r in results)
    agree = sum(r["pass"] == r["predicted_pass"] for r in results)
    late_runs = sum(r["late"] > 0 for r in results)
    line = (f"probe: runs={n} fails={fails} prediction_agrees={agree}/{n} runs_with_late_packets={late_runs} "
            f"reorder_s={reorder} Python {sys.version.split()[0]}")
    print("SUMMARY " + line, flush=True)
    rows = ["| run | pass | session start s | writer start ws | ws mod 1 | dw | last tick | final_t | late | "
            "first pick | gap end |", "|" + "---|" * 11]
    for i, r in enumerate(results, 1):
        rows.append(f"| {i} | {r['pass']} | {r['s_first_tick']} | {r['ws']} | {r['ws_frac']} | {r['dw']} | "
                    f"{r['last_tick']} | {r['final_t']} | {r['late']} | {r['first_pick_boundary']} | {r['gap_end']} |")
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"**{line}**

" + "
".join(rows) + "

")


if __name__ == "__main__":
    main()
