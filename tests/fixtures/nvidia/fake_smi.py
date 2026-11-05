"""A stand-in for `nvidia-smi --query-gpu=... --format=csv,nounits -lms N`, for the gpu collector's tests.

It prints the header and first row of a real capture (smi_stream_591.86.csv) with live timestamps, and a SW
thermal counter that grows by --share of the elapsed time, CRLF-terminated like the real tool on a pipe.
After --rows rows it idles until killed (as nvidia-smi does), or exits with --exit.
Run it with the base interpreter (not the venv launcher), so terminating it ends the process that writes.
"""
import argparse
import datetime
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--fixture", required=True)
p.add_argument("--rows", type=int, default=1000)
p.add_argument("--interval", type=float, default=0.25)
p.add_argument("--share", type=float, default=0.5)
p.add_argument("--gpus", type=int, default=1)
p.add_argument("--stderr-bytes", type=int, default=0)
p.add_argument("--extra-column", action="store_true")
p.add_argument("--junk", action="store_true", help="a junk line and a 10 kB line after the first row")
p.add_argument("--fail-text", default="", help="print this instead of CSV, then exit")
p.add_argument("--exit", type=int, default=None, help="exit with this code after the rows instead of idling")
a = p.parse_args()

with open(a.fixture, encoding="utf-8") as f:
    lines = f.read().splitlines()
header, template = lines[0], lines[1].split(", ")
out = sys.stdout.buffer


def emit(text: str) -> None:
    out.write(text.encode("ascii") + b"\r\n")
    out.flush()


if a.stderr_bytes:
    sys.stderr.buffer.write(b"e" * a.stderr_bytes)      # blocks here unless the reader drains stderr
    sys.stderr.buffer.flush()
if a.fail_text:
    emit(a.fail_text)
    sys.exit(9 if a.exit is None else a.exit)
emit(header + (", extra.field [x]" if a.extra_column else ""))
sw_thermal_us = int(template[14])
start = time.perf_counter()
for n in range(a.rows):
    now = datetime.datetime.now()
    elapsed_us = (time.perf_counter() - start) * 1e6
    for g in range(a.gpus):
        row = list(template)
        row[0] = now.strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]
        row[1] = str(g)
        row[2] = f"00000000:{g + 1:02X}:00.0"
        row[14] = str(sw_thermal_us + int(elapsed_us * a.share))
        emit(", ".join(row))
    if a.junk and n == 0:
        emit("this is not a row")
        emit("x" * 10_000)
    time.sleep(a.interval)
if a.exit is not None:
    sys.exit(a.exit)
time.sleep(120)
