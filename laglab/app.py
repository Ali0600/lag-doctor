"""LagLab main program. See laglab/__init__.py for why it exists.

Run:  uv run python -m laglab --mode gpu --duration 30 --log laglab.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

MODES = {
    "smooth": "Light load, capped at --fps-cap. The control case: nothing should be wrong.",
    "gpu": "A heavy pixel shader, retuned continuously so each frame takes about --target-ms on the GPU.",
    "cpu": "About --target-ms of busy work on the main thread every frame (CPU-bound main thread).",
    "hitch": "Smooth, plus --hitch-ms of main-thread CPU work every --every seconds.",
    "hitch-io": "Smooth, plus a burst of uncached random disk reads every --every seconds.",
    "hitch-wait": "Smooth, plus a --hitch-ms sleep on the main thread every --every seconds.",
    "hog": "Moderate load while background processes burn every CPU core in bursts.",
    "pace": "Uneven frame pacing: frames alternate between --pace-short-ms and --pace-long-ms.",
}
HITCH_MODES = {"hitch", "hitch-io", "hitch-wait"}

# The GPU watchdog (TDR) resets the driver if one frame runs for about 2 s.
# Stay far below it: any frame over this halves the shader's work at once.
GPU_FRAME_LIMIT_S = 0.4
MAX_ITERATIONS = 400_000

VERT = """#version 330 core
in vec2 position;
out vec2 uv;
void main() {
    uv = position * 0.5 + 0.5;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

FRAG = """#version 330 core
in vec2 uv;
out vec4 color;
uniform float time;
uniform int iterations;
void main() {
    vec2 p = uv * 2.0 - 1.0;
    vec2 z = p;
    float acc = 0.0;
    for (int i = 0; i < iterations; i++) {
        z = vec2(z.x * z.x - z.y * z.y, 2.0 * z.x * z.y) + p * 0.7
            + 0.3 * vec2(sin(time + float(i) * 0.013), cos(time * 0.7));
        z = clamp(z, -4.0, 4.0);
        acc += exp(-dot(z, z));
    }
    float s = sin(time * 2.0 + uv.x * 6.2831);
    color = vec4(0.5 + 0.5 * sin(acc * 0.11 + s),
                 0.5 + 0.5 * sin(acc * 0.07 + time),
                 0.5 + 0.5 * cos(acc * 0.05), 1.0);
}
"""

QUAD = (-1.0, -1.0, 1.0, -1.0, 1.0, 1.0, -1.0, -1.0, 1.0, 1.0, -1.0, 1.0)


def _burn(seconds: float) -> None:
    """Busy-wait on this thread: pure CPU work, no sleeping, no I/O."""
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        x += 1


def _wait_until(deadline: float) -> None:
    """Sleep most of the way, then spin, so frame caps are accurate to ~0.1 ms."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.0015)


class UncachedReader:
    """Random 4 KiB reads that bypass the Windows file cache, so they really reach the disk."""

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x1
    _OPEN_EXISTING = 3
    _FILE_FLAG_NO_BUFFERING = 0x20000000
    _FILE_FLAG_RANDOM_ACCESS = 0x10000000
    _MEM_COMMIT_RESERVE = 0x3000
    _MEM_RELEASE = 0x8000
    _PAGE_READWRITE = 0x04
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    BLOCK = 4096

    def __init__(self, path: Path) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.VirtualAlloc.restype = wintypes.LPVOID
        k32.VirtualAlloc.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k32.VirtualFree.restype = wintypes.BOOL
        k32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, wintypes.LPVOID, wintypes.DWORD]
        k32.ReadFile.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._blocks = path.stat().st_size // self.BLOCK
        self._handle = k32.CreateFileW(str(path), self._GENERIC_READ, self._FILE_SHARE_READ, None,
                                       self._OPEN_EXISTING,
                                       self._FILE_FLAG_NO_BUFFERING | self._FILE_FLAG_RANDOM_ACCESS, None)
        if self._handle == self._INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        # Unbuffered reads need a sector-aligned buffer; VirtualAlloc returns page-aligned memory.
        self._buf = k32.VirtualAlloc(None, self.BLOCK, self._MEM_COMMIT_RESERVE, self._PAGE_READWRITE)
        if not self._buf:
            raise ctypes.WinError(ctypes.get_last_error())

    def read_random(self, count: int) -> int:
        got = wintypes.DWORD(0)
        total = 0
        for _ in range(count):
            offset = random.randrange(self._blocks) * self.BLOCK
            if not self._k32.SetFilePointerEx(self._handle, offset, None, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._k32.ReadFile(self._handle, self._buf, self.BLOCK, ctypes.byref(got), None):
                raise ctypes.WinError(ctypes.get_last_error())
            total += got.value
        return total

    def close(self) -> None:
        if self._buf:
            self._k32.VirtualFree(self._buf, 0, self._MEM_RELEASE)
            self._buf = None
        if self._handle and self._handle != self._INVALID_HANDLE:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def _make_io_file(path: Path, megabytes: int) -> None:
    chunk = 8 * 1024 * 1024
    with open(path, "wb") as f:
        for _ in range(max(1, megabytes * 1024 * 1024 // chunk)):
            f.write(os.urandom(chunk))
        f.flush()
        os.fsync(f.fileno())


def _own_image_path() -> str:
    """The exe this process really runs (a venv's python.exe is only a launcher)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    buf = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buf))
    if k32.QueryFullProcessImageNameW(k32.GetCurrentProcess(), 0, buf, ctypes.byref(size)):
        return buf.value
    return sys.executable


def _write_json_atomic(path: Path, data: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="laglab", description="A fake game that lags on purpose.")
    ap.add_argument("--mode", choices=sorted(MODES), default="smooth")
    ap.add_argument("--list-modes", action="store_true", help="describe every mode and exit")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds to run (default 30)")
    ap.add_argument("--warmup", type=float, default=3.0,
                    help="seconds of normal running before the first hitch or hog burst (default 3)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--vsync", action="store_true", help="turn VSync on (default off)")
    ap.add_argument("--fps-cap", type=float, default=120.0, help="frame cap for capped modes; 0 = none")
    ap.add_argument("--target-ms", type=float, default=25.0, help="per-frame work for gpu/cpu modes")
    ap.add_argument("--hitch-ms", type=float, default=150.0, help="hitch length for hitch/hitch-wait")
    ap.add_argument("--every", type=float, default=3.0, help="seconds between hitches")
    ap.add_argument("--io-reads", type=int, default=1500, help="random 4 KiB reads per hitch-io burst")
    ap.add_argument("--io-file-mb", type=int, default=512, help="size of the hitch-io scratch file")
    ap.add_argument("--hogs", type=int, default=0, help="background hog processes (default 2 per CPU)")
    ap.add_argument("--hog-on", type=float, default=6.0)
    ap.add_argument("--hog-off", type=float, default=6.0)
    ap.add_argument("--hog-game-ms", type=float, default=4.0, help="main-thread work per frame in hog mode")
    ap.add_argument("--pace-short-ms", type=float, default=4.0)
    ap.add_argument("--pace-long-ms", type=float, default=20.0)
    ap.add_argument("--pid-file", type=Path, help="write this process's PID here once frames are flowing")
    ap.add_argument("--log", type=Path, help="write the ground-truth log here as JSON")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list_modes:
        for name in sorted(MODES):
            print(f"{name:11} {MODES[name]}")
        return 0

    import pyglet  # imported late so --help and --list-modes work without a GPU
    from pyglet.gl import GL_TRIANGLES, gl_info
    from pyglet.graphics.shader import Shader, ShaderProgram

    log: dict = {
        "schema": "laglab.log/1",
        "mode": args.mode,
        "params": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "pid": os.getpid(),
        "exe": _own_image_path(),
        "clock": "time.perf_counter seconds (QueryPerformanceCounter)",
        "frames": [],
        "hitches": [],
        "hog_bursts": [],
        "hog_pids": [],
        "hog_survivors": [],
        "gpu_iterations": [],
    }

    reader = io_path = job = None
    hogs: list[subprocess.Popen] = []
    window = pyglet.window.Window(args.width, args.height, caption=f"LagLab - {args.mode}", vsync=args.vsync)
    try:
        program = ShaderProgram(Shader(VERT, "vertex"), Shader(FRAG, "fragment"))
        vlist = program.vertex_list(6, GL_TRIANGLES, position=("f", QUAD))
        log["gl_renderer"] = gl_info.get_renderer()
        log["gl_vendor"] = gl_info.get_vendor()

        if args.mode == "hitch-io":
            io_path = Path(tempfile.gettempdir()) / f"laglab-io-{os.getpid()}.bin"
            _make_io_file(io_path, args.io_file_mb)
            reader = UncachedReader(io_path)

        iterations = 32 if args.mode == "gpu" else 8
        budget = 1.0 / args.fps_cap if args.fps_cap > 0 and args.mode not in ("gpu", "cpu") else 0.0
        t_start = time.perf_counter()
        t_stop = t_start + args.duration
        log["t_start"] = t_start

        if args.mode == "hog":
            from laglab.jobs import KillOnCloseJob
            job = KillOnCloseJob()
            hog_t0 = t_start + args.warmup
            count = args.hogs or 2 * (os.cpu_count() or 4)
            hog_script = Path(__file__).with_name("hog.py")
            base_python = getattr(sys, "_base_executable", sys.executable)
            for _ in range(count):
                p = subprocess.Popen(
                    [base_python, "-I", str(hog_script), "--t0", repr(hog_t0), "--on", str(args.hog_on),
                     "--off", str(args.hog_off), "--duration", str(t_stop - hog_t0 + 1.0),
                     "--parent", str(os.getpid())],
                    creationflags=subprocess.CREATE_NO_WINDOW)
                job.add(p.pid)
                hogs.append(p)
            log["hog_pids"] = [p.pid for p in hogs]
            period = args.hog_on + args.hog_off
            k = 0
            while hog_t0 + k * period < t_stop:
                on0 = hog_t0 + k * period
                log["hog_bursts"].append([on0, min(on0 + args.hog_on, t_stop)])
                k += 1

        next_hitch = t_start + args.warmup
        pace_short = True
        calib_t0 = t_start
        calib_frames: list[float] = []
        last_frame = None
        frames = log["frames"]
        print(f"laglab: mode={args.mode} pid={os.getpid()} renderer={log['gl_renderer']}", flush=True)

        while not window.has_exit:
            frame_start = time.perf_counter()
            if frame_start >= t_stop:
                break
            window.dispatch_events()

            if args.mode == "cpu":
                _burn(args.target_ms / 1000)
            elif args.mode == "hog":
                _burn(args.hog_game_ms / 1000)
            if args.mode in HITCH_MODES and frame_start >= next_hitch:
                h0 = time.perf_counter()
                detail: dict = {}
                if args.mode == "hitch":
                    _burn(args.hitch_ms / 1000)
                elif args.mode == "hitch-wait":
                    time.sleep(args.hitch_ms / 1000)
                else:
                    detail["bytes"] = reader.read_random(args.io_reads)
                log["hitches"].append({"start": h0, "end": time.perf_counter(), "kind": args.mode, **detail})
                next_hitch += args.every

            program.use()
            program["time"] = float(frame_start - t_start)
            program["iterations"] = int(iterations)
            vlist.draw(GL_TRIANGLES)
            program.stop()
            window.flip()
            frames.append(frame_start)

            if args.pid_file and len(frames) == 1:
                _write_json_atomic(args.pid_file, {"pid": os.getpid(), "exe": log["exe"]})

            if args.mode == "gpu" and last_frame is not None:
                dt = frame_start - last_frame
                calib_frames.append(dt)
                if dt > GPU_FRAME_LIMIT_S:
                    iterations = max(1, iterations // 2)
                    calib_frames.clear()
                    calib_t0 = frame_start
                elif frame_start - calib_t0 >= 0.25 and calib_frames:
                    avg = sum(calib_frames) / len(calib_frames)
                    target = args.target_ms / 1000
                    if avg < target * 0.5:
                        iterations = min(MAX_ITERATIONS, int(iterations * 1.6) + 1)
                    elif avg < target * 0.9:
                        iterations = min(MAX_ITERATIONS, int(iterations * 1.1) + 1)
                    elif avg > target * 1.15:
                        iterations = max(1, int(iterations * 0.9))
                    log["gpu_iterations"].append([frame_start, iterations, avg])
                    calib_frames.clear()
                    calib_t0 = frame_start
            last_frame = frame_start

            if args.mode == "pace":
                _wait_until(frame_start + (args.pace_short_ms if pace_short else args.pace_long_ms) / 1000)
                pace_short = not pace_short
            elif budget:
                _wait_until(frame_start + budget)

        log["t_stop"] = time.perf_counter()
    finally:
        window.close()
        if job is not None:
            job.close()  # kills every hog still running
        for p in hogs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log["hog_survivors"].append(p.pid)
        if reader is not None:
            reader.close()
        if io_path is not None:
            io_path.unlink(missing_ok=True)
        if args.log:
            _write_json_atomic(args.log, log)

    n = len(log["frames"])
    secs = log.get("t_stop", t_start) - t_start
    print(f"laglab: {n} frames in {secs:.1f}s ({n / secs if secs else 0:.0f} fps avg), "
          f"{len(log['hitches'])} hitches, {len(log['hog_bursts'])} hog bursts, "
          f"survivors={log['hog_survivors']}", flush=True)
    return 1 if log["hog_survivors"] else 0
