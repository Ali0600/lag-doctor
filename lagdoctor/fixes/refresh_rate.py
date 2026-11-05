"""Fix "refresh_rate": set a display's refresh rate at its current resolution, journaled and reversible.

Every write is tried with CDS_TEST first and only then made with CDS_UPDATEREGISTRY, which stores the mode
in the user's profile, so no elevation (WT 6.4). DISP_CHANGE_RESTART counts as a failure: the mode would
only apply after a reboot. The resolution is never changed: the value journaled and compared is
{width, height, hz}, and a write refuses if the resolution moved. The readback needs EnumDisplaySettings and
QueryDisplayConfig to agree, since one API's answer is not proof the mode is on screen (WT 11.5.10).
"""
from __future__ import annotations

from typing import Any

from lagdoctor.fixes.base import GONE, Fix, is_gone
from lagdoctor.fixes.catalog import register
from lagdoctor.winapi.display import DISP_CHANGE, DISP_CHANGE_SUCCESSFUL, check_device, check_hz

# dmDisplayFrequency is whole Hz (144.003 Hz reads 144; 143.98 Hz would read 143, WT 6.3), so the two
# readouts agree when they are less than this apart.
QDC_TOLERANCE_HZ = 1.5


def _code(rc: int) -> str:
    return f"{DISP_CHANGE.get(rc, 'unknown result')} ({rc})"


@register
class RefreshRateFix(Fix):
    id = "refresh_rate"
    title = "Refresh rate"
    needs_admin = False
    needs_restart = None

    def __init__(self, display: Any = None) -> None:
        """`display` provides current_mode, list_displays and set_refresh; default the real winapi.display."""
        if display is None:
            from lagdoctor.winapi import display as display_api
            display = display_api
        self.display = display

    def target(self, params: dict[str, Any]) -> str:
        return (f"the refresh rate of display {check_device(params.get('device'))} "
                f"(Settings > System > Display > Advanced display)")

    def read(self, params: dict[str, Any]) -> Any:
        device = check_device(params.get("device"))
        try:
            mode = self.display.current_mode(device)
        except OSError:
            if self._info(device) is None:
                return dict(GONE)             # unplugged or switched off since
            raise
        return {"width": mode["width"], "height": mode["height"], "hz": mode["hz"]}

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        device = check_device(params.get("device"))
        hz = check_hz(params.get("hz"))
        if is_gone(current):
            raise LookupError(f"display {device} is not connected")
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
        live = self.display.current_mode(device)
        if (live["width"], live["height"]) != (value["width"], value["height"]):
            raise RuntimeError(f"{device} is now at {live['width']}x{live['height']}, not "
                               f"{value['width']}x{value['height']}; this fix never changes the resolution")
        rc = self.display.set_refresh(device, hz, test_only=True)
        if rc != DISP_CHANGE_SUCCESSFUL:
            raise RuntimeError(f"Windows rejected {hz} Hz on {device} in a test: {_code(rc)}; nothing was changed")
        rc = self.display.set_refresh(device, hz, test_only=False)
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

    def describe(self, before: Any, after: Any) -> str:
        if not isinstance(before, dict) or is_gone(before):
            return f"{self.title}: the display is not connected"
        return f"{self.title}: {before['hz']} Hz -> {after['hz']} Hz at {before['width']}x{before['height']}"

    def _info(self, device: str) -> dict | None:
        for d in self.display.list_displays():
            if d["device"].upper() == device:
                return d
        return None
