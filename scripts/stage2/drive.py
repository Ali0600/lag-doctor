"""Stage 2's driver: builds the service layout and runs E1, E3c, E3 and E3b on a GitHub-hosted runner.

    uv run python scripts/stage2/drive.py facts|layout|e1|e3c|e3|e3b|summary

.github/workflows/experiments.yml runs them in that order; each reads what the ones before it wrote. Every command
but `summary` creates or changes something (a folder under Program Files, a results folder, services), so it
refuses to run anywhere but a GitHub-hosted runner (guard.py) before its first change. Results land in RESULTS as
JSON: one file per command, plus what the services and the stand-in wrote; `summary` turns them into the job
summary. The experiments are defined in docs/research/install-and-rights.md (section 6).

Exit codes: 0 the command measured what it set out to, whatever the answer; 1 the instrument failed (the layout
could not be built, a service wrote no result, the stand-in never presented a frame); 2 refused (not a runner).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
for _p in (str(SCRIPTS), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runtime_pin import EMBED_PTH, EMBED_SHA256, EMBED_SIZE, EMBED_URL, EMBED_VERSION  # noqa: E402
from stage2 import guard, pipes, scm, tokeninfo  # noqa: E402

INSTALL = Path(r"C:\Program Files\lagdoctor-stage2")      # where the MSI would put lag-doctor (Stage 3)
RESULTS = Path(r"C:\lagdoctor-stage2-results")             # uploaded by the workflow
LOCAL_SERVICE_SID = "S-1-5-19"
SE_CHANGE_NOTIFY = "SeChangeNotifyPrivilege"
GAME_SECONDS = 45            # how long the stand-in draws
CAPTURE_SECONDS = 120        # the service's own deadline; the capture should end when the stand-in exits
IDLE_SECONDS = 10            # E3b: the service stops itself after this long without a client
E3B_CYCLES = 3               # start, idle stop, start, idle stop, start, idle stop
ERROR_WMI_INSTANCE_NOT_FOUND = 4201
# E1 on the owner's laptop (build 26200, docs/research/install-and-rights.md section 1). The default descriptor's
# last entry names a capability SID, compared here by its shape only.
LAPTOP_OWN = "O:BAG:BAD:(A;;0x120fff;;;SY)(A;;LO;;;LS)(A;;0x1207ff;;;BA)"
LAPTOP_DEFAULT = ("O:BAG:BAD:(A;;0x1800;;;WD)(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)(A;;0x120fff;;;NS)"
                  "(A;;0x120fff;;;BA)(A;;0xee5;;;LU)(A;;LC;;;MU)(A;;0x1800;;;AC)(A;;0x1800;;;<capability>)")
CAPABILITY_SID = re.compile(r"S-1-15-3-[0-9-]+")
MEASURED, INSTRUMENT_FAILED = 0, 1


# ------------------------------------------------------------------------------------------ helpers ---

def _write_json(path: Path, data: object) -> None:
    text = json.dumps(data, indent=1, default=str)
    with open(path, "x", encoding="utf-8") as f:               # a new file only
        f.write(text)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _wait_json(path: Path, done, timeout: float) -> dict | None:
    """Poll a JSON file someone else writes until done(data) or timeout seconds; the last data read."""
    deadline, data = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        data = _read_json(path)
        if data is not None and done(data):
            return data
        time.sleep(0.1)
    return data


def inside(entry: str, root: Path) -> bool:
    """Whether a sys.path entry names root or something under it ("" is the current folder: never inside)."""
    if not entry:
        return False
    try:
        p, r = os.path.normcase(os.path.abspath(entry)), os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:                                         # another drive
        return False


def masked(sddl: str | None) -> str | None:
    return None if sddl is None else CAPABILITY_SID.sub("<capability>", sddl)


def create_service(name: str, mode: str, sid_type: str, command_args: tuple[str, ...] = ()) -> dict:
    """A demand-start LocalService service running service_host.py in `mode`, its privileges trimmed to
    SeChangeNotifyPrivilege and the given service SID type, allowed to write RESULTS; sc.exe's read-back of it."""
    bin_path = subprocess.list2cmdline([str(INSTALL / "python.exe"), str(INSTALL / "stage2" / "service_host.py"),
                                        mode, *command_args])
    scm.sc("create", name, "type=", "own", "start=", "demand", "error=", "normal", "binPath=", bin_path,
           "obj=", r"NT AUTHORITY\LocalService", "DisplayName=", f"lag-doctor Stage 2 experiment {mode}")
    scm.sc("privs", name, SE_CHANGE_NOTIFY)
    scm.sc("sidtype", name, sid_type)
    sid = scm.service_sid(name)
    showsid = scm.sc("showsid", name)
    grant = subprocess.run(["icacls", str(RESULTS), "/grant", f"*{sid}:(OI)(CI)M"], capture_output=True, text=True)
    if grant.returncode != 0:
        raise RuntimeError(f"icacls could not give {name} access to {RESULTS}: {grant.stdout}{grant.stderr}")
    return {"name": name, "sid_type": sid_type, "bin_path": bin_path, "sid": sid, "sid_matches_showsid": sid in showsid,
            "qc": scm.sc("qc", name), "qprivs": scm.sc("qprivs", name), "qsidtype": scm.sc("qsidtype", name),
            "showsid": showsid}


