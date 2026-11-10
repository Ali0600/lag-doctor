"""Valve's KeyValues text, as Steam writes its library files, read strictly (libraryfolders.vdf, appmanifest_*.acf).

Every local account can write these files (research note desktop-app.md, section 15), so the reader takes only what
Steam itself writes: quoted tokens, braces, whitespace and // comments; the escapes \\\\, \\", \\n and \\t (Valve turns
any other escape into a NUL that cuts the string short, so it is refused here); and no conditionals ([$WIN32]) and
no top-level key that starts with # (Valve's reader takes #include and #base there as directives), which Steam's
library files never hold. Keys are case-insensitive and the first of two equal keys wins, as in Valve's own lookup;
sizes, tokens and nesting are capped. ValueError says what was refused, and where.
"""
from __future__ import annotations

MAX_TOKEN_CHARS = 4096          # Valve's own limit is 4096 bytes a token (KeyValues.cpp); this counts characters
MAX_DEPTH = 16                  # Steam's library files nest 3 deep
MAX_KEYS = 100_000              # keys in one file, all levels together
MAX_NAMED_CHARS = 60            # of a key that a refusal names
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}
# A brace in the text, never a string: a quoted "{" is a value, as in Valve's own reader.
OPEN, CLOSE = object(), object()


def _named(key: str) -> str:
    """A key as a refusal names it: its repr, of at most its first MAX_NAMED_CHARS characters."""
    return repr(key[:MAX_NAMED_CHARS]) + ("..." if len(key) > MAX_NAMED_CHARS else "")


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.keys = 0

    def where(self) -> str:
        line = self.text.count("\n", 0, self.i) + 1
        return f"line {line}"

    def skip(self) -> None:
        """Whitespace and // comments."""
        t, n = self.text, len(self.text)
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif t.startswith("//", self.i):
                end = t.find("\n", self.i)
                self.i = n if end < 0 else end + 1
            else:
                return

    def token(self) -> str | object | None:
        """The next token: a quoted string's text, OPEN or CLOSE for a brace, or None at the end."""
        self.skip()
        t = self.text
        if self.i >= len(t):
            return None
        c = t[self.i]
        if c in "{}":
            self.i += 1
            return OPEN if c == "{" else CLOSE
        if c != '"':
            raise ValueError(f"an unquoted token at {self.where()} (Steam quotes every token)")
        self.i += 1
        out = []
        while True:
            if self.i >= len(t):
                raise ValueError(f"a quoted string that never ends, from {self.where()}")
            c = t[self.i]
            if c == '"':
                self.i += 1
                break
            if c == "\\":
                nxt = t[self.i + 1:self.i + 2]
                if nxt not in _ESCAPES:
                    raise ValueError(f"an escape Steam does not write (\\{nxt}) at {self.where()}")
                out.append(_ESCAPES[nxt])
                self.i += 2
            elif c == "\n":
                raise ValueError(f"a line break inside a quoted string at {self.where()}")
            else:
                out.append(c)
                self.i += 1
            if len(out) > MAX_TOKEN_CHARS:
                raise ValueError(f"a token longer than {MAX_TOKEN_CHARS} characters at {self.where()}")
        text = "".join(out)
        if "\0" in text:
            raise ValueError(f"a NUL inside a token at {self.where()}")
        return text

    def block(self, depth: int) -> dict:
        """Keys and their values up to the closing brace (or the end, at the top)."""
        if depth > MAX_DEPTH:
            raise ValueError(f"blocks nested more than {MAX_DEPTH} deep at {self.where()}")
        out: dict = {}
        while True:
            key = self.token()
            if key is None:
                if depth:
                    raise ValueError("a block that never closes")
                return out
            if key is CLOSE:
                if not depth:
                    raise ValueError(f"a closing brace with no block open at {self.where()}")
                return out
            if key is OPEN:
                raise ValueError(f"a block with no key at {self.where()}")
            if key.startswith("#") and not depth:
                raise ValueError(f"the key {_named(key)} at {self.where()} starts with # (Valve's reader takes "
                                 f"#include and #base there as directives; Steam's library files hold no such key)")
            self.keys += 1
            if self.keys > MAX_KEYS:
                raise ValueError(f"more than {MAX_KEYS} keys")
            self.skip()
            if self.text.startswith("[", self.i):
                raise ValueError(f"a conditional at {self.where()} (Steam's library files hold none)")
            value = self.token()
            if value is None:
                raise ValueError(f"the key {_named(key)} has no value")
            if value is CLOSE:
                raise ValueError(f"the key {_named(key)} has no value at {self.where()}")
            child = self.block(depth + 1) if value is OPEN else value
            self.skip()
            if self.text.startswith("[", self.i):
                raise ValueError(f"a conditional at {self.where()} (Steam's library files hold none)")
            out.setdefault(key.casefold(), child)          # the first of two equal keys wins


def parse(text: str) -> dict:
    """The file's keys and values: str for a value, dict for a block, keys case-folded. ValueError says why not.
    A byte order mark is the file reader's to drop (stores.read_text); here it is text like any other."""
    if not isinstance(text, str):
        raise ValueError("KeyValues text must be a str")
    return _Reader(text).block(0)
