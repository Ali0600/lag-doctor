"""A program the player picks for the Library (screen 6, "Add a game"): its name and its icon, read from the program's
own file, never by running it or having Windows load it.

A Windows program (a PE file) keeps both among its resources: the version resource (RT_VERSION) names the product and
describes the file, and its first group icon (RT_GROUP_ICON, the icon Explorer shows for it) lists the icon's sizes,
each an RT_ICON of its own. The structures and their numbers are the Windows SDK's (winnt.h: the headers, the
sections and the resource directory; winuser.h: the resource types; the version block as Microsoft documents
VS_VERSIONINFO), and tests/test_stores_exe.py checks what is read here against Windows' own reading of the same files.

A program is as untrusted as any file the player downloaded, and a game's can be gigabytes: it is read through stores'
file access (target.local_problem first: nothing on a share, nothing kept online only), in parts, within READ_BUDGET
bytes in all, with every count, offset and size checked against the file and against its caps before it is followed,
and a fixed depth of directories (no offset is followed twice). What cannot be read is named in Program.problems, and
the program still adds: named by its file, with the generated tile.

The icon reaches the page as an icon file holding one image (image/x-icon), the largest its group lists, taken only
when that image's own header (a PNG's, or a bitmap's) declares at most 256 by 256, the most an icon holds. The page's
engine decodes it, as it does a store's cover.
"""
from __future__ import annotations

import ntpath
import struct
from dataclasses import dataclass
from typing import Any

from lagdoctor import stores
from lagdoctor.stores import art

ICO = "image/x-icon"
# winnt.h (SDK 10.0.26100.0)
MZ = b"MZ"                                  # IMAGE_DOS_SIGNATURE
PE = b"PE\0\0"                              # IMAGE_NT_SIGNATURE
PE32, PE32_PLUS = 0x10B, 0x20B              # IMAGE_NT_OPTIONAL_HDR32_MAGIC, IMAGE_NT_OPTIONAL_HDR64_MAGIC
RESOURCE_DIRECTORY = 2                      # IMAGE_DIRECTORY_ENTRY_RESOURCE
SECTION_SIZE = 40                           # IMAGE_SIZEOF_SECTION_HEADER
HIGH_BIT = 0x80000000                       # IMAGE_RESOURCE_NAME_IS_STRING, IMAGE_RESOURCE_DATA_IS_DIRECTORY
# winuser.h
RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16

READ_BUDGET = 4 << 20             # bytes read from one program in all
MAX_SECTIONS = 96                 # the most Windows' loader takes
MAX_ENTRIES = 1024                # entries read from one resource directory
MAX_VERSION_BYTES = 64 << 10      # a version resource is a kilobyte or two
MAX_VERSION_NODES = 512           # blocks of it walked
MAX_GROUP_ENTRIES = 256           # sizes one group icon lists
MAX_ICON_BYTES = 1 << 20          # one icon image: 256 x 256 in 32 bits is 262,144 bytes and its mask
MAX_ICON_SIDE = 256               # the most an icon image holds (an ICONDIRENTRY's 0)
BITMAP_HEADERS = (40, 108, 124)   # BITMAPINFOHEADER, BITMAPV4HEADER, BITMAPV5HEADER
BITMAP_BITS = (1, 4, 8, 16, 24, 32)
BITMAP_RGB, BITMAP_BITFIELDS = 0, 3
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Program:
    """What a program's file says of it. `name`: its version resource's product name, else its file description, each
    cleaned for display (stores.name); None when it has neither. `icon`: (ICO, bytes) of its icon, or None. `problems`:
    what could not be read, in plain words."""
    name: str | None
    icon: tuple[str, bytes] | None
    problems: tuple[str, ...] = ()


def file_name(path: str) -> str:
    """The program's file name, as the page shows it ("Moss Garden.exe")."""
    return ntpath.basename(path)


