# lag-doctor

Finds out **why a PC game lags** (low FPS, stutter, uneven pacing or network lag) by watching it from
outside while you play, explains the cause with the numbers behind it, and fixes what can be fixed safely.
It never injects into, hooks or even opens the game process, so anti-cheat has nothing to object to.

Status: a working command-line tool for Windows 10/11, built and tested on one hybrid-GPU laptop. The parts
that need admin rights (live frame timing and the game-server tracker) are tested against recorded traces
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

From the repo folder, in PowerShell (Python 3.11 or newer, managed by [uv](https://docs.astral.sh/uv/)):

```powershell
uv sync                                                 # create .venv
uv run python -m lagdoctor setup                        # download + verify PresentMon 2.6.0 (asks first)
uv run python -m lagdoctor check                        # settings check-up, no game needed
uv run python -m lagdoctor watch --game eldenring.exe   # or --pid PID; with neither, the busiest game
uv run python -m lagdoctor fix --list                   # the automatic fixes the last report suggested
uv run python -m lagdoctor fix power_plan               # shows before -> after and asks
uv run python -m lagdoctor undo                         # what lag-doctor changed; undo an ID, or --all
uv run python -m lagdoctor analyze CAPTURE              # re-run the analysis on a saved capture
```

- **A recording ends** with Ctrl+C, after `--duration S`, or when the game exits. After the first Ctrl+C it
  says it is stopping and saving, and a second press cannot lose the capture.
- **Frame timing uses Windows event tracing (ETW), which needs admin rights.** `watch` offers one UAC prompt
  per recording (`--elevate` skips the question, `--no-elevate` records without frame timing). Only the
  recording runs elevated, in a window of its own: stop it there with Ctrl+C, and the first window analyses
  it. `setup` explains the alternative of joining *Performance Log Users* once. Without either, everything
  else is still measured and the report says frame timing was not.
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
- **`--elevate` runs this checkout's Python code as administrator.** A program already running as you could
  edit that code and ride along on the prompt you approve. Approve it only for a checkout you trust, or join
  Performance Log Users and skip elevation.
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
- **Testing that proves itself**: 2,547 tests, plus a mutation harness of 1,406 deliberate sabotages in 26
  specs that must each turn a test red, run on a throwaway copy of the tree with hash checks.
- **Safe automation of system changes**: 7 opt-in fixes behind a write-ahead journal, readback
  verification, undo and crash healing, with a handle-free identity check before touching any process.
- **Supply chain and least privilege**: PresentMon pinned by SHA-256, checked through a deny-write handle and
  launched by that handle's resolved path; only the capture runs elevated, behind 1 UAC prompt and a
  machine-wide one-recording lock.
- **Review to fix**: closed all 49 findings (12 high) of an adversarial code review in 3 verified fix waves,
  cut the capture save from 36 s to 2.6 s to fit the 5 s Windows gives a closing console, and traced a 14-minute
  game recording with no frame timing to Windows keeping a packaged app's AppData files in a private copy.

## Credits

Frame timing comes from [Intel PresentMon](https://github.com/GameTechDev/PresentMon) 2.6.0 (MIT licence),
downloaded and hash-verified at setup. The tests replay PresentMon's own gold ETW captures.

## Licence

lag-doctor is released under the [MIT licence](LICENSE).
