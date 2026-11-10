# lag-doctor: agent contract

Read `docs/DESIGN.md` before changing anything structural: it is the contract (module interfaces,
collector `result()` keys, inventory keys, stats keys, fix ids). Exact Windows names (PresentMon columns,
counter paths, struct offsets, event layouts, registry values) are in the fact-checked notes under
`docs/research/`; where a note's body and its verification log disagree, the log wins.

## Commands

```powershell
uv sync --group laglab                                   # set up .venv on Python 3.13 (.python-version)
uv run pytest -q                                         # unit + live read-only tests
uv run pytest -q tests/test_<area>.py                    # one area (use this while other work is running)
uv run python scripts/redfirst.py tests/redfirst/<spec>.json   # prove a guard's test can fail
uv run python -m tests.downloads fetch-gold              # PresentMon's gold captures, for the replay tests
uv run python -m tests.downloads fetch-webview2-idl      # WebView2.idl of the SDK, for the vtable-slot tests
uv run python -m laglab --list-modes                     # the fake laggy game used by tests
uv run lagdoctor ...                                     # the tool (see README); the elevated child runs -m lagdoctor
uv build --clear --out-dir dist                          # the sdist, and the wheel built from it
uv run python scripts/wheel_check.py dist                # that wheel in a fresh venv, run outside the checkout
uv run pytest -q tests/test_msi.py                       # the MSI's build, WiX source and install check, no install
uv run python scripts/runtime_modules.py --zip <path>.zip [--write]   # what the pinned runtime imports, asked of it
uv run python scripts/webview_spike.py --auto            # M0: the WebView2 window's automated checks (the owner runs it bare)
uv run pytest -q tests/test_app_page.py                  # M1: the app's real window, driven on a demo library (~10 s)
uv run pytest -q tests/test_app_recording.py             # M4: the recorder the app starts, with a stand-in for it
uv run python -m tests.app_demo <home> [--pictures]      # a demo library of made-up games, under <home>\sessions
uv run python -m tests.app_harness <home> <out.json> [<shots>]   # drive the window on it; LAGDOCTOR_HOME=<home>
uv run python scripts/make_icon.py                       # rewrite lag-doctor's icon, lagdoctor/app/lag-doctor.ico
```

CI (`.github/workflows/ci.yml`) runs the suite on `windows-2025` for Python 3.13 on every pull request and on every
push to `main` except one that changes only `docs/**`, `CLAUDE.md` or `.github/SECURITY.md` (its `paths-ignore`; no job
reads them, and `tests/test_ci_triggers.py` keeps both the list and that claim true). Such a push starts no run, and
`gh run list --commit <sha>` then prints nothing and exits 0, so never wait for one: GitHub filters a push on its whole
range, so prove it with `git diff --name-only <base> <sha>`, where `<base>` is the head of the last push to main that
has a ci.yml run (`gh run list --workflow ci.yml --branch main --event push --limit 1 --json headSha`). Main is green
when that run concluded success and every later commit is such a push. `README.md` is not exempt: the build reads it
into the wheel's metadata. 3.13 is the Python the installer ships (`scripts/runtime_pin.py`); `requires-python`,
`.python-version` and the matrix follow it (`tests/test_python_versions.py`), and when the pin moves to a new minor
version, that version joins the matrix in the same change (`docs/DECISIONS.md`, 2026-11-08). Code and workflow
changes go through a pull request and merge on green. Before the tests it gets
PresentMon through `lagdoctor setup --download` and the gold captures through `tests.downloads fetch-gold`, checks
both against the tests' pins and caches them; each job's summary counts the tests that use them. Its wheel job runs
`uv build --offline` (with the copy of uv_build inside the pinned uv: `pyproject.toml`'s bound stays on that uv's
minor version, and `tests/test_wheel_check.py` fails when the two part, or when any workflow pins another uv than
ci.yml) and then `scripts/wheel_check.py`. Its msi job
first asks the pinned runtime what it imports (`scripts/runtime_modules.py`) and fails unless
`scripts/runtime_modules.json` is that answer, then installs WiX 7.0.1 from its SHA-512-pinned package
(`scripts/wix_tool.py`), builds the MSI (`scripts/msi_build.py`), keeps it as the `lag-doctor-msi` artifact, then
installs, runs and uninstalls it there (`scripts/msi_check.py`, whose verdicts are the job summary; msiexec's logs are
the `msi-logs` artifact, kept whatever ended the job). Both check scripts run their commands under one time budget
(`BUDGET_S`) that `tests/test_ci_budgets.py` holds under the job's `timeout-minutes`, so a hang still prints every
verdict ("not run" for what the budget left out) instead of the job being cancelled with nothing printed; when a check
grows, it shares the budget rather than outgrowing the cap. The Tests step stops a hung test after 15 minutes, with
every thread's stack printed once a test has run 5. `.github/workflows/webview-spike.yml` (the Window spike, about 1.5
min) runs the window spike's automated checks and a rehearsal on pull requests that change the spike or a module it
imports (its `paths:`, which `tests/test_side_workflows.py` keeps equal to what the spike's code imports, lazy imports
included): GitHub's runner has the WebView2 Runtime (156.0.4314.58 on 2026-11-08), and its "no
runtime" answer passes only when the runtime's registry version and exe agree. The rehearsal types step 2's keys, a
wheel turn and a right-click itself there, and its `keys-typed` check fails the job unless each arrived and nothing
acted. It does not run for the app's own code: M1's window tests (`tests/test_app_page.py`) are in ci.yml's test
jobs; to run the spike against a branch that changes none of its paths, `gh workflow run webview-spike.yml --ref
<branch>`. The PresentMon cache key hashes
`setup.py`, `paths.py` and `cli.py`, so a change to one makes every job download PresentMon afresh; GitHub's release
host answered 504 to whole jobs' tries on 2026-11-08: rerun the failed job before blaming the change.

