"""lagdoctor.setup: the PresentMon pin, its Authenticode check, the guarded download, the ETW-rights how-to.

No test downloads anything: the network fetcher is replaced, and the real fetch() runs only against a
fake opener. Verification uses WinVerifyTrust with cached revocation data only.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import struct
import subprocess
import urllib.request
from pathlib import Path

import pytest

from lagdoctor import setup


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def tampered(presentmon_exe, tmp_path) -> Path:
    data = bytearray(presentmon_exe.read_bytes())
    data[500_000] ^= 0xFF                         # inside the code, far from the signature
    bad = tmp_path / "tampered" / presentmon_exe.name
    bad.parent.mkdir()
    bad.write_bytes(bytes(data))
    return bad


def unsigned_copy(exe: Path, dest: Path) -> Path:
    """The same PE with its Authenticode certificate table removed (security data directory zeroed)."""
    data = bytearray(exe.read_bytes())
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    optional = pe + 24
    assert struct.unpack_from("<H", data, optional)[0] == 0x20B                 # PE32+
    entry = optional + 112 + 4 * 8                                              # IMAGE_DIRECTORY_ENTRY_SECURITY
    offset, size = struct.unpack_from("<II", data, entry)
    assert offset and offset + size == len(data)
    struct.pack_into("<II", data, entry, 0, 0)
    dest.write_bytes(bytes(data[:offset]))
    return dest


# ---------------------------------------------------------------- verification ---

def test_the_pin_is_the_v2_6_0_release_asset():
    # PM header and the GitHub release record: 980,320 bytes, sha256:b2a706bc...188f1af, this URL.
    assert setup.SHA256 == "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"
    assert setup.SIZE == 980_320 and setup.SIGNER == "Intel Corporation"
    assert setup.URL == "https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe"


def test_the_real_exe_verifies(presentmon_exe):
    info = setup.verify(presentmon_exe)
    assert info == {"path": str(presentmon_exe), "sha256": sha256(presentmon_exe), "size": 980_320,
                    "signer": "Intel Corporation"}


def test_a_tampered_copy_is_refused_by_its_hash_first(tampered):
    with pytest.raises(setup.VerifyError, match="SHA-256"):
        setup.verify(tampered)


def test_a_copy_of_the_wrong_size_is_refused_before_hashing(presentmon_exe, tmp_path):
    longer = tmp_path / presentmon_exe.name
    longer.write_bytes(presentmon_exe.read_bytes() + b"\0")
    with pytest.raises(setup.VerifyError, match="980321 bytes, expected 980320"):
        setup.verify(longer)


def test_without_an_expected_size_the_read_is_still_bounded(tmp_path, monkeypatch):
    big = tmp_path / "big.exe"
    big.write_bytes(b"x" * 5000)
    monkeypatch.setattr(setup, "MAX_EXE_BYTES", 4096)
    with pytest.raises(setup.VerifyError, match="larger than 4096 bytes"):
        setup.verify(big, sha256=sha256(big), size=None)


def test_authenticode_refuses_a_changed_file_even_when_the_hash_is_its_own(tampered):
    with pytest.raises(setup.VerifyError, match="TRUST_E_BAD_DIGEST"):
        setup.verify(tampered, sha256=sha256(tampered))


def test_an_unsigned_copy_is_refused(presentmon_exe, tmp_path):
    bare = unsigned_copy(presentmon_exe, tmp_path / "unsigned.exe")
    with pytest.raises(setup.VerifyError, match="TRUST_E_NOSIGNATURE"):
        setup.verify(bare, sha256=sha256(bare), size=None)


def test_a_valid_signature_from_another_publisher_is_refused():
    py = Path(r"C:\Python313\python.exe")         # signed by the Python Software Foundation on this PC
    if not py.is_file() or setup.authenticode(py) != (0, "Python Software Foundation"):
        pytest.skip(f"{py} is not here or not signed by the PSF")
    with pytest.raises(setup.VerifyError, match="Python Software Foundation.*not 'Intel Corporation'"):
        setup.verify(py, sha256=sha256(py), size=None)


def test_revocation_is_checked_from_the_cache_only_and_the_state_is_closed(presentmon_exe, monkeypatch):
    calls = []

    def fake(hwnd, action, data):
        d = data._obj
        calls.append((d.dwStateAction, d.fdwRevocationChecks, d.dwProvFlags, d.dwUIChoice, d.dwUnionChoice))
        return ctypes.c_long(0x800B0100 - (1 << 32)).value      # TRUST_E_NOSIGNATURE as WinVerifyTrust's LONG

    monkeypatch.setattr(setup, "_WinVerifyTrust", fake)
    assert setup.authenticode(presentmon_exe) == (0x800B0100, None)
    verify, close = calls
    assert verify[0] == setup.WTD_STATEACTION_VERIFY and close[0] == setup.WTD_STATEACTION_CLOSE
    assert verify[1] == setup.WTD_REVOKE_WHOLECHAIN
    assert verify[2] & setup.WTD_CACHE_ONLY_URL_RETRIEVAL                     # never fetch a CRL or AIA
    assert verify[3] == setup.WTD_UI_NONE and verify[4] == setup.WTD_CHOICE_FILE


def test_pinned_locks_the_file_against_writers_until_the_block_ends(presentmon_exe, tmp_path):
    copy = tmp_path / presentmon_exe.name
    other = tmp_path / "other.exe"
    shutil.copyfile(presentmon_exe, copy)
    shutil.copyfile(presentmon_exe, other)
    with setup.pinned(copy) as p:
        assert p == copy
        with pytest.raises(PermissionError):
            open(copy, "r+b").close()
        with pytest.raises(PermissionError):
            os.replace(other, copy)
        r = subprocess.run([str(copy), "--help"], capture_output=True, timeout=30)     # prints help, exits 1
        assert r.stderr.startswith(b"PresentMon 2.6.0")                               # still runnable
    open(copy, "r+b").close()


def test_an_error_inside_the_pinned_block_passes_through_unchanged(presentmon_exe):
    with pytest.raises(PermissionError, match="the caller's own"):
        with setup.pinned(presentmon_exe):
            raise PermissionError("the caller's own")


def test_pinning_a_missing_file_says_not_installed(tmp_path):
    with pytest.raises(setup.NotInstalled):
        with setup.pinned(tmp_path / "missing.exe"):
            pass


# ------------------------------------------------------------------ downloading ---

@pytest.fixture
def fetcher(monkeypatch):
    calls: list = []

    def install(payload: bytes) -> list:
        def fake(url, out, max_bytes):
            calls.append((url, max_bytes))
            out.write(payload)
        monkeypatch.setattr(setup, "fetch", fake)
        return calls
    return install


def test_the_install_folder_is_inside_lag_doctors_home():
    assert setup.installed_exe().is_relative_to(Path(os.environ["LAGDOCTOR_HOME"]))   # tests never touch the real one


def test_a_download_is_verified_and_only_then_installed(presentmon_exe, fetcher):
    calls = fetcher(presentmon_exe.read_bytes())
    path = setup.ensure_presentmon(allow_download=True)
    assert path == setup.installed_exe() and sha256(path) == setup.SHA256
    assert calls == [(setup.URL, setup.SIZE)]
    assert not path.with_name(path.name + ".part").exists()


def test_a_download_that_fails_verification_is_deleted_not_kept(tampered, fetcher):
    fetcher(tampered.read_bytes())
    with pytest.raises(setup.VerifyError, match="SHA-256"):
        setup.ensure_presentmon(allow_download=True)
    exe = setup.installed_exe()
    assert not exe.exists() and not exe.with_name(exe.name + ".part").exists()


def test_without_permission_nothing_is_downloaded(fetcher):
    calls = fetcher(b"never")
    with pytest.raises(setup.NotInstalled, match="lagdoctor setup"):
        setup.ensure_presentmon(allow_download=False)
    assert calls == []


def test_a_verified_install_is_used_without_downloading(presentmon_exe, fetcher):
    exe = setup.installed_exe()
    exe.parent.mkdir(parents=True)
    shutil.copyfile(presentmon_exe, exe)
    calls = fetcher(b"never")
    assert setup.ensure_presentmon(allow_download=True) == exe and calls == []


def test_a_tampered_install_is_reported_and_replaced_only_when_allowed(presentmon_exe, tampered, fetcher):
    exe = setup.installed_exe()
    exe.parent.mkdir(parents=True)
    shutil.copyfile(tampered, exe)
    calls = fetcher(b"never")
    with pytest.raises(setup.VerifyError, match="Run `lagdoctor setup`"):
        setup.ensure_presentmon(allow_download=False)
    assert calls == []
    fetcher(presentmon_exe.read_bytes())
    assert setup.ensure_presentmon(allow_download=True) == exe and sha256(exe) == setup.SHA256


class _Response:
    def __init__(self, length: str | None, chunks: int) -> None:
        self.headers = {"Content-Length": length} if length else {}
        self._left = chunks

    def read(self, n):
        if self._left == 0:
            return b""
        self._left -= 1
        return b"x" * n

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, response):
        self.response, self.urls = response, []

    def open(self, req, timeout):
        self.urls.append(req.full_url)
        return self.response


def test_fetch_refuses_a_body_larger_than_expected(monkeypatch, tmp_path):
    for response in (_Response(None, chunks=3), _Response(str(10 ** 9), chunks=0)):
        monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers, r=response: _Opener(r))
        with open(tmp_path / "out.part", "wb") as out, pytest.raises(setup.SetupError, match="larger|more than"):
            setup.fetch(setup.URL, out, max_bytes=100_000)


def test_fetch_writes_a_body_within_the_limit(monkeypatch, tmp_path):
    opener = _Opener(_Response("131072", chunks=2))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    with open(tmp_path / "out.part", "wb") as out:
        setup.fetch(setup.URL, out, max_bytes=131_072)
    assert (tmp_path / "out.part").stat().st_size == 131_072 and opener.urls == [setup.URL]


def test_fetch_refuses_plain_http_and_redirects_away_from_https(tmp_path, monkeypatch):
    def no_network(*handlers):
        raise AssertionError("fetch went to the network for a plain-HTTP URL")
    monkeypatch.setattr(urllib.request, "build_opener", no_network)
    with open(tmp_path / "out.part", "wb") as out, pytest.raises(setup.SetupError, match="non-HTTPS"):
        setup.fetch("http://github.com/x.exe", out, max_bytes=10)
    handler = setup._HttpsOnlyRedirects()
    req = urllib.request.Request(setup.URL)
    with pytest.raises(setup.SetupError, match="away from HTTPS"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://objects.example/x.exe")
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://objects.example/x.exe") is not None


# --------------------------------------------------------------- the ETW how-to ---

def test_the_howto_joins_the_group_by_sid_and_says_how_to_undo_it():
    text = setup.performance_log_users_howto("PC\\Sam")
    assert "Add-LocalGroupMember -SID S-1-5-32-559 -Member 'PC\\Sam'" in text
    assert "Remove-LocalGroupMember -SID S-1-5-32-559 -Member 'PC\\Sam'" in text
    assert "sign back in" in text and "never changes group membership" in text


def test_the_howto_quotes_the_name_for_powershell():
    assert "-Member 'PC\\O''Brien'" in setup.performance_log_users_howto("PC\\O'Brien")


def test_the_howto_names_this_account():
    me = subprocess.run(["whoami"], capture_output=True, text=True, timeout=30).stdout.strip()
    assert me and f"-Member '{me}'".lower() in setup.performance_log_users_howto().lower()