def start_service(name: str, args: list[str]) -> str | None:
    """StartServiceW; the error as text when the SCM refused or the service failed to start, else None."""
    try:
        scm.start(name, args)
    except OSError as exc:
        return str(exc)
    return None


def delete_service(name: str) -> str:
    try:
        if scm.status(name)["state"] != "stopped":
            scm.sc("stop", name, check=False)
            scm.wait_for(name, {"stopped"}, 30)
    except OSError:
        pass
    return scm.sc("delete", name, check=False)


# ----------------------------------------------------------------------------------------- commands ---

def cmd_facts() -> int:
    """The runner as the experiments find it: Windows build and edition, UAC, this account and its session."""
    import winreg
    from lagdoctor.winapi import elevation, ntquery

    def values(path: str, names: tuple[str, ...]) -> dict:
        out = {}
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                for n in names:
                    try:
                        out[n] = winreg.QueryValueEx(key, n)[0]
                    except OSError:
                        out[n] = None
        except OSError as exc:
            out["error"] = str(exc)
        return out

    token = tokeninfo.snapshot()
    procs = ntquery.Snapshotter().snapshot()
    data = {"windows": values(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                              ("ProductName", "EditionID", "InstallationType", "DisplayVersion", "CurrentBuild", "UBR")),
            "uac": values(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                          ("EnableLUA", "ConsentPromptBehaviorAdmin", "FilterAdministratorToken")),
            "elevated": elevation.is_elevated(), "user": token["user"], "session_id": token["session_id"],
            "integrity": token["integrity"],
            "dwm_sessions": sorted({p.session_id for p in procs.values() if p.name.lower() == "dwm.exe"}),
            "image": {k: os.environ.get(k) for k in ("ImageOS", "ImageVersion")}}
    _write_json(RESULTS / "facts.json", data)
    print(json.dumps(data, indent=1))
    return MEASURED


TRUSTED = "0x00000000"