def stem(path: str) -> str:
    """The program's file name without .exe: the name a program with none in its file is shown by."""
    name = file_name(path)
    return name[:-4] if name.casefold().endswith(".exe") else name


def _u16(b: bytes, i: int) -> int:
    return int.from_bytes(b[i:i + 2], "little")


def _u32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i:i + 4], "little")


def _i32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i:i + 4], "little", signed=True)


@dataclass(frozen=True)
class _Image:
    """The parts of a program's headers the resources are reached through: its sections (virtual address, virtual
    size, size and place of the data in the file), and its resource directory's address and size (0, 0: none)."""
    path: str
    sections: tuple[tuple[int, int, int, int], ...]
    rsrc: int
    rsrc_size: int


def _not_a_program(path: str) -> ValueError:
    return ValueError(f"{path} is not a Windows program that can be read")


def _image(f: Any) -> _Image:
    """The headers: the DOS header's pointer, the PE signature, the file header's counts, the optional header's
    resource directory and the section table, each checked before it is followed."""
    if f.size < 64:
        raise _not_a_program(f.path)
    dos = f.at(0, 64)
    if dos[:2] != MZ:
        raise _not_a_program(f.path)
    at = _u32(dos, 0x3C)                                    # e_lfanew: past the file, f.at says so
    head = f.at(at, 24)                                     # the signature and IMAGE_FILE_HEADER
    sections, optional = _u16(head, 6), _u16(head, 20)      # NumberOfSections, SizeOfOptionalHeader
    if head[:4] != PE or not 1 <= sections <= MAX_SECTIONS:
        raise _not_a_program(f.path)
    opt = f.at(at + 24, optional)
    magic = _u16(opt, 0)
    if magic == PE32:
        count_at, dirs_at = 92, 96                          # NumberOfRvaAndSizes, DataDirectory
    elif magic == PE32_PLUS:
        count_at, dirs_at = 108, 112
    else:
        raise _not_a_program(f.path)
    entry = dirs_at + 8 * RESOURCE_DIRECTORY
    rsrc = rsrc_size = 0
    if optional >= entry + 8 and optional >= count_at + 4 and _u32(opt, count_at) > RESOURCE_DIRECTORY:
        rsrc, rsrc_size = _u32(opt, entry), _u32(opt, entry + 4)
    table = f.at(at + 24 + optional, SECTION_SIZE * sections)
    out = []
    for i in range(sections):
        s = table[SECTION_SIZE * i:SECTION_SIZE * (i + 1)]
        out.append((_u32(s, 12), _u32(s, 8), _u32(s, 16), _u32(s, 20)))   # VirtualAddress, VirtualSize, raw size, place
    return _Image(path=f.path, sections=tuple(out), rsrc=rsrc, rsrc_size=rsrc_size)


def _at_rva(f: Any, img: _Image, rva: int, n: int) -> bytes:
    """n bytes at the virtual address `rva`, from the file's data of the section holding them all."""
    for va, vsize, raw_size, raw_at in img.sections:
        if va <= rva < va + max(vsize, raw_size):
            if rva + n > va + raw_size:                     # past what the file holds of the section
                break
            return f.at(raw_at + (rva - va), n)
    raise ValueError(f"{img.path} points to a part of itself that is not in the file")


def _rsrc(f: Any, img: _Image, offset: int, n: int) -> bytes:
    """n bytes `offset` bytes into the resource directory, which must hold them."""
    if offset < 0 or offset + n > img.rsrc_size:
        raise ValueError(f"{img.path} points past its own resources")
    return _at_rva(f, img, img.rsrc + offset, n)