## Rules an agent must not guess

- **Never touch the game process.** No injection, hooks, memory reads, or `OpenProcess` on the game. Use
  `winapi/ntquery.py` for other processes' PIDs, names, paths, CPU, threads, I/O and parents. `OpenProcess` lives
  only in `winapi/process.py` (fixes on the user's own background processes). `tests/test_no_handles.py` enforces
  both. The one exception is PresentMon's own child process (DESIGN.md, Principles). In tests, where psutil stays as
  an independent readout, `psutil.Process(pid)` (even constructing it), `psutil.process_iter()` and
  `psutil.pid_exists()` open a handle: `psutil.pids()` is the only psutil call allowed on other processes.
- **`lagdoctor/` imports only itself and what the installed runtime imports** (`tests/test_stdlib_only.py`): the
  installer ships python.org's embeddable runtime, which leaves tkinter, IDLE, turtle, venv and ensurepip out of the
  standard library (so no tkinter UI), and Smart App Control would block psutil's unsigned native module. The modules
  allowed are that runtime's own answer, `scripts/runtime_modules.json`, never this interpreter's
  `sys.stdlib_module_names`, which names the left-out ones too. When `scripts/runtime_pin.py` moves, rewrite the JSON
  with `scripts/runtime_modules.py --write` in the same change (CI's msi job fails until it is the new runtime's
  answer), and never edit it by hand. Read Windows through `lagdoctor/winapi/` (ctypes), and name a dynamically
  imported module in a string literal.
- **Tests never change real settings.** Registry fixes run against the `reg_sandbox` fixture
  (`HKCU\Software\LagDoctorTest\<uuid>`, deleted afterwards); power, display and GPU-preference backends
  are faked. Process-priority tests act only on a child process the test spawned. `LAGDOCTOR_HOME` is
  redirected for every test by `tests/conftest.py`; the real PresentMon exe and PresentMon's gold ETL
  captures come from the `presentmon_exe` and `gold_etl_dir` fixtures, which check them against the tests' own
  pins in `tests/downloads.py`. Absent, they skip with the reason, unless `LAGDOCTOR_REQUIRE` names them (CI
  does): then they fail, because there a missing download means a broken download step.
- **Red first.** A new guard ships with a mutation in `tests/redfirst/*.json` that turns its test red;
  `scripts/redfirst.py` must report every mutation `CAUGHT`. Never weaken a test to make it pass. The runner
  mutates a temp copy of the tree, never the working tree; so tests must not read the real repo by an
  absolute path (reported UNISOLATED) or write into their own tree (stops the run). It copies the tree when each run
  starts, so keep the checkout still until a queued run has begun. Editing a line that an existing mutation's `old`
  quotes makes that mutation BAD-SPEC, and no test notices: after an edit, check every `old` in
  `tests/redfirst/*.json` still occurs exactly once in its file, and re-point the ones that moved (same sabotage, new
  text). Most specs (`report.json`, `user_text.json` and `session.json` among them) are laid out by hand: change their
  values in place, never by rewriting the file through `json.dumps`. A script may rewrite a spec only after checking
  it is byte for byte `json.dumps(indent=1)` plus a newline (`first_capture.json` and `power_plan.json` are), and with
  `write_bytes`: Python's `write_text` writes CRLF on Windows. The copy holds `lagdoctor/`, `laglab/`, `tests/`,
  `scripts/`, `docs/`, `.github/`, `installer/`, `pyproject.toml`, `uv.lock`, `LICENSE` and `.python-version`
  (`COPY_DIRS`, `COPY_FILES`): a test that reads any other file fails before its sabotage (BAD-BASE) until that file
  is added there.
- **Not measured is not fine.** A collector that cannot run reports `unavailable`/`failed` with the reason;
  a failed sample is `None`, never `0`; rules that need a missing source are listed as unchecked.
- **The game folder check reads folders, never the game** (`capture/gamefiles.py`, what Compare's drop-in and Unreal
  rows show). Find the user's own folders through `winapi/folders.py`: OneDrive can hold Documents, so
  `%USERPROFILE%\Documents` may be the wrong place. Never open a file whose listing marks it online only (reading it
  makes OneDrive download it). A file size is `labels.byte_count`, never `charts.number`, which refuses anything past
  1e7 as corrupt: an 81 MB exe read as "not read" until a demo used a real size. A DLL name joins `PROXY_DLLS` only
  from a primary source, and never one on KnownDLLs (`tests/test_gamefiles.py` reads both lists).
- **Text a player reads is plain words.** Research citations (`WT 8`, `LC 4.9`, `NE 7 row 9`) and review ids
  (`CC-5`) go in comments, never in a string the report, console or a fix shows; `tests/test_user_text.py`
  scans every module but `winapi/`. Name sources with `labels.source_text`, never by their ids.
- **The HTML report is shared, so it never shows the Windows user name.** Put text on the page through `html.e()` or
  `html.prose()`, never `escape()` alone: `e()` also shows a folder under a user's profile as `%LOCALAPPDATA%\...`
  or `%USERPROFILE%\...` (`html.user_neutral`), and a command the page offers names it as `"$env:LOCALAPPDATA\..."`
  (`html.powershell_path`). `charts.py` escapes its own labels without that, so pass a chart only fixed words and
  numbers. Tests use a made-up profile (`C:\Users\Jo Tester` in `tests/test_report.py`). The console summary keeps
  real paths on purpose (DESIGN.md, Report).
- **Column names come from `lagdoctor/model.py`** (`F`, `S`, `G`, `N`); `SeriesBuilder` rejects unknown names.
- **One clock**: `lagdoctor.clock.now()` (QPC seconds). PresentMon runs with `--qpc_time`; its rows are
  timed by `TimeInQPC`, never by when they arrive (they arrive 0.1–2 s late, in bursts).
- **PresentMon**: parse its CSV by header name; on a pipe it is Latin-1 text, not UTF-16. Stop it with
  CTRL_BREAK (shared console) or `--terminate_existing_session --session_name lagdoctor`; never
  `Popen.terminate()` first, which leaks its ETW session until reboot. Exit code 6 is ambiguous: read stderr. Leave
  `--track_pc_latency` out: it switches on the latency-ping code inside games built with NVIDIA's Reflex markers
  (DECISIONS 2026-11-09; `test_pc_latency_tracking_stays_off` guards it).
- **Fixes go through `lagdoctor/fixes/base.py`** (journal first, readback, undo). Never offered: disabling
  security features, killing or suspending processes, HKLM or policy tweaks, anything on the game process.
- **ETW needs rights.** PresentMon (session `lagdoctor`) and the network tracker (session `lag-doctor-net`)
  need ETW rights: PresentMon an elevated token or Performance Log Users, the tracker an elevated token only
  (measured, E1 in `docs/research/install-and-rights.md`: Kernel-Network's own descriptor lets only SYSTEM,
  LocalService and Administrators enable it). Tests use private
  in-process sessions with unique names instead. The end-to-end run (`scripts/e2e.py`) is on demand. The venv's
  `python.exe` is a launcher: the real process is the base interpreter, so target LagLab by the PID it writes to
  `--pid-file`.
- **This laptop is hybrid** (RTX 3070 Laptop + Intel UHD; the internal panel is driven by the Intel GPU).
  OpenGL apps such as LagLab run on the Intel GPU by default; `SHIM_MCCOMPAT=0x800000001` in LagLab's
  environment moves it to the NVIDIA GPU. A long-running ML training job may be using the NVIDIA GPU: never
  stop it or change its priority, and keep heavy GPU tests on the Intel GPU.
- Windows PowerShell 5.1: `Out-File -Encoding utf8` writes a BOM (breaks commit messages: write with
  `[IO.File]::WriteAllText` and `UTF8Encoding($false)`), and `Get-Process` has no `CommandLine`
  (use `Get-CimInstance Win32_Process`).
- **Run the tool's real commands in the user's terminal, not the agent's shell.** On this PC the Claude desktop
  app is an MSIX package, and the agent's shell has its `%LOCALAPPDATA%` and `%APPDATA%` writes moved into
  `...\Packages\Claude_pzs8sxrjxfjjc\LocalCache` (measured 2026-11-05; `%TEMP%` and the home folder are not
  moved), where the user's terminal and the elevated recording window never see them: a PresentMon set up from
  the agent's shell was "not installed" for the user. `setup`, `watch`, `fix`, `undo` and `sweep` refuse there
  (exit 5, `paths.private_copy`) and `scripts/e2e.py` stops before its UAC prompt (exit 3). `scripts/etw_access.py`
  (E1) has no such check and raises its UAC prompt at once, so an agent never runs it either. Run them all in the
  user's Terminal panel. Tests are unaffected (their `LAGDOCTOR_HOME` is under `%TEMP%`). Nor does an agent open a
  store link (`steam://`, `com.epicgames.launcher://`), which starts a game on the owner's screen: Steam's own logs
  and `appcache\appinfo.vdf`, read-only, show how a launch behaves (`docs/research/desktop-app.md`, section 14).
  `check` and `analyze` only print a note there, yet each writes a new folder under `paths.sessions_dir()`, into that
  private copy, where the owner never sees it: to re-run today's analysis on one of the owner's recordings, load it
  and call `diagnose()` in memory (`Capture.load(path)`; the agent's shell reads the real sessions folder, as it did on
  2026-11-09). Nor does an agent change a game's own files (its settings, saves or install folder): Documents is not
  moved, so an edit to a game's config there (FINAL FANTASY VII REBIRTH keeps its own under `Documents\My Games`)
  changes the owner's real game, while one under `%LOCALAPPDATA%` lands in the private copy yet reads back as applied
  (`docs/research/settings-sweeps.md`, section 6). Reading them is fine. `lagdoctor sweep` does change a game's
  settings file, and records each run, so only the owner runs it, in their terminal; its tests keep the settings file
  under the test's own `tmp_path` (`tests/sweep_world.py`), and the one that drives `cmd_watch` uses stand-in
  collectors.
- **`scripts/stage2` creates throwaway Windows services** (the capture-service experiments, `docs/INSTALLER-PLAN.md`
  Stage 2). It runs only on GitHub's hosted runners, from `.github/workflows/experiments.yml` (started by hand, or by
  a pull request that changes it, `scripts/stage2` or a module the experiments import, which its `paths:` lists and
  `tests/test_side_workflows.py` keeps whole, and then on every push to that pull request, about 4 minutes a run);
  `scripts/stage2/guard.py` refuses anywhere else (exit 2). Never run it on this PC. Its instruments are
  checked by `tests/test_stage2.py` without any service; its results are read from the run's `stage2-results`
  artifact and the job summary (`docs/research/install-and-rights.md`, section 8, has the measured ones).
