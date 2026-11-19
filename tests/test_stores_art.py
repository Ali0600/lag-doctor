"""The library's cover art (lagdoctor/stores/art.py): Steam's own cache, read through stores' file access, each image's
type told from its bytes, every size capped. Every folder here is the test's own (tmp_path): no test looks for this
PC's Steam (the autouse guard fails one that would), and the games are tests/app_demo.py's made-up ones."""
from __future__ import annotations

import dataclasses

import pytest

from lagdoctor import stores
from lagdoctor.stores import art, steam
from tests import app_demo

def jpeg(width: int, height: int) -> bytes:
    """A JPEG's segments up to its first frame header (SOF0), which declares the size, then its end."""
    return (bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 16]) + b"JFIF" + bytes(10)
            + bytes([0xFF, 0xC0, 0, 17, 8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes([3])
            + bytes(9) + bytes([0xFF, 0xD9]))


def webp(width: int, height: int, kind: str = "VP8X") -> bytes:
    """A WebP's RIFF header and one chunk that declares the size: the extended canvas, a lossless or a lossy frame."""
    if kind == "VP8X":
        body = bytes(4) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    elif kind == "VP8L":
        body = bytes([0x2F]) + ((width - 1) | (height - 1) << 14).to_bytes(4, "little") + bytes(5)
    else:
        body = bytes(3) + bytes([0x9D, 0x01, 0x2A]) + width.to_bytes(2, "little") + height.to_bytes(2, "little")
    chunk = kind.encode("ascii").ljust(4) + len(body).to_bytes(4, "little") + body
    return b"RIFF" + (4 + len(chunk)).to_bytes(4, "little") + b"WEBP" + chunk


def png_header(width: int, height: int) -> bytes:
    """A PNG's signature and IHDR alone: what it declares, with nothing to decode."""
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return app_demo.COVER[1][:8] + len(ihdr).to_bytes(4, "big") + b"IHDR" + ihdr + bytes(4)


PNG = app_demo.COVER[1]                        # 60 x 90
JPEG = jpeg(600, 900)
WEBP = webp(600, 900)
RACER = app_demo.INSTALLED[0]                  # Steam, app id 3999901
COAST = app_demo.INSTALLED[2]                  # Steam, app id 3999902
LANTERN = app_demo.INSTALLED[1]                # Epic: no art on the PC


@pytest.fixture(autouse=True)
def no_real_steam(monkeypatch):
    monkeypatch.setattr(steam, "find_steam_folder", lambda: pytest.fail("a test looked for this PC's Steam"))


def cache(steam_folder):
    folder = steam_folder / "appcache" / "librarycache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.mark.parametrize("data, kind", [
    (JPEG, art.JPEG), (PNG, art.PNG), (WEBP, art.WEBP),
    (b"GIF89a" + bytes(10), None), (b"", None), (b"RIFF" + bytes(4) + b"WAVEfmt ", None), (b"<svg/>", None),
    (PNG[:7], None),                           # a PNG's signature cut short
])
def test_an_images_type_is_told_from_its_bytes_and_anything_else_is_none(data, kind):
    assert art.image_type(data) == kind


@pytest.mark.parametrize("data, size", [
    (PNG, (60, 90)), (JPEG, (600, 900)), (WEBP, (600, 900)), (webp(601, 899, "VP8L"), (601, 899)),
    (webp(600, 900, "VP8 "), (600, 900)), (png_header(23000, 23000), (23000, 23000)),
    (jpeg(600, 900)[:20] + bytes([0xFF, 0xDA, 0, 8]) + bytes(6) + jpeg(600, 900)[20:], None),   # the scan came first
    (jpeg(600, 900)[:20] + bytes([0xFF, 0xC4, 0, 5, 1, 2, 3]) + jpeg(600, 900)[20:], (600, 900)),  # a table first
    (jpeg(600, 900)[:12], None), (webp(600, 900)[:26], None), (PNG[:20], None), (b"GIF89a", None),
    (PNG[:12] + b"IDAT" + PNG[16:], None),                                   # its first chunk is no IHDR
    (webp(601, 899, "VP8L")[:20] + bytes([0x2E]) + webp(601, 899, "VP8L")[21:], None),   # no lossless signature
])
def test_an_images_size_is_read_from_its_header_and_none_when_it_cannot_be(data, size):
    assert art.image_size(data) == size


@pytest.mark.parametrize("data", [png_header(23000, 23000), png_header(4097, 10), png_header(2001, 2000),
                                  png_header(0, 900), jpeg(5000, 900), webp(600, 9000), jpeg(600, 900)[:20]])
def test_a_cover_whose_header_declares_too_large_an_image_or_none_is_no_cover(tmp_path, data):
    # A small file can declare a huge image (zeros deflate a thousand to one), which the page would decode whole.
    app = cache(tmp_path) / "3999901"
    app.mkdir()
    (app / "library_600x900.jpg").write_bytes(data)
    assert art.steam_cover(str(tmp_path), "3999901") is None
    (app / "library_600x900.jpg").write_bytes(png_header(2000, 2000))          # at the cap: taken
    assert art.steam_cover(str(tmp_path), "3999901") == (art.PNG, png_header(2000, 2000))


