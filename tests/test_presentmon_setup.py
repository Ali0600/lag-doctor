"""lagdoctor.setup: the PresentMon pin, its Authenticode check, the guarded download, the ETW-rights how-to.

No test downloads anything: the network fetcher is replaced, and the real fetch() runs only against a
fake opener. Verification uses WinVerifyTrust with cached revocation data only.
"""
from __future__ import annotations

import _winapi
import argparse
import base64
import csv
import ctypes
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import urllib.request
from pathlib import Path

import pytest

from lagdoctor import setup

# A different, harmless program to put under PresentMon's name (it prints the computer's name).
DECOY = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "hostname.exe"


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def junction(target: Path, link: Path) -> None:
    """A folder junction at `link` (no admin needed). Remove it with os.rmdir(link): the target stays."""
    _winapi.CreateJunction(str(target), str(link))


def genuine_and_decoy(presentmon_exe: Path, root: Path) -> tuple[Path, Path, Path]:
    """root/genuine holds a copy of PresentMon, root/decoy a different exe under the same name, and the junction
    root/link points at genuine. Returns (link, genuine, decoy)."""
    genuine, decoy, link = root / "genuine", root / "decoy", root / "link"
    genuine.mkdir()
    decoy.mkdir()
    shutil.copyfile(presentmon_exe, genuine / presentmon_exe.name)
    shutil.copyfile(DECOY, decoy / presentmon_exe.name)
    junction(genuine, link)
    return link, genuine, decoy


def remove_junction(link: Path) -> None:
    if os.path.lexists(link):
        os.rmdir(link)               # the junction only, never its target's files


def run_help(exe: Path) -> bytes:
    """Run `exe --help` with exe as the program, as capture/presentmon.spawn launches; its stderr."""
    return subprocess.run(["PresentMon", "--help"], executable=str(exe), capture_output=True, timeout=30).stderr


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
                    "signer": "Intel Corporation", "note": ""}


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
    calls.clear()
    setup.authenticode(presentmon_exe, revocation=False)
    verify, _ = calls
    assert verify[1] == setup.WTD_REVOKE_NONE
    assert not verify[2] & setup.WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT
    assert verify[2] & setup.WTD_CACHE_ONLY_URL_RETRIEVAL


def fake_authenticode(results):
    """An authenticode() stand-in answering per revocation flag, recording the order it was asked in."""
    calls = []

    def fake(path, handle=None, revocation=True):
        calls.append(revocation)
        return results[revocation]
    return fake, calls


def test_unknown_revocation_offline_is_accepted_once_the_pin_matched(presentmon_exe, monkeypatch):
    fake, calls = fake_authenticode({True: (0x800B010E, None), False: (0, "Intel Corporation")})
    monkeypatch.setattr(setup, "authenticode", fake)
    info = setup.verify(presentmon_exe)
    assert calls == [True, False]
    assert info["signer"] == "Intel Corporation"
    assert "revocation status could not be checked offline" in info["note"]


def test_an_explicitly_revoked_certificate_is_still_refused(presentmon_exe, monkeypatch):
    fake, calls = fake_authenticode({True: (0x800B010C, None), False: (0, "Intel Corporation")})
    monkeypatch.setattr(setup, "authenticode", fake)
    with pytest.raises(setup.VerifyError, match="CERT_E_REVOKED"):
        setup.verify(presentmon_exe)
    assert calls == [True]


def test_the_offline_fallback_still_checks_the_signature(presentmon_exe, monkeypatch):
    fake, calls = fake_authenticode({True: (0x80092013, None), False: (0x80096010, None)})
    monkeypatch.setattr(setup, "authenticode", fake)
    with pytest.raises(setup.VerifyError, match="TRUST_E_BAD_DIGEST"):
        setup.verify(presentmon_exe)
    assert calls == [True, False]


