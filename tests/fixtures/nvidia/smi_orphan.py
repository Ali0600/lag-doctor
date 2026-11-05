"""Runs the gpu collector's nvidia-smi fallback over fake_smi.py, then dies at once by TerminateProcess on itself
(no finally, no atexit, no stop(): a hard kill), so a test can see whether the stand-in died with it.

    python -I smi_orphan.py REPO_ROOT HEARTBEAT_FILE OUT_FILE

Writes "<stand-in pid> <rows>" to OUT_FILE once two rows have streamed (or after 15 s), then exits with code 9.
Run it with the base interpreter: everything it imports is in the standard library and the repo.
"""
import ctypes
import sys
import time
from pathlib import Path

root, heartbeat, out_file = sys.argv[1:4]
sys.path.insert(0, root)

from lagdoctor import clock                  # noqa: E402 - after the path is set
from lagdoctor.capture import gpumon         # noqa: E402

here = Path(__file__).resolve().parent
gpumon.smi_argv = lambda: [sys.executable, "-I", str(here / "fake_smi.py"), "--fixture",
                           str(here / "smi_stream_591.86_default_limit.csv"), "--heartbeat", heartbeat]
gpumon.Nvml.open = classmethod(lambda cls: None)
s = gpumon.NvidiaSampler(None)
s.start(clock.now())
deadline = time.monotonic() + 15
while len(s.series) < 2 and time.monotonic() < deadline:
    time.sleep(0.05)
child = s._child
Path(out_file).write_text(f"{child.pid if child else -1} {len(s.series)}")
k32 = ctypes.WinDLL("kernel32")
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
k32.TerminateProcess(k32.GetCurrentProcess(), 9)