def _signatures(folder: Path, names: list[str]) -> tuple[list[dict], dict]:
    """Each file's Authenticode state two ways: lag-doctor's own WinVerifyTrust check (setup.authenticode, cached
    revocation data only), which decides, and PowerShell's Get-AuthenticodeSignature as an independent second
    reading. (rows, readout): the readout says whether PowerShell's answer could be read at all."""
    from lagdoctor import setup
    script = ("Get-ChildItem -LiteralPath $env:STAGE2_DIR -File | ForEach-Object { try { "
              "$s = Get-AuthenticodeSignature -LiteralPath $_.FullName -ErrorAction Stop; [pscustomobject]@{ "
              "name = $_.Name; status = [string]$s.Status; type = [string]$s.SignatureType; "
              "signer = $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { $null }) } } catch { "
              "[pscustomobject]@{ name = $_.TargetObject; status = 'error'; type = ''; signer = $_.Exception.Message } } } "
              "| ConvertTo-Json -Compress")
    # PowerShell 7 where the runner has it; Windows PowerShell 5.1 otherwise, without the module path a PowerShell 7
    # job shell hands down (5.1 then cannot load its own Microsoft.PowerShell.Security: measured on the first run).
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    shell = shutil.which("pwsh.exe") or "powershell.exe"
    ps = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                        text=True, timeout=120, env={**env, "STAGE2_DIR": str(folder)})
    by_name, parse_error = {}, None
    try:
        listed = json.loads(ps.stdout)
        by_name = {r["name"]: r for r in (listed if isinstance(listed, list) else [listed])}
    except (ValueError, TypeError, KeyError) as exc:
        parse_error = f"{type(exc).__name__}: {exc}"
    readout = {"shell": shell, "returncode": ps.returncode, "parsed": len(by_name), "parse_error": parse_error,
               "stdout_head": ps.stdout[:300], "stderr_tail": ps.stderr[-500:]}
    rows = []
    for n in names:
        code, signer = setup.authenticode(folder / n)
        ps_row = by_name.get(n, {})
        rows.append({"name": n, "winverifytrust": f"0x{code:08X}", "signer_cn": signer, "ps_status": ps_row.get("status"),
                     "ps_type": ps_row.get("type"), "ps_signer": ps_row.get("signer")})
    return rows, readout


def signature_verdict(rows: list[dict], readout: dict | None) -> dict:
    """E3c's signature answer from the rows: which binaries WinVerifyTrust refused, who signed the rest, and whether
    PowerShell's independent reading agrees (or could not be read, which is said, never counted as unsigned)."""
    binaries = [r for r in rows if not r["name"].lower().endswith(".cat")]
    unsigned = [r["name"] for r in binaries if r["winverifytrust"] != TRUSTED]
    signers = sorted({r["signer_cn"] for r in binaries if r["winverifytrust"] == TRUSTED and r["signer_cn"]})
    if not readout or not readout.get("parsed"):
        cross = "PowerShell's reading could not be read: " + str((readout or {}).get("parse_error") or
                                                                 (readout or {}).get("stderr_tail") or "no output")
    else:
        disagree = sorted(r["name"] for r in binaries if (r["ps_status"] == "Valid") != (r["winverifytrust"] == TRUSTED))
        cross = "PowerShell agrees on every binary" if not disagree else f"PowerShell disagrees on {disagree}"
    return {"binaries": len(binaries), "unsigned": unsigned, "signers": signers, "cross_check": cross}


def _catalog_check(folder: Path, names: list[str]) -> dict:
    """For files with no embedded signature: does python.org's python.cat vouch for them (signtool verify /c)?"""
    tools = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    if not tools or not (folder / "python.cat").is_file():
        return {"checked": False, "why": "no signtool.exe" if not tools else "no python.cat"}
    out = {"checked": True, "signtool": tools[-1], "files": {}}
    for n in names:
        p = subprocess.run([tools[-1], "verify", "/pa", "/c", str(folder / "python.cat"), str(folder / n)],
                           capture_output=True, text=True, timeout=60)
        out["files"][n] = {"exit": p.returncode, "output": (p.stdout + p.stderr).strip()[-400:]}
    return out


