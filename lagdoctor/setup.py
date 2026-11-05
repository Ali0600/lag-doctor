"""Fetch and verify PresentMon 2.6.0, and explain how to get ETW rights without a prompt per run.

The exe is trusted only when all of these hold (docs/research/presentmon.md, header; the GitHub release
record for v2.6.0 lists the same size and digest):
- its size is 980,320 bytes and its SHA-256 is the pinned digest of the release asset;
- WinVerifyTrust accepts its Authenticode signature, with revocation checked against cached data only,
  so verifying never touches the network;
- the signing certificate's common name is "Intel Corporation".

`pinned()` checks the bytes through a handle that refuses writers and deletes, and keeps that handle
open while the caller launches the exe, so the file cannot be swapped between the check and the run.
That matters because the elevated capture child runs this exe from a user-writable folder.
"""
from __future__ import annotations

import ctypes
import hashlib
import msvcrt
import os
import ssl
import urllib.request
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO, Iterator

from lagdoctor import paths

VERSION = "2.6.0"
EXE_NAME = f"PresentMon-{VERSION}-x64.exe"
SHA256 = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"
SIZE = 980_320
URL = f"https://github.com/GameTechDev/PresentMon/releases/download/v{VERSION}/{EXE_NAME}"
SIGNER = "Intel Corporation"
MAX_EXE_BYTES = 64 * 1024 * 1024          # read cap when no exact size is expected


class SetupError(Exception):
    """PresentMon cannot be used; the message says why in plain English."""


class NotInstalled(SetupError):
    pass


class VerifyError(SetupError):
    pass


def installed_exe() -> Path:
    return paths.presentmon_dir(VERSION) / EXE_NAME


# ------------------------------------------------------------- ctypes, private ---

GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 0x80000000, 0x1, 3, 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# WinTrust.h / SoftPub.h / wincrypt.h (Windows SDK 10.0.19041.0)
WTD_UI_NONE = 2
WTD_REVOKE_WHOLECHAIN = 0x1
WTD_CHOICE_FILE = 1
WTD_STATEACTION_VERIFY, WTD_STATEACTION_CLOSE = 0x1, 0x2
WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT = 0x80
WTD_CACHE_ONLY_URL_RETRIEVAL = 0x1000      # "affects CRL retrieval and AIA retrieval": no network
CERT_NAME_ATTR_TYPE = 3
SZ_OID_COMMON_NAME = b"2.5.4.3"

TRUST_PROBLEMS = {                          # winerror.h values
    0x800B0100: "it is not signed (TRUST_E_NOSIGNATURE)",
    0x80096010: "its signature does not match its contents (TRUST_E_BAD_DIGEST)",
    0x800B010C: "its signing certificate was revoked (CERT_E_REVOKED)",
    0x800B010E: "the revocation status of its certificate could not be checked offline (CERT_E_REVOCATION_FAILURE)",
    0x800B0111: "its signer is explicitly distrusted on this PC (TRUST_E_EXPLICIT_DISTRUST)",
    0x800B0109: "its certificate chain ends in an untrusted root (CERT_E_UNTRUSTEDROOT)",
    0x800B010A: "its certificate chain could not be built (CERT_E_CHAINING)",
    0x800B0101: "its certificate has expired (CERT_E_EXPIRED)",
    0x800B0004: "its signature is not trusted (TRUST_E_SUBJECT_NOT_TRUSTED)",
    0x80092026: "this PC's security settings blocked the check (CRYPT_E_SECURITY_SETTINGS)",
}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


class WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR), ("hFile", wintypes.HANDLE),
                ("pgKnownSubject", ctypes.POINTER(GUID))]


class WINTRUST_DATA(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPWSTR),
                ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                ("pSignatureSettings", ctypes.c_void_p)]


class CRYPT_PROVIDER_CERT_HEAD(ctypes.Structure):
    # Only the first two fields of CRYPT_PROVIDER_CERT; the rest is never read.
    _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.c_void_p)]


# The research notes give no sizes for these; these are the x64 layouts of the SDK headers above.
assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(WINTRUST_FILE_INFO) == 32
assert ctypes.sizeof(WINTRUST_DATA) == 88
assert CRYPT_PROVIDER_CERT_HEAD.pCert.offset == 8

