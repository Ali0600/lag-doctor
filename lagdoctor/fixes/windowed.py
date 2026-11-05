"""windowed_optimizations_on: Windows 11 "Optimizations for windowed games" (WT 8; LC 4.14).

The setting lives in the same kind of "key=value;" string as the per-app GPU preference
(HKCU\\...\\UserGpuPreferences\\DirectXUserGlobalSettings, e.g. "SwapEffectUpgradeEnable=1;
VRROptimizeEnable=0;"), so only the SwapEffectUpgradeEnable field is edited and every other field
(VRR, Auto HDR) and all spacing stay as found.
"""
from __future__ import annotations

from typing import Any

from lagdoctor.fixes.catalog import register
from lagdoctor.fixes.gpu_preference import StringFieldFix, no_params

GLOBAL_SETTINGS = "DirectXUserGlobalSettings"


@register
class WindowedOptimizationsFix(StringFieldFix):
    id = "windowed_optimizations_on"
    title = "Turn on optimizations for windowed games"
    needs_restart = "game"
    field_name = "SwapEffectUpgradeEnable"
    caveat = ("Community-sourced (unverified): Microsoft documents the feature and its Settings switch (System > "
              "Display > Graphics), not this registry string. Restart the game afterwards.")
    description = ("Turns on Windows 11 \"Optimizations for windowed games\": DirectX 10 and 11 games in windowed "
                   "and borderless windows move from blt-model to flip-model presentation, which cuts frame "
                   "latency (Microsoft support article). Sets SwapEffectUpgradeEnable=1 in "
                   "DirectXUserGlobalSettings and keeps the other fields. " + caveat)

    def value_name(self, params: dict[str, Any]) -> str:
        no_params(params)
        return GLOBAL_SETTINGS

    def field_value(self, params: dict[str, Any]) -> str:
        no_params(params)
        return "1"

    def describe(self, before: Any, after: Any) -> str:
        shown = {"1": "on", "0": "off"}
        def label(v: Any) -> str:
            return shown.get(v, repr(v)) if isinstance(v, str) else "not set (Windows default)"
        return f"{self.title}: SwapEffectUpgradeEnable {label(before)} -> {label(after)}"