def test_the_offline_fallback_never_runs_before_the_pin(tampered, monkeypatch):
    fake, calls = fake_authenticode({True: (0x800B010E, None), False: (0, "Intel Corporation")})
    monkeypatch.setattr(setup, "authenticode", fake)
    with pytest.raises(setup.VerifyError, match="SHA-256"):
        setup.verify(tampered)
    assert calls == []


def test_the_real_exe_also_verifies_without_a_revocation_check(presentmon_exe):
    assert setup.authenticode(presentmon_exe, revocation=False) == (0, "Intel Corporation")


def test_pinned_locks_the_file_against_writers_until_the_block_ends(presentmon_exe, tmp_path):
    copy = tmp_path / presentmon_exe.name
    other = tmp_path / "other.exe"
    shutil.copyfile(presentmon_exe, copy)
    shutil.copyfile(presentmon_exe, other)
    with setup.pinned(copy) as p:
        # the path to launch is the checked handle's own (it used to be `copy` as given; SEC-9)
        assert str(p).startswith("\\\\?\\") and os.path.samefile(p, copy)
        assert os.path.normcase(str(p)[4:]) == os.path.normcase(os.path.realpath(copy))
        with pytest.raises(PermissionError):
            open(copy, "r+b").close()
        with pytest.raises(PermissionError):
            os.replace(other, copy)
        with pytest.raises(PermissionError):
            os.rename(tmp_path, tmp_path.with_name(tmp_path.name + "-moved"))      # nor can its folder move
        assert run_help(p).startswith(b"PresentMon 2.6.0")                           # runnable by that path
    open(copy, "r+b").close()


def test_a_junction_repointed_after_the_check_cannot_change_what_the_pinned_path_runs(presentmon_exe, tmp_path):
    # SEC-9: the check went through a junction; whatever it points at afterwards, the yielded path is the file
    # that was checked.
    link, genuine, decoy = genuine_and_decoy(presentmon_exe, tmp_path)
    name = presentmon_exe.name
    try:
        with setup.pinned(link / name) as launch:
            remove_junction(link)
            junction(decoy, link)                                  # re-pointed while the pin is held
            assert sha256(link / name) == sha256(DECOY) != setup.SHA256        # by the given path: the decoy
            assert sha256(launch) == setup.SHA256                               # by the yielded path: PresentMon
            assert os.path.samefile(launch, genuine / name) and "link" not in Path(launch).parts
            assert run_help(launch).startswith(b"PresentMon 2.6.0")             # what runs is what was checked
    finally:
        remove_junction(link)


def test_the_launch_path_keeps_its_prefix_so_windows_cannot_renormalise_it(presentmon_exe, tmp_path):
    # Without \\?\ Windows drops a folder's trailing dot, so "...\x.\PresentMon.exe" would run "...\x\...".
    name = presentmon_exe.name
    dotted = "\\\\?\\" + str(tmp_path / "x.")
    plain = tmp_path / "x"
    os.mkdir(dotted)
    plain.mkdir()
    try:
        shutil.copyfile(presentmon_exe, dotted + "\\" + name)
        shutil.copyfile(DECOY, plain / name)
        with setup.pinned(Path(dotted + "\\" + name)) as launch:
            assert str(launch).endswith("\\x.\\" + name)
            assert sha256(launch) == setup.SHA256
            assert run_help(launch).startswith(b"PresentMon 2.6.0")
    finally:
        if os.path.exists(dotted + "\\" + name):
            os.remove(dotted + "\\" + name)
        os.rmdir(dotted)                                   # a plain path could not name this folder


def test_a_launch_path_longer_than_the_first_buffer_is_read_whole(presentmon_exe, tmp_path):
    deep = "\\\\?\\" + str(tmp_path) + "".join(f"\\{c * 120}" for c in "abcde")    # over 600 characters
    os.makedirs(deep)
    try:
        exe = deep + "\\" + presentmon_exe.name
        shutil.copyfile(presentmon_exe, exe)
        with setup.pinned(Path(exe)) as launch:
            assert len(str(launch)) > 600 and str(launch).endswith("e" * 120 + "\\" + presentmon_exe.name)
            assert os.path.samefile(launch, exe) and sha256(launch) == setup.SHA256
    finally:
        shutil.rmtree("\\\\?\\" + str(tmp_path / ("a" * 120)))


