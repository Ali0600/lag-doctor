# lag-doctor: agent contract

Read `docs/DESIGN.md` before changing anything structural: it is the contract (module interfaces,
collector `result()` keys, inventory keys, stats keys, fix ids). Exact Windows names (PresentMon columns,
counter paths, struct offsets, event layouts, registry values) are in the fact-checked notes under
`docs/research/`; where a note's body and its verification log disagree, the log wins.

## Commands

```powershell
uv sync --extra laglab --python 3.13                     # set up .venv (Python 3.13: unpinned, uv takes 3.12)
uv run pytest -q                                         # unit + live read-only tests
uv run pytest -q tests/test_<area>.py                    # one area (use this while other work is running)
uv run python scripts/redfirst.py tests/redfirst/<spec>.json   # prove a guard's test can fail
uv run python -m tests.downloads fetch-gold              # PresentMon's gold captures, for the replay tests
uv run python -m laglab --list-modes                     # the fake laggy game used by tests
uv run python -m lagdoctor ...                           # the tool (see README)
```

CI (`.github/workflows/ci.yml`) runs the suite on `windows-2025` for Python 3.13 and 3.11 on every push to `main` and
every pull request; code and workflow changes go through a pull request and merge on green. Before the tests it gets
PresentMon through `lagdoctor setup --download` and the gold captures through `tests.downloads fetch-gold`, checks
both against the tests' pins and caches them; each job's summary counts the tests that use them.

## Rules an agent must not guess

- **Never touch the game process.** No injection, hooks, memory reads, or `OpenProcess` on the game. Use
  `winapi/ntquery.py` for other processes' PIDs, names, paths, CPU, threads, I/O and parents. `OpenProcess` lives
  only in `winapi/process.py` (fixes on the user's own background processes). `tests/test_no_handles.py` enforces
  both. The one exception is PresentMon's own child process (DESIGN.md, Principles). In tests, where psutil stays as
  an independent readout, `psutil.Process(pid)` (even constructing it), `psutil.process_iter()` and
  `psutil.pid_exists()` open a handle: `psutil.pids()` is the only psutil call allowed on other processes.
- **`lagdoctor/` imports only the standard library and itself** (`tests/test_stdlib_only.py`): the installer ships
  python.org's embeddable runtime, and Smart App Control would block psutil's unsigned native module. Read Windows
  through `lagdoctor/winapi/` (ctypes), and name a dynamically imported module in a string literal.
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
  absolute path (reported UNISOLATED) or write into their own tree (stops the run).
- **Not measured is not fine.** A collector that cannot run reports `unavailable`/`failed` with the reason;
  a failed sample is `None`, never `0`; rules that need a missing source are listed as unchecked.
- **Text a player reads is plain words.** Research citations (`WT 8`, `LC 4.9`, `NE 7 row 9`) and review ids
  (`CC-5`) go in comments, never in a string the report, console or a fix shows; `tests/test_user_text.py`
  scans every module but `winapi/`. Name sources with `labels.source_text`, never by their ids.
- **Column names come from `lagdoctor/model.py`** (`F`, `S`, `G`, `N`); `SeriesBuilder` rejects unknown names.
- **One clock**: `lagdoctor.clock.now()` (QPC seconds). PresentMon runs with `--qpc_time`; its rows are
  timed by `TimeInQPC`, never by when they arrive (they arrive 0.1–2 s late, in bursts).
- **PresentMon**: parse its CSV by header name; on a pipe it is Latin-1 text, not UTF-16. Stop it with
  CTRL_BREAK (shared console) or `--terminate_existing_session --session_name lagdoctor`; never
  `Popen.terminate()` first, which leaks its ETW session until reboot. Exit code 6 is ambiguous: read stderr.
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
  the agent's shell was "not installed" for the user. `setup`, `watch`, `fix` and `undo` refuse there (exit 5,
  `paths.private_copy`) and `scripts/e2e.py` stops before its UAC prompt (exit 3). `scripts/etw_access.py`
  (E1) has no such check and raises its UAC prompt at once, so an agent never runs it either. Run them all in the
  user's Terminal panel. Tests are unaffected (their `LAGDOCTOR_HOME` is under `%TEMP%`).
- **`scripts/stage2` creates throwaway Windows services** (the capture-service experiments, `docs/INSTALLER-PLAN.md`
  Stage 2). It runs only on GitHub's hosted runners, from `.github/workflows/experiments.yml` (started by hand, or by
  a pull request that changes it or `scripts/stage2`, and then on every push to that pull request, about 4 minutes a
  run); `scripts/stage2/guard.py` refuses anywhere else (exit 2). Never run it on this PC. Its instruments are
  checked by `tests/test_stage2.py` without any service; its results are read from the run's `stage2-results`
  artifact and the job summary (`docs/research/install-and-rights.md`, section 8, has the measured ones).
