# lag-doctor: agent contract

Read `docs/DESIGN.md` before changing anything structural: it is the contract (module interfaces,
collector `result()` keys, inventory keys, stats keys, fix ids). Exact Windows names (PresentMon columns,
counter paths, struct offsets, event layouts, registry values) are in the fact-checked notes under
`docs/research/`; where a note's body and its verification log disagree, the log wins.

## Commands

```powershell
uv sync --group laglab --python 3.13                     # set up .venv (Python 3.13: unpinned, uv takes 3.12)
uv run pytest -q                                         # unit + live read-only tests
uv run pytest -q tests/test_<area>.py                    # one area (use this while other work is running)
uv run python scripts/redfirst.py tests/redfirst/<spec>.json   # prove a guard's test can fail
uv run python -m tests.downloads fetch-gold              # PresentMon's gold captures, for the replay tests
uv run python -m laglab --list-modes                     # the fake laggy game used by tests
uv run lagdoctor ...                                     # the tool (see README); the elevated child runs -m lagdoctor
uv build --clear --out-dir dist                          # the sdist, and the wheel built from it
uv run python scripts/wheel_check.py dist                # that wheel in a fresh venv, run outside the checkout
uv run pytest -q tests/test_msi.py                       # the MSI's build, WiX source and install check, no install
uv run python scripts/runtime_modules.py --zip <path>.zip [--write]   # what the pinned runtime imports, asked of it
```

CI (`.github/workflows/ci.yml`) runs the suite on `windows-2025` for Python 3.13 and 3.11 on every push to `main` and
every pull request; code and workflow changes go through a pull request and merge on green. Before the tests it gets
PresentMon through `lagdoctor setup --download` and the gold captures through `tests.downloads fetch-gold`, checks
both against the tests' pins and caches them; each job's summary counts the tests that use them. Its wheel job runs
`uv build --offline` (with the copy of uv_build inside the pinned uv: `pyproject.toml`'s bound stays on that uv's
minor version, and `tests/test_wheel_check.py` fails when the two part) and then `scripts/wheel_check.py`. Its msi job
first asks the pinned runtime what it imports (`scripts/runtime_modules.py`) and fails unless
`scripts/runtime_modules.json` is that answer, then installs WiX 7.0.1 from its SHA-512-pinned package
(`scripts/wix_tool.py`), builds the MSI (`scripts/msi_build.py`), keeps it as the `lag-doctor-msi` artifact, then
installs, runs and uninstalls it there (`scripts/msi_check.py`, whose verdicts are the job summary; msiexec's logs are
the `msi-logs` artifact). The PresentMon cache key hashes
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
  `scripts/`, `docs/`, `.github/`, `installer/`, `pyproject.toml`, `uv.lock` and `LICENSE` (`COPY_DIRS`,
  `COPY_FILES`): a test that reads any other file fails before its sabotage (BAD-BASE) until that file is added there.
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
- **The desktop app is a plan only** (`docs/LAUNCHER-PLAN.md`; the owner, 2026-11-08). Build no part of it, the M0
  window spike included, until the owner says go. Its relay is also the protocol of Stage 4's capture service
  (`docs/INSTALLER-PLAN.md`), so Stage 4 work starts from that plan too.
