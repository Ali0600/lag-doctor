"""Fix "refresh_rate": set a display's refresh rate at its current resolution, journaled and reversible.

Every write is tried with CDS_TEST first and only then made with CDS_UPDATEREGISTRY, which stores the mode
in the user's profile, so no elevation (WT 6.4). The resolution is never changed: the value journaled and
compared is {width, height, hz}, and a write refuses if the resolution moved. The readback needs
EnumDisplaySettings and QueryDisplayConfig to agree, since one API's answer is not proof the mode is on screen
(WT 11.5.10).

What Windows has stored is part of the value: when the profile's mode (ENUM_REGISTRY_SETTINGS) differs from
the one on screen, read() adds it as "after_restart", the mode Windows will switch to at the next sign-in.
That is how DISP_CHANGE_RESTART is handled: Windows did not switch, but it did store the new mode (Microsoft's
ChangeDisplaySettingsEx documentation), so base.py puts the original back in the profile and reads it back,
and if that fails the entry stays applied, so undo can still take it back.

The GDI name (\\\\.\\DISPLAY1) belongs to whichever monitor Windows lists first, so a plan also stores the
monitor's own identity (its monitorDevicePath, WT 6.2) as params "monitor". read() and write() refuse a name
that now shows another monitor: to them it is a display that is not connected.
"""
from __future__ import annotations

from typing import Any

from lagdoctor.fixes.base import GONE, Fix, FixPlan, is_gone
from lagdoctor.fixes.catalog import register
from lagdoctor.winapi.display import DISP_CHANGE, DISP_CHANGE_SUCCESSFUL, check_device, check_hz

DISP_CHANGE_RESTART = 1         # WinUser.h: the mode was stored but applies only after a restart
MAX_MONITOR_PATH = 127          # monitorDevicePath is WCHAR[128] (wingdi.h)

# dmDisplayFrequency is whole Hz (144.003 Hz reads 144; 143.98 Hz would read 143, WT 6.3), so the two
# readouts agree when they are less than this apart.
QDC_TOLERANCE_HZ = 1.5


def _code(rc: int) -> str:
    return f"{DISP_CHANGE.get(rc, 'unknown result')} ({rc})"


def _monitor(params: dict[str, Any]) -> str | None:
    """The stored monitor identity; None for entries journaled before identities were stored."""
    value = params.get("monitor")
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= MAX_MONITOR_PATH or not value.isprintable():
        raise ValueError(f"monitor must be a monitor device path, got {value!r:.80}")
    return value


def _core(mode: dict) -> dict:
    return {"width": mode.get("width"), "height": mode.get("height"), "hz": mode.get("hz")}


def _shown(mode: Any) -> str:
    if not isinstance(mode, dict):
        return "nothing"
    hz = "the hardware default rate" if mode.get("hz") is None else f"{mode['hz']} Hz"
    return f"{mode.get('width')}x{mode.get('height')} at {hz}"