def cmd_layout() -> int:
    """The folder the MSI would install (Stage 3): python.org's embeddable runtime, lag-doctor's code and the pinned
    PresentMon, under Program Files; plus E3c's signature census of the runtime's own files. lag-doctor's code needs
    nothing beyond the standard library (tests/test_stdlib_only.py), so no other package goes in."""
    from lagdoctor import setup
    if INSTALL.exists():
        print(f"{INSTALL} already exists: this command builds it once, on a fresh runner.")
        return INSTRUMENT_FAILED
    work = Path(tempfile.mkdtemp(prefix="lagdoctor-stage2-"))
    zip_path = work / f"python-{EMBED_VERSION}-embed-amd64.zip"
    with open(zip_path, "xb") as f:
        setup.fetch(EMBED_URL, f, EMBED_SIZE)
    data = zip_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != EMBED_SIZE or digest != EMBED_SHA256:
        print(f"The embeddable ZIP is {len(data)} bytes with SHA-256 {digest}; expected {EMBED_SIZE} and {EMBED_SHA256}.")
        return INSTRUMENT_FAILED
    INSTALL.mkdir()
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        bad = [n for n in names if not re.fullmatch(r"[A-Za-z0-9_.+-]+", n) or n in (".", "..")]
        if bad:
            print(f"The ZIP holds names that are not plain file names: {bad}")
            return INSTRUMENT_FAILED
        for n in names:
            with z.open(n) as src, open(INSTALL / n, "xb") as dst:
                shutil.copyfileobj(src, dst)
    binaries = sorted(n for n in names if n.lower().endswith((".exe", ".dll", ".pyd")))
    signatures, readout = _signatures(INSTALL, binaries + ["python.cat"])
    unsigned = signature_verdict(signatures, readout)["unsigned"]
    catalog = _catalog_check(INSTALL, unsigned) if unsigned else {"checked": False, "why": "every binary is signed"}
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "lagdoctor", INSTALL / "lagdoctor", ignore=skip)
    shutil.copytree(SCRIPTS / "stage2", INSTALL / "stage2", ignore=skip)
    (INSTALL / "scripts").mkdir()
    shutil.copy2(SCRIPTS / "etw_access.py", INSTALL / "scripts" / "etw_access.py")
    os.environ["LAGDOCTOR_HOME"] = str(work / "home")         # setup's download folder, away from the profile
    exe = setup.ensure_presentmon(allow_download=True)
    shutil.copy2(exe, INSTALL / setup.EXE_NAME)
    presentmon = setup.verify(INSTALL / setup.EXE_NAME)
    record = {"install": str(INSTALL), "embed": {"url": EMBED_URL, "sha256": digest, "size": len(data)},
              "files": sorted(names), "pth": (INSTALL / EMBED_PTH).read_text(),
              "signatures": signatures, "signature_readout": readout, "catalog": catalog, "presentmon": presentmon}
    RESULTS.mkdir(exist_ok=True)
    _write_json(RESULTS / "layout.json", record)
    print(f"Built {INSTALL}: {len(names)} runtime files, {len(binaries) - len(unsigned)} of {len(binaries)} binaries "
          f"with a valid signature; PresentMon verified.")
    return MEASURED


def cmd_e1() -> int:
    """scripts/etw_access.py's reading on this runner, judged by its own models, and set beside the laptop's."""
    import etw_access
    data = etw_access.read_all()
    _write_json(RESULTS / "e1-descriptors.json", data)          # also what the E3 service judges with its own token
    if not isinstance(data.get("providers"), dict):
        return INSTRUMENT_FAILED
    judged = etw_access.add_verdicts(data)
    rows = judged["providers"]
    same = {n: rows.get(n, {}).get("sddl") == LAPTOP_OWN
            for n in ("Microsoft-Windows-Kernel-Network", "Microsoft-Windows-Kernel-Process")}
    same["default"] = masked(rows.get(etw_access.DEFAULT_ROW, {}).get("sddl")) == LAPTOP_DEFAULT
    text = etw_access.summary(judged)
    _write_json(RESULTS / "e1.json", {
        "build": data.get("build"), "elevated": data.get("elevated"), "same_as_laptop": same,
        "own_descriptor": sorted(n for n, r in rows.items() if r.get("own_descriptor")),
        "local_service_model": etw_access.outcome(judged, etw_access.LOCAL_SERVICE),
        "you_note": "the You+group column is this runner's administrator account (UAC is off), so it says nothing "
                    "about a Performance Log Users member", "summary": text, "judged": judged})
    print(text)
    return MEASURED