def test_no_launch_path_when_the_checked_files_own_path_cannot_be_read(presentmon_exe, monkeypatch):
    def unreadable(f):
        raise OSError(6, "The handle is invalid")

    monkeypatch.setattr(setup, "_final_path", unreadable)
    ran = []
    with pytest.raises(setup.VerifyError, match="own path could not be read"):
        with setup.pinned(presentmon_exe):
            ran.append(1)                                          # never reached: nothing to launch by
    assert ran == []


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

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"


def test_the_howto_joins_the_account_by_its_sid_and_says_how_to_undo_it():
    text = setup.performance_log_users_howto("PC\\Sam", sid=SID)
    assert f"Add-LocalGroupMember -SID S-1-5-32-559 -Member '{SID}'" in text
    assert f"Remove-LocalGroupMember -SID S-1-5-32-559 -Member '{SID}'" in text
    assert f"{SID} is your account, PC\\Sam" in text           # the reader sees whose number it is
    assert "sign back in" in text and "never changes group membership" in text


@pytest.mark.parametrize("bad", ["", "1001", "S-1-5", "S-1-5-21-1'; Remove-Item C:\\x", "S-1-5-21-1\n-1001"])
def test_only_a_well_formed_sid_goes_into_the_command(bad):
    text = setup.performance_log_users_howto("PC\\Sam", sid=bad)
    assert "Add-LocalGroupMember -SID S-1-5-32-559 -Member 'PC\\Sam'" in text
    assert "Remove-Item" not in text and "security ID" not in text


def test_without_this_accounts_sid_the_howto_names_the_account(monkeypatch):
    from lagdoctor.winapi import elevation

    def refuse():
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(elevation, "token_user_sid", refuse)
    assert "-Member 'PC\\Sam'" in setup.performance_log_users_howto("PC\\Sam")


@pytest.mark.parametrize("name,quoted", [("PC\\O'Brien", "'PC\\O''Brien'"),       # ' doubled, as PowerShell reads it
                                         ("PC\\Ann Lee", "'PC\\Ann Lee'"),        # the ends of printable ASCII stay
                                         ("PC\\a~b", "'PC\\a~b'")])
def test_the_howto_quotes_the_name_for_powershell(name, quoted):
    text = setup.performance_log_users_howto(name, sid="")
    assert f"Add-LocalGroupMember -SID S-1-5-32-559 -Member {quoted}" in text
    assert f"Remove-LocalGroupMember -SID S-1-5-32-559 -Member {quoted}" in text
    assert setup.NAME_PLACEHOLDER not in text


@pytest.mark.parametrize("name", ["PC\\O‘Brien", "PC\\O’Brien", "PC\\O‚Brien", "PC\\O‛Brien",
                                  "PC\\Zoë", "PC\\tab\there", "PC\\esc\x1b[2J", "PC\\del\x7f"])
def test_a_name_powershell_or_the_console_would_change_becomes_a_placeholder(name):
    # PowerShell ends a single-quoted string at the curly quotes too, and the console prints non-ASCII as '?'.
    text = setup.performance_log_users_howto(name, sid="")
    assert "Add-LocalGroupMember -SID S-1-5-32-559 -Member 'YOUR-ACCOUNT-NAME'" in text
    assert "Remove-LocalGroupMember -SID S-1-5-32-559 -Member 'YOUR-ACCOUNT-NAME'" in text
    assert "in place of YOUR-ACCOUNT-NAME" in text and "whoami shows your account name" in text
    assert name.split("\\")[1] not in text and text.isascii()


def test_the_howto_names_this_account_by_its_sid():
    # Live: whoami reads the same token's user, independently.
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30).stdout
    name, sid = next(csv.reader(io.StringIO(out)))
    text = setup.performance_log_users_howto()
    assert sid.startswith("S-1-") and f"-Member '{sid}'" in text
    assert name.lower() in text.lower()


