"""A picked program's name and icon (lagdoctor/stores/exe.py), from made-up programs (tests/pe_build.py) and, read
back by Windows itself, from those and from this Python's own python.exe: no game's file is read here."""
from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from lagdoctor import stores
from lagdoctor.capture import target
from lagdoctor.stores import exe
from tests import pe_build

MOSS = {"ProductName": "Moss Garden", "FileDescription": "Moss Garden game"}


def write(tmp_path: Path, data: bytes, name: str = "mossgarden.exe") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def image_of(icon: tuple[str, bytes]) -> bytes:
    kind, data = icon
    assert kind == exe.ICO
    reserved, typ, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, typ, count) == (0, 1, 1)
    w, h, colours, _r, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data, 6)
    assert offset == 22 and size == len(data) - 22 and planes == 1
    return data[22:]


# ---------------------------------------------------------------- the name ---

@pytest.mark.parametrize("bits", [64, 32])
def test_a_program_is_named_by_its_product_name_and_shown_by_its_largest_icon(tmp_path, bits):
    small, large = pe_build.icon_bitmap(16, 16), pe_build.icon_bitmap(48, 48)
    path = write(tmp_path, pe_build.program(MOSS, [small, large], bits=bits))
    p = exe.read(path)
    assert p.name == "Moss Garden" and p.problems == ()
    assert image_of(p.icon) == large
    w, h, _c, _r, _p, b = struct.unpack_from("<BBBBHH", p.icon[1], 6)
    assert (w, h, b) == (48, 48, 32)


def test_the_file_description_names_it_when_there_is_no_product_name_and_neither_gives_none(tmp_path):
    described = exe.read(write(tmp_path, pe_build.program({"FileDescription": "Kiln Runner"})))
    assert described.name == "Kiln Runner" and described.problems == ()
    blank = exe.read(write(tmp_path, pe_build.program({"ProductName": "  \t ", "CompanyName": "Made Up"}), "b.exe"))
    assert blank.name is None and blank.problems == ()
    none = exe.read(write(tmp_path, pe_build.program(None, [pe_build.icon_bitmap(16, 16)]), "c.exe"))
    assert none.name is None and none.icon is not None and none.problems == ()


def test_a_name_is_cleaned_for_display_and_cut(tmp_path):
    p = exe.read(write(tmp_path, pe_build.program({"ProductName": "Moss\u202e\nGarden " + "x" * 300})))
    assert "\n" not in p.name and "\u202e" not in p.name and len(p.name) <= stores.MAX_TITLE_CHARS
    assert p.name.startswith("Moss Garden x")


def test_the_first_string_table_with_a_product_name_names_it(tmp_path):
    res = pe_build.version(tables=[("040704b0", {"FileDescription": "Mooskarten"}),
                                   ("040904b0", {"ProductName": "Moss Garden"}),
                                   ("040c04b0", {"ProductName": "Jardin de Mousse"})])
    assert exe.version_name(res) == "Moss Garden"
    only = pe_build.version(tables=[("040704b0", {"FileDescription": "Mooskarten"}),
                                    ("040904b0", {"FileDescription": "Moss Garden"})])
    assert exe.version_name(only) == "Mooskarten"


@pytest.mark.parametrize("bad", [
    b"", b"\0" * 6, b"\x40\0\0\0\0\0" + "VS_VERSION_INFO".encode("utf-16-le"),     # a block longer than the resource
    b"\x30\0\0\0\0\0" + "VS_VERSION_INF".encode("utf-16-le") + b"\0\0",            # another key
    b"\x10\0\0\0\0\0" + "VS_VERS".encode("utf-16-le"),                              # a key with no end
])
def test_a_version_resource_that_is_not_one_is_refused(bad):
    with pytest.raises(ValueError):
        exe.version_name(bad)


def test_a_version_block_that_does_not_fit_in_its_parent_is_refused():
    res = bytearray(pe_build.version(MOSS))
    struct.pack_into("<H", res, 0, len(res) + 40)           # the root says it is longer than the resource
    with pytest.raises(ValueError, match="does not fit"):
        exe.version_name(bytes(res))


def test_padding_after_the_last_block_inside_its_parent_still_reads():
    table = pe_build._node("040904b0", kind=1, children=[pe_build._string("ProductName", "Moss Garden")])
    sfi = pe_build._node("StringFileInfo", kind=1, children=[table, b"\0" * 8])     # 8 zero bytes inside it
    res = pe_build._node("VS_VERSION_INFO", b"\0" * 52, children=[sfi])
    assert exe.version_name(res) == "Moss Garden"


