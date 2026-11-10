"""Play's store launch (docs/LAUNCHER-PLAN.md, M6): a game starts only through its store's link, which Explorer opens.

Each link is built from its store's fixed template, and only after every field passes a strict grammar: the fields
come from library files that every local account can write (research note desktop-app.md, section 5). Steam's link
carries no arguments, so Steam asks nothing for it (section 14); Epic's is its documented three-part form. `play`
takes only a link these built, refuses when lag-doctor runs elevated, and hands the link to Explorer
(winapi/launch.py), so lag-doctor creates no process and holds no handle: the store creates the game.
"""
from __future__ import annotations

import re

from lagdoctor.winapi import launch as shell

STEAM_APPID = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)       # Steam's app ids are unsigned 32-bit numbers
MAX_STEAM_APPID = 0xFFFFFFFF
# Epic publishes no grammar for its ids. Every one seen is in RFC 3986's unreserved set and not always 32 hex digits
# (Fortnite's namespace is "fn", the engine's app name "UE_5.4"), so: letters and digits, with dashes, underscores and
# dots inside. Each is then the same written raw or percent-encoded, and none can end a part of the link or start a
# query key (Epic's link takes arg= and env.<name>= keys).
EPIC_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?", re.ASCII)
STEAM_LINK = re.compile(r"steam://rungameid/([1-9][0-9]{0,9})", re.ASCII)
EPIC_LINK = re.compile(r"com\.epicgames\.launcher://apps/([A-Za-z0-9._-]+)%3A([A-Za-z0-9._-]+)%3A([A-Za-z0-9._-]+)"
                       r"\?action=launch&silent=true", re.ASCII)


def steam_link(appid: object) -> str:
    """steam://rungameid/<appid>, with no arguments; ValueError unless appid is a Steam app id written in digits."""
    text = appid if isinstance(appid, str) else None
    if text is None or not STEAM_APPID.fullmatch(text) or int(text) > MAX_STEAM_APPID:
        raise ValueError(f"not a Steam app id: {appid!r}")
    return f"steam://rungameid/{text}"


def epic_link(namespace: object, item: object, app: object) -> str:
    """Epic's launch link for a game's catalog namespace, catalog item id and app name; ValueError unless each is an
    id of letters and digits, with dashes, underscores and dots inside."""
    for name, value in (("catalog namespace", namespace), ("catalog item id", item), ("app name", app)):
        if not isinstance(value, str) or not EPIC_ID.fullmatch(value):
            raise ValueError(f"not an Epic {name}: {value!r}")
    return f"com.epicgames.launcher://apps/{namespace}%3A{item}%3A{app}?action=launch&silent=true"


def store_of(link: object) -> str | None:
    """"steam" or "epic" for a link steam_link or epic_link built, else None."""
    if not isinstance(link, str):
        return None
    m = STEAM_LINK.fullmatch(link)
    if m and int(m.group(1)) <= MAX_STEAM_APPID:
        return "steam"
    m = EPIC_LINK.fullmatch(link)
    if m and all(EPIC_ID.fullmatch(part) for part in m.groups()):
        return "epic"
    return None


def play(link: str) -> dict:
    """Have Explorer open a store link that steam_link or epic_link built: winapi/launch.py's dict, plus "store".

    Refuses (ValueError) any other link, and (PermissionError) when lag-doctor runs elevated: Play is the player's
    own, unelevated window's to start. OSError when Explorer's desktop cannot be reached; the link is then not
    opened any other way.
    """
    store = store_of(link)
    if store is None:
        raise ValueError(f"not a store link lag-doctor builds: {link!r}")
    from lagdoctor.winapi import elevation
    if elevation.is_elevated():
        raise PermissionError("lag-doctor is running with administrator rights, and Play starts games only from the "
                              "player's own window")
    done = shell.open_through_explorer(link)
    return {**done, "store": store}