- **The repo is public** (https://github.com/Ali0600/lag-doctor). Never commit the owner's personal details: write
  user-folder paths as `%LOCALAPPDATA%`, `%TEMP%` or `<repo>`, and leave out this PC's name and SID, its adapter and
  power-plan GUIDs, its public IP and MAC addresses (tests use documentation ranges, `192.168.0.x` for a home network,
  and made-up MACs and GUIDs), what else runs on it, and when its owner played or downloaded what. Push only `main` and
  branches cut from it: the history from before the 2026-11-07 rewrite stays in `refs/backup/`, `refs/original/` and
  the merged `worktree-*` branches, and `.git/hooks/pre-push` (local, not in the repo) refuses any commit that holds
  those details.
- **CI's runner lacks some of this laptop**: no GPU, NVIDIA driver, thermal sensors or ping replies. (Its High
  performance plan has no power-mode slider; `slider_mode()` reads the plan's personality, so the power tests hold
  there undeclared.) The workflow declares that in `LAGDOCTOR_ABSENT`, and a test that needs one of those carries
  `@pytest.mark.needs(name)` (names in `tests/capabilities.py`), so it skips there with the reason; undeclared, a
  missing capability fails. Never make a test skip on a detected absence that a regression could also cause (the NVIDIA
  fixtures used to), and mark a test only when a CI run shows it failing for that lack: a test that passes on the
  runner keeps running there, even on another branch (elevated, the ETW tests check the real-time session). A PC
  without an NVIDIA GPU sets `LAGDOCTOR_ABSENT=nvidia`. The runner's account is an administrator with UAC off, so
  `tests/test_cli.py` models an unelevated owner unless a test says otherwise.
- `python3` in Git Bash here is the Microsoft Store placeholder: it waits instead of running. Use `uv run python`.
- A new worktree has no `.venv`, and the first `uv run` builds one on uv's own managed Python 3.12 (pyproject asks
  only for 3.11 or later; seen 2026-11-07). Run the `uv sync ... --python 3.13` line first: it resolves to
  `C:\Python313`, the interpreter of the main checkout's venv.
- Git Bash rewrites an argument that looks like a POSIX path before starting a native program: `git sparse-checkout
  set --no-cone /Tests/Gold/` stored `C:/Program Files/Git/Tests/Gold/` (the checkout then wrote nothing), and `git
  show origin/main:.github/workflows/ci.yml` became `origin\main;.github\...`. Prefix such commands with
  `MSYS_NO_PATHCONV=1`, or run them from PowerShell or Python's `subprocess`, which pass arguments as typed.
- `tests/test_etw_tracker.py::test_the_tracker_over_a_real_private_etw_session_finds_the_server_and_a_delay_gap` is
  timing-sensitive under full-suite load (failed once on 2026-11-05 with 0 delay gaps, then 4/4 alone; on CI once in
  two full runs): rerun it alone before blaming a change for it, as CI does. A test that starts a child pytest gives it
  its own `TEMP` (as `tests/test_redfirst.py` and `tests/test_downloads.py` do): child runs in the shared pytest temp
  folder made this test about 2 s slower on CI and failed it 3 times in 16 runs (2026-11-07).
  `tests/test_nvidia_live.py::test_sampler_
  reads_each_row_just_after_a_driver_refresh` failed on 2026-11-07, alone too, with the NVIDIA GPU idle in P8 (no
  training job) and the screen locked; it had passed with the GPU busy. Two more depend on the PC's state: `tests/test_cli.py::test_a_real_console_event_
  while_recording_still_saves_the_capture` (its child hit the 60 s timeout once under full-suite load, then 3/3
  alone), and `tests/test_sysmon_target.py::test_auto_picks_a_windowed_renderer_and_never_a_windowless_gpu_job`, which
  needs LagLab to render where Windows counts it: on 2026-11-07 at 00:30 (Silent mode, display asleep) it failed on
  unchanged code too, in a worktree of the previous commit. Rerun them alone, with the display awake, and run that
  control before blaming a change. On a CI runner, the self-test cases of `tests/test_etw_flows.py` and
  `tests/test_etw_tracker.py` (4 at once) failed once with "no free port for both UDP and TCP" (2026-11-07): their
  loopback DNS stand-ins try 10 ephemeral ports for one number free for both; rerun before blaming a change.
  `tests/test_machine_inventory.py::test_live_collect_agrees_with_independent_readouts` failed once on a runner
  (2026-11-07, a docs-only change) with "the Win32_DeviceGuard query took over 10 s and was stopped": WMI was slow
  on that VM, not the code; rerun before blaming a change, and look at the inventory's own timeout if it repeats.