- **The MSI installs only on CI's runner.** `scripts/msi_check.py` installs and uninstalls a per-machine product
  (Program Files, the system PATH, Windows Installer's registry) and refuses anywhere but a GitHub-hosted runner
  (exit 2), before it reads the MSI or changes anything; never install the MSI on this PC. Building it needs the .NET SDK, which this
  laptop lacks, so it is built in CI; `tests/test_msi.py` checks the build, the WiX source and every install verdict
  on hand-written inputs. The owner accepted WiX v7's Open Source Maintenance Fee EULA for CI's builds
  (`docs/DECISIONS.md`, 2026-11-08): `--accept-eula wix7` appears once in `ci.yml` (the test holds it there), every
  `wix` command but help, version and eula needs it, and no other place accepts it without asking the owner. The
  `UpgradeCode` in `installer/lag-doctor.wxs` never changes (a new one installs beside the old version). The
  install's `PresentMon\` folder is where `setup.BUNDLED` looks (a test pins the two together), and `bin\` holds only
  `lagdoctor.cmd`, since the folder on PATH must not offer the runtime's `python.exe`. Python must never write into
  the install: the build ships unchecked-hash `.pyc` files the shipped runtime compiles, and the check's `untouched`
  and `gone` verdicts fail on anything an administrator's run wrote there (a build without them did exactly that).
  The Start-menu shortcut's AppUserModel ID in the `.wxs` equals `lagdoctor.app.APP_ID` (`tests/test_msi.py` holds
  them equal): change both together. Its component's key path stays an HKCU value: HKMU, HKLM, a file and the folder
  each fail an ICE that `wix msi validate` runs (WiX v7.0.1's darice.cub, measured 2026-11-08), and
  `SUPPRESSED_ICES` stays ICE61 alone. `msi_check` reads back only `#<digits>` and plain-string Registry rows under
  HKCU or HKLM; a new row of another form fails its `registry` verdict until the check learns it.
- **`watch --control stdio` is lag-doctor's window's** (`lagdoctor/control.py`, DESIGN.md "The control channel"). Its
  stdout is the channel, so nothing but `Channel` may write there: `serve` points stdout at stderr before anything
  else runs, and a new message or field goes through `control.from_app` or `from_recorder` with its checks. An agent
  starts one only as `tests/test_control.py` does (a pid that does not exist, a home under `%TEMP%`), never against a
  running game: the app starts it with `--elevate` and raises the UAC prompt for it (`lagdoctor/app/recording.py`).
- **An agent never starts the app.** `lagdoctor app` (and the MSI's Start-menu shortcut) is the owner's: the
  agent's shell has another app's private copy of AppData, where `app` refuses (exit 5, `cli.PRIVATE_COPY_POLICY`),
  and from M4 on the app raises a UAC prompt. What an agent may run is the app's tests: `tests/test_app_page.py`
  (through `tests/app_harness.py`) opens the real window for about 10 s without taking the focus, on a demo library
  (`tests/app_demo.py`) under the test's own `LAGDOCTOR_HOME` in `%TEMP%`, crashes its page and ends its WebView2
  browser process on purpose (both recover), and closes it; run it when the owner is not mid-game. The library the
  repo shows is only ever the demo one: never put the owner's games or sessions in a test, a fixture or a picture.
  The app's recording runs in its tests only through stand-ins: `tests/fake_recorder.py` in the recorder's place
  (the real control channel, recording nothing) and `fake_recorder.stand_in_prompt`, a harmless process of the
  test's own, in the relay's. An `App` a test builds passes its own `recorder` and `launcher`: the defaults start
  `watch --control stdio --elevate` and raise the real UAC prompt (`tests/test_app_recording.py` also makes
  `launch_elevated` refuse, so a slip fails instead of prompting).
- **The app's page puts outside text on screen as text only.** Game, process and window names, evidence,
  reasons and the player's note (`watch --note`, cleaned by `model.clean_note` in every writer and reader) reach the
  page as JSON and go into the DOM through `el()` in `lagdoctor/app/assets/app.js`, which makes
  text nodes; nothing there parses markup (`tests/test_app_page.py` refuses `innerHTML`, `outerHTML`,
  `insertAdjacentHTML`, `document.write`, `DOMParser` and `eval` in it, and drives a game named like markup through
  the real page). The page names a session or a game only by the id the app gave it; `bridge.parse` checks every
  field, and `App.entry` resolves an id against its own listing, never by joining it to a path. The vendored
  `assets/vendor/` (uPlot 1.6.32 and its licence) is pinned by SHA-256 in that test: replace all three files and
  the pins together, never edit one.
- **The desktop app is built milestone by milestone** (`docs/LAUNCHER-PLAN.md`; the owner said go on 2026-11-08).
  M0's spike (`scripts/webview_spike.py`) gated the window, and the owner's own run passed on 2026-11-08 (the plan
  records it). M1, the app with your results, is `lagdoctor/app/` over `lagdoctor/winapi/webview2.py`, whose slots
  and IIDs `tests/test_webview2_idl.py` checks against the SDK's own `WebView2.idl` (`fetch-webview2-idl`; a slot or
  IID the module adds must be in that IDL, or the test fails). The spike stays as the record of what WebView2 does
  from ctypes, and its tests and CI probe keep running. An agent may run `--auto` or `--rehearse`, which do
  this on the owner's screen for about 45 s: a window opens without taking the focus; it opens a DevTools window on
  purpose and closes it; it goes blank for about 6 s and reloads, because the run ends its own WebView2 browser
  process; its move check holds it in Windows' move loop for about 2.5 s, which moves the mouse pointer onto its title
  bar and captures the mouse (the spike puts the pointer back); then three short windows follow, one of them a child
  that dies of an access violation on purpose. Run it when the owner is not mid-game. Nothing is typed there: only
  `--rehearse --ci` on GitHub's own runner types step 2's keys with SendInput (`types_keys`, through
  `scripts/stage2/guard.py`, and checked again inside `type_keys`), because typed keys go to whatever window has the
  keyboard; the spike's tests make every real input call raise. Its interactive run is the owner's, in their terminal. The spike keeps everything under `%TEMP%\lag-doctor-webview-spike`, which the private
  copy leaves alone. The relay is also the protocol of Stage 4's capture service (`docs/INSTALLER-PLAN.md`), so Stage
  4 work starts from that plan too.
- **The relay** (`lagdoctor/relay/`, DESIGN.md "The relay") runs PresentMon and the Kernel-Network session with
  administrator rights for a recording in the user's own window. Everything it does comes from its command line
  (`client.relay_argv`, parsed by `server.parse_args` on both sides of the prompt; the player's note is never on it,
  which `test_the_note_stays_with_the_recorder_and_never_goes_to_the_relay` holds); from the pipe it takes only `start`
  and `stop`, and it passes back only what `protocol.py` lists: the game's frame rows and events, the recorder's own
  events, and per-program byte totals for the recorder's Windows session (DECISIONS 2026-11-08). A new message or field
  goes through `protocol.from_relay` or `from_recorder` with its checks. `tests/test_relay_live.py` starts the real
  relay elevated and runs only on GitHub's runner (`scripts/stage2/guard.py`); the other relay tests run its code on a
  thread here, unelevated. An agent never starts `python -m lagdoctor.relay` itself: only `watch` does.
- **The relay and the capture service talk only through `lagdoctor/winapi/pipe.py`.** Name a pipe through
  `pipe.path` (Windows folds `..` in a `\\.\` path, so a name holding one opened an ordinary file), build its
  descriptor with `pipe.client_dacl` (a SID string can carry entries of its own), and check who answered: a server
  compares `client_pid()` and `client_session()` with what it expects, a client compares `server_pid()` with the
  process it started or the service's and checks `owner_sid()`, since any process may create a pipe of that name
  first. Code that issues overlapped I/O through ctypes keeps the OVERLAPPED and buffer on an object that outlives the
  call, and cancels and waits out the call on every exception path: Ctrl+C lands between any two bytecodes, and Windows
  also cancels a thread's I/O when the thread exits.
- **The repo is public** (https://github.com/Ali0600/lag-doctor). Never commit the owner's personal details: write
  user-folder paths as `%LOCALAPPDATA%`, `%TEMP%` or `<repo>`, and leave out this PC's name and SID, its adapter and
  power-plan GUIDs, its public IP and MAC addresses (tests use documentation ranges, `192.168.0.x` for a home network,
  and made-up MACs and GUIDs), what else runs on it, and when its owner played or downloaded what. That includes the
  store app ids of the owner's games: write `<appid>`, since the pre-push hook refuses them even where the game's name
  is already in the docs (it refused a Steam app id on 2026-11-09). Push only `main` and
  branches cut from it: the history from before the 2026-11-07 rewrite stays in `refs/backup/`, `refs/original/` and
  the merged `worktree-*` branches, and `.git/hooks/pre-push` (local, not in the repo) refuses any commit that holds
  those details. Pull request bodies, review comments and issue text are public too, and no hook reads them: a
  command for the owner names the folder as `<repo>\.claude\worktrees\<name>` there, never the full user path (a PR
  body that held it was refused before posting, 2026-11-08). Name another project's issue in plain words ("PresentMon's
  issue 570"): `owner/repo#N` or its URL puts a backlink on that issue's public timeline that editing the body does not
  remove (PR #27, 2026-11-09). Run a PR body's scan in the same `&&` chain as `gh pr create`: on a line of its own, a
  failing scan still let that body go out.
- **The README's Experience Gained carries totals** (tests, red-first sabotages and their specs, the tests on the real
  PresentMon). Count them from the source, never the old figure plus your delta: `uv run pytest --collect-only -q`
  for tests, the mutations in `tests/redfirst/*.json` for sabotages and specs. When PRs merge in parallel, the last
  to land recounts on main: a total its PR did not touch goes stale without a merge conflict (after #4 and #5, and
  again after #7). The PresentMon figure (43 since `--track_pc_latency` was dropped) is the tests that run on CI with a
  download, not CI's "tests that use a download" count (44, one of which skips there): read a counter at the commit
  that set a figure before
  correcting the figure against it.
- **CI's runner lacks some of this laptop**: no GPU, NVIDIA driver, thermal sensors or ping replies. (Its High
  performance plan has no power-mode slider; `slider_mode()` reads the plan's personality, so the power tests hold
  there undeclared.) The workflow declares that in `LAGDOCTOR_ABSENT`, and a test that needs one of those carries
  `@pytest.mark.needs(name)` (names in `tests/capabilities.py`), so it skips there with the reason; undeclared, a
  missing capability fails. Never make a test skip on a detected absence that a regression could also cause (the NVIDIA
  fixtures used to), and mark a test only when a CI run shows it failing for that lack: a test that passes on the
  runner keeps running there, even on another branch (elevated, the ETW tests check the real-time session). A PC
  without an NVIDIA GPU sets `LAGDOCTOR_ABSENT=nvidia`. The runner has the WebView2 Runtime and a desktop (M0's
  probe), so the app's window tests carry `needs("webview2")` and CI declares nothing for them. The runner's account
  is an administrator with UAC off, so `tests/test_cli.py` models an unelevated owner unless a test says otherwise,
  and the app's own `run()`, which refuses an elevated start, is never what a test opens: `tests/app_harness.py`
  builds `App` itself.
- `python3` in Git Bash here is the Microsoft Store placeholder: it waits instead of running. Use `uv run python`.
- A new worktree has no `.venv`; the first `uv sync` or `uv run` builds one on Python 3.13, named by `.python-version`,
  which resolves to `C:\Python313` here (measured 2026-11-08, uv 0.10.4). Before that file, uv took its own managed
  3.12, which CI never ran. A venv still on 3.12 is rebuilt by the next `uv run`: not while a red-first run uses it.
- The main checkout (`<repo>`) is also a session's working folder: on 2026-11-08 the original session worked there
  while worktree sessions built M0 and its step 2. Work in a worktree. After landing a change, bring the main checkout
  to the new main only with `git merge --ff-only origin/main`, only when it is on `main` with `git status --porcelain`
  empty, and never switch its branch; the owner runs the tool there.
- `uv sync` installs lag-doctor itself, editable: `.venv\Lib\site-packages\lag_doctor.pth` holds the checkout's root,
  so every process in the venv can import `lagdoctor`, `laglab`, `tests` and `scripts`, after its own folder,
  `PYTHONPATH` and site-packages. Red-first runs still test their copy: pytest's `pythonpath` and `cwd` put it first,
  and the probe takes the real tree off the test process's sys.path (Python lists a sys.path folder again after a
  file in it is saved, which read as UNISOLATED). A child that a test starts with neither its `cwd` nor `PYTHONPATH`
  on the copy still imports the real checkout, and its mutation shows as SURVIVED. A folder named
  `lagdoctor` without `__init__.py` (a fixture, a scratch project) loses to the real package even when it comes first
  on sys.path, so give it one (as `tests/fixtures/redfirst/project/lagdoctor/__init__.py` does). The wheel holds
  `lagdoctor/` and nothing else (uv_build takes the whole folder): a file the product reads from anywhere else would
  not ship, and the files check, which compares `lagdoctor/` only, would not name it. `uv run` syncs the venv first:
  after an edit to `pyproject.toml` (a comment was enough, 2026-11-08) it rebuilt and reinstalled lag-doctor there
  while a red-first run was using that venv. While a run or the suite uses the venv, start other commands with
  `uv run --no-sync`.
- Git Bash rewrites an argument that looks like a POSIX path before starting a native program: `git sparse-checkout
  set --no-cone /Tests/Gold/` stored `C:/Program Files/Git/Tests/Gold/` (the checkout then wrote nothing), and `git
  show origin/main:.github/workflows/ci.yml` became `origin\main;.github\...`. Prefix such commands with
  `MSYS_NO_PATHCONV=1`, or run them from PowerShell or Python's `subprocess`, which pass arguments as typed.
  A native program cannot open bash's process substitution either: `gh pr merge --body-file <(git log -1 ...)` failed
  with "The system cannot find the path specified" (and a `| tail` after it hid the exit code); write the text to a
  file first.
- The agent's Edit and Write tools decode a `\u` escape typed into new text into the character itself: `\u00a0`
  landed as a literal no-break space (2026-11-07), which is what `console._PUNCT` writes as escapes to avoid. Build
  such text in Python from counted pieces (`chr(92) + "u00a0"`) and read the line back with `ascii()`. The Bash tool
  goes the other way: a doubled backslash typed into a command, heredocs with a quoted delimiter included, arrives
  halved, so `\\localhost` landed as `\localhost` in a PR body and a doc, and `"..\\x"` broke a script (four times on
  2026-11-08). Write text that holds Windows paths with the Write tool, or build it from `chr(92)`, and read it back.
- To read one CI job's log while the rest of its run goes on (a fast job's verdict, such as the wheel job on a
  sabotage commit), use `gh api repos/Ali0600/lag-doctor/actions/jobs/<job id>/logs --allow-escape-sequences`:
  `gh run view <run> --log --job <job id>` refuses until the whole run has finished, and without the flag `gh api`
  prints only a refusal of the log's colour codes (seen 2026-11-07). That job itself must have finished: while it
  runs, the call fails with HTTP 404 and an XML `BlobNotFound` body (2026-11-08).
