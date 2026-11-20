"""Made-up Windows programs for the tests of lagdoctor/stores/exe.py and of Add a game: a PE file holding no code,
only resources (a version resource and a group icon with its images), laid out as winnt.h and Microsoft's
VS_VERSIONINFO describe them. tests/test_stores_exe.py has Windows read the same files (LoadLibraryExW as a data file,
FindResourceW, VerQueryValueW), so a mistake here shows there as a disagreement, not as a reader that agrees with it.

    program(version={"ProductName": "Moss Garden"}, icons=[icon_bitmap(32, 32)])
"""
from __future__ import annotations

import struct
import zlib

SECTION_RVA = 0x1000
FILE_ALIGN = 0x200
SECTION_ALIGN = 0x1000
LANG = 0x0409                         # the language every resource here is listed under
RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16


def _pad(data: bytes, to: int) -> bytes:
    return data + b"\0" * (-len(data) % to)


# ---------------------------------------------------------------- version resource ---

def _node(key: str, value: bytes = b"", kind: int = 0, children: list[bytes] = (), value_len: int | None = None) \
        -> bytes:
    """One block: wLength, wValueLength, wType, the key (UTF-16, ended), padding to 4, the value, padding, then the
    blocks under it, each padded to 4."""
    head = key.encode("utf-16-le") + b"\0\0"
    body = _pad(struct.pack("<HHH", 0, 0, 0) + head, 4)[6:] + value
    kids = b"".join(_pad(c, 4) for c in children)
    if kids:
        body = _pad(struct.pack("<HHH", 0, 0, 0) + body, 4)[6:]
    if value_len is None:
        value_len = len(value) // 2 if kind == 1 else len(value)
    return struct.pack("<HHH", 6 + len(body) + len(kids), value_len, kind) + body + kids


def _string(key: str, text: str) -> bytes:
    return _node(key, text.encode("utf-16-le") + b"\0\0", kind=1)


def version(strings: dict[str, str] | None = None, tables: list[tuple[str, dict[str, str]]] | None = None,
            translation: tuple[int, int] = (LANG, 1200)) -> bytes:
    """A version resource: VS_VERSION_INFO with its fixed part, a StringFileInfo of `tables` ((key, strings) each;
    `strings` alone: one table, 040904b0) and a VarFileInfo naming `translation`."""
    if tables is None:
        tables = [("040904b0", strings or {})]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, 0x00010000, 0, 0x00010000, 0, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    sfi = _node("StringFileInfo", kind=1, children=[
        _node(key, kind=1, children=[_string(k, v) for k, v in table.items()]) for key, table in tables])
    vfi = _node("VarFileInfo", kind=1, children=[_node("Translation", struct.pack("<HH", *translation))])
    return _node("VS_VERSION_INFO", fixed, children=[sfi, vfi])


# ---------------------------------------------------------------- icons ---

