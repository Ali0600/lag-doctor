# Decisions

## Backlog: alternatives worth trying later

- **Keep Alt from starting Windows' menu mode in the app's window**: the window procedure returns 0 for
  `WM_SYSCOMMAND` with `SC_KEYMENU` and no character, as Chromium's own window does, or the controller is created with
  AllowHostInputProcessing (`docs/research/desktop-app.md`, section 12). Alt+Left did not start menu mode on GitHub's
  runner or the owner's PC; a lone Alt is untested. Worth it if the app's window ever loses the keyboard after one.
  Revisit hook: the window procedure M1 writes, and `on_message` in the spike.

- **An advertised Start-menu shortcut** (`DISABLEADVTSHORTCUTS=1`) instead of the HKCU value that keys today's,
  so the install writes nothing to the installing account's registry. Needs `pythonw.exe` in a component of its own.
  Revisit hook: the `StartMenuShortcut` component in `installer/lag-doctor.wxs` (DECISIONS 2026-11-08, M1).
- **Analyse a recording again when Results opens**, so an old recording shows the current build's analysis, once
  long captures analyse fast enough. Revisit hook: `App.load` in `lagdoctor/app/main.py`.

- **A weekly CI run that skips the PresentMon cache**, so the real download also runs when nothing in the repo
  changed (GitHub moving its download hosts, say). Revisit hook: an `on: schedule` trigger in `ci.yml` and a
  condition on its "Restore PresentMon from the cache" step.
- **Own ETW frame tracer instead of PresentMon**: no download, but no GPU-busy timing and a lot of
  present-mode state to get right. Revisit hook: a second `Collector` named `frames` in `capture/`.
- **A live view on a second monitor**: a screen of the app's window (`docs/LAUNCHER-PLAN.md`), not a local web page,
  whose server 2026-11-08 rejected. While a game records the app draws nothing that moves, because its GPU time counts
  as another program's, so that cost needs measuring first. Revisit hook: the recorder's status messages
  (`watch --control stdio`, M4), which would carry `live()`, gathered once a second in `capture/session.py`.
- **`uv tool install` from PyPI as a developer channel**: no admin at install, but every recording keeps its UAC
  prompt and the elevated code stays user-writable. Revisit hook: the build backend and console entry point that
  the installer stage adds to `pyproject.toml` (`docs/INSTALLER-PLAN.md`).
- **The Wi-Fi background-scan fix** (`WlanSetInterface` background_scan_enabled / media_streaming_mode,
  session-scoped). The DACL allows it unelevated but it was never executed. Revisit hook: the
  `wifi_scan_spikes` rule, once a capture shows ping spikes lined up with `wifi_scans`.
- **Asking for location permission** to see the access point (BSSID) and detect roaming exactly.
  Revisit hook: `Wlan.bssid()` already returns `None` when the permission is off.
- **Compact frame storage for long captures** (`array('d')` columns, interned strings, or rows spilled to a
  temp file and streamed into the gzip writer). Today a frame costs about 0.7 KB of RAM and a 3-hour 240 FPS
  evening about 1.8 GiB. Revisit hook: `PresentMonSource`'s `SeriesBuilder` and `Capture.save`.
- **Ask before recording without frame timing** ("PresentMon is not ready: record everything else? [y/N]")
  instead of stopping. Today `watch` stops, and `--no-elevate` records without it. Revisit hook: the
  not-ready branch at the top of `cli.cmd_watch`.
- **A cause of its own for the commit limit** (`commit_limit`), so a page file running short never reads "low on
  memory" in the headline; it means a 37th rule, its labels and the rule counts. Revisit hook: the `roomy` branch
  of `rules._ram_pressure`.
- **Break ties at `Z_CAP` by how far past it each signal went**, not by `CAUSE_ORDER` alone, for a mark's or an
  event's first cause. Revisit hook: the `ranked` sort in `attribute.attribute`.
- **A CPU label picked per mark from the part that fired**: "held below full speed by Windows" where the limit flag
  rose at the mark, "running below full speed" where only the clock fell. Revisit hook: `diagnose.mark_list` would
  keep each cause's part, for `labels.cause_text` to read.
