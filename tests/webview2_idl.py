"""A reader for Microsoft's WebView2.idl: each interface's IID, parent and methods, and the vtable slots they make.

lagdoctor/winapi/webview2.py calls WebView2 through raw vtable slots, so a slot one off calls the wrong method.
tests/test_webview2_idl.py checks every slot and IID that module names against the IDL of the SDK it was written for,
which tests/downloads.py fetches and pins. The IDL is MIDL:

    [uuid(76eceacb-0462-4d94-ac83-423a6793775e), object, pointer_default(unique)]
    interface ICoreWebView2 : IUnknown {
      [propget] HRESULT Settings([out, retval] ICoreWebView2Settings** settings);
      HRESULT Navigate([in] LPCWSTR uri);
      ...
    }

An interface's methods take its vtable's slots in the order written, after all of its parents' methods, which come
after IUnknown's QueryInterface, AddRef and Release (slots 0-2). The C vtable names a [propget] method get_Name and a
[propput] one put_Name. A forward declaration (`interface Name;`) has no body and defines nothing. Anything inside an
interface that the reader does not know raises ValueError: a member skipped or miscounted would move every slot after
it, and a reader that guessed would hide that.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

IUNKNOWN = "IUnknown"
IUNKNOWN_IID = "00000000-0000-0000-C000-000000000046"     # unknwn.idl, which the IDL imports rather than defines
IUNKNOWN_METHODS = ("QueryInterface", "AddRef", "Release")
PREFIXES = {"propget": "get_", "propput": "put_", "propputref": "putref_"}
GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

# Strings and comments, matched together from left to right, so a "//" inside a string, or a quote or "/*" inside a
# "//" comment, is read as part of what it is inside. The lone openers at the end match only one that is never closed.
_SKIP = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/|/\*|"', re.S)
_TOKEN = re.compile(r'(?P<space>\s+)|(?P<token>"(?:[^"\\\n]|\\.)*"|[A-Za-z_]\w*|[0-9][\w.]*|[][(){};:,*=<>|&~+./#-])'
                    r'|(?P<other>.)', re.S)
_WORD = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class Interface:
    name: str
    iid: str                     # as the IDL writes it (WebView2.idl: lower case)
    parent: str | None           # None only for an interface with no base, which is no COM interface
    methods: tuple[str, ...]     # its own methods in vtable order, named as the C vtable names them


def strip_comments(text: str) -> str:
    """`text` with every comment blanked (newlines kept) and strings left whole. Raises ValueError for a string or a
    /* comment that is never closed."""
    def blank(m: re.Match) -> str:
        found = m.group(0)
        if found in ('"', "/*"):
            line = text.count("\n", 0, m.start()) + 1
            raise ValueError(f"a {found} on line {line} is never closed")
        return found if found.startswith('"') else re.sub(r"[^\n]", " ", found)
    return _SKIP.sub(blank, text)


def tokens(text: str) -> list[str]:
    """The tokens of `text` once its comments are gone: words, numbers, strings (whole) and punctuation."""
    out = []
    for m in _TOKEN.finditer(strip_comments(text)):
        if m.group("other") is not None:
            raise ValueError(f"unexpected character {m.group('other')!r} in the IDL")
        if m.group("token") is not None:
            out.append(m.group("token"))
    return out


def _bracketed(toks: list[str], i: int) -> tuple[list[str], int]:
    """The tokens inside the [...] that starts at toks[i], and the index after its closing ]."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j] == "[":
            depth += 1
        elif toks[j] == "]":
            depth -= 1
            if depth == 0:
                return toks[i + 1:j], j + 1
    raise ValueError("an attribute list [ is never closed")


def _attribute_words(attrs: list[str]) -> list[str]:
    """The name of each attribute in an attribute list: the first token of each comma-separated part."""
    words, depth, start = [], 0, True
    for t in attrs:
        if start:
            words.append(t)
            start = False
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        elif t == "," and depth == 0:
            start = True
    return words


def _uuid(attrs: list[str], name: str) -> str:
    found = []
    for k, t in enumerate(attrs):
        if t == "uuid" and k + 1 < len(attrs) and attrs[k + 1] == "(":
            end = attrs.index(")", k)
            found.append("".join(attrs[k + 2:end]).strip('"'))
    if len(found) != 1:
        raise ValueError(f"interface {name} has {len(found)} uuid attributes, not one")
    if not GUID.fullmatch(found[0]):
        raise ValueError(f"interface {name} has uuid {found[0]!r}, which is not a GUID")
    return found[0]


def _member_end(toks: list[str], i: int, name: str) -> int:
    """The index of the ; that ends the member starting at toks[i] (outside any parentheses or braces)."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t in ("(", "{"):
            depth += 1
        elif t in (")", "}"):
            depth -= 1
            if depth < 0:
                raise ValueError(f"interface {name}: a member ends without its ;")
        elif t == ";" and depth == 0:
            return j
    raise ValueError(f"interface {name}: a member has no ;")


def _body(toks: list[str], i: int, name: str) -> tuple[tuple[str, ...], int]:
    """The methods of the interface body starting after its { at toks[i], and the index after its closing }."""
    methods = []
    while True:
        if i >= len(toks):
            raise ValueError(f"interface {name} is never closed")
        if toks[i] == "}":
            return tuple(methods), i + 1
        if toks[i] == ";":                                   # an empty member: no slot
            i += 1
            continue
        if toks[i] == "cpp_quote":                           # cpp_quote("...") ends without a ;
            if toks[i + 1:i + 2] != ["("] or toks[i + 3:i + 4] != [")"] or not toks[i + 2].startswith('"'):
                raise ValueError(f"interface {name}: a cpp_quote that is not cpp_quote(\"...\")")
            i += 4
            continue
        attrs = []
        if toks[i] == "[":
            attrs, i = _bracketed(toks, i)
        end = _member_end(toks, i, name)
        member, i = toks[i:end], end + 1
        if "(" not in member:
            if member and member[0] in ("typedef", "const"):  # declarations, no slot
                continue
            raise ValueError(f"interface {name}: a member that is no method: {' '.join(member)!r}")
        k = member.index("(")
        if k < 2 or not _WORD.fullmatch(member[k - 1]) or member[-1] != ")":
            raise ValueError(f"interface {name}: a method the reader cannot read: {' '.join(member)!r}")
        prefixes = [PREFIXES[w] for w in _attribute_words(attrs) if w in PREFIXES]
        if len(prefixes) > 1:
            raise ValueError(f"interface {name}: {member[k - 1]} has more than one property attribute")
        methods.append((prefixes[0] if prefixes else "") + member[k - 1])


def parse(text: str) -> list[Interface]:
    """Every interface `text` defines (forward declarations left out), in the order written."""
    toks = tokens(text)
    found: list[Interface] = []
    attrs: list[str] | None = None            # the attribute list just before the current token, if any
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "[":
            attrs, i = _bracketed(toks, i)
            continue
        if t != "interface":
            attrs = None
            i += 1
            continue
        name = toks[i + 1] if i + 1 < len(toks) else ""
        if not _WORD.fullmatch(name):
            raise ValueError(f"'interface' followed by {name!r}, not a name")
        j = i + 2
        if toks[j:j + 1] == [";"]:                            # a forward declaration
            attrs, i = None, j + 1
            continue
        parent = None
        if toks[j:j + 1] == [":"]:
            parent = toks[j + 1] if j + 1 < len(toks) else ""
            if not _WORD.fullmatch(parent):
                raise ValueError(f"interface {name}: its parent {parent!r} is not a name")
            j += 2
        if toks[j:j + 1] != ["{"]:
            raise ValueError(f"interface {name}: no body and no ; after its name")
        if attrs is None:
            raise ValueError(f"interface {name} has no attributes, so no uuid")
        iid = _uuid(attrs, name)
        methods, i = _body(toks, j + 1, name)
        found.append(Interface(name, iid, parent, methods))
        attrs = None
    return found


def by_name(found: list[Interface]) -> dict[str, Interface]:
    """`found` keyed by name. Raises ValueError for a name defined twice."""
    out: dict[str, Interface] = {}
    for itf in found:
        if itf.name in out:
            raise ValueError(f"interface {itf.name} is defined twice")
        out[itf.name] = itf
    return out


def load(path: Path) -> dict[str, Interface]:
    """The interfaces of the IDL file at `path`, by name."""
    return by_name(parse(Path(path).read_text(encoding="utf-8")))


def vtable(defs: Mapping[str, Interface], name: str) -> list[str]:
    """The methods of `name`'s vtable, slot by slot: IUnknown's, then each parent's from the root down, then its own."""
    chain: list[Interface] = []
    while name != IUNKNOWN:
        if name not in defs:
            raise ValueError(f"interface {name} is not defined in the IDL")
        if any(itf.name == name for itf in chain):
            raise ValueError(f"interface {name} inherits from itself")
        chain.append(defs[name])
        if defs[name].parent is None:
            raise ValueError(f"interface {name} has no parent, so it does not start with IUnknown's methods")
        name = defs[name].parent
    methods = list(IUNKNOWN_METHODS)
    for itf in reversed(chain):
        methods.extend(itf.methods)
    return methods


def slots(defs: Mapping[str, Interface], name: str) -> dict[str, int]:
    """{method: slot} for `name`'s vtable. Raises ValueError when two slots share a name, which a dict cannot hold."""
    out: dict[str, int] = {}
    for slot, method in enumerate(vtable(defs, name)):
        if method in out:
            raise ValueError(f"interface {name} has two methods named {method}: slots {out[method]} and {slot}")
        out[method] = slot
    return out


def same_iid(a: str, b: str) -> bool:
    """Whether two IIDs, each written as 8-4-4-4-12 hex digits without braces, are the same; case does not matter."""
    return bool(GUID.fullmatch(a)) and bool(GUID.fullmatch(b)) and a.upper() == b.upper()
