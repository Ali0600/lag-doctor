# lag-doctor design

lag-doctor watches a running PC game from outside its process, works out why it lags, and fixes what
can be fixed safely. This document is the contract the modules are built against. Exact Windows names
(counter paths, CSV columns, event IDs, registry values) live in the fact-checked research notes under
`docs/research/`; this file says how the pieces fit.

## Principles

1. **Never touch the game.** No DLL injection, no hooks, no memory reads, no handles to the game process
   beyond what Task Manager uses. Everything comes from OS telemetry: ETW (via PresentMon), performance
   counters, system-wide process snapshots, nvidia-smi. Anti-cheat cannot see us, so it cannot ban for us.
2. **Evidence or silence.** Every finding carries the numbers behind it. A source that did not run is
   reported as *not measured*, never as *fine*.
3. **Fixes are opt-in, reversible and journaled.** The original value is written to the journal before
   anything changes; the change is verified by reading it back through an independent path; `undo`
   restores from the journal, never from an assumed default.
4. **Least privilege.** Only frame timing (and ETW network flows) need ETW rights. Everything else runs as
   a normal user, and the tool says plainly what it can't see without elevation.

## Data flow

```
                  ┌───────────────────────── capture session (capture/session.py) ──────────────────────────┐
 capture/target ─►│ frames   : PresentMon 2.6 child process, CSV stream ──► F columns                       │
  (pid, exe,      │ system   : 4 Hz sampler (PDH + NtQuerySystemInformation) ──► S columns + top "others"   │
   window,        │ gpu      : nvidia-smi streaming, 1 Hz ──► G columns                                     │
   adapter)       │ network  : ICMP pings + Wi-Fi + adapter counters, 4 Hz ──► N columns                    │
                  │ net_flows: ETW Microsoft-Windows-Kernel-Network (when allowed) ──► server, gaps, talkers│
                  │ marks    : global hotkey "it lagged now" ──► marks                                      │
                  └──────────────────────────────────────────────────────────────────────────────────────────┘
 capture/inventory ─ static snapshot at start (hardware, adapters, displays, power, game settings) ─┐
                                                                                                     ▼
                                                                        Capture (.ldcap.json.gz, model.py)
                                                                                                     │
 analysis/events ──► lag events (stutters, FPS drops, pacing, ping spikes/loss/server gaps, marks)  │
 analysis/attribute ──► per-event causes from the aligned system/gpu/network data around the event   │
 analysis/rules ──► session-level findings (wrong GPU, GPU/CPU limit, throttling, VRAM, settings ...)│
 analysis/diagnose ──► Diagnosis (ranked findings, lag type, headline, stats, what was not measured) ◄┘
          │
          ├─► report/console (terminal summary)     ├─► report/html (self-contained page, inline SVG charts)
          └─► fixes/catalog (apply with consent ─ journal ─ verify ─ undo)
```

## Privileges

| Needs | Works unelevated | Needs ETW rights (admin, or the Performance Log Users group) |
|---|---|---|
| Capture | system sampler, nvidia-smi, pings, Wi-Fi, inventory, hotkey | PresentMon frame timing, ETW network flows |
| Fixes | everything in HKCU, power scheme switch, priority of the user's own processes | nothing (HKLM changes are advice only) |

`lagdoctor watch` checks ETW rights first (`winapi/elevation.can_trace()`), then offers: relaunch elevated
(a UAC prompt), or continue without frame timing (frames reported *not measured*), plus how to join
Performance Log Users once so no prompt is ever needed.

## Package map

```
lagdoctor/
  model.py clock.py paths.py          shared contract (done)
  winapi/   pdh.py ntquery.py dxgi.py display.py power.py icmp.py wlan.py etw.py hotkey.py elevation.py
  capture/  presentmon.py sysmon.py gpumon.py netmon.py inventory.py target.py session.py
  analysis/ stats.py (done) events.py attribute.py rules.py diagnose.py
  fixes/    base.py journal.py catalog.py + one module per fix
  report/   console.py html.py
  cli.py __main__.py
laglab/     the fake laggy game used by tests (done)
```

`winapi/` modules are thin ctypes wrappers with no policy: they return plain Python values and raise
`OSError` with the Windows error code on failure. Policy (what to sample, what it means) lives above them.