- **A launcher .exe for `lagdoctor` instead of the `.cmd` shim**, built in CI from our own C source: no "Terminate
  batch job (Y/N)?" after Ctrl+C and exact arguments, once recordings run in the user's own window (from M3 of
  `docs/LAUNCHER-PLAN.md`, before Stage 4); SignPath could sign it. Revisit hook: `SHIM` in `scripts/msi_build.py`
  and the `bin\` harvest in `installer/lag-doctor.wxs`.
- **Check whole dotted module names against the runtime**, not only top-level ones, should a pin ever ship a package
  without one of its submodules (3.13.17 has every one the full installer has). Revisit hook: the child in
  `scripts/runtime_modules.py` would list every module in `python313.zip`, and `allowed()` in
  `tests/test_stdlib_only.py` would check the whole name.
- **Microsoft's `WebView2Loader.dll` for the app's window, instead of the runtime's own export**: documented, and it
  honours WebView2's policy keys and overrides, but it is a binary without published source that SignPath must accept
  first. Revisit hook: the app's create-environment function (`docs/LAUNCHER-PLAN.md`, "The window").
- **A host exe for the app's window** (C# with Microsoft's WebView2 control, or C++ on webview/webview): Task Manager
  would name lag-doctor, and DPI, focus and accessibility would come with the control. The cost is a second toolchain
  and a signed exe, and it would reverse 2026-11-04's "Python with ctypes, no compiled code". Revisit hook: the M0
  gate in `docs/LAUNCHER-PLAN.md`.
- **Starting the store client through Explorer** (`IShellDispatch2::ShellExecute`) when Play finds it closed, so it
  never becomes lag-doctor's child. Revisit hook: the store-client check in the launch policy module
  (`docs/LAUNCHER-PLAN.md`, M6).
- **Profile folders shown by their variable in the terminal too** (the console summary and the CLI's own lines), so
  a pasted terminal is as safe to share as the report. Its report path would then need a form a browser opens.
  Revisit hook: `console.ascii_text` and `cli.say`, calling `html.user_neutral`.
- **A `changes` job with an `if:` on each CI job, instead of the push trigger's `paths-ignore`**, once a CI check is
  ever required: a job skipped by `if:` reports success, while a workflow skipped by its path filter leaves a required
  check Pending. Revisit hook: the `on:` block of `ci.yml`, plus one aggregate job that `needs:` the rest, to require.
- **More drop-in names, once a primary source states them**: ReShade's (it is said to install as `opengl32.dll` too,
  with a `ReShade.ini`) and Special K's own file names. No page that could be read on 2026-11-08 named them, so the
  folder check leaves them out; ReShade or Special K installed under a name the check knows (`dxgi.dll`, `d3d11.dll`)
  is listed already. Revisit hook: `PROXY_DLLS` and `LOADER_CONFIGS` in `lagdoctor/capture/gamefiles.py`.
- **PC-latency tracking as an opt-in** (a `watch` option that adds `--track_pc_latency`): gives back the exact
  animation error, and PC latency if it were stored, for games built with NVIDIA's latency markers, at the cost of
  switching on their ping code for that recording. Worth it if someone asks for those numbers in a Reflex game, or if
  anim-only pacing findings in a Reflex game turn out to come from Reflex's own sleep.
  Revisit hook: `FLAGS` and `live_args` in `lagdoctor/capture/presentmon.py`, and `test_pc_latency_tracking_stays_off`
  (DECISIONS 2026-11-09).

---

## 2026-11-09: PresentMon runs without PC-latency tracking

lag-doctor started PresentMon with `--track_pc_latency` on every capture. Measuring lag-doctor's own cost
(`docs/research/overhead.md`, section 5.1) found that the flag enables NVIDIA's PC Latency provider, which switches on
the latency-ping code inside every game built with its markers (Reflex or Streamline titles): a ping every 100-299 ms
and a real ETW write for every marker. A CapFrameX developer tied stutter in FINAL FANTASY VII REBIRTH to that switch
(PresentMon issue #570, blaming the game's own latency code). lag-doctor stores no PC-latency column; for those games
the flag only changes how PresentMon computes `MsAnimationError`. Put to the owner with A recommended; the owner
answered "Yes, drop it".
- **A: drop the flag (chosen)**: a PCL game records without its pings, and its `anim_error_ms` falls back to
  PresentMon's CPU-start estimate, as every other game's does. In a Reflex game that estimate is weaker, since NVIDIA
  places Reflex's sleep between the previous Present() and the simulation start, so its anim-only pacing events (and
  the dropped_frames, frame_cap, uneven_pacing and vsync_judder findings built on pacing events) are less trustworthy
  there, and a recording of such a game made before this change is not like-for-like with a later one on them
  [inferred, `docs/research/overhead.md` section 5.1]. The PresentMon fixtures were made again without the flag (only
  the `MsPCLatency` column changed, `NA` in every row), and `scripts/etw_access.py` marks Nvidia_PCL unused.
- **B: an opt-in `watch` option**: deferred, worth trying if someone asks for those numbers (backlog).
- **C: keep it**: rejected. A reported stutter in the owner's own game, for data lag-doctor does not keep.

Revisit hook: `FLAGS` in `lagdoctor/capture/presentmon.py`, guarded by `test_pc_latency_tracking_stays_off`.

## 2026-11-08: Compare shows what changed in the game itself, read from the game's folder at record time

Compare (screen 5 of `lagdoctor app`) set two recordings side by side with four conditions (power plan and mode,
refresh rate, how frames were timed, measured length), none of which a fan fix changes. A fan fix is a file beside
the game's exe (a DLL Windows loads from there instead of System32, an ASI loader and its plugins), a user
`Engine.ini`, or an in-game setting, so two recordings could differ in their numbers under identical conditions. Put
to the owner with A recommended; the owner chose "Both, A first".
- **A: automatic, at record time (chosen, built first)**: the inventory reads the game's folder (never the game's
  process) and lists the files a fan fix adds, with a fingerprint of each, and for an Unreal Engine game the user
  config files. Nothing to type or forget; it cannot see an in-game setting a game keeps elsewhere.
- **B: a note per recording, `lagdoctor watch --note TEXT` (chosen, its own PR next)**: the player's own words,
  kept in the capture and the summary, shown in the report and Compare. It catches anything the player names, an
  in-game setting included, but only when they remember to type it. It changes `cli.py` and
  `capture/session.py`, which the open M3 relay PR (#23) rewrites, so it follows that PR.
- **Only A, or only B**: not chosen. Each misses what the other sees.

What A lists (`lagdoctor/capture/gamefiles.py`; the implementing session's choices, from sources read on
2026-11-08):
- **The DLL names Ultimate ASI Loader's README says it can be installed as** that Windows itself keeps in System32
  (`dinput8.dll`, `dxgi.dll`, `d3d9.dll` ... `winmm.dll`, `xinput1_3.dll`), plus `d3d10core.dll` (DXVK's README) and
  `dwmapi.dll` (UE4SS's default proxy). Its `binkw32.dll`-style names are left out: games ship those DLLs
  themselves. No name is on Windows' KnownDLLs list, which Windows loads from System32 whatever lies beside the exe:
  a test reads that list from the registry and from the `\KnownDlls` object directory, which also holds the DLLs
  those import (42 DLLs against the key's 38 on the owner's PC).
  - **Every DLL in the exe's folder**: rejected. Games ship dozens of their own, so a game update would read as a fix.
- **ASI plugins** in the exe's folder and the loader's `scripts`, `plugins` and `update` folders (one level of
  subfolders, as its `LoadRecursively` default does), and the settings beside them: the loader's `global.ini`, a
  `<dll name>.ini` per DLL found, a `<plugin>.ini` per plugin, `wndmode.ini`, `dxvk.conf`, `UE4SS-settings.ini`
  and UE4SS's `Mods\mods.txt`.
- **For an Unreal Engine game** (the exe in `<project>\Binaries\Win64` or `WinGDK`, with the project's
  `Content\Paks` or the install's `Engine\Binaries` beside it): every `.ini` in `Saved\Config\<platform>` under
  `%LOCALAPPDATA%\<name>`, `Documents\My Games\<name>` (Windows' Documents folder, which OneDrive may hold) and the
  project folder itself, for the project's name and the install folder's, and the `.pak`, `.utoc` and `.ucas` files
  in `Content\Paks\~mods`. Epic's own page on config files does not give the packaged game's folder, so the places
  were measured: every Unreal game on the owner's PC kept its config under `%LOCALAPPDATA%\<name>\Saved\Config` in
  `WindowsNoEditor`, `Windows` or `WinGDK`, one named after its install folder rather than its project; FINAL
  FANTASY VII REBIRTH keeps it in `Documents\My Games\FINAL FANTASY VII REBIRTH\Saved\Config\WindowsNoEditor`.
  Every `.ini` there, not only `Engine.ini`: `GameUserSettings.ini` is where many Unreal games keep the settings
  chosen in their own menus.
- **The game's exe, by size and date**: a game update between two recordings shows too. It is not hashed: Rebirth's
  is 119 MB, past the hash limit below.
- **Each file by its SHA-256 up to 32 MB, within 256 MB and 5 s per recording**; a larger file, one past the budget
  and one that OneDrive keeps online only (hashing it would download it) are compared by size and date instead, with
  the reason kept. A folder that cannot be read leaves its section unread with the reason (DESIGN principle 2).
- **No new summary schema**: the new summary keys are optional within `lagdoctor.summary/1`, so a summary written
  before them reads as before and Compare shows "-" for them. A `/2` would make today's build ignore every newer
  summary. Revisit hook: `summary.SCHEMA`.
Revisit hooks: `PROXY_DLLS`, `LOADER_CONFIGS` and `unreal_config_bases` in `lagdoctor/capture/gamefiles.py`;
`compare_view` in `lagdoctor/app/views.py`.

---

## 2026-11-08: How the relay is built

M3 (`docs/LAUNCHER-PLAN.md`) replaced the elevated child of `watch --elevate` with the relay. The plan fixed what the
relay does; how it does it was chosen by the implementing session, and the owner has not weighed in.
- **Run PresentMon and the ETW session in the relay, and everything built on them in the recorder (chosen)**: the
  relay only filters and forwards, so the least code runs with administrator rights, and the flow tracker, the frame
  storage and the analysis run in the user's account.
  - **Run the flow tracker in the relay and send its results**: rejected. More code with administrator rights, and the
    tracker's DNS self-test would then test the relay's traffic, not the recorder's.
  - **Run a whole PresentMonSource in the relay and send its result at the end**: rejected. The relay would hold a
    second copy of every frame (about 0.7 KB each), and a long recording's frames would come over only at the stop.
- **Forward parsed rows, only the columns the recorder reads (chosen)**, about 150 bytes a frame, through
  `PresentMonSource(forward=)`, which keeps none. Forwarding PresentMon's CSV lines: rejected, since the relay must
  parse them anyway to forward only the game's.
- **pythonw beside the base interpreter (chosen)**: no console window, and the process `launch_elevated` returns is
  the relay itself, so its pid is the pipe server's. PresentMon then has no console to share and stops by its session
  name, a path PresentMonSource already had.
  - **python.exe with a hidden console**: rejected. Whether a console started elevated stays hidden when Windows
    Terminal is the default terminal is not known, and a venv's python.exe is a launcher whose pid is not the relay's.
- **Check the pipe's owner as well as the server's pid (chosen)**: an owner of Administrators proves an elevated
  process made the pipe (an unelevated token is refused it, error 1307), and the pid proves it was the relay launched.
  Stage 4's service will check its own SID.
- **A relay that cannot record ends the watch with the reason (chosen)**, as the elevated child's failures did, so a
  recording never runs on without the frame timing the player asked for. Recording on without it: deferred, as the
  backlog's "ask before recording without frame timing".
Revisit hooks: `client.relay_python`, `client.launch`'s checks, and `Relay._on_event` in `relay/server.py`.

## 2026-11-08: The side workflows' filters follow their imports, and the check scripts run on one time budget

The rest of the same CI review; the owner said "Yes, fix them". The implementing session chose how.
- **Each side workflow's `paths:` lists every file its code imports, by name (chosen)**, kept equal to that list by
  `tests/test_side_workflows.py`, which follows the imports from the scripts the workflow runs, lazy ones included.
  PR #6 had changed four modules the experiments run as a service and started no experiments run.
  - **`lagdoctor/**` for the experiments**, since they copy the whole package: rejected, they run only what they
    import, and every lagdoctor pull request would start a 4-minute run.
  - **Name only the scripts, as before**: rejected, that is the gap.
- **One time budget per check script, taken when its checks start (chosen)**: each command's timeout is cut to what
  is left, and the rest report "not run" once it is spent, so a hang still prints every verdict before the job's cap.
  `tests/test_ci_budgets.py` holds each budget, what the script does outside it and the job's earlier steps under
  `timeout-minutes`. Per-command timeouts also came down to measured sizes: msiexec 900 to 300 s (it took under 2 s),
  a command 300 to 120 s.
  - **Only lower the per-command timeouts until their sum fits**: rejected, the sum breaks again when a check is
    added, and a test would have to count calls.
  - **Raise the job caps instead**: rejected, a hang would then cost up to an hour before any verdict.
- **Dependabot for the pinned actions**: not added; it would open pull requests unasked. Revisit hook: a
  `.github/dependabot.yml` for `github-actions`, plus the repository setting that requires SHA-pinned actions.

## 2026-11-08: CI tests the Python the installer ships, and skips pushes that change only docs

The owner asked why CI ran two Python versions and said docs changes should not run everything. A review of every CI
job so far (88 test jobs on 3.11, over 85 runs), checked by a second reviewer, answered both; the owner chose the
one-version option ("do A").
- **One Python, the one the MSI ships (chosen by the owner)**: `requires-python = ">=3.13"`, a matrix of `["3.13"]`
  and a committed `.python-version` of 3.13; when `scripts/runtime_pin.py` moves to a new minor version (3.13 now
  gets security releases only, with no more Windows binaries), that version joins the matrix in the same change.
  `tests/test_python_versions.py` holds the four together. The 3.11 leg never caught a 3.11 incompatibility in the
  product: its reds alone were flakes and one test bug (a handle count that needed `gc.collect()` on 3.11), and it
  caused 5 of the 6 reruns where only one leg went red. Nothing is published, so no 3.11 user is known, while a new
  checkout here built its venv on uv's own 3.12, which CI never ran.
  - **Keep 3.13 and 3.11**: rejected, it guarded a promise no known user relied on and doubled the flake noise.
  - **A quick 3.11 check instead of the suite** (the wheel job on 3.11, or compiling and importing every module):
    rejected, it keeps a promise nobody uses at lower coverage. Revisit hook: the `wheel` job in `ci.yml`, as a
    matrix, if a user on an older Python appears.
  - **An upper bound on `requires-python`**: rejected, it would block the planned move to the next runtime.
- **Pushes to main that change only docs start no run (chosen)**: the push trigger's `paths-ignore` of `docs/**`,
  `CLAUDE.md` and `.github/SECURITY.md`. No job reads them, and `tests/test_ci_triggers.py` scans everything the jobs
  run to keep that true. 20 of the 42 pushes to main before this were such pushes, about 248 of ci.yml's 960
  job-minutes. `README.md` stays a trigger: uv_build reads it into the wheel's metadata, so a deleted or non-UTF-8
  README fails the build.
  - **Filter pull requests too**: rejected. GitHub filters a pull request on its whole diff, none of the 18 so far
    was docs-only, and a path-skipped check would stay Pending if one were ever required.
  - **A `changes` job with an `if:` on each job**: deferred, worth it once a check is required (Backlog).
  - **`[skip ci]` in docs commits**: rejected. Every agent would have to remember it, and on a pull request it
    leaves the head commit with no checks, which reads as a merge conflict (CLAUDE.md).
  - **An extension glob such as `**/*.md`**: rejected, it would also skip `tests/fixtures/presentmon/README.md`,
    which a test reads.
- **No concurrency group (kept)**: cancelling superseded runs would have cancelled the one-at-a-time sabotage pushes
  whose verdicts red-first work reads, and main's per-commit verdicts; the waste it would have saved was about 27
  job-minutes in two days.

## 2026-11-08: M1 reads each session's saved diagnosis, and the Start-menu shortcut is keyed by an HKCU value

Choices the implementing session made while building M1 (the app with your results); the owner has not weighed in.
- **Results read the session's `diagnosis.json` back (chosen)**: the screen shows what the report showed, and a long
  capture is analysed once. The capture is analysed again only when that file cannot be read.
  - **Analyse every capture again when Results opens**: rejected for M1. It shows the current build's analysis of an
    old recording, but costs the analysis time each time (an open question for long captures, research note
    section 10). Revisit hook: `App.load` in `lagdoctor/app/main.py`, the one place that picks.
- **The summary carries the numbers and the conditions too (chosen)**, beyond the plan's who and when (game, exe,
  start, tool version, kind): the library, a game's page and Compare then read no capture.
  - **Load the capture's inventory for Compare**: rejected. A capture is tens of MB; Compare would wait on two.
- **Frame times in a shared buffer (chosen, as the plan says)**: little-endian doubles, times then frame times.
  - **JSON, downsampled to the chart's width**: rejected for M1. Zooming in would then ask the app again for each
    view. Revisit hook: `views.frame_bytes` and the page's `sharedbufferreceived` listener.
- **The shortcut's component keyed by an HKCU value (chosen)**: measured with WiX v7.0.1's own ICE database on
  small per-machine test packages, it is the only key path that passes with no ICE suppressed (HKMU fails ICE57; HKLM
  fails ICE38, ICE43 and ICE57; a file fails ICE43 and ICE57; the folder fails ICE18, ICE38 and ICE43). It leaves
  one DWORD in the installing account's hive, which the uninstall removes; another administrator's uninstall would
  leave it.
  - **An advertised shortcut with `DISABLEADVTSHORTCUTS=1`**: also passes, but needs `pythonw.exe` taken out of the
    harvested files into a component of its own. Deferred, worth trying if the HKCU value ever matters. Revisit
    hook: the `StartMenuShortcut` component in `installer/lag-doctor.wxs`.
- **Tests drive the real window through `ExecuteScript` in a child process (chosen)**: WebDriver for WebView2 is
  broken since runtime 133, and a child keeps the window's COM apartment and any crash out of pytest's process.
  Revisit hook: `tests/app_harness.py`.

## 2026-11-08: The relay passes back per-program byte totals from the user's own Windows session

The relay (`docs/LAUNCHER-PLAN.md`, M3) runs the Kernel-Network session with administrator rights, and Stage 4's
capture service will serve any local caller, so it must not hand a caller other people's traffic. Today's elevated
child sees every program's traffic and names the one that filled the connection (`bandwidth_hog`). Put to the owner
with per-program totals recommended; the owner chose them ("do A) per-program byte totals, from your own Windows
session only").
- **Per-program byte totals, from the caller's own Windows session (chosen)**: the relay forwards the game's
  network events in full (plus the recorder's own, for the DNS self-test) and, for every other program, only bytes
  received and sent, and only for processes in the recorder's session, judged from its own handle-free snapshot.
  `bandwidth_hog` can still name a program of the player's; services run in session 0, so ruling a download out also
  needs the adapter's own counters, which count every program together.
- **The game's traffic only**: rejected by the owner. `talkers` would be None, and `bandwidth_hog` could only say
  that something filled the connection, never what.
- **Every program's traffic, as the elevated child sees it today**: rejected. The service would show one user the
  traffic of another user's programs and of every service.
Revisit hooks: the relay's per-program counter (`lagdoctor/relay/`) and `rules._all_traffic_quiet`.

## 2026-11-08: M0's step 2 is rebuilt before a third run, and CI's rehearsal types its keys

The owner's first run of the window spike passed everything but step 2 (keys and right-click), which saw no
right-click. Asked whether to rerun it as it was or fix the step first, the owner reran it ("Reran it, check it"); the
second run saw four right-clicks and only four keys. Asked again, the owner answered "Fix step 2 first". The rest was
chosen by the implementing session.
- **Rerun as it is (the owner's first answer)**: cheapest, and the first run's record fitted a step left half done.
  It did not complete: each run missed a different part, and the step could not say why.
- **Fix step 2 first (the owner's second answer)**: each action is a line that ticks off as it arrives; a Ctrl key
  counts when the page or the view's key event saw it; the step keeps every key press, the page's keys and the
  window's focus and menu messages, and says what followed Alt+Left. Its bar stays where it was: 6 of the 7 Ctrl keys
  and a right-click. Counting Alt+Left as well made it looser, because the window's event always sees it and it can
  show no effect here, so it is reported, never counted.
- **Count the two runs together** (the first saw seven keys, the second the right-click): rejected. It turns the gate
  from one passing run into two partial ones, and leaves Alt+Left unexplained.
- **CI's rehearsal types step 2's keys with SendInput on GitHub's runner (chosen)**: the owner's own step runs on real
  input before the owner's third run, under a check of its own that can fail. Only there: `scripts/stage2/guard.py`,
  checked again inside `type_keys`.
  - **A separate automated keys check in `--ci`**: rejected. It would test other code than the owner runs.
- **Keeping Alt from starting Windows' menu mode now**: deferred, worth trying (Backlog), once a run shows what Alt+Left
  does.
Revisit hooks: `KEYS_NEEDED` and `COUNTED_KEYS`, `type_keys`, and `on_message`'s `WM_SYSCOMMAND` in the spike.

## 2026-11-08: The HTML report shows profile folders by their variable; the terminal keeps real paths

The report is meant to be shared, but its footer showed the capture's path, which holds the Windows user name
(PR #17). The implementing session chose; the owner has not weighed in.
- **Rewrite in `html.e()`, which every string on the page passes through (chosen)**: it covers the footer, the
  game's exe, a reason quoting PresentMon's path, evidence, and any text added later.
- **Rewrite only the footer and the Game file row**: rejected. The other strings, and the next new one, would
  still carry the name.
- **Rewrite the finished page**: rejected. After HTML escaping, a name with `&` or `'` no longer matches its folder.
- **A profile this PC's variables do not name shown as `%USERPROFILE%` (chosen)** rather than as
  `C:\Users\<user>`: the command stays runnable. It names the wrong folder only for a capture kept in another local
  account's profile.