def test_the_portrait_in_the_apps_own_folder_is_its_cover_whatever_its_name_says(tmp_path):
    app = cache(tmp_path) / "3999901"
    app.mkdir()
    (app / "library_600x900.jpg").write_bytes(PNG)          # a .jpg that holds PNG data, as some of Steam's do
    (app / "header.jpg").write_bytes(JPEG)
    assert art.steam_cover(str(tmp_path), "3999901") == (art.PNG, PNG)


def test_the_portrait_in_a_content_hash_subfolder_and_by_the_old_flat_name(tmp_path):
    sub = cache(tmp_path) / "3999901" / "4f1c0a9e"
    sub.mkdir(parents=True)
    (sub / "library_600x900.jpg").write_bytes(JPEG)
    assert art.steam_cover(str(tmp_path), "3999901") == (art.JPEG, JPEG)
    (cache(tmp_path) / "3999902_library_600x900.jpg").write_bytes(WEBP)      # before the 2026 layout
    assert art.steam_cover(str(tmp_path), "3999902") == (art.WEBP, WEBP)


def test_only_so_many_subfolders_and_entries_of_an_apps_folder_are_looked_at(tmp_path, monkeypatch):
    app = cache(tmp_path) / "3999901"
    for name in "abcde":
        (app / name).mkdir(parents=True)
    (app / "e" / "library_600x900.jpg").write_bytes(PNG)                   # in the last subfolder, by name
    monkeypatch.setattr(art, "MAX_SUBFOLDERS", 4)
    assert art.steam_cover(str(tmp_path), "3999901") is None
    monkeypatch.setattr(art, "MAX_SUBFOLDERS", 5)
    assert art.steam_cover(str(tmp_path), "3999901") == (art.PNG, PNG)
    monkeypatch.setattr(art, "MAX_ENTRIES", 0)                              # the folder's listing not read at all
    assert art.steam_cover(str(tmp_path), "3999901") is None


def test_bytes_that_are_no_image_or_too_large_are_no_cover(tmp_path, monkeypatch):
    app = cache(tmp_path) / "3999901"
    app.mkdir()
    (app / "library_600x900.jpg").write_bytes(b"<html>not an image</html>")
    assert art.steam_cover(str(tmp_path), "3999901") is None
    (app / "library_600x900.jpg").write_bytes(PNG)
    monkeypatch.setattr(art, "MAX_IMAGE_BYTES", len(PNG) - 1)
    assert art.steam_cover(str(tmp_path), "3999901") is None
    assert art.steam_cover(str(tmp_path), "3999909") is None                 # no folder and no flat file


@pytest.mark.parametrize("appid", ["..", "0", "12a", "1/2", "", 3999901, None, "99999999999"])
def test_an_id_that_is_not_a_steam_app_id_names_no_folder(tmp_path, appid):
    (tmp_path / "library_600x900.jpg").write_bytes(PNG)                     # what ".." would reach from the cache
    (cache(tmp_path) / ".." / "library_600x900.jpg").write_bytes(PNG)
    assert art.steam_cover(str(tmp_path), appid) is None


def test_covers_are_steams_games_only_and_stop_at_their_total(tmp_path, monkeypatch):
    for appid, data in (("3999901", PNG), ("3999902", JPEG)):
        (cache(tmp_path) / appid).mkdir()
        (cache(tmp_path) / appid / "library_600x900.jpg").write_bytes(data)
    games = [RACER, LANTERN, COAST, RACER]
    assert art.covers(games, str(tmp_path)) == {("steam", "3999901"): (art.PNG, PNG),
                                               ("steam", "3999902"): (art.JPEG, JPEG)}
    monkeypatch.setattr(art, "MAX_TOTAL_BYTES", len(PNG) + len(JPEG) - 1)    # room for the first only
    assert art.covers(games, str(tmp_path)) == {("steam", "3999901"): (art.PNG, PNG)}


def test_without_steam_no_game_has_art_and_without_steam_games_steam_is_not_looked_for(monkeypatch):
    assert art.covers([LANTERN]) == {}                       # the guard fails it if Steam were looked for
    monkeypatch.setattr(steam, "find_steam_folder", lambda: None)
    assert art.covers([RACER, dataclasses.replace(COAST)]) == {}


def test_read_bytes_gives_the_file_as_it_is_and_refuses_what_read_text_refuses(tmp_path):
    marked = tmp_path / "marked.bin"
    marked.write_bytes(bytes([0xEF, 0xBB, 0xBF]) + b"Kiln")
    assert stores.read_bytes(marked, 100) == bytes([0xEF, 0xBB, 0xBF]) + b"Kiln"        # nothing taken off
    with pytest.raises(ValueError, match="is larger than 6 bytes"):
        stores.read_bytes(marked, 6)
    with pytest.raises(ValueError, match="could not be read"):
        stores.read_bytes(tmp_path / "missing.bin", 100)
