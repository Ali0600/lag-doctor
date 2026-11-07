"""scripts/stage2: the instruments of the Stage 2 service experiments, checked wherever no service is needed.

The experiments themselves (a LocalService service running PresentMon and the tracker, the named-pipe trigger)
run only on GitHub's throwaway runners, from .github/workflows/experiments.yml. What can be checked on any machine
is checked here: the refusal to run anywhere else, the token reader against whoami, the service SID against sc.exe,
the pipe server and client against each other, the service host's result writing, and the WARP stand-in presenting
frames. Nothing here creates a service or changes a setting.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stage2 import drive, guard, pipes, scm, service_host, tokeninfo  # noqa: E402

from lagdoctor.winapi import elevation  # noqa: E402

STANDIN = Path(__file__).resolve().parents[1] / "scripts" / "stage2" / "standin.py"
RUNNER = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}


# ---------------------------------------------------------------------------------------- the guard ---

def test_the_experiments_run_only_on_a_github_hosted_runner(monkeypatch, capsys):
    assert guard.on_throwaway_runner(RUNNER)
    for env in ({}, {"GITHUB_ACTIONS": "true"}, {"RUNNER_ENVIRONMENT": "github-hosted"},
                {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"},
                {"GITHUB_ACTIONS": "1", "RUNNER_ENVIRONMENT": "github-hosted"}):
        assert not guard.on_throwaway_runner(env), env
    with pytest.raises(SystemExit) as exc:
        guard.require_throwaway_runner({"GITHUB_ACTIONS": "true"})
    assert exc.value.code == guard.REFUSED_EXIT
    assert "Nothing was changed" in capsys.readouterr().out
    # The driver refuses before touching anything, on this machine and on a CI test job alike.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)
    with pytest.raises(SystemExit) as exc:
        drive.main(["layout"])
    assert exc.value.code == guard.REFUSED_EXIT


# ------------------------------------------------------------------------------------ pure helpers ---

def test_a_sys_path_entry_is_inside_the_install_folder_only_when_under_it():
    root = Path(r"C:\Program Files\lagdoctor-stage2")
    assert drive.inside(r"C:\Program Files\lagdoctor-stage2\python313.zip", root)
    assert drive.inside(r"C:\Program Files\lagdoctor-stage2", root)
    assert drive.inside(r"c:\program files\LAGDOCTOR-STAGE2\stage2", root)
    assert not drive.inside(r"C:\Program Files\lagdoctor-stage2-other\python313.zip", root)   # a prefix is not inside
    assert not drive.inside(r"C:\Program Files", root)
    assert not drive.inside("", root)                                                         # "" = the current folder
    assert not drive.inside(r"D:\lagdoctor-stage2", root)


def test_a_capability_sid_in_the_default_descriptor_is_masked():
    sddl = "O:BAG:BAD:(A;;0x1800;;;AC)(A;;0x1800;;;S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687)"
    assert drive.masked(sddl) == "O:BAG:BAD:(A;;0x1800;;;AC)(A;;0x1800;;;<capability>)"
    assert drive.masked(None) is None
    assert drive.masked(drive.LAPTOP_OWN) == drive.LAPTOP_OWN


def test_the_signature_verdict_decides_by_winverifytrust_and_reports_the_cross_check():
    rows = [{"name": "python.exe", "winverifytrust": "0x00000000", "signer_cn": "Python Software Foundation",
             "ps_status": "Valid"},
            {"name": "vcruntime140.dll", "winverifytrust": "0x00000000", "signer_cn": "Microsoft", "ps_status": "Valid"},
            {"name": "mystery.pyd", "winverifytrust": "0x800B0100", "signer_cn": None, "ps_status": "NotSigned"},
            {"name": "python.cat", "winverifytrust": "0x00000000", "signer_cn": "Python Software Foundation",
             "ps_status": None}]
    v = drive.signature_verdict(rows, {"parsed": 3})
    assert v["binaries"] == 3 and v["unsigned"] == ["mystery.pyd"]
    assert v["signers"] == ["Microsoft", "Python Software Foundation"]
    assert v["cross_check"] == "PowerShell agrees on every binary"
    rows[0]["ps_status"] = "NotSigned"
    assert drive.signature_verdict(rows, {"parsed": 3})["cross_check"] == "PowerShell disagrees on ['python.exe']"
    # A reading PowerShell could not give is said, never counted as unsigned (the first run did that).
    v = drive.signature_verdict(rows, {"parsed": 0, "parse_error": "ValueError: no JSON"})
    assert v["unsigned"] == ["mystery.pyd"] and v["cross_check"].startswith("PowerShell's reading could not be read")


def test_the_all_process_probe_stops_itself_and_drops_display_tracking_only_with_gpu_tracking():
    from lagdoctor.capture import presentmon
    exe = Path(r"C:\Program Files\lagdoctor-stage2\PresentMon-2.6.0-x64.exe")
    argv = service_host.all_presents_argv(exe, 8, "lagdoctor-e3-all")
    assert argv[:4] == [str(exe), "--timed", "8", "--terminate_after_timed"]
    assert list(presentmon.FLAGS) == argv[4:4 + len(presentmon.FLAGS)] and "--process_id" not in argv
    assert argv[-3:] == ["--session_name", "lagdoctor-e3-all", "--stop_existing_session"]
    bare = service_host.all_presents_argv(exe, 8, "lagdoctor-e3-all2", track_display=False)
    assert "--no_track_display" in bare and "--no_track_gpu" in bare       # the first is ignored without the second
    assert "--track_pc_latency" not in bare and "--track_frame_type" not in bare   # they would shift the columns
    assert "--output_stdout" in bare and "--qpc_time" in bare and "--no_console_stats" in bare


def test_presentmon_rows_are_tallied_per_process():
    from lagdoctor.capture import presentmon
    header = "Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,FrameType,TimeInQPC,MsBetweenPresents,MsUntilDisplayed\n"
    row = "{app},{pid},0x1A2B,DXGI,1,0,0,Composed: Flip,Application,{q},16.7,NA\n"
    text = header + "".join(row.format(app=a, pid=p, q=100 + i) for i, (a, p) in
                            enumerate([("game.exe", 7), ("dwm.exe", 3), ("game.exe", 7), ("game.exe", 7)]))
    rows = list(presentmon.parse([text.encode("latin-1")]))
    assert service_host.tally(rows) == {"7": {"rows": 3, "application": "game.exe"},
                                        "3": {"rows": 1, "application": "dwm.exe"}}
    assert service_host.tally([]) == {}


def test_e3_judges_refusals_and_disagreements_from_the_service_record():
    rec = {"result": {
        "token": {"user": {"sid": "S-1-5-19"}, "privileges": [{"name": "SeChangeNotifyPrivilege"}],
                  "restricted": True, "restricted_sids": [{"sid": scm.service_sid("svc")}]},
        "providers": {"A": {"enabled": True}, "B": {"enabled": False, "error": "EnableTraceEx2: Access is denied."},
                      "C": {"enabled": False, "error": "StartTraceW: Access is denied."}},
        "authz": {"A": {"enable": True}, "B": {"enable": True}, "C": {"enable": False}},
        "game_pid": 42,
        "all_presents": {"exit_code": 0, "rows": 5, "rows_per_pid": {"42": {"rows": 4, "application": "<unknown>"},
                                                                       "3": {"rows": 1, "application": "dwm.exe"}}},
        "capture": {"ended_by_itself": True, "ended_qpc_s": 110.0,
                    "frames": {"status": {"state": "ok"}, "presentmon": {"rows": 7, "exit_code": 0, "stop_path": "exited"}},
                    "net_flows": {"status": {"state": "ok"}, "health": {"events": 3, "game_events": 2,
                                                                         "selftest": {"verified": True}}}}},
        "standin": {"ended_qpc_s": 100.0}, "leftover_sessions": {"lagdoctor": 4201, "lag-doctor-net": 0}}
    v = drive._judge_e3(rec, "svc")
    assert v["user_is_local_service"] and v["privileges"] == ["SeChangeNotifyPrivilege"]
    assert v["restricted_token"] and v["service_sid_in_restricted_list"]
    assert v["providers_enabled"] == ["A"]
    assert v["providers_refused"] == {"B": "EnableTraceEx2: Access is denied.", "C": "StartTraceW: Access is denied."}
    assert v["authz_disagrees_on"] == ["B"]                # Windows' check said yes, enabling said no
    assert v["ended_s_after_game_exit"] == 10.0
    assert v["leftover_sessions"] == ["lag-doctor-net"]
    assert v["frame_rows"] == 7 and v["tracker_events"] == 3 and v["tracker_selftest"] is True
    assert v["all_presents"]["rows"] == 5 and v["all_presents"]["pids"] == 2 and v["all_presents"]["game_rows"] == 4
    assert v["all_presents_no_display"] == {"exit_code": None, "rows": None, "pids": 0, "game_rows": 0,
                                            "first_rows": None, "stderr_tail": None, "error": None}


# ------------------------------------------------------------------------------- live, read-only ---

def _whoami(*args: str) -> list[list[str]]:
    out = subprocess.run(["whoami.exe", *args, "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
    return [row for row in csv.reader(out.stdout.splitlines()) if row]


def test_the_token_reader_agrees_with_whoami():
    snap = tokeninfo.snapshot()
    assert snap["user"]["sid"] == elevation.token_user_sid() == _whoami("/user")[0][1]
    assert {p["name"] for p in snap["privileges"]} == {row[0] for row in _whoami("/priv")}
    assert {g["sid"] for g in snap["groups"]} >= {row[2] for row in _whoami("/groups")}   # name, type, SID, ...
    assert snap["restricted"] is False and snap["restricted_sids"] == []
    assert snap["integrity"].startswith("S-1-16-") and snap["session_id"] >= 0
    assert tokeninfo.service_sids([{"sid": "S-1-5-80-1-2-3-4-5"}, {"sid": "S-1-5-32-544"}]) == ["S-1-5-80-1-2-3-4-5"]


def test_the_service_sid_is_the_one_windows_derives():
    for name in ("EventLog", "lagdoctor-e3-restricted", "Mixed Case Name"):
        out = subprocess.run(["sc.exe", "showsid", name], capture_output=True, text=True, check=True).stdout
        assert f"SERVICE SID: {scm.service_sid(name)}" in out, out
    assert scm.status("EventLog")["state"] in scm.STATES.values()


# ----------------------------------------------------------------------------------------- pipes ---

def test_the_pipe_server_serves_one_client_at_a_time_and_is_the_only_instance():
    path = "\\\\.\\pipe\\lagdoctor-test-" + uuid.uuid4().hex
    sddl = f"D:P(A;;GA;;;SY)(A;;GRGW;;;{elevation.token_user_sid()})"
    with pytest.raises(TimeoutError) as exc:                 # no server yet: every open fails with "not found"
        pipes.request(path, b"early", timeout_s=0.3)
    details = exc.value.details
    assert details["first_error"] == pipes.ERROR_FILE_NOT_FOUND and details["attempts"] >= 2
    assert details["errors"] == {pipes.ERROR_FILE_NOT_FOUND: details["attempts"]}   # every attempt, counted
    served: list[bytes] = []
    with pipes.PipeServer(path, sddl) as server:
        assert server.wait_client(0.2) is False              # idle: the wait returns, the server can stop itself
        with pytest.raises(OSError) as second:
            pipes.PipeServer(path, sddl)
        assert second.value.winerror == pipes.ERROR_ACCESS_DENIED

        def serve() -> None:
            assert server.wait_client(5.0)
            served.append(server.serve_one(lambda m: b"reply:" + m))
            assert server.wait_client(5.0)
            served.append(server.serve_one(lambda m: b"again:" + m))

        worker = threading.Thread(target=serve)
        worker.start()
        first = pipes.request(path, b"one", timeout_s=5.0)
        second_reply = pipes.request(path, b"two", timeout_s=5.0)
        worker.join(10.0)
    assert not worker.is_alive()
    assert served == [b"one", b"two"]
    assert first["reply"] == b"reply:one" and second_reply["reply"] == b"again:two"
    assert first["attempts"] >= 1 and first["connect_s"] < 5.0
    with pytest.raises(ValueError):
        pipes._write_message(0, 0, b"x" * (pipes.MAX_MESSAGE + 1))


# ---------------------------------------------------------------------------------- service host ---

def test_the_service_host_writes_each_result_into_a_new_file_only(tmp_path):
    out = tmp_path / "e3c.json"
    assert service_host.run_mode("e3c", "svc", [str(out)], [], threading.Event()) == service_host.FINISHED
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mode"] == "e3c" and data["service"] == "svc" and data["servicemain_args"] == [str(out)]
    assert data["token"]["user"]["sid"] == elevation.token_user_sid()
    assert isinstance(data["runtime"]["sys_path_at_start"], list) and data["runtime"]["flags"]["safe_path"] in (0, 1)
    before = out.read_bytes()
    assert service_host.run_mode("e3c", "svc", [str(out)], [], threading.Event()) == service_host.NOT_WRITTEN
    assert out.read_bytes() == before
    assert service_host.run_mode("e3", "svc", [str(tmp_path / "x")], [], threading.Event()) == service_host.BAD_ARGS
    assert service_host.run_mode("e3b", "svc", [], ["a", "b"], threading.Event()) == service_host.BAD_ARGS
    assert service_host.run_mode("nope", "svc", [], [], threading.Event()) == service_host.BAD_ARGS
    assert not (tmp_path / "x").exists()
    # A crash is a result too: the traceback is saved and the exit code says so.
    assert service_host.run_mode("e3", "svc", [str(tmp_path / "crash.json"), "not-a-pid", "1", "none"], [],
                                 threading.Event()) == service_host.CRASHED
    assert "Traceback" in json.loads((tmp_path / "crash.json").read_text(encoding="utf-8"))["crashed"]


def test_the_service_host_refuses_to_run_outside_the_service_control_manager():
    proc = subprocess.run([sys.executable, str(STANDIN.with_name("service_host.py")), "e3c"], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == service_host.NOT_A_SERVICE
    assert "service control manager" in proc.stderr


# -------------------------------------------------------------------------------------- stand-in ---

def test_the_stand_in_presents_frames_on_warp(tmp_path):
    status = tmp_path / "standin.json"
    proc = subprocess.run([sys.executable, str(STANDIN), "--seconds", "1.5", "--status", str(status)],
                          capture_output=True, text=True, timeout=60)
    data = json.loads(status.read_text(encoding="utf-8"))
    assert proc.returncode == 0, (proc.stderr, data)
    assert data["ready"] is True and data["driver"] == "warp"
    assert data["presents"]["ok"] >= 10 and data["presents"]["failed"] == 0
    assert data["adapter"]["vendor_id"] == "0x1414"           # Microsoft Basic Render Driver: WARP, not a GPU
    assert isinstance(data["pid"], int) and data["pid"] > 0
    assert data["first_present_qpc_s"] < data["ended_qpc_s"]
