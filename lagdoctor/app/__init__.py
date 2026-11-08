"""lag-doctor's desktop app (docs/LAUNCHER-PLAN.md): its own window, drawn by WebView2, listing your recorded games
and the results of each recording.

    lagdoctor app              # or the Start-menu shortcut the MSI adds: pythonw.exe -m lagdoctor app

The window (window.py) hosts WebView2 through lagdoctor/winapi/webview2.py; the bridge (bridge.py) is the only way
the page reaches Python, and the view-models (views.py, over library.py) turn sessions on disk into what each screen
shows. The screens themselves are the files in assets/.
"""
from __future__ import annotations

from pathlib import Path

# The app's AppUserModel ID: set by the process before any window opens, and on the MSI's Start-menu shortcut
# (installer/lag-doctor.wxs, ShortcutProperty), so the taskbar groups, pins and relaunches the app under its own icon.
# tests/test_msi.py holds the two equal. Microsoft's form: Company.Product, no spaces, at most 128 characters.
APP_ID = "LagDoctor.App"
APP_NAME = "lag-doctor"
ICON = Path(__file__).resolve().parent / "lag-doctor.ico"      # written by scripts/make_icon.py
ASSETS = Path(__file__).resolve().parent / "assets"            # the page: mapped to APP_HOST, read-only