# {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}, SoftPub.h
WINTRUST_ACTION_GENERIC_VERIFY_V2 = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                                         (wintypes.BYTE * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_secur32 = ctypes.WinDLL("secur32", use_last_error=True)

_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
_wintrust.WinVerifyTrust.restype = wintypes.LONG
_wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
_wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
_wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.c_void_p
_wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_wintrust.WTHelperGetProvCertFromChain.restype = ctypes.c_void_p
_crypt32.CertGetNameStringW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.LPWSTR, wintypes.DWORD]
_crypt32.CertGetNameStringW.restype = wintypes.DWORD
_secur32.GetUserNameExW.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
_secur32.GetUserNameExW.restype = ctypes.c_ubyte       # BOOLEAN
NAME_SAM_COMPATIBLE = 2                                 # "DOMAIN\user", secext.h

_WinVerifyTrust = _wintrust.WinVerifyTrust              # tests replace this to inspect the flags


def _open_deny_write(path: Path) -> BinaryIO:
    """Open for reading with share mode READ only: nobody may write, rename or delete it meanwhile."""
    h = _kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, None)
    if h is None or h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
    except OSError:
        _kernel32.CloseHandle(h)
        raise
    return os.fdopen(fd, "rb")                          # closing it closes the handle


def _sha256(f: BinaryIO, limit: int) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := f.read(1 << 20):
        total += len(chunk)
        if total > limit:
            raise VerifyError(f"the file is larger than {limit} bytes")
        digest.update(chunk)
    return digest.hexdigest()


def _signer_name(state: int) -> str | None:
    """Common name of the leaf certificate WinVerifyTrust used for signer 0."""
    prov = _wintrust.WTHelperProvDataFromStateData(state)
    sgnr = _wintrust.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
    cert = _wintrust.WTHelperGetProvCertFromChain(sgnr, 0) if sgnr else None
    if not cert:
        return None
    pcert = CRYPT_PROVIDER_CERT_HEAD.from_address(cert).pCert
    if not pcert:
        return None
    buf = ctypes.create_unicode_buffer(256)
    oid = ctypes.c_char_p(SZ_OID_COMMON_NAME)
    n = _crypt32.CertGetNameStringW(pcert, CERT_NAME_ATTR_TYPE, 0, oid, buf, len(buf))
    return buf.value if n > 1 else None


def authenticode(path: Path, handle: int | None = None) -> tuple[int, str | None]:
    """(WinVerifyTrust result as an unsigned HRESULT, signer common name or None). 0 = trusted."""
    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), str(path), handle, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = WTD_UI_NONE
    data.fdwRevocationChecks = WTD_REVOKE_WHOLECHAIN
    data.dwUnionChoice = WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = WTD_STATEACTION_VERIFY
    data.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT
    action = GUID.from_buffer_copy(WINTRUST_ACTION_GENERIC_VERIFY_V2)
    no_window = wintypes.HWND(INVALID_HANDLE_VALUE)
    rc = _WinVerifyTrust(no_window, ctypes.byref(action), ctypes.byref(data)) & 0xFFFFFFFF
    try:
        signer = _signer_name(data.hWVTStateData) if rc == 0 and data.hWVTStateData else None
    finally:
        data.dwStateAction = WTD_STATEACTION_CLOSE      # frees the state WinVerifyTrust kept
        _WinVerifyTrust(no_window, ctypes.byref(action), ctypes.byref(data))
    return rc, signer


# ------------------------------------------------------------------ public ---

@contextmanager
def _pinned_file(path: Path, sha256: str, size: int | None) -> Iterator[BinaryIO]:
    try:
        f = _open_deny_write(path)
    except FileNotFoundError as exc:
        raise NotInstalled(f"{path} does not exist") from exc
    except OSError as exc:
        raise VerifyError(f"{path} could not be opened for checking (in use or locked: {exc})") from exc
    with f:
        actual_size = os.fstat(f.fileno()).st_size
        if size is not None and actual_size != size:
            raise VerifyError(f"{path} is {actual_size} bytes, expected {size}")
        digest = _sha256(f, size if size is not None else MAX_EXE_BYTES)
        if digest != sha256.lower():
            raise VerifyError(f"{path} has SHA-256 {digest}, expected the pinned {sha256.lower()}")
        yield f