def icon_bitmap(w: int, h: int, bits: int = 32, colour: tuple[int, int, int, int] = (40, 120, 200, 255)) -> bytes:
    """An icon image as a program holds one: a BITMAPINFOHEADER of twice the height, the pixels bottom up, then the
    mask. Only 32-bit images carry pixels of `colour` (blue, green, red, alpha); other depths are zeros."""
    stride = (w * bits + 31) // 32 * 4
    if bits == 32:
        row = bytes(colour) * w
        pixels = row * h
    else:
        pixels = b"\0" * (stride * h)
    mask = b"\0" * ((w + 31) // 32 * 4 * h)
    palette = b"\0" * (4 * (1 << bits)) if bits <= 8 else b""
    header = struct.pack("<IiiHHIIiiII", 40, w, 2 * h, 1, bits, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    return header + palette + pixels + mask


def png(w: int, h: int, rgb: tuple[int, int, int] = (200, 80, 40)) -> bytes:
    """A PNG of one colour (an icon image as Windows Vista's 256 x 256 ones are kept)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def group(entries: list[tuple[int, int, int, int, int]]) -> bytes:
    """A group icon: GRPICONDIR (0, 1, count) and one GRPICONDIRENTRY per (width, height, bits, bytes, id); a width or
    height of 256 is written 0, as Windows writes it."""
    out = struct.pack("<HHH", 0, 1, len(entries))
    for w, h, bits, size, nid in entries:
        out += struct.pack("<BBBBHHIH", w % 256, h % 256, 0, 0, 1, bits, size, nid)
    return out


def icon_resources(images: list[bytes], first_id: int = 1) -> dict[int, dict[int, bytes]]:
    """The RT_ICON and RT_GROUP_ICON resources of one icon of these images, sized as each image's header says."""
    entries, icons = [], {}
    for i, image in enumerate(images):
        nid = first_id + i
        if image.startswith(b"\x89PNG"):
            w, h, bits = int.from_bytes(image[16:20], "big"), int.from_bytes(image[20:24], "big"), 32
        else:
            w, h, bits = struct.unpack_from("<i", image, 4)[0], struct.unpack_from("<i", image, 8)[0] // 2, \
                struct.unpack_from("<H", image, 14)[0]
        entries.append((w, h, bits, len(image), nid))
        icons[nid] = image
    return {RT_ICON: icons, RT_GROUP_ICON: {1: group(entries)}}


# ---------------------------------------------------------------- the resource directory and the file ---

def resources(tree: dict[int, dict[int, bytes]], rva: int = SECTION_RVA) -> bytes:
    """A resource section at `rva`: the type directory, one directory of ids per type, one of languages per id, the
    data entries, then the data. Types and ids are listed ascending, as linkers list them."""
    types = sorted(tree)
    names = [(t, sorted(tree[t])) for t in types]
    size = 16 + 8 * len(types)
    type_at = {}
    for t, ids in names:
        type_at[t] = size
        size += 16 + 8 * len(ids)
    lang_at = {}
    for t, ids in names:
        for i in ids:
            lang_at[(t, i)] = size
            size += 16 + 8
    entry_at = {}
    for t, ids in names:
        for i in ids:
            entry_at[(t, i)] = size
            size += 16
    data_at = {}
    for t, ids in names:
        for i in ids:
            size += -size % 8
            data_at[(t, i)] = size
            size += len(tree[t][i])
    out = bytearray(size)

    def directory(at: int, entries: list[tuple[int, int]]) -> None:
        struct.pack_into("<IIHHHH", out, at, 0, 0, 0, 0, 0, len(entries))
        for n, (key, offset) in enumerate(entries):
            struct.pack_into("<II", out, at + 16 + 8 * n, key, offset)

    directory(0, [(t, 0x80000000 | type_at[t]) for t in types])
    for t, ids in names:
        directory(type_at[t], [(i, 0x80000000 | lang_at[(t, i)]) for i in ids])
        for i in ids:
            directory(lang_at[(t, i)], [(LANG, entry_at[(t, i)])])
            struct.pack_into("<IIII", out, entry_at[(t, i)], rva + data_at[(t, i)], len(tree[t][i]), 0, 0)
            out[data_at[(t, i)]:data_at[(t, i)] + len(tree[t][i])] = tree[t][i]
    return bytes(out)


def pe(rsrc: bytes, *, bits: int = 64, rva: int = SECTION_RVA) -> bytes:
    """A program of one section, .rsrc at `rva`, holding `rsrc`: the DOS header, the PE signature, the file header,
    the optional header (PE32+ for `bits` 64, else PE32) with the resource directory's entry, the section table."""
    raw_size = len(_pad(rsrc, FILE_ALIGN))
    image = rva + max(len(_pad(rsrc, SECTION_ALIGN)), SECTION_ALIGN)
    optional = 240 if bits == 64 else 224
    dirs = [(0, 0)] * 16
    dirs[2] = (rva, len(rsrc))
    if bits == 64:
        opt = struct.pack("<HBBIIIIIQIIHHHHHHIIIIHHQQQQII", 0x20B, 14, 0, 0, raw_size, 0, 0, 0, 0x140000000,
                          SECTION_ALIGN, FILE_ALIGN, 6, 0, 0, 0, 6, 0, 0, image, FILE_ALIGN, 0, 2, 0x8160,
                          0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    else:
        opt = struct.pack("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", 0x10B, 14, 0, 0, raw_size, 0, 0, 0, 0, 0x400000,
                          SECTION_ALIGN, FILE_ALIGN, 6, 0, 0, 0, 6, 0, 0, image, FILE_ALIGN, 0, 2, 0x8140,
                          0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    opt += b"".join(struct.pack("<II", *d) for d in dirs)
    assert len(opt) == optional, len(opt)
    machine, flags = (0x8664, 0x0022) if bits == 64 else (0x014C, 0x0102)
    head = b"PE\0\0" + struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, optional, flags)
    section = struct.pack("<8sIIIIIIHHI", b".rsrc", len(rsrc), rva, raw_size, FILE_ALIGN, 0, 0, 0, 0, 0x40000040)
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    headers = _pad(bytes(dos) + head + opt + section, FILE_ALIGN)
    assert len(headers) == FILE_ALIGN
    return headers + _pad(rsrc, FILE_ALIGN)


def program(version_strings: dict[str, str] | None = None, icons: list[bytes] | None = None, *, bits: int = 64,
            version_resource: bytes | None = None, extra: dict[int, dict[int, bytes]] | None = None) -> bytes:
    """A made-up program: its version resource (of `version_strings`, or `version_resource` as given; none when both
    are None) and one icon of `icons` (none when None), plus any `extra` resources."""
    tree: dict[int, dict[int, bytes]] = {}
    if version_resource is None and version_strings is not None:
        version_resource = version(version_strings)
    if version_resource is not None:
        tree[RT_VERSION] = {1: version_resource}
    if icons:
        tree.update(icon_resources(icons))
    for t, items in (extra or {}).items():
        tree.setdefault(t, {}).update(items)
    return pe(resources(tree) if tree else b"", bits=bits)
