"""The library's cover art (docs/LAUNCHER-PLAN.md, "Library, art and demo data"): read from a store's own cache on the
PC, never downloaded.

Steam keeps the art its library shows under its folder's appcache\\librarycache. Since late 2026 that is a folder per
app id holding library_600x900.jpg (the portrait cover), header.jpg, library_hero.jpg, logo.png and an icon, with
content-hash subfolders for some apps; before, flat <appid>_library_600x900.jpg files (research note desktop-app.md,
section 6). The layout is undocumented, so the lookup takes the portrait by its name in the app's folder, then in each
of its subfolders, then by the old flat name, and tells the image's type from its bytes (some .jpg files hold PNG
data). Epic keeps no image of its own on the PC (its catalog cache lists only https links), so an Epic game has no art
here, and the app draws a generated tile for it.

These folders are as untrusted as the stores' other files: every read goes through stores' file access (which asks
target.local_problem first: nothing on a share, nothing kept online only), each image is capped at MAX_IMAGE_BYTES and
all of one listing's at MAX_TOTAL_BYTES, and only bytes that begin as a JPEG, PNG or WebP image whose header declares
a size within MAX_SIDE and MAX_PIXELS are taken.
"""
from __future__ import annotations

import os
from typing import Any

from lagdoctor import stores
from lagdoctor.stores import launch

LIBRARY_CACHE = ("appcache", "librarycache")
PORTRAIT = "library_600x900.jpg"
MAX_IMAGE_BYTES = 4 << 20          # one 600x900 cover is a few hundred kilobytes
MAX_TOTAL_BYTES = 96 << 20         # every cover of one listing together, kept in the app's memory
MAX_SUBFOLDERS = 16                # content-hash subfolders looked into, in one app's folder
MAX_ENTRIES = 256                  # entries read from one app's folder
# What a cover's header may declare: the page decodes all of it to draw a tile, and a small file can declare a huge
# image (a PNG of zeros deflates about a thousand to one). Steam's portrait is 600 x 900; twice that is 2.2 million.
MAX_SIDE = 4096
MAX_PIXELS = 4_000_000
JPEG, PNG, WEBP = "image/jpeg", "image/png", "image/webp"


def image_type(data: bytes) -> str | None:
    """An image's type from its first bytes (JPEG, PNG or WebP), or None for anything else."""
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """A JPEG's (width, height) from its first frame header (SOF0 to SOF15, which DHT, JPG and DAC are not), walking
    the segments from the start; None when the scan or the end comes first, or a segment does not fit."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                       # a fill byte before the marker
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:     # markers with no length
            i += 2
            continue
        if marker in (0xD9, 0xDA):               # the end, or the scan, before any frame header
            return None
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > len(data):
                return None
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + length
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """The (width, height) an image's header declares, or None when it cannot be read: a PNG's IHDR, a JPEG's first
    frame header, a WebP's extended canvas (VP8X) or its lossless or lossy frame (VP8L, VP8)."""
    kind = image_type(data)
    if kind == PNG:
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if kind == JPEG:
        return _jpeg_size(data)
    if kind == WEBP:
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == bytes([0x9D, 0x01, 0x2A]):
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    return None


def _image(path: str) -> tuple[str, bytes] | None:
    try:
        data = stores.read_bytes(path, MAX_IMAGE_BYTES)
    except ValueError:                           # not there, too large, off the local disks, or unreadable
        return None
    kind = image_type(data)
    size = image_size(data) if kind is not None else None
    if size is None or not (0 < size[0] <= MAX_SIDE and 0 < size[1] <= MAX_SIDE and size[0] * size[1] <= MAX_PIXELS):
        return None                              # no image, one whose size cannot be read, or one too large to draw
    return kind, data


def _subfolders(app: str) -> list[str]:
    """The app folder's subfolders (content-hash names), sorted, at most MAX_SUBFOLDERS; none when it cannot be read."""
    names = []
    try:
        with stores.scandir(app) as listing:
            for i, entry in enumerate(listing):
                if i >= MAX_ENTRIES:
                    break
                names.append(entry.name)
    except OSError:
        return []
    out = []
    for name in sorted(names):
        try:
            if stores.is_folder(os.path.join(app, name)):
                out.append(name)
        except OSError:
            continue
        if len(out) >= MAX_SUBFOLDERS:
            break
    return out


def steam_cover(steam_folder: str, appid: object) -> tuple[str, bytes] | None:
    """The portrait cover Steam keeps for the game `appid` (its type and bytes), or None when it keeps none that can be
    read: in the app's folder, then in its subfolders, then by the old flat name."""
    if not isinstance(appid, str) or not launch.STEAM_APPID.fullmatch(appid):    # it names a folder: digits only
        return None
    cache = os.path.join(steam_folder, *LIBRARY_CACHE)
    app = os.path.join(cache, appid)
    for path in [os.path.join(app, PORTRAIT)] + [os.path.join(app, s, PORTRAIT) for s in _subfolders(app)]:
        found = _image(path)
        if found is not None:
            return found
    return _image(os.path.join(cache, f"{appid}_{PORTRAIT}"))


def covers(games: list[Any], steam_folder: str | None = None) -> dict[tuple[str, str], tuple[str, bytes]]:
    """{(store, id): (type, bytes)}: the cover each game's store keeps on the PC, for the games that have one, up to
    MAX_TOTAL_BYTES in all (a cover past that is left out, and the game keeps its generated tile). Steam's folder is
    find_steam_folder's when None; without one, no Steam game has art."""
    from lagdoctor.stores import steam
    out: dict[tuple[str, str], tuple[str, bytes]] = {}
    if not any(g.store == steam.STORE for g in games):
        return out
    folder = steam_folder if steam_folder is not None else steam.find_steam_folder()
    if folder is None:
        return out
    total = 0
    for g in games:
        if g.store != steam.STORE or (g.store, g.id) in out:
            continue
        found = steam_cover(folder, g.id)
        if found is None:
            continue
        if total + len(found[1]) > MAX_TOTAL_BYTES:
            break
        total += len(found[1])
        out[(g.store, g.id)] = found
    return out