@register
class RefreshRateFix(Fix):
    id = "refresh_rate"
    title = "Refresh rate"
    needs_admin = False
    needs_restart = None

    def __init__(self, display: Any = None) -> None:
        """`display` provides current_mode, stored_mode, monitor_path, list_displays and set_refresh; default the
        real winapi.display."""
        if display is None:
            from lagdoctor.winapi import display as display_api
            display = display_api
        self.display = display

    def target(self, params: dict[str, Any]) -> str:
        return (f"the refresh rate of display {check_device(params.get('device'))} "
                f"(Settings > System > Display > Advanced display)")

    def plan(self, params: dict[str, Any]) -> FixPlan:
        """The report suggests {device, hz}; the plan adds which monitor that name shows now, so a later read,
        write or undo can tell when Windows has given the name to another monitor."""
        device = check_device(params.get("device"))
        if _monitor(params) is None:
            monitor = self.display.monitor_path(device)
            if not monitor:
                raise LookupError(f"display {device} is not connected (or Windows did not say which monitor it "
                                  f"is), so lag-doctor could not be sure a later undo reaches the same screen")
            params = {**params, "monitor": monitor}
        return super().plan(params)

    def read(self, params: dict[str, Any]) -> Any:
        device = check_device(params.get("device"))
        monitor = _monitor(params)
        if monitor is not None and self.display.monitor_path(device) != monitor:
            return dict(GONE)                 # unplugged, or the name now belongs to another monitor
        try:
            mode = self.display.current_mode(device)
        except OSError:
            if self._info(device) is None:
                return dict(GONE)             # unplugged or switched off since
            raise
        value = {"width": mode["width"], "height": mode["height"], "hz": mode["hz"]}
        stored = self.display.stored_mode(device)
        if stored != value:
            value["after_restart"] = stored   # what Windows switches to at the next sign-in
        return value

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        device = check_device(params.get("device"))
        hz = check_hz(params.get("hz"))
        if is_gone(current):
            raise LookupError(f"display {device} is not connected")
        if "after_restart" in current:
            raise ValueError(f"{device} shows {_shown(current)} but Windows has {_shown(current['after_restart'])} "
                             f"stored for it (a full-screen game may have changed the mode, or a change waits for "
                             f"a restart); lag-doctor leaves it alone until the two agree")
        if current["hz"] is None:
            raise ValueError(f"{device} reports its refresh rate only as 'hardware default', so a change could "
                             f"not be undone; change it in Settings instead")
        info = self._info(device)
        if info is None:
            raise LookupError(f"display {device} is not connected")
        if hz not in info["modes_hz"]:
            raise ValueError(f"{device} does not offer {hz} Hz at {current['width']}x{current['height']} "
                             f"(it offers {info['modes_hz']})")
        return {"width": current["width"], "height": current["height"], "hz": hz}

    def write(self, params: dict[str, Any], value: Any) -> None:
        device = check_device(params.get("device"))
        hz = check_hz(value["hz"])
        monitor = _monitor(params)
        if monitor is not None and self.display.monitor_path(device) != monitor:
            raise RuntimeError(f"{device} no longer shows the monitor lag-doctor read (it was unplugged, or Windows "
                               f"gave the name to another monitor); nothing was changed")
        live = self.display.current_mode(device)
        if (live["width"], live["height"]) != (value["width"], value["height"]):
            raise RuntimeError(f"{device} is now at {live['width']}x{live['height']}, not "
                               f"{value['width']}x{value['height']}; this fix never changes the resolution")
        rc = self.display.set_refresh(device, hz, test_only=True)
        if rc != DISP_CHANGE_SUCCESSFUL:
            raise RuntimeError(f"Windows rejected {hz} Hz on {device} in a test: {_code(rc)}; nothing was changed")
        rc = self.display.set_refresh(device, hz, test_only=False)
        if rc == DISP_CHANGE_RESTART:
            # Not a success (WT 6.4), but not "nothing changed" either: the mode is stored for the next sign-in.
            raise RuntimeError(f"Windows can switch {device} to {hz} Hz only after a restart ({_code(rc)}), and "
                               f"stored it for then")
        if rc != DISP_CHANGE_SUCCESSFUL:
            raise RuntimeError(f"setting {hz} Hz on {device} failed: {_code(rc)}")

    def readback(self, params: dict[str, Any]) -> Any:
        mode = self.read(params)
        if is_gone(mode):
            return mode
        info = self._info(check_device(params.get("device")))
        exact = info.get("hz") if info else None
        if mode["hz"] is None or exact is None or abs(exact - mode["hz"]) >= QDC_TOLERANCE_HZ:
            return {**mode, "display_config_hz": exact}     # the two readouts disagree: not confirmed
        return mode

    def also_ours(self, live: Any, before: Any, after: Any) -> bool:
        """lag-doctor's rate stored for after a restart while the screen still shows the original."""
        return (isinstance(live, dict) and isinstance(before, dict) and isinstance(after, dict)
                and _core(live) == _core(before) and live.get("after_restart") == _core(after))

    def describe(self, before: Any, after: Any) -> str:
        if not isinstance(before, dict) or is_gone(before):
            return f"{self.title}: the display is not connected"
        return f"{self.title}: {before['hz']} Hz -> {after['hz']} Hz at {before['width']}x{before['height']}"

    def _info(self, device: str) -> dict | None:
        for d in self.display.list_displays():
            if d["device"].upper() == device:
                return d
        return None
