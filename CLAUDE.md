# lag-doctor: agent contract

Read `docs/DESIGN.md` before changing anything structural: it is the contract (module interfaces,
collector `result()` keys, inventory keys, stats keys, fix ids). Exact Windows names (PresentMon columns,
counter paths, struct offsets, event layouts, registry values) are in the fact-checked notes under
`docs/research/`; where a note's body and its verification log disagree, the log wins.

## Commands

```powershell
uv sync --extra laglab                                   # set up .venv (Python 3.13)
uv run pytest -q                                         # unit + live read-only tests
uv run pytest -q tests/test_<area>.py                    # one area (use this while other work is running)
uv run python scripts/redfirst.py tests/redfirst/<spec>.json   # prove a guard's test can fail
uv run python -m laglab --list-modes                     # the fake laggy game used by tests
uv run python -m lagdoctor ...                           # the tool (see README)
```

## Rules an agent must not guess

- **Never touch the game process.** No injection, hooks, memory reads, or `OpenProcess` on the game.
  `psutil.Process(pid)` (even constructing it), `psutil.process_iter()` and `psutil.pid_exists()` open a
  handle: use `winapi/ntquery.py` for other processes' names, paths, CPU, threads, I/O and parents;
  `psutil.pids()` is the only psutil call allowed on other processes. `OpenProcess` lives only in
  `winapi/process.py` (fixes on the user's own background processes). `tests/test_no_handles.py` enforces
  both. The one exception is PresentMon's own child process (DESIGN.md, Principles).
- **Tests never change real settings.** Registry fixes run against the `reg_sandbox` fixture
  (`HKCU\Software\LagDoctorTest\<uuid>`, deleted afterwards); power, display and GPU-preference backends
  are faked. Process-priority tests act only on a child process the test spawned. `LAGDOCTOR_HOME` is
  redirected for every test by `tests/conftest.py`; the real PresentMon exe and PresentMon's gold ETL
  captures come from the `presentmon_exe` and `gold_etl_dir` fixtures (they skip, with the reason, when
  absent).
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
  need an elevated token or Performance Log Users; tests use private in-process sessions with unique names
  instead. The end-to-end run (`scripts/e2e.py`) is on demand. The venv's `python.exe` is a launcher: the
  real process is the base interpreter, so target LagLab by the PID it writes to `--pid-file`.
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
  `paths.private_copy`) and `scripts/e2e.py` stops before its UAC prompt (exit 3); run them in the user's
  Terminal panel. Tests are unaffected (their `LAGDOCTOR_HOME` is under `%TEMP%`).
- `python3` in Git Bash here is the Microsoft Store placeholder: it waits instead of running. Use `uv run python`.
