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
uv run python scripts/overhead/bench.py cycles --suite <name> --out <dir>   # lag-doctor's own cost on LagLab, ~6.5 min a suite
uv run python scripts/overhead/phase_lock.py <capture>.ldcap.json.gz      # do frames at its sampling moments run long?
```

CI (`.github/workflows/ci.yml`) runs the suite on `windows-2025` for Python 3.13 on every pull request and on every
push to `main` except one that changes only `docs/**`, `CLAUDE.md` or `.github/SECURITY.md` (its `paths-ignore`; no job
reads them, and `tests/test_ci_triggers.py` keeps both the list and that claim true). Such a push starts no run, and
`gh run list --commit <sha>` then prints nothing and exits 0, so never wait for one: GitHub filters a push on its whole
range, so prove it with `git diff --name-only <base> <sha>`, where `<base>` is the head of the last push to main that
has a ci.yml run (`gh run list --workflow ci.yml --branch main --event push --limit 1 --json headSha`). Main is green
when that run concluded success and every later commit is such a push. `--commit` takes the full SHA: a short one matched nothing and
exited 0 for 10 minutes while the run existed and passed (2026-11-19), which reads like that docs-only skip. `README.md` is not exempt: the build reads it
into the wheel's metadata. 3.13 is the Python the installer ships (`scripts/runtime_pin.py`); `requires-python`,
`.python-version` and the matrix follow it (`tests/test_python_versions.py`), and when the pin moves to a new minor
version, that version joins the matrix in the same change (`docs/DECISIONS.md`, 2026-11-08). Code and workflow
changes go through a pull request and merge on green: the repo has auto-merge off and `main` requires no checks, so
wait for the PR's checks yourself and then squash it (`gh pr merge --squash`). Before the tests it gets
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
  does): then they fail, because there a missing download means a broken download step. No test registers a real
  key or makes a sound: `watch` measures from a start key by default (Ctrl+Alt+S, which the owner's own watch or
  sweep may hold), so `tests/conftest.py`'s `_no_real_keys_or_sounds` fails a test that registers any key but
  Ctrl+Alt+Shift+F23 or F24 or plays a sound, and gives a console `watch` a stand-in player (its `played` list). A
  stand-in session takes a stand-in `hotkey_factory`; a test that runs `watch` in a child process, out of the
  guard's reach, passes `--hotkey none --start-key none` (and `--no-sound` for a console `watch`): the lag key
  Ctrl+Alt+L is a real key too.
- **Red first.** A new guard ships with a mutation in `tests/redfirst/*.json` that turns its test red;
  `scripts/redfirst.py` must report every mutation `CAUGHT`. Never weaken a test to make it pass. A pytest run that
  outlives `TIMEOUT_S` (10 minutes; `REDFIRST_TIMEOUT_S` sets it in seconds) is ended with every process under it (a
  job object) and reads TIMEOUT, which is not caught: a hang names no failing test, so give the test's wait a bound
  past which it fails. A baseline that times out is BAD-BASE. The runner
  mutates a temp copy of the tree, never the working tree; so tests must not read the real repo by an
  absolute path (reported UNISOLATED) or write into their own tree (stops the run). It copies the tree when each run
  starts, so keep the checkout still until a queued run has begun. Editing a line that an existing mutation's `old`
  quotes, or adding a line that repeats one, makes that mutation BAD-SPEC, and no test notices (M6b's spawn scan
  copied the handle scan's parse-failure line, 2026-11-10): after any edit to a file a spec names, check every `old`
  in `tests/redfirst/*.json` still occurs exactly once in its file, and re-point the ones that moved (same sabotage,
  new text). Renaming a test does the same to each mutation that names it: only a run of that spec notices, as
  BAD-BASE, so grep the specs for a test's old name when you rename it. Before merging, rerun every mutation whose
  `file` or tests the change touches, picked by path from all of `tests/redfirst/`, never from a list kept by hand
  (one missed `crop.json`, and a renamed test sat BAD-BASE until the gate ran, PR #40, 2026-11-10). Commit before
  picking: a pick by `git diff --name-only origin/main HEAD` cannot see an uncommitted edit (PR #53's first pick left
  out `frames.py`'s 112). Merge `origin/main` into the branch first, too: worktrees share their refs, so another
  session's fetch moves `origin/main` under yours, and the two-dot diff then counts main's new files as the branch's
  (PR #66's first pick took 1,644 mutations, 511 once main was merged). A picked set runs about 3.7 s a mutation here (PR #53's 1,098 took 67 minutes), and one heavy in the window's own
  tests about 8 s (each run of `tests/test_app_page.py` opens the real window: PR #62's 308 took about 40 minutes,
  PR #64's 466, 71 of them on the window, about 68, and PR #66's 511, 83 on the window, 69). Every mutation on
  `lagdoctor/analysis/rules.py` (456 after PR #52) takes up to an hour here (PR #50's 494 picked mutations took about
  70 minutes, PR #51's 744 about 36 and PR #52's 643 about 38 on a quiet PC): start it in the background as soon as the code is settled, and
  review meanwhile with agents that run no tests (no second pytest meanwhile, so write the next change's tests without
  running them; the full suite may run beside such agents, about 8.5 minutes). While another session's run holds
  pytest, a test that takes no fixtures can still be called from plain Python, which shares no pytest temp folder,
  and a sabotage can be pre-checked that way on a temp copy of `lagdoctor/` and `tests/` (PR #67 pre-checked its 25
  so during another session's 511-mutation run; the gate is still `redfirst.py`). A guard a later edit makes redundant
  (PR #51: `return out or None` after an `if not proc_ded: return None`) leaves its mutation equivalent, so it can
  only survive: drop the redundant check and its mutation together. An `old` that
  still occurs once can still go dead: a change that sends its test down a new path leaves the sabotaged line
  unreached, and only the gate's SURVIVED shows it (PR #45's tick test took a stall past the residual's GPU wording an
  older sabotage changed, 2026-11-11), so read each survivor before assuming the new tests are at fault. A `new` that names a variable an edit moved into another function raises NameError there, and the HTML report's chart wrapper turns that into a page that passes (PR #55 lifted `scale` into `html.frame_scale`, and `max(scale)` survived): re-point such a `new` with the same sabotage in the names its line can see. A shared fixture
  such as `tests/synth.py` touches every test built on it, so pick the mutations those tests guard too (PR #43 refit
  synth's hog mode and reran the 7 whose tests build it). A change that leaves every call the old tests make giving
  the same output needs no rerun once that is shown: wrap the fixture's function for one run of those tests, compare
  each call's output with the old version's (`git show <base>:tests/synth.py`), and first see a planted change read
  as different (PR #44's `covered` knob: 319 calls, all identical, 2026-11-11). To rerun a picked set, copy its
  entries byte for byte into a spec under the short `TEMP` folder and pass that file to `redfirst.py`. Most specs (`report.json`, `user_text.json` and `session.json` among them) are laid out by hand: change their
  values in place, never by rewriting the file through `json.dumps`. A script may rewrite a spec only after checking
  it is byte for byte `json.dumps(indent=1)` plus a newline (`first_capture.json` and `power_plan.json` are), and with
  `write_bytes`: Python's `write_text` writes CRLF on Windows. The copy holds `lagdoctor/`, `laglab/`, `tests/`,
  `scripts/`, `docs/`, `.github/`, `installer/`, `pyproject.toml`, `uv.lock`, `LICENSE` and `.python-version`
  (`COPY_DIRS`, `COPY_FILES`): a test that reads any other file fails before its sabotage (BAD-BASE) until that file
  is added there. Keep a run's `TEMP` short (a few letters under `%TEMP%`), never a deep folder such as an agent's
  scratchpad: one test's file sits 144 characters below `TEMP` (`tests/test_report.py`'s PowerShell test), long paths
  are off on this laptop, and past 259 characters Python's own `write_bytes` raises FileNotFoundError before any
  sabotage, so red-first reported three mutations BAD-BASE (2026-11-09).
- **Not measured is not fine.** A collector that cannot run reports `unavailable`/`failed` with the reason;
  a failed sample is `None`, never `0`; rules that need a missing source are listed as unchecked.
- **The game folder check reads folders, never the game** (`capture/gamefiles.py`, what Compare's drop-in and Unreal
  rows show). Find the user's own folders through `winapi/folders.py`: OneDrive can hold Documents, so
  `%USERPROFILE%\Documents` may be the wrong place. Never open a file whose listing marks it online only (reading it
  makes OneDrive download it). A file size is `labels.byte_count`, never `charts.number`, which refuses anything past
  1e7 as corrupt: an 81 MB exe read as "not read" until a demo used a real size. A DLL name joins `PROXY_DLLS` only
  from a primary source, and never one on KnownDLLs (`tests/test_gamefiles.py` reads both lists).
- **The store readers (`lagdoctor/stores/`) reach files only through `stores`' own file access** (`scandir`, `stat`,
  `is_folder`, `exists`, `read_text`, `read_bytes`, `open_file`), which asks `target.local_problem` first: it reads each junction and link on the
  way instead of following it, so nothing on a network share, or kept online only, is opened. `tests/test_stores.py`
  scans every module of `lagdoctor/stores` but `__init__.py` for any other way in (aliases, `ntpath`, the raw
  seams), and proves it on planted lines. Their tests never read this PC's own Steam or Epic files: the
  Steam tests' `registry` fixture (a fake behind `steam._open_key` and `_query_value`) is autouse, and the Epic tests'
  `no_real_folders` fails any default place a test did not move. A link onto a share needs admin rights to make, so a
  test makes a real junction and fakes what `target._readlink` gives for it. A Steam tool id joins `steam.NOT_GAMES`
  only from a source that names it (`docs/research/desktop-app.md`, section 15), never from recall.
- **Text a player reads is plain words.** Research citations (`WT 8`, `LC 4.9`, `NE 7 row 9`) and review ids
  (`CC-5`) go in comments, never in a string the report, console or a fix shows; `tests/test_user_text.py`
  scans every module but `winapi/`. Name sources with `labels.source_text`, never by their ids.
- **The HTML report is shared, so it never shows the Windows user name.** Put text on the page through `html.e()` or
  `html.prose()`, never `escape()` alone: `e()` also shows a folder under a user's profile as `%LOCALAPPDATA%\...`
  or `%USERPROFILE%\...` (`html.user_neutral`), and a command the page offers names it as `"$env:LOCALAPPDATA\..."`
  (`html.powershell_path`). `charts.py` escapes its own labels without that, so pass a chart only fixed words and
  numbers. Tests use a made-up profile (`C:\Users\Jo Tester` in `tests/test_report.py`). The console summary keeps
  real paths on purpose (DESIGN.md, Report). The README's picture of it (`docs/images/report.png`, 2026-11-15) is
  `tests.synth.laglab("hitch-io")` with a made-up `BackupSync.exe` reading and writing about 31 MB/s added to every
  `others` row (keeping the 8 busiest by CPU), through `diagnose` and `html.write`, shot by headless Edge
  (`--headless=new --hide-scrollbars --user-data-dir=<a folder under %TEMP%> --blink-settings=preferredColorScheme=1
  --window-size=1100,2400 --screenshot=<png> file:///<html>`; without the colour-scheme flag it follows Windows' dark
  mode) and cropped to 1100x1690, just below the third finding. Never shoot one of the owner's own recordings.
- **Column names come from `lagdoctor/model.py`** (`F`, `S`, `G`, `N`); `SeriesBuilder` rejects unknown names. The
  dict `frames.main_chain` returns also holds two per-row lists that are no capture column, `frames.HIDDEN` and
  `FRONT` (time behind another window, DESIGN.md "Frames and stats"), and a tuple, `EDGES` (such time before its
  first row or after its last): copy or cut it by its own items, never by `F.ALL`, which drops them and judges a
  covered window's frames again, and keep `EDGES` a tuple, which a row-by-row copy (`sweep/results.clipped_frames`)
  leaves out instead of cutting, and cut each `FRONT` (a frozen screen's from and to) to the copy's own window: a row
  the copy keeps can hold one that began before it or ends after it. Read `FRONT` on every row of a `HIDDEN` run, never
  only its last: `_hidden` puts each on the shown frame ending its own run, and a shown burst joining two stretches
  leaves that frame inside the run (until PR #68, `frames._row_spans` read only the last, and `hidden_s` counted such a
  freeze as time behind too). A measured part takes HIDDEN, FRONT and EDGES
  from `Capture.covers` (`crop.crop`, the whole recording's judgement), never from its own rows alone. A frame test builds its capture and goes
  through `main_chain`, since a dict built by hand has no `HIDDEN` and hides nothing.
- **A rule test asserts the kind it expects.** `rules.run_rule` turns a rule that raises into Unchecked ("the check
  failed"), so `assert not is_finding(res) or ...` passes on a crash: a sabotage that crashed `bufferbloat` survived
  that way (PR #48). Assert `is_finding(res)` (or the Cleared/Unchecked you mean) first. A hand-built `others` table
  lists some process on every tick: attribution reads a row listing nothing as not read (`_Others.total` and `io_top`
  None), so a table holding only the program under test reads "not measured" on its quiet ticks. Its entries' length
  picks the path under test: 9 fields (since PR #50) say each process's session and whether its program sits in
  System32 (`rules._where`), so a Windows name is judged by where it runs and the "8 busiest on the CPU" caveat goes;
  12 fields (since 2026-11-16) add each process's RAM, memory committed and video memory, which alone let
  `ram_pressure` and `vram_overflow` name programs (fewer keep their old line word for word); 5 to 7 fields are an
  older capture, judged by name. `explorer.exe` is judged by where it runs only when `target["windows_folder"]` is
  True (since 2026-11-16 the folder check covers the Windows folder itself, where Windows runs it); without it, by
  name. A memory figure is read at the system rows' own ticks (`rules._when`), so a hand-built others table must
  carry the system table's times (`synth.system_table`'s 0.25 s steps). A test of the half second that reaches the
  nearest memory reading (`MEMORY_NEAR_S`) needs a window holding no reading of its own: `tests/test_analysis_rules.py`'s `stutter` lasts 0.15 s and attribution looks from 0.5 s before
  to 0.25 s after, so with memory read on whole seconds a stutter at x.6 s ends its window exactly on one (PR #52's first
  version passed without the reach, and only the gate's SURVIVED showed it); one at x.55 s holds none. The sampler reads memory
  once a second (`memory_every_s`): a `SystemSampler` test at `period_s=0.05` sees it on every 20th row unless it
  passes `memory_every_s` too. A `SystemSampler` test with a fake snapshotter calls the real
  `ntquery.image_path` on its made-up PIDs unless it passes `system_folders=sysmon.SystemFolders(...)`, whose fake
  paths must end in the snapshot's own image name (a different one reads as a reused PID: None).
- **A measured part keeps the recording's own times** (`lagdoctor/analysis/crop.py`, DESIGN.md "The measured part").
  On a cropped capture `duration_s` is the part's length, never an end time: code that counts from the start counts
  from `_Env.start`, and text that names a share or a time of the recording names `_Env.part` ("the capture" until
  it is cut; `tests/test_crop.py` scans every string rules.py and diagnose.py show for text naming the whole capture,
  recording or session). A new `net_flows` key needs its decision in `crop.NET_FLOWS` (a test fails until it has
  one); a total over the whole recording becomes None in a part. A cropped capture is never saved (`Capture.save`
  refuses it). watch's start key keeps its press in the capture's `start_key`, and every path that analyses a
  recording finds its part through `crop.key_part` (the end of a watch, `analyze` without `--from/--to`, the reports'
  "why the whole recording was analysed" note); never derive it anywhere else.
- **One clock**: `lagdoctor.clock.now()` (QPC seconds). PresentMon runs with `--qpc_time`; its rows are
  timed by `TimeInQPC`, never by when they arrive (they arrive 0.1–2 s late, in bursts).
- **PresentMon**: parse its CSV by header name; on a pipe it is Latin-1 text, not UTF-16. Stop it with
  CTRL_BREAK (shared console) or `--terminate_existing_session --session_name lagdoctor`; never
  `Popen.terminate()` first, which leaks its ETW session until reboot. Exit code 6 is ambiguous: read stderr. Leave
  `--track_pc_latency` out: it switches on the latency-ping code inside games built with NVIDIA's Reflex markers
  (DECISIONS 2026-11-09; `test_pc_latency_tracking_stays_off` guards it).
- **The fan reader only reads** (`lagdoctor/winapi/atkacpi.py`, `docs/research/asus-fans.md`). ASUS's device takes
  reads (DSTS) and sets (DEVS) through one control code, and DEVS on the very fan ids it reads can pin a fan at full
  speed, so the handle's rights cannot keep it read-only: `_call` refuses any buffer that is not a DSTS read of a fan
  id before DeviceIoControl, and `tests/test_fans.py` fails if any other module names the device or its control code,
  or anything names the set method. Never add a set, a curve, a mode or another id there; a fan setting stays the
  owner's, in ASUS's own tool. Every recording runs the fans collector, so `tests/conftest.py`'s
  `_no_real_asus_device` fails a test that reaches the real device, except those marked `needs("asus_acpi")`
  (`tests/test_fans_live.py`, which reads this laptop's fans). An agent may run those: they only read.
- **Fixes go through `lagdoctor/fixes/base.py`** (journal first, readback, undo). Never offered: disabling
  security features, killing or suspending processes, HKLM or policy tweaks, anything on the game process.
- **ETW needs rights.** PresentMon (session `lagdoctor`) and the network tracker (session `lag-doctor-net`)
  need ETW rights: PresentMon an elevated token or Performance Log Users, the tracker an elevated token only
  (measured, E1 in `docs/research/install-and-rights.md`: Kernel-Network's own descriptor lets only SYSTEM,
  LocalService and Administrators enable it). Tests use private
  in-process sessions with unique names instead. The end-to-end run (`scripts/e2e.py`) is on demand, and the owner's
  (UAC prompt): each mode is judged only if LagLab's window kept the focus for the whole capture (a covered window
  gives PresentMon no rows), so when you ask for a run, ask the owner to leave the PC alone until it ends (about 5.5
  minutes). LagLab's hog mode burns every CPU in bursts: an agent runs it only on an idle PC, after the same check for
  other sessions' `pytest`, `redfirst.py` or `laglab` as before a test run. The venv's
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
  and call `diagnose()` in memory (`Capture.load(path)`; to diff a change against main, put a `git worktree add
  --detach` of main under `%TEMP%` first on `PYTHONPATH` and check `lagdoctor.__file__`, as PR #45 did for every e2e
  and FF7 capture; the agent's shell reads the real sessions folder, as it did on
  2026-11-09). Nor does an agent change a game's own files (its settings, saves or install folder): Documents is not
  moved, so an edit to a game's config there (FINAL FANTASY VII REBIRTH keeps its own under `Documents\My Games`)
  changes the owner's real game, while one under `%LOCALAPPDATA%` lands in the private copy yet reads back as applied
  (`docs/research/settings-sweeps.md`, section 6). Reading them is fine. `lagdoctor sweep` does change a game's
  settings file, and records each run, so only the owner runs it, in their terminal; its tests keep the settings file
  under the test's own `tmp_path` (`tests/sweep_world.py`), and the one that drives `cmd_watch` uses stand-in
  collectors. A sweep run counts only once the game has been in front `results.SETTLE_S` (120 s) before the press, so
  `sweep_world.PRESS_T` sits at 140 s and its recordings last `PRESS_T + 80`: a sweep test with a focus pattern of its
  own keeps the game in front that long before the press, or the run reads as pressed too soon (PR #69 rewrote every
  sweep test's times as offsets from `PRESS_T` and the window's end). Before you change a run's acceptance rule, replay
  `results.window_metrics` and `problems` over the owner's own recordings in memory: the clock-spread rule PR #69
  replaced would have kept out nearly every run on this laptop, and only that replay showed it.
- **`scripts/stage2` creates throwaway Windows services** (the capture-service experiments, `docs/INSTALLER-PLAN.md`
  Stage 2), and E9 a throwaway local account, which it deletes at the end. It runs only on GitHub's hosted runners, from `.github/workflows/experiments.yml` (started by hand, or by
  a pull request that changes it, `scripts/stage2` or a module the experiments import, which its `paths:` lists and
  `tests/test_side_workflows.py` keeps whole, and then on every push to that pull request, about 4 minutes a run);
  `scripts/stage2/guard.py` refuses anywhere else (exit 2). Never run it on this PC. Its instruments are
  checked by `tests/test_stage2.py` without any service (E9's
  WTSEnumerateProcessesExW opens every process it can, so its test stubs it out: never call it here); its results are read from the run's `stage2-results`
  artifact and the job summary (`docs/research/install-and-rights.md`, section 8, has the measured ones).
- **The MSI installs only on CI's runner.** `scripts/msi_check.py` installs and uninstalls a per-machine product
  (Program Files, the system PATH, Windows Installer's registry) and refuses anywhere but a GitHub-hosted runner
  (exit 2), before it reads the MSI or changes anything; never install the MSI on this PC. Building it needs the .NET SDK, which this
  laptop lacks, so it is built in CI; `tests/test_msi.py` checks the build, the WiX source and every install verdict
  on hand-written inputs. The owner accepted WiX v7's Open Source Maintenance Fee EULA for CI's builds
  (`docs/DECISIONS.md`, 2026-11-08): `--accept-eula wix7` appears once in `ci.yml` (the test holds it there), every
  `wix` command but help, version and eula needs it, and no other place accepts it without asking the owner. The
  `UpgradeCode` in `installer/lag-doctor.wxs` never changes (a new one installs beside the old version). The
  install's `python.exe` is the capture service's program, so it sits in a component of its own (`CaptureService`,
  keyed by it) and the harvest excludes it; the service's name, arguments and descriptor are `protocol.SERVICE_NAME`,
  `SERVICE_ARGUMENTS` and `SERVICE_SDDL`, which `tests/test_msi.py` holds equal to the `.wxs`. `wix build` lets
  WIX1149 through by its id (`-sw1149`, DECISIONS 2026-11-25) and no other warning. The check's `recording` verdict
  makes a throwaway standard account (`stage2/logon.py`), runs `scripts/msi_service_client.py` as it with the
  installed runtime against a stand-in game (`stage2/standin.py`), and deletes the account after the uninstall: like
  the install itself, only on the runner. The
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
  `launch_elevated` refuse, so a slip fails instead of prompting). For Play (M6), `App(installed=)` and `covers=`
  default to none, and only `run()` gives the stores' own (`library.installed_games`, `stores/art.covers`), so a test
  that builds an App never lists this PC's store library or reads its Steam cache: give it `tests/app_demo.py`'s
  made-up `INSTALLED` and `covers`, and an `open_link` and `play_recorder` of the test's own (the default opens a real
  store link, which starts a game). `App(picker=)`, Add a game's Open dialog (screen 6), defaults to none as well: the
  real one waits for a person, and `tests/conftest.py`'s `_no_real_dialog` fails a test that reaches it. Its own test
  (`tests/test_dialogs.py`) puts a stand-in under the guard's seam (`dialogs._GetOpenFileNameW`): a sabotage of the
  guard switches the autouse copy off too, and without the stand-in the first red-first run of it opened Windows' real
  Open dialog, which sat on the owner's screen until the run's 10-minute bound (PR #64, 2026-11-19). A test that
  adds a game picks a made-up program (`tests/pe_build.py`) under its own `tmp_path`, never a game's file.
  `App(capture_lock=)`, the lock name Delete looks at for another process's recording, defaults to none as well, so
  a test never meets the owner's own `watch`; a test of it holds a lock of its own name (`elevation.hold_named_mutex`).
  `App(trash=)`, Delete's move to the Recycle Bin (`winapi/recycle.to_recycle_bin`), defaults to none too: the real
  one puts the test's folder in the owner's own Recycle Bin, so `tests/conftest.py`'s `_no_real_recycle_bin` fails a
  test that reaches it, its own test (`tests/test_recycle.py`) stubs `recycle._SHFileOperationW` under the guard's
  seam, and only `tests/test_recycle_live.py`, on GitHub's runner (`scripts/stage2/guard.py`), moves a folder for
  real. `App(fixes=)`, Apply and Undo's fixes and journal (`fixes/flow.Fixes`), defaults to none too: the real ones
  change this PC's own settings, so an App a test builds takes `tests/fake_fixes.py`'s stand-ins, whose values live
  in a dict, and `tests/conftest.py`'s `_no_real_fixes` fails a test that reaches `Fixes.fix` (its own test stubs
  `flow.catalog.get` under the guard's seam). A test whose sabotage could let a refusal slip must not let that reach
  anything real either: `test_app_fixes.py`'s one-change-at-a-time test asks to record while a fix runs, so it gives
  the App a stand-in recorder first. A program icon is read for any game with a program and no cover (an Epic launcher's, a recording's
  `summary.json` `exe`): a test that needs an icon makes its program under its own `tmp_path` (`tests/pe_build.py`).
  `tests/app_demo.py`'s recordings name made-up programs under `C:\Games`, which an App with no covers then looks for
  there, read-only (`stores.open_file`'s stat first); keep them names no real game has.
- **The overhead benchmark** (`scripts/overhead/bench.py`, `docs/research/overhead.md`) opens a LagLab window for
  about 6.5 minutes a suite and holds lag-doctor's capture lock for the whole run, so the owner's own `watch` refuses
  meanwhile (exit 4), and the benchmark refuses to start while a recording runs: run it when the owner is not
  mid-game, and only on a quiet PC (its CPU figures nearly double under load). Its `pm-` suites need an
  administrator terminal, so they are the owner's; without one, or without a PresentMon that passes `watch`'s check,
  it exits 2 before starting anything.
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
  administrator rights for a recording in the user's own window. What it may record comes from its command line
  (`client.relay_argv`, parsed by `server.parse_args` on both sides of the prompt; the player's note is never on it,
  which `test_the_note_stays_with_the_recorder_and_never_goes_to_the_relay` holds): the running game, or for Play the
  forms of its install folder and how long to wait for it. From the pipe it takes only `start`, `stop` and, for Play,
  `game` naming the process the recorder found, which it records only once `target.check_proposed` accepts it on the
  relay's own snapshots (DECISIONS 2026-11-17); the relay resolves no path and reads no folder. It passes back only
  what `protocol.py` lists: the game's frame rows and events, the recorder's own events, per-program byte totals for
  the recorder's Windows session (DECISIONS 2026-11-08) and its answer about each game. A new message or field goes
  through `protocol.from_relay` or `from_recorder` with its checks, and changes `protocol.VERSION`.
  `tests/test_relay_live.py` starts the real relay elevated and runs only on GitHub's runner
  (`scripts/stage2/guard.py`); the other relay tests run its code on a thread here, unelevated. An agent never starts `python -m lagdoctor.relay` itself: only `watch` does.
- **The capture service (`lagdoctor/service/`, DESIGN.md "The capture service") runs only as the SCM starts it.**
  An agent never installs, starts, stops or reconfigures a service on this PC (`sc.exe`, StartServiceW): only the
  MSI installs it, and only on CI's runner. Its tests (`tests/test_service.py`) run its `Host` on a thread with the
  relay tests' stand-ins, on a pipe of the test's own name with `owner=""` (the service's own SID exists only in the
  service), and ask the SCM about services by name, read-only; `python -m lagdoctor.service` started any other way
  runs nothing (exit 1063). A caller is judged by its own token through the pipe (`PipeServer.caller`), never by what
  it sends; a change to what the service records needs the same judgement on the service's own reads. `watch` asks
  the service before the UAC prompt (`relay/client.via_service`), so `tests/conftest.py` sets `LAGDOCTOR_NO_SERVICE`
  for the whole run (a `watch` a test starts in a child inherits it) and `_no_real_capture_service` fails a test that
  calls `winapi/service.start` or `query` on `protocol.SERVICE_NAME`; once the MSI is installed here the real service
  would answer. `tests/test_service_client.py` lifts the variable for itself and talks to an in-process host.
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
  failing scan still let that body go out. The scan passes on grep's exit 1 only: `! grep` also passes on grep's own
  error (exit 2), and PR #34's body went out unscanned that way when a backslash the Bash tool halved left `\2` in the
  pattern (2026-11-09; a rescan found it clean). Take `.git/hooks/pre-push`'s own `pattern=` line in a script written
  with the Write tool, add `Users[/\]` for folder paths (Git Bash's `grep -F` aborted on a backslash), and prove it on a
  planted positive first.
- **The README's Experience Gained carries totals** (tests, red-first sabotages and their specs, the tests on the real
  PresentMon). Count them from the source, never the old figure plus your delta: `uv run pytest --collect-only -q`
  for tests, the mutations in `tests/redfirst/*.json` for sabotages and specs. When PRs merge in parallel, the last
  to land recounts on main: a total its PR did not touch goes stale without a merge conflict (after #4 and #5, and
  again after #7). The PresentMon figure (44 since M6d's live relay test for a folder) is the tests that run on CI with a
  download, not CI's "tests that use a download" count (45, one of which skips there): read a counter at the commit
  that set a figure before
  correcting the figure against it.
- **CI's runner lacks some of this laptop**: no GPU, NVIDIA driver, thermal sensors, ping replies or ASUS fan device
  (`asus_acpi`, declared before a CI run showed it: a GitHub VM has no ASUS firmware). (Its High
  performance plan has no power-mode slider; `slider_mode()` reads the plan's personality, so the power tests hold
  there undeclared.) The workflow declares that in `LAGDOCTOR_ABSENT`, and a test that needs one of those carries
  `@pytest.mark.needs(name)` (names in `tests/capabilities.py`), so it skips there with the reason; undeclared, a
  missing capability fails. Never make a test skip on a detected absence that a regression could also cause (the NVIDIA
  fixtures used to), and mark a test only when a CI run shows it failing for that lack: a test that passes on the
  runner keeps running there, even on another branch (elevated, the ETW tests check the real-time session). A PC
  without an NVIDIA GPU sets `LAGDOCTOR_ABSENT=nvidia`, and one that is not an ASUS laptop adds `asus_acpi`. The
  runner has the WebView2 Runtime and a desktop (M0's
  probe), so the app's window tests carry `needs("webview2")` and CI declares nothing for them. The runner's account
  is an administrator with UAC off, so `tests/test_cli.py` models an unelevated owner unless a test says otherwise,
  and the app's own `run()`, which refuses an elevated start, is never what a test opens: `tests/app_harness.py`
  builds `App` itself.
- `python3` in Git Bash here is the Microsoft Store placeholder: it waits instead of running, and so does a bare
  `python` (`python -` hung a whole command on 2026-11-16). Use `uv run python`.
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
  A Bash heredoc whose body holds a Python `'''` string failed outright (bash: "unexpected EOF while looking for
  matching" quote, three times on 2026-11-11), and so did one holding only f-strings with quotes in them (2026-11-19):
  write any multi-line edit script with the Write tool and run the file.
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
  needs LagLab to render where Windows counts it (`tests/test_folder_wait.py::test_a_real_laglab_window_is_found_by_its_
  folder` with it: both failed, with `test_nvidia_live`'s driver-refresh test, on unchanged code on 2026-11-15, twice): on 2026-11-07 at 00:30 (Silent mode, display asleep) it failed on
  unchanged code too, in a worktree of the previous commit, and again on 2026-11-08 in a worktree of main. It also
  fails while another LagLab window is open: on 2026-11-08 another worktree's measurement script was running `python
  -m laglab` in a window of its own, the test's auto pick took that window, and it failed alone and on unchanged main
  alike, with `test_live_sampler_on_a_spinning_child_and_its_spinning_grandchild` failing beside it under that
  script's CPU load. Before rerunning, list the `python.exe` command lines that hold `laglab` (`Get-CimInstance
  Win32_Process`): one the test did not start means another session is measuring, so wait for it to end. Rerun them
  alone, with the display awake, and run that control before blaming a change. `tests/test_sysmon_windows.py::test_the_foreground_pid_owns_a_listed_window` fails whenever the screen is locked: LockApp then holds the foreground and owns no listed window (2026-11-17, 3 of 3 alone, on a branch that touched nothing it reads). The self-test cases of
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
  `tests/test_sweep_runner.py::test_input_that_ends_is_said_as_such` failed once in a full local run (2026-11-09, on
  M6c's branch, which touches nothing the sweep imports: exit 2, so the settings were not all put back), then passed
  3 of 3 alone and twice in its file. Its fake world's journal waits 0.3 s for its lock, which a loaded PC may
  outrun (not measured); rerun it alone before blaming a change.
  `tests/test_control.py::test_the_relay_is_started_through_the_windows_prompt` failed once in a full local run
  (2026-11-20, a branch that touched no control, relay or app code: `IndexError` at `fake_relay["launches"][-1]`,
  so the 0.1 s watch ended before it launched the stand-in relay), then passed 3 of 3 alone; rerun it alone first.
  On CI, `tests/test_relay_live.py::test_the_real_relay_for_a_folder_judges_each_game_by_its_own_read_and_records_the_
  one_it_took` failed once (PR #65, 2026-11-20, a change to the analysis only) on its check that no process it started
  sits under the recorder or the relay, and passed on the rerun of that job: rerun the failed job before blaming a
  change, and read which process the check found if it repeats.
  `tests/test_relay_live.py::test_the_real_relay_runs_presentmon_and_the_network_trace_for_this_process` failed once
  on main's push run (69bd3b7, 2026-11-25: PresentMon's exit code at the stop was 1, not 0) after the same tree had
  passed it on its pull request, and passed on the rerun of that job: rerun before blaming a change.
  `tests/test_folder_wait.py::test_a_real_laglab_window_is_found_by_its_folder` (needs a GPU, so not on CI) failed on
  2026-11-17 with "a window from the game's folder (python.exe) never drew within 20 seconds", alone and on main's own
  `target.py` alike, with no other LagLab window open and the screen unlocked: LagLab's draws did not reach the GPU
  counters then. It failed again on 2026-11-19, with `test_sysmon_target.py`'s windowed-renderer test beside it, both
  on main's own `target.py` too (byte-copied in, restored by hash): this PC's state; and on 2026-11-20 alone, on
  origin/main's own tree (a `git worktree add --detach` under `%TEMP%`, its `lagdoctor.__file__` checked). Run that
  control before blaming a change. `tests/test_app_page.py::test_record_runs_a_recording_the_window_follows_with_the_game_named_as_text` read
  BAD-BASE once in a long red-first run (PR #62, 2026-11-19: its baseline failed under the run's load) and passed its
  baseline and was CAUGHT when that one mutation was rerun alone: rerun such a mutation alone before blaming a change.
  `tests/test_redfirst.py::test_the_job_counts_every_process_under_pytest_and_ending_it_ends_them_all` failed once on
  main's push run (2026-11-11, 5ea3f80; the PR's run of the same tree and the rerun passed): right after `job.end()`,
  with the job counting none of them, the snapshot still listed one of the tree's four processes (the parent's
  interpreter under the venv's launcher), gone when the assertion built its message. It looked once; since 2026-11-12
  it waits for the tree to leave the snapshot (`_still_listed`, by a deadline taken when it starts, `TREE_GONE_S` =
  10 s; in 60 runs here the first snapshot after `end()` never listed one). A failure now names what stayed listed
  that long: a real leftover, not that lag. `test_a_hang_is_a_timeout_whose_processes_all_end_and_the_
  run_goes_on` still reads its leftovers once, right after the runner exits: if it fails the same way, give it
  `_still_listed` too.
  `tests/test_folder_wait.py::test_one_round_reads_at_most_max_reads_per_round_new_paths` failed once on main's push
  run (2026-11-17, PR #53's merge; `assert 513 == (513 + 1)`, green on rerun): its made-up world named lag-doctor by
  the test process's real number, `os.getpid()`, and the runner had handed out one between 1000 and 1512, where the
  test makes up 513 processes, so one of them read as lag-doctor and was never read (forcing `os.getpid` to 1004
  failed it every time). Since then the folder wait asks `target._own_pid()`, the world's fixture makes it `ME`
  (4,000,001, a number no real process holds), and `World.start` refuses that number. A fake process table elsewhere
  names lag-doctor by a made-up number through a seam too, never by mixing the real one in with made-up ones.
  Never run two pytest processes here at once (a review agent's tests beside the suite): they share
  `%TEMP%\pytest-of-<user>` and its keep-three cleanup, and on 2026-11-08
  `tests/test_downloads.py::test_fetch_gold_takes_the_pinned_commits_gold_folder_byte_for_byte` failed twice that way
  (WinError 5 renaming a folder under its `tmp_path`), then passed 3 of 3 alone. Another worktree's session counts
  too: before a run, list the `python.exe` command lines (`Get-CimInstance Win32_Process`) and wait while one holds
  `pytest`, `redfirst.py` or `laglab` (2026-11-09: a rerun met the M2 session's red-first), as a check that exits
  non-zero while one does and that the run's `&&` chain stops on: one that only printed "BUSY" let a run overlap
  another session's full suite (2026-11-17). The Window spike workflow's
  socket check (`sockets-1` or `sockets-2`) failed 3 of 20 attempts on 2026-11-08 (main 1 of 13, the M1 branch 2 of
  5), each on one UDP4 socket "bound" on an ephemeral port in a WebView2 process: the runtime's own lookups (in 20
  runs watched every 20 ms, all 70 of its UDP sockets were a utility process's, on 0.0.0.0, on a port Windows chose).
  Since PR #22 the check counts that one shape as WebView2's client and fails on every other (`server_reason` in the
  spike; `docs/research/desktop-app.md` section 11). If it fails again, read the failing reading's `servers` in
  results.json (each names its reason and process kind) before rerunning.