def cmd_e3c() -> int:
    """E3c: the embeddable runtime as an SCM service from Program Files: where its sys.path points."""
    name = "lagdoctor-e3c"
    record: dict = {"service": create_service(name, "e3c", "restricted")}
    result = RESULTS / "e3c-service.json"
    try:
        record["start_error"] = start_service(name, [str(result)])
        record["status"] = scm.wait_for(name, {"stopped"}, 60)
    finally:
        record["delete"] = delete_service(name)
    data = record["result"] = _read_json(result)
    layout = _read_json(RESULTS / "layout.json") or {}
    record["signatures"], record["catalog"] = layout.get("signatures"), layout.get("catalog")
    record["signature_readout"] = layout.get("signature_readout")
    if data is None:
        record["verdict"] = None
        _write_json(RESULTS / "e3c.json", record)
        print(f"The service wrote no result; its status: {record['status']}")
        return INSTRUMENT_FAILED
    entries = data.get("runtime", {}).get("sys_path_at_start") or []
    record["verdict"] = {
        "ran_as_service": True,
        "user": (data.get("token") or {}).get("user"),
        "sys_path": entries,
        "all_inside_install": bool(entries) and all(inside(e, INSTALL) for e in entries),
        "outside_install": [e for e in entries if not inside(e, INSTALL)],
        "script_folder_on_path": any(inside(e, INSTALL / "stage2") for e in entries),
        "site_imported": data.get("runtime", {}).get("site_imported_at_start"),
        "flags": data.get("runtime", {}).get("flags"),
        "signatures": signature_verdict(record["signatures"] or [], record["signature_readout"]),
    }
    _write_json(RESULTS / "e3c.json", record)
    print(json.dumps(record["verdict"], indent=1))
    return MEASURED


def _judge_e3(rec: dict, name: str) -> dict:
    res = rec["result"]
    token = res.get("token") or {}
    providers = res.get("providers") if isinstance(res.get("providers"), dict) else {}
    authz = res.get("authz") if isinstance(res.get("authz"), dict) else {}
    cap = res.get("capture") or {}
    frames, flows = cap.get("frames") or {}, cap.get("net_flows") or {}
    pm, health = frames.get("presentmon") or {}, flows.get("health") or {}
    standin = rec.get("standin") or {}
    game_pid = str(res.get("game_pid") or standin.get("pid"))
    probe_rows = {n: r for n, r in providers.items() if isinstance(r, dict) and "enabled" in r}

    def presents_summary(key: str) -> dict:
        """PresentMon on every process (one probe): what this token saw at all, and for the game's PID."""
        everyone = res.get(key) if isinstance(res.get(key), dict) else {}
        per_pid = everyone.get("rows_per_pid") or {}
        return {"exit_code": everyone.get("exit_code"), "rows": everyone.get("rows"), "pids": len(per_pid),
                "game_rows": per_pid.get(game_pid, {}).get("rows", 0), "first_rows": everyone.get("first_rows"),
                "stderr_tail": everyone.get("stderr_tail"), "error": everyone.get("error")}
    # Windows' own check with this token (authz, E1's method) against what enabling really did.
    disagree = sorted(n for n, r in probe_rows.items()
                      if isinstance(authz.get(n), dict) and authz[n].get("enable") is not r["enabled"])
    ended_after = None
    if cap.get("ended_by_itself") and isinstance(standin.get("ended_qpc_s"), (int, float)):
        ended_after = round(cap["ended_qpc_s"] - standin["ended_qpc_s"], 2)
    return {
        "user_is_local_service": (token.get("user") or {}).get("sid") == LOCAL_SERVICE_SID,
        "privileges": sorted(p["name"] for p in token.get("privileges", [])),
        "restricted_token": token.get("restricted"),
        "service_sid_in_restricted_list": scm.service_sid(name) in [r["sid"] for r in token.get("restricted_sids", [])],
        "providers_enabled": sorted(n for n, r in probe_rows.items() if r["enabled"]),
        "providers_refused": {n: r.get("error") for n, r in probe_rows.items() if not r["enabled"]},
        "authz_disagrees_on": disagree,
        "standin": {k: standin.get(k) for k in ("pid", "session_id", "presents", "swap_effect", "adapter")},
        # PresentMon on every process, before the targeted capture: what this token sees at all, with frames
        # tracked to the display (lag-doctor's flags) and without
        "all_presents": presents_summary("all_presents"),
        "all_presents_no_display": presents_summary("all_presents_no_display"),
        "frames_status": frames.get("status"), "frame_rows": pm.get("rows"), "presentmon_exit": pm.get("exit_code"),
        "stop_path": pm.get("stop_path"), "stderr_tail": pm.get("stderr_tail"),
        "capture_ended_by_itself": cap.get("ended_by_itself"), "ended_s_after_game_exit": ended_after,
        "tracker_status": flows.get("status"), "tracker_events": health.get("events"),
        "tracker_selftest": (health.get("selftest") or {}).get("verified"), "game_udp_events": health.get("game_events"),
        "leftover_sessions": sorted(s for s, rc in rec.get("leftover_sessions", {}).items()
                                    if rc != ERROR_WMI_INSTANCE_NOT_FOUND),
        "parts_that_crashed": sorted(k for k in ("token", "providers", "authz", "capture")
                                     if isinstance(res.get(k), dict) and "error" in res[k]),
    }


