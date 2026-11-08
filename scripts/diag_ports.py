"""Throwaway diagnostic (diag/** branches only, never merged).

Why do the tests' loopback DNS stand-ins (tests/test_etw_flows.py and tests/test_etw_tracker.py) sometimes find no
port number free for both UDP and TCP on GitHub's runners? They bind TCP to port 0 and try UDP on the same number,
10 times. Measured here:
  - how the runner hands out port-0 ports (the step, whether the cursor is shared across processes and by connect());
  - its dynamic port range and excluded port ranges, per protocol (netsh);
  - the stand-ins' step (TCP port 0, then UDP on that number) swept once around the dynamic range, every failure
    with its error code, and the share of start positions where 10 (20, 40) steps in a row all fail;
  - the real stand-in classes started at every 32nd position around the range, and inside the longest blocked runs;
  - the real tests (pytest, in a child) started with the TCP cursor inside a blocked run, and once outside one;
  - how often a random port in the dynamic range (and in 20000-40000) is free for both.

Writes a Markdown summary (stdout and $GITHUB_STEP_SUMMARY) and every reading to $DIAG_OUT/diag-ports.json.
"""
from __future__ import annotations

import importlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("DIAG_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
OUT = Path(os.environ.get("DIAG_OUT") or ROOT / "diag-out")
HOST = "127.0.0.1"
TCP, UDP = socket.SOCK_STREAM, socket.SOCK_DGRAM
NODES = ["tests/test_etw_flows.py::test_selftest_traffic_sends_one_udp_and_one_tcp_query_from_the_ports_it_reports",
         "tests/test_etw_tracker.py::test_the_selftest_marks_the_layout_verified_only_with_every_event_and_no_mismatch",
         "tests/test_loopback_dns.py"]
STANDINS = [("tests.test_etw_flows", "_FakeDns"), ("tests.test_etw_tracker", "_LoopbackDns"),
            ("tests.loopback_dns", "LoopbackDns")]
data: dict = {"python": sys.version.split()[0]}
md: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    md.append(text)


def code(exc: OSError) -> int:
    return getattr(exc, "winerror", None) or exc.errno or -1


def netsh(*args: str) -> str:
    return subprocess.run(["netsh", *args], capture_output=True, text=True, timeout=60).stdout


def excluded(proto: str) -> list[list]:
    rows = []
    for line in netsh("int", "ipv4", "show", "excludedportrange", f"protocol={proto}").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s*(\*?)\s*$", line)
        if m:
            rows.append([int(m[1]), int(m[2]), m[3] == "*"])
    return rows


def dynamic(proto: str) -> list[int]:
    nums = [int(x) for x in re.findall(r":\s*(\d+)", netsh("int", "ipv4", "show", "dynamicport", proto))]
    return nums[:2]


def ports_of(rows) -> set[int]:
    out: set[int] = set()
    for a, b, _ in rows:
        out.update(range(a, b + 1))
    return out


def next_port(kind: int) -> int:
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind((HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def bind_code(kind: int, port: int) -> int:
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind((HOST, port))
        return 0
    except OSError as exc:
        return code(exc)
    finally:
        s.close()


def step_hist(seq: list[int]) -> dict:
    hist: dict[int, int] = {}
    for a, b in zip(seq, seq[1:]):
        hist[b - a] = hist.get(b - a, 0) + 1
    return {str(k): v for k, v in sorted(hist.items(), key=lambda kv: -kv[1])[:8]}


def allocation() -> dict:
    tcp = [next_port(TCP) for _ in range(64)]
    udp = [next_port(UDP) for _ in range(64)]
    a = next_port(TCP)
    child = subprocess.run([sys.executable, "-c", "import socket; s = socket.socket(); s.bind(('127.0.0.1', 0)); "
                            "print(s.getsockname()[1])"], capture_output=True, text=True, timeout=60).stdout.strip()
    b = next_port(TCP)
    srv = socket.socket()
    srv.bind((HOST, 0))
    srv.listen(1)
    listen_port = srv.getsockname()[1]
    cli = socket.create_connection((HOST, listen_port), timeout=5)
    connect_port = cli.getsockname()[1]
    conn, _ = srv.accept()
    after = next_port(TCP)
    conn.close()
    cli.close()
    srv.close()
    return {"tcp_first": tcp[:16], "tcp_steps": step_hist(tcp), "udp_first": udp[:16], "udp_steps": step_hist(udp),
            "cross_process": [a, int(child) if child.isdigit() else child, b],
            "connect": {"before": b, "listener": listen_port, "connect": connect_port, "after": after}}


def sweep(first: int, other: int, count: int) -> list[list[int]]:
    """The stand-ins' step `count` times: the OS's next `first` port (held), then `other` on that number."""
    seq = []
    for _ in range(count):
        a = socket.socket(socket.AF_INET, first)
        try:
            a.bind((HOST, 0))
            p = a.getsockname()[1]
            seq.append([p, bind_code(other, p)])
        finally:
            a.close()
    return seq


def one_cycle(seq: list[list[int]], n: int) -> list[list[int]]:
    travelled = 0
    for i in range(1, len(seq)):
        travelled += (seq[i][0] - seq[i - 1][0]) % n
        if travelled >= n:
            return seq[:i]
    return seq


def runs_of(seq: list[list[int]]) -> list[dict]:
    out, cur = [], None
    for p, c in seq:
        if c:
            if cur is None:
                cur = {"first": p, "last": p, "len": 0, "codes": {}}
            cur["last"] = p
            cur["len"] += 1
            cur["codes"][str(c)] = cur["codes"].get(str(c), 0) + 1
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def all_fail_share(flags: list[bool], k: int) -> list[int]:
    """[start positions whose next k steps all fail, positions], the sequence taken as a cycle."""
    n = len(flags)
    return [sum(all(flags[(i + j) % n] for j in range(k)) for i in range(n)), n]


def predicted(lo: int, n: int, tcp_ex: set[int], udp_ex: set[int], k: int) -> list[int]:
    """From netsh alone: TCP's allocator walks the dynamic range minus TCP's exclusions; a start fails when its next
    k ports are all in UDP's exclusions."""
    order = [p for p in range(lo, lo + n) if p not in tcp_ex]
    return all_fail_share([p in udp_ex for p in order], k)


def walk_into(run: dict, limit: int = 50000) -> dict | None:
    """Bind and close TCP port-0 sockets until the OS hands out one of the run's first 6 ports."""
    lo, hi = run["first"], min(run["first"] + 5, run["last"])
    for k in range(1, limit + 1):
        p = next_port(TCP)
        if lo <= p <= hi:
            return {"steps": k, "at": p}
    return None


def standins() -> list[tuple[str, type]]:
    found = []
    for mod, name in STANDINS:
        try:
            found.append((f"{mod}.{name}", getattr(importlib.import_module(mod), name)))
        except (ImportError, AttributeError):
            continue
    return found


def try_standin(cls: type) -> dict:
    from lagdoctor.capture import netflows as nf
    t0 = time.perf_counter()
    try:
        dns = cls()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)[:400], "s": round(time.perf_counter() - t0, 4)}
    try:
        out = nf.selftest_traffic(HOST, dns.port, dns.port, timeout=3.0)
    finally:
        dns.close()
    return {"ok": True, "port": dns.port, "replies": [out["udp_reply"], out["tcp_reply"]], "errors": out["errors"],
            "s": round(time.perf_counter() - t0, 4)}


PLUGIN = r'''"""Loaded with -p diag_walk: walks the TCP port-0 hint into [DIAG_WALK_FIRST, +5] right before each test's call,
in the test's own process, so no hint jump can come between the walk and the stand-in's tries."""
import json
import os
import socket

import pytest

FIRST, LAST = int(os.environ["DIAG_WALK_FIRST"]), int(os.environ["DIAG_WALK_LAST"])
LOG = []


def _next():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    if FIRST < 0:
        return
    hi = min(FIRST + 5, LAST)
    for k in range(1, 60001):
        p = _next()
        if FIRST <= p <= hi:
            LOG.append([item.name, k, p])
            return
    LOG.append([item.name, None, None])


def pytest_sessionfinish(session):
    with open(os.environ["DIAG_WALK_LOG"], "w", encoding="utf-8") as f:
        json.dump(LOG, f)
'''


def pytest_at(tmp: str, first: int, last: int) -> dict:
    """The real tests in a child pytest, each started with the TCP hint walked to [first, first+5] (first < 0: not
    walked)."""
    plugdir = Path(tmp) / "plugin"
    plugdir.mkdir(exist_ok=True)
    (plugdir / "diag_walk.py").write_text(PLUGIN, encoding="utf-8")
    log = Path(tmp) / f"walk-{first}.json"
    env = dict(os.environ, TEMP=tmp, TMP=tmp, DIAG_WALK_FIRST=str(first), DIAG_WALK_LAST=str(last),
               DIAG_WALK_LOG=str(log), PYTHONPATH=os.pathsep.join(filter(None, [str(plugdir), os.environ.get("PYTHONPATH")])))
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider", "-p", "diag_walk",
                        *NODES], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    verdicts = re.findall(r"^(PASSED|FAILED|ERROR) (\S+)", r.stdout, re.M)
    reasons = sorted(set(re.findall(r"^E\s+(.*)$", r.stdout, re.M)))[:5]
    walks = json.loads(log.read_text(encoding="utf-8")) if log.exists() else None
    return {"exit": r.returncode, "verdicts": [[v, n.split("::")[-1]] for v, n in verdicts], "reasons": reasons,
            "walks": walks, "s": round(time.perf_counter() - t0, 2),
            "tail": r.stdout.strip().splitlines()[-1:] + r.stderr.strip().splitlines()[-3:]}


