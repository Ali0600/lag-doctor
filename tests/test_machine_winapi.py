"""Machine-state wrappers (dxgi, display, power, storage).

Pure decoding is pinned on literal sample points, most of them the values this laptop returned in the research
(WT 5-9). The live tests are read-only and compare with an independent readout: nvidia-smi, typeperf, the
registry, powercfg, psutil, Get-PhysicalDisk and WMI. Nothing here changes a setting: the only display call that
reaches Windows uses CDS_TEST, and the setters are exercised through recorders.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import string
import subprocess
import uuid
import winreg
from pathlib import Path

import psutil
import pytest

from lagdoctor.winapi import display, dxgi, power, storage

SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
POWERSHELL = SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def run(argv: list, timeout: float = 60) -> str:
    proc = subprocess.run([str(a) for a in argv], capture_output=True, timeout=timeout)
    assert proc.returncode == 0, f"{argv[0]} exited {proc.returncode}: {proc.stderr[-500:]!r}"
    return proc.stdout.decode("oem", errors="replace")


def powershell_json(script: str):
    return json.loads(run([POWERSHELL, "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", script]))


# ======================================================================== dxgi: pure

@pytest.mark.parametrize("high, low, text", [
    (0, 0x00010F83, "0x00000000_0x00010f83"),          # the NVIDIA GPU here (WT 5.1)
    (1, 2, "0x00000001_0x00000002"),                    # HighPart first, as in PDH names
    (-1, 0xFFFFFFFF, "0xffffffff_0xffffffff"),          # HighPart is a signed LONG
])
def test_luid_str_is_pdh_order_lower_case(high, low, text):
    assert dxgi.luid_str(high, low) == text


@pytest.mark.parametrize("vid, name", [(0x10DE, "nvidia"), (0x1002, "amd"), (0x8086, "intel"),
                                       (0x1414, "microsoft"), (0x10DF, "other"), (0, "other")])
def test_vendor_names(vid, name):
    assert dxgi.vendor_name(vid) == name


@pytest.mark.parametrize("bus, dev, fn, text", [
    (1, 0, 0, "01:00.0"),                               # NVIDIA here; NVML says 00000000:01:00.0 (WT 4.5)
    (0, 2, 0, "00:02.0"),                               # Intel here
    (0xFF, 0x1F, 7, "FF:1F.7"),                         # the largest PCI address
    (0x100, 0, 0, None), (0, 0x20, 0, None), (0, 0, 8, None),
    (0xFFFFFFFF, 0xFFFF, 0xFFFF, None),                 # the software adapter's "no location" (WT 5.3)
])
def test_pci_str(bus, dev, fn, text):
    assert dxgi.pci_str(bus, dev, fn) == text


@pytest.mark.parametrize("value, role", [(0x2313, "discrete"), (0x232B, "integrated"), (0x105, None),
                                         (0x30, None), (0, None)])
def test_hybrid_role_from_the_adapter_type_bits(value, role):
    assert dxgi.hybrid_role(value) == role             # 0x2313 / 0x232B / 0x105 are this machine's (WT 5.3)


@pytest.mark.parametrize("caps, enabled", [(0xB, True), (0x1, False), (0x2, True), (0x0, False)])
def test_hags_is_the_enabled_bit_not_the_supported_bit(caps, enabled):
    assert dxgi.hags_enabled(caps) is enabled          # Intel here: 0x1 = supported but off (WT 5.3)


@pytest.mark.parametrize("flags, kmt, software", [(2, None, True), (0, 0x105, True), (0, 0x2313, False),
                                                  (0, None, False), (1, 0x232B, False)])
def test_software_adapter_from_dxgi_flag_or_kmt_bit(flags, kmt, software):
    assert dxgi.is_software(flags, kmt) is software


# ======================================================================== display: pure

@pytest.mark.parametrize("good", [r"\\.\DISPLAY1", r"\\.\display2", r"\\.\DISPLAY999"])
def test_check_device_accepts_gdi_names(good):
    assert display.check_device(good) == good.upper()


@pytest.mark.parametrize("bad", [r"\\.\DISPLAY0", r"\\.\DISPLAY1000", "DISPLAY1", r"\\?\DISPLAY1",
                                 r"\\.\DISPLAY1\Monitor0", r"C:\Windows", "", None, 1])
def test_check_device_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        display.check_device(bad)


@pytest.mark.parametrize("hz", [20, 60, 144, 2000])
def test_check_hz_accepts(hz):
    assert display.check_hz(hz) == hz


@pytest.mark.parametrize("hz", [19, 2001, 0, 1, True, 60.0, "60", None])
def test_check_hz_rejects(hz):
    with pytest.raises(ValueError):
        display.check_hz(hz)


def test_cds_flags_map_test_only_to_cds_test_and_nothing_else():
    assert display.cds_flags(True) == display.CDS_TEST == 0x2
    assert display.cds_flags(False) == display.CDS_UPDATEREGISTRY == 0x1
    for not_bool in (1, 0, "yes", None):
        with pytest.raises(TypeError):
            display.cds_flags(not_bool)


@pytest.mark.parametrize("num, den, hz", [(342060000, 2375360, 144.003), (60000, 1001, 59.94), (60, 1, 60.0),
                                          (1, 0, None), (0, 1, None)])
def test_rational_refresh(num, den, hz):
    assert display.rational_hz(num, den) == hz         # 342060000/2375360 is this panel (WT 6.1)


@pytest.mark.parametrize("tech, internal", [
    (0x80000000, True), (6, True), (11, True), (13, True),           # built-in panel values (WT 6.2)
    (5, False), (10, False), (4, False), (15, False), (0, False), (17, False),
    (0xFFFFFFFF, None), (7, None), (18, None),          # OTHER, a gap in the enum, newer than the SDK
])
def test_internal_from_output_technology(tech, internal):
    assert display.is_internal(tech) is internal


def test_rates_at_keeps_progressive_rates_at_this_resolution_and_depth():
    modes = [(1920, 1080, 32, 60, 0), (1920, 1080, 32, 144, 0), (1920, 1080, 32, 144, 0),   # a repeat
             (1920, 1080, 16, 120, 0),                  # another colour depth
             (1280, 720, 32, 165, 0),                   # another resolution
             (1920, 1080, 32, 240, display.DM_INTERLACED),
             (1920, 1080, 32, 1, 0), (1920, 1080, 32, 0, 0)]   # "hardware default"
    assert display.rates_at(modes, 1920, 1080, 32) == [60, 144]


@pytest.mark.parametrize("current, offered, exact, merged", [
    (144, [60, 144], 144.003, (144.003, [60, 144])),    # this panel: QDC's exact rate wins
    (120, [60, 144], None, (120.0, [60, 120, 144])),    # the rate in use is offered; whole Hz stands in
    (1, [60], None, (None, [60])),                      # "hardware default" is no rate
    (0, [], None, (None, [])),
])
def test_merge_rates(current, offered, exact, merged):
    assert display.merge_rates(current, offered, exact) == merged


def test_set_refresh_sends_only_size_and_rate_with_the_mapped_flag(monkeypatch):
    calls = []

    def recorder(device, dm_ref, hwnd, flags, lparam):
        dm = dm_ref._obj                      # the DEVMODEW behind ctypes.byref()
        calls.append((device, dm.dmFields, dm.dmDisplayFrequency, dm.dmPelsWidth, dm.dmPelsHeight, flags))
        return 0

    dev = display.list_displays()[0]["device"]
    now = display.current_mode(dev)
    monkeypatch.setattr(display, "_ChangeDisplaySettingsExW", recorder)
    assert display._ChangeDisplaySettingsExW is recorder      # never reach Windows with the real-change flag
    assert display.set_refresh(dev, now["hz"], test_only=True) == 0
    assert display.set_refresh(dev, now["hz"], test_only=False) == 0
    fields = display.DM_PELSWIDTH | display.DM_PELSHEIGHT | display.DM_DISPLAYFREQUENCY
    assert calls == [(dev.upper(), fields, now["hz"], now["width"], now["height"], display.CDS_TEST),
                     (dev.upper(), fields, now["hz"], now["width"], now["height"], display.CDS_UPDATEREGISTRY)]


# ======================================================================== power: pure

@pytest.mark.parametrize("value, name", [(0, "battery_saver"), (1, "better_battery"), (2, "balanced"),
                                         (3, "high_performance"), (4, "max_performance"), (5, "game_mode"),
                                         (6, "mixed_reality"), (7, None), (-1, None)])
def test_effective_mode_names_are_the_header_values(value, name):
    assert power.mode_name(value) == name


@pytest.mark.parametrize("guid, name", [
    ("961cc777-2547-4f9d-8174-7d86181b8a7a", "best_efficiency"),
    ("961CC777-2547-4F9D-8174-7D86181B8A7A", "best_efficiency"),
    ("00000000-0000-0000-0000-000000000000", "balanced"),
    ("3af9b8d9-7c97-431d-ad78-34a8bfea439f", "balanced"),
    ("ded574b5-45a0-4f42-8737-46345c09c238", "best_performance"),
    ("64a64f24-65b9-4b56-befd-5ec1eaced9b3", None),     # a power plan, not a slider position
])
def test_slider_names(guid, name):
    assert power.slider_name(guid) == name


def _le(guid: str) -> bytes:
    return uuid.UUID(guid).bytes_le


@pytest.mark.parametrize("kind, data, name", [
    # What PowerReadPossibleValue gave for the personality's indexes 0, 1 and 2 here (WT 7.2, 2026-11-07)
    (3, bytes.fromhex("081384a14135ab4fbc81f71556f20b4a"), "power_saver"),
    (3, bytes.fromhex("da7f5e8cbfe8964a9a85a6e23a8c635c"), "high_performance"),
    (3, bytes.fromhex("22421b3894f6f0419685ff5bb260df2e"), "balanced"),
    (4, _le("381b4222-f694-41f0-9685-ff5bb260df2e"), None),            # REG_DWORD, not a GUID
    (3, _le("381b4222-f694-41f0-9685-ff5bb260df2e") + b"\0", None),    # 17 bytes
    (3, _le("381b4222-f694-41f0-9685-ff5bb260df2e")[:15], None),
    (3, b"", None),
    (3, _le("ded574b5-45a0-4f42-8737-46345c09c238"), None),            # a slider position, not a base plan
    (3, _le("00000000-0000-0000-0000-000000000000"), None),
])
def test_personality_from_its_possible_value(kind, data, name):
    assert power.decode_personality(kind, data) == name


# ======================================================================== power: the slider over fakes

SILENT = "64a64f24-65b9-4b56-befd-5ec1eaced9b3"


@pytest.fixture
def slider_on(monkeypatch):
    """slider_mode over fakes: the active plan, its personality (set per test), and getters that answer GUID_NULL
    ("balanced") with rc 0 on any plan, as the AC one answered "balanced" on CI's High performance plan (2026-11-07)."""
    state = {"personality": "balanced", "asked": []}

    def personality(scheme, on_ac):
        state["asked"].append((scheme, on_ac))
        return state["personality"]

    monkeypatch.setattr(power, "active_scheme", lambda: (SILENT, "Silent"))
    monkeypatch.setattr(power, "scheme_personality", personality)
    monkeypatch.setattr(power, "_optional_export", lambda name, argtypes, restype: lambda guid_ptr: 0)
    return state