## Collector contract

Every collector (capture/presentmon, sysmon, gpumon, netmon, and the ETW flow tracker) implements:

```python
class Collector(Protocol):
    name: str                                   # key in Capture.sources
    def start(self, origin: float) -> None      # origin = clock.now() at t = 0; starts its own thread
    def stop(self) -> None                      # idempotent; returns within 5 s; never raises
    def status(self) -> SourceStatus            # ok / partial / unavailable / failed / off + why
    def result(self) -> dict                    # what goes into the Capture (columns, extras)
    def live(self) -> dict                      # latest values for the live status line; thread-safe
```

Rules every collector follows:
- **One writer per series.** The collector's own thread owns its `SeriesBuilder`; others only read `live()`.
- **Timestamps** are `clock.now() - origin` (seconds) at the moment the sample's data was read.
- **A failure degrades, loudly.** A collector that cannot start sets `status()` to `unavailable`/`failed`
  with the reason (e.g. "PresentMon needs admin rights: access denied (error 5)") and returns empty
  columns; it never raises into the session and never fakes data.
- **Bounded.** Fixed sample rates, bounded buffers, no unbounded growth per tick (others: top 8 only).
- **No handles to the game.** The system sampler reads the game's CPU/thread/IO numbers from the
  system-wide `NtQuerySystemInformation(SystemProcessInformation)` snapshot and GPU numbers from PDH;
  it does not call `OpenProcess` on the game. (Other processes may be opened with
  `PROCESS_QUERY_LIMITED_INFORMATION` to read an exe path, never more.)

### What each collector's `result()` returns

The session copies these into the `Capture`. Keys not listed here go nowhere, so add them here first.

| collector (`name`) | `result()` keys → Capture field |
|---|---|
| `frames` (capture/presentmon.py) | `columns` (F) → `frames`; `swap_chains` `{address: frame_count}` and `presentmon` `{version, exe, args, exit_code, stderr_tail}` → `target["presentmon"]` |
| `system` (capture/sysmon.py) | `columns` (S) → `system`; `others` → `others`; `game_adapter_luid` (the adapter whose 3D engine the game used most, `"0xHHHHHHHH_0xLLLLLLLL"`, PDH's luid format) → `target["adapter_luid"]` |
| `gpu` (capture/gpumon.py) | `columns` (G) → `gpu`; `gpu_name`, `clock_max_mhz`, `mem_total_mb` → `inventory["nvidia"]` |
| `network` (capture/netmon.py) | `columns` (N) → `network`; `targets` `{gateway, internet, server}` (IP strings or None) and `interface` `{name, kind: "wifi"/"ethernet"/"other", ssid, band, channel}` → `net_flows["targets"]`, `inventory["network"]` |
| `net_flows` (capture/netflows.py) | `server` `{ip, port, proto, rx_packets, tx_packets, rx_bytes, tx_bytes}` or None; `talkers` `[{pid, name, rx_bytes, tx_bytes}]` top 8; `gaps` `[[t, ms], ...]` silences from the server over 3x its normal interval → `net_flows` |

`others` rows are `[t, [[pid, name, cpu_pct, io_bps, gpu_pct], ...]]`, top 8 non-game processes by CPU at that
tick, `cpu_pct` with 100 = one logical CPU.

### Inventory keys (capture/inventory.py)

A value of `None` means unknown. Rules and the report read exactly these keys.

```
os        {name, build}                                   e.g. {"name": "Windows 11 Home", "build": 26200}
cpu       {name, cores, threads, hybrid}                  hybrid: P+E cores (bool)
ram_gb    float
laptop    bool
adapters  [{luid, name, vendor, vram_mb, integrated}]     added by the session from winapi/dxgi
displays  [{device, name, adapter, internal, primary, width, height, hz, max_hz}]
          max_hz = highest refresh offered at the current resolution
power     {scheme_guid, scheme_name, overlay, on_battery, battery_saver}
          overlay: "best_performance" / "balanced" / "best_efficiency" / None
settings  {game_mode, game_dvr_background, hags, windowed_optimizations, vrr, memory_integrity}   bools
game      {exe, drive, media, free_gb, gpu_preference, monitor_device}
          media: "SSD" / "HDD" / "Unknown"; gpu_preference: 0 = let Windows decide, 1 = power saving,
          2 = high performance, None = not set; monitor_device: the display the game window is on
network   {name, kind, ssid, band, channel}               from the network collector
nvidia    {gpu_name, clock_max_mhz, mem_total_mb}         from the gpu collector
```

## Target selection (capture/target.py)

`find_target(name=None, pid=None, auto=False)` returns `{pid, name, exe, window_title, monitor, how}`:
- by `pid`, or by exe `name` (case-insensitive; if several match, the one with the most GPU 3D use);
- `auto`: the process with the highest 3D-engine GPU use over a 2 s PDH sample that owns a visible
  top-level window and is not on the ignore list (dwm.exe, explorer.exe, browsers, Discord, OBS, the
  tool itself). Ties go to the foreground window's process.
- The game's monitor comes from its largest visible top-level window (`EnumWindows` +
  `GetWindowThreadProcessId` + `MonitorFromWindow`), which needs no process handle.