@contextmanager
def pinned(path: Path, sha256: str = SHA256, size: int | None = SIZE) -> Iterator[Path]:
    """Check the file's bytes against the pin and keep it locked against writers until the block ends.

    Launch the exe inside the block; once the process exists Windows itself refuses writes to it.
    Errors raised by the caller's block pass through unchanged.
    """
    path = Path(path)
    with _pinned_file(path, sha256, size):
        yield path


def verify(path: Path, *, sha256: str = SHA256, size: int | None = SIZE, signer: str = SIGNER) -> dict:
    """Raise VerifyError unless the file is the pinned, validly signed Intel PresentMon (NotInstalled if it
    is missing). Never runs it."""
    path = Path(path)
    with _pinned_file(path, sha256, size) as f:
        f.seek(0)
        rc, name = authenticode(path, msvcrt.get_osfhandle(f.fileno()))
        actual_size = os.fstat(f.fileno()).st_size
    if rc != 0:
        why = TRUST_PROBLEMS.get(rc, f"WinVerifyTrust returned 0x{rc:08X}")
        raise VerifyError(f"{path} failed its Authenticode check: {why}")
    if name != signer:
        raise VerifyError(f"{path} is signed by {name or 'an unnamed certificate'!r}, not {signer!r}")
    return {"path": str(path), "sha256": sha256.lower(), "size": actual_size, "signer": name}


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise SetupError(f"refusing a redirect away from HTTPS: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, out: BinaryIO, max_bytes: int) -> None:
    """Download url into out over HTTPS only, refusing anything larger than max_bytes."""
    if not url.lower().startswith("https://"):
        raise SetupError(f"refusing a non-HTTPS download: {url}")
    opener = urllib.request.build_opener(_HttpsOnlyRedirects,
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, headers={"User-Agent": "lag-doctor-setup"})
    with opener.open(req, timeout=30) as resp:
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise SetupError(f"the download is {length} bytes, more than the expected {max_bytes}")
        total = 0
        while chunk := resp.read(1 << 16):
            total += len(chunk)
            if total > max_bytes:
                raise SetupError(f"the download is larger than the expected {max_bytes} bytes")
            out.write(chunk)


def ensure_presentmon(allow_download: bool) -> Path:
    """The verified PresentMon exe, downloading it first when allowed. Raises SetupError with the reason.

    A download lands in a .part file, is verified, and only then replaces the exe; a file that fails
    verification is deleted, never kept or run.
    """
    path = installed_exe()
    problem: VerifyError | None = None
    if path.is_file():
        try:
            verify(path)
            return path
        except VerifyError as exc:
            problem = exc
    if not allow_download:
        if problem is not None:
            raise VerifyError(f"{problem}. Run `lagdoctor setup` to replace it.") from problem
        raise NotInstalled(f"PresentMon {VERSION} is not installed ({path}). Run `lagdoctor setup` to download it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with open(part, "wb") as out:
            fetch(URL, out, SIZE)
        verify(part)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)
    return path


def _user_name() -> str:
    size = wintypes.ULONG(512)
    buf = ctypes.create_unicode_buffer(size.value)
    if _secur32.GetUserNameExW(NAME_SAM_COMPATIBLE, buf, ctypes.byref(size)) and buf.value:
        return buf.value
    return f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ.get('USERNAME', 'YOUR-USER-NAME')}"


def performance_log_users_howto(user: str | None = None) -> str:
    """Plain-English steps for joining Performance Log Users once. Text only: nothing is changed here."""
    who = user or _user_name()
    quoted = "'" + who.replace("'", "''") + "'"            # a PowerShell single-quoted literal
    return (
        "Frame timing needs the right to start an event trace. Instead of approving an administrator\n"
        "prompt for every capture, you can add your account to the built-in \"Performance Log Users\"\n"
        "group once. An administrator has to do it, and it takes effect after you sign in again:\n"
        "\n"
        "  1. Open Windows PowerShell as administrator (Start, type PowerShell, choose Run as administrator).\n"
        f"  2. Run:  Add-LocalGroupMember -SID S-1-5-32-559 -Member {quoted}\n"
        "  3. Sign out of Windows and sign back in.\n"
        "\n"
        f"To undo it later:  Remove-LocalGroupMember -SID S-1-5-32-559 -Member {quoted}\n"
        "\n"
        "Members of this group can record system-wide performance traces (activity of every process on\n"
        "this PC), so only do this on your own computer. lag-doctor never changes group membership itself.\n"
    )