- **The repo is public** (https://github.com/Ali0600/lag-doctor). Never commit the owner's personal details: write
  user-folder paths as `%LOCALAPPDATA%`, `%TEMP%` or `<repo>`, and leave out this PC's name and SID, its adapter and
  power-plan GUIDs, its public IP and MAC addresses (tests use documentation ranges, `192.168.0.x` for a home network,
  and made-up MACs and GUIDs), what else runs on it, and when its owner played or downloaded what. Push only `main` and
  branches cut from it: the history from before the 2026-11-07 rewrite stays in `refs/backup/`, `refs/original/` and
  the merged `worktree-*` branches, and `.git/hooks/pre-push` (local, not in the repo) refuses any commit that holds
  those details.
- **The README's Experience Gained carries totals** (tests, red-first sabotages and their specs, the tests on the real
  PresentMon). Count them from the source, never the old figure plus your delta: `uv run pytest --collect-only -q`
  for tests, the mutations in `tests/redfirst/*.json` for sabotages and specs. When PRs merge in parallel, the last
  to land recounts on main: a total its PR did not touch goes stale without a merge conflict (after #4 and #5, and
  again after #7). The PresentMon figure (40 since #13) is the tests that run on CI with a download, not CI's "tests
  that use a download" count (41, one of which skips there): read a counter at the commit that set a figure before
  correcting the figure against it.
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
- `uv sync` installs lag-doctor itself, editable: `.venv\Lib\site-packages\lag_doctor.pth` holds the checkout's root,
  so every process in the venv can import `lagdoctor`, `laglab`, `tests` and `scripts`, after its own folder,
  `PYTHONPATH` and site-packages. Red-first runs still test their copy: pytest's `pythonpath` and `cwd` put it first,
  and the probe takes the real tree off the test process's sys.path (Python lists a sys.path folder again after a
  file in it is saved, which read as UNISOLATED). A child that a test starts with neither its `cwd` nor `PYTHONPATH`
  on the copy still imports the real checkout, and its mutation shows as SURVIVED. A folder named
  `lagdoctor` without `__init__.py` (a fixture, a scratch project) loses to the real package even when it comes first
  on sys.path, so give it one (as `tests/fixtures/redfirst/project/lagdoctor/__init__.py` does). The wheel holds
  `lagdoctor/` and nothing else (uv_build takes the whole folder): a file the product reads from anywhere else would
  not ship, and the files check, which compares `lagdoctor/` only, would not name it.
- Git Bash rewrites an argument that looks like a POSIX path before starting a native program: `git sparse-checkout
  set --no-cone /Tests/Gold/` stored `C:/Program Files/Git/Tests/Gold/` (the checkout then wrote nothing), and `git
  show origin/main:.github/workflows/ci.yml` became `origin\main;.github\...`. Prefix such commands with
  `MSYS_NO_PATHCONV=1`, or run them from PowerShell or Python's `subprocess`, which pass arguments as typed.
  A native program cannot open bash's process substitution either: `gh pr merge --body-file <(git log -1 ...)` failed
  with "The system cannot find the path specified" (and a `| tail` after it hid the exit code); write the text to a
  file first.
- The agent's Edit and Write tools decode a `\u` escape typed into new text into the character itself: `\u00a0`
  landed as a literal no-break space (2026-11-07), which is what `console._PUNCT` writes as escapes to avoid. Build
  such text in Python from counted pieces (`chr(92) + "u00a0"`) and read the line back with `ascii()`.
- To read one CI job's log while the rest of its run goes on (a fast job's verdict, such as the wheel job on a
  sabotage commit), use `gh api repos/Ali0600/lag-doctor/actions/jobs/<job id>/logs --allow-escape-sequences`:
  `gh run view <run> --log --job <job id>` refuses until the whole run has finished, and without the flag `gh api`
  prints only a refusal of the log's colour codes (seen 2026-11-07).
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
  training job) and the screen locked; it had passed with the GPU busy. Two more depend on the PC's state: `tests/test_cli.py::test_a_real_console_event_
  while_recording_still_saves_the_capture` (its child hit the 60 s timeout once under full-suite load, then 3/3
  alone), and `tests/test_sysmon_target.py::test_auto_picks_a_windowed_renderer_and_never_a_windowless_gpu_job`, which
  needs LagLab to render where Windows counts it: on 2026-11-07 at 00:30 (Silent mode, display asleep) it failed on
  unchanged code too, in a worktree of the previous commit. Rerun them alone, with the display awake, and run that
  control before blaming a change. The self-test cases of `tests/test_etw_flows.py` and `tests/test_etw_tracker.py`
  failed on CI with "no free port for both UDP and TCP" until 2026-11-08 (4 at once on 2026-11-07, 2 on 2026-11-08):
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
