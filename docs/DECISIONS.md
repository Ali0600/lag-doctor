# Decisions

## Backlog: alternatives worth trying later

- **Let the fan line count in the GPU heat finding**, once a yardstick for "slow" exists: the fastest this laptop's
  fans reached across its recordings, or ASUS's performance mode read through the same device (a read, DSTS). Today
  the line is context only (2026-11-17, the fan readings). Revisit hook: `rules._fans_text` and
  `attribute.fans_while`; a scored line would go through `c.support` before `conclude`.
- **Cut a part bounded by `--duration` at the end the session announced** (`until` saved in `start_key`), so the
  analysis stops at the set length rather than at the recorded end with the stop's own seconds, and a press too late
  for a part could count and be judged there: worth trying for a terminal `--duration` (2026-11-10, the start key in
  `watch`); the app's own recordings never meet it, since their limit lies past every window (2026-11-17, M5c).
  Revisit hook: `Session.start`, `Session._assemble`'s `start_key` record, `crop.key_part`.
- **Stop an app recording at 10 minutes when Windows refuses the start key**, rather than at its 31-minute limit:
  worth trying if the owner finds 31 minutes too long (2026-11-17, M5c). Revisit hook: `App.on_recorder` on
  `start_key` with `registered` false.
- **The terminal's falling chime at the window's end**, as the app plays it, instead of the three notes the app
  keeps for "results ready": the owner's call (2026-11-17, M5c). Revisit hook: `cli._Cues`, `cli.CONSOLE_SOUNDS`.
