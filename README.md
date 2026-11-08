# lag-doctor

[![CI](https://github.com/Ali0600/lag-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/Ali0600/lag-doctor/actions/workflows/ci.yml)

Finds out **why a PC game lags** (low FPS, stutter, uneven pacing or network lag) by watching it from
outside while you play, explains the cause with the numbers behind it, and fixes what can be fixed safely.
Its own code never injects into, hooks or opens the game process. The one exception is PresentMon, which it
runs for frame timing: PresentMon holds a query-only handle to the game, the kind Task Manager uses. No tool
can promise what a given anti-cheat accepts.

Status: a working command-line tool for Windows 10/11, built on one hybrid-GPU laptop; CI runs its tests on
GitHub's Windows runners for every change. The parts that need admin rights (live frame timing and the game-server tracker) are tested against recorded traces
and private trace sessions; their live check is `scripts/e2e.py` (see Testing).

![lag-doctor's HTML report: verdict, ranked causes and frame-time chart](docs/images/report.png)

*The HTML report for a synthetic capture from the test suite (LagLab's disk-hitch scenario): the numbers are
generated, the analysis and the page are the real ones.*

## What it does

- **Records while you play**: every frame's timing (Intel PresentMon), the game's CPU per thread, GPU load
  and video memory, disk, RAM, CPU/GPU throttling, and the network path (router, internet, the game server's
  packet stream). Press **Ctrl+Alt+L** the moment it lags; the analysis looks at what happened just before.
- **Explains**: the lag type, the likely causes ranked with their evidence ("9 of 9 stutters and FPS drops
  coincided with the game reading from disk"), what was **checked and fine**, and what **could not be
  checked** and why. Nothing unmeasured is ever reported as fine.
- **Fixes, only with your OK**: the power plan, the GPU a game runs on (hybrid laptops), the screen refresh
  rate, Game Mode, background recording, windowed-game optimisations, and the priority of your own
  background programs. Each change is journaled first, verified by reading it back, and undone with
  `lagdoctor undo`. It never turns off a security feature, never kills or suspends a process, and never
  touches the game.

## Run it

In PowerShell (Python 3.11 or newer, managed by [uv](https://docs.astral.sh/uv/)):

```powershell
git clone https://github.com/Ali0600/lag-doctor         # get the code
cd lag-doctor
uv sync                                                 # create .venv, with the lagdoctor command in it
uv run lagdoctor setup                                  # download + verify PresentMon 2.6.0 (asks first)
uv run lagdoctor check                                  # settings check-up, no game needed
uv run lagdoctor watch --game eldenring.exe             # or --pid PID; with neither, the busiest game
uv run lagdoctor fix --list                             # the automatic fixes the last report suggested
uv run lagdoctor fix power_plan                         # shows before -> after and asks
uv run lagdoctor undo                                   # what lag-doctor changed; undo an ID, or --all
uv run lagdoctor analyze CAPTURE                        # re-run the analysis on a saved capture
```

- **`lagdoctor` is a command in `.venv`**: after `.venv\Scripts\activate`, plain `lagdoctor watch ...` works in that
  window. `uv build` makes a wheel of the `lagdoctor` package alone (`dist\`); nothing is published yet.

- **A recording ends** with Ctrl+C, after `--duration S`, or when the game exits. After the first Ctrl+C it
  says it is stopping and saving, and a second press cannot lose the capture.
- **Frame timing uses Windows event tracing (ETW), which needs admin rights.** `watch` offers one UAC prompt
  per recording (`--elevate` skips the question, `--no-elevate` records without frame timing). Only the
  recording runs elevated, in a window of its own: stop it there with Ctrl+C, and the first window analyses
  it. `setup` explains the alternative of joining *Performance Log Users* once, which covers frame timing
  but not the game-server tracker (Windows does not let that group start it; `--elevate` still does).
  Without either, everything else is still measured and the report says frame timing was not.
- **No silent recordings without frame timing.** If PresentMon is missing or fails its check, `watch` stops
  before it records and says what to run (`--no-elevate` records everything else anyway). If frame timing stops
  working mid-recording, or the game is in front for 10 s without a frame, it says so on the spot.
- **Run it from your own PowerShell window.** A shell that a packaged app started (the Claude desktop app is one)
  can have what it saves under AppData moved into that app's private folder, which your other windows and the
  admin recording window never see. lag-doctor checks where its files really land, and refuses to set up, record
  or fix from such a shell.
- **Closing the recording window** also stops and saves, but Windows allows a closing console about 5
  seconds. A 30-minute recording at 144 FPS saves in 2.6 s; much longer ones may not make it. Signing out or
  shutting down mid-recording loses the recording.
- Each recording is saved under `%LOCALAPPDATA%\lag-doctor\sessions\` with a self-contained HTML report
  (charts, causes, checked-and-fine, not measured) and the raw capture.

## Limits

- **GPU heat, clocks and throttling are NVIDIA-only** (NVML). On other GPUs the load and video memory still
  come from Windows counters.
- **`--elevate`, `scripts/e2e.py` and `scripts/etw_access.py` run this checkout's Python code as administrator.**
  A program already running as you could edit that code and ride along on the prompt you approve. Approve them
  only for a checkout you trust. For recordings, joining Performance Log Users skips elevation (frame timing then
  works without it; the game-server tracker does not).
- **Frame caps a hair under the refresh rate** (about 143.3–144 FPS on a 144 Hz screen) can still count a
  frame held one refresh longer than usual as a stutter.
- **The low-frame-rate threshold** (80 % of the refresh rate) comes from a study of 60–120 Hz screens and is
  not tuned above that.
- **Long recordings use RAM**: about 0.7 KB per frame, so a 3-hour evening at 240 FPS needs about 1.8 GiB.
- **Single-player games: add `--no-network`.** The game-traffic tracker follows UDP only, so it cannot tell an
  offline game from one that talks to its server over TCP. Without the flag, internet ping spikes and lost
  pings count as network lag even in a game that never goes online.

## How it works

```
 game (untouched) ──► ETW ──► PresentMon ──► frames ─┐
 Windows counters (PDH), NtQuerySystemInformation ──► system (4 Hz) ─┤
 NVML ──► NVIDIA clocks, temperature, throttling ──► gpu (1 Hz) ─────┼─► capture ─► analysis ─► report
 ICMP, Wi-Fi, NIC counters ──► network (2 Hz) ───────────────────────┤        (one QPC clock)   + fixes
 ETW Kernel-Network ──► the game server's packet stream ─────────────┘
```

- One clock (QueryPerformanceCounter) for every source, so a stutter lines up exactly with the CPU, GPU,
  disk and network samples around it.
- **Analysis**: finds stutters, FPS drops, pacing problems, ping spikes, packet loss and server silences;
  decides whether the GPU, one CPU thread, VSync or a frame cap limits the frame rate (Intel's GPU Busy
  method); links each stutter to what changed at that moment (a lift test against the calm stretches); then
  36 rules score causes by evidence family, so no single signal is counted twice.
- **Layout**: `lagdoctor/winapi` (thin ctypes wrappers), `capture` (one collector per source plus the
  session), `analysis`, `fixes` (journal, readback, undo), `report` (console and HTML). `laglab/` is a fake
  game that lags on purpose, used to test the whole chain. `docs/DESIGN.md` is the contract between them.

## Testing

```powershell
uv sync --extra laglab                                         # adds pyglet, for LagLab
uv run pytest -q                                               # unit + live read-only tests
uv run python scripts/redfirst.py tests/redfirst/<area>.json   # prove each guard's test can fail
uv run python scripts/e2e.py                                   # LagLab scenarios, judged end to end (UAC prompt)
uv run python scripts/etw_access.py                            # who may trace on this PC (read-only, UAC prompt)
uv build --clear --out-dir dist                                # the sdist and the wheel, as CI builds them
uv run python scripts/wheel_check.py dist                      # install the wheel in a fresh venv and run it there
```

- **LagLab** produces one known cause per scenario (GPU-bound, CPU-bound, CPU hitch, disk hitch, wait hitch,
  background hogs, uneven pacing, and a smooth control) and logs the ground truth on the same clock.
  `scripts/e2e_truth.py` scores each diagnosis against that log: found the lag moments, invented none,
  blamed the right cause.
- **Red first**: every guard ships with a mutation that must turn its test red. `scripts/redfirst.py`
  applies each one to a throwaway copy of the repo, runs the named tests there, and counts it only when a
  test failed (an import error does not count); the real working tree is never written.
- **No real settings change in tests**: registry fixes run in a sandbox key that is deleted afterwards, the
  power, display and GPU-preference backends are faked, and priority tests act only on a child process the
  test started.
- PresentMon's parser is tested against its own gold ETW captures replayed through the real exe, and the
  ETW consumer against private in-process sessions, so neither needs admin rights in the test suite.
- **CI** (`.github/workflows/ci.yml`) runs the suite on GitHub's Windows Server 2025 runners for Python 3.13 and
  3.11. A runner has no GPU, no NVIDIA driver, no thermal sensors and no ping replies: the workflow declares that in
  `LAGDOCTOR_ABSENT`, and the tests marked as needing those skip there with the reason. Its High performance plan
  needs no declaring: the power tests hold on any plan. On a PC without an NVIDIA GPU, set
  `$env:LAGDOCTOR_ABSENT = "nvidia"` before running the tests, or the NVIDIA ones fail.
- **The wheel, installed**: a second CI job builds the wheel, installs it into a fresh venv and runs `lagdoctor`
  there, outside the checkout (`scripts/wheel_check.py`). The wheel must hold exactly the files git tracks under
  `lagdoctor/`, every module must import from that venv, `--version` must name the wheel's version, and `analyze`
  must write its report for a synthetic capture. `--help` and `analyze` alone would miss most of that: `analyze`
  never imports psutil, nor most of `capture/`, `fixes/` and `winapi/`.
- **Downloaded test data**: the tests that replay or verify the real PresentMon need its pinned release and its gold
  captures. `uv run lagdoctor setup` gets the first, `uv run python -m tests.downloads fetch-gold` the
  second; without them those tests skip with the reason. CI gets both the same way before the tests, checks them
  against the tests' own pins, caches them, and sets `LAGDOCTOR_REQUIRE` so a missing one fails those tests
  instead; each job's summary counts them.

## Experience Gained

- **Windows systems programming from Python**: 15 ctypes modules over 17 Windows DLLs plus NVIDIA's NVML
  (real-time and private ETW sessions, PDH, NtQuerySystemInformation, DXGI/DXCore, ICMP, WLAN), with zero
  handles opened to the game, enforced by a syntax-tree scan of the code.
- **Telemetry on one clock**: 5 collectors (per-frame timing, a 4 Hz system sampler, 1 Hz NVIDIA telemetry,
  2 Hz network probes, an ETW packet tracker) stamped by QueryPerformanceCounter, so a stutter lines up with
  the samples around it.
- **A diagnosis engine judged against ground truth**: 36 rules with lift-test attribution and
  evidence-family scoring, scored end to end against LagLab, a fake game with 8 scenarios that logs what it
  did wrong.
- **Testing that proves itself**: 2,645 tests, run in CI on GitHub's Windows runners for 2 Python versions with
  SHA-pinned actions, beside a job that installs the built wheel into a fresh venv and runs it there; the hardware
  a runner lacks is declared, so those tests skip there by name and still fail on the laptop if a bug hides the
  hardware, and the 32 tests on the real PresentMon get its release and gold captures fetched, pinned and cached
  there, failing rather than skipping if they go missing; plus a mutation harness of 1,485 deliberate sabotages in
  29 specs that must each turn a test red, run on a throwaway copy of the tree with hash checks.
- **Safe automation of system changes**: 7 opt-in fixes behind a write-ahead journal, readback
  verification, undo and crash healing, with a handle-free identity check before touching any process.
- **Supply chain and least privilege**: PresentMon pinned by SHA-256, checked through a deny-write handle and
  launched by that handle's resolved path; only the capture runs elevated, behind 1 UAC prompt and a
  machine-wide one-recording lock; the planned capture service measured on GitHub's throwaway runners before a
  line of it ships: a 1-privilege LocalService service enabling all 13 ETW providers, a named-pipe trigger starting
  it in 0.08 s, and a 30-binary signed Python runtime, all read back from the running token.
- **Review to fix**: closed all 49 findings (12 high) of an adversarial code review in 3 verified fix waves,
  cut the capture save from 36 s to 2.6 s to fit the 5 s Windows gives a closing console, and traced a 14-minute
  game recording with no frame timing to Windows keeping a packaged app's AppData files in a private copy.

## Credits

Frame timing comes from [Intel PresentMon](https://github.com/GameTechDev/PresentMon) 2.6.0 (MIT licence),
downloaded and hash-verified at setup. The tests replay PresentMon's own gold ETW captures. The repo carries
some of PresentMon's own output, Copyright Intel Corporation under the MIT licence: its `--help` text
(`docs/research/presentmon-2.6.0-help.txt`) and trimmed captures in `tests/fixtures/presentmon/`, whose README
holds the licence text.

## Licence

lag-doctor is released under the [MIT licence](LICENSE).