@pytest.mark.parametrize("personality, mode", [("balanced", "balanced"), ("high_performance", None),
                                               ("power_saver", None), (None, None)])
def test_slider_mode_answers_only_on_a_plan_whose_personality_is_balanced(slider_on, personality, mode):
    slider_on["personality"] = personality
    assert power.slider_mode(True) == mode
    assert power.slider_mode(False) == mode


def test_slider_mode_asks_the_active_plans_personality_for_the_same_power_source(slider_on):
    power.slider_mode(True)
    power.slider_mode(False)
    assert slider_on["asked"] == [(SILENT, True), (SILENT, False)]


def test_slider_mode_without_the_active_plan_is_none(slider_on, monkeypatch):
    def broken():
        raise OSError("PowerGetActiveScheme failed (5)")
    monkeypatch.setattr(power, "active_scheme", broken)
    assert power.slider_mode(True) is None and slider_on["asked"] == []


@pytest.mark.parametrize("raw, decoded", [
    ((1, 1, 100, 0), {"on_battery": False, "battery_pct": 100, "saver": False}),   # this laptop (WT 7.4)
    ((0, 2, 30, 1), {"on_battery": True, "battery_pct": 30, "saver": True}),
    ((255, 255, 255, 0), {"on_battery": None, "battery_pct": None, "saver": False}),
    ((1, 128, 100, 0), {"on_battery": False, "battery_pct": None, "saver": False}),  # no battery: no percent
    ((0, 0, 101, 2), {"on_battery": True, "battery_pct": None, "saver": None}),
    ((0, 4, 0, 0), {"on_battery": True, "battery_pct": 0, "saver": False}),
])
def test_decode_power_status(raw, decoded):
    assert power.decode_status(*raw) == decoded


