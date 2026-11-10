"""The settings-file editor: one value's characters change, every other byte stays (lagdoctor/sweep/inifile.py)."""
from __future__ import annotations

import pytest

from lagdoctor.sweep import inifile

# Shaped like the GameUserSettings.ini read on a real install (docs/research/settings-sweeps.md, section 2): two
# sections, CRLF endings, no byte-order mark. The values are made up.
FILE = ("[ScalabilityGroups]\r\n"
        "sg.ResolutionQuality=100\r\n"
        "sg.ShadowQuality=0\r\n"
        "sg.StaticMeshQuality=3\r\n"
        "\r\n"
        "[/Script/Engine.GameUserSettings]\r\n"
        "bUseVSync=False\r\n"
        "FrameRateLimit=0.000000\r\n")


def test_a_value_changes_and_nothing_else_does():
    new = inifile.set_value(FILE, "ScalabilityGroups", "sg.StaticMeshQuality", "1")
    assert new == FILE.replace("sg.StaticMeshQuality=3", "sg.StaticMeshQuality=1")
    assert inifile.get_value(new, "ScalabilityGroups", "sg.StaticMeshQuality") == "1"
    assert inifile.set_value(new, "ScalabilityGroups", "sg.StaticMeshQuality", "3") == FILE


@pytest.mark.parametrize("bom,codec,newline", [(b"", "utf-8", "\r\n"), (b"\xef\xbb\xbf", "utf-8", "\n"),
                                               (b"\xff\xfe", "utf-16-le", "\r\n"), (b"\xfe\xff", "utf-16-be", "\r")])
def test_every_encoding_and_line_ending_comes_back_byte_for_byte(bom, codec, newline):
    raw = bom + FILE.replace("\r\n", newline).encode(codec)
    text, enc = inifile.decode(raw)
    assert enc == inifile.Encoding(codec, bom)
    changed = inifile.encode(inifile.set_value(text, "ScalabilityGroups", "sg.ShadowQuality", "2"), enc)
    assert changed.startswith(bom) and changed != raw
    back, _ = inifile.decode(changed)
    assert inifile.encode(inifile.set_value(back, "ScalabilityGroups", "sg.ShadowQuality", "0"), enc) == raw


def test_bytes_that_are_not_utf8_come_back_as_they_were():
    # An ANSI file's accented letter (0xE9) is not UTF-8; it must survive an edit elsewhere unchanged.
    raw = b"[Game]\r\nName=Caf\xe9\r\nLevel=3\r\n"
    text, enc = inifile.decode(raw)
    edited = inifile.encode(inifile.set_value(text, "Game", "Level", "4"), enc)
    assert edited == b"[Game]\r\nName=Caf\xe9\r\nLevel=4\r\n"


def test_the_spacing_around_the_value_is_kept():
    text = "[S]\n  sg.X = 3  \n"
    assert inifile.get_value(text, "S", "sg.X") == "3"
    assert inifile.set_value(text, "S", "sg.X", "12") == "[S]\n  sg.X = 12  \n"


def test_an_empty_value_takes_a_value_and_gives_it_back():
    text = "[S]\nsg.X=\nsg.Y=   \n"
    assert inifile.get_value(text, "S", "sg.X") == "" and inifile.get_value(text, "S", "sg.Y") == ""
    one = inifile.set_value(text, "S", "sg.Y", "1")
    assert one == "[S]\nsg.X=\nsg.Y=   1\n" and inifile.set_value(one, "S", "sg.Y", "") == text


def test_keys_and_sections_match_whatever_their_case():
    assert inifile.get_value(FILE, "scalabilitygroups", "SG.STATICMESHQUALITY") == "3"
    assert inifile.sections_with(FILE, "bUseVSync") == ["/Script/Engine.GameUserSettings"]


