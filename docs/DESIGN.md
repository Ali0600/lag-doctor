# lag-doctor design

lag-doctor watches a running PC game from outside its process, works out why it lags, and fixes what
can be fixed safely. This document is the contract the modules are built against. Exact Windows names
(counter paths, CSV columns, struct offsets, event layouts, registry values) live in the fact-checked
research notes, cited below as `PM` (`docs/research/presentmon.md`), `WT` (`windows-telemetry.md`),
`LC` (`lag-causes.md`) and `NE` (`network-and-etw.md`) with their section numbers. Each note ends with a
verification log; trust a note's *Corrected* entries over its body.

## Principles

1. **Never touch the game.** No DLL injection, no hooks, no memory reads, no handles to the game process.
   Everything comes from OS telemetry: ETW (via PresentMon and our own Kernel-Network session),
   performance counters (PDH), the system-wide `NtQuerySystemInformation` snapshot, NVML. Anti-cheat
   cannot object to what never touches the game.
   - **The one exception is PresentMon itself**: its child process opens the game with
     `PROCESS_QUERY_LIMITED_INFORMATION` (the access Task Manager uses) and holds it until the game exits
     (PM 8.1). lag-doctor's own process never opens the game.
   - **psutil opens handles.** `psutil.Process(pid)` (the constructor itself), all its methods,
     `psutil.process_iter()` and `psutil.pid_exists()` open the process (WT 9.4). Only `psutil.pids()`
     is allowed for processes other than our own. Names, paths, CPU, threads, I/O and parent PIDs come
     from `winapi/ntquery.py`. `tests/test_no_handles.py` enforces this.
2. **Evidence or silence.** Every finding carries the numbers behind it. A source that did not run is
   reported as *not measured*, never as *fine*; a cause that was checked and ruled out is listed as
   *checked and fine* with what was measured.
3. **Fixes are opt-in, reversible and journaled.** The original value is written to the journal before
   anything changes; the change is verified by reading the effective state back through an independent
   path; `undo` restores from the journal, never from an assumed default.
4. **Least privilege.** Only frame timing and the ETW network flows need ETW rights. Everything else runs
   as a normal user, fixes never need elevation, and the tool says plainly what it cannot see.

## Data flow

```
                  ┌─────────────────────────── capture session (capture/session.py) ───────────────────────────┐
 capture/target ─►│ frames   : PresentMon 2.6.0 child, CSV on a pipe ──► F columns (all swap chains)            │
  (pid, exe,      │ system   : 4 Hz sampler (one PDH query + NtQuerySystemInformation) ──► S columns, others   │
   window,        │ gpu      : NVML, 1 Hz, only when the game renders on the NVIDIA GPU ──► G columns          │
   adapter)       │ network  : ICMP rounds at 2 Hz + Wi-Fi + NIC counters ──► N columns, lost probes, scans   │
                  │ net_flows: ETW Microsoft-Windows-Kernel-Network (when allowed) ──► server flow, gaps        │
                  │ marks    : global hotkey "it lagged now" ──► marks                                          │
                  └─────────────────────────────────────────────────────────────────────────────────────────────┘
 capture/inventory ─ static snapshot at start (hardware, adapters, displays, power, settings, game file) ─┐
                                                                                                           ▼
                                                                              Capture (.ldcap.json.gz, model.py)
                                                                                                           │
 analysis/frames    ──► main swap chain, frame stats, bound verdict (GPU / CPU / VSync / cap / waiting)   │
 analysis/events    ──► lag events (stutters, FPS drops, pacing, ping spikes, loss, server gaps, marks)  │
 analysis/attribute ──► per-event causes from the aligned system/gpu/network samples (lift test)         │
 analysis/rules     ──► session findings (wrong GPU, limits, throttling, settings, network ...)          │
 analysis/diagnose  ──► Diagnosis: ranked findings, lag type, headline, stats, cleared, unchecked     ◄┘
          ├─► report/console (terminal summary)     ├─► report/html (one self-contained page, inline SVG)
          └─► fixes/catalog (apply with consent ─ journal ─ verify ─ undo)
```

## Privileges and elevation