## Analysis

### Frames
- Use the swap chain with the most frames; note others in `Diagnosis.stats["swap_chains"]`.
- Stats: `avg_fps` (frames / time), `low1_fps` and `low01_fps` (average of the slowest 1% / 0.1%),
  `p99_frame_ms`, `median_frame_ms`, `worst_frame_ms`, `stutters_per_min`, `dropped_pct`,
  `gpu_bound_pct`, `cpu_bound_pct`, `display_bound_pct`, `refresh_hz`, `present_mode` (most common).

`Diagnosis.stats` keys (missing or None = not measured; the report shows "-" for those):

```
frames, duration_s                      int, float       frames analysed (main swap chain), seconds
avg_fps, low1_fps, low01_fps            float            frames/time; average of the slowest 1% / 0.1%
median_frame_ms, p99_frame_ms, worst_frame_ms   float
stutters, stutters_per_min              int, float
dropped_pct                             float            % of frames never displayed
gpu_bound_pct, cpu_bound_pct, display_bound_pct float    % of frames
refresh_hz, present_mode                float, str       the game's display; most common present mode
ping_gw_ms, ping_inet_ms, ping_server_ms float           medians
jitter_ms, loss_pct                     float            internet anchor
```

### Lag events (analysis/events.py)
- **stutter**: frame time well above the trailing median of the frames before it (multiple *and*
  absolute floor; defaults from `docs/research/lag-causes.md`). Neighbouring stutter frames within 100 ms
  merge into one event; `severity` = worst frame ms.
- **fps_drop**: 1-second FPS windows well below the session's typical FPS, at least 2 s long.
- **pacing**: stretches where consecutive frame times swing widely without single big stutters, or where
  on-screen durations swing while present intervals are steady (VSync judder).
- **net_spike / net_loss / net_gap**: RTT far above its baseline, lost probes, or silences in the game
  server's packet stream.
- **mark**: each hotkey press; the analysis looks at the 5 s before it.

### Attribution (analysis/attribute.py)
For each event: the worst frame's split (CPU busy vs GPU busy vs waiting) says *which side* was slow;
then each signal in the samples overlapping the event is compared with the same signal's baseline (the
surrounding 10 s, other events excluded) using both a robust z-score (median/MAD) and an absolute
minimum change, so a flat-but-noisy signal can't fire. Candidate causes, best first:

| side | signal that moved | cause id |
|---|---|---|
| CPU | game's busiest thread up | `game_cpu_spike` (game's own work: loading, shader compiles, logic) |
| CPU | game I/O reads up, disk busy | `asset_streaming` |
| CPU | hard page faults up, RAM low | `memory_paging` |
| CPU | another process's CPU up, machine CPU high | `background_cpu` (names the process) |
| any | another process's disk or GPU up | `background_disk` / `background_gpu` |
| any | DPC/interrupt time up | `driver_dpc` |
| CPU | CPU effective speed down / thermal limit < 100 | `cpu_throttle` |
| GPU | GPU busy long, NVIDIA throttle reason active | `gpu_throttle` |
| GPU | VRAM full and shared GPU memory rising | `vram_overflow` |
| GPU | GPU busy long, nothing else moved | `gpu_spike` |
| none | nothing moved | `game_stall` (the game waited on itself) |