@pytest.mark.parametrize("line", [";sg.X=1", "# sg.X=1", "+sg.X=1", "-sg.X=1", ".sg.X=1", "!sg.X"])
def test_comments_and_unreal_array_lines_never_match_a_plain_key(line):
    text = f"[S]\n{line}\n"
    assert inifile.get_value(text, "S", "sg.X") is None
    with pytest.raises(ValueError):
        inifile.set_value(text, "S", "sg.X", "2")


def test_keys_before_the_first_section_belong_to_none():
    text = "sg.X=1\n[S]\nsg.Y=2\n"
    assert inifile.sections_with(text, "sg.X") == []


def test_a_header_with_something_after_it_does_not_leave_its_keys_in_the_section_before():
    # Unreal writes plain headers; one carrying a comment names no section this module can be sure of, so its keys
    # must never be taken for the previous section's (which a sweep would then change by mistake).
    text = "[ScalabilityGroups]\nsg.X=3\n[Other] ; a comment\nsg.Y=1\n[Last]\nsg.Z=0\n"
    assert inifile.get_value(text, "ScalabilityGroups", "sg.Y") is None
    assert inifile.sections_with(text, "sg.Y") == []
    assert inifile.get_value(text, "Last", "sg.Z") == "0"


def test_a_key_set_twice_in_its_section_is_refused():
    text = "[S]\nsg.X=1\nsg.X=2\n"
    with pytest.raises(ValueError, match="2 times"):
        inifile.get_value(text, "S", "sg.X")
    with pytest.raises(ValueError, match="2 times"):
        inifile.set_value(text, "S", "sg.X", "3")


def test_a_section_written_twice_counts_its_keys_together():
    # Unreal merges a section that appears twice, so a key in both copies is set twice.
    text = "[S]\nsg.X=1\n[T]\nsg.X=5\n[S]\nsg.X=2\n"
    with pytest.raises(ValueError):
        inifile.get_value(text, "S", "sg.X")
    assert inifile.get_value(text, "T", "sg.X") == "5"
    assert inifile.sections_with(text, "sg.X") == ["S", "T"]


def test_a_missing_key_reads_as_none_and_is_never_added():
    assert inifile.get_value(FILE, "ScalabilityGroups", "sg.Missing") is None
    with pytest.raises(ValueError, match="not set"):
        inifile.set_value(FILE, "ScalabilityGroups", "sg.Missing", "1")


@pytest.mark.parametrize("value", ["1\r\n[Evil]", "1\n", "a\x00b", " 1", "1\t", "x" * (inifile.LINE_MAX + 1), 3])
def test_a_value_that_would_break_its_line_is_never_written(value):
    assert not inifile.line_safe(value)
    with pytest.raises(ValueError):
        inifile.set_value(FILE, "ScalabilityGroups", "sg.ShadowQuality", value)


def test_files_that_are_no_settings_file_are_refused():
    with pytest.raises(ValueError, match="more than a settings file"):
        inifile.decode(b"[S]\n" + b"x" * inifile.MAX_BYTES)
    with pytest.raises(ValueError, match="NUL"):
        inifile.decode("[S]\r\nsg.X=1\r\n".encode("utf-16-le"))      # UTF-16 without its byte-order mark
    with pytest.raises(ValueError, match="not text"):
        inifile.decode(b"\xff\xfe[\x00S\x00]")                       # an odd number of UTF-16 bytes
    with pytest.raises(TypeError):
        inifile.decode("[S]")


@pytest.mark.parametrize("pattern,good,bad", [
    (inifile.KEY, ["sg.StaticMeshQuality", "bUseVSync", "r.VSync"], ["", "sg X", "a=b", "x" * 129]),
    (inifile.SECTION, ["ScalabilityGroups", "/Script/Engine.GameUserSettings"], ["", "S]", "A B", "x" * 129]),
    (inifile.VALUE, ["0", "3", "1.000000", "True", "-1", "+2"], ["", "1 2", "1;", "(X=1)", "x" * 65]),
])
def test_what_a_sweep_may_name_and_write(pattern, good, bad):
    assert all(pattern.fullmatch(x) for x in good)
    assert not any(pattern.fullmatch(x) for x in bad)