def hint_jumps(seconds: float) -> list:
    """Read the TCP port-0 hint every 0.25 s: (t, from, to, ports between them) for every move of more than 3."""
    last, out, t0 = next_port(TCP), [], time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        time.sleep(0.25)
        p = next_port(TCP)
        d = (p - last) % 16384 - 1
        if d > 3:
            out.append([round(time.perf_counter() - t0, 1), last, p, d])
        last = p
    return out


def random_pairs(lo: int, count: int, n: int, rng: random.Random) -> dict:
    ok, fails = 0, {}
    for _ in range(n):
        p = rng.randrange(lo, lo + count)
        u = socket.socket(socket.AF_INET, UDP)
        t = socket.socket(socket.AF_INET, TCP)
        try:
            try:
                u.bind((HOST, p))
            except OSError as exc:
                key = f"udp {code(exc)}"
                fails[key] = fails.get(key, 0) + 1
                continue
            try:
                t.bind((HOST, p))
            except OSError as exc:
                key = f"tcp {code(exc)}"
                fails[key] = fails.get(key, 0) + 1
                continue
            ok += 1
        finally:
            u.close()
            t.close()
    return {"range": [lo, lo + count - 1], "tries": n, "ok": ok, "fail": fails}


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100.0 * part / whole:.2f}%)" if whole else f"{part}/0"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t_all = time.perf_counter()
    say(f"## Stand-in ports on this runner (Python {data['python']})")
    say()

    dyn = {"tcp": dynamic("tcp"), "udp": dynamic("udp")}
    ex = {"tcp": excluded("tcp"), "udp": excluded("udp")}
    tcp_ex, udp_ex = ports_of(ex["tcp"]), ports_of(ex["udp"])
    data.update(dynamic=dyn, excluded=ex)
    lo, n = dyn["tcp"] if len(dyn["tcp"]) == 2 else (49152, 16384)
    udp_only = sorted(udp_ex - tcp_ex)
    in_dyn = [p for p in udp_only if lo <= p < lo + n]
    say(f"- dynamic ports: tcp {dyn['tcp']}, udp {dyn['udp']}")
    say(f"- excluded: tcp {len(ex['tcp'])} ranges, {len(tcp_ex)} ports; udp {len(ex['udp'])} ranges, {len(udp_ex)} ports")
    say(f"- closed to UDP but not to TCP, inside TCP's dynamic range: {len(in_dyn)} ports")
    say(f"- udp ranges: {', '.join(f'{a}-{b}' + ('*' if s else '') for a, b, s in ex['udp']) or 'none'}")
    say(f"- tcp ranges: {', '.join(f'{a}-{b}' + ('*' if s else '') for a, b, s in ex['tcp']) or 'none'}")

    t0 = time.perf_counter()
    alloc = allocation()
    data["allocation"] = alloc
    say(f"- port 0 steps: tcp {alloc['tcp_steps']}, udp {alloc['udp_steps']}; first tcp {alloc['tcp_first'][:6]}")
    say(f"- cross-process tcp cursor (this, child, this): {alloc['cross_process']}; "
        f"connect(): {alloc['connect']} ({time.perf_counter() - t0:.1f} s)")

    t0 = time.perf_counter()
    seq = sweep(TCP, UDP, int(n * 1.15) + 200)
    cyc = one_cycle(seq, n)
    data["sweep_tcp_then_udp"] = seq
    flags = [c != 0 for _, c in cyc]
    codes: dict[str, int] = {}
    in_udp_ex = 0
    for p, c in cyc:
        if c:
            codes[str(c)] = codes.get(str(c), 0) + 1
            in_udp_ex += p in udp_ex
    runs = runs_of(cyc)
    long_runs = sorted((r for r in runs if r["len"] >= 10), key=lambda r: -r["len"])
    shares = {k: all_fail_share(flags, k) for k in (10, 20, 40)}
    pred = {k: predicted(lo, n, tcp_ex, udp_ex, k) for k in (10, 20)}
    data.update(cycle_len=len(cyc), fail_codes=codes, runs=runs, shares=shares, predicted=pred)
    say()
    say(f"### TCP port 0, then UDP on that number, once around ({len(cyc)} positions, "
        f"{time.perf_counter() - t0:.1f} s)")
    say(f"- UDP refused {sum(flags)} of them, codes {codes}; {in_udp_ex} inside UDP's excluded ranges")
    say(f"- runs of 10 or more refusals in a row: {len(long_runs)}: "
        + ", ".join(f"{r['first']}-{r['last']} ({r['len']}, {r['codes']})" for r in long_runs[:12]))
    for k, (bad, tot) in shares.items():
        say(f"- start positions where {k} tries in a row all fail: {pct(bad, tot)}")
    for k, (bad, tot) in pred.items():
        say(f"- predicted from netsh alone, {k} in a row: {pct(bad, tot)}")

    t0 = time.perf_counter()
    rseq = sweep(UDP, TCP, int(n * 1.15) + 200)
    rcyc = one_cycle(rseq, n)
    rflags = [c != 0 for _, c in rcyc]
    rruns = [r for r in runs_of(rcyc) if r["len"] >= 10]
    data.update(sweep_udp_then_tcp_runs=runs_of(rcyc), udp_then_tcp_share10=all_fail_share(rflags, 10))
    say(f"- the other way round (UDP port 0, then TCP): refused {sum(rflags)} of {len(rcyc)}, runs of 10+: {len(rruns)}, "
        f"10 in a row: {pct(*all_fail_share(rflags, 10))} ({time.perf_counter() - t0:.1f} s)")

    found = standins()
    data["standins"] = [name for name, _ in found]
    say()
    say(f"### The real stand-ins: {', '.join(name for name, _ in found) or 'none found'}")
    t0 = time.perf_counter()
    around: dict[str, list] = {name: [] for name, _ in found}
    travelled, last = 0, next_port(TCP)
    while travelled < n:
        for _ in range(32):
            p = next_port(TCP)
            travelled += (p - last) % n
            last = p
        for name, cls in found:
            r = try_standin(cls)
            around[name].append([last, r["ok"]])
    data["around"] = around
    for name, res in around.items():
        bad = [p for p, ok in res if not ok]
        say(f"- {name}, started at every 32nd position once around: failed {pct(len(bad), len(res))}"
            + (f", at {bad[:8]}" if bad else ""))
    say(f"  ({time.perf_counter() - t0:.1f} s)")

    inside: list[dict] = []
    for run in [r for r in long_runs if r["first"] <= r["last"] and r["len"] >= 25][:4]:
        for name, cls in found:
            pos = walk_into(run)
            res = try_standin(cls) if pos else {"ok": None, "error": "could not walk into the run"}
            inside.append({"run": [run["first"], run["last"], run["len"]], "standin": name, "walk": pos, **res})
            say(f"- {name} started inside {run['first']}-{run['last']} (at {pos}): "
                + ("got a port" if res.get("ok") else f"failed: {res.get('error')}"))
    data["inside"] = inside

    tmp = tempfile.mkdtemp(prefix="diag-pytest-", dir=str(OUT))
    tests: list[dict] = []
    say()
    say("### The real tests (4 cases), each started with the TCP hint walked into a run from inside the test process")
    for run in [r for r in long_runs if r["first"] <= r["last"] and r["len"] >= 60][:3]:
        res = pytest_at(tmp, run["first"], run["last"])
        tests.append({"where": f"inside {run['first']}-{run['last']} ({run['len']})", **res})
    good = [r for r in runs_of([[p, 0 if c else 1] for p, c in cyc]) if r["first"] <= r["last"]]
    if good:
        best = max(good, key=lambda r: r["len"])
        res = pytest_at(tmp, best["first"], best["last"])
        tests.append({"where": f"control: inside free stretch {best['first']}-{best['last']} ({best['len']})", **res})
    res = pytest_at(tmp, -1, -1)
    tests.append({"where": "control: not walked", **res})
    for t in tests:
        verdicts = ", ".join(f"{v} {name}" for v, name in t["verdicts"]) or f"no verdicts {t['tail']}"
        say(f"- {t['where']}: exit {t['exit']}; {verdicts}; {t['reasons']}; walks {t['walks']} ({t.get('s')} s)")
    data["tests"] = tests

    t0 = time.perf_counter()
    jumps = hint_jumps(30.0)
    data["hint_jumps_30s"] = jumps
    say()
    say(f"### TCP hint moves of more than 3 ports in 30 s idle: {len(jumps)}: {jumps[:10]}")

    rng = random.Random()
    t0 = time.perf_counter()
    rand = [random_pairs(lo, n, 20000, rng), random_pairs(20000, 20001, 20000, rng)]
    data["random"] = rand
    say()
    say(f"### A random port, then UDP and TCP on it ({time.perf_counter() - t0:.1f} s)")
    for r in rand:
        say(f"- {r['range'][0]}-{r['range'][1]}: free for both {pct(r['ok'], r['tries'])}; refusals {r['fail']}")

    data["seconds"] = round(time.perf_counter() - t_all, 1)
    say()
    say(f"Total {data['seconds']} s")
    (OUT / "diag-ports.json").write_text(json.dumps(data), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