POWERSHELL = shutil.which("powershell.exe")

# Reads each command with PowerShell's own parser; prints, per command, its parse errors, how many commands it
# holds and its elements: "-Name" for a parameter, "<quoting>:<base64 of the UTF-8 value>" for a string.
PARSE_COMMANDS = r"""
$ErrorActionPreference = 'Stop'
$texts = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@TEXTS@@')) | ConvertFrom-Json
$results = foreach ($text in $texts) {
  $tokens = $null; $errors = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
  $commands = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))
  $elements = @()
  if ($commands.Count -eq 1) { $elements = @($commands[0].CommandElements) }
  $described = @(foreach ($e in $elements) {
    if ($e -is [System.Management.Automation.Language.CommandParameterAst]) { '-' + $e.ParameterName }
    elseif ($e -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
      [string]$e.StringConstantType + ':' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($e.Value)) }
    else { $e.GetType().Name }
  })
  [pscustomobject]@{ errors = $errors.Count; commands = $commands.Count; elements = $described }
}
ConvertTo-Json -InputObject @($results) -Depth 3 -Compress
"""


def powershell_parse(texts: list[str]) -> list[tuple[int, int, list[str]]]:
    """(parse errors, commands, elements) for each text, as PowerShell 5.1's parser reads it."""
    payload = base64.b64encode(json.dumps(texts).encode("utf-8")).decode("ascii")
    script = PARSE_COMMANDS.replace("@@TEXTS@@", payload)
    ps = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                        capture_output=True, text=True, timeout=120)
    assert ps.returncode == 0, ps.stderr[-2000:]
    results = json.loads(ps.stdout)
    assert len(results) == len(texts), ps.stdout
    decoded = []
    for r in results:
        elements = []
        for e in r["elements"]:
            kind, _, value = e.partition(":")
            elements.append(e if e.startswith("-") or not value else
                            f"{kind}:{base64.b64decode(value).decode('utf-8')}")
        decoded.append((r["errors"], r["commands"], elements))
    return decoded


def one_string_member(verb: str, member: str) -> tuple[int, int, list[str]]:
    return 0, 1, [f"BareWord:{verb}", "-SID", "BareWord:S-1-5-32-559", "-Member", f"SingleQuoted:{member}"]


@pytest.mark.skipif(POWERSHELL is None, reason="powershell.exe is not on this PC")
def test_powershell_reads_every_printed_member_as_one_string():
    # Live: PowerShell's own parser reads the printed commands. The last texts check the instrument: a curly
    # quote inside the quotes, as the howto printed such a name before, must not read as one string.
    names = {"PC\\O'Brien": "PC\\O'Brien", "PC\\a b$c`d\"e;f|g(h)": "PC\\a b$c`d\"e;f|g(h)",
             "PC\\O’Brien": setup.NAME_PLACEHOLDER}
    howtos = [(setup.performance_log_users_howto("PC\\Sam", sid=SID), SID)]
    howtos += [(setup.performance_log_users_howto(name, sid=""), member) for name, member in names.items()]
    texts, expect = [], []
    for text, member in howtos:
        for verb in ("Add-LocalGroupMember", "Remove-LocalGroupMember"):
            lines = [line[line.index(verb):] for line in text.splitlines() if verb in line]
            assert len(lines) == 1, text
            texts.append(lines[0])
            expect.append(one_string_member(verb, member))
    curly = [f"PC\\O{q}Brien" for q in "‘’‚‛"]
    parsed = powershell_parse(texts + [f"Add-LocalGroupMember -SID S-1-5-32-559 -Member '{c}'" for c in curly])
    assert parsed[:len(texts)] == expect
    for name, (errors, _, elements) in zip(curly, parsed[len(texts):]):
        assert errors > 0 and f"SingleQuoted:{name}" not in elements, (name, elements)   # the string ended early


