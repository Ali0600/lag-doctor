# Decisions

## Backlog: alternatives worth trying later

- **A weekly CI run that skips the PresentMon cache**, so the real download also runs when nothing in the repo
  changed (GitHub moving its download hosts, say). Revisit hook: an `on: schedule` trigger in `ci.yml` and a
  condition on its "Restore PresentMon from the cache" step.
- **Own ETW frame tracer instead of PresentMon**: no download, but no GPU-busy timing and a lot of
  present-mode state to get right. Revisit hook: a second `Collector` named `frames` in `capture/`.
- **A live dashboard on a second monitor** (local web page fed by the session's `live()` snapshots).
  Revisit hook: `capture/session.py` already gathers `live()` once a second for the console line.
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

---

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
