"""A stand-in for `nvidia-smi --query-gpu=... --format=csv,nounits -lms N`, for the gpu collector's tests.

It prints the header and first row of a real capture (--fixture, smi_stream_591.86_default_limit.csv: the
collector's own field list) with live timestamps, a SW thermal counter that grows by --share of the elapsed
time and a SW power-cap counter that grows by --cap-share, CRLF-terminated like the real tool on a pipe. The
columns it rewrites are found by their header names. After --rows rows it idles until killed (as
nvidia-smi does), or exits with --exit. Like nvidia-smi, it keeps going when its stdout pipe is gone.
--heartbeat FILE appends a byte to FILE on every row, and the stand-in exits once FILE.stop exists.
Run it with the base interpreter (not the venv launcher), so terminating it ends the process that writes.
"""
import argparse
import datetime
import os
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--fixture", required=True)
p.add_argument("--rows", type=int, default=1000)
p.add_argument("--interval", type=float, default=0.25)
p.add_argument("--share", type=float, default=0.5)
p.add_argument("--cap-share", type=float, default=0.0)
p.add_argument("--gpus", type=int, default=1)
p.add_argument("--stderr-bytes", type=int, default=0)
p.add_argument("--extra-column", action="store_true")
p.add_argument("--junk", action="store_true", help="a junk line and a 10 kB line after the first row")
p.add_argument("--fail-text", default="", help="print this instead of CSV, then exit")
p.add_argument("--exit", type=int, default=None, help="exit with this code after the rows instead of idling")
p.add_argument("--heartbeat", default=None)
a = p.parse_args()

with open(a.fixture, encoding="utf-8") as f:
    lines = f.read().splitlines()
header, template = lines[0], lines[1].split(", ")
names = [part.split(" [")[0].strip() for part in header.split(",")]     # "power.draw [W]" -> "power.draw"
col = {name: names.index(name) for name in ("timestamp", "index", "pci.bus_id",
                                            "clocks_event_reasons_counters.sw_thermal_slowdown",
                                            "clocks_event_reasons_counters.sw_power_cap")}
out = sys.stdout.buffer


def emit(text: str) -> None:
    try:
        out.write(text.encode("ascii") + b"\r\n")
        out.flush()
    except OSError:
        pass                                            # a dead pipe: nvidia-smi goes on polling regardless


def beat() -> None:
    if a.heartbeat is None:
        return
    if os.path.exists(a.heartbeat + ".stop"):
        os._exit(0)                                     # no flush of a dead stdout on the way out
    with open(a.heartbeat, "ab") as f:
        f.write(b".")


if a.stderr_bytes:
    sys.stderr.buffer.write(b"e" * a.stderr_bytes)      # blocks here unless the reader drains stderr
    sys.stderr.buffer.flush()
if a.fail_text:
    emit(a.fail_text)
    sys.exit(9 if a.exit is None else a.exit)
emit(header + (", extra.field [x]" if a.extra_column else ""))
THERMAL, CAP = col["clocks_event_reasons_counters.sw_thermal_slowdown"], col["clocks_event_reasons_counters.sw_power_cap"]
sw_thermal_us, sw_power_cap_us = int(template[THERMAL]), int(template[CAP])
start = time.perf_counter()
for n in range(a.rows):
    now = datetime.datetime.now()
    elapsed_us = (time.perf_counter() - start) * 1e6
    for g in range(a.gpus):
        row = list(template)
        row[col["timestamp"]] = now.strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]
        row[col["index"]] = str(g)
        row[col["pci.bus_id"]] = f"00000000:{g + 1:02X}:00.0"
        row[THERMAL] = str(sw_thermal_us + int(elapsed_us * a.share))
        row[CAP] = str(sw_power_cap_us + int(elapsed_us * a.cap_share))
        emit(", ".join(row))
    if a.junk and n == 0:
        emit("this is not a row")
        emit("x" * 10_000)
    beat()
    time.sleep(a.interval)
if a.exit is not None:
    sys.exit(a.exit)
for _ in range(480):                                    # idle up to 120 s, still answering the stop file
    beat()
    time.sleep(0.25)