def test_a_version_resource_of_endless_blocks_stops_at_its_budget():
    leaf = struct.pack("<HHH", 8, 0, 1) + b"\0\0"           # a block of 8 bytes, its key empty
    many = leaf * (exe.MAX_VERSION_NODES + 10)
    root = pe_build._node("VS_VERSION_INFO", b"\0" * 52)
    res = struct.pack("<H", len(root) + len(many)) + root[2:] + many
    with pytest.raises(ValueError, match="too many blocks"):
        exe.version_name(res)


# ---------------------------------------------------------------- the icon ---

def test_the_largest_icon_image_is_taken_then_the_one_of_more_colours(tmp_path):
    images = [pe_build.icon_bitmap(32, 32, bits=8), pe_build.icon_bitmap(32, 32, bits=32),
              pe_build.icon_bitmap(16, 16)]
    p = exe.read(write(tmp_path, pe_build.program(MOSS, images)))
    assert image_of(p.icon) == images[1]


def test_a_png_icon_image_is_taken_as_it_is(tmp_path):
    big = pe_build.png(256, 256)
    p = exe.read(write(tmp_path, pe_build.program(MOSS, [pe_build.icon_bitmap(32, 32), big])))
    assert image_of(p.icon) == big
    w, h = struct.unpack_from("<BB", p.icon[1], 6)
    assert (w, h) == (0, 0)                                 # 256 is written 0, as Windows writes it


@pytest.mark.parametrize("too_big", [
    lambda: pe_build.png(257, 16), lambda: pe_build.png(4096, 4096)[:33] + b"\0" * 64,
    lambda: pe_build.icon_bitmap(512, 16), lambda: struct.pack("<IiiHHI", 40, 64, -128, 1, 32, 0) + b"\0" * 64,
    lambda: struct.pack("<IiiHHI", 40, 16, 32, 2, 32, 0) + b"\0" * 64,     # two planes
    lambda: struct.pack("<IiiHHI", 40, 16, 32, 1, 7, 0) + b"\0" * 64,      # 7 bits a pixel
    lambda: struct.pack("<IiiHHI", 40, 16, 32, 1, 32, 4) + b"\0" * 64,     # a JPEG inside
    lambda: struct.pack("<IiiHHI", 40, 16, 31, 1, 32, 0) + b"\0" * 64,     # no mask's half
    lambda: struct.pack("<IHHHH", 12, 16, 32, 1, 32) + b"\0" * 64,         # an OS/2 BITMAPCOREHEADER
    lambda: struct.pack("<IiiHHI", 64, 16, 32, 1, 32, 0) + b"\0" * 64,     # a header of no size Windows writes
    lambda: b"GIF89a" + b"\0" * 64,
])
def test_an_icon_image_its_header_does_not_hold_within_256_is_passed_over(tmp_path, too_big):
    image = too_big()
    assert exe.icon_size(image) is None
    small = pe_build.icon_bitmap(16, 16)
    res = pe_build.icon_resources([small, image])
    # the bad one listed as the largest, so it is tried first
    res[pe_build.RT_GROUP_ICON][1] = pe_build.group([(255, 255, 32, len(image), 2), (16, 16, 32, len(small), 1)])
    p = exe.read(write(tmp_path, pe_build.program(MOSS, extra=res)))
    assert image_of(p.icon) == small and p.problems == ()


def test_an_icon_whose_images_are_all_bad_is_named_and_the_name_still_reads(tmp_path):
    bad = b"GIF89a" + b"\0" * 64
    p = exe.read(write(tmp_path, pe_build.program(MOSS, [bad])))
    assert p.name == "Moss Garden" and p.icon is None
    assert p.problems == ("its icon could not be read from mossgarden.exe (its icon holds no image that could be "
                          "read)",)


def test_an_icon_image_the_group_names_but_the_file_lacks_is_passed_over(tmp_path):
    small = pe_build.icon_bitmap(16, 16)
    res = pe_build.icon_resources([small])
    res[pe_build.RT_GROUP_ICON][1] = pe_build.group([(48, 48, 32, 4000, 9), (16, 16, 32, len(small), 1)])
    p = exe.read(write(tmp_path, pe_build.program(MOSS, extra=res)))
    assert image_of(p.icon) == small


