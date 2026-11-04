# lag-doctor: agent contract

Read `docs/DESIGN.md` before changing anything structural; exact Windows names (PresentMon columns,
counter paths, event layouts, registry values) are in the fact-checked notes under `docs/research/`.

## Commands

```powershell
uv sync --extra laglab                                   # set up .venv (Python 3.13)
uv run pytest -q                                         # unit + live read-only tests
uv run python scripts/redfirst.py tests/redfirst/<spec>.json   # prove a guard's test can fail
uv run python -m laglab --list-modes                     # the fake laggy game used by tests
uv run python -m lagdoctor ...                           # the tool (see README)
```

## Rules an agent must not guess

- **Never touch the game process.** No injection, hooks, memory reads, or `OpenProcess` on the game.
  Game CPU/thread/IO numbers come from the system-wide `NtQuerySystemInformation` snapshot; GPU numbers
  from PDH counters. Anti-cheat safety depends on this.
- **Tests never change real settings.** Registry fixes run against the `reg_sandbox` fixture
  (`HKCU\Software\LagDoctorTest\<uuid>`, deleted afterwards); power, display and GPU-preference backends
  are faked. Process-priority tests act only on a child process the test spawned. `LAGDOCTOR_HOME` is
  redirected for every test by `tests/conftest.py`.
- **Red first.** A new guard ships with a mutation in `tests/redfirst/*.json` that turns its test red;
  `scripts/redfirst.py` must report every mutation `CAUGHT`. Never weaken a test to make it pass.
- **Not measured is not fine.** A collector that cannot run reports `unavailable`/`failed` with the reason;
  rules that need it are skipped and listed as not checked.
- **Column names come from `lagdoctor/model.py`** (`F`, `S`, `G`, `N`); `SeriesBuilder` rejects unknown names.
- **One clock**: `lagdoctor.clock.now()` (QPC seconds). PresentMon runs with `--qpc_time`.
- **Fixes go through `lagdoctor/fixes/base.py`** (journal first, readback, undo). Never offered: disabling
  security features, killing processes, HKLM tweaks.
- **ETW needs rights.** PresentMon and the ETW network tracker need admin or Performance Log Users; the
  end-to-end run (`scripts/e2e.py`) is on demand, never in CI. The venv's `python.exe` is a launcher: the
  real process is the base interpreter, so target LagLab by the PID it writes to `--pid-file`.
- **This laptop is hybrid** (RTX 3070 Laptop + Intel UHD). OpenGL apps such as LagLab run on the Intel GPU
  by default; `SHIM_MCCOMPAT=0x800000001` in LagLab's environment moves it to the NVIDIA GPU.
- Windows PowerShell 5.1: `Out-File -Encoding utf8` writes a BOM (breaks commit messages: write with
  `[IO.File]::WriteAllText` and `UTF8Encoding($false)`), and `Get-Process` has no `CommandLine`
  (use `Get-CimInstance Win32_Process`).