| Needs | Works unelevated | Needs ETW rights (an elevated token, or membership of Performance Log Users) |
|---|---|---|
| Capture | system sampler, NVML, pings, Wi-Fi, inventory, hotkey | PresentMon frame timing (exit 6 "access denied" otherwise, PM 6.2), ETW network flows |
| Fixes | everything in the catalogue (HKCU, power scheme, display mode, priority of the user's own processes) | nothing: fixes never run elevated |

- `winapi/elevation.can_trace()` = `CheckTokenMembership(Administrators S-1-5-32-544)` OR
  `CheckTokenMembership(Performance Log Users S-1-5-32-559)`; both count enabled SIDs only, so a UAC-filtered
  admin token answers False (PM 6.3, verify log).
- `lagdoctor watch` without ETW rights asks: relaunch the capture elevated (one UAC prompt), or continue
  without frame timing (frames reported *not measured*). `--elevate` / `--no-elevate` answer it up front.
  The elevated child runs **only the capture** (`watch ... --capture-only --out <absolute path>`) in its own
  console window, writes the capture file and exits; the unelevated parent waits, loads it, analyses,
  reports and offers fixes. So fixes and the journal stay in the user's own context (an over-the-shoulder
  elevation would otherwise write the admin account's HKCU).
- `lagdoctor setup` explains the alternative: join Performance Log Users once (an admin command the user
  runs, then sign out and in), after which no prompt is needed. The tool never changes group membership.

## Package map and ownership

```
lagdoctor/
  model.py clock.py paths.py                         shared contract (done)
  winapi/   hotkey.py (done)
            pdh.py ntquery.py windows.py                system side          (capture/sysmon, capture/target)
            dxgi.py display.py power.py storage.py      machine state        (capture/inventory, fixes)
            nvml.py                                     NVIDIA telemetry     (capture/gpumon)
            icmp.py iphlp.py wlan.py                    network probes       (capture/netmon)
            etw.py                                      ETW sessions         (capture/netflows)
            elevation.py                                rights + UAC relaunch
            process.py                                  priority of other processes (fixes/priority)
  capture/  base.py (done) target.py sysmon.py gpumon.py netmon.py netflows.py presentmon.py inventory.py
            session.py
  analysis/ stats.py (done) frames.py events.py attribute.py rules.py diagnose.py
  fixes/    base.py journal.py registry.py catalog.py (done) + power.py refresh_rate.py gpu_preference.py
            gamebar.py windowed.py priority.py
  report/   charts.py labels.py console.py html.py (done)
  setup.py  fetch + verify PresentMon
  cli.py __main__.py
laglab/     the fake laggy game used by tests (done)
scripts/    redfirst.py e2e_truth.py (done) e2e.py
```

`winapi/` modules are thin ctypes wrappers with no policy: plain Python values in and out, `OSError`
(with `.winerror`) on failure, every struct size asserted at import (the sizes are in the research notes).
Policy (what to sample, what it means) lives above them.

## winapi interfaces

Signatures other modules may rely on. Anything else in a module is private.

```python
# winapi/pdh.py (WT 1, 2) ------------------------------------------------------------------------------
class PdhQuery:
    def __init__(self, paths: list[str]) -> None   # English paths (PdhAddEnglishCounterW), "(*)" wildcards ok
    def collect(self) -> None                      # PdhCollectQueryData; rate counters need two collects
    def read(self, path: str) -> dict[str, float | None]  # instance -> value (PDH_FMT_DOUBLE|NOCAP100);
                                                   # a failed item -> None; a failed whole call -> {}
    def close(self) -> None

# winapi/ntquery.py (WT 3, 9.4; LC 1.3) -----------------------------------------------------------------
@dataclass(slots=True)
class ThreadInfo:  tid: int; create_time: int; user_100ns: int; kernel_100ns: int; priority: int; state: int
@dataclass(slots=True)
class ProcInfo:
    pid: int; ppid: int; create_time: int; name: str; session_id: int
    user_100ns: int; kernel_100ns: int; ws_bytes: int; private_bytes: int; page_faults: int
    read_bytes: int; write_bytes: int; other_bytes: int; read_ops: int; write_ops: int; other_ops: int
    thread_count: int; threads: list[ThreadInfo] | None      # parsed only for the pids asked for
class Snapshotter:                                 # one reusable buffer, grown on 0xC0000004
    def snapshot(self, threads_for: Iterable[int] = ()) -> dict[int, ProcInfo]
def image_path(pid: int) -> str | None            # SystemProcessIdInformation (88) + QueryDosDeviceW
def self_check() -> None                          # compare our own process with GetProcessTimes /
                                                  # GetProcessIoCounters; raise if the layout drifted

# winapi/windows.py (no process handles) ---------------------------------------------------------------
def top_windows() -> list[dict]   # visible, uncloaked top-level windows:
                                  # {hwnd, pid, title, area, minimized, monitor_device}
def foreground_pid() -> int | None
def window_monitor(hwnd: int) -> str | None       # "\\.\DISPLAY1" via MonitorFromWindow + GetMonitorInfoW

# winapi/dxgi.py (WT 5) ---------------------------------------------------------------------------------
def list_adapters() -> list[dict]
# {luid: "0xHHHHHHHH_0xLLLLLLLL" (lower-case, PDH's order: HighPart then LowPart), name, vendor:
#  "nvidia"|"amd"|"intel"|"microsoft"|"other", vendor_id, vram_mb, integrated: bool|None (DXCore),
#  software: bool, hags: bool|None (D3DKMT HwSchEnabled), hybrid: "discrete"|"integrated"|None, pci: str|None}

# winapi/display.py (WT 6; LC 9) -------------------------------------------------------------------------
def list_displays() -> list[dict]
# {device: "\\.\DISPLAY1", name, adapter_luid, internal: bool|None, primary: bool, width, height,
#  hz: float (QueryDisplayConfig rational, e.g. 144.003), max_hz: int (best at this resolution),
#  modes_hz: list[int] (offered at this resolution)}
def current_mode(device: str) -> dict             # {width, height, hz: int} from EnumDisplaySettingsW
def set_refresh(device: str, hz: int, test_only: bool) -> int   # DISP_CHANGE_* code; CDS_TEST or CDS_UPDATEREGISTRY

# winapi/power.py (WT 7) --------------------------------------------------------------------------------
def active_scheme() -> tuple[str, str]            # (guid, friendly name)
def list_schemes() -> list[tuple[str, str]]
def set_active_scheme(guid: str) -> None
def power_status() -> dict                        # {on_battery: bool|None, battery_pct: int|None, saver: bool|None}
def slider_mode(on_ac: bool) -> str | None        # "best_efficiency"|"balanced"|"best_performance"|None
def effective_mode(timeout_s: float = 1.0) -> str | None
# EFFECTIVE_POWER_MODE V2, hard-coded 0..6: "battery_saver","better_battery","balanced",
# "high_performance","max_performance","game_mode","mixed_reality"

# winapi/storage.py (WT 9.5) ---------------------------------------------------------------------------
def media_type(path: str) -> str                  # "SSD" | "HDD" | "Unknown"  (seek penalty, access 0)
def free_gb(path: str) -> float | None

# winapi/nvml.py (WT 4.4, 4.5) -------------------------------------------------------------------------
class Nvml:
    @classmethod
    def open(cls) -> "Nvml | None"                # None when nvml.dll is missing or init fails
    def devices(self) -> list[dict]               # {index, name, pci_bus_id, mem_total_mb, clock_max_mhz, temp_target_c}
    def sample(self, index: int) -> dict          # {util_pct, mem_used_mb, temp_c, power_w, power_limit_w,
                                                  #  clock_mhz, pstate, reasons: int, throttle_ns: {reason: cumulative ns}}
    def close(self) -> None

# winapi/icmp.py (NE 1) ---------------------------------------------------------------------------------
def ping(ip: str, timeout_ms: int = 1000, ttl: int = 128) -> tuple[int, float | None, str | None]
# (ICMP status, RTT in ms timed with perf_counter around the call, replying address); IPv4 and IPv6;
# success means status == 0, never just a non-zero return value

# winapi/iphlp.py (NE 1.5) -----------------------------------------------------------------------------
def best_route(dest_ip: str) -> dict | None       # {if_index, next_hop, source}; GetBestRoute2
def if_row(if_index: int) -> dict                 # {in_octets, out_octets, type, description, oper_status, tunnel}
def default_gateway() -> str | None

# winapi/wlan.py (NE 3) ---------------------------------------------------------------------------------
class Wlan:
    @classmethod
    def open(cls) -> "Wlan | None"
    def interfaces(self) -> list[dict]            # {guid, description, connected}
    def quality(self, guid: str) -> dict | None   # opcode 19: {rssi_dbm, signal_pct, rx_mbps, tx_mbps, freq_mhz, channel, phy}
    def retries(self, guid: str) -> int | None    # driver statistics, cumulative
    def bssid(self, guid: str) -> str | None      # None when location permission is off (error 5)
    def on_scan_complete(self, callback) -> None  # ACM notification 7; callback runs on a foreign thread
    def close(self) -> None

# winapi/etw.py (NE 5) ----------------------------------------------------------------------------------
def stop_session(name: str) -> int                # ControlTraceW STOP by name; 0 stopped, 4201 none
class RealtimeSession:                            # controller + consumer thread, one named session
    def __init__(self, name, providers: list[tuple[str, int, int]], on_event, private: bool = False)
    def start(self) -> None                       # stops an orphan of the same name first
    def stop(self) -> None
    def health(self) -> dict                      # {events, lost, callback_errors, pump_alive, process_trace_rc}

# winapi/elevation.py (PM 6) ----------------------------------------------------------------------------
def is_elevated() -> bool
def in_performance_log_users() -> bool
def can_trace() -> bool
def relaunch_elevated(argv: list[str], cwd: str) -> int   # ShellExecuteExW "runas", new console, waits;
                                                          # raises PermissionError when the user declines (1223)

# winapi/process.py (WT 9) ------------------------------------------------------------------------------
def priority_class(pid: int) -> int               # GetPriorityClass via PROCESS_QUERY_LIMITED_INFORMATION
def set_priority_class(pid: int, cls: int) -> None  # PROCESS_SET_INFORMATION; verify with priority_class()
def same_user(pid: int) -> bool                   # token user SID equals ours
def is_service_or_protected(pid: int) -> bool     # session 0, a service PID, or OpenProcess denied
```

## Collector contract

Every collector subclasses `capture/base.Collector` and implements:

```python
class Collector(Protocol):
    name: str                                   # key in Capture.sources
    def start(self, origin: float) -> None      # origin = clock.now() at t = 0; starts its own thread
    def stop(self) -> None                      # idempotent; returns within 5 s (PresentMon: 25 s); never raises
    def status(self) -> SourceStatus            # ok / partial / unavailable / failed / off + why
    def result(self) -> dict                    # what goes into the Capture (columns, extras)
    def live(self) -> dict                      # latest values for the live status line; thread-safe
```

Rules every collector follows:
- **One writer per series.** The collector's own thread owns its `SeriesBuilder`; others only read `live()`.
- **Timestamps** are `clock.now() - origin` seconds when the sample was read; PresentMon rows use their own
  `TimeInQPC` converted the same way (`clock.qpc_to_seconds(ticks) - origin`), never arrival time.
- **A failure degrades, loudly.** A collector that cannot start sets `status()` to `unavailable`/`failed`
  with the reason (e.g. "PresentMon needs admin rights: access denied") and returns empty columns; it never
  raises into the session and never fakes data. A failed sample is `None`, never `0`.
- **Bounded.** Fixed rates, bounded buffers, reused ctypes buffers, no unbounded growth per tick
  (`others`: top 8 only).
- **No handles to the game** (Principle 1).

### What each collector's `result()` returns

The session copies these into the `Capture`. Keys not listed here go nowhere, so add them here first.

| collector (`name`), module, class | `result()` keys → Capture field |
|---|---|
| `frames`, capture/presentmon.py, `PresentMonSource(pid, exe=None, etl_file=None)` | `columns` (F, every swap chain of the target) → `frames`; `swap_chains` `{"0x…": {"frames", "displayed", "runtime"}}`, `main_chain` (most displayed frames), `presentmon` `{version, exe, args, exit_code, stop_path, rows, malformed, rejoined, headers, stderr_tail}` → `target["presentmon"]` |
| `system`, capture/sysmon.py, `SystemSampler(target)` | `columns` (S) → `system`; `others` → `others`; `game_adapter_luid` (adapter whose 3D engine the game used most) → `target["adapter_luid"]`; `game_pids` (game + children seen) → `target["pids"]` |
| `gpu`, capture/gpumon.py, `NvidiaSampler(adapter)` | `columns` (G) → `gpu`; `device` `{name, clock_max_mhz, mem_total_mb, temp_target_c}` → `inventory["nvidia"]` |
| `network`, capture/netmon.py, `NetworkSampler(server_ip=None)` | `columns` (N) → `network`; `targets` `{gateway, internet, server}` (IPs or None), `probes` `{target: {"sent", "lost", "from_t"}}`, `lost` `{target: [t, …]}`, `wifi_scans` `[t, …]`, `route` `{if_index, description, vpn: bool}` → `net_flows[…]` under the same keys; `interface` `{name, kind: "wifi"/"ethernet"/"other", band, channel}` → `inventory["network"]`; `downloads` `[{t, source, mbps}]` (Steam log, Delivery Optimization) → `net_flows["downloads"]` |
| `net_flows`, capture/netflows.py, `FlowTracker(game_pids)` | `server` `{ip, port, proto, rx_packets, tx_packets, rx_bytes, tx_bytes, tick_ms}` or None; `talkers` `[{pid, name, rx_bytes, tx_bytes}]` top 8; `gaps` `[[t, ms, kind], …]` (kind: "delay" burst after the gap / "loss" no burst / "local" our own tx paused too); `server_rx` `[[t, pps], …]` at 1 Hz; `layout` "verified"/"unverified"; `health` → `net_flows[…]` |

`others` rows are `[t, [[pid, name, cpu_pct, io_bps, gpu_pct], …]]`, top 8 non-game processes by CPU at that
tick, `cpu_pct` with 100 = one logical CPU, `gpu_pct` their busiest engine on any adapter.

### Collector specifics

**frames (PresentMon, PM 10).** Command (argv list, never a shell; PID validated `0 < pid <= 0xFFFFFFFF`,
because `0` or junk means *every process*):
`--process_id <PID> --output_stdout --no_console_stats --qpc_time --track_frame_type --track_pc_latency
--track_hybrid_present --session_name lagdoctor --stop_existing_session --terminate_on_proc_exit`.
- Parse by header name, never position; decode per PM 10 *Parser rules* (latin-1 on a pipe; BOM-sniff
  otherwise); rebuild the column map on every header line; count malformed and rejoined rows.
- Map: `TimeInQPC`→`t`, `MsBetweenPresents`→`frame_ms`, `MsCPUBusy`→`cpu_busy_ms`, `MsCPUWait`→`cpu_wait_ms`,
  `MsGPULatency`, `MsGPUBusy`, `MsGPUWait`, `MsBetweenDisplayChange`→`display_ms`,
  `MsCPUBusy + MsUntilDisplayed`→`display_latency_ms`, `MsAllInputToPhotonLatency`→`input_latency_ms`,
  `MsAnimationError`→`anim_error_ms`, `PresentMode`, `PresentRuntime`, `SyncInterval`, `AllowsTearing`,
  `SwapChainAddress` (as `"0x…"` text), `FrameType`, `HybridPresent`. `displayed` = 0 when `MsUntilDisplayed`
  is NA, else 1. Store `None` for NA, and for the zeros PresentMon writes in place of missing values
  (`MsCPUBusy`, `MsBetweenAppStart`; `MsGPUBusy` and all CPU columns on frames whose `FrameType` is not
  `Application`).
- Stop (PM 7.4): `CTRL_BREAK_EVENT` to the `CREATE_NEW_PROCESS_GROUP` child when we share a console; else, or
  if it has not exited in 10 s, a second PresentMon `--terminate_existing_session --session_name lagdoctor`
  (exit 0 stopped, 7 none); only then `kill()` plus the same cleanup. Never `terminate()` first: it leaks the
  ETW session. Keep draining both pipes until EOF.
- Start failure: exit code 6 is ambiguous (PM verify log); classify by stderr text ("access denied" →
  `unavailable` "needs admin rights or Performance Log Users"; "already exists" → retry once after
  `--terminate_existing_session`).
- Watch the game ourselves (`target.is_alive`), because `--terminate_on_proc_exit` cannot fire if anti-cheat
  strips PresentMon's handle (PM 8.1).
- `etl_file=` runs `--etl_file <path>` instead of `--process_id` (no rights needed): used by tests against
  the gold captures in PresentMon's repo.

**system (WT 11).** One sampler thread at 4 Hz:
- One `PdhQuery` with exactly the WT 11.1 counter list. Never `% Processor Time`, `% Idle Time`, `\Process(*)`,
  `\Thread(*)`, `\System\*` or `\Network Interface(*)` (WT 11.3).
- One `Snapshotter.snapshot(threads_for={game pid})` per tick. Per-process and per-thread CPU are the delta
  over the last tick (about 250 ms), stored sharp so a 150 ms hitch shows in the tick that holds it; clamp each
  thread to 100; key by (id, CreateTime). Thread times move in 15.625 ms steps (WT 3.5), so the analysis judges
  saturation over ≥ 1 s (the mean of 4 ticks), never from one tick.
- The `game_*` columns are the target process only. Every other process, the game's own child processes
  included, counts as "other" (a child that burns CPU is still competing with the game's render thread).
- GPU Engine: keep per-sample values unclamped, average over the trailing 1 s, clamp only the average
  (single samples reached 3,652 %, WT verify log). Game GPU % = the game's busiest engine type on its adapter.
- `cpu_total_pct` / `cpu_max_core_pct` from `% User Time + % Privileged Time` per CPU over 1 s;
  `cpu_perf_pct` from `% Processor Performance`, `cpu_perf_limit_pct` from `% Performance Limit` (_Total);
  `thermal_limit_pct` = lowest `% Passive Limit`; `dpc_pct` / `interrupt_pct` = busiest CPU.
- `game_focused` from `windows.foreground_pid() == game pid`.
- `game_pids` (reported for the network tracker) = the target plus any process whose `ppid` chain leads to
  it, `create_time` checked so a reused PID never joins.

**gpu (WT 4.4).** NVML via ctypes at 1 Hz, started only when the game's adapter is the NVIDIA GPU or still
unknown (do not wake a sleeping dGPU, WT 11.5.7). `nv_throttle_pct` = Δ(cumulative throttle ns of the thermal
and power reasons) / Δt; `nv_reasons` names the active reason bits. Fallback when NVML is unavailable:
`nvidia-smi --query-gpu=… -lms 1000` streaming (never one-shot calls); `status` says which ran.
`nvmlDeviceGetDisplayActive` stays out of the loop.

**network (NE 1–3, 8).** One row per 0.5 s probe round: the gateway, the internet anchor (`1.1.1.1`,
configurable) and the game server (once `net_flows` reports one, or `--server`) are each pinged once per
round on their own worker (blocking `IcmpSendEcho2`, so use enough workers that a lost probe never delays the
next round). RTT `None` = no reply within 1000 ms; `probes`/`lost` count them, `from_t` says when probing a
target began, so "not probed" never reads as "lost". Wi-Fi quality, retries and channel and the NIC octets
(`GetIfEntry2`) are read every round; `best_route` every 5 s (a VPN egress is reported); ACM
`scan_complete` notifications are timestamped into `wifi_scans`. Steam `logs\content_log.txt` is tailed
for "Current download rate" lines. Location-gated calls are never made; BSSID stays `None` unless the
OS already allows it.

**net_flows (NE 4, 5).** One real-time session `lag-doctor-net` with Microsoft-Windows-Kernel-Network
(IPv4 + IPv6 keywords, level 4); orphan stopped by name at every start and in `finally`. Filter by the
payload PID (a PID scope filter does not work for kernel providers, NE 4.5), compute inter-arrival from
`EventHeader.TimeStamp` with a 2 s reorder buffer, classify the remote end against the local address set,
and pick the server flow and gaps per NE 4.6. Until an elevated self-test (one UDP DNS query, one TCP connect)
proves which field is the remote address, `layout` is "unverified" and the report says so.

## Inventory keys (capture/inventory.py)

`collect(target: dict | None) -> dict`. `None` means unknown. Rules and the report read exactly these keys.

```
os        {name, build}                                  e.g. {"name": "Windows 11 Home", "build": 26200}
cpu       {name, cores, threads, hybrid, p_threads, e_threads}   GetSystemCpuSetInformation EfficiencyClass
ram_gb    float
laptop    bool                                           a battery is present
adapters  [ … winapi.dxgi.list_adapters() … ]            software adapters dropped
displays  [ … winapi.display.list_displays() … ]
power     {scheme_guid, scheme_name, schemes: [[guid, name], …], slider, effective_mode, on_battery,
           battery_pct, saver}                           slider = slider_mode() for the current power source
settings  {game_mode, game_dvr, background_recording, hags, windowed_optimizations, vrr, memory_integrity}
          bools or None; hags = for the game's adapter (else any discrete adapter)
game      {exe, drive, media, free_gb, gpu_preference, monitor_device, launcher}
          gpu_preference: 0 let Windows decide, 1 power saving, 2 high performance, None not set;
          launcher: "steam"/"epic"/"other"/None from the exe path
oem       {tools: [names of running OEM utilities, e.g. "ArmouryCrate.Service.exe"]}
network   {name, kind, band, channel}                    filled by the session from the network collector
nvidia    {name, clock_max_mhz, mem_total_mb, temp_target_c}   filled by the session from the gpu collector
```

## Target selection (capture/target.py)

```python
def find_target(name: str | None = None, pid: int | None = None, auto: bool = False,
                sample_s: float = 2.0) -> dict
# {pid, create_time, name, exe, window_title, monitor_device, adapter_luid, how: "pid"|"name"|"auto",
#  candidates: [{pid, name, gpu_pct, has_window}]}; raises TargetNotFound with the reason
def is_alive(pid: int, create_time: int) -> bool        # NtQuerySystemInformation snapshot, no handle
def game_pids(pid: int, create_time: int) -> set[int]   # the target and its descendants
```
- by `pid`; by exe `name` (case-insensitive; several matches → the one with the most 3D-engine use);
- `auto`: highest 3D-engine use over a `sample_s` PDH sample among processes that own a visible,
  uncloaked top-level window and are not on the ignore list (dwm, explorer, browsers, steamwebhelper,
  Discord, OBS, the tool itself, …). A busy 3D engine alone is not a game: CUDA compute shows as
  `engtype_3D` (WT verify log). Ties go to the foreground window's process.

## Analysis

All pure functions over a `Capture`; thresholds from LC 2–5, NE 2 and 4.6 (they are starting points
tagged *inferred* there and are tuned against LagLab, never silently).

```python
# analysis/frames.py
def main_chain(cap) -> dict[str, list]              # F columns of the swap chain with the most displayed frames
def frame_stats(fr, duration_s) -> dict             # the frame keys of Diagnosis.stats
def bound(cap, fr, events) -> dict                  # LC 3.4, steady windows only (event frames excluded):
# {verdict: "gpu"|"cpu_main"|"cpu_all"|"waiting"|"vsync"|"capped"|"mixed"|"unknown", r, ft_ms, gpu_ms,
#  busiest_thread, cap_fps, refresh_hz, vsync: bool|None, vrr: bool|None, confidence, notes: [str]}
# analysis/events.py
def find_events(cap, fr, stats) -> list[LagEvent]
# analysis/attribute.py
def attribute(cap, fr, events, bound) -> dict       # fills ev.causes (best first) and ev.detail["why"];
                                                    # returns {cause: {a, n, share, lift, label}} (LC 5.2)
# analysis/rules.py
@dataclass
class Rule:  cause: str; family: str; needs: tuple[str, ...]; gate: str | None; fn: Callable
RULES: list[Rule]
def run_rules(cap, ctx: dict) -> tuple[list[Finding], dict[str, str], dict[str, str]]  # findings, cleared, unchecked
# analysis/diagnose.py
def diagnose(cap) -> Diagnosis
```
`ctx` = `{"frames": main chain, "stats", "bound", "events", "event_causes": attribute()'s summary}`.

### Frames and stats
- Series for stutter detection: `display_ms` over displayed frames (fallback `frame_ms` when display
  tracking is missing), LC 2.4. Bound and pacing use app frames only (`frame_type` "Application" or None).
- `low1_fps` / `low01_fps` = FPS of the average of exactly `ceil(p·n)` slowest frames (never "all frames ≥ the
  quantile", which sweeps in ties, LC 2.5); `p1_fps` = 1000 / P99 frame time, reported separately and labelled.
  Below 1,000 frames the 1% figures are `None` ("not enough frames"), below 10,000 the 0.1% figure (LC 2.6).

`Diagnosis.stats` keys (missing or None = not measured; the report shows "-"):

```
frames, duration_s                         int, float      frames analysed (main chain), capture seconds
avg_fps, low1_fps, low01_fps, p1_fps       float
median_frame_ms, p99_frame_ms, worst_frame_ms   float
stutters, stutters_per_min                 int, float      stutter events (any class)
hitches, severe_hitches, freezes           int             frames >= 50 / 150 / 500 ms (LC 2.4 classes)
time_beyond_50_ms                          float           sum of (D - 50) over D > 50
hitch_rate_bound                           float | None    3/n when zero hitches in n > 30 frames (rule of three)
dropped_pct                                float           % of frames never displayed
bound, bound_r, gpu_busy_pct               str, float, float   verdict; r; 100 x median GPU busy / median frame time
refresh_hz, present_mode, runtime          float, str, str     the game's display; most common mode and runtime
swap_chains, main_chain                    int, str        chains seen; the address the analysis used (the
                                                           HTML frame chart draws only that chain)
ping_gw_ms, ping_inet_ms, ping_server_ms   float           medians
jitter_ms, loss_pct                        float           internet anchor: mean consecutive difference; lost / sent
```

### Lag events (LC 2.4; NE 2.2, 4.6)
- **stutter**: `D ≥ 2.5 × M` and `D − M ≥ 8 ms`, where `M` = median of the previous
  `clamp(round(10·√median_ms), 20, 120)` frames; events closer than 250 ms merge. `severity` = worst frame ms;
  `detail` = `{worst_t, worst_ms, baseline_ms, frames, class: "stutter"|"hitch"|"severe"|"freeze"}`
  (hitch ≥ 50 ms and ≥ 2 M, severe ≥ 150 ms, freeze ≥ 500 ms). Also a stall: a jump in `t` between consecutive
  rows of the chain, which PresentMon can leave when several chains exist (PM 10 rule 7).
  The event spans `[t_first − frame_ms_first/1000, t_last]` of its frames.
- **fps_drop**: 1 s rolling displayed FPS below 80 % of the target for ≥ 2 s; target = the cap if one is
  detected, else the refresh rate if VSync-locked, else the run's p90 FPS. `severity` = % below target.
- **pacing**: over 2 s, more than 10 % of `|D_i − D_(i−1)| ≥ 4 ms`, or p95 `|anim_error_ms|` > 2 ms.
  `severity` = median swing ms.
- **net_spike**: internet (or server) RTT ≥ 30 ms above its rolling 120 s minimum and ≥ 2× the median;
  **net_loss**: a lost probe (any 15 s window ≥ 1 %, or a burst); **net_gap**: each `net_flows` gap ≥ 100 ms.
- **mark**: each hotkey press; its window is `[mark − 3 s, mark + 0.5 s]` (LC 2.4).

### Attribution (LC 5.2)
For each stutter/fps_drop event: the worst frame's split (CPU busy vs GPU busy vs waiting) says which side was
slow; then each 4 Hz signal in `[t_start − 0.5 s, t_end + 0.25 s]` is "active" when it clears both a robust
z-score against its baseline (median/MAD of the windows with no event within 2 s) and an absolute floor.
Lift test: a cause *supports* when `a ≥ 3`, `a/n ≥ 0.30` and `lift ≥ 2` (n ≥ 5 events). Each event is then
attributed to its single best cause (explaining away), first in `ev.causes`. Cause ids and signals:

| side | signal that moved | cause id |
|---|---|---|
| CPU | game's busiest thread up | `game_cpu_spike` (the game's own work: loading, shader compiles, logic) |
| CPU | game I/O reads up (disk busy or read latency up) | `asset_streaming` |
| CPU | hard page faults up, RAM low | `memory_paging` |
| CPU | another process's CPU up | `background_cpu` (`detail["process"]` names it) |
| any | another process's disk or GPU up | `background_disk` / `background_gpu` |
| any | DPC/interrupt time up on the busiest CPU | `driver_dpc` (capped at "possible": PDH cannot name the driver) |
| CPU | `cpu_perf_pct` or `cpu_perf_limit_pct` down | `cpu_throttle` |
| GPU | GPU busy long and `nv_throttle_pct` up | `gpu_throttle` |
| GPU | VRAM near full and `game_shared_mb` rising | `vram_overflow` |
| GPU | GPU busy long, nothing else moved | `gpu_spike` |
| none | game not focused (`game_focused` = 0) | `not_focused` |
| none | nothing moved, thread idle, GPU idle | `game_stall` (the game waited on itself) |

### Session rules (analysis/rules.py, LC 4, NE 7)
Each rule is a pure function `fn(cap, ctx) -> Finding | Cleared | Unchecked`, declares the sources it `needs`
and, for side-specific causes, a `gate` on the bound verdict (GPU-side causes need "gpu" or "mixed";
CPU-side need "cpu_main", "cpu_all", "mixed" or "unknown"; LC 5.1). A rule whose source was not measured is
`unchecked` with the reason. Rules: `wrong_gpu`, `hybrid_copy`, `gpu_limit`, `cpu_limit`, `gpu_throttle`,
`cpu_throttle`, `vram_overflow`, `ram_pressure`, `power_plan`, `refresh_rate`, `vsync_judder`, `uneven_pacing`,
`frame_cap`, `windowed_composition`, `game_dvr`, `game_mode_off`, `not_focused`, `background_hogs`,
`hdd_streaming`, `shader_compile` (first-run stutter that a second run does not repeat: advice only),
and the network rules `wifi_weak`, `wifi_roaming`, `wifi_scan_spikes`, `bufferbloat`, `isp_latency`,
`server_lag`, `packet_loss`, `vpn_route`, `bandwidth_hog`.
- `wrong_gpu`: the game rendered (by its 3D engine's LUID) on an integrated adapter while a discrete one
  exists.
- `hybrid_copy`: the game rendered on the discrete adapter but its window is on a display driven by the
  integrated one (`HybridPresent = 1` is strong evidence, 0 is not evidence).
- `uneven_pacing`: pacing events cover ≥ 10 % of the capture with no single cause.
- `background_hogs`: non-game processes summing ≥ 25 % of one CPU (or any VideoEncode/disk hog) across the
  stutter or fps_drop events, named with their numbers.

### Scoring (LC 5)
- Evidence families: frame timing, GPU telemetry, GPU engine/memory, CPU threads, CPU frequency/limits,
  memory, disk, interrupts, background processes, static configuration, network path, Wi-Fi. Per family only
  the strongest supporting and the strongest contradicting item count. Weights: 1 static setting, 2 steady
  metric past threshold, 3 event-linked with lift ≥ 4, 4 direct readout (a throttle counter, `FrameType`).
  Static configuration adds at most +2 and never makes a cause likely on its own.
- Labels → `Finding.confidence`: `high` = likely (S ≥ 4, key family measured and supporting), `medium` =
  possible (2 ≤ S < 4), `low` = worth checking. "Checked and fine" (S ≤ −2 with coverage) → `Diagnosis.cleared`;
  "not enough data" or not measured → `Diagnosis.unchecked`.
- Rank by label, then S, then explained share of events, then whether an auto-fix exists. Lag type = the
  category with the most lag time; the headline names the top finding in one sentence. With zero hitches,
  say "no hitches ≥ 50 ms in N frames (at most 3/N at 95 %)".

## Fixes (fixes/)

`Fix` subclasses implement `target`, `read`, `write`, `wanted` (and `readback`/`describe` when the effective
state is read differently from the written one) on top of `fixes/base.py`: plan → journal first → write →
read back → `applied`, or `failed` with the original restored; `undo` restores the journaled value and reads
it back; `heal` resolves `pending` entries left by a crash. Backends are injectable so tests never touch real
settings (`reg_sandbox` for the registry, fakes for power/display/process).

| fix id | params | what it changes | readback | needs restart |
|---|---|---|---|---|
| `power_plan` | `{"scheme": guid}` | active power scheme (`PowerSetActiveScheme`, allowed for standard users, WT 7.5) | `active_scheme()` | no |
| `refresh_rate` | `{"device": "\\.\DISPLAY1", "hz": int}` | display refresh at the current resolution: `CDS_TEST` first, then `CDS_UPDATEREGISTRY` | `current_mode()` and `list_displays()` hz | no |
| `gpu_preference` | `{"exe": path, "preference": 2}` | `HKCU\Software\Microsoft\DirectX\UserGpuPreferences\<exe>` field `GpuPreference=N;`, other fields kept | registry, then the game's LUID next run | game |
| `game_dvr_off` | `{}` | `HKCU\System\GameConfigStore\GameDVR_Enabled` = 0 and `HKCU\…\GameDVR\HistoricalCaptureEnabled` = 0 | registry | game |
| `game_mode_on` | `{}` | `HKCU\Software\Microsoft\GameBar\AutoGameModeEnabled` = 1 | registry; `effective_mode()` = game_mode while playing | no |
| `windowed_optimizations_on` | `{}` | `DirectXUserGlobalSettings` field `SwapEffectUpgradeEnable=1;`, other fields kept | registry | game |
| `background_priority` | `{"pid": int, "create_time": int, "class": "below_normal"}` | priority class of one of the user's own non-game processes, for this session only | `priority_class()` | no |

- Registry value semantics marked *unverified* in WT 8 are community-sourced; each such fix says so in its
  description.
- `background_priority` refuses the game and its children, services, protected processes and other users'
  processes; undo restores the journaled class if the same (pid, create_time) still runs, else the entry is
  `gone`.
- Advice only (never automated): hardware GPU scheduling, Memory Integrity / VBS (information only), the MUX
  / "Ultimate" GPU mode and the OEM fan/power mode (Armoury Crate), drivers, in-game settings, frame caps,
  cooling, Wi-Fi band and placement, pausing downloads, closing apps, the Wi-Fi background-scan setting.
- Never offered: disabling security features, killing or suspending processes, HKLM or policy changes,
  anything that touches the game process (including its priority: that needs a handle, WT 9.2).

## Report

- **Console**: the headline, key stats, up to three findings with their top evidence lines and fixes, then
  "checked and fine" and "not measured / could not check". Fits one terminal screen; a fix command is never
  wrapped.
- **HTML** (`report/html.py`): one self-contained file (inline CSS + SVG, no script, no URL), light/dark via
  `prefers-color-scheme`: verdict, stat tiles, frame-time chart with stutters and marks, CPU/GPU chart, VRAM
  chart, ping chart, finding cards, checked-and-fine, system snapshot, not measured.

## CLI

```
lagdoctor watch [--game NAME | --pid PID | --auto] [--duration S] [--server IP] [--no-network]
                [--elevate | --no-elevate] [--out DIR] [--hotkey KEY]
lagdoctor analyze CAPTURE            re-run the analysis on a saved capture
lagdoctor check                      settings check-up without a game running
lagdoctor fix [ID ...] [--list]      apply fixes suggested by the last report (asks first)
lagdoctor undo [ID | --all]          restore from the journal
lagdoctor setup                      fetch + verify PresentMon, explain how to get ETW rights
```

- Each `watch` writes `capture.ldcap.json.gz`, `diagnosis.json` and `report.html` into a new folder under
  `paths.sessions_dir()` and remembers it in `last-session.txt`; `fix` reads that diagnosis.
- The elevated child is `watch --pid PID --capture-only --out-file FILE` (hidden flags): it records into the
  file the parent chose and exits; it never analyses, fixes or touches the journal.
- `fix` applies only fixes the last report suggested, with the params it suggested, after showing the plan;
  it refuses to run elevated. Every command except `setup` first settles journal entries a crash left
  `pending`, and marks session-only changes whose process has ended as undone.

## Testing

- **Unit**: pure functions on literal sample points; parsers against fixture text taken from real tool output
  (PresentMon CSV from replays of the gold ETL captures in PresentMon's repo); rules on synthetic captures
  built through `SeriesBuilder`.
- **Live, read-only**: ctypes wrappers run against this machine and are compared with an independent readout
  (psutil on our own process, typeperf, `powercfg`, `nvidia-smi`, `Get-NetRoute`).
- **ETW without rights**: a private in-process session (allowed for standard users) proves the consumer
  path (NE 5.6); PresentMon's `--etl_file` replay proves the frames path.
- **Red first**: every guard is sabotaged once and must turn its test red (`scripts/redfirst.py`).
- **No handles**: `tests/test_no_handles.py` fails if `psutil.Process`, `process_iter` or `pid_exists` is
  used outside the allow-list, and if `OpenProcess` appears outside `winapi/process.py`.
- **End to end**: `scripts/e2e.py` runs each LagLab mode with a real capture and judges the diagnosis against
  LagLab's ground-truth log (`scripts/e2e_truth.py`). It needs ETW rights, so it runs on demand: without them
  it relaunches itself once through a UAC prompt and prints the verdicts back in the original window. The
  `smooth` run also exercises the real ping and Kernel-Network ETW paths.
