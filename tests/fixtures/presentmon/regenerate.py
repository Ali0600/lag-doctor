"""Rebuild the PresentMon fixtures in this folder from PresentMon's own gold ETL captures.

    uv run python tests/fixtures/presentmon/regenerate.py [EXE] [GOLD_DIR]

Every file is real PresentMon 2.6.0 output: a gold capture replayed with --etl_file (no admin
rights needed), read the way lag-doctor reads it, then trimmed by the rule printed next to it.
The only live call is the unelevated access-denied probe, and it runs only when this token has
no ETW rights, so it can never start a real capture.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from lagdoctor.capture.presentmon import FLAGS

HERE = Path(__file__).resolve().parent
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else LOCAL / "lag-doctor/presentmon/2.6.0/PresentMon-2.6.0-x64.exe"
GOLD = Path(sys.argv[2]) if len(sys.argv) > 2 else LOCAL / "lag-doctor/testdata/presentmon-gold"

# lag-doctor's live flags minus the live-only ones (PID, session name, --stop_existing_session,
# --terminate_on_proc_exit): docs/research/presentmon.md section 10. Read from the code, so the fixtures are made
# with the flags lag-doctor really passes.
REPLAY = list(FLAGS)
CRLF = b"\r\n"


def replay(case: int, *flags: str, stdout=subprocess.PIPE) -> bytes:
    p = subprocess.run([str(EXE), "--etl_file", str(GOLD / f"test_case_{case}.etl"), *flags],
                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE, timeout=120)
    if p.returncode != 0 or p.stderr:
        raise SystemExit(f"replay of test_case_{case} {flags}: exit {p.returncode}, stderr {p.stderr!r}")
    return p.stdout


def pipe_lines(data: bytes) -> list[bytes]:
    assert data.endswith(CRLF) and not data.startswith((b"\xff\xfe", b"\xef\xbb\xbf")), data[:8]
    return data[:-2].split(CRLF)


def keep_first_lines(data: bytes, n: int, eol: bytes, step: int = 1) -> bytes:
    """The first n lines, cut at the n-th end of line (an even offset for UTF-16)."""
    pos = 0
    for _ in range(n):
        pos = data.index(eol, pos)
        while pos % step:
            pos = data.index(eol, pos + 1)
        pos += len(eol)
    return data[:pos]


def write(name: str, data: bytes, rule: str) -> None:
    (HERE / name).write_bytes(data)
    print(f"{name}: {len(data)} bytes; {rule}")


def main() -> None:
    # test_case_5, every row of PresentBench.exe (PID 24892, both of its chains) plus the first 3 rows
    # of every other chain, in the order PresentMon wrote them.
    lines = pipe_lines(replay(5, *REPLAY))
    seen: dict[tuple[bytes, bytes], int] = {}
    kept = [lines[0]]
    for ln in lines[1:]:
        f = ln.split(b",")
        key = (f[1], f[2])
        seen[key] = seen.get(key, 0) + 1
        if f[1] == b"24892" or seen[key] <= 3:
            kept.append(ln)
    write("tc5_pid24892.csv", CRLF.join(kept) + CRLF,
          "test_case_5: every PID 24892 row plus the first 3 rows of each other chain")

    # test_case_0, the first 3 rows of every chain plus every dropped frame (MsUntilDisplayed NA).
    lines = pipe_lines(replay(0, *REPLAY))
    col = lines[0].split(b",").index(b"MsUntilDisplayed")
    seen, kept = {}, [lines[0]]
    for ln in lines[1:]:
        f = ln.split(b",")
        key = (f[1], f[2])
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= 3 or f[col] == b"NA":
            kept.append(ln)
    write("tc0_head.csv", CRLF.join(kept) + CRLF,
          "test_case_0: first 3 rows of every chain plus every dropped frame")

    # The 2.6.0 header/row mismatch (17 header fields, 18 in every row), PM 1.4.
    data = replay(0, "--output_stdout", "--no_track_display", "--no_track_gpu", "--track_pc_latency")
    write("tc0_header_row_mismatch.csv", keep_first_lines(data, 5, CRLF),
          "test_case_0 with --no_track_display --no_track_gpu --track_pc_latency: header + 4 rows")

    # A different header (28 + FrameType columns), for the header-rebuild rule.
    data = replay(1, "--output_stdout", "--qpc_time", "--track_frame_type")
    write("tc1_qpc_frametype.csv", keep_first_lines(data, 4, CRLF),
          "test_case_1 with --qpc_time --track_frame_type: header + 3 rows")

    with tempfile.TemporaryDirectory() as tmp:
        # --output_file: UTF-8 with a BOM.
        out = Path(tmp) / "out.csv"
        replay(1, "--output_file", str(out), *[f for f in REPLAY if f != "--output_stdout"])
        data = out.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), data[:8]
        write("tc1_output_file_utf8.csv", keep_first_lines(data, 5, CRLF),
              "test_case_1 with --output_file: header + 4 rows, UTF-8 BOM kept")

        # stdout redirected to an empty disk file: UTF-16LE with a BOM.
        out = Path(tmp) / "stdout.csv"
        with open(out, "wb") as f:
            replay(1, *REPLAY, stdout=f)
        data = out.read_bytes()
        assert data.startswith(b"\xff\xfe"), data[:8]
        write("tc1_stdout_file_utf16.csv", keep_first_lines(data, 5, "\r\n".encode("utf-16-le"), step=2),
              "test_case_1 with stdout to an empty file: header + 4 rows, UTF-16LE BOM kept")

    # The real unelevated failure (PM 6.2), only when this token cannot trace.
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.CheckTokenMembership.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    def member(sid: str) -> bool:
        p, ok = ctypes.c_void_p(), ctypes.c_int()
        assert adv.ConvertStringSidToSidW(sid, ctypes.byref(p))
        try:
            assert adv.CheckTokenMembership(None, p, ctypes.byref(ok))
        finally:
            k32.LocalFree(p)
        return bool(ok.value)

    if member("S-1-5-32-544") or member("S-1-5-32-559"):
        print("access_denied.stderr: skipped (this token can trace; the probe would start a real capture)")
        return
    p = subprocess.run([str(EXE), "--process_id", str(os.getpid()), *REPLAY, "--session_name", "lagdoctor",
                        "--stop_existing_session", "--terminate_on_proc_exit"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    assert p.returncode == 6 and not p.stdout and b"access denied" in p.stderr, (p.returncode, p.stderr)
    write("access_denied.stderr", p.stderr, "live, unelevated: exit 6, stderr as written into a pipe")


if __name__ == "__main__":
    main()