def _directory(f: Any, img: _Image, offset: int) -> list[tuple[int | None, int, bool]]:
    """One resource directory's entries, in the file's order (named ones first, then ids ascending): (the id, None
    for a named entry; the offset it points to; whether that is another directory). At most MAX_ENTRIES."""
    head = _rsrc(f, img, offset, 16)
    count = min(_u16(head, 12) + _u16(head, 14), MAX_ENTRIES)
    raw = _rsrc(f, img, offset + 16, 8 * count)
    out = []
    for i in range(count):
        name, data = _u32(raw, 8 * i), _u32(raw, 8 * i + 4)
        out.append((None if name & HIGH_BIT else name & 0xFFFF, data & ~HIGH_BIT, bool(data & HIGH_BIT)))
    return out


def _of_type(f: Any, img: _Image, kind: int) -> list[tuple[int | None, int]]:
    """The resources of one type, in the file's order: (their id, None when named; their directory's offset)."""
    if not img.rsrc_size:
        return []
    for key, offset, is_dir in _directory(f, img, 0):
        if key == kind and is_dir:
            return [(k, o) for k, o, d in _directory(f, img, offset) if d]
    return []


def _data(f: Any, img: _Image, offset: int, cap: int) -> bytes:
    """A resource's bytes, from its directory of languages (the first one listed): at most `cap` of them."""
    for _key, leaf, is_dir in _directory(f, img, offset):
        if is_dir:
            continue
        entry = _rsrc(f, img, leaf, 16)                     # IMAGE_RESOURCE_DATA_ENTRY
        rva, size = _u32(entry, 0), _u32(entry, 4)
        if size > cap:
            raise ValueError(f"{img.path} holds a resource larger than {cap} bytes, so it was not read")
        return _at_rva(f, img, rva, size)
    raise ValueError(f"{img.path} lists a resource with no data")


# ---------------------------------------------------------------- the name ---

def _align(i: int) -> int:
    return (i + 3) & ~3


def _blocks(data: bytes, start: int, end: int, budget: list[int]) -> list[tuple[str, bytes, int, int]]:
    """The version blocks between `start` and `end` (each a length, a value's length, a type, a key, the value and
    blocks of its own): (key, value, where its own blocks start, where it ends). Every length must fit inside the
    block holding it; `budget` counts the blocks walked."""
    out = []
    at = start
    while at + 6 <= end:
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("too many blocks")
        length, value_len, kind = _u16(data, at), _u16(data, at + 2), _u16(data, at + 4)
        if length == 0:                                     # padding after the last block
            break
        if length < 6 or at + length > end:
            raise ValueError("a block that does not fit")
        stop = at + length
        k = at + 6
        while k + 2 <= stop and data[k:k + 2] != b"\0\0":
            k += 2
        key = data[at + 6:k].decode("utf-16-le", errors="replace")      # with no end, the rest of the block
        v = _align(k + 2)
        n = value_len * 2 if kind == 1 else value_len      # a text value's length counts characters
        value = data[v:min(v + n, stop)] if v < stop else b""
        out.append((key, value, min(_align(v + n), stop), stop))
        at = _align(stop)
    return out