def test_a_group_listing_more_sizes_than_it_holds_reads_those_it_holds(tmp_path):
    small = pe_build.icon_bitmap(16, 16)
    res = pe_build.icon_resources([small])
    group = bytearray(res[pe_build.RT_GROUP_ICON][1])
    struct.pack_into("<H", group, 4, 5)                     # five sizes listed, one there
    res[pe_build.RT_GROUP_ICON][1] = bytes(group)
    p = exe.read(write(tmp_path, pe_build.program(MOSS, extra=res)))
    assert image_of(p.icon) == small and p.problems == ()


def test_a_cursors_group_is_no_icon(tmp_path):
    small = pe_build.icon_bitmap(16, 16)
    res = pe_build.icon_resources([small])
    group = bytearray(res[pe_build.RT_GROUP_ICON][1])
    struct.pack_into("<H", group, 2, 2)                     # GRPICONDIR's type 2: a cursor's
    res[pe_build.RT_GROUP_ICON][1] = bytes(group)
    p = exe.read(write(tmp_path, pe_build.program(MOSS, extra=res)))
    assert p.icon is None and p.problems == ("its icon could not be read from mossgarden.exe (not a group icon)",)


def test_the_first_group_icon_is_the_programs_icon(tmp_path):
    first, second = pe_build.icon_bitmap(16, 16), pe_build.icon_bitmap(64, 64)
    res = {pe_build.RT_ICON: {1: first, 2: second},
           pe_build.RT_GROUP_ICON: {5: pe_build.group([(16, 16, 32, len(first), 1)]),
                                    7: pe_build.group([(64, 64, 32, len(second), 2)])}}
    p = exe.read(write(tmp_path, pe_build.program(MOSS, extra=res)))
    assert image_of(p.icon) == first


def test_an_icon_image_past_its_cap_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(exe, "MAX_ICON_BYTES", 1000)
    big, small = pe_build.icon_bitmap(32, 32), pe_build.icon_bitmap(8, 8)
    assert len(big) > 1000 > len(small)
    p = exe.read(write(tmp_path, pe_build.program(MOSS, [big, small])))
    assert image_of(p.icon) == small


def test_the_icon_file_names_its_image_as_its_header_does():
    image = pe_build.icon_bitmap(24, 24, bits=8)
    data = exe.ico(image)
    assert data[:6] == struct.pack("<HHH", 0, 1, 1)
    assert struct.unpack_from("<BBBBHHII", data, 6) == (24, 24, 0, 0, 1, 8, len(image), 22)
    with pytest.raises(ValueError):
        exe.ico(b"GIF89a" + b"\0" * 64)


# ---------------------------------------------------------------- what is not a program, or a broken one ---

@pytest.mark.parametrize("damage", [
    lambda b: b"",
    lambda b: b"#!/bin/sh\n" + b"\0" * 100,
    lambda b: b[:40],
    lambda b: b"ZM" + b[2:],
    lambda b: b[:0x3C] + struct.pack("<I", 0x7FFFFFF0) + b[0x40:],          # the PE header far past the file
    lambda b: b[:0x3C] + struct.pack("<I", 8) + b[0x40:],                   # inside the DOS header
    lambda b: b[:64] + b"PX\0\0" + b[68:],
    lambda b: b[:70] + struct.pack("<H", 0) + b[72:],                       # no sections
    lambda b: b[:70] + struct.pack("<H", 97) + b[72:],                      # more than Windows takes
    lambda b: b[:84] + struct.pack("<H", 0) + b[86:],                       # no optional header
    lambda b: b[:88] + struct.pack("<H", 0x107) + b[90:],                   # a ROM image's
])
def test_what_is_not_a_windows_program_is_named_so(tmp_path, damage):
    data = damage(pe_build.program(MOSS, [pe_build.icon_bitmap(16, 16)]))
    p = exe.read(write(tmp_path, data))
    assert p.name is None and p.icon is None
    assert len(p.problems) == 1 and ("is not a Windows program that can be read" in p.problems[0]
                                     or "points past its own end" in p.problems[0]), p.problems


def test_a_program_listing_more_sections_than_windows_takes_is_refused(tmp_path):
    data = bytearray(pe_build.program(MOSS))
    struct.pack_into("<H", data, 70, exe.MAX_SECTIONS + 1)
    data += b"\0" * (64 + 24 + 240 + 40 * (exe.MAX_SECTIONS + 1))         # room for the whole table
    p = exe.read(write(tmp_path, bytes(data)))
    assert p.name is None and p.problems == (f"{tmp_path / 'mossgarden.exe'} is not a Windows program that can be "
                                             "read",)


