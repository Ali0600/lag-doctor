"""A stand-in for PresentMon in lag-doctor's tests: the same pipes, exit codes and stop behaviour, no ETW.

    python -I fake_presentmon.py MODE STATE_DIR [PresentMon arguments ...]

It writes the real replay rows of tc5_pid24892.csv (or the real access-denied stderr) and reacts to a
console CTRL_BREAK, through a native SetConsoleCtrlHandler handler as PresentMon does, and to a second
run with --terminate_existing_session (docs/research/presentmon.md 7.1). Files in STATE_DIR:
  running          exists while this fake "owns" a trace session; removed on a clean exit or by a terminate run
  ctrl             one line per console control event received (1 = CTRL_BREAK_EVENT)
  terminate_calls  one line per --terminate_existing_session run
  session_stopped  written by a terminate run that found a running session
Modes: eof (all rows, exit 0), breakable (stops on CTRL_BREAK or a stopped session), deaf (ignores
CTRL_BREAK, stops only with its session), hung (only a kill stops it), denied (real stderr, exit 6),
busy_once (first run: "already running" and exit 6), crash (some rows, then 0xC0000005), lossy (all rows,
then a lost-events warning), reheader (19 rows, then a header without TimeInQPC and 3 rows under it),
silent (like hung, but writes nothing at all), garbled (all rows, one of them cut short), covered (no CSV at all, then
the overflow warning PresentMon 2.6.0 printed for a game window covered from the start, e2e 2026-11-09, and exit 0).
Every mode gives up after 30 s so a broken test leaves nothing running.
"""
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
mode, state, args = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
out, err = sys.stdout.buffer, sys.stderr.buffer


def log(name, text):
    with open(state / name, "a", encoding="ascii") as f:
        f.write(text + "\n")


if "--terminate_existing_session" in args:
    log("terminate_calls", "x")
    err.write(b"warning: --terminate_existing_session exits without capturing anything; ignoring all other options.\r\n")
    if (state / "running").exists():
        (state / "running").unlink(missing_ok=True)
        (state / "session_stopped").write_text("x")
        sys.exit(0)
    err.write(b"error: no existing sessions found: lagdoctor\r\n")
    sys.exit(7)

if mode == "denied":
    err.write((HERE / "access_denied.stderr").read_bytes())
    sys.exit(6)
if mode == "busy_once" and not (state / "busy_done").exists():
    (state / "busy_done").write_text("x")
    err.write(b'warning: a trace session named "lagdoctor" is already running and it will be stopped.\r\n'
              b"         Use --session_name with a different name to start a new session.\r\n"
              b"error: failed to start trace session: error code 183.\r\n")
    sys.exit(6)

got = []
HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


def on_ctrl(kind):
    log("ctrl", str(kind))                   # before signalling the main thread, which may exit at once
    got.append(kind)
    return True                              # handled: the process is not ended by Windows


handler = HANDLER(on_ctrl)
ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(handler, True)
(state / "running").write_text("x")

lines = (HERE / "tc5_pid24892.csv").read_bytes().split(b"\r\n")[:-1]
if mode == "reheader":                       # a restarted recording whose header lacks TimeInQPC
    lines = lines[:20] + [lines[0].replace(b"TimeInQPC", b"TimeInMs")] + lines[20:23]
if mode == "garbled":                        # one PresentBench row cut short
    lines[2] = lines[2][:120]
if mode == "covered":                        # every frame held back behind a shown one that never comes (PM 2.4)
    lines = []
first = {"crash": 10, "silent": 0, "breakable": 41, "deaf": 41, "hung": 41, "busy_once": 41}.get(mode, len(lines))
for ln in lines[:first]:
    out.write(ln + b"\r\n")
    out.flush()                              # PresentMon flushes after every row on a pipe
if mode == "crash":
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.ExitProcess.argtypes = [wintypes.UINT]
    kernel32.ExitProcess(0xC0000005)         # as an access violation would end it: no clean-up

deadline = time.monotonic() + 30
while mode in ("breakable", "deaf", "hung", "silent", "busy_once"):
    if time.monotonic() > deadline:
        sys.exit(3)
    stopped = (state / "session_stopped").exists()
    if mode in ("breakable", "busy_once") and (got or stopped):
        break
    if mode == "deaf" and stopped:
        break
    time.sleep(0.02)

for ln in lines[first:]:                     # what PresentMon writes while it shuts down
    out.write(ln + b"\r\n")
    out.flush()
if mode == "lossy":
    err.write(b"warning: 12 ETW events were lost.\r\n")
if mode == "covered":
    err.write(b"warning: 236 overflowed present events detected. This could be due to a high-fps application.\r\n")
(state / "running").unlink(missing_ok=True)
sys.exit(0)