@pytest.mark.parametrize("flag, present", [(1, True), (0, True), (8, True), (128, False), (255, None)])
def test_battery_present_from_the_battery_flag(flag, present):
    assert power.decode_battery_present(flag) is present


def test_guid_text_round_trips_in_windows_byte_order():
    g = power.guid_from_str("64A64F24-65B9-4B56-BEFD-5EC1EACED9B3")
    assert (g.Data1, g.Data2, g.Data3, bytes(g.Data4)) == (0x64A64F24, 0x65B9, 0x4B56,
                                                           bytes.fromhex("befd5ec1eaced9b3"))
    assert power.guid_str(g) == "64a64f24-65b9-4b56-befd-5ec1eaced9b3"


@pytest.mark.parametrize("bad", ["{64a64f24-65b9-4b56-befd-5ec1eaced9b3}", "64a64f2465b94b56befd5ec1eaced9b3",
                                 "64a64f24-65b9-4b56-befd-5ec1eaced9b3}", "urn:uuid:64a64f24-65b9-4b56-befd-5ec1eaced9b3",
                                 "64a64f24-65b9-4b56-befd-5ec1eaced9bZ", "", None, 123])
def test_guid_from_str_rejects_anything_but_the_canonical_form(bad):
    with pytest.raises(ValueError):
        power.guid_from_str(bad)