def test_a_program_whose_optional_header_lists_no_resource_directory_has_none(tmp_path):
    data = bytearray(pe_build.program(MOSS))
    struct.pack_into("<I", data, 64 + 24 + 108, 2)          # NumberOfRvaAndSizes: the export and import ones only
    assert exe.read(write(tmp_path, bytes(data))) == exe.Program(name=None, icon=None, problems=())


def test_a_resource_past_what_the_file_holds_of_its_section_is_named_so(tmp_path):
    data = bytearray(pe_build.program(MOSS))
    struct.pack_into("<I", data, 64 + 24 + 240 + 8, 0x10000)     # the section's virtual size, past its data
    struct.pack_into("<I", data, 64 + 24 + 112 + 20, 0x10000)    # and the resource directory's size with it
    at = data.find(struct.pack("<I", pe_build.LANG), pe_build.FILE_ALIGN)
    entry = pe_build.FILE_ALIGN + struct.unpack_from("<I", data, at + 4)[0]
    struct.pack_into("<I", data, entry, pe_build.SECTION_RVA + 0x800)    # in the section, not in the file
    p = exe.read(write(tmp_path, bytes(data)))
    assert p.problems == ("its name could not be read from mossgarden.exe (" + str(tmp_path / "mossgarden.exe")
                          + " points to a part of itself that is not in the file)",)


def test_a_program_with_no_resources_has_no_name_or_icon_and_no_problem(tmp_path):
    p = exe.read(write(tmp_path, pe_build.pe(b"")))
    assert p == exe.Program(name=None, icon=None, problems=())


def test_a_type_directory_pointing_past_the_resources_is_named_and_the_other_types_still_read(tmp_path):
    data = bytearray(pe_build.program(MOSS, [pe_build.icon_bitmap(16, 16)]))
    rsrc = pe_build.FILE_ALIGN
    assert struct.unpack_from("<I", data, rsrc + 16)[0] == pe_build.RT_ICON      # listed first: types ascend
    struct.pack_into("<I", data, rsrc + 16 + 4, 0x80000000 | 0x0FFFFFF0)    # its directory, far away
    p = exe.read(write(tmp_path, bytes(data)))
    assert p.name == "Moss Garden" and p.icon is None
    assert p.problems == (f"its icon could not be read from mossgarden.exe ({tmp_path / 'mossgarden.exe'} points "
                          "past its own resources)",)


def test_a_data_entry_pointing_outside_its_section_is_named(tmp_path):
    data = bytearray(pe_build.program(MOSS))
    at = data.find(struct.pack("<I", pe_build.LANG), pe_build.FILE_ALIGN)    # the version's language entry
    assert at > 0
    entry = pe_build.FILE_ALIGN + struct.unpack_from("<I", data, at + 4)[0]
    struct.pack_into("<I", data, entry, 0x00F00000)        # the version's data, at an address no section holds
    p = exe.read(write(tmp_path, bytes(data)))
    assert p.name is None
    assert p.problems == ("its name could not be read from mossgarden.exe (" + str(tmp_path / "mossgarden.exe")
                          + " points to a part of itself that is not in the file)",)


def test_a_directory_listing_more_entries_than_its_cap_reads_only_the_cap(tmp_path, monkeypatch):
    data = bytearray(pe_build.program(MOSS))
    big = 1 << 20                                           # a resource section a megabyte long, all of it in the file
    struct.pack_into("<I", data, 64 + 24 + 112 + 20, big)
    struct.pack_into("<II", data, 64 + 24 + 240 + 8, big, pe_build.SECTION_RVA)
    struct.pack_into("<I", data, 64 + 24 + 240 + 16, big)
    data += b"\0" * (pe_build.FILE_ALIGN + big - len(data))
    struct.pack_into("<HH", data, pe_build.FILE_ALIGN + 12, 0, 0xFFFF)     # the type directory claims 65,535 ids
    asked = []
    real = stores.Reader.at

    def at(self, offset, n):
        asked.append(n)
        return real(self, offset, n)
    monkeypatch.setattr(stores.Reader, "at", at)
    exe.read(write(tmp_path, bytes(data)))
    assert max(asked) <= 8 * exe.MAX_ENTRIES