def _e3_config(sid_type: str) -> dict:
    import etw_access
    from lagdoctor.winapi import etw
    name = f"lagdoctor-e3-{sid_type}"
    rec: dict = {"service": create_service(name, "e3", sid_type)}
    status_file, result = RESULTS / f"standin-{sid_type}.json", RESULTS / f"e3-{sid_type}-service.json"
    game = subprocess.Popen([sys.executable, str(SCRIPTS / "stage2" / "standin.py"), "--seconds", str(GAME_SECONDS),
                             "--status", str(status_file)])
    try:
        first = _wait_json(status_file, lambda d: d.get("ready") or "ended_qpc_s" in d, 60)
        if not first or not first.get("ready"):
            rec.update(measured=False, why="the stand-in never presented a frame", standin=first)
            return rec
        rec["start_error"] = start_service(name, [str(result), str(first["pid"]), str(CAPTURE_SECONDS),
                                                  str(RESULTS / "e1-descriptors.json")])
        rec["started"] = scm.status(name)
        game.wait(GAME_SECONDS + 60)
        rec["status"] = scm.wait_for(name, {"stopped"}, CAPTURE_SECONDS + 60)
    finally:
        if game.poll() is None:
            game.kill()
        sessions = ["lagdoctor", "lag-doctor-net", "lagdoctor-e3-all", "lagdoctor-e3-all2",
                    *(f"lagdoctor-e3-probe-{i}" for i in range(len(etw_access.PROVIDERS)))]
        rec["leftover_sessions"] = {s: etw.query_session(s) for s in sessions}
        rec["delete"] = delete_service(name)
    rec["standin"] = _read_json(status_file)
    rec["result"] = _read_json(result)
    rec["measured"] = rec["result"] is not None
    if not rec["measured"]:
        rec["why"] = "the service wrote no result"
    else:
        rec["verdict"] = _judge_e3(rec, name)
    return rec