def _powercfg_index(scheme: str, alias: str) -> tuple[int, int]:
    """The plan setting's stored AC and DC index as `powercfg /qh` prints it: the independent readout. Its labels
    are translated, so this reads only the numbers: the last two "0x" values of the one setting's block are the AC
    and DC index, in that order (the possible-values lines come before them)."""
    out = subprocess.run(["powercfg", "/qh", scheme, "SUB_PROCESSOR", alias], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    values = re.findall(r":\s*0x([0-9a-fA-F]{8})\s*$", out, flags=re.M)
    if len(values) < 2:
        pytest.skip(f"powercfg printed no AC and DC index for {alias} in {scheme}: {out[:200]!r}")
    return int(values[-2], 16), int(values[-1], 16)


def test_plan_settings_match_what_powercfg_reports_for_every_plan():
    # Read-only. On this laptop cooling differs plugged in and on battery (Active, Passive), so a mix-up of the
    # two sides shows; every plan is read, the active one included.
    aliases = {"max_cpu": "PROCTHROTTLEMAX", "max_cpu1": "PROCTHROTTLEMAX1", "boost": "PERFBOOSTMODE",
               "cooling": "SYSCOOLPOL"}
    schemes = [g for g, _ in power.list_schemes()]
    assert power.active_scheme()[0] in schemes
    for scheme in schemes:
        got = power.plan_settings(scheme)
        assert set(got) == {f"{k}_{s}" for k in aliases for s in ("ac", "dc")}
        for key, alias in aliases.items():
            assert (got[f"{key}_ac"], got[f"{key}_dc"]) == _powercfg_index(scheme, alias), (scheme, key)


def test_set_active_scheme_passes_the_guid_and_raises_on_refusal(monkeypatch):
    calls = []
    answer = {"rc": 0}

    def recorder(root, guid_ref):
        calls.append((root, power.guid_str(guid_ref._obj)))   # the GUID behind ctypes.byref()
        return answer["rc"]

    current = power.active_scheme()[0]       # even a call that slipped through would change nothing
    monkeypatch.setattr(power, "_PowerSetActiveScheme", recorder)
    assert power._PowerSetActiveScheme is recorder
    power.set_active_scheme(current.upper())
    assert calls == [(None, current)]
    answer["rc"] = 5
    with pytest.raises(OSError) as err:
        power.set_active_scheme(current)
    assert err.value.winerror == 5
    with pytest.raises(ValueError):
        power.set_active_scheme("not-a-guid")
    assert len(calls) == 2                    # the malformed one never reached the call


# ======================================================================== storage: pure

@pytest.mark.parametrize("seek, media", [(False, "SSD"), (True, "HDD"), (None, "Unknown")])
def test_media_from_seek_penalty(seek, media):
    assert storage.media_from_seek_penalty(seek) == media


@pytest.mark.parametrize("drive_type, judged", [(2, True), (3, True), (0, False), (1, False), (4, False),
                                                (5, False), (6, False)])
def test_only_local_disks_are_judged(drive_type, judged):
    assert storage.judged_drive_type(drive_type) is judged


@pytest.mark.parametrize("root, guid_path, device", [
    ("C:\\", None, r"\\.\C:"),
    ("e:\\", None, r"\\.\E:"),
    ("C:\\mnt\\data\\", r"\\?\Volume{12345678-abcd-1234-abcd-123456789abc}" + "\\",
     r"\\.\Volume{12345678-abcd-1234-abcd-123456789abc}"),
    ("C:\\mnt\\data\\", "not a volume name", None),
    ("\\\\server\\share\\", None, None),
])
def test_volume_device(root, guid_path, device):
    assert storage.volume_device(root, guid_path) == device


@pytest.mark.parametrize("bad", ["relative\\game.exe", "C:game.exe", "C:\\x\0y", "", None, "C:\\" + "a" * 33000])
def test_check_path_wants_an_absolute_path(bad):
    with pytest.raises(ValueError):
        storage.check_path(bad)


# ======================================================================== live, read-only

@pytest.fixture(scope="module")
def adapters():
    return dxgi.list_adapters()


def test_live_adapters_have_the_contract_keys(adapters):
    keys = {"luid", "name", "vendor", "vendor_id", "vram_mb", "integrated", "software", "hags", "hybrid", "pci"}
    assert adapters and all(set(a) == keys for a in adapters)
    assert any(not a["software"] for a in adapters)
    assert len({a["luid"] for a in adapters}) == len(adapters)


@pytest.mark.needs("nvidia")
def test_live_nvidia_vram_and_pci_agree_with_nvidia_smi(adapters):
    nv = [a for a in adapters if a["vendor"] == "nvidia"]
    smi = shutil.which("nvidia-smi") or str(SYSTEM32 / "nvidia-smi.exe")
    # Declared absent (LAGDOCTOR_ABSENT=nvidia) this test skips before it runs; here an empty list is a failure.
    assert nv and Path(smi).is_file(), f"no NVIDIA adapter listed {[a['vendor'] for a in adapters]} or no {smi}"
    rows = [r.split(", ") for r in run([smi, "--query-gpu=memory.total,pci.bus_id",
                                        "--format=csv,noheader,nounits"]).strip().splitlines()]
    by_pci = {bus.split(":", 1)[1].upper(): int(total) for total, bus in rows}   # 00000000:01:00.0 -> 01:00.0
    for a in nv:
        total = by_pci[a["pci"]]
        # DXGI's dedicated memory excludes what the driver reserves: 8018 of 8192 MiB here (WT 5.1).
        assert 0.9 * total <= a["vram_mb"] <= total, (a["vram_mb"], total)


@pytest.mark.needs("gpu")
def test_live_every_pdh_gpu_engine_luid_is_a_listed_adapter(adapters):
    out = run([SYSTEM32 / "typeperf.exe", "-qx", "GPU Engine"])
    pdh = {m.lower() for m in re.findall(r"luid_(0x[0-9a-fA-F]{8}_0x[0-9a-fA-F]{8})_phys", out)}
    assert pdh, "typeperf listed no GPU Engine instances"
    ours = {a["luid"] for a in adapters}
    assert pdh <= ours, f"PDH LUIDs not listed: {pdh - ours}"
    assert {a["luid"] for a in adapters if not a["software"]} & pdh


def test_live_hags_agrees_with_the_hwschmode_registry_value(adapters):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers") as k:
            mode = winreg.QueryValueEx(k, "HwSchMode")[0]
    except FileNotFoundError:
        pytest.skip("HwSchMode is not set on this machine")
    hardware = [a for a in adapters if not a["software"]]
    # 2 = on, 1 = off (WT 8; the value's meaning is community-sourced, the D3DKMT bit is the effective state)
    assert (mode == 2) == any(a["hags"] for a in hardware), (mode, [(a["name"], a["hags"]) for a in hardware])


@pytest.fixture(scope="module")
def displays():
    found = display.list_displays()
    assert found, "no display attached to the desktop"
    return found


def test_live_displays_agree_with_enum_display_settings(displays, adapters):
    keys = {"device", "name", "adapter_luid", "internal", "primary", "width", "height", "hz", "max_hz", "modes_hz"}
    assert sum(d["primary"] for d in displays) == 1
    for d in displays:
        assert set(d) == keys
        mode = display.current_mode(d["device"])          # EnumDisplaySettingsW, a different API from QDC
        assert (d["width"], d["height"]) == (mode["width"], mode["height"])
        assert abs(d["hz"] - mode["hz"]) < 1.5, (d["hz"], mode["hz"])
        assert mode["hz"] in d["modes_hz"] and d["max_hz"] == max(d["modes_hz"])
        assert d["adapter_luid"] in {a["luid"] for a in adapters}


def test_live_internal_panels_agree_with_wmi(displays):
    rows = powershell_json("ConvertTo-Json -Compress -InputObject @(Get-CimInstance -Namespace root\\wmi "
                           "-ClassName WmiMonitorConnectionParams | Where-Object Active | "
                           "ForEach-Object { [int64]$_.VideoOutputTechnology })")
    if len(rows) != len(displays):
        pytest.skip(f"WMI lists {len(rows)} active monitors for {len(displays)} displays (clones or mirrors)")
    # Classified here with its own literals, not with display.is_internal: the readout must not share our code.
    wmi_internal = sorted(t in (0x80000000, 6, 11, 13) for t in rows)
    assert sorted(d["internal"] is True for d in displays) == wmi_internal


def test_live_cds_test_validates_without_changing_the_mode(displays):
    assert display.cds_flags(True) == display.CDS_TEST     # a CDS_UPDATEREGISTRY call would change the screen
    dev = next(d["device"] for d in displays if d["primary"])
    before = display.current_mode(dev)
    assert display.set_refresh(dev, before["hz"], test_only=True) == display.DISP_CHANGE_SUCCESSFUL
    # Negative control: 997 Hz is offered by no panel; CDS_TEST must refuse it (WT 6.1).
    assert display.set_refresh(dev, 997, test_only=True) == display.DISP_CHANGE_BADMODE
    assert display.current_mode(dev) == before


def test_live_current_mode_of_a_display_that_is_not_attached_raises():
    attached = {d["device"].upper() for d in display.list_displays()}
    spare = next(f"\\\\.\\DISPLAY{n}" for n in range(1, 999) if f"\\\\.\\DISPLAY{n}" not in attached)
    with pytest.raises(OSError):
        display.current_mode(spare)


def _powercfg(*args: str) -> str:
    return run([SYSTEM32 / "powercfg.exe", *args])


_SCHEME_LINE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s+\((.*)\)")


def test_live_active_scheme_matches_powercfg():
    guid, name = power.active_scheme()
    [(their_guid, their_name)] = _SCHEME_LINE.findall(_powercfg("/getactivescheme"))
    assert (guid, name) == (their_guid.lower(), their_name)


def test_live_list_schemes_matches_powercfg_list():
    theirs = {g.lower(): n for g, n in _SCHEME_LINE.findall(_powercfg("/list"))}
    assert theirs and dict(power.list_schemes()) == theirs


def test_live_power_status_matches_psutil():
    status = power.power_status()
    battery = psutil.sensors_battery()
    if battery is None:
        assert power.battery_present() is False
        return
    assert power.battery_present() is True
    assert status["on_battery"] == (not battery.power_plugged)
    assert status["battery_pct"] == battery.percent


def test_live_effective_mode_is_a_known_mode():
    mode = power.effective_mode(timeout_s=2.0)
    assert mode in power.EFFECTIVE_MODES
    status = power.power_status()
    slider = power.slider_mode(not status["on_battery"]) if status["on_battery"] is not None else None
    assert slider in (None, "best_efficiency", "balanced", "best_performance")


def test_live_effective_mode_fits_the_slider():
    # AC at the middle slider position gives balanced (or game mode) on a Balanced-type plan (WT 7.3). On any other
    # plan there is no slider: on CI's High performance plan the effective mode was high_performance while slider_mode
    # still said "balanced" (2026-11-07), which this test caught before slider_mode read the plan's personality.
    mode = power.effective_mode(timeout_s=2.0)
    status = power.power_status()
    slider = power.slider_mode(not status["on_battery"]) if status["on_battery"] is not None else None
    if status["on_battery"] is False and status["saver"] is False and slider == "balanced":
        assert mode in ("balanced", "game_mode"), mode


@pytest.mark.parametrize("scheme, personality", [
    ("381b4222-f694-41f0-9685-ff5bb260df2e", "balanced"),           # Balanced
    ("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", "high_performance"),   # High performance (hidden on this laptop)
    ("a1841308-3541-4fab-bc81-f71556f20b4a", "power_saver"),        # Power saver (hidden on this laptop)
])
def test_live_each_base_plan_is_most_like_itself(scheme, personality):
    # Each base plan's index, looked up in Windows' own list of possible values, must give that plan's own GUID.
    assert power.scheme_personality(scheme, True) == personality
    assert power.scheme_personality(scheme, False) == personality


def test_live_the_personality_of_a_plan_that_is_not_there_is_none():
    assert power.scheme_personality(str(uuid.uuid4()), True) is None


@pytest.mark.parametrize("rc, personality", [(0, "balanced"), (234, None)])   # 234 = ERROR_MORE_DATA
def test_a_possible_value_counts_only_when_windows_says_it_read_it(rc, personality, monkeypatch):
    # The fake leaves a well-formed Balanced GUID in the buffer either way, so the return code alone decides.
    def index_of(root, scheme_ref, sub_ref, setting_ref, index_ref):
        index_ref._obj.value = 2                                # the DWORD behind ctypes.byref()
        return 0

    def possible_value(root, sub_ref, setting_ref, kind_ref, index, buf, size_ref):
        assert index == 2
        kind_ref._obj.value = power.REG_BINARY
        ctypes.memmove(buf, _le("381b4222-f694-41f0-9685-ff5bb260df2e"), 16)
        size_ref._obj.value = 16
        return rc
    monkeypatch.setattr(power._powrprof, "PowerReadACValueIndex", index_of)
    monkeypatch.setattr(power._powrprof, "PowerReadPossibleValue", possible_value)
    assert power.scheme_personality(SILENT, True) == personality


@pytest.mark.parametrize("on_ac, reader", [(True, "PowerReadACValueIndex"), (False, "PowerReadDCValueIndex")])
def test_live_the_personality_is_read_for_the_power_source_asked_for(on_ac, reader, monkeypatch):
    # Every plan here has the same personality on AC and battery, so only the call itself can show the side.
    called = []
    for name in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
        real = getattr(power._powrprof, name)
        monkeypatch.setattr(power._powrprof, name, lambda *a, _n=name, _r=real: called.append(_n) or _r(*a))
    assert power.scheme_personality("381b4222-f694-41f0-9685-ff5bb260df2e", on_ac) == "balanced"
    assert called == [reader]


def _plan_type_index(scheme: str, on_ac: bool) -> int:
    """The personality ("Power plan type") index powercfg prints for a plan. Its labels are translated, so the two
    values that end their lines are read: AC first, then DC."""
    found = re.findall(r":\s*0x([0-9a-fA-F]{8})\s*$", _powercfg("/qh", scheme, "SUB_NONE", "PERSONALITY"), flags=re.M)
    assert len(found) == 2, found
    return int(found[0 if on_ac else 1], 16)


@pytest.mark.parametrize("on_ac", [True, False])
def test_live_the_slider_answers_exactly_on_a_plan_of_balanceds_type(on_ac):
    # powercfg's readout, not ours: the active plan has the slider when its type index is the Balanced plan's own.
    has_slider = _plan_type_index("SCHEME_CURRENT", on_ac) == _plan_type_index("SCHEME_BALANCED", on_ac)
    slider = power.slider_mode(on_ac)
    if has_slider:
        assert slider in ("best_efficiency", "balanced", "best_performance"), slider
    else:
        assert slider is None, slider


def test_live_media_type_matches_get_physicaldisk():
    rows = powershell_json(
        "ConvertTo-Json -Compress -InputObject @(Get-Partition | Where-Object DriveLetter | ForEach-Object { "
        "$n = [string]$_.DiskNumber; $d = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq $n }; "
        "[pscustomobject]@{drive = [string]$_.DriveLetter; media = [string]$d.MediaType} })")
    judged = [r for r in rows if r["media"] in ("SSD", "HDD")]
    if not judged:
        pytest.skip(f"Get-PhysicalDisk names no SSD or HDD: {rows}")
    for r in judged:
        assert storage.media_type(f"{r['drive']}:\\") == r["media"], r


def test_live_free_gb_matches_shutil():
    free = storage.free_gb(os.environ.get("SystemDrive", "C:") + "\\")
    theirs = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free / 2**30
    assert free is not None and abs(free - theirs) < 0.5


def test_live_unknowns_for_a_missing_drive_and_a_network_path():
    used = {p.device[0].upper() for p in psutil.disk_partitions(all=True)}
    missing = next(c for c in reversed(string.ascii_uppercase) if c not in used)
    assert storage.media_type(f"{missing}:\\Games\\game.exe") == "Unknown"
    assert storage.free_gb(f"{missing}:\\Games\\game.exe") is None
    assert storage.media_type(r"\\localhost\share-that-is-not-there\game.exe") == "Unknown"