def test_a_resource_larger_than_its_cap_is_refused_before_it_is_read(tmp_path, monkeypatch):
    data = bytearray(pe_build.program(MOSS))
    at = data.find(struct.pack("<I", pe_build.LANG), pe_build.FILE_ALIGN)
    entry = pe_build.FILE_ALIGN + struct.unpack_from("<I", data, at + 4)[0]
    struct.pack_into("<I", data, entry + 4, exe.MAX_VERSION_BYTES + 1)
    asked = []
    real = stores.Reader.at

    def read_at(self, offset, n):
        asked.append(n)
        return real(self, offset, n)
    monkeypatch.setattr(stores.Reader, "at", read_at)
    p = exe.read(write(tmp_path, bytes(data)))
    assert p.name is None and "larger than" in p.problems[0]
    assert max(asked) < exe.MAX_VERSION_BYTES


def test_a_program_is_read_within_its_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(exe, "READ_BUDGET", 600)            # the headers fit; the resources do not
    p = exe.read(write(tmp_path, pe_build.program(MOSS, [pe_build.icon_bitmap(48, 48)])))
    assert p.icon is None
    assert any("budget" in q for q in p.problems), p.problems


def test_a_program_off_the_local_disks_is_never_opened(tmp_path, monkeypatch):
    path = write(tmp_path, pe_build.program(MOSS))
    monkeypatch.setattr(target, "local_problem", lambda p: f"{p} is kept online only")
    opened = []
    monkeypatch.setattr(stores, "_open", lambda p: opened.append(p))
    p = exe.read(path)
    assert p == exe.Program(name=None, icon=None, problems=(f"{path} is kept online only",))
    assert opened == []


def test_a_folder_is_not_a_program(tmp_path):
    p = exe.read(str(tmp_path))
    assert p.name is None and p.problems == (f"{tmp_path} is not a file",)


def test_the_file_name_and_stem_of_a_program():
    assert exe.file_name("D:\\Games\\Moss Garden\\Moss Garden.EXE") == "Moss Garden.EXE"
    assert exe.stem("D:\\Games\\Moss Garden\\Moss Garden.EXE") == "Moss Garden"
    assert exe.stem("D:\\Games\\x\\run") == "run"


# ---------------------------------------------------------------- stores.open_file ---

def test_open_file_reads_parts_within_the_file_and_its_budget(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(bytes(range(100)))
    with stores.open_file(str(path), 30) as f:
        assert f.size == 100
        assert f.at(10, 5) == bytes(range(10, 15))
        with pytest.raises(ValueError, match="past its own end"):
            f.at(98, 5)
        with pytest.raises(ValueError, match="past its own end"):
            f.at(-1, 2)
        assert f.at(0, 25) == bytes(range(25))
        with pytest.raises(ValueError, match="budget"):
            f.at(50, 1)                                     # 30 asked for in all, and 30 already read


def test_open_file_names_a_file_that_shrank_while_it_was_read(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(bytes(100))
    with stores.open_file(str(path), 1000) as f:
        f.size = 200                                        # as if the stat had said more than the file now holds
        with pytest.raises(ValueError, match="changed while it was read"):
            f.at(90, 20)


# ---------------------------------------------------------------- Windows' own reading of the same files ---

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
ver = ctypes.WinDLL("version", use_last_error=True)
k32.LoadLibraryExW.restype = ctypes.c_void_p
k32.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD)
k32.FreeLibrary.argtypes = (ctypes.c_void_p,)
k32.FindResourceW.restype = ctypes.c_void_p
k32.FindResourceW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
k32.SizeofResource.restype = wintypes.DWORD
k32.SizeofResource.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
k32.LoadResource.restype = ctypes.c_void_p
k32.LoadResource.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
k32.LockResource.restype = ctypes.c_void_p
k32.LockResource.argtypes = (ctypes.c_void_p,)
ENUM_NAMES = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
k32.EnumResourceNamesW.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ENUM_NAMES, ctypes.c_void_p)
u32.LookupIconIdFromDirectoryEx.argtypes = (ctypes.c_char_p, wintypes.BOOL, ctypes.c_int, ctypes.c_int, wintypes.UINT)
u32.CreateIconFromResourceEx.restype = ctypes.c_void_p
u32.CreateIconFromResourceEx.argtypes = (ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                         ctypes.c_int, ctypes.c_int, wintypes.UINT)
u32.DestroyIcon.argtypes = (ctypes.c_void_p,)
ver.GetFileVersionInfoSizeExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
ver.GetFileVersionInfoExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p)
ver.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                               ctypes.POINTER(wintypes.UINT))
LOAD_LIBRARY_AS_DATAFILE = 0x2          # libloaderapi.h
FILE_VER_GET_NEUTRAL = 0x2              # verrsrc.h
LR_DEFAULTCOLOR = 0                     # winuser.h
ICON_VERSION = 0x00030000               # CreateIconFromResourceEx's dwVer for Windows 3.x and later icons


