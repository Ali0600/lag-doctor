"""Write lag-doctor's icon, lagdoctor/app/lag-doctor.ico: the orange square with a white L that the window spike
showed on the taskbar (docs/research/desktop-app.md, section 11), at every size Windows asks a shortcut or a window
for. Each size is drawn on its own pixel grid (no scaling), stored as a PNG inside the ICO (Windows Vista and later
read those). Standard library only, and byte for byte the same on every run: tests/test_app_assets.py rebuilds it and
compares it with the committed file.

    uv run python scripts/make_icon.py          # rewrite the committed icon
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "lagdoctor" / "app" / "lag-doctor.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
EDGE = (0xB4, 0x47, 0x08)          # the darker rim
FILL = (0xE8, 0x59, 0x0C)          # lag-doctor's orange
INK = (0xFF, 0xFF, 0xFF)           # the L


def pixels(size: int) -> bytes:
    """RGBA rows, top to bottom, of one size: the spike's own drawing (scripts/webview_spike.py, make_icon)."""
    stem = foot = max(2, size // 6)
    rim = max(1, size // 16)
    rows = bytearray()
    for y in range(size):
        rows.append(0)                                     # PNG filter type 0 (none) for each row
        for x in range(size):
            colour = EDGE if min(x, y, size - 1 - x, size - 1 - y) < rim else FILL
            in_l = ((size * 3 // 10 <= x < size * 3 // 10 + stem and size // 5 <= y < size * 4 // 5)
                    or (size * 3 // 10 <= x < size * 7 // 10 and size * 4 // 5 - foot <= y < size * 4 // 5))
            rows += bytes((*(INK if in_l else colour), 0xFF))
    return bytes(rows)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)        # 8-bit RGBA, no interlace
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(pixels(size), 9))
            + _chunk(b"IEND", b""))


def ico_bytes() -> bytes:
    """The whole .ico: ICONDIR, one ICONDIRENTRY per size, then each PNG."""
    images = [png(s) for s in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    offset = 6 + 16 * len(SIZES)
    for size, data in zip(SIZES, images):
        side = 0 if size >= 256 else size                  # 0 means 256 in an ICONDIRENTRY
        out += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    return bytes(out)


def main() -> int:
    ICON.parent.mkdir(parents=True, exist_ok=True)
    ICON.write_bytes(ico_bytes())
    print(f"wrote {ICON} ({ICON.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