- **Hand the sweep's held start key to watch**, so nothing frees it between Enter and watch's registration (the UAC
  prompt and the PC's settings sit in that gap): worth trying if a sweep run ever fails as `KEY_TAKEN` there
  (2026-11-09, the start key in `watch`). Revisit hook: `runner._record`'s `_let_go(held)` and `Session`'s
  `hotkey_factory`.
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
- **A note for a past recording** (`lagdoctor note <session> TEXT`, or an edit field on the app's Results or Compare):
  today the note is given only as the recording starts (`watch --note`), so a forgotten one stays forgotten. It would
  rewrite a finished session's capture and summary.json, which nothing does today; the app (M4 on) is the likelier
  place. Revisit hook: `Capture.note` and `clean_note` in `lagdoctor/model.py`, `summary.NOTE_KEY`, `library.Entry`.
- **The note through `SessionOptions`**, for M4's recording from the app, so the session's observers could show it
  while it records. Revisit hook: `_record` in `lagdoctor/cli.py` (where the note is set today) and `SessionOptions`
  in `lagdoctor/capture/session.py`.
- **The recorder raises the relay's UAC prompt with the window's HWND**, instead of the window raising it and
  handing the relay's handle over: simpler, if Windows treats another process's window as the prompt's foreground
  owner, which Microsoft's pages never say. An owner run would show it. Revisit hook: the `launcher` that
  `relay/client.launch` takes, and `launch_elevated(..., hwnd=)` (DECISIONS 2026-11-09, M4's prompt).
- **Stop and save at once when lag-doctor's window goes away** during a recording, instead of recording on to the
  game's exit: worth it if recordings left running by a closed window turn out to be a nuisance. Revisit hook: the end
  of `Channel._read_loop` in `lagdoctor/control.py`.
- **Bring lag-doctor's window forward at a recording's end**, flashing its taskbar button only when Windows refuses,
  once Play (M6) ends a recording after the game has closed. Revisit hook: the end branch of `App.on_recorder` in
  `lagdoctor/app/main.py` (DECISIONS 2026-11-09, M4's end).
- **Keep the app's view showing while its window can still be seen** during a recording (a second monitor), if
  Windows' own occlusion state can tell "in sight" from "covered by the game"; today an inactive window hides its view
  and draws one still line. Revisit hook: `AppWindow.shown` (DECISIONS 2026-11-09, M4's end).
- **Lead the headline with whichever finding explains the most lag time**, not the best-ranked claimer. Revisit hook:
  `diagnose._lead` (DECISIONS 2026-11-09, the audit's eleven bugs).
- **Save the game's top threads per tick** and judge the busiest thread (and so the headroom's `u`) on each one's
  median, unbiased even for work that hops between threads. Since the audit's fixes the bound, and the headroom
  with it, reads the lead thread, so several busy threads no longer read as one busier one. Revisit hook:
  `sysmon.ProcessRates.update`, `frames.busiest_column` (same entry).
- **Take uneven presents for VSync's judder where most frames hold one refresh too**. Revisit hook: `usual >= 2` in
  `rules._vsync_shaped` (same entry).
- **Name judder from Windows' composition for what it is**: a game presenting with VSync off (SyncInterval 0) or
  unknown (-1, OpenGL) in a composed window still has its frames held on whole refreshes, and `vsync_judder` calls
  it VSync judder and offers "turn VSync off". Revisit hook: the title, summary and fixes of `rules._vsync_judder`,
  with `frames._sync_off` and the present mode (same entry).
- **Tell a game's own one-refresh swings from VSync's**: LagLab's pace mode at 6 / 14 ms (not its 4 / 20 default)
  holds its frames one or two refreshes, 53 % on two, and reads as VSync judder. Revisit hook: `rules._vsync_shaped`,
  with the presents' own swing share beside the displayed one (same entry).
- **The page file with the RAM size unknown**: when GlobalMemoryStatusEx fails, `ram_pressure` cannot tell the page
  file from RAM, and the paging lift test still counts against a commit charge at the limit. Revisit hook: the
  `roomy` branch of `rules._ram_pressure` with attribution's own RAM floor (same entry).
- **Judder that slips both ways, counted from 20 %**: frames held one refresh fewer and one more around the usual
  hold (8 % and 6 % around a usual three, LagLab's gpu mode at a 20.8 ms frame) stay unnamed by the owner's choice
  (2026-11-10). Worth trying if a recording shows that judder on screen and no rule names it; from 10 % it would flag
  a cap at refresh / 2 that slips 5 % each way (LagLab's smooth control at a 72 FPS cap). Revisit hook: the `spread`
  branch of `rules._vsync_judder` (same entry).
- **VSync judder judged per stretch, not over the whole capture**: a long menu at the refresh rate or a heavy stretch
  bends the whole capture's hold counts, so judder in one part can go unnamed. Revisit hook: `rules._refresh_holds`
  and `rules._judder` (same entry).
- **An event's side under the 3D engine's verdict**: a stutter whose frames had no GPU timing takes the engine's
  "gpu" for its own side, leaving the CPU causes out; reading it as unknown instead made an idle-thread stutter on
  a pegged GPU "the game waited on itself". Revisit hook: `VERDICT_SIDE` in `attribute._side` and `_residual`
  (same entry).
- **The game's own packets as the arbiter of ICMP loss** on its server, which would cover rate-limited servers.
  Revisit hook: `diagnose._ignored_losses` with `rules._packet_loss` (same entry).
- **An unattended settings sweep** (B of 2026-11-09's sweep fork): the assisted sweep plus key presses lag-doctor sends
  to the game's menus and a close request between runs, so nobody has to sit through each run. Needs frame timing
  without a UAC prompt per launch (Performance Log Users, E2 in `docs/INSTALLER-PLAN.md`, or Stage 4's capture
  service), a game known to take injected keys (`docs/research/settings-sweeps.md`, section 8, question 4), and the
  owner lifting the rule that nothing types on the player's PC. Revisit hook: the per-run steps of the sweep runner,
  where the player now starts the game, presses the start key and quits.
- **Each program's network bytes in time bins**, so a measured part can sum its own instead of losing them: today
  `talkers` and `game_wire_bytes` are totals over the whole recording, and a part sets them to None, so
  `bandwidth_hog` can name a program only for a whole recording. Revisit hook: `top_talkers` and `game_wire` in
  `lagdoctor/capture/netflows.py`, and `crop.NET_FLOWS` (DECISIONS 2026-11-09, the measured part).
- **The power settings read again when measuring starts**, so a part is judged by the power plan and mode it ran under:
  today the inventory is read once, before the recording starts, and the start key may come 20 minutes later; a mode
  switched during the startup would be described for the part. Revisit hook: `Session.start` in
  `capture/session.py`, the start key's press (built in M5's second part, which left this out), and
  `_collect_inventory` (DECISIONS 2026-11-09, the measured part).
- **A second press of the start key ends the window early**, as FrameView's, CapFrameX's and PresentMon's own capture
  keys do: the player decides where the interesting part ends. Today a second press changes nothing, so a stray one
  cannot cut a measurement short. Revisit hook: `Session.start` in `capture/session.py` (DECISIONS 2026-11-09, the
  start key).
- **One edge rule for a sweep run's numbers and its report**: the sweep clips the frame at each edge of its window
  (`results.window_metrics`), while the run's report, now cut to the same window, leaves out a frame that began
  before the press (`crop.frames_inside`), so the two can differ by a frame. Revisit hook: those two functions.
- **A window-visibility reading per sampler tick** (the game's window minimized, cloaked, or covered by the visible
  windows above it in z-order), so time behind another window is told by visibility, not by the foreground: a game
  visible on a second monitor that freezes while not in front would stay a freeze, and one covered by a topmost
  window while in front would be left out. Revisit hook: `frames.hidden_rows`'s foreground test, and a column read
  through `lagdoctor/winapi/windows.py` in `capture/sysmon.py` (DECISIONS 2026-11-11).
- **Keep LagLab's window topmost during the end-to-end run**, so a click elsewhere cannot cover it: its frames would
  keep coming and the other checks would still say something, though the run would still fail its front check (a
  change of focus makes LagLab hitch). Revisit hook: right after pyglet makes the window in `laglab/app.py` `main()`
  (DECISIONS 2026-11-10, the end-to-end failures).
- **No cause for a frozen frame too short or too noisy to tell working from waiting**: such a frame still reads
  `game_stall`, "the busiest game thread was not busy", from a 1 s mean that cannot see it (a 40 ms frame worked
  through adds 3 points).
  Naming none would say what was measured. Revisit hook: the last return in `attribute._residual` (DECISIONS
  2026-11-11, the e2e attribution).
- **A cause of its own for a held screen** ("Windows held the screen", with fullscreen or flip-model advice), so a
  stutter the game did not make points at a fix instead of naming no cause. Revisit hook: `attribute._residual`'s
  screen branch and `rules.STUTTER_CAUSES` (DECISIONS 2026-11-11, the e2e attribution).
- **A held screen for a game faster than the refresh**: its frames are under one refresh apart, so no frame Windows
  left off passes `_screen_hold`'s refresh test, and such a stutter still reads as the game's. Asking whether a vblank
  passed between a frame's present and the next (from the shown frames' screen times) would judge it. Revisit hook:
  the refresh test in `attribute._screen_hold` (DECISIONS 2026-11-11, the e2e attribution).
- **Rule out another program's GPU use, or paging, at a frozen frame the game's thread worked through**, as the disk
  is: a thread that ran through the frame was not waiting. Not for the GPU yet, since a driver can spin on the CPU
  while it waits on the GPU, so "worked through" may be a wait there; paging blocks the thread, so it would follow the
  disk once a capture shows it. Revisit hook: `waits=` on the part in `attribute._causes` (DECISIONS 2026-11-12).
- **Pass network lag in the e2e smooth control** when only ping events make it (nothing blamed on the game), since
  the control asks whether the doctor invents game lag, and the Wi-Fi's own spikes fail it today (the second 2026-11-11 run:
  the router itself answered in 120 ms). Revisit hook: the "no lag in the control" check in `e2e_truth.judge`
  (DECISIONS 2026-11-12).
- **Name the programs that held memory where the disk or CPU advice meets Memory Compression**: its one sentence
  there (`rules._itself_means`, inside `_windows_text`) still says "close memory-heavy programs" without names, while
  `background_hogs` names them since 2026-11-16. Revisit hook: `_cpu_advice` and `_disk_advice` would add
  `_compression_holders`' advice beside theirs (DECISIONS 2026-11-16, the store app and Windows' shell).
- **Something to do about the store app's own GPU or CPU use**: `gpu_contention` and `background_hogs` now say only
  that `steamwebhelper.exe` is part of Steam, never to close it. Steam's own setting for its web views' GPU use, or
  closing its window while it keeps running, would give the player a step, once a primary source states what each
  does. Revisit hook: `rules._store_advice` (DECISIONS 2026-11-16, the store app and Windows' shell).
- **An explorer.exe look-alike in a capture recorded before the folder check covered the Windows folder** is judged
  by its name, as Windows' shell: such a capture cannot tell it from the real one. Revisit hook: none needed unless a
  recording from 2026-11-15 or 2026-11-16 shows one (DECISIONS 2026-11-16, the store app and Windows' shell).
- **Play's relay writes an 8.3 short name out long**, so a game a launcher starts through a short path (C:\PROGRA~2\...)
  matches its folder there too, as it does in the folder wait: worth doing if a Play run is ever refused for that
  reason, by having the player's side approve the folder's short form as one more form before the prompt (the relay
  must still read no folder itself). Revisit hook: `target.folder_forms` and `program_reason(resolve=False)`
  (DECISIONS 2026-11-17, M6d).
- **A relay that follows a re-targeted game after `start`** (stop PresentMon, start it on the new process, move the
  game's tree), for a launcher the re-arm's hold missed: worth trying if Play recordings show the hold delaying too
  much of a game's start. Revisit hook: `Relay._out_of_turn` ("a game after the start") and `_start_sources` in
  relay/server.py (DECISIONS 2026-11-17, M6d).

---

## 2026-11-17: An ASUS laptop's fans are read, only read, by a collector of their own (PR #57)

The owner asked to measure the fan speeds after a recording showed the GPU held at 210-345 MHz by heat and nothing
could say whether the fans were part of it. The forks were the session's; the owner's request was the feature.

- **Where the readings come from.**
  - Chosen: ASUS's System Control Interface device (`\\.\ATKACPI`) with DSTS, the call G-Helper and Linux's asus-wmi
    use. It opened with no rights and answered in about 0.5 ms (`docs/research/asus-fans.md`, section 9).
  - `AsusAtkWmi_WMNB` through WMI (`root\wmi`): deferred, worth trying only if a laptop has the WMI class without the
    device. COM from ctypes costs far more code, and its rights and cost were not measured. Revisit hook:
    `winapi/atkacpi.py` keeps one reader, so a second one would sit beside it with the same DSTS-only rule.
  - D3DKMT's fan field: rejected, it read 0 on this laptop's NVIDIA GPU (`docs/research/other-hardware.md`, 5.1).
- **Read only, and how that is held.** The device takes reads and sets through one FILE_ANY_ACCESS control code, so
  the handle cannot make it read-only (docs/learnings.md).
  - Chosen: one builder that can only build a DSTS read of a fan id, checked again in `_call` before
    DeviceIoControl, and a source scan in `tests/test_fans.py` that refuses any other way to the device.
  - Opening the device read-only instead: rejected, Windows checks no right for that code.
- **Where the rows live.**
  - Chosen: a collector and table of their own (`fans`, `Capture.fans`), at 1 Hz on its own thread. A stuck firmware
    call then costs only the fan rows, and the fans have a status of their own for "not measured".
  - Columns in the system table: rejected. Its 4 Hz thread would wait on every firmware call, and 3 of 4 rows would
    hold a None meaning "not read this tick", unlike its other Nones.
  - Columns in the NVIDIA table: rejected. That table exists only when the game renders on the NVIDIA GPU, so a game
    on the Intel GPU would record no CPU fan.
- **What the analysis does with them.**
  - Chosen: one line of context in `gpu_throttle` while heat held the GPU down (each fan's median and its fastest),
    never scored.
  - A finding of its own, or a scored line: deferred (Backlog). A fan's top speed is per model, so "slow" has no
    yardstick yet.
- **A speed past 12,000 RPM** is a corrupt reading counted as a failed one, at the source (G-Helper's own bound).
  Storing it and leaving it out in the analysis was rejected: the chart scaled to it.
- **A fan that errors before it ever answers** is tried ten times, then named as never answering. Counting it as a
  failure every second was rejected: the source would read `partial` for a fan the laptop may not have.
- **CI declares `asus_acpi` absent before a run showed it**, against CLAUDE.md's rule to mark only after a CI failure.
  A GitHub VM has no ASUS firmware, so the run could only fail. The rule exists to keep a test that passes on the
  runner running there, which a live read of ASUS fans cannot do.

## 2026-11-17: The relay for Play judges the game the recorder names, against the install folder the prompt approved (M6d)

Play's UAC prompt must come before the store starts the game (the player presses Play, approves the prompt, then the
store starts it), so the relay cannot be approved for a running game as M3's is. Put to the owner on 2026-11-10 with A
recommended, and answered on 2026-11-17 in the window-side session ("A, pass it on to the Play session"):
- **A: the recorder finds the game and names it; the relay judges it (chosen by the owner)**: the prompt approves the
  game's install folder (its forms, `target.folder_forms`, worked out on the player's side) and how long the recorder
  will wait for it. The recorder's folder wait finds the game as before and names it to the relay by its pid and create
  time; the relay judges it on its own snapshots, by its own read of the program's path. Less code runs with
  administrator rights, the game is found in one place, and Stage 4's capture service takes the same request.
  - **B: the relay runs the folder wait itself**: rejected by the owner. The pipe would keep carrying only start and
    stop, but the wait's GPU samples, window reads and image reads would all run with administrator rights, and the
    recorder would still need the game for its own collectors: two places finding it.

How it was built, after a design review and a code review (each a fork of the implementing session, not the
owner's):
- **A `game` message of its own before `start` (chosen)**: the relay answers `game` (it will record that one) or
  `refused` with why, and the recording starts only on a game it accepted, so a refused game ends `watch` with the
  reason before the session starts (as 2026-11-08's "A relay that cannot record ends the watch" wants) and M3's
  `start` stays as it was.
  - **The game inside `start`**: rejected. `start` goes out from a collector's thread after the PC's details are read,
    so a refusal would leave the recording running without frame timing.
- **A refusal is an answer, and the relay waits on (chosen)**: up to `protocol.MAX_GAMES` (8) games before `start`, a
  later one taking the place of the one before. A launcher that draws and then leaves, or a read that failed
  (`retry`), costs a new proposal, not a new UAC prompt: a new relay's `since` would refuse a game that started
  before it.
  - **A refusal ends the relay**: rejected. Nothing it protects: any process the folder rule passes may be recorded
    anyway, and the recorder can read every fact the relay reads.
- **The relay reads no folder (chosen)**: it compares the program's path as Windows gave it (`resolve=False`). Writing
  an 8.3 short name out long reads the folders along the path, which the player's side can change, with administrator
  rights: a link swapped in could lead it onto a share. A short-name path is refused there with the reason; writing
  it out long is in the Backlog above.
- **Its wait for the game is the recorder's own (chosen)**: `--wait-s`, at most `target.MAX_WAIT_S`, plus
  `server.GAME_SLACK_S` (60 s: one round of the wait past its deadline, the re-arm's hold `target.REARM_HOLD_S` and
  the recorder's own check), which a test holds above their sum and below 120 s. The folder's approval lasts no
  longer than the recording asked.
  - **A fixed constant from `target.MAX_WAIT_S`**: rejected. Simpler (no new argument), but every Play relay would
    take a game for over an hour.
- **The checks both sides make are one function**, `target.check_proposed`: the program's path is read between two
  snapshots that both list the process with its create time (a number never names two processes at once, so the
  path is its own), the recorder must still run at the end, and lag-doctor's own processes (the recorder's tree and
  the relay's) are never the game. The recorder asks it first (`RelayLink.aim`), so a game the relay would refuse is
  never sent.
- **The Windows folder from Windows itself** (`GetSystemWindowsDirectoryW`), never the SystemRoot variable, on both
  sides: the relay checks the forms its prompt approved by it, and an environment is the caller's to set.
- **The app refuses a relay command for a folder** until Play's half of the app chooses the game: nothing the window
  chose would bound what the relay may record.

Limits, accepted:
- Another account's process from the folder, or an elevated or protected one, in the player's Windows session passes
  the rule, as in M3: the handle-free snapshot names no account or integrity level.
- A program another local account wrote into a writable game folder passes it, if it starts after the relay.
- A folder form as wide as `C:\Program Files`, which a store's library file could name, takes in every program under
  it; the rule refuses only a drive's root and the Windows folder.
- With the clock stepped back after the relay started, a game's create time can fall before `since`: it is then
  refused, never wrongly taken.

Revisit hooks: `target.check_proposed` and `accept_target(own=, resolve=)`, `Relay._play`, `_judge` and
`_out_of_turn` in relay/server.py, `RelayLink.aim` in relay/client.py, and `recording.relay_problem` (Play's app half).

---

## 2026-11-17: The app measures from its own start key (M5c, the app's half of M5)

The app's recorder runs `watch --control stdio --elevate --start-key ctrl+alt+s --window 600 --start-by 1200
--duration 1860` (`recording.recorder_argv`), and the app follows the key: the running screen asks for the press once
Windows has given the key, counts the measured window from it ("07:12 of 10:00"), and words the two ends; the app
plays its own chimes; Results shade what a measured part leaves out. Five read-only readers mapped the recorder, the
page, watch's start key, the tests and the plan first, and four read-only reviewers read the change before its gate.

The owner's (a question card, 2026-11-17):
- **Trim handles in their own pull request, next (chosen: "Own PR, next (Recommended)")**: this one carries the key,
  the words, the chimes, the shading and the note for a key that measured nothing; the handles follow, and the owner
  tries that one too.
  - **All in this pull request**, a trimmed part analysed in the window only: not chosen.
- **A start key that measured nothing: the whole recording, labelled (chosen: "Whole recording, labelled
  (Recommended)")**, as in the terminal: Results say why (`labels.unmeasured_note` without the terminal's `analyze
  --from` command), and the recorder's own analysis of all of it stays what the Library and Compare show.
  - **Nothing until the player sets the start** (the plan's first rule): not chosen. Revisit hook: the trim handles'
    pull request, which could offer a part on such a recording without changing what it shows first.

The session's:
- **A recording limit of 31 minutes (chosen)**: `recording.LENGTH_S` is `START_BY_S + WINDOW_S + 60`, past the end of
  the last window a press in time can start (watch looks at `--duration` before the window, so a smaller limit would
  end that window early as "duration", and M4's 600 would leave no press its window). It binds only when Windows does
  not give the start key: the whole recording is analysed then, and the screen says the key is off and when it stops.
  - **No `--duration`**: rejected; a refused key would record until Stop or the game's end, which M4 rejected
    (2026-11-09, M4's end: stopping means switching to the window, which changes the measurement).
  - **Stop at 10 minutes when the key is refused** (the app sending Stop itself): deferred, worth trying if the owner
    finds 31 minutes too long; it needs a timer of the app's own and words for a stop it made. Revisit hook:
    `App.on_recorder` on `start_key` with `registered` false.
- **The two ends told apart by the `stopping` reason (chosen)**: both reach the app as `done` "completed", and
  `RecordingState.stop_reason` (session's `WINDOW_ENDED`, `NO_START`) picks the words (`views.KEY_ENDS`,
  `KEY_STOPPING`); the protocol is unchanged.
  - **New `control.REASONS` for them**: rejected for now; it changes `done_reason`, `from_recorder` and every reader
    of a reason, for words the app can already pick. Revisit hook: `control.done_reason`.
- **The app's chimes (chosen, the plan's Play steps 4 to 7)**: one note when the key is ready, the rising chime at the
  press (no longer when the recording starts), a new falling "measured" chime (the rising one's notes turned round)
  when the window ends, the low tone when no press came or the key was refused, and "done" when the results are
  ready. A feel number for the owner to judge.
  - **The terminal's three notes at the window's end** (the owner's choice for a terminal watch, 2026-11-09): kept
    there; in the app that chime already means "results ready", seconds later. Making the terminal play the falling
    chime too is the owner's call: deferred. Revisit hook: `cli._Cues` and `cli.CONSOLE_SOUNDS`.
- **The frame chart's scale is the HTML report's (chosen, after the review)**: `html.frame_scale`, from the frames
  the analysis counted, so neither a loading screen before the part, nor the wait the part starts in, nor time
  behind another window sets it; the page computes its own only when that gives none.
  - **The part's frames by their time alone** (the first version): rejected, a short part that starts in a load was
    scaled to the load's one frame.
- **Presses outside the part drawn apart (chosen)**, dashed in the reference colour, as the HTML report draws them;
  every time chart and a moment's charts shade the rest (`labels.rest_spans`, which the report's `_shade_rest` now
  uses too).
  - **Leave them off** (the app before M5c): rejected; a press the player made in the startup would seem lost.
- **No choice of key, window or length in Settings yet (chosen)**: Settings name the start key; the numbers are the
  terminal's defaults, held equal by a test. A choice needs app settings the app does not keep yet. Revisit hook:
  `recording.recorder_argv` and `views.settings_view`.
- **A `measuring` the stop overtook changes nothing (chosen, after the review)**: the session sends it after its
  lock, so a stop at that moment can reach the window first; the app takes it only while recording.

Revisit hooks: `RecordingState.apply` (`lagdoctor/app/recording.py`), `views.recording_view` and
`views.results_view`, `overlays` in `lagdoctor/app/assets/app.js`.

---

## 2026-11-16: The store app that started the game and Windows' shell are never told to close; Memory Compression names who held the RAM

The owner chose "PR #51's leftovers" (2026-11-16), the three backlog items its entry left: `background_hogs` and
`gpu_contention` could still tell a Steam game's player to pause `steamwebhelper.exe`; `background_hogs`' Memory
Compression advice said "close memory-heavy programs" without a name; and `explorer.exe`, Windows' shell, was a
program to close.

**The store app's programs.** They are treated as Windows' own programs are in those two rules (`rules._store_advice`): said to be part
of the store app, which the game may need, never told to pause or close, never given a priority fix, and in
`gpu_contention` no "run it on another GPU". A priority fix would not close Steam, but lowering the program a game
talks to while it plays is a change the game did not ask for, and the memory, disk and CPU advice already never named
them as programs to close. `bufferbloat`'s "pause or limit the downloads of steam.exe" stays: pausing a download is
what helps, and it closes nothing.

**Memory Compression.** `background_hogs` adds, right after what Memory Compression is, the advice `ram_pressure`
gives (`rules._compression_holders` through `_held_advice`): the other programs that held the most RAM around the
stutters it rose at, from 500 MB on average, else the most any program held. The stutters' windows (half a second
before, a quarter after) reach `MEMORY_NEAR_S` further each side, since memory is read once a second. Memory
Compression itself is left out (`_heavy`'s `leave_out`): its own store is what the advice explains, and counting it
made a figure under the floor read "only Windows' own processes held 500 MB or more". An older capture keeps its old
advice alone. The disk and CPU advice's one sentence on Memory Compression still names nobody (backlog). Where every program the
line would tell to close is already one `background_hogs` names, it is left out, as the same advice twice (found in
review).

**explorer.exe: where Windows runs it**
- **A: the folder check also covers the Windows folder, and the capture says so (chosen)**: `SystemFolders` adds
  GetSystemWindowsDirectoryW's folder (the one every user shares; GetWindowsDirectoryW can give a private one per user,
  Microsoft's page says) for `explorer.exe` alone (`sysmon.WINDOWS_FOLDER_PROGRAMS`), so the real one reads
  `system_folder` True, and `target["windows_folder"]` marks the capture. Only an administrator writes directly to
  that folder (a folder below it, such as `Temp`, is not it). Counting every program there, as the first version did,
  turned a `svchost.exe` look-alike dropped in `C:\Windows`, a usual place for one, into Windows' own (found in
  review).
  Without the marker, a False meant "not System32", which the real one also is, so the analysis decides by name there,
  as it does in a capture of names only.
- **B: judge `explorer.exe` by name alone, like Defender's scanner**: rejected: the real one and a look-alike would read
  the same, which is what the folder check exists to tell apart.
- **C: a 13th field per entry for the Windows folder**: rejected: the same answer in a field of its own on every entry
  of every row, where one key per capture says which question field 9 answered.
- **D: widen field 9 without a marker**: rejected: a capture from 2026-11-15 or 2026-11-16 says False for the real
  one, so it would read as a look-alike, worse than before this change.
Revisit hook: `rules.WINDOWS_FOLDER_PARTS` for another program Windows runs from its own folder.

Checked: new tests for each (the store app in both rules with Steam and Epic and when another store started the game;
Memory Compression above and under the floor, between two memory readings and in an older capture; explorer.exe by
where it runs, with and without the marker, in a capture of names only; the folder check with and without the Windows
folder, a failed read of it, the sampler's result and the session's target; a live check of the folder against
`%SystemRoot%` and of this PC's explorer.exe).

---

## 2026-11-16: The recorder keeps each listed program's memory, and the memory advice names who held it

The owner asked to "name memory- and GPU-heavy programs too" (2026-11-16), the backlog item the entry below left:
`ram_pressure` said "Close memory-heavy programs while playing" and `vram_overflow` "Close other GPU-heavy programs
while playing" with no name, since the recorder kept no process's memory.

What each others entry keeps:
- **A: the RAM it alone holds, the memory committed to it, and its video memory on the game's GPU, in whole MiB
  (chosen)**: the private working set (`WorkingSetPrivateSize`, Task Manager's "Memory"; one more field of the snapshot
  the sampler already takes) is what fills RAM, the private bytes are what count toward the commit limit (the page-file
  branch of `ram_pressure`, the one this PC's own recordings hit), and the dedicated video memory on the game's adapter
  (the PDH counter the sampler already reads for the game) is what fills the GPU. Fields 9-11, None when not read.
- **B: one memory figure**: rejected. RAM held and memory reserved part for exactly the programs that matter: a VM or a
  leaking program can reserve gigabytes it keeps out of RAM, and the two findings ask about different ones.
- **C: the whole working set**: rejected. It counts shared pages (DLLs) once per process, so a browser's 30 processes
  read several times what they hold.

Which processes a tick lists:
- **A: after the 8 by CPU and 4 by I/O, the 4 of the rest holding the most RAM, then committed memory, then video
  memory, busy or idle, once a second (chosen)**: a browser holding 3 GB while idle was never listed. Memory moves
  slowly, so only every fourth row (at 4 Hz) carries the memory picks and fields 9-11; the rows between say None
  there (not read at that tick), and the analysis counts only the rows that read memory. 24 entries on those rows, 12
  on the others. Their CPU counts in the listed totals too, as the disk entries' does. `gpu_contention` judges its
  busiest GPU user over the rows that read memory alone, where a table lists them (`rules._memory_row`): a program
  holding video memory with little CPU is listed only there, and over every row its median read 0 (found by the
  review workflow).
- **B: the picks on every row**: rejected in review. The recorder keeps every row in memory for the whole recording,
  and 24 entries of 12 fields on every row took about 2.5 times the others table's memory (about 160 MB an hour
  against 63, a reviewer's tracemalloc estimate) for figures that barely move between rows.
- **C: a separate table once a second**: rejected. The same saving, but a second table to crop, window and read.
- **D: totals per image name in the recorder**: rejected. Exact for a browser's many processes, but it loses each
  process's session and folder, which tell Windows' own from a look-alike.

Where the advice reads them (`rules._held_advice`):
- **A: at the ticks the finding's own condition held (chosen)**: free RAM under the warning floor, committed memory
  over 80 %, video memory at 90 % of its size (`rules._when`, the system rows' own times, each reaching half a second
  to either side, `MEMORY_NEAR_S`, so a dip between two memory readings meets the nearest); the whole part when no
  tick was, as with a finding made by hard faults alone. A program that held memory only after it was free again is not
  named for it.
- **B: the whole part**: rejected. A program started after the pressure ended would be named for it.

Floors: 500 MB of RAM or memory reserved, 250 MB of video memory (a browser holds 100-500 MB of it), the lowest a
player would close a program for; inferred, not measured, so worth the owner's look. The figures are means over the
rows that read memory, unlisted ones as 0, so lower bounds ("at least"), and a name's processes add up; under the
floor the advice says the most any program held ("just under" the floor when it rounds to it) and that the table lists
the 4 biggest holders at each moment, so a program split into many smaller processes, such as a browser, can hold
more. A finding the commit charge made with the RAM size unknown reads memory reserved, not RAM.

What the advice says of programs a player should not close:
- **Windows' own**: Memory Compression and `dwm.exe` as before, and now `vmmem` and `VmmemWSL`, the process behind a
  virtual machine such as WSL 2's (Microsoft's WSL blog and troubleshooting guide, windows-telemetry.md section 10),
  with how to give its memory back (`wsl --shutdown`, from Microsoft's WSL commands page).
- **The store app that started the game (chosen)**: where the game sits in a Steam library or an Epic install
  (`inventory` game.launcher), Steam's or the Epic launcher's own programs (`steamwebhelper.exe` holds hundreds of MB)
  are said to be part of it, which the game may need, never "pause or close", in every advice `rules._heavy` writes
  (disk, CPU, memory). Rejected: leaving them out (a Steam download's disk use would then go unsaid), and naming them as
  any program (closing Steam can end the game).

An older capture keeps the old memory lines word for word: re-analysed, the owner's 40 recordings are unchanged, the
store app's line included. The app's moment view averaged each program over only the rows it was listed
on, so a program listed for its memory read far less busy than one listed only when busy; it now averages over every
row there, as the analysis does, and leaves out programs listed for their memory alone. Known limits: `background_hogs`'
Memory Compression line still names nobody, and `background_hogs`, `gpu_contention` and `explorer.exe` keep their old
treatment (backlog). Revisit hook: `sysmon.top_others`, `sysmon.pid_vram` and `MEMORY_EVERY_S` (the recorder),
`rules._held_advice` and `rules._store_of` (the analysis).

## 2026-11-15: The recorder lists the disk users too, and says where each process runs

The named-program advice (the entry below) could name only what the others table listed: the 8 busiest on the CPU
each tick, by image name. A program reading the disk hard with little CPU was never listed, and a name could not tell
Windows' own `svchost.exe` from one started out of a user's Temp folder, so `background_hogs` and `gpu_contention`
still told the player to "pause or close" Windows' own user-mode processes (`dwm.exe`, `csrss.exe`, a real
`svchost.exe`) and offered a priority fix that `fixes/priority.py` refuses when run (PR #32's reviewers' bug). The
owner said "do 1-3" to the list that named both.

Which processes a tick lists:
- **A: the top 8 by CPU as before, then the top 4 of the rest by reads and writes (chosen)**: the 8 by CPU stay the
  same processes, and a disk user with little CPU is listed. 12 entries a tick at most. The 4 extra entries' CPU now
  counts in the listed totals (`attribute._others`' total, `rules._others_steady`) too: a disk user's little CPU.
- **B: one ranking by CPU and I/O together**: rejected. It would change the 8 by CPU that the background CPU signals
  and their tests rest on, for a gain the 4 extra entries already give.

How a capture tells Windows' own from a look-alike:
- **A: each entry's Windows session and whether its program sits directly in System32 or SysWOW64 (chosen)**: both
  handle-free (the snapshot's SessionId; `ntquery.image_path`, read once per listed process), and only the one bit of
  the path is kept.
- **B: the name alone (the entry below)**: rejected now that the recorder can say more. It read a look-alike as
  Windows', and could not tell a System32 tool the player runs from Windows' own work.
- **C: keep each listed process's image path**: rejected. A path can hold the user's folder names, and the check needs
  one bit of it.
- **D: the priority fix's own checks (a protected process, the service list)**: rejected for the recorder. The
  protected check opens a handle (`winapi/process.py` only), and session 0 already marks every service.

What counts as Windows' own, given those (`rules._windows_own`):
- **A: a `WINDOWS_PARTS` name in session 0 or from System32 (chosen)**: Windows runs its services in session 0
  (Defender's programs sit outside System32) and runs `csrss.exe` and `dwm.exe` from System32 in each player's
  session. A `WINDOWS_PARTS` name in the player's session from another folder is a look-alike: named as such ("named
  like a part of Windows, but not where Windows runs it"), closable and lowerable; when one name has both, each gets
  its own advice. Defender's scanner (`MpCmdRun.exe`), which a player may start from Defender's own folder, an older
  capture, and a process whose path was not read decide by name. Known limits: a service someone installs under a
  Windows name runs in session 0 and reads as Windows' (installing one takes an administrator, and the priority fix
  refuses session 0 whatever it is); Windows' own programs on no list (`winlogon.exe`, `fontdrvhost.exe`,
  `sihost.exe`, `taskhostw.exe`) are still told to close, and the first two still get a priority fix that refuses
  them when run ("another user"), until a source names each.
- **B: also any program from System32 in session 0**: rejected in review. A backup service's `robocopy.exe` child or
  a scheduled task run as SYSTEM is no Windows service, and "pause that backup" would be lost.
- **C: any program from System32**: rejected. `robocopy.exe` or `cmd.exe` running a backup in the player's session are
  the player's own, and closable.

Also: `dwm.exe` joins `WINDOWS_PARTS`, now that Microsoft's DirectComposition page names it (windows-telemetry.md
section 10), and no priority fix goes to a session-0 process, as `fixes/priority.py` refuses those. Revisit hook:
`sysmon.top_others` and `sysmon.SystemFolders` (the recorder), `rules._windows_own` (the analysis).

## 2026-11-15: The advice to close other heavy programs names the ones the capture saw

The owner, on the README's report: "Close other disk heavy programs" could post what else was disk heavy, "maybe
posting the second highest disk reading", and the same for statements like it. The capture already holds every
listed process's CPU and I/O (the others table, the 8 busiest on the CPU each tick) and the network talkers. Re-run in
memory over the owner's 40 saved recordings, only these advice lines changed (no title, score or evidence); the
largest other disk user at a real game's loading stutters averaged 7.5 MB/s, and one e2e run's CPU limit named two
programs and said a third was Windows Security.

Over which stretch a program counts as heavy:
- **A: the windows attribution looked at around the finding's own stutters, as a mean over the table's ticks there,
  a tick it was not listed on counting as 0 (chosen)**: what was busy when the game stalled, as a lower bound, with
  the floor its cause already blames at (20 MB/s, 25 % of a CPU). `hdd_streaming` with no loading stutters, `cpu_limit`
  and `bufferbloat` take the whole part; a talker counts only when its own process moved 1 MB/s, as `bandwidth_hog`
  counts them, so the two never disagree in one report (found in review).
- **B: the highest one-second reading anywhere in the part**: rejected. A single burst far from any stutter names a
  program that had nothing to do with them.
- **C: only the programs attribution blamed** (`detail["process"]`): rejected. A stutter names the program only when
  its rise beat the game's own reading, so a steady heavy user is never named, and the README's own capture would
  keep the old line.

What the advice says when no program reaches the floor:
- **A: the most any program did, by name, with the table's limit (chosen)**, which is the owner's "second highest
  reading" (for example "the most was OneDrive.exe, at 3.1 MB/s"), and that a disk user with little CPU can be
  missing.
- **B: the figure without the name**: rejected. Under "close other disk-heavy programs" a name can read as blame, but
  the sentence says no program reached the floor, and the name is what the owner asked for.
- **C: drop the advice**: rejected. The game is still loading from disk, so the advice stays useful.

Windows' own processes in a list of programs to close:
- **A: said to be Windows by name, never told to close (chosen)**: `WINDOWS_ITSELF`, plus Defender, Windows Update,
  Search, `svchost.exe`, `csrss.exe` and `audiodg.exe` by image name (`WINDOWS_PARTS`, from windows-telemetry.md
  section 10). A look-alike under one of those names is only under-named here; the text says what the name "normally
  is" and offers no fix. `dwm.exe` waits for a Microsoft source that names it. The same table could stop
  `background_hogs` telling the player to close them, the open bug PR #32's reviewers found: worth trying with a
  session or path check (since built with both, and `dwm.exe` sourced: the entry above).
- **B: leave them in the list**: rejected. "Pause or close MsMpEng.exe" would suggest turning off Windows Security,
  which lag-doctor never offers.

Which programs a capture can show:
- **A: list the top 4 by I/O besides the top 8 by CPU in the recorder (chosen; built as its own pull request, the entry
  above)**: a disk user with
  little CPU is never listed today, so it can be neither named nor blamed. It changes what attribution sees on new
  captures, so it lands apart from this. Revisit hook: `sysmon.top_others`.
- **B: per-process memory and video memory**: deferred then; since built (2026-11-16, above): `ram_pressure` and
  `vram_overflow` name the programs that held the most.

## 2026-11-12: Another program's disk use never explains a frozen frame the game worked through

The owner's second e2e run of 2026-11-11, on PR #45's code, passed 4 of 8. Smooth, hog and pace lost the window's
focus partway (from 6.2, 15.4 and 2.6 s), so they cannot be judged; other programs were busy meanwhile, one reading up
to 630 MB/s. Smooth's verdict,
"network", was two real ping spikes (124 and 79 ms against 7 ms, the router itself at 120 and 74 ms): the Wi-Fi's, not
the doctor's. Hitch kept the focus and found all 8 hitches, but 3 named `background_disk` first: another program read
32-521 MB/s over them, and since both causes passed the lift test at weight 3, the ranking fell to each one's capped
z: the disk's 554 to 9,129 off a near-zero normal (0.3 MB/s) against the busy wait's 5.4 to 8.9. The ticks showed the
game's thread running 105-137 ms more than usual in those 160 ms frames. The owner said "Yes, fix it" to ruling the
disk out there.

Another program's disk use at a frozen frame the game's busiest thread worked through:
- **A: ruled out, as the turbo veto rules out a clock that load explains (chosen)**: the residual's tick test
  (`_hold_reading`, now one function both use) reading "worked" leaves the disk out of that stutter's causes and its
  lift test, and its last why-line says why. A thread that ran through the frame was not waiting on a disk. A frame
  read as waiting or in between, one too short or noisy to tell, and an FPS drop keep it, and so does a stutter of
  more than one frozen frame (added in review): its reading judges the longest alone, and another may have waited.
  Known limits: the reading is the busiest game thread's, whichever thread that is, so a worker's burst through the
  frame would rule the disk out while another thread waited (the capture names no thread that held the frame; review
  found no case where that read wrong); and its "ran N ms more" sums the whole ticks that hold the frame, so it can
  pass the frame's length where the thread also worked around it.
- **B: listed after the game's own work, not left out**: rejected. It would still name a mechanism the reading rules
  out, and still count in the lift test: on this run that count made a medium "background programs" finding.
- **C: rank by evidence differently** (z rescaled, or ties at the cap broken by how far past it): rejected. A signal
  that is near zero normally saturates z on any burst; no scale says whether the burst could hold the frame. The
  backlog's tie-break at `Z_CAP` is another question (this was no tie: 10 against 8.9).
- **D: judge a mode only on a quiet PC**, as the front check does for focus: rejected. It would hide what this run
  found, since a player's PC is busy too.
- **E: the same for other programs' CPU use**: rejected. A program hogging the CPU slows a thread that runs (it
  competes for the core), so it can explain a frame worked through. GPU use and paging are in the backlog.

Where a move was ruled out and nothing else counted, the residual's line now starts "nothing else that moved explains
it", never "nothing else moved" above the disk its last line names, and the count lines of `game_cpu_spike`,
`gpu_spike` and `game_stall` say nothing else "explained them" (review found the latter two still saying "no other
measured cause moved" above such an example; the turbo veto needed it too). Re-judged in memory, this run's hitch passes; across the six FF7
captures and the e2e runs of 2026-11-09 and both of 2026-11-11, only that hitch's diagnosis changes: its three
stutters name the game's own work, and its false "background programs" finding goes. Revisit hook: `_wait_veto` and
`_Part.waits` in `attribute.py`.

## 2026-11-11: The e2e run's three misattributions: a busy hitch, turbo under load, a screen Windows held

The owner's e2e rerun of 2026-11-11 passed 5 of 8 modes with every window in front; lined up with LagLab's own log,
the three failures were the doctor's. The owner said "Yes, fix them"; the implementing session chose where the fixes
forked, except the judge's control, which the owner chose. Re-judged in memory on the owner's captures, that run
passes 8 of 8; the 2026-11-09 run's diagnoses change only at its two held screens (its logs predate the focus log, so
it cannot be judged).

Telling a long frozen frame the game's thread worked through (hitch's 150 ms busy wait read `game_stall` in 5 of 8)
from one it waited through (hitch-wait's sleep):
- **A: the thread's extra CPU time in the 4 Hz ticks that hold the frame (chosen)**, at the midpoint of the two
  answers, where two spreads of those ticks' noise fit in half their distance. On the run the busy wait read +85 to
  +101 ms and the sleep -42 to +5 ms, against a 52 ms midpoint.
- **B: a lower z cut for the thread**: rejected. A 1 s mean diluted 150 ms to 6-16 points over the thread's usual
  17 % (under the cut in 5 of 8), and the cut is every signal's guard against noise.
- **C: PresentMon's CPU-busy time of the frame**: rejected. OpenGL reports no CPU wait, so busy is the whole frame
  whether the game worked or slept (PM 8.2).
- **One tick's spread, the larger of twice the 1 s means' and the quiet ticks' own (chosen)**: each alone fell short
  of the real spread of 160 ms sums on some e2e capture (6.3 against 9.6 ms; 31 against 43 ms).
- **Working through it needs a rise past the noise too (chosen in review)**: for a thread usually past half a CPU the
  midpoint is below nothing, so its usual level read as worked through. A reading nearer working through it but within
  that bar names no cause and says neither answer was measured. Stutters only: an FPS drop holds no frame, and a tick
  missing inside the frame leaves it unjudged.
- **A frame too short to tell keeps the 1 s reading (chosen for now)**: naming no cause there is in the backlog.
- Such frames leave no "contradicts" behind where counting them ends it (one in twenty does not): the lift test, whose
  1 s means could not show them, reads "neutral" there. The rule counts every stutter the residual named for the game's
  work on a line of its own ("in 5 of 8 ...", the hitch finding had said "8 of 8" over a count of 3), weighed only by
  the frames worked through, since a thread saturated all along says nothing of the moment.

The CPU's clock falling under load (hog's FPS drops named `cpu_throttle` first, 102 % of base against 144 %):
- **A: a fall to base speed or above while the whole machine's CPU use rose is turbo, not throttling (chosen)**:
  LC 4.5 compares speeds "at a similar load", and the run's clock was about 137-156 % with the CPUs nearly idle and
  100-105 % with all of them busy, % Performance Limit at 100 throughout. Such a moment leaves the cause's lift test,
  and a quiet window like it leaves its baseline share. The margin is small: the run's falls bottomed out at 100.6 to
  102.9 %, so a hotter run under the same load that fell below base speed would name `cpu_throttle` again, which a fall
  below base is.
- **B: only when background CPU fires**: rejected. The game's own all-core work lowers turbo the same way, and the
  machine's whole load is what turbo answers to.
- **C: only a fall below base speed counts**: rejected. At the same load, a hot laptop's turbo falling 20 % is the
  throttling LC 4.5 names.
- **Counting the explained moments as "did not coincide"**: rejected. "Only 0 of 12 coincided with the CPU slowing
  down" for a CPU that slowed at all 12. The review weighed it: such a moment could still have shown a fall below base
  speed and did not, so it is partly a miss; the line names the CPU slowing down, which it did there.

A stutter the screen made (smooth's 36 ms: Windows left 8 of 17 frames unshown while LagLab drew on time):
- **A: no cause of the game's, when nothing else moved (chosen)**: the game presented at least half the frames its
  usual rate gives, at least two of them, 25 points over its usual share, never reached the screen, and Windows had a
  whole refresh to show at least one of those before the game's next present. Its why-line says the screen held. A
  long stall of the game's own presents almost nothing until it ends (0.05-0.18 of its rate in the run's 100-160 ms
  hitches), but more where the stutter outlasts it (0.76, a 104 ms hitch in a 371 ms stutter) or where it is short or
  spread out (pace 0.99-1.16, hitch-io's 34 ms one 0.73), so the frames left off decide; and a game catching up after a
  stall presents a burst whose frames composition drops by design, which the refresh test leaves out. Its side line
  names the slowest frame the game presented, never "the CPU side was slow" beside "the screen held". Besides both smooth stutters it cleared one in pace on 2026-11-11, where
  LagLab's own frame starts kept their 4/20 ms rhythm while Windows' display latency climbed from 25 to 56 ms, and the
  2026-11-09 hog's, 0.6 s after its window came back in front.
- **The game's longest present gap against the hold, as the burst test**: rejected in review. The four real held
  screens read 0.11 to 0.75 and a stall with its catch-up burst about 0.9: too thin a margin.
- Known limits: above the refresh rate a held screen cannot clear, since the game's frames are under one refresh apart
  (backlog); in composed GPU-bound play most dropped frames pass the refresh test (240 of 245 in the 2026-11-09 gpu
  mode), so there the share decides, as before.
- **B: a cause of its own**: deferred, worth trying (backlog).
- **C: judged before the measured signals**: rejected. Other programs on every CPU can hold up Windows' composition
  too, and such a signal then explains it.

The e2e judge's smooth control, which failed on any stutter (the owner's choice, asked with AskUserQuestion):
- **"Pass screen-side ones (Recommended)" (chosen by the owner)**: a stutter passes only where LagLab's own frame
  starts span it and held no gap there as big as a stutter (LC 2.4), and the doctor named no cause of the game's for
  it; its verdict may then say "stutter". A log without usable frame starts fails every stutter. On the run LagLab's
  longest gap was 16.4 ms against its usual 8.3. Which causes blame the game is a list every cause of the doctor's
  must take a side in (`GAME_CAUSES`, `NOT_GAME`), as review found `vram_overflow` on neither; and nothing of another
  kind blamed on the game may sit beside those stutters (an FPS drop blamed on it passed under a "stutter" verdict in
  review). The pacing of a 120 FPS cap on a 144 Hz screen, which names no cause, passes: failing every event of
  another kind failed the owner's smooth run on it.
- **"Keep \"no stutters\""**: not chosen. Smooth would stay red whenever Windows itself hitches.

Also: "only 1 stutters and FPS drops", "only 1 stutter and FPS drops" and "only 1 stutters came from the game's own
work" now take the singular. Revisit hook: `attribute._residual`
(the frozen frame and the held screen), `attribute._turbo_veto` (the clock), `e2e_truth.own_gaps` and the smooth
branch of `judge` (the control).

## 2026-11-11: Time behind another window is left out, and not_focused names only a cost it measured

A covered window read as lag: in the e2e run of 2026-11-09, LagLab drew 120 FPS behind another window while the doctor
said "2 stutters (worst 9483 ms) and low frame rate (13 FPS average)" and, at medium, that Windows gave the game less
CPU time. Windows showed almost none of its frames, and the few it did show carried display gaps of 1.3, 9.5 and
2.1 s (PresentMon holds each frame it did not show behind the next shown one, PM 2.4). The owner asked for the fix
("start the covered-window fix"); where it forked, the implementing session chose, after a three-lens critique of
the first design and a five-lens review of the change, and the owner has not weighed in.

What time behind another window is judged by:
- **A: nothing; it is left out and said (chosen)**: nobody saw those frames. The headline gives the seconds left out;
  a capture with every frame in it reads "frame timing was not measured" with that reason.
- **B: the game's own presents**: rejected. It read the e2e capture right (about 118 FPS), but many games slow
  themselves down behind other windows (NVIDIA's background frame limit, in-game options), which would read as FPS
  drops and as a hitch at each Alt+Tab against the in-front baseline, and PresentMon loses and mistimes frames it
  holds back (the rows after its lost stretch carried 34 and 42 ms where LagLab drew 8.3).

How a stretch is told from a frozen screen:
- **A: frames unshown for 1 s, every foreground reading a tick or more inside it "another window" (chosen)**: shorter
  unshown runs within a tick of one, every reading between them "another window", join it, and two stretches with
  every reading between "another window" and at most half the frames between shown are one (the e2e capture showed
  bursts of 1-12 frames between its runs).
- **B: the readings bracketing the run too**: rejected. An Alt+Tab covers the game as it takes the foreground, so
  the reading just before a cover and the one just after it read "in front": about 93 % of real covers would have
  been missed (the critique's estimate, one tick against one or two frames).
- **C: any unshown run with a reading inside**: rejected. Whether a frame or two went unshown at a tick was luck, so
  real hitches on a visible window would have been hidden at random.
- **D: a window-visibility reading instead of the foreground**: deferred, worth trying (backlog): it needs a new
  sampler column, and would tell a covered window from one visible on a second screen.

Readings "in front" at only one end of a run:
- **A: cut the run there (chosen)**: the part read "in front" is one frozen screen of its own length, from the last
  frame to the last such reading, or from the first one to the frame ending the run (a freeze before an Alt+Tab; the
  foreground back before a frame shows, as a game restored from minimized can be). A reading "in front" in the middle
  still leaves the whole run judged.
- **B: judge the whole run**: rejected. One such reading made a 6 s cover one 6 s stutter, the bug again.

A gap with no frame in it:
- **A: a run like any other (chosen)**: many games stop drawing while minimized or covered, and the frame after the
  gap read as a freeze of that length. The cost: a game visible on a second monitor that hangs for 1 s or more while
  not in front is hidden too.
- **B: always judged, as a stall is**: rejected for the minimized games; option D above would remove the cost.

What `not_focused` needs:
- **A: a cost it measured (chosen)**: the game's frame rate behind another window (its frames' count over their summed
  frame times) at least 10 % under its rate in front supports; no such cost clears, and so does, with too few frames,
  CPU use behind at 90 % or more of its use in front; less CPU behind with too few frames is no cost by itself (a game
  may slow itself down there), so that stays unchecked, and so does nothing to compare. In all five e2e modes where
  the old rule fired (cpu, hitch, hitch-io, hitch-wait and hog, 2026-11-09), nothing backed it: in the three with
  frames LagLab's own log shows it drew at its usual rate behind the window (39-40 a second in cpu both before and
  after its window went behind; 107-120 in hitch-io and hog), and the other two had no frames at all.
- **B: the share of time behind, on the CPU side (the old rule)**: rejected for that reason.
- **The frame rate from a median frame time**: rejected in review. Frame times that alternate between two values
  (judder, uneven pacing) put the median on one of them, 2x off either way.

Also: with no frames, PresentMon's own warning ("overflowed present events") is kept, and the status says why it
writes nothing for a window covered from the start. The lift test's "contradicts" is dropped where the cause's quiet
level is already its top (`attribute._blind`), as for a game behind another window most of the time. Left for later:
a measured part re-judges a cover cut by its edge on the cut length (carrying the whole recording's stretches into
the part needs `crop.py`), and the time a cover keeps no PresentMon row before the first or after the last.

Revisit hook: `frames.hidden_rows` (the stretch), `rules._not_focused` and `_focus_split` (the cost); the backlog's
window-visibility reading would replace the foreground as the stretch's second witness.

## 2026-11-10: Why five end-to-end modes failed, and what a LagLab run must now show

The owner's end-to-end run of 2026-11-09 (before PR #40) passed gpu, cpu and pace. Re-judged
in memory on main after #40, the other five still failed, none of them in the analysis. Decided by the session that
looked into it; the owner has not weighed in.

What the saved captures and LagLab's logs show:
- hitch and hitch-wait (no CSV at all), hitch-io (198 rows), hog (rows only from 7.4 s, 191 of 1,796 shown) and cpu's
  last 5.7 s: LagLab's window was not the foreground window (`game_focused` 0 at every sample of the four modes; in cpu
  from 17.0 s, and its rows stop at 17.08 s). PresentMon 2.6.0's default metrics hold every frame that was not shown
  behind the last one that was (`UnifiedSwapChain::Enqueue`), so a covered window gives few rows or none, and with no
  row no header. smooth lost the focus at 5.3 s with its window still shown, so its rows kept coming; LagLab's own log
  has one long frame after t = 0 (17.8 ms, 150 ms after the focus left), the control's one stutter.
- hog would have failed with every frame shown: LagLab's own frames held 8.34 ms through both bursts. Its game work was
  a timed busy-wait, which ends on time whatever else runs. Measured 2026-11-10 (LagLab's log, per-CPU load):
  - 32 hogs, LagLab in front: every CPU at 95 %, LagLab's frames 8.34 ms (p95 8.36 ms);
  - the same with LagLab spinning out its frame cap instead of sleeping: 8.34 ms;
  - hogs at above-normal priority: 8 FPS in the bursts (median 105 ms);
  - 6 ms of fixed work, a counted loop: 11.4 ms in the bursts (86 FPS), 8.34 ms between them;
  - LagLab losing the focus mid-burst: the hogs' load on the performance cores fell to 68-90 % while the efficient
    cores stayed at 99 %. Windows gives the processes descended from a window's owner that window's QoS; in the owner's
    run, with the window never in front, the 32 hogs added about 4 logical CPUs of load.
- And the e2e's hog run held 2 bursts, while the doctor names background programs only from 5 lag moments
  (`attribute.LIFT_MIN_EVENTS`): no capture of it could have named the hogs. The synthetic model passed because it
  assumed a 2-3x stretch and a 15-50 ms wait on 10 % of frames, never measured.

Forks:
- **Every mode's first check is that LagLab's window kept the focus for the whole capture (chosen)**, from LagLab's
  own log of the changes (`FrontWatch`, `e2e_truth.front_check`); `e2e.py` tells the owner to leave the PC alone.
  - **Keep LagLab's window topmost**: deferred, worth trying (Backlog).
  - **Leave such a mode unjudged instead of failed**: rejected; an unjudged mode reads as nothing to fix.
- **Hog mode's game work is a fixed count of loop turns, measured in the warmup (chosen)**: the 90th percentile of the
  warmup frames' counts for 6 ms, since the warmup overlaps the recorder's start; and the hogs burn nothing before
  their start (`hog.py` used to run its on/off pattern backwards into the warmup).
  - **Hogs at above-normal priority**: rejected; 8 FPS, and they would starve lag-doctor's own samplers, which run at
    normal priority, just when those must measure.
  - **LagLab spinning instead of sleeping**: rejected, measured: no slowdown.
  - **The hogs tagged high QoS** (`SetProcessInformation`, `ProcessPowerThrottling`), so their placement no longer
    follows LagLab's window: rejected; the front check fails a run whose window was not in front, and in front the
    hogs have the window's high QoS anyway.
- **3 s bursts every 5 s, whole bursts only, and 40 s for hog mode in the e2e (chosen)**: 7 bursts, 6 of them a
  rolling window into the capture (`tests/test_e2e_schedule.py`). On the model refit to the measurement
  (`synth.HOG_STRETCH`) and timed like the e2e's capture (it starts 2.1-2.7 s after LagLab's first frame), 40 of 40
  runs passed the judge, the lift test supporting in all of them (lift 45 or more, 2 needed). A burst cut short by
  LagLab's exit is never started: it
  is too short for a lag event, and the doctor then took its busy seconds for quiet ones, which on 30 s of the model
  (3 on, 3 off) hid every other burst (0 of 8 events blamed on background programs).
  - **6 s on, 6 s off (the old)**: rejected; 2 bursts in 25 s.
  - **3 s on, 3 s off**: rejected; it passed the same 40 runs, but a 30 s capture that starts just before LagLab (the
    model's, LagLab from 0.4 to 29.8 s) then holds 4 whole bursts, one short of the doctor's minimum: 4 of 40 runs fell
    short. With 2 s off, 5 fit there.
- **Hog mode's frame-rate verdict is now cpu_main (changed on purpose)**: to fall below 80 % of the 120 FPS cap under
  the measured 1.6-1.8x stretch, the work must fill at least ~76 % of the frame budget, and with the cap's last 1.5 ms
  spun out the main thread reads 98 % of a CPU; capped needs it under 85 % (LC 3.2).

Also found, left for a change of their own: the doctor reads a covered window's unshown frames as lag (hog's capture
reads "2 stutters (worst 9483 ms) and low frame rate (13 FPS average)" while LagLab drew 120 FPS), and the frame
source said only "PresentMon ran but wrote no CSV", leaving out PresentMon's own warning (overflowed present events).
Both were fixed on 2026-11-11 (the entry above).

## 2026-11-10: A red-first run that hangs is a TIMEOUT, ended with every process under it

`scripts/redfirst.py` ran each pytest under `subprocess.run(timeout=600)` with no handler, so a sabotage that made a
test hang would have crashed the whole run and lost its verdicts. Decided in the session that built the fix, from the
repo's red-first rules; the owner was not asked.

What a hang means:
- **A: its own verdict, TIMEOUT, not caught (chosen)**: the run exits 1. A hang names no failing test, a deadlock the
  sabotage causes while loading looks the same as a test noticing, and a wait without a bound is the test's defect;
  bounding it turns the hang into a red that names its line.
- **B: count a hang as caught**: rejected for those reasons; it would also make every unbounded wait in a test pass
  red-first.

How the run is ended:
- **A: a job object (chosen)**: pytest starts suspended and joins a job of its own before it runs any code, so every
  process it starts, at any depth, is in the job; `TerminateJobObject` ends them, and the job's process count must
  reach zero within 15 s or the run stops (exit 3).
- **B: `taskkill /T /F`**: rejected: it gives no count of what is left, so "nothing survived" could not be checked.
- **C: `subprocess.run`'s own kill**: rejected, measured: it ends only the venv's launcher, and of the 3 processes
  under it, the 2 a test had started kept running.

Revisit hook: `_Job` and the TIMEOUT branch of `_mutation` in `scripts/redfirst.py`.

## 2026-11-09: How the bottleneck audit's eleven analysis bugs were fixed

The audit of "which part of a PC limits a game" found eleven analysis bugs; the owner said "Fix all eleven". Where a
fix forked, the implementing session chose; the owner has not weighed in.
- **The headline's lead (chosen)**: the steady limit leads when the steady low-FPS time is longer than the events the
  best-ranked claimer claims, and that claimer stays as a side note (the limit, when the claimer leads), so a mixed
  lag names the cause of each part.
  - **Keep the claimer, document that the headline and Top cause can differ**: rejected; a GPU-bound recording's
    headline blamed background programs for 4 FPS drops.
  - **Lead with whichever finding explains the most lag time**: deferred, worth trying; it reorders stutter headlines
    LC 5.4 ranks by confidence. Revisit hook: `diagnose._lead`.
- **Throttling's lift test only supports (chosen)**, and adds no line when it does not: LC 5.2's "contradicts" is for
  stutter causes.
  - **Count "contradicts" only where the throttle could have risen**: rejected; throttling's claim is the steady
    frame rate, which no stutter speaks to.
- **The busiest thread, judged on the thread busiest over the previous second (chosen)**: a new sampler column,
  `game_lead_thread_pct`, read at the thread's share in the current tick; unbiased for steady loads.
  - **Each thread's 1 s mean, then the maximum**: rejected; four threads at 80 % still read 85 %.
  - **The top threads saved per tick, each one's median, then the maximum**: deferred, worth trying; unbiased even for
    work that hops between threads every second, at the cost of the capture format. Revisit hook:
    `sysmon.ProcessRates.update` and `frames.busiest_column`.
- **Under VSync at k >= 2, a thread whose own CPU time a frame passes k - 1 periods is the limit (chosen)**, in place of
  MsCPUBusy, which counts waits outside Present().
  - **Only a saturated thread**: rejected; a thread VSync holds waits for the refresh and is never saturated.
- **No CPU-side rule clears a CPU that `cpu_limit` holds open (chosen)**: one predicate (`rules._cpu_not_ruled_out`)
  for `cpu_limit`, the cpu gate, `not_focused`, `cpu_throttle`'s thread line and the headline's low-FPS words. It
  holds LC 3.2's grey zone, a
  saturated thread under VSync at one refresh a frame and an older capture past VSync's work edge, and never at the
  display's rate, where a CPU limit costs nothing the player sees.
  - **Hold only the grey zone**: rejected; under VSync at one refresh a frame a 90 % thread was then ruled out where
    an 84 % one was held.
  - **Leave out `cpu_throttle`'s thread line wherever the CPU is held open**: rejected; LC 4.5's silence at 0.69 needs
    its weight, so the line stays, worded as under the 70 % at which CPU speed starts to matter.
- **VSync judder at any k (chosen)**, judged on the two adjacent hold counts with the most frames, with uneven
  presents taken for VSync's when 90 % of the holds sit on those two counts (`VSYNC_SHARE`; a real recording: 92 %)
  and judder is found there, never under a shared GPU's unknown verdict. Holds that slip both ways are neither judder
  nor fine: the texts name one side, and a cap at refresh / 2 slipping 5 % each way is no judder to fix. A pair is
  named only when at least half of the frames on its side sit on its other count, or, around a usual hold of two or
  more, when that count holds 10 % of all frames itself.
  - **Holds that slip both ways stay unnamed (the owner's choice, 2026-11-10: "do A")**: uneven pacing still reports
    them where the game's own presents swing. Counting both sides from 10 % was rejected, since it flags a steady
    72 FPS cap that slips 5 % each way and looks smooth; counting them from 20 % is deferred, worth trying (Backlog).
  - **Any frame at that count**: rejected; one frame in a thousand at three brought back "two and three" and a 48 FPS
    cap for frames held four refreshes around a usual two.
  - **Half of that side only**: rejected; a light stretch alternating two and three (26 % of all frames at three)
    went silent beside a heavier one held four and five.
  - **Judder judged per stretch**: deferred, worth trying; around a usual one refresh the pair still needs half its
    side, since a long menu at the refresh rate looks the same over the whole capture. Revisit hook:
    `rules._refresh_holds`.
  - **Count frames off the usual hold on both sides as judder**: rejected; the title, the summary's range and the cap
    then named the wrong side (10 % held one refresh around a usual three read "three and four").
  - **Only up to two and three refreshes**: rejected; an invented bound. LagLab's gpu and cpu modes (3 or 4 refreshes)
    now read low FPS and uneven pacing. LagLab presents with VSync off, Windows' composition holding its frames on
    whole refreshes, so the finding's VSync wording and its "turn VSync off" advice are wrong there: a backlog item.
  - **The same exemption where most frames take one refresh**: deferred, worth trying. Revisit hook: `usual >= 2` in
    `rules._vsync_shaped`.
- **A game server or router that never answered a ping has no loss events (chosen)**; the internet anchor keeps them
  (an outage).
  - **The game's own packets as the arbiter of ICMP loss**: deferred, worth trying; it would also cover rate-limited
    servers and one that answered, then stopped while its packets kept arriving. Revisit hook:
    `diagnose._ignored_losses` with `rules._packet_loss`'s list of far losses.
- **Disk reads capped at what the physical disks read, and none without their counter (chosen)**.
  - **The game's reads, worded "from disk or from memory"**: rejected; the title and the SSD advice would still claim
    disk loading.

Revisit hooks: as named above, and `tests/redfirst/analysis_rules.json`, `diagnose.json`, `analysis_frames.json` and
`sysmon.json`, which pin each choice.

---

## 2026-11-09: The start key in `watch` (M5's second part)

`watch` gets a second key beside the lag key: the start key, Ctrl+Alt+S by default. Its first press after the
recording has started begins a fixed window, 10 minutes by default, and only that window is analysed (the plan's
"Measuring the right part"). The owner settled three forks on a question card (2026-11-09); the implementing session
settled the rest, after eight readers mapped the listener, the recording loop, the control channel, the sweep, the
capture, sound, the tests and the docs.

The owner's:
- **On by default in a terminal (chosen: "On by default (Recommended)")**: `lagdoctor watch` measures from the start
  key, and `--start-key none` records as before, the whole session.
  - **Only when asked**, with `--start-key KEY`: not chosen.
- **No press: the whole recording, labelled (chosen: "Whole recording, labelled (Recommended)")**: when the start key
  is on but was never pressed, could not be registered, or was pressed too late for an 11 s part (10 s until
  2026-11-10), the whole recording
  is analysed, startup included, and both reports say so and name the `analyze --from` command that picks a part.
  - **Nothing until the player sets the start** (the plan's rule for the app's Results): not chosen for the terminal.
    The owner chose the whole recording, labelled, for the app too (2026-11-17, M5c).
- **Sounds in a terminal watch (chosen: "Yes, with --no-sound (Recommended)")**: a rising chime when the start press
  counts, three notes when the window ends, a low tone when something went wrong, a blip per lag-key press;
  `--no-sound` turns them off.
  - **Silent, as before**: not chosen.

The session's:
- **One listener per key (chosen)**: the session calls its hotkey factory once for each key, so each key has its own
  `HotkeyListener` thread, and every stand-in in the tests keeps its shape.
  - **One thread holding both keys under two ids** (the plan's wording, "a second key on the existing listener"):
    deferred; worth it only if a thread per key ever costs something, since it needs a result per key and a new
    factory signature. Revisit hook: `Session._start_hotkey`.
- **Only the first press after "recording" counts (chosen, the sweep's rule)**: an earlier, later or repeated press
  changes nothing.
  - **A second press ends the window early**: deferred, worth trying (Backlog). **A second press restarts the
    window**: rejected, it throws away measured time.
- **A start key Windows will not give records anyway (chosen)**: said at once, while the player is still at the
  terminal, and kept as a note; the recording runs as with `--start-key none`, and its whole is analysed, labelled.
  The line says the recording no longer stops by itself, and advises closing another program only when Windows said
  one holds the key (`HotkeyListener.taken`).
  - **Refuse to record** (the sweep's way, which it keeps): rejected for watch, whose recording is still worth having.
- **The press is asked for only once the key is live (chosen, after the review)**: the session registers the key
  after reading the PC's settings, seconds after the "Recording." line, so the line says to press once the key is
  ready, and the session says "Ctrl+Alt+S is ready" with a short chime (a new "ready" tone, a feel number) when
  Windows has given it. An earlier press went to the game unrecorded.
- **What a line promises holds (chosen, after the review)**: the measuring line names the recording's set length when
  `--duration` ends it first, the `start_key` event waits for a press only up to that length, and a sweep run's lines
  leave out watch's own advice (`cmd_watch(start_advice=False)`), since the sweep words and fails such a run itself.
- **A press too late for a part measures nothing (chosen, after the second review, 2026-11-10)**: a first press that
  leaves less than 11 s (`session.MIN_PART_S`) before `--duration`'s end, past it included, is refused as it comes:
  no `measuring`, no rising chime, one line saying why, and the report says the key "was not pressed in time". It
  used to count, be announced and chimed, and then be analysed whole anyway (or, a few ms past the end, be told with
  `until` before `at`, which the control channel refuses). A recording that ends early in another way (Ctrl+C, the
  game's exit) is still judged by `crop.key_part` afterwards. The rule is judged to the millisecond, as a part is,
  and the line says the time that was left. Tests that press keep a set length a part fits in.
  - **A `--duration` of a part or less turns the key off (chosen, after the fourth review)**: watch never registers
    it, says so once, and the capture has no `start_key`, so the reports treat it as `--start-key none`.
  - **Offer it and refuse every press, or have the session withhold it at the ready point**: rejected; the first
    promised a press that could never count, and the second left a `start_key` record that the reports then read
    as "not pressed in time" after the console had said the key was off.
  - **Count it, and cut its part at the end the session announced** (save `until` in `start_key`, so a part bounded
    by `--duration` also stops at the set length rather than at the recorded end, which includes the stop's own
    seconds): deferred, worth trying when the app's half shows `until`; it changes the capture's record. Revisit
    hook: `Session.start`, the `start_key` record in `Session._assemble`, and `crop.key_part`.
- **The window's end stops the recording (chosen, the plan's step 6)**, 3 s later for PresentMon's late rows; it is
  saved and analysed while the player plays on. With no press, the recording stops after 20 minutes (`--start-by`).
  - **Record on until the game closes**: deferred to M6 (Play's "after the game closes").
- **The press is saved in the capture (chosen)**: a new optional field, `start_key` {key, registered, window_s, at},
  after the `note` precedent. The end-of-watch analysis and a later `analyze CAPTURE` find the same part through
  `crop.key_part`, and `analyze --from/--to` still picks any other.
  - **Only in diagnosis.json or summary.json**: rejected, `analyze` could not find the part again. **In `target`**:
    rejected, older builds would read it silently.
- **watch owns the window and the cap; the sweep follows its events (chosen)**: a sweep run passes `--start-key`,
  `--window` and `--start-by`, plays its chimes from watch's `measuring` and `stopping` events, and keeps its early
  check that the key is free. It holds the key while the game starts, as it did before (a program that registers the
  key then cannot have it), and lets go as the recording starts. The key is then free until watch registers it,
  after the UAC prompt and the PC's settings are read: before M5b the sweep's own listener held it through that too.
  - **Hand the sweep's registration to watch** (a key factory that adopts the held listener and forwards its presses
    to `Session.start`, as the old `_StartKey.target` did): deferred, worth trying if a run ever fails with
    `KEY_TAKEN` taken in that gap; no program that does so is known. Revisit hook: `runner._record`'s `_let_go(held)`
    and the `hotkey_factory` that `capture/session.Session` takes.
  - **The sweep keeps its own listener and timers**: rejected; two copies of one rule drift, and both would race to
    stop the recording.
- **One player per sound (chosen)**: a terminal watch plays its own; under `--control stdio` the app keeps playing
  its own, and a sweep run its own.
- **The app records the whole session until M5c (chosen; M5c gave it its own key, 2026-11-17)**: its recorder passed
  `--start-key none`, so M4's screen, clock and words stayed true. A terminal recording measured from its start key still shows in the app, so Results say
  which part its numbers cover, draw only that part's presses, and a diagnosis.json it lacks is made again for the
  same part (`crop.key_part`); the shading of the rest waits for M5c.
- **A sweep run's report and its numbers may differ by a frame at an edge (accepted)**. **One rule for both**:
  deferred, worth trying (Backlog).

Revisit hooks: `Session.start` and `Session._watch` in `capture/session.py`, `crop.key_part`, `cli._record`.

---

## 2026-11-09: A measured part keeps the recording's own times (M5's crop)

M5 analyses only part of a recording: from the start key's press, or a part the player sets (`analyze --from/--to`
first, the app's trim handles later). The plan names one seam, `crop(cap, t0, t1)` before the unchanged `diagnose()`.
How the cut works was the implementing session's call, after eight readers mapped every place the analysis, the
reports and the app read a time or a total (the owner has not weighed in).
- **Keep the recording's own times (chosen)**: rows keep their t, `duration_s` becomes the part's length, and the part
  is recorded on the cut copy (`Capture.span`), which diagnose copies into `stats["span"]`. A cropped diagnosis's
  events, marks and moments land on the whole recording's charts as they are, and the times the reports and the app
  show stay the ones the status line showed. Its cost is one seam: what counts "from the start" counts from the
  part's start (`_Env.start`, shader_compile's first third), and the words name "the measured part".
  - **Rebase every time to the part's start**: rejected. Times also sit in text (a source's "stopped at t=812.3 s",
    a note, evidence lines) that cannot be shifted, nine kinds of nested network times would each need shifting
    (one missed misaligns silently), and the report, the app's chart, its moment view and the trim handles would add
    the start back at about a dozen places.
- **Totals over the whole recording become None in a part (chosen, the plan's)**: each program's network bytes, the
  game's own and the server flow's packet counts. A launcher's download during the startup would otherwise be named a
  bandwidth hog in play.
  - **Keep them**: rejected, for that reason. **Split them by time**: deferred, worth trying (Backlog: per-program
    bins).
- **The probe counts are counted again over the part (chosen)**: exact, from the lost probes' own times and the rows
  that hold a round trip, so the packet-loss figure and rule cover the part.
  - **None, like the other totals**: rejected; the loss figure and the steady-loss evidence would be lost for every
    part.
- **A frame whose own interval began before the part is left out (chosen)**: a start set at the end of a loading
  screen would make the whole wait the part's worst frame. That is any row whose time since its chain's previous
  present reaches back past the part's start, and each chain's first shown row whose display change does (the review
  found a load whose first present was never shown, so the next, shown, frame carried the wait).
  - **Keep it**, as the recording keeps PresentMon's first row: rejected, for the wait. It is kept only for a part
    from the recording's own start, so such a part frames as the whole does.
  - **Clip it to the part** (count only the time after the start): deferred, worth trying if a freeze that runs into a
    part should show in it. Today a part whose start falls inside a 13 s freeze reads "No lag stood out"; clipped, a
    start inside a loading screen would count the rest of the load. Revisit hook: `crop.frames_inside`.
- **The finding text names "the measured part" (chosen)**: about 22 strings that said "of the capture" read
  `_Env.part`. Leaving them with only a header note was rejected: a finding would claim the whole recording.
- **summary.json's `duration_s` is the length its numbers cover (chosen)**, with `recorded_s` beside it, so Compare's
  "Measured length" stays true; the library can show both.
- **A part is at least 11 s (chosen; 10 s until 2026-11-10)**, a second past `frames.BOUND_MIN_S`. At exactly 10 s no
  part ever got a verdict on what limits the game: the frame that began before a part is left out, so its steady
  frames summed to 9.96-9.99 s. The analysis-audit session measured it on LagLab-like captures (`tests/synth.py`, the
  gpu, cpu, smooth and pace modes, two starts each), and every part of 10.5 s or more got one. A test now holds that a
  part of exactly the minimum can get a verdict.
  - **Keep 10 s and let a short part say it had no verdict**: rejected; `watch --window 10` would then never get one.
- **The inventory stays as recorded (chosen for now)**: read once before the recording starts. **Read the power
  settings again at the start key's press**: deferred, worth trying (Backlog), with M5's second half.

Revisit hooks: `lagdoctor/analysis/crop.py` (`NET_FLOWS`, `frames_inside`, `MIN_SPAN_S`), `_Env.start` and
`_Env.part` in `analysis/rules.py`.

## 2026-11-09: The headroom estimate comes from thread time, and is context, not evidence

A 2026-11-09 audit found the bound verdict names one limit and nothing about the next, so it cannot say how much a
faster or cooler part would help. `frames.headroom` now gives each side's ceiling (DESIGN, Headroom).
- **The CPU's ceiling from thread CPU time, as a range from the 0.85 saturation edge to 1 (chosen)**: its busiest
  thread, or every core where that is busier.
- **From PresentMon's `MsCPUBusy`**: rejected; it is wall time from one `Present()` to the next, so in a GPU-bound
  game it fills the frame (16.55 ms of a 16.9 ms frame, `docs/learnings.md`) and only restates the frame rate.
- **As evidence for or against `gpu_limit` and `cpu_limit` (chosen: context)**: the sentence is inserted into their
  evidence after scoring, so no verdict or rank moves; a test holds the score equal with and without it.
- **Its own finding**: rejected; a headroom is not a cause of lag, and a finding of its own would compete in the
  ranking and the headline.
- **The console shows it under "limited by" (chosen)**, once, rather than only among the findings' first three
  lines, where a GPU limit ranked fourth would hide it; the HTML shows every finding whole, so it stays in the card.
Revisit hook: `frames.headroom`, `rules.headroom_text` and `rules._with_headroom`.

---

## 2026-11-09: A settings sweep, assisted first: lag-doctor edits the settings file, the player plays each run

Asked on 2026-11-08: could lag-doctor run short runs of a game with different settings (config files edited, or a
mod's files set aside), record each and rank them? The research (`docs/research/settings-sweeps.md`) found it possible
for single-player games, with every run restarting the game, and the hard parts in reaching the same scene each time,
the run-to-run noise and heat. Put to the owner with A recommended; the owner answered "do A) Assisted: lag-doctor
edits the settings file for each variant and undoes it afterwards. You start the game, load the save, press a key at
the spot and quit. It ranks the results with their noise."
- **A: assisted (chosen)**: lag-doctor writes each setting into the game's settings file while the game is closed,
  through the fix journal, and puts the original back at the end; the player starts the game, loads a save, presses a
  start key at a chosen spot and quits; each run is a recording measured over a fixed window from the key, and the
  settings are ranked against their own run-to-run spread. Nothing is typed on the player's PC. Each run still asks
  one UAC prompt, unless the account records frame timing without one.
  - This is an exception to DESIGN's "in-game settings are advice only", which the owner asked for: only for the
    length of a sweep, only keys already in the file, only while the game is closed, journaled and put back at the
    end, and never offered by `lagdoctor fix`.
- **B: unattended**, A plus menu keys and a close request sent by lag-doctor: deferred, worth trying (Backlog).
- **C: not now**: not chosen.

The implementing session's choices for `lagdoctor sweep` (DESIGN, The settings sweep); the owner has not weighed in:
- **Edits through the fix journal, as the fix `game_setting` (chosen)**, plus a byte copy of the file in the sweep's
  folder: a crash leaves an entry that the next start heals and `lagdoctor undo` puts back, and an undo never
  clobbers a value something else set since.
  - **Write the whole original file back at the end**: rejected. It would undo whatever the game or the player
    changed in the file during the sweep, and a crash would leave nothing that knows the file was changed.
- **Only values the file already has, with a strict value grammar, in .ini files (chosen)**: a sweep never adds or
  removes a line, so the game never meets a key its own build did not write.
  - **Adding keys (Engine.ini's console variables)**: deferred. FINAL FANTASY VII REBIRTH reads Engine.ini only through
    a proxy DLL (research note, section 2), which a sweep would first have to know is there. Revisit hook:
    `inifile.set_value`, which refuses a missing key today.
- **Each run through `cmd_watch` (chosen)**, with two new keyword seams, `remember` and `show_summary` (both False for
  a sweep's runs), so a run is an ordinary recording: the capture lock, the PresentMon check, the relay and its
  prompt, the save and the report.
  - **The session alone, with the sweep's own save**: rejected. It would copy watch's checks and drift from them.
- **The player presses Enter here once at the spot, before the recording starts (chosen)**: the UAC prompt then comes
  up while this terminal is in front, and the player goes back to the game for the start key.
  - **Start recording as soon as the game appears**: rejected for now. The prompt would come up behind a starting
    game, which only blinks on the taskbar. Play's prompt before the launch (M6) makes it worth trying. Revisit hook:
    the Enter loop in `runner._one`.
- **The sweep's own start key, Ctrl+Alt+S, registered for each run only (chosen)**, with watch's lag key off in a
  sweep's runs. Watch has no start key yet (M5 is building one): once it has, a run uses watch's, so one listener owns
  the key. Revisit hook: `runner._record` (and `docs/LAUNCHER-PLAN.md`, M5).
  - **The lag key as the start key**: rejected. A run's report would list the press as a lag the player marked.
  - **Wait for M5 before building the sweep**: rejected. The owner asked for the sweep now, and the seam is small.
- **A warm-up block, then counted blocks that rotate one shuffled order (chosen, after the review)**: drift spreads
  over every setting, a setting's slower first run is not counted, every setting takes every place in a block equally
  often, and A's runs in every block are the noise check the research asked for (A/A) without runs of their own.
  - **A fresh shuffle for every block**: rejected in review. It does not balance places: with one `--try` and three
    blocks, all three run in the same order one time in four.
  - **A separate A/A block first**: rejected. It costs runs that A's repeats already give.
- **Welch's t interval on average FPS and the 1 % low, in % of A's mean, widened for the number of settings compared
  (Bonferroni; chosen, after the review)**: "faster" or "slower" only when the interval excludes 0, and all the
  comparisons hold together 95 % of the time. At 2 to 10 runs a bootstrap's interval is too narrow (research note,
  section 4). The review simulated 5 settings with no real difference at 3 runs each: 13.6 % of sweeps called one
  faster or slower on average FPS alone without the widening.
  - **A rank test (Mann-Whitney)**: rejected. At 3 runs a side its smallest possible p is 0.1, so it can never call a
  difference.
- **A run that moved does not count and is offered again (chosen)**: the checks of DESIGN's sweep section. After the
  review they judge the whole window rather than its median (every focus sample but the edges', the share of samples
  with another program on the GPU), and keep out a window whose GPU clock moved (still heating up). A game update, a
  game that writes its own value back or drops the line, and an original changed between sessions end the sweep
  (state "ended", which `--resume` only shows).
  - **The NVIDIA power limit held within 10 % of the first good run's**: rejected in review. It moves by itself: in
    the first real capture it fell from about 85 to 55 W mid-recording with no plan change, and ASUS's Dynamic Boost
    moves it with CPU load, so a sweep could never finish. Armoury Crate's mode already shows as a plan change, which
    is held. The limit is recorded.
- **The journal says what is in the file (chosen, after the review)**: every outstanding `game_setting` entry of the
  file is undone, pending ones settled first, each tried even when another stays. A new sweep refuses while one is
  outstanding, every start says so, and `--resume` puts them back first whatever the sweep's state.
  - **The sweep's own list of its journal entries**: rejected in review. An edit that reached the file just before a
    crash was in the journal but not in the list, so nothing put it back, and the next sweep took it for the original.
- **A value the game wrote in place of lag-doctor's is put back only when the player says so (chosen)**: the sweep
  asks "Put 3 back? [Y/n]" once the game has closed. The project's rule is that an undo never overwrites a value
  something else set; here the player, who knows whether they changed the menu, decides each case.
  - **Always force the original back**: rejected. It would overwrite a change the player made on purpose.
  - **Never force it**: rejected. The game's own rewrite (a clamp, a reset) would leave the player's setting changed.
- **The file's other values are compared before each run (chosen)**: a change is said and the player decides whether
  runs count with the file as it is (recorded in the sweep's notes), or stops to put it back.
  - **End the sweep on any change**: rejected. A game may rewrite a value of its own at every exit, which would end
    every sweep after its first run.
- **Closing the console puts the settings back at once when the game is closed (chosen)**: a console control handler
  for the sweep's length, one write at a time with the runner. During a recording watch's own handler comes first.
  - **Only remind at the next start**: kept as well, for a close while the game runs and for a crash.
- **Feel numbers, for the owner to judge after a real sweep**: the 60 s window, 3 counted runs, the 5 minutes the
  start key may take, and Ctrl+Alt+S itself.

An adversarial review from three angles (the settings file and journal, control flow and threads, measurement and
statistics) before the pull request raised 40 findings; all were checked against the code and fixed. The adjudication
is in the pull request.

Revisit hooks: `runner._record` (the start key), `runner._one` (Enter, and where an unattended sweep would drive the
game), `results.problems` and `results.moved` (what keeps a run out), `plan.make_slots` (the order),
`runner.put_back_now` (what goes back, and when the player is asked).

## 2026-11-09: M4's end flashes the taskbar button and never brings the window forward

Play's step 7 has the window come back at the end, or flash on the taskbar if Windows will not bring it forward. In
M4 a recording can end by its length while the game is still running in front. The implementing session chose; the
owner has not weighed in.
- **Flash only (chosen)**: at `done`, while another window is in front, the taskbar button flashes until the window is
  activated (FlashWindowEx with FLASHW_TRAY and FLASHW_TIMERNOFG), and the "done" sound plays; the window stays put.
  The same goes for a prompt raised while the game is in front, with a knock instead of the chime.
  - **Bring the window forward, flashing when Windows refuses**: deferred, worth trying (Backlog), once Play (M6) ends
    a recording only after the game has closed. Today it would take the keyboard from a game still being played, and
    a fullscreen game minimizes when it loses the foreground.
- **The flash runs only on Windows' say-so (chosen, M4b's review)**: only while SHQueryUserNotificationState answers
  QUNS_ACCEPTS_NOTIFICATIONS, the one state Microsoft's page allows notifications in. The heartbeat asks again,
  stopping the flash (FLASHW_STOP) on any other answer and starting it on that one, so a player who steps out of the
  game and back does not leave it flashing over the game; coming to the front ends the wish, and an answered prompt
  drops its own. A flashing button can show an auto-hidden taskbar over a borderless game and keep it there; the sound
  already tells the player in the game.
  - **Flash at once**: rejected, for that taskbar.
  - **No flash, sound only**: rejected. A player who left the game and the room has nothing to see on return.
- **The view hides only while the game records (chosen)**: from `recording` until `stopping`, and only while the window
  is not the active one or is minimized. Before (the wait for the game, the prompt) and after (saving, analysing) it
  shows, since nothing is measured then.
  - **Hidden from Record to the end**: rejected. The prompt and the analysis would sit behind a blank window, with no
    measurement to protect.
- **A hidden view's window draws one still line of its own (chosen, M4b's review)**: "Recording. This window stays
  still while you play; switch to it to see the recording.", with "lag-doctor: recording" as its title, painted by the
  window itself (GDI), so a window still in sight on a second monitor is never a blank rectangle.
  - **Leave it blank**: rejected; a blank window reads as hung.
  - **Keep the view showing while the window can still be seen**: deferred, worth trying, if Windows' own occlusion
    state can tell "in sight" from "covered by the game". Revisit hook: `AppWindow.shown`.
- **A fixed length of 10 minutes (chosen)**, the plan's default, until M5 brings a choice and the start key. Stop ends
  it sooner, and so does the game's exit. Since M5c the 10 minutes run from the start key's press, with a limit of 31
  minutes (2026-11-17).
  - **Until Stop, as `watch` runs without `--duration`**: rejected. Stopping would mean switching to the window, which
    is what changes the measurement.

Revisit hooks: the end branch of `App.on_recorder` (`lagdoctor/app/main.py`), `AppWindow.update_view`
(`lagdoctor/app/window.py`) and `recording.LENGTH_S`.

## 2026-11-09: M4's relay prompt is raised by the window, which hands the relay's handle to the recorder

The plan has the app raise the UAC prompt with its own window ("asks for UAC with its own window") and the recorder
check the relay against the pipe's server. In M3 the recorder raised the prompt itself and kept the relay's process
handle, which pins the relay's pid and gives its exit codes (3: the game changed during the prompt; 6: PresentMon
failed its check). The implementing session chose; the owner has not weighed in.
- **The window hands the relay's handle to the recorder (chosen)**: after the prompt it copies its handle into the
  recorder with DuplicateHandle (it holds the recorder's own process handle from starting it), closes its own and
  sends `elevated {handle, pid}`; the recorder adopts it (`ElevatedChild.adopt`). Every check M3 has stays as it is,
  the recorder holds the handle as DESIGN says, and a window that dies after the hand-over takes nothing with it.
  - **The window keeps the handle and forwards `relay_exit {code}`**: rejected. The pid's pin and the exit codes
    would live in another process, kept in step by messages, and a window that dies would drop both.
  - **The recorder raises the prompt with the window's HWND**: deferred, worth trying (Backlog). Microsoft's archived
    Vista UAC guidance says the HWND given to ShellExecuteEx decides whether a prompt is foreground or only blinks on
    the taskbar, and never says a window of another process counts; an agent cannot raise a prompt to find out.
- **A Cancel ends watch with nothing recorded only until the recording begins (chosen)**; after that it is a Stop,
  and the capture is kept, as the CLI promises that nothing the user does loses a started recording.
  - **Discard a started recording on Cancel**: rejected. It breaks that promise, and the player has spent the time.
- **A closed window never ends a recording (chosen)**: stdin's end is no stop; the recorder records on to the game's
  exit or the duration, saves, and the app lists the session when it opens again. Amended in M4b's review: this holds
  once the recording has begun. Before that, a window that goes away is a Cancel, because otherwise a window closed
  during the prompt left a 10-minute recording without frame timing that no window could stop, and that held the
  capture lock against the next Record.
  - **Stop and save at once when the window goes**: deferred, worth trying (Backlog).
- **The recorder takes its parent for the window (chosen)**: a Python that started before it counts alone
  (`tool_alone`), and its `msedgewebview2.exe` children are roots found in each snapshot (`tool_roots`), while the
  window is still that process. This holds because the window starts the recorder with its base interpreter, as the
  recorder starts the relay: through a venv's `python.exe`, a launcher, the parent would be the launcher, and the
  relay's handle would be handed over into the launcher (M4a's review measured both). So the window also checks
  `hello.pid` against the process it holds before the hand-over.
  - **A `--window PID:CT` argument**: rejected. With the base interpreter the parent is what started the recorder,
    and an argument would only restate it.
- **A Cancel reaches the session too (chosen)**: until `recording` goes out, the session checks it once everything
  has started (the inventory and the relay's network trace can take seconds after the last check in `cmd_watch`),
  stops what it started and keeps no capture and no folder.
  - **Turn Cancel into Stop once the session starts, and say so with an event**: rejected. The player's natural
  moment to cancel, on seeing the game `target` names, falls in that gap, and it would save an empty recording.

Revisit hooks: `control.Channel.relay_launcher`, `cli._check_cancel` and its four places in `cmd_watch`,
`control.window_of`.

## 2026-11-09: A recording's note, `watch --note TEXT` (B of "Compare shows what changed in the game itself")

The owner chose B to follow A (2026-11-08, below): the player's own words, for what the game-folder check cannot see
(an in-game setting kept in the registry or a save file). The implementing session's choices:
- **A capture field of its own, `note`, copied into summary.json as an optional key within `lagdoctor.summary/1`
  (chosen)**, as A's keys are. The note is the capture's, so `analyze` keeps it, and an older build reads a capture
  holding one with its usual note about fields it does not know.
  - **An inventory key**: rejected. The inventory is what the PC reported, and the rules read it; a note is the
    player's word.
  - **A file beside the capture** (`note.txt`): rejected. `analyze`, and a capture shared on its own, would lose it.
- **Refused past 200 characters as kept, before anything starts (chosen, as the brief asked)**: the parser says so
  before the lock, the game search or the UAC prompt. A note read from a file (a newer build's, which may allow more)
  is cut to 200 ending in "...", never refused: a line of text is no reason to lose a recording.
  - **Cut silently at the prompt**: rejected. The player would find a shorter note than they typed only in the report.
- **One cleaner for every writer and reader, `model.clean_note` (chosen)**: the parser, `_record`, the capture's load,
  the summary's build and read, the report and Results all go through it, so no path shows an uncleaned note. A
  character that is not printable (`str.isprintable`, the summary's own rule for text) becomes a space: control
  characters, a lone surrogate from a broken command line, private-use characters, spaces other than the plain one,
  and the marks that reorder text (U+202E), which could make a shared report's note read backwards. Then each run of
  spaces becomes one, and an empty note is none. Left as it is: an emoji built with a zero-width joiner shows as its
  parts with spaces between.
- **Set by `_record` once the session returns (chosen)**: `capture/session.py` stays as it is while M2's seams are
  built there. The relay is never given the note: `relay_argv` is built field by field, and a test runs an elevated
  watch with a note and checks that nothing the relay is started with holds it.
  - **Through `SessionOptions`**: deferred until M4 records from the app (backlog).
- **A capture holding a note of another type is "damaged" (chosen)**, as one holding any field of the wrong type is;
  a change of type would be a change of format (`lagdoctor.capture/2`).
  - **Read it as no note, with a line in the capture's notes**: rejected. The note would be the one field the format's
    check lets through.
- **Where it shows (chosen)**: under the HTML report's header, through `e()` (a folder under the profile shows by its
  variable) and never as prose (a backtick stays a backtick); first among Compare's conditions as "Your note", "-" for
  none, differing only when both recordings have one and they differ; under the date on a game's recordings (the
  session's own call: it tells two recordings apart before they are picked for Compare); and on Results. The console
  summary does not repeat it: the player typed it a minute before.
- **Adding a note to a past recording** (`lagdoctor note <session> TEXT`): deferred, worth trying (backlog). Not built
  unasked: it is the first thing that would change a finished session.

Revisit hooks: `NOTE_MAX` and `clean_note` in `lagdoctor/model.py`; `_note` and `_record` in `lagdoctor/cli.py`;
`summary.NOTE_KEY`; the "Your note" row of `compare_view` in `lagdoctor/app/views.py`.

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

## 2026-11-09: Play has Explorer open the store's link, so a store client that has to start is Explorer's child

The planning session chose that Play refuses while the store client is closed (2026-11-08, below), because the link,
opened by lag-doctor, would start the client as lag-doctor's child. Steam's own log on the owner's PC then showed
launches in which the link itself started Steam, so the refusal would come up often (`docs/research/desktop-app.md`,
section 14). Put to the owner with Explorer recommended; the owner chose it ("do B) have Windows Explorer open the
link, so a Steam that has to start is Explorer's child, as if you'd clicked its icon (the plan's deferred option)").
- **Explorer opens every link Play opens (chosen by the owner)**: lag-doctor asks the desktop's folder view for
  `IShellDispatch2::ShellExecute` (research note, section 5). The handler's process, and a client that has to start,
  are then Explorer's children, as when the player clicks the store's icon, and lag-doctor creates no store process
  at all. It is one path whether or not the client runs, so nothing has to tell a closed client from a closing one.
  It costs COM calls through ctypes and a running Explorer: when the desktop cannot be reached, Play refuses and says
  so, and never opens the link itself. The window in front is lag-doctor's, not Explorer's, so M6 checks that the
  game's window and the store's own prompts still come to the front.
  - **Refuse while the store client is closed** ("Start Steam, then press Play"): rejected by the owner. The
    simplest, but the player would often have to start Steam by hand first.
  - **Open the link from lag-doctor anyway** (ShellExecuteEx without `SEE_MASK_NOCLOSEPROCESS`): rejected by the
    owner. A client that has to start would be lag-doctor's child: Task Manager's End process tree on lag-doctor
    would close it, and it would inherit lag-doctor's environment.
Revisit hooks: `winapi/launch.py` and the launch policy module (`docs/LAUNCHER-PLAN.md`, M6).

## 2026-11-08: Compare shows what changed in the game itself, read from the game's folder at record time

Compare (screen 5 of `lagdoctor app`) set two recordings side by side with four conditions (power plan and mode,
refresh rate, how frames were timed, measured length), none of which a fan fix changes. A fan fix is a file beside
the game's exe (a DLL Windows loads from there instead of System32, an ASI loader and its plugins), a user
`Engine.ini`, or an in-game setting, so two recordings could differ in their numbers under identical conditions. Put
to the owner with A recommended; the owner chose "Both, A first".
- **A: automatic, at record time (chosen, built first)**: the inventory reads the game's folder (never the game's
  process) and lists the files a fan fix adds, with a fingerprint of each, and for an Unreal Engine game the user
  config files. Nothing to type or forget; it cannot see an in-game setting a game keeps elsewhere.
- **B: a note per recording, `lagdoctor watch --note TEXT` (chosen, built second as #29)**: the player's own
  words, kept in the capture and the summary, shown in the report and Compare. It catches anything the player names,
  an in-game setting included, but only when they remember to type it. It changes `cli.py` and
  `capture/session.py`, which the M3 relay PR (#23) was rewriting, so it followed that PR. Built 2026-11-09 (above).
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
owner's, answered on 2026-11-08. The planning session chose the rest; the owner has since chosen otherwise on two
of them (marked below).
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
  (Amended by M6d, 2026-11-17, above: for Play the prompt approves the install folder, and the game comes over the
  pipe in a `game` message that the relay judges by that folder; the service will judge the same request by the
  caller's session.)
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
  (chosen provisionally; the owner chose otherwise the same day, above)**: the elevated side hands over nothing about
  other programs. `watch --elevate` then stops naming a program that filled the connection, and `bandwidth_hog`
  falls back to the adapter's total.
  - **Per-program byte totals from the user's own session**: chosen by the owner (2026-11-08, "The relay passes back
    per-program byte totals from the user's own Windows session", above).
- **Play requires the store client to be running (chosen; the owner chose otherwise on 2026-11-09, above)**: opening
  the URI would otherwise make the client lag-doctor's child.
  - **Starting it through Explorer**: chosen by the owner (2026-11-09, "Play has Explorer open the store's link",
    above).
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