def windows_name(path: str) -> str | None:
    """VerQueryValueW's product name, else file description, from the first translation the file lists."""
    handle = wintypes.DWORD()
    size = ver.GetFileVersionInfoSizeExW(FILE_VER_GET_NEUTRAL, path, ctypes.byref(handle))
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    assert ver.GetFileVersionInfoExW(FILE_VER_GET_NEUTRAL, path, 0, size, buf)
    p, n = ctypes.c_void_p(), wintypes.UINT()
    assert ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(p), ctypes.byref(n)) and n.value >= 4
    lang, cp = struct.unpack("<HH", ctypes.string_at(p.value, 4))
    for key in ("ProductName", "FileDescription"):
        if ver.VerQueryValueW(buf, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}", ctypes.byref(p), ctypes.byref(n)) \
                and n.value:
            found = stores.name(ctypes.wstring_at(p.value, n.value).split("\0")[0])
            if found:
                return found
    return None


def windows_icon(path: str) -> bytes | None:
    """The RT_ICON image Windows picks at 256 x 256 from the program's first group icon, as LoadResource gives it,
    after checking that Windows makes an icon of it."""
    h = k32.LoadLibraryExW(path, None, LOAD_LIBRARY_AS_DATAFILE)
    assert h, ctypes.get_last_error()
    try:
        first = []

        def take(module, kind, name, param):
            first.append(name)
            return False
        k32.EnumResourceNamesW(h, pe_build.RT_GROUP_ICON, ENUM_NAMES(take), None)
        if not first:
            return None
        res = k32.FindResourceW(h, first[0], pe_build.RT_GROUP_ICON)
        directory = ctypes.string_at(k32.LockResource(k32.LoadResource(h, res)), k32.SizeofResource(h, res))
        nid = u32.LookupIconIdFromDirectoryEx(directory, True, 256, 256, LR_DEFAULTCOLOR)
        assert nid
        res = k32.FindResourceW(h, nid, pe_build.RT_ICON)
        assert res
        image = ctypes.string_at(k32.LockResource(k32.LoadResource(h, res)), k32.SizeofResource(h, res))
        icon = u32.CreateIconFromResourceEx(image, len(image), True, ICON_VERSION, 0, 0, LR_DEFAULTCOLOR)
        assert icon, f"Windows made no icon of {path}'s image {nid}"
        u32.DestroyIcon(icon)
        return image
    finally:
        k32.FreeLibrary(h)


MADE_UP = {
    "bitmaps, 64-bit": lambda: pe_build.program(MOSS, [pe_build.icon_bitmap(16, 16), pe_build.icon_bitmap(48, 48)]),
    "bitmaps, 32-bit": lambda: pe_build.program(MOSS, [pe_build.icon_bitmap(32, 32), pe_build.icon_bitmap(16, 16)],
                                                bits=32),
    "a png and a bitmap": lambda: pe_build.program({"FileDescription": "Kiln Runner"},
                                                   [pe_build.icon_bitmap(32, 32), pe_build.png(256, 256)]),
    "8-bit colours": lambda: pe_build.program(MOSS, [pe_build.icon_bitmap(32, 32, bits=8)]),
    "two tables": lambda: pe_build.program(version_resource=pe_build.version(
        tables=[("040904b0", {"ProductName": "Moss Garden"}), ("040704b0", {"ProductName": "Moosgarten"})])),
}


@pytest.mark.parametrize("kind", sorted(MADE_UP))
def test_windows_reads_the_made_up_programs_as_the_reader_does(tmp_path, kind):
    path = write(tmp_path, MADE_UP[kind]())
    p = exe.read(path)
    assert p.problems == ()
    assert p.name == windows_name(path)
    expected = windows_icon(path)
    assert (image_of(p.icon) if p.icon else None) == expected


def test_windows_reads_this_pythons_own_programs_as_the_reader_does():
    base = Path(getattr(sys, "_base_executable", sys.executable))
    programs = [str(base)] + [str(p) for p in (base.with_name("pythonw.exe"),) if p.is_file()]
    for path in programs:
        p = exe.read(path)
        assert p.problems == (), path
        assert p.name is not None and p.name == windows_name(path), path
        assert p.icon is not None and image_of(p.icon) == windows_icon(path), path