- **A pull request with a merge conflict gets no CI**: GitHub starts no `pull_request` workflow while the PR cannot
  merge. On 2026-11-08 other PRs landed during a review, and two pushes to it started nothing (`gh pr checks`: no
  checks reported). When a push shows no runs, read `gh pr view <n> --json mergeable`, merge `origin/main` into the
  branch and push again.
- On some runners `Get-PhysicalDisk` reports every disk's media type as `Unspecified`, and one
  `tests/test_machine_winapi.py` test skips saying so: 27 skips instead of 26 in a job's summary (2026-11-08). Read the
  skip list before taking a changed count for a changed suite.
- To measure a flaky test on GitHub's runners, push a throwaway branch `diag/<topic>-before` (or `-after`), cut from
  main and never merged, whose own workflow runs on `push: branches: ["diag/**"]` (`ci.yml` and the experiments
  workflow do not run for it). Run the test many times, each as its own pytest process, and read each outcome from
  `--junitxml`. Where timing is the suspect, add a fixed delay per run at the suspect point and sweep it: a late session
  start made the tracker test below fail 16 of 120 runs before its fix and 0 of 120 after (PR #14). Where the suspect
  is OS state that other processes share and move (Windows' port-0 counter), set it inside the test's own process,
  right before the test body: a `-p` plugin on `PYTHONPATH` with `pytest_runtest_call` at `tryfirst=True`. Set from
  the parent before a child pytest, a counter jump during the child's start undid it (PR #15's `scripts/diag_ports.py`
  on `diag/dns-ports-before`). Count a flake in CI's history over every attempt: `gh run list` shows each run's last
  attempt only, so a failure a rerun turned green is invisible there (both port-flake sightings were). Take
  `run_attempt` from `gh api repos/Ali0600/lag-doctor/actions/workflows/ci.yml/runs --paginate` and each attempt's
  jobs from `.../actions/runs/<run id>/attempts/<n>/jobs`. The four `diag/*` branches of PRs #14 and #15, and PR
  #22's `diag/spike-udp-before` (a spike that watched every WebView2 socket for a whole run, dispatched with `gh
  workflow run webview-spike.yml --ref` and needing no workflow of its own), stay on GitHub while those PRs cite their
  runs.
- `tests/test_etw_tracker.py::test_the_tracker_over_a_real_private_etw_session_finds_the_server_and_a_delay_gap` failed
  at random until 2026-11-08 (`AssertionError: []`; in CI's step that ran it alone, 4 of 57 runs on Python 3.11 and
  none of 56 on 3.13). Its private ETW session hands every event over inside `stop()`, so with the tracker's 2 s
  reorder window the first second of the stream came late, and whether the server was known before the planted gap
  hung on when the session started against whole seconds. The test now gives the tracker a window longer than the run
  and asserts that no packet came late, and CI runs it in the suite again; a test that drives the tracker over a
  private session needs the same window. A test that starts a child pytest gives it its own `TEMP` (as
  `tests/test_redfirst.py` and `tests/test_downloads.py` do): child runs in the shared pytest temp folder made this
  test about 2 s slower on CI and failed it 3 times in 16 runs (2026-11-07, before the window).
  `tests/test_nvidia_live.py::test_sampler_
  reads_each_row_just_after_a_driver_refresh` failed on 2026-11-07, alone too, with the NVIDIA GPU idle in P8 (no
  training job) and the screen locked; it had passed with the GPU busy. `tests/test_nvidia.py::test_rows_stay_exact_
  at_a_faster_rate` failed once in a full local run (2026-11-09: the last of five readings None), then passed 3 of 3
  alone, on M4b's branch and on main alike. Two more depend on the PC's state: `tests/test_cli.py::test_a_real_console_event_
  while_recording_still_saves_the_capture` (its child hit the 60 s timeout once under full-suite load, then 3/3
  alone), and `tests/test_sysmon_target.py::test_auto_picks_a_windowed_renderer_and_never_a_windowless_gpu_job`, which
  needs LagLab to render where Windows counts it: on 2026-11-07 at 00:30 (Silent mode, display asleep) it failed on
  unchanged code too, in a worktree of the previous commit, and again on 2026-11-08 in a worktree of main. It also
  fails while another LagLab window is open: on 2026-11-08 another worktree's measurement script was running `python
  -m laglab` in a window of its own, the test's auto pick took that window, and it failed alone and on unchanged main
  alike, with `test_live_sampler_on_a_spinning_child_and_its_spinning_grandchild` failing beside it under that
  script's CPU load. Before rerunning, list the `python.exe` command lines that hold `laglab` (`Get-CimInstance
  Win32_Process`): one the test did not start means another session is measuring, so wait for it to end. Rerun them
  alone, with the display awake, and run that control before blaming a change. The self-test cases of
  `tests/test_etw_flows.py` and `tests/test_etw_tracker.py` failed on CI with "no free port for both UDP and TCP"
  until 2026-11-08 (4 at once on 2026-11-07, 2 on 2026-11-08):
  their loopback DNS stand-ins took TCP's next port-0 number and tried UDP on it, 10 times. Windows hands port-0
  numbers out in sequence, and on 9 of 10 runners Docker's NAT holds 200 UDP ports in a row that TCP still hands out;
  with the hint there (1.2 % of its positions), all 10 tries failed. The one stand-in left, `tests/loopback_dns.py`,
  draws random dynamic ports and names every refusal: never take one protocol's port from another's port-0 choice.
  `tests/test_machine_inventory.py::test_live_collect_agrees_with_independent_readouts` failed once on a runner
  (2026-11-07, a docs-only change) with "the Win32_DeviceGuard query took over 10 s and was stopped": WMI was slow
  on that VM, not the code; rerun before blaming a change, and look at the inventory's own timeout if it repeats.
  `tests/test_model.py::test_the_save_takes_a_fraction_of_the_old_saves_time` failed once on main's Python 3.11 job
  (2026-11-08, on a tree that had passed on its PR: new save 0.35 s against the old 0.84 s, needs under 0.3x) and
  passed on rerun; it timed the old save once, before the new one's best of three. Since PR #11 the two run in turns,
  each at its best of three (about 0.06x on this laptop); if it fails again, rerun it alone before blaming a change.
  `tests/test_sysmon_sampler.py::test_live_sampler_on_a_spinning_child_and_its_spinning_grandchild` failed once in a
  full local run (2026-11-08: the grandchild's first reading was 0.0, so its mean was 72 % against the 88 % expected,
  outside the 10-point bound) and passed alone; rerun it alone before blaming a change.
  Never run two pytest processes here at once (a review agent's tests beside the suite): they share
  `%TEMP%\pytest-of-<user>` and its keep-three cleanup, and on 2026-11-08
  `tests/test_downloads.py::test_fetch_gold_takes_the_pinned_commits_gold_folder_byte_for_byte` failed twice that way
  (WinError 5 renaming a folder under its `tmp_path`), then passed 3 of 3 alone. Another worktree's session counts
  too: before a run, list the `python.exe` command lines (`Get-CimInstance Win32_Process`) and wait while one holds
  `pytest`, `redfirst.py` or `laglab` (2026-11-09: a rerun met the M2 session's red-first). The Window spike workflow's
  socket check (`sockets-1` or `sockets-2`) failed 3 of 20 attempts on 2026-11-08 (main 1 of 13, the M1 branch 2 of
  5), each on one UDP4 socket "bound" on an ephemeral port in a WebView2 process: the runtime's own lookups (in 20
  runs watched every 20 ms, all 70 of its UDP sockets were a utility process's, on 0.0.0.0, on a port Windows chose).
  Since PR #22 the check counts that one shape as WebView2's client and fails on every other (`server_reason` in the
  spike; `docs/research/desktop-app.md` section 11). If it fails again, read the failing reading's `servers` in
  results.json (each names its reason and process kind) before rerunning.