# ---------------------------------------------------------------- what setup prints ---

@pytest.fixture
def setup_output(monkeypatch, tmp_path, capsys):
    """cmd_setup with a verified PresentMon, for a given trace_rights() answer; returns what it printed."""
    from lagdoctor import cli
    from lagdoctor.winapi import elevation
    exe = tmp_path / setup.EXE_NAME
    monkeypatch.setattr(setup, "ensure_presentmon", lambda allow_download: exe)
    monkeypatch.setattr(setup, "verify", lambda path: {"signer": "Intel Corporation"})
    monkeypatch.setattr(setup, "_user_sid", lambda: SID)

    def run(rights: str, elevated: bool = False) -> str:
        monkeypatch.setattr(elevation, "trace_rights", lambda: rights)
        monkeypatch.setattr(elevation, "is_elevated", lambda: elevated)
        assert cli.cmd_setup(argparse.Namespace(download=False)) == 0
        return capsys.readouterr().out
    return run


# Measured with scripts/etw_access.py (E1): the group may trace frames but not the game-server tracker's provider.
TRACKER_NOTE = "The game-server tracker still needs one UAC prompt: Windows does not let the Performance Log Users"


def test_setup_says_ready_when_this_token_may_trace(setup_output):
    out = setup_output("now", elevated=True)                 # an administrator window: everything is traced
    assert "Frame timing: ready" in out and "Two ways" not in out and "Add-LocalGroupMember" not in out
    assert TRACKER_NOTE not in out


def test_a_member_of_the_group_is_told_the_tracker_still_needs_elevate(setup_output):
    out = setup_output("now", elevated=False)                # the right comes from Performance Log Users
    assert "Frame timing: ready" in out and TRACKER_NOTE in out and "add `--elevate` to a recording" in out


def test_setup_asks_only_for_a_sign_out_when_the_account_already_joined(setup_output):
    out = setup_output("after_sign_in")
    assert "in the Performance Log Users group, but this sign-in began before it" in out
    assert "Sign out of Windows and back in once" in out and TRACKER_NOTE in out
    assert "Two ways" not in out and "Add-LocalGroupMember" not in out


def test_setup_never_asks_for_a_sign_out_while_windows_keeps_the_group_switched_off(setup_output):
    out = setup_output("filtered")
    assert "in the Performance Log Users group, but Windows keeps that group" in out
    assert "switched off in normal windows here, so `lagdoctor watch` asks for one UAC prompt each recording" in out
    assert "Sign out" not in out and "sign-in began before" not in out
    assert "Two ways" not in out and "Add-LocalGroupMember" not in out
    assert TRACKER_NOTE not in out                           # every recording is elevated anyway


def test_setup_prints_a_placeholder_for_a_name_the_console_cannot_show(setup_output, monkeypatch):
    monkeypatch.setattr(setup, "_user_sid", lambda: None)
    monkeypatch.setattr(setup, "_user_name", lambda: "PC\\Zoë")      # say() would print it as "PC\Zo?"
    out = setup_output("no")
    assert "Add-LocalGroupMember -SID S-1-5-32-559 -Member 'YOUR-ACCOUNT-NAME'" in out
    assert "in place of YOUR-ACCOUNT-NAME" in out and "Zo?" not in out


@pytest.mark.parametrize("rights", ["no", "unknown"])
def test_setup_explains_both_ways_and_that_the_group_leaves_out_the_tracker(setup_output, rights):
    out = setup_output(rights)
    assert "Two ways" in out and "one UAC prompt each recording" in out
    assert f"Add-LocalGroupMember -SID S-1-5-32-559 -Member '{SID}'" in out
    # Measured (docs/research/install-and-rights.md, E1): the group may not enable Kernel-Network.
    assert "The game-server tracker still does: Windows does not" in out
    assert "add `--elevate` to a recording to include it" in out
    assert "Not checked yet" not in out and "sign-in began before" not in out
