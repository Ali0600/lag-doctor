# Decisions

## Backlog: alternatives worth trying later

- **Own ETW frame tracer instead of PresentMon**: no download, but no GPU-busy timing and a lot of
  present-mode state to get right. Revisit hook: a second `Collector` named `frames` in `capture/`.
- **A live dashboard on a second monitor** (local web page fed by the session's `live()` snapshots).
  Revisit hook: `capture/session.py` already gathers `live()` once a second for the console line.
- **An elevated helper that relays PresentMon rows to an unelevated tool** (no whole-capture elevation).
  Revisit hook: `PresentMonSource` reads a line stream; a named-pipe reader could replace the child's stdout.
- **The Wi-Fi background-scan fix** (`WlanSetInterface` background_scan_enabled / media_streaming_mode,
  session-scoped). The DACL allows it unelevated but it was never executed. Revisit hook: the
  `wifi_scan_spikes` rule, once a capture shows ping spikes lined up with `wifi_scans`.
- **Asking for location permission** to see the access point (BSSID) and detect roaming exactly.
  Revisit hook: `Wlan.bssid()` already returns `None` when the permission is off.

---

## 2026-11-04: How frame timing gets ETW rights

PresentMon exits with code 6 ("access denied") unless the token is elevated or in Performance Log Users.
- **Relaunch only the capture elevated, per run (chosen as the offer `watch` makes)**: one UAC prompt; the
  elevated child captures and writes the file, the unelevated parent analyses and fixes, so HKCU fixes and
  the journal stay with the real user.
- **Join Performance Log Users once (offered by `setup` as instructions)**: no prompt afterwards, but it is a
  security-group change the user makes themselves; untested for the kernel network provider.
- **Elevated helper relaying rows**: deferred, worth trying (see Backlog).
- **Elevated `--output_file` run, analysed afterwards**: rejected. Same prompt as the chosen option, but no
  live status, and PresentMon block-buffers that file until exit.
Both offered options stay available at run time, so the user picks per run.

## 2026-11-04: NVIDIA telemetry through NVML, nvidia-smi only as fallback

- **NVML via ctypes (chosen)**: about 1.3 µs per call, and cumulative throttle counters (field 269 and
  friends) that give the share of each second spent throttled.
- **nvidia-smi streaming**: no ctypes layouts to get right, but a child process and a parser. Kept as
  the fallback when NVML cannot load.
- **One-shot nvidia-smi calls**: rejected, 96–106 ms each.

## 2026-11-04: Network probes in rounds

- **One round every 0.5 s pings each target once (chosen)**: a `None` RTT then always means "lost", so
  loss, charts and gaps never mix "not sent" with "lost".
- **Per-target rates (gateway 2 Hz, anchor 1 Hz) on a 4 Hz table**: what the research proposed; rejected
  because most rows would hold `None` for "not sent this tick".

## 2026-11-04: No fix that raises the game's priority

- **Lower the priority of the user's own background hogs, session-only (chosen)**.
- **Raise the game's priority**: rejected. It needs `PROCESS_SET_INFORMATION` on the game, which breaks
  "never touch the game", and anti-cheat may strip or flag that handle.

## 2026-11-04: Watch the game from outside, never inject

- **Observe from outside (chosen)**: ETW via PresentMon for frames, performance counters and system-wide
  snapshots for the rest. Anti-cheat cannot object to what it cannot see.
- **Inject an overlay DLL / hook Present** (how RTSS-style overlays work): exact per-frame CPU/GPU timing,
  but a ban risk with Easy Anti-Cheat, BattlEye or Vanguard, and a crash risk. Rejected: the risk lands
  on the user's game account.

## 2026-11-04: Frame timing from Intel PresentMon 2.6.0

- **PresentMon console exe (chosen)**: the industry-standard ETW frame tracer (MIT, Intel-signed), with
  GPU Busy / CPU Busy per frame, which is what separates GPU-bound from CPU-bound. Downloaded once to
  `%LOCALAPPDATA%\lag-doctor\presentmon\2.6.0`, SHA-256 and Authenticode checked.
  The user approved the download.
- **Write our own ETW tracer**: no download; less accurate and much more work. Status: deferred, worth
  trying (see Backlog).

## 2026-11-04: Python with ctypes, no compiled code

- **Python 3.13 + ctypes + psutil (chosen)**: matches the owner's other projects, no compiler needed, and
  every Windows API used (PDH, NtQuerySystemInformation, DXGI, IcmpSendEcho2, WLAN, ETW) is callable via
  ctypes.
- **C# with TraceEvent**: the most robust ETW library, but a second toolchain. Rejected for now: PresentMon
  already covers the heavy ETW work.

## 2026-11-04: Test against a fake game that lags on purpose (LagLab)

- **LagLab, pyglet/OpenGL (chosen)**: each mode produces one known cause and logs ground truth on the
  shared QPC clock. OpenGL presents show in PresentMon as runtime "Other".
- **Only recorded fixtures**: cheaper, but a fixture cannot prove the live capture path works.
  Rejected as the only check; fixtures are still used for parser and rule unit tests.

## 2026-11-04: Fixes are opt-in, journaled and verified

- **Plan, then ask, then journal-first apply with readback, and undo from the journal (chosen)**.
- **Auto-apply "safe" fixes**: faster, but changes the user's PC without consent. Rejected.
- **One-shot tweak scripts**: no undo. Rejected.