### Session rules (analysis/rules.py)
Sustained conditions and settings, each a pure function `rule(capture, ctx) -> Finding | None`:
`wrong_gpu`, `hybrid_copy`, `gpu_limit`, `cpu_limit`, `gpu_throttle`, `cpu_throttle`, `vram_overflow`,
`ram_pressure`, `power_plan`, `refresh_rate`, `vsync_judder`, `windowed_composition`, `game_dvr`,
`game_mode_off`, `background_hogs`, `hdd_streaming`, and the network rules `wifi_weak`, `wifi_roaming`,
`wifi_scan_spikes`, `bufferbloat`, `isp_latency`, `server_lag`, `packet_loss`, `bandwidth_hog`.
Every rule declares the sources it needs; when one was not measured the rule is skipped and the
Diagnosis lists it under `measured`, so the report says "not checked: frame timing (no admin rights)".

### Scoring
Per-event causes are counted (`background_cpu` explains 12 of 15 stutters). A finding's score combines
how strongly the evidence cleared its thresholds with how much of the lag it explains; confidence is
`high` only with direct evidence above threshold and enough samples. Lag type = the category with the
most lag time; the headline names the top finding in one sentence.

## Fixes (fixes/)

```python
class Fix(Protocol):
    id: str; title: str; needs_admin: bool; needs_restart: str | None
    def plan(self, params: dict) -> FixPlan          # reads the current value; says before -> after
    def apply(self, plan: FixPlan) -> FixResult      # journal FIRST, then change, then verify by readback
    def undo(self, entry: JournalEntry) -> FixResult # restore the journaled original, verify by readback
```

- **Journal** (`paths.journal_path()`): JSON list of entries `{id, fix_id, target, before, after, state,
  applied_at}` written atomically (temp file + replace). `state` goes `pending` (written before the change)
  → `applied` (after the readback matched) or `failed`. On start-up a `pending` entry means a crash
  mid-change: the tool reads the live value and offers to restore `before`.
- Fix backends are injectable (registry root, power API, display API) so tests use a sandbox
  (`HKCU\Software\LagDoctorTest\<uuid>`) or fakes, never the real settings.
- Catalogue (each verified by readback): `power_high_performance` (scheme/overlay), `gpu_preference_high`
  (per-exe, HKCU), `game_dvr_off`, `game_mode_on`, `windowed_optimizations_on`, `refresh_rate_max`
  (CDS_TEST first), `game_priority_above_normal` (session-only), `background_priority_lower`
  (session-only, the user's own processes only). Everything else (in-game settings, drivers, HAGS, MUX,
  cooling, Wi-Fi band, closing apps) is advice.
- Never offered: disabling security features (Memory Integrity/VBS, Defender), killing processes,
  HKLM tweaks.

## Report

- **Console**: the headline, key stats, top findings with their main evidence line and fixes, and what was
  not measured. Fits in one terminal screen.
- **HTML** (`report/html.py`): one self-contained file (inline CSS + SVG, no network), light/dark via
  `prefers-color-scheme`: verdict, stat tiles, frame-time chart with stutters and marks, CPU/GPU chart,
  VRAM chart, ping chart, finding cards, system snapshot, "not measured".

## CLI

```
lagdoctor watch [--game NAME | --pid PID | --auto] [--duration S] [--no-network] [--out DIR] [--hotkey KEY]
lagdoctor analyze CAPTURE            re-run the analysis on a saved capture
lagdoctor check                      settings check-up without a game running
lagdoctor fix [ID ...] [--list]      apply fixes suggested by the last report (asks first)
lagdoctor undo [ID | --all]          restore from the journal
lagdoctor setup                      fetch + verify PresentMon, check ETW rights
```

## Testing

- **Unit**: pure functions on literal sample points; parsers against fixture text taken from the real
  tool output; rules on synthetic captures built through `SeriesBuilder`.
- **Live, read-only**: ctypes wrappers run against this machine and are compared with an independent
  readout (psutil, typeperf, nvidia-smi).
- **Red first**: every guard is sabotaged once and must turn its test red (byte backup, hash check).
- **End to end**: `scripts/e2e.py` runs each LagLab mode with a real capture and checks the top cause
  against LagLab's own ground-truth log. Needs ETW rights, so it is run on demand, not in CI.