- **The analyze command for PowerShell, `"$env:LOCALAPPDATA\..."` (chosen)**: `%LOCALAPPDATA%` is literal inside
  PowerShell's quotes, and `(Join-Path $env:LOCALAPPDATA '...')` is longer to read.
- **The terminal keeps real paths (chosen)**: its report path is pasted into a browser, which does not expand
  `%LOCALAPPDATA%`, and the CLI prints the same paths around it. Neutral there too: deferred, worth trying
  (Backlog).
Revisit hook: `user_neutral` and `powershell_path` in `lagdoctor/report/html.py`.

## 2026-11-08: A desktop app that launches the games, drawn by WebView2 in lag-doctor's own window

The owner asked for a light app that launches games with lag-doctor attached and shows results to explore. The plan
is `docs/LAUNCHER-PLAN.md`; the facts and sources are in `docs/research/desktop-app.md`. The first three forks are the
owner's, answered on 2026-11-08. The planning session chose the rest, and the owner has not weighed in.
- **A desktop app you start like Steam (chosen by the owner)**: its own icon, window and taskbar button.
  - **A local web page in an Edge `--app` window, served by `http.server`** (the session's first proposal): rejected by
    the owner ("Why a web app?"). It would also be Edge's window, with an "Open browser window" item that cannot be
    hidden and Edge's first-run screens, and missing where Edge was uninstalled. And it brings a server to harden:
    CPython's default SO_REUSEADDR lets another account bind the same port, and cookies are not isolated by port.
- **It launches the games (chosen by the owner)**.
  - **Results first, launching last or never** (the session's proposal: one click saved, for the most risk): rejected by
    the owner.
- **Plan only for now (the owner)**: nothing was built until the owner said go. They did on 2026-11-08 ("let's do the
  Launcher-Plan.md"), and M0's spike passed the owner's own run that day (`docs/LAUNCHER-PLAN.md`, Status).
- **The window: WebView2 in lag-doctor's own window, driven by its Python through ctypes (chosen; the M0 spike's gate
  passed on 2026-11-08)**. It is the launchers' own architecture (Steam draws with an embedded Chromium). No new binary
  and no server, and the stdlib-only rule holds.
  - **A small C# host (.NET Framework 4.8.1) with Microsoft's WebView2 control**: deferred, the fallback had M0
    failed; not needed since it passed. It costs a second toolchain, a signed exe and three binary-only Microsoft DLLs,
    and it would reverse 2026-11-04's "Python with ctypes, no compiled code".
  - **A C++ host on webview/webview**: deferred, the same fallback without Microsoft's DLLs; not needed either.
  - Rejected: **tkinter** (no JPEG cover art; not in the embeddable runtime), **pure Win32** (no DOM to test),
    **pywebview** (an unsigned native module), **PySide6** (size; GPLv3 charts), **Electron and Tauri** (prebuilt
    binaries, or two more toolchains).
- **WebView2's environment from the runtime's own `CreateWebViewEnvironmentWithOptionsInternal` (chosen)**: it ships no
  Microsoft binary, so SignPath's "no proprietary, non open-source component" question never arises; webview/webview
  and Wails take this path. Microsoft says the function "may be modified or removed in future versions". The owner's
  PC, whose runtime updates itself, would meet a break first, and the app then falls back to `report.html` with a plain
  message.
  - **Microsoft's `WebView2Loader.dll`**: deferred. Documented and Microsoft-signed, but without published source; it
    ships only with SignPath's written OK.
- **One relay protocol with two hosts (chosen)**: a relay elevated by one UAC prompt per recording first, and the
  Stage 4 LocalService service later. Under the prompt it takes every decision from its approved command line; the
  service checks the target against the caller's own session instead. Either way it talks over a pipe it creates.
  From M3 it replaces 2026-11-04's elevated child, which recorded in its own window and wrote the capture, for
  `watch --elevate` too.
  - **A waiting elevated child steered by files in the user's session folder**: rejected. User-writable files would
    steer an administrator's process after the prompt, and Stage 4 replaces it anyway.
  - **The app runs the Session in its own process**: rejected. A crash or a closed window would lose the recording, and
    the samplers would share the UI's interpreter.
  - **Keeping today's elevated child, prompted after the game is found**: rejected. For Play the prompt would come over
    a starting game, with a second console window; for `watch --elevate`, two child protocols would stay.
- **Measure from an in-game start key for a fixed window, with the startup shaded and trim handles in Results
  (chosen)**: recording from launch to quit folds shader preparation and menus into every statistic. The cost: the
  player must press the key, and a game that takes raw keyboard input with `RIDEV_NOHOTKEYS` may swallow it, so the
  trim handles fix a missed press.
  - **Analysing the whole recording**: rejected, the startup would weigh in every statistic, baseline and rule.
  - **Skipping a fixed startup per game**: rejected, menus last as long as the player stays in them.
  - **Detecting gameplay automatically**: rejected as a default, since menus draw too (the render gate passes them),
    so a detected start is a guess; it is only proposed, labelled as one.
- **The relay forwards only the game's network events, plus the recorder's own for the tracker's DNS self-test
  (chosen provisionally; open to the owner before M3)**: the elevated side hands over nothing about other programs.
  `watch --elevate` then stops naming a program that filled the connection, and `bandwidth_hog` falls back to the
  adapter's total.
  - **Per-program byte totals from the user's own session**: open, the owner's call (`docs/LAUNCHER-PLAN.md`, Open
    questions).
- **Play requires the store client to be running (chosen)**: opening the URI would otherwise make the client
  lag-doctor's child.
  - **Starting it through Explorer**: deferred (Backlog).
Revisit hooks: the M0 gate and the create-environment function in `docs/LAUNCHER-PLAN.md`; the relay's policy (`uac`
or `service`) and its network filter; `crop` and the start key.

## 2026-11-08: lagdoctor/ may import what the installed runtime imports, by that runtime's own answer

`tests/test_stdlib_only.py` allowed every name on the test interpreter's `sys.stdlib_module_names`, which names
tkinter, IDLE, turtle, venv and ensurepip: python.org's embeddable runtime, the one the MSI ships, has none of them
(`docs/research/install-and-rights.md`, section 10). The implementing session chose; the owner has not weighed in.
- **An allow-list the pinned runtime answers itself (chosen)**: `scripts/runtime_modules.py` unpacks the pinned ZIP,
  has its own `python.exe` look up and import every name on its standard-library list, checks the answer against the
  ZIP's listing and writes `scripts/runtime_modules.json`. The test allows only the importable modules, and CI's msi
  job asks the runtime again on every push and fails unless the JSON is its answer. It holds whichever interpreter
  runs the tests (3.11's list also names modules that 3.13 removed) and whatever a new pin leaves out.
- **A pinned deny-list of the 7 left-out modules beside `sys.stdlib_module_names`**: rejected, it still leans on the
  test interpreter's list, and a module a later pin drops would pass until someone listed it.
- **The ZIP's listing alone, without running the runtime**: rejected, the 68 built-in modules (`sys`, `winreg`, `nt`)
  sit inside `python313.dll`, which no listing shows, and a module the listing holds can still fail to import
  (`curses`).
- **Looking each module up without importing it**: rejected, `curses`, `pty` and `tty` are found and fail to import,
  as would a `.pyd` whose DLL went missing.
- **Checking whole dotted names**: deferred, worth trying if a pin ever ships a package without one of its
  submodules; this one ships every submodule the full installer has under each package it has.
- **The runtime asked in the test job**: rejected, only the msi job downloads the runtime ZIP; the tests check the
  pin the JSON records against `scripts/runtime_pin.py` instead, so a pin moved without the JSON fails everywhere.
Revisit hooks: `allowed()` and `IMPORTABLE` in `tests/test_stdlib_only.py`; `CHILD` and `disagreements()` in
`scripts/runtime_modules.py`; the msi job's "The runtime's module list is its own answer" step in `ci.yml`.

## 2026-11-08: The loopback DNS stand-in draws random dynamic ports

The self-test's tests need one loopback port number free for both UDP and TCP. The stand-ins took TCP's next port-0
number and tried UDP on it, ten times; Windows hands those numbers out in sequence, and on 9 of 10 GitHub runners
Docker's NAT holds 200 UDP ports in a row that TCP still hands out, so at 1.2 % of the hint's positions every try
failed. The implementing session chose; the owner has not weighed in.
- **One stand-in, `tests/loopback_dns.py`, tries up to 64 random numbers from the dynamic range (chosen)**: the tries
  are independent, and 2.3 % of dynamic ports were refused on the runners (15 % on a laptop with Hyper-V's blocks),
  so all 64 refused is out of reach; each refusal is named in the error.
- **More tries in sequence**: rejected, an excluded block is 200 ports on the runners and 800 on that laptop, and no
  count is long enough everywhere.
- **UDP's next port-0 number first, then TCP on it**: rejected, Docker's NAT holds 200 TCP-only ports too, and this
  order failed at the same 1.2 % of positions on the runners (7.7 % on the laptop, against 7.5 % for TCP first).
- **Random numbers below the dynamic range** (20000 to 40000, free for both 100 % of the time on the runners):
  rejected, fixed-port services live there, and a test holding one could stop one starting; the dynamic range is
  where short-lived ports belong.
- **Reading the excluded ranges and skipping them**: rejected, it parses netsh's localised text, and still misses
  ports another program holds.
- **Separate port numbers for UDP and TCP**: rejected, the tracker takes one self-test port for both, as DNS uses 53.
Revisit hook: `random_ports()` and `TRIES` in `tests/loopback_dns.py`.

## 2026-11-08: The MSI: WiX v7 under its EULA, a .cmd shim on PATH, the wheel's code with its bytecode

Stage 3's per-machine MSI (`docs/INSTALLER-PLAN.md`; built by `scripts/msi_build.py`, checked on CI's runner by
`scripts/msi_check.py`). The first two forks are the owner's, asked on 2026-11-08; the rest the implementing session
chose, and the owner has not weighed in.
- **WiX v7, its Open Source Maintenance Fee EULA accepted for CI's builds (chosen by the owner)**: `-acceptEula wix7`
  on each build, written once in `ci.yml` (`tests/test_msi.py` holds it there). The EULA (v1.1, read in full) asks a
  fee only of users who use WiX in revenue-generating work and have US$10,000 or more a year in gross revenue; it is
  not a licence fee, binds nothing on the MSIs built or the people who install them, and yields to WiX's MS-RL
  licence. **WiX v5** (MS-RL only, nothing to accept): rejected, FireGiant's security fixes for it ended on
  2026-02-05. **WiX v6** (EULA v1.0, no switch): rejected, the same kind of EULA binds by use, unsaid. **WiX v7 built
  from source** (self-compiled binaries need no EULA): rejected, a .NET build of WiX on every run for nothing gained.
- **`lagdoctor` on PATH through `bin\lagdoctor.cmd` (chosen by the owner)**: it runs the PSF-signed `python.exe`;
  App Control does not check batch files, so nothing new needs signing or can be blocked. Its costs: after Ctrl+C
  stops a recording in that window, cmd asks "Terminate batch job (Y/N)?" (rare before Stage 4: recordings run in
  their own administrator window), and cmd reads `&`, `^` and `%` in unquoted arguments. **A launcher .exe built in
  CI from our own C source**, as pip and uv do it: deferred, worth trying at Stage 4, when every recording runs in
  the user's own window (no prompt, exact arguments, signable by SignPath). **A prebuilt launcher** (distlib's
  `t64.exe`, uv's trampoline): rejected, SignPath signs only binaries built from the project's own source, so Smart
  App Control would block it for good. **The Python Install Manager's renamable, PSF-signed launcher**: deferred, it
  needs the runtime's absolute path written at install time, and its signature and `sys.executable` are unchecked. **A
  `.ps1` shim**: rejected, Windows clients refuse scripts by default (execution policy). **The install folder itself
  on PATH**: rejected, its `python.exe` would stand in for the user's own `python`.
- **The code: the wheel's `lagdoctor\`, with unchecked-hash `.pyc` files the shipped runtime compiles (chosen)**:
  Python then never writes a `__pycache__` beside them. An administrator's run (the elevated recording, an admin
  terminal, CI) would otherwise write one into Program Files that the uninstall leaves behind. **`.py` only, with `-B`
  on every launch**: rejected, each launcher (the shim, the elevated child, the Stage 4 service) would have to
  remember it. **A ZIP of the package on the `._pth`**: rejected, `cli.PROJECT_ROOT`, the elevated child's working
  folder, would be the ZIP, and the `._pth` E3c measured would change. **Timestamp-checked `.pyc` files**: rejected,
  Python rewrites one whenever the installed `.py`'s time differs from the one recorded. **Copying `lagdoctor\` from
  the checkout**, as `drive.py` does: rejected, the MSI would not ship what the wheel job checked.
- **PresentMon: the installer's copy beside the code first, and a damaged one reported (chosen)**: `setup.bundled()`,
  under Program Files in an install; a checkout or a wheel has none. **Falling back to the per-user download when the
  installer's copy fails its check**: rejected, that routes around a damaged install, which reinstalling fixes.
  **Looking in `%ProgramFiles%\lag-doctor` wherever the code runs**: rejected, a checkout would then depend on an
  install being there.
- **Two builds of one version replace each other (`AllowSameVersionUpgrades`, ICE61 suppressed) (chosen)**: CI makes
  many builds of 0.1.0. **Windows Installer's default**: rejected, two such builds install side by side, sharing files.
Revisit hooks: `--accept-eula` in `ci.yml` and `WIX_VERSION` in `scripts/wix_tool.py`; `SHIM` in
`scripts/msi_build.py` and the `bin\` harvest in `installer/lag-doctor.wxs`; `compile_bytecode` in `msi_build.py`;
`setup.bundled` and `ensure_presentmon`.

## 2026-11-08: The real-ETW tracker test takes its stream in whole

Its private ETW session hands every event over inside `stop()`, while the tracker's live loop finalised the timeline up
to its 2 s reorder window before now: the first second of the stream came late (status partial in every run), and
whether the server was known before the planted gap hung on where whole seconds fell. The implementing session chose;
the owner has not weighed in.
- **The test gives the tracker a window longer than the run and asserts that no packet came late (chosen)**: the fix
  sits where the mismatch is (only this test drives the tracker over a private session), product code is untouched,
  and putting the 2 s window back fails every run instead of now and then.
- **The tracker stops finalising live when its session is private**: rejected, a branch no production session can
  reach, written for one test.
- **Reshaping the stream to fit inside 2 s (a shorter run, the gap earlier)**: rejected, its margin would again hang on
  how fast a runner starts the session and how long the stop takes.
- **Wider bounds, or a retry**: rejected, both would also pass a tracker that misses a real gap.
Revisit hook: `tr._table.reorder_s` in the test; a `FlowTracker` argument if a second test ever drives it over a
private session.

## 2026-11-08: LagLab's pyglet is a dependency group, not an extra

With psutil moved to the dev group, the wheel's metadata still offered a `laglab` extra that installs pyglet, though
the wheel ships no `laglab/`. The implementing session chose; the owner has not weighed in.
- **A `laglab` dependency group (chosen)**: groups never reach the wheel's metadata, and `uv sync --group laglab`
  keeps the step that adds pyglet for the tests separate, as the README had it.
- **Keep the extra**: rejected. `pip install lag-doctor[laglab]` would install a graphics library for a package that
  is not there.
- **pyglet in the dev group**: rejected. Every `uv sync`, a checkout user's included, and the wheel job's checker
  environment would install it for nothing.
Revisit hook: `[dependency-groups]` in `pyproject.toml`.

## 2026-11-07: How the first capture's leftovers were fixed

The four things the review noticed while its items were fixed (`docs/FIRST-CAPTURE-REVIEW.md`); PR #11 fixed them.
The implementing session chose at each fork; the owner has not weighed in.
- **The CPU's cause label: one wording true for every way the cause is named, "the CPU running below full speed"
  (chosen)**. **"The CPU slowing down"**: rejected, a clock that stayed low all along never slowed down. **A label
  picked per mark from the part that fired**: deferred (Backlog).
- **The save-speed test: the two saves in turns, each at its best of three (chosen)**: one estimator for both, and a
  slow stretch lands on both. **CPU time instead of wall time**: rejected, Windows counts it in 15.6 ms ticks (a fifth
  of the new save) and it cannot see a wait a player feels. **Repeating the comparison until one passes**: rejected,
  an intermittent real slowdown would pass. **Only listing it as a flake**: rejected as the only step, since the
  comparison itself was lopsided.
- **The byte-order mark the new character check found: a console form of its own, nothing (chosen)**: one in a
  capture's text would print "?" too. **Exempting the literal `gpumon` strips**: rejected, a hand-kept exemption list
  is the kind of list the check replaces.
Revisit hooks: `labels.CAUSES` and `diagnose.mark_list`; the loop in
`test_the_save_takes_a_fraction_of_the_old_saves_time`; `console._PUNCT`.

## 2026-11-07: How the first capture's analysis defects were fixed

The review of the first real capture (`docs/FIRST-CAPTURE-REVIEW.md`) listed 11 analysis defects; PRs #7 and #9 fixed
them. The implementing session chose at each fork; the owner has not weighed in.
- **No frame timing: keep the verdict `unknown` and let a GPU-side finding through where the engine was pegged
  per second (chosen)**: CPU-side causes keep passing their gate. **Switching the verdict to `gpu` when the engine
  is pegged for part of the capture**: rejected, since every CPU-side cause would then be cleared by its gate ("not
  what limits the frame rate") on a capture limited by the GPU only in its last minutes.
- **A MAD of 0: the mean absolute deviation × √(π/2) as the spread (chosen)**: it keeps the z-score's meaning on
  normal data. **A quantile of the quiet windows (say p95) as the threshold**: rejected, a second scale beside the z
  that every floor test would need to state.
- **The lift test's chance: runs of consecutive quiet 1 s windows as long as each event's window (chosen)**: exact
  against the per-second activity, one pass per length. **Re-judging quiet windows of every length with
  `_evaluate`**: rejected, seconds per length on a long capture. **`1 - (1 - b)^k` from the 1 s share**: rejected,
  disk reads come in bursts, so seconds are not independent.
- **Idle NVIDIA readings: left out of the heat share (chosen)**: a GPU throttled every time it works reads 100 %
  however long it idled. **Counted as 0**: rejected, it read such a GPU as "held down only 20 % of the time".
- **The commit limit with RAM free: the same `ram_pressure` cause, titled for the page file (chosen)**: no new rule.
  **A cause of its own**: deferred (Backlog).
- **A mark's causes: keep every one that moved (chosen)**. **Re-ranking ties at `Z_CAP`**: deferred (Backlog).
Revisit hooks: `frames.probable_gpu` and `rules._probable_gpu`; `attribute._Baseline`; `attribute._expected`;
`attribute.throttle_readings`; `rules._ram_pressure`; `diagnose.mark_list`.

## 2026-11-07: uv_build makes the wheel, and `uv sync` installs the project

Stage 3 needed a build backend and a `lagdoctor` console entry point (`docs/INSTALLER-PLAN.md`); until then
`pyproject.toml` had `package = false` and no backend. The implementing session chose; the owner has not weighed in.
- **uv_build, bounded to the pinned uv's minor version (chosen)**: the uv that CI pins carries a copy, so
  `uv build --offline` needs no download; the flat layout takes two keys (`module-name = "lagdoctor"`,
  `module-root = ""`); the wheel holds that package and its metadata, and the sdist adds only `pyproject.toml`, the
  README and the licence. The cost: the bound must move with CI's `version:` (`tests/test_wheel_check.py`).
- **hatchling or setuptools**: rejected. Either is one more package for uv to download before it can build, and
  this pure-Python package needs nothing from them that uv_build lacks.
- **Keep `package = false` and build the wheel only in CI**: rejected. A checkout would have no `lagdoctor` command, so
  the README would keep `python -m`. The editable install it brings (a `.pth` naming the checkout's root) was measured
  not to change what red-first runs test (`collector_base.json` 4 of 4 caught before and after). It did shadow the
  harness's scratch package, which has an `__init__.py` now, and a file saved in the root mid-run read as UNISOLATED,
  so the probe takes the real tree off sys.path (learnings).
Revisit hook: `[build-system]` and `[tool.uv.build-backend]` in `pyproject.toml`.

## 2026-11-07: The capture service gets an unrestricted service SID

The plan wanted a restricted service SID on the LocalService capture service if E3 passed with it. E3 ran the
throwaway service both ways on a GitHub runner (`docs/research/install-and-rights.md`, section 8). The implementing
session chose from the measurement; the owner has not weighed in.
- **Unrestricted service SID, privileges trimmed to SeChangeNotifyPrivilege (chosen)**: every provider enables, the
  tracker and PresentMon run, and the service SID still names the service in ACLs (its pipe, its folder).
- **Restricted service SID**: rejected. The write-restricted token's second SID list (the service SID, Everyone, the
  logon SID, WRITE RESTRICTED) grants nothing on the ETW descriptors, so `StartTraceW` refuses every session, and
  PresentMon cannot create its message-only window (exit 4).
- **Restricted SID plus trace GUIDs of our own whose descriptors name the service SID**: rejected. lag-doctor's own
  session could be granted that way at install time, but PresentMon's session, its window and the providers' own
  descriptors could not.
Revisit hook: `create_service` in `scripts/stage2/drive.py` (`sidtype`) and the WiX `ServiceInstall` of Stage 4.

## 2026-11-07: CI caches the PresentMon download, keyed on the code that downloads it

CI was to get PresentMon through `lagdoctor setup --download`, because no test runs the real download (they replace
the fetcher), and to cache it; a cache hit skips the download, so the two pull apart. The implementing session chose;
the owner has not weighed in.
- **Cache the exe under a key that hashes `setup.py`, `paths.py` and `cli.py` (chosen)**: a change to the download
  code misses the cache, so that pull request's CI runs the real download; otherwise setup re-checks the restored exe
  (size, SHA-256, Intel's signature). It also misses on a new pin and after 7 days without a run.
- **Download on every run**: rejected. Each run would depend on GitHub's release servers for 1 MB that never changes.
- **Key on the pin alone**: rejected. The real download would run once, then not again while the cache lasts.
- **A weekly run without the cache**: deferred, worth trying (Backlog).
Revisit hook: `DOWNLOAD_CODE` and `ci_outputs` in `tests/downloads.py`.

## 2026-11-07: How the history went public

Before the first push, 60 of the 70 commits carried the owner's Windows user name (folder paths in 4 research
notes) and the name of the owner's machine-learning job (research note `install-and-rights.md`, section 7).
- **Rewrite the old commits, then publish (chosen by the owner)**: every commit kept with its author and dates;
  only the commit ids changed, before anyone outside had them.
- **Publish the history as it is**: rejected. No work, but the details public for good.
- **One fresh commit of today's files**: rejected. Simplest and leak-proof, but the public repo would lose the
  history of how lag-doctor was built.
- **Private first**: rejected. CI on limited minutes, and signing and winget need a public repo, so the question
  only comes back.

How it was done: three passes of git's own `filter-branch`, each swapping the affected file versions for scrubbed
ones. The first removed the user name and the job's name, and dropped the scrub commit, which had nothing left to
change. A five-angle pre-publish review (personal data, secrets, what the push sends, the rewrite, licensing) then
found what the first pass had not looked for, and further passes, the agent's call within the owner's choice,
removed it too: the job's script name; this PC's adapter and power-plan GUIDs; the home network's addresses (now
`192.168.0.x`) and a virtual adapter's; other software named as installed here, and test fixtures that listed
exactly this PC's adapters; the Steam log's dates, times and AppIDs; the time in the first capture's folder name;
and the owner's first name as an example account in old tests. A fresh-eyes recheck after the second pass found
the last of these. Three commit messages that cited old commit ids now cite the new ones. A script checked the
result against the original commit by commit: author, dates and parents unchanged, messages unchanged apart from
those ids, only the swapped files differ, and nothing personal is left in any file, path, message or author field.
Kept on purpose: game names where they are the tool's subject (the first capture's game, the README's example),
stock launchers and OEM software, download rates, a generic mention that a machine-learning job sometimes uses the
GPU, clock times that tie to nothing personal, the internal panel's model-level device path, the time zone in the
commit dates, and the author's name.
Revisit hook: none. The pre-rewrite history stays in local refs (`refs/backup/`, `refs/original/`, the merged
`worktree-*` branches), so a local `.git/hooks/pre-push` refuses any commit that holds those details.

## 2026-11-06: How other people get frame timing without prompts

Windows lets only administrators, members of Performance Log Users and the LocalSystem, LocalService and
NetworkService accounts control ETW, so one administrator approval is the floor (`docs/research/install-and-rights.md`).
This takes up two former backlog items: the elevated helper that relays PresentMon rows, and elevating only code
the user cannot write.
- **A signed per-machine MSI that installs a capture service (chosen)**: one UAC prompt at install (an
  administrator's password on a standard account), then nothing per recording and no sign-out, and the user's own
  account gains nothing. The service runs as LocalService with its privileges trimmed, starts when lag-doctor opens
  its named pipe, and records only the requested process in the caller's own session. It is the most work and the
  project's highest-stakes code, so it is built in stages behind experiments (`docs/INSTALLER-PLAN.md`).
- **Per-recording UAC (`watch --elevate`) and the printed Performance Log Users steps**: kept, as the fallback for
  git checkouts and PCs without the service.
- **lag-doctor joins the account to Performance Log Users behind one UAC prompt (as Xbox Game Bar does)**: rejected.
  It still needs a sign-out, it gives the account and everything it runs machine-wide trace rights, and the service
  makes it unnecessary. lag-doctor still never changes group membership itself (2026-11-04).
- **Intel's PresentMon service through its API**: rejected. Frame timing only, a resident LocalSystem service whose
  pipe admits all authenticated users (v2.6.0), coupling to Intel's API, and a 170 MB install.
- **An elevated scheduled task**: rejected. It is the UAC-bypass pattern Administrator protection is built to remove,
  and it prompts nothing only on administrator accounts.
- **MSIX**: rejected. Its AppData virtualization is the private-copy failure of 2026-11-05, and a packaged service
  needs restricted capabilities.
- **A frozen exe (PyInstaller, Nuitka)**: rejected. Antivirus false positives that those projects do not fix, and no
  rights gained.
- **`uv tool install` from PyPI**: deferred, worth trying as a developer channel (backlog).
Revisit hook: the service's pipe protocol and the rights branch of `cli.cmd_watch` (`elevation_choice`).

## 2026-11-06: MIT licence; code signing through SignPath Foundation

Smart App Control blocks unknown unsigned code and allows no exception for a single app, so other people's PCs need
a trusted signature on the installer and on every binary in it.
- **SignPath Foundation (chosen)**: free for open-source projects; the signature names SignPath Foundation as the
  publisher. Its conditions (signpath.org/terms): an OSI licence and no proprietary component; the project "already
  released in the form that should be signed"; "a certain verifiable reputation" for downloadable programs; binaries
  built verifiably by the repository's CI; multi-factor authentication for every team member; a code signing policy
  and a privacy statement on the project's home page; manual approval of every signing request. So the repo goes
  public and an unsigned release comes first.
- **Microsoft Artifact Signing**: rejected. About US$10 a month, and open to individuals only in the US or Canada.
- **An OV certificate**: rejected. About US$150 to 300 a year plus a hardware token.
- **Licence: MIT (chosen)**, the same as PresentMon's and OSI-approved, as SignPath requires.
Revisit hook: the signing step of the release workflow. If SignPath declines (its reputation condition), releases
stay unsigned and the README says what SmartScreen and Smart App Control will show.

## 2026-11-05: Find a private copy of AppData by where a new file lands

PresentMon set up from the agent's shell inside the Claude desktop app landed in that app's private folder
(`...\Packages\Claude_pzs8sxrjxfjjc\LocalCache`), so the user's terminal and the elevated window said "not
installed", and a 14-minute recording of a real game had no frame timing.
- **Ask Windows where a new file lands (chosen)**: a hidden, delete-on-close probe and its
  `GetFinalPathNameByHandleW`; a final path inside `\Packages\<app>\LocalCache\` that was not asked for is a
  private copy. It caught the measured case and stays silent in the user's own terminal (both checked live).
- **Package identity (`GetCurrentPackageFullName`)**: rejected; the redirected shell answered
  APPMODEL_ERROR_NO_PACKAGE.
- **Keep lag-doctor's files outside AppData**: rejected; every other window would still look in AppData, and
  `LAGDOCTOR_HOME` already moves them for whoever wants that.
- **Refuse what saves for other windows, note it for what only reads (chosen)**: `setup`, `watch`, `fix` and
  `undo` refuse; `check` and `analyze` read the PC correctly and only note it. Refusing everything was rejected
  (no reason to), and a warning before going on was rejected too: a line in front of a recording is exactly
  what that recording lacked.
Revisit hook: `cli.PRIVATE_COPY_POLICY`, `paths.redirected`.

## 2026-11-05: No recording without frame timing unless asked for

- **Stop before looking for the game, exit 6, unless `--no-elevate` (chosen)**; a recording without frame
  timing says so before "Recording.", and the elevated child, opened only for frame timing, records nothing
  when its PresentMon fails its check.
- **Record everything else and say so in the report (the old behaviour)**: rejected; it cost the user a
  14-minute session of the game they meant to diagnose.
- **Ask first**: deferred, worth trying (backlog).
Revisit hook: the not-ready branch at the top of `cli.cmd_watch`.

## 2026-11-05: A power plan is judged by its settings, not its name

The first real report put "Power saving is holding the PC back [likely]" first and offered a switch to
Balanced. The ASUS plan "Silent" set the CPU exactly as Balanced does (`powercfg /qh`: 180 of 182 settings
equal, the others screen brightness and the battery hibernate timer), and "likely" came from GPU heat.
- **The active plan's own processor settings (chosen)**: maximum processor state, boost mode and cooling
  policy for the power source in use (`inventory.power.plan`); the switch to Balanced is offered only for a plan
  that holds the CPU back.
- **Its name**: kept only for captures without the settings, as whole words ("eco" had matched "ASUS
  Recommended"); rejected as the test itself.
- **GPU heat as evidence for the plan**: rejected; a Windows plan sets no GPU limit, and heat is
  `gpu_throttle`'s evidence.
- **Armoury Crate's quiet mode read through the plan it switches to (chosen, after review)**: the first version
  said the maker's mode was not readable and dropped its advice, so this laptop's next report would have cleared
  "Silent" as fine. The System log has 163 switches of the active plan by ArmouryCrate.UserSessionHelper.exe
  between its Silent, Performance and Turbo plans (the last to Silent on 2026-11-04 14:54), so a quiet-named plan
  while Armoury Crate runs is its quiet mode: its own cause, the GPU's power limit held below its default as
  evidence, and Armoury's setting as the fix. Switching the Windows plan instead is not offered, since it would not
  change the fan and power mode Armoury sets.
- **The performance-core cap** (`PROCTHROTTLEMAX1`, Windows' Power saver: 75 %) counts on hybrid CPUs (found in
  review). Energy-performance preference (`PERFEPP`) is not read: a bias, not a cap.
Revisit hook: `rules._plan_held_back`, `rules._oem_quiet_mode`.

## 2026-11-04: Stutters on a display that only changes on a refresh

At 120 FPS on a 144 Hz panel the display alternates 1- and 2-refresh holds, so the trailing median of
displayed intervals (LC 2.4's baseline) is one refresh (6.94 ms). One late OS wake-up then holds a frame for
3 refreshes (20.8 ms), which passed "2.5x the median and 8 ms above it" though it is only 6.9 ms longer than
the cadence's own regular 2-refresh hold.
- **On a quantised series, measure the excess against the longest regular hold (chosen)**: a stutter needs
  D >= 2.5 M and D - max(M, E) >= 8 ms, with E = ceil(Mp/P - slack) x P from the presented cadence Mp and the
  refresh period P (slack 0.005, see 2026-11-05 below). A 4-refresh hold and every real hitch still count; a
  3-refresh slip does not.
- **CapFrameX's mean baseline**: would sit at 8.33 ms; rejected because LC 2.4 chose the median for its
  robustness to the very spikes it measures.
- **Allow a few stutters in the smooth control**: rejected; it would hide a misleading verdict from users.
  The end-to-end judge stays strict and now also requires lag type "none" for the control.
Revisit hook: `analysis/events._stutters`.

## 2026-11-04: Low frame rate means below 80 % of the target

- **80 % of the target (chosen)**: LC 2.4's low-FPS episode and CHI '23 (experience stays acceptable while
  the 5th-percentile FPS is within about 80 % of the target).
- **0.9 x refresh**: what the first build used, borrowed from LC 4.14's cap rule; rejected, it made a
  GPU-bound 120 FPS game on a 144 Hz panel "low frame rate" for the whole capture.
- **Judged on the steady rate (chosen)**, the frame rate outside stutters and FPS drops: an average that
  includes six 600 ms freezes made a steady 120 FPS game read "Low frame rate". The freezes are named instead.
- **The target is the refresh rate for every verdict except a cap or a VSync lock (chosen)**, "unknown"
  included: the frame rate is measured even when the limiting side is not. A regular cadence while other
  programs share the game's GPU is not the game's own cap, so it counts too. Rejected: counting only
  hardware-limited verdicts, which hid VSync-halved and "waiting" games.
Not tuned above 120 Hz: CHI '23 validated the 80 % rule for 60–120 Hz only. Revisit hook:
`analysis/diagnose.lag_time`, `LOW_FPS_FRAC`.

## 2026-11-05: Dropped frames above a fixed refresh are their own cause

A game presenting faster than a fixed-refresh screen (86 FPS on 60 Hz) drops frames, and its animation
judders although every displayed interval is one refresh (PresentMon's gold test_case_5).
- **Its own cause, `dropped_frames`, claiming those pacing events and counting as pacing lag (chosen)**,
  advising a cap at the refresh rate, VSync or VRR.
- **Under `vsync_judder`**: rejected; the headline then said "VSync judder" where VSync is not involved.
- **Counting every cap's display judder as pacing lag**: rejected; LagLab's 120 FPS smooth control on 144 Hz
  would become "pacing" and fail its judge.
Revisit hook: `rules._dropped_judder`, `diagnose.lag_time(dropped=...)`.

## 2026-11-05: The quantised-display slack is measured jitter, not 5 %

The first stutter-rule fix rounded the regular cadence's longest hold with a 5 % slack, so caps at 137–143 FPS
on 144 Hz (141 is the commonly advised cap) still turned one-refresh slips into stutters. Any presented cadence
slower than the refresh produces regular 2-refresh holds, so the slack only has to absorb the jitter of the
trailing median present interval: on the refresh-locked LagLab model at 144 Hz with the measured OS wake-ups
(30 seeds, 125,317 frames) it strayed p99.9 +0.50 % (max +0.65 %) past the refresh.
- **0.005 (chosen)**: caps 137–143 give no false stutters (0 in 10 seeds each, against 2–8 with 5 %); a game
  locked at 144 FPS that misses two refreshes still stutters.
- **No slack**: rejected; a locked game's jitter would then hide its real 3-refresh hitches.
Residual: caps within that jitter of the refresh (about 143.3–144 FPS on 144 Hz) still turn single 3-refresh
slips into stutters (8 in 10 seeds at 143.5 in the model); telling them from a locked game would need a longer
present window than the stutter baseline. Measured on the model at 144 Hz and on gold dwm.exe at 60 Hz only.
Revisit hook: `analysis/events.CADENCE_SLACK`.

## 2026-11-05: Save captures streamed, at gzip level 1

A 30-minute 144 FPS capture took 36 s to save (`asdict`, a pure-Python dump, gzip level 9), far beyond the
4.5 s Windows gives a closing console.
- **Stream the JSON column by column with the C encoder, compressed at level 1 on a worker thread (chosen)**:
  2.6 s for the same capture, 12 MB instead of 9 MB; `Capture.load` is unchanged.
- **Keep level 9**: rejected, 24 s of the save was compression alone.
- **Checkpoint the capture while recording**: deferred (see the compact-storage backlog item); it needs every
  collector's results to be readable mid-run.

## 2026-11-04: An NVIDIA power cap is not throttling

- **Separate columns (chosen)**: `nv_throttle_pct` counts only thermal slowdown and the hardware power brake;
  `nv_power_cap_pct` holds the software power-cap share, reported as context (LC 4.1, 4.2: during GPU-bound play
  the power cap is the design limit).
- **One summed column with a name heuristic in the rule**: rejected; one idle second's `gpu_idle` reason turned
  a normal power cap into a "likely" throttling finding.

## 2026-11-04: One recording at a time

Both ETW sessions have fixed names and each start removes a session of the same name, so a second capture
silently ended the first one's frame and network tracing.
- **A machine-wide lock held for the whole recording (chosen)**: a second `watch` refuses before starting
  anything.
- **A unique session name per capture**: would let two captures coexist, but orphan clean-up by a fixed name
  is what keeps a crashed run from leaving a trace session behind until reboot. Revisit hook: the session
  names in `capture/presentmon.py` and `capture/netflows.py`.

## 2026-11-04: How frame timing gets ETW rights

PresentMon exits with code 6 ("access denied") unless the token is elevated or in Performance Log Users.
- **Relaunch only the capture elevated, per run (chosen as the offer `watch` makes)**: one UAC prompt; the
  elevated child captures and writes the file, the unelevated parent analyses and fixes, so HKCU fixes and
  the journal stay with the real user.
- **Join Performance Log Users once (offered by `setup` as instructions)**: no prompt afterwards, but it is a
  security-group change the user makes themselves; untested for the kernel network provider. Measured on
  2026-11-06 (E1): the group may not enable Kernel-Network, so it covers frame timing only, and the game-server
  tracker still needs `--elevate`.
- **Elevated helper relaying rows**: deferred, worth trying; taken up on 2026-11-06 as the capture service
  ("How other people get frame timing without prompts").
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

## 2026-11-04: An offline PC may run the pinned PresentMon without a revocation answer

On a PC with no cached CRL, WinVerifyTrust (cache-only, so no network) can answer "revocation unknown",
and setup would refuse an exe whose bytes match the pinned SHA-256.
- **Accept "revocation unknown" only after the pin matched, re-checking the signature without revocation
  (chosen)**: the pin is the exact official release, which a later revocation cannot change; an explicit
  `CERT_E_REVOKED` still refuses, and the result carries a note saying the check was offline.
- **Fail closed on every non-zero answer**: rejected; it would block frame timing on offline PCs while
  adding no protection the pin does not already give.
- **Fetch revocation data online**: rejected; setup would contact certificate servers on every run.
Revisit hook: `setup.REVOCATION_UNKNOWN` and `verify()`.

## 2026-11-04: No fix that raises the game's priority

- **Lower the priority of the user's own background hogs, session-only (chosen)**.
- **Raise the game's priority**: rejected. It needs `PROCESS_SET_INFORMATION` on the game, which breaks
  "never touch the game", and anti-cheat may strip or flag that handle.

## 2026-11-04: Watch the game from outside, never inject

- **Observe from outside (chosen)**: ETW via PresentMon for frames, performance counters and system-wide
  snapshots for the rest. Nothing is injected or hooked; PresentMon's query-only handle to the game is the one
  thing anti-cheat can see (DESIGN, Principles).
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
