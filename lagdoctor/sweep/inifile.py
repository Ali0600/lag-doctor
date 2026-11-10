"""One value of a game's ini settings file, read or changed, with every other byte left as it was.

Unreal Engine games keep their settings in ini files such as GameUserSettings.ini: `[Section]` lines, then `Key=Value`
lines, and a line starting with ";" or "#" is a comment (docs/research/settings-sweeps.md, section 2). A sweep changes
a few values for its length and puts them back, so this module changes only the characters of one value. Comments,
the order of lines, the spacing around "=", the line endings, the byte-order mark and the encoding stay byte for byte,
and a file whose bytes would not come back the same when written again is refused rather than rewritten.

Lines of Unreal's array syntax (`+Key=`, `-Key=`, `.Key=`, `!Key`) never match a plain key, so they are never changed.
Keys and sections match without regard to case, as Unreal's do. Pure: bytes and text in, bytes and text out; the fix
that writes the file is lagdoctor/fixes/game_setting.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_BYTES = 1 << 20      # an ini is a few KB (the one read on the owner's PC, under 2 KB): refuse anything absurd
LINE_MAX = 4096          # a value put back may be the file's own original, so it is checked only for line safety

KEY = re.compile(r"[A-Za-z0-9_.]{1,128}")                 # sg.StaticMeshQuality, bUseVSync, r.VSync
SECTION = re.compile(r"[A-Za-z0-9_./:-]{1,128}")          # ScalabilityGroups, /Script/Engine.GameUserSettings
VALUE = re.compile(r"[A-Za-z0-9_.+-]{1,64}")              # what a sweep may write: 0, 3, 1.000000, True, -1
_NEWLINE = re.compile(r"\r\n|\n|\r")
_HEADER = re.compile(r"[ \t]*\[([^\]\r\n]*)\][ \t]*")
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_BLANK = " \t"


@dataclass(frozen=True)
class Encoding:
    """How a file's text is stored: its codec, and the byte-order mark it starts with (b"" for none)."""

    codec: str
    bom: bytes

    @property
    def errors(self) -> str:
        # A UTF-8 file is read with surrogateescape, so a byte that is not UTF-8 (an ANSI file's accented letter)
        # comes back as the same byte when the text is written again.
        return "surrogateescape" if self.codec == "utf-8" else "strict"


@dataclass(frozen=True)
class Hit:
    """One `Key=Value` line for the key asked about."""

    line: int          # the line's index (0-based)
    section: str       # the section it is in, as the file spells it
    start: int         # where the value's characters start and end within the line
    end: int
    value: str         # the value, without the spaces and tabs around it


def decode(raw: bytes) -> tuple[str, Encoding]:
    """The file's text and how it is stored. ValueError for a file this module will not rewrite: too large, not
    text, holding NUL characters, or bytes that would not come back the same."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("decode takes the file's bytes")
    raw = bytes(raw)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"the file holds {len(raw):,} bytes, more than a settings file would ({MAX_BYTES:,} at most)")
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            break
    else:
        bom, codec = b"", "utf-8"
    enc = Encoding(codec, bom)
    try:
        text = raw[len(bom):].decode(codec, enc.errors)
    except UnicodeDecodeError as exc:
        raise ValueError(f"the file is not text lag-doctor can read ({exc.reason})") from None
    if "\x00" in text:
        raise ValueError("the file holds NUL characters, so it is not a plain settings file")
    if encode(text, enc) != raw:
        raise ValueError("the file's bytes would not come back the same when written again, so it is left alone")
    return text, enc


def encode(text: str, enc: Encoding) -> bytes:
    """The bytes of `text` stored as `enc` says, the byte-order mark first."""
    return enc.bom + text.encode(enc.codec, enc.errors)


def _lines(text: str) -> list[tuple[str, str]]:
    """(content, line ending) of each line; the last line's ending may be ''. Joined again they are `text`."""
    out, pos = [], 0
    for m in _NEWLINE.finditer(text):
        out.append((text[pos:m.start()], m.group()))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def _hits(lines: list[tuple[str, str]], key: str) -> list[Hit]:
    """Every plain `key=value` line for the key, in file order. Lines before the first section belong to none and
    never count."""
    want = key.casefold()
    section: str | None = None
    out = []
    for i, (content, _end) in enumerate(lines):
        body = content.lstrip(_BLANK)
        if body.startswith("["):
            # A header with anything after its "]" (a comment, say) names no section this module can be sure of, so
            # the keys under it belong to none until the next plain header.
            head = _HEADER.fullmatch(content)
            section = head.group(1).strip() if head is not None else None
            continue
        if section is None or not body or body[0] in ";#" or "=" not in body:
            continue
        name, rest = content.split("=", 1)
        if name.strip().casefold() != want:
            continue
        value = rest.strip(_BLANK)
        if value:
            start = len(name) + 1 + len(rest) - len(rest.lstrip(_BLANK))
            end = start + len(value)
        else:
            start = end = len(content)        # an empty value: a new one goes at the line's end
        out.append(Hit(i, section, start, end, value))
    return out


def _in_section(hits: list[Hit], section: str) -> list[Hit]:
    want = section.strip().casefold()
    return [h for h in hits if h.section.casefold() == want]


def get_value(text: str, section: str, key: str) -> str | None:
    """The key's value in the section (the spaces and tabs around it left out), or None when the section does not set
    it. ValueError when it is set there more than once: which one the game reads cannot be told."""
    hits = _in_section(_hits(_lines(text), key), section)
    if len(hits) > 1:
        raise ValueError(f"{key} is set {len(hits)} times in [{section}], so which one the game reads cannot be told")
    return hits[0].value if hits else None


def set_value(text: str, section: str, key: str, value: str) -> str:
    """The text with the key's value in the section replaced and every other character kept. ValueError unless the
    section sets the key exactly once (a sweep never adds or removes a line), or when the value would break its line."""
    if not line_safe(value):
        raise ValueError(f"{value!r} cannot be written as a value")
    lines = _lines(text)
    hits = _in_section(_hits(lines, key), section)
    if len(hits) != 1:
        raise ValueError(f"{key} is not set in [{section}]" if not hits
                         else f"{key} is set {len(hits)} times in [{section}], so which one the game reads cannot be told")
    h = hits[0]
    content, end = lines[h.line]
    lines[h.line] = (content[:h.start] + value + content[h.end:], end)
    return "".join(c + e for c, e in lines)


def sections_with(text: str, key: str) -> list[str]:
    """Every section that sets the key, each as the file first spells it, in file order."""
    seen: set[str] = set()
    out = []
    for h in _hits(_lines(text), key):
        if h.section.casefold() not in seen:
            seen.add(h.section.casefold())
            out.append(h.section)
    return out


def line_safe(value: object) -> bool:
    """True for a value that keeps its line a `Key=Value` line and reads back as itself: text without a line break or
    a NUL, without spaces or tabs around it, and not absurdly long. (What a sweep may write is narrower: VALUE.)"""
    return (isinstance(value, str) and len(value) <= LINE_MAX and not any(c in value for c in "\r\n\x00")
            and value == value.strip(_BLANK))