def _e3_control() -> dict:
    """The same capture run by this job's own account (an administrator, in the desktop session, PresentMon able
    to open the stand-in): what a GPU-less runner's WARP stand-in gives PresentMon at all, apart from any
    service question. A service result is read against this."""
    import threading
    from lagdoctor import setup
    from lagdoctor.winapi import etw
    from stage2 import service_host
    exe = INSTALL / setup.EXE_NAME
    status_file = RESULTS / "standin-control.json"
    rec: dict = {"service": None}
    game = subprocess.Popen([sys.executable, str(SCRIPTS / "stage2" / "standin.py"), "--seconds", str(GAME_SECONDS),
                             "--status", str(status_file)])
    try:
        first = _wait_json(status_file, lambda d: d.get("ready") or "ended_qpc_s" in d, 60)
        if not first or not first.get("ready"):
            rec.update(measured=False, why="the stand-in never presented a frame", standin=first)
            return rec
        rec["result"] = {"game_pid": first["pid"], "token": service_host._part(tokeninfo.snapshot),
                         "all_presents": service_host._part(service_host.all_presents, service_host.ALL_PRESENTS_S, exe),
                         "all_presents_no_display": service_host._part(service_host.all_presents,
                                                                       service_host.ALL_PRESENTS_S, exe,
                                                                       "lagdoctor-e3-all2", False),
                         "capture": service_host._part(service_host.capture, first["pid"], CAPTURE_SECONDS,
                                                       threading.Event(), exe)}
        game.wait(GAME_SECONDS + 60)
    finally:
        if game.poll() is None:
            game.kill()
        rec["leftover_sessions"] = {s: etw.query_session(s) for s in ("lagdoctor", "lag-doctor-net", "lagdoctor-e3-all",
                                                                       "lagdoctor-e3-all2")}
    rec["standin"] = _read_json(status_file)
    rec["measured"] = True
    rec["verdict"] = _judge_e3(rec, "control")
    return rec


def cmd_e3() -> int:
    """E3: lag-doctor's frame timing and tracker in a LocalService service with trimmed privileges, once with an
    unrestricted service SID and once with a restricted one (which tells whether the restricted SID is the cause
    of anything refused), read against a control run by the job's own administrator account."""
    configs = {"control_admin": _e3_control()}
    configs.update({t: _e3_config(t) for t in ("unrestricted", "restricted")})
    _write_json(RESULTS / "e3.json", configs)
    for t, rec in configs.items():
        print(f"--- {t}:", json.dumps(rec.get("verdict") or {"why": rec.get("why")}, indent=1))
    return MEASURED if all(rec.get("measured") for rec in configs.values()) else INSTRUMENT_FAILED


def cmd_e3b() -> int:
    """E3b: the named-pipe trigger starts the service, it stops itself when idle, and the next connect starts it."""
    name = pipe = "lagdoctor-e3b"
    folder = RESULTS / "e3b"
    rec: dict = {"service": create_service(name, "e3b", "restricted", (str(folder), pipe, str(IDLE_SECONDS)))}
    folder.mkdir()                                             # after the grant on RESULTS, so it inherits it
    path = f"\\\\.\\pipe\\{pipe}"
    cycles: list[dict] = []
    try:
        rec["service"]["triggerinfo"] = scm.sc("triggerinfo", name, f"start/namedpipe/{pipe}")
        rec["service"]["qtriggerinfo"] = scm.sc("qtriggerinfo", name)
        for i in range(E3B_CYCLES):
            cycle: dict = {"state_before": scm.status(name)["state"]}
            try:
                got = pipes.request(path, f"cycle {i + 1}".encode(), timeout_s=60)
            except TimeoutError as exc:
                cycle.update(connected=False, **getattr(exc, "details", {}))
                cycles.append(cycle)
                break
            cycle.update(connected=True, reply=json.loads(got.pop("reply")), connect=got,
                         state_after=scm.status(name))
            cycle["stop"] = scm.wait_for(name, {"stopped"}, IDLE_SECONDS + 60)
            cycles.append(cycle)
            if cycle["stop"]["timed_out"]:
                break
        if not any(c.get("connected") for c in cycles):
            # A trigger that never fired, or a service that cannot serve? Start it by hand once to tell.
            rec["manual_start_error"] = start_service(name, [])
            try:
                got = pipes.request(path, b"manual start", timeout_s=30)
                rec["manual_start"] = {"connected": True, "reply": json.loads(got.pop("reply")), **got}
            except TimeoutError as exc:
                rec["manual_start"] = {"connected": False, **getattr(exc, "details", {})}
    finally:
        rec["cycles"] = cycles
        rec["delete"] = delete_service(name)
    starts = {s.get("pid"): s for s in (_read_json(p) for p in sorted(folder.glob("e3b-*.json"))) if s}
    rec["starts"] = list(starts.values())
    ok = [c for c in cycles if c.get("connected")]
    pids = [c["reply"]["pid"] for c in ok]

    def span(p: int, a: str, b: str) -> float | None:
        s = starts.get(p, {})
        return round(s[b] - s[a], 4) if isinstance(s.get(a), float) and isinstance(s.get(b), float) else None

    rec["verdict"] = {
        "cycles_connected": f"{len(ok)} of {E3B_CYCLES}",
        "started_by_trigger": all(c["state_before"] == "stopped" for c in ok) and bool(ok),
        "distinct_pids": len(set(pids)) == len(pids),
        "start_latency_s": [c["connect"]["connect_s"] for c in ok],
        "connect_attempts": [c["connect"]["attempts"] for c in ok],
        "first_error_seen_by_client": [c["connect"]["first_error"] for c in ok],
        "service_start_to_pipe_s": [span(p, "started_qpc_s", "pipe_ready_qpc_s") for p in pids],
        "idle_to_stop_s": [span(p, "pipe_ready_qpc_s", "stopped_qpc_s") for p in pids],
        "stopped_by": [starts.get(p, {}).get("stopped_by") for p in pids],
        "stop_wait_s": [c["stop"]["waited_s"] for c in ok],
        "servicemain_args": [starts.get(p, {}).get("servicemain_args") for p in pids],
        "service_exit_codes": [c["stop"]["service_exit_code"] for c in ok],
    }
    _write_json(RESULTS / "e3b.json", rec)
    print(json.dumps(rec["verdict"], indent=1))
    return MEASURED if ok or rec.get("manual_start", {}).get("connected") else INSTRUMENT_FAILED