def _text(value: bytes) -> str:
    return value[:len(value) // 2 * 2].decode("utf-16-le", errors="replace").split("\0")[0]


def version_name(data: bytes) -> str | None:
    """The product name a version resource gives (VS_VERSIONINFO, its StringFileInfo's string tables in the file's
    order), else its file description, cleaned (stores.name); None when it gives neither. ValueError when the
    resource is not one."""
    budget = [MAX_VERSION_NODES]
    roots = _blocks(data, 0, len(data), budget)
    if not roots or roots[0][0] != "VS_VERSION_INFO":
        raise ValueError("no version information")
    _key, _value, inner, stop = roots[0]
    described = None
    for key, _v, tables_at, tables_end in _blocks(data, inner, stop, budget):
        if key != "StringFileInfo":
            continue
        for _lang, _v2, strings_at, strings_end in _blocks(data, tables_at, tables_end, budget):
            strings = {k: _text(v) for k, v, _a, _e in _blocks(data, strings_at, strings_end, budget)}
            product = stores.name(strings.get("ProductName"))
            if product:
                return product
            described = described or stores.name(strings.get("FileDescription"))
    return described


# ---------------------------------------------------------------- the icon ---

def icon_size(data: bytes) -> tuple[int, int] | None:
    """The (width, height) an icon image's own header declares, when it is one Windows holds in an icon (a PNG, or a
    bitmap with its mask: twice the height) of at most MAX_ICON_SIDE a side; None for anything else."""
    if data.startswith(PNG_SIGNATURE):
        size = art.image_size(data)
        if size is None:
            return None
        w, h = size
    elif len(data) >= 40 and _u32(data, 0) in BITMAP_HEADERS:
        w, h2 = _i32(data, 4), _i32(data, 8)
        if _u16(data, 12) != 1 or _u16(data, 14) not in BITMAP_BITS or \
                _u32(data, 16) not in (BITMAP_RGB, BITMAP_BITFIELDS) or h2 % 2:
            return None
        h = h2 // 2
    else:
        return None
    return (w, h) if 0 < w <= MAX_ICON_SIDE and 0 < h <= MAX_ICON_SIDE else None


def ico(data: bytes) -> bytes:
    """An icon file (ICONDIR, one ICONDIRENTRY) of one icon image, sized as its own header declares (icon_size)."""
    size = icon_size(data)
    if size is None:
        raise ValueError("not an icon image")
    w, h = size
    bits = 32 if data.startswith(PNG_SIGNATURE) else _u16(data, 14)
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, bits, len(data),
                                                      22) + data


def _group(data: bytes) -> list[int]:
    """The icon ids a group icon (GRPICONDIR: reserved, type 1, count; then 14-byte entries) lists, largest first
    (its listed width times height, 0 meaning 256, then its colour bits), the file's order among equals."""
    if len(data) < 6 or _u16(data, 0) != 0 or _u16(data, 2) != 1:
        raise ValueError("not a group icon")
    count = min(_u16(data, 4), (len(data) - 6) // 14)     # at most MAX_GROUP_ENTRIES: _icon reads no more
    entries = []
    for i in range(count):
        e = data[6 + 14 * i:20 + 14 * i]
        w, h = e[0] or 256, e[1] or 256
        entries.append((-(w * h), -_u16(e, 6), i, _u16(e, 12)))       # wBitCount, nId
    return [nid for *_rank, nid in sorted(entries)]


def _icon(f: Any, img: _Image) -> tuple[str, bytes] | None:
    groups = _of_type(f, img, RT_GROUP_ICON)
    if not groups:
        return None
    ids = _group(_data(f, img, groups[0][1], 6 + 14 * MAX_GROUP_ENTRIES))
    icons = {key: offset for key, offset in _of_type(f, img, RT_ICON) if key is not None}
    for nid in ids:
        if nid not in icons:
            continue
        try:
            image = _data(f, img, icons[nid], MAX_ICON_BYTES)
        except ValueError:
            continue
        if icon_size(image) is not None:
            return ICO, ico(image)
    raise ValueError("its icon holds no image that could be read")


def read(path: str) -> Program:
    """The program's name and icon from its file (any size: only its headers and those resources are read); what
    could not be read is in `problems`, in plain words."""
    problems: list[str] = []
    name = icon = None
    try:
        with stores.open_file(path, READ_BUDGET) as f:
            img = _image(f)
            try:
                versions = _of_type(f, img, RT_VERSION)
                if versions:
                    name = version_name(_data(f, img, versions[0][1], MAX_VERSION_BYTES))
            except ValueError as exc:
                problems.append(f"its name could not be read from {file_name(path)} ({exc})")
            try:
                icon = _icon(f, img)
            except ValueError as exc:
                problems.append(f"its icon could not be read from {file_name(path)} ({exc})")
    except ValueError as exc:
        problems.append(str(exc))
    return Program(name=name, icon=icon, problems=tuple(stores.plain(p) for p in problems))