def cmd_summary() -> int:
    """A readable digest of every result file, for the job summary (and the log)."""
    lines = ["# Stage 2 service experiments", ""]
    facts = _read_json(RESULTS / "facts.json")
    if facts:
        w = facts.get("windows", {})
        lines += [f"Runner: {w.get('ProductName')} ({w.get('EditionID')}, {w.get('InstallationType')}), build "
                  f"{w.get('CurrentBuild')}.{w.get('UBR')}; UAC EnableLUA={facts.get('uac', {}).get('EnableLUA')}; "
                  f"elevated={facts.get('elevated')}; session {facts.get('session_id')}; dwm.exe in sessions "
                  f"{facts.get('dwm_sessions')}.", ""]
    for title, file, key in (("E1 on the runner", "e1.json", None), ("E3c", "e3c.json", "verdict"),
                             ("E3b", "e3b.json", "verdict")):
        data = _read_json(RESULTS / file)
        lines += [f"## {title}", "", "```"]
        if data is None:
            lines.append("no result")
        elif key is None:
            lines += [data.get("summary", ""), "", f"same as the laptop: {data.get('same_as_laptop')}"]
        else:
            lines.append(json.dumps(data.get(key), indent=1))
        lines += ["```", ""]
    e3 = _read_json(RESULTS / "e3.json")
    lines += ["## E3", ""]
    for t, rec in (e3 or {}).items():
        lines += [f"### {t} service SID", "", "```", json.dumps(rec.get("verdict") or {"why": rec.get("why")}, indent=1),
                  "```", ""]
    if not e3:
        lines.append("no result")
    text = "\n".join(lines)
    print(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return MEASURED


COMMANDS = {"facts": cmd_facts, "layout": cmd_layout, "e1": cmd_e1, "e3c": cmd_e3c, "e3": cmd_e3, "e3b": cmd_e3b,
            "summary": cmd_summary}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=list(COMMANDS))
    args = ap.parse_args(argv)
    if args.command != "summary":
        guard.require_throwaway_runner()
        RESULTS.mkdir(exist_ok=True)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
