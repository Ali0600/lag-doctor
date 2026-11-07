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
   performance counters (PDH), the system-wide `NtQuerySystemInformation` snapshot, NVML. Nothing is
   injected or hooked for anti-cheat to object to, though no tool can promise what a given anti-cheat accepts.
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

| Needs | Works unelevated | Needs ETW rights |
|---|---|---|
| Capture | system sampler, NVML, pings, Wi-Fi, inventory, hotkey | PresentMon frame timing: an elevated token or membership of Performance Log Users (exit 6 "access denied" otherwise, PM 6.2); ETW network flows: an elevated token only (E1) |
| Fixes | everything in the catalogue (HKCU, power scheme, display mode, priority of the user's own processes) | nothing: fixes never run elevated |

- E1 (`scripts/etw_access.py`, measured 2026-11-06 on build 26200; `docs/research/install-and-rights.md`):
  Microsoft-Windows-Kernel-Network and Kernel-Process have descriptors of their own that let only SYSTEM,
  LocalService and Administrators enable them. Every other provider PresentMon needs falls under the default
  descriptor, which Performance Log Users may use. So a member records frame timing unelevated (without process
  start and stop events, which PresentMon tolerates losing) but not the game-server tracker.

- `winapi/elevation.can_trace()` = `CheckTokenMembership(Administrators S-1-5-32-544)` OR
  `CheckTokenMembership(Performance Log Users S-1-5-32-559)`; both count enabled SIDs only, so a UAC-filtered
  admin token answers False (PM 6.3, verify log).
- `lagdoctor watch` without ETW rights asks: relaunch the capture elevated (one UAC prompt), or continue
  without frame timing (frames reported *not measured*). `--elevate` / `--no-elevate` answer it up front. A
  member of Performance Log Users is not asked (frame timing works unelevated), but an explicit `--elevate`
  still elevates them, for the game-server tracker; a member who declines records frame timing here. An
  administrator window is never elevated again. Without rights, the tracker's not-measured reason names what
  helps: `--elevate`, and for a member, that the group cannot start it (`netflows._refused_why`).
  The elevated child runs **only the capture** in its own console window, writes the capture file and
  exits; the unelevated parent waits, loads it, analyses, reports and offers fixes. So fixes and the journal
  stay in the user's own context.
- The child is `python -E -s -m lagdoctor watch --pid P --capture-only --create-time T --out-file F
  --presentmon-exe X --inventory-file I --parent-sid S --parent-procs pid:ct,... (--steam-log L | --no-steam-log)`
  (hidden flags; every path absolute; `launch_elevated` refuses any empty argument or control character).
  An administrator approving the prompt for a standard user runs the child as *that* account, with its own
  profile and HKCU, so the parent hands over what it read in the user's own context: the PresentMon it
  verified (the child verifies it again), the inventory (read before the prompt), the user's Steam log (or that
  there is none) and its SID (the child notes when its token user differs), plus its own process identity for
  `target["tool_pids"]`. The child refuses to record a process whose create time differs (the game exited during
  the prompt), and the parent refuses to analyse a capture of another process.
- `-E -s` keep the user's PYTHON* variables and user site-packages out of the elevated interpreter. It still
  runs the checkout's own code, which the user can write: UAC shows the signed Python launcher, not the code
  it runs, and is not a security boundary for same-user code. Elevating only admin-writable code was taken up
  as the installer and capture service (DECISIONS 2026-11-06, `docs/INSTALLER-PLAN.md`).
- One recording at a time: whichever process records holds the named mutex `Local\lag-doctor-capture`
  (`session.CaptureLock`); a second `watch` refuses before starting PresentMon or any ETW session. The parent
  releases the lock just before the UAC launch, the child takes it, and the parent takes it back if the
  prompt is declined.
- `lagdoctor setup` explains the alternative: join Performance Log Users once (an admin command the user
  runs, then sign out and in), after which frame timing needs no prompt. The tool never changes group
  membership.
  - The printed `Add-LocalGroupMember` command names the account by its SID: it runs in an administrator's
    window, which may belong to another account. Only a well-formed SID goes in; without one the command
    names the account when the name is printable ASCII, else it holds a placeholder and says what to put
    there (`cli.say` prints other characters as `?`).
  - It prints one of four texts, picked by `elevation.trace_rights()`, so an account that already joined is
    never told to join again: `after_sign_in` (it joined after this sign-in began) asks for one sign-out;
    `filtered` (the group is in this token but not enabled, e.g. deny-only, which a sign-out does not change)
    says each recording asks for one UAC prompt.
  - The group covers frame timing only (E1, above): setup says so in the join steps, and tells a member
    (`now` without elevation) and a pending member (`after_sign_in`) that the game-server tracker still needs
    one UAC prompt, through `--elevate` (`cli.TRACKER_NOT_IN_GROUP`).
- **Frame timing is never silently dropped.** If PresentMon is missing or fails its check, `watch` stops before
  it looks for the game (exit 6) unless `--no-elevate` asked for a recording without it; a recording without
  frame timing says so before "Recording.", and the elevated child, opened only for frame timing, records
  nothing when its PresentMon fails its check (exit 6). A 14-minute recording of a real game was once made
  with every frame missing and the reason only in the report afterwards.

### Where files go: another app's private copy

A program started by a packaged (MSIX) app can have what it writes under `%LOCALAPPDATA%` and `%APPDATA%` kept in
that app's private folder, `%LOCALAPPDATA%\Packages\<app>\LocalCache\...`: it and its own children see the files
at the usual path, the user's other windows and an elevated window never do. Measured on 2026-11-05: a shell the
Claude desktop app started has no package identity (`GetCurrentPackageFullName` answers
APPMODEL_ERROR_NO_PACKAGE), yet its new files in `%LOCALAPPDATA%` landed in
`...\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local`; `%TEMP%` and the user's home were not moved. PresentMon set
up from there was "not installed" for the user's terminal and the elevated recording window.

- `paths.private_copy(folder)` asks Windows where a new file really lands (`winapi/storage.new_file_path`: a
  hidden, delete-on-close probe and its `GetFinalPathNameByHandleW`, in drive-letter form, else NT or volume-less
  form, which RAM disks without a Mount Manager entry still give), never whether the process has a package
  identity; `paths.redirected()` is the pure reading of that path.
- `cli.main` runs it once, before anything is written (the journal healing writes), per
  `cli.PRIVATE_COPY_POLICY`: `setup`, `watch`, `fix` and `undo` save files other windows must find and refuse
  (exit 5, saying where the files would go and to open PowerShell from the Start menu); `check` and `analyze`
  only read the PC and print a one-line note, and the journal is not healed under a private copy. A probe that
  cannot be made refuses the savers too. The elevated child probes the folder it saves the capture in (the
  nearest existing one), never a home of its own. When lag-doctor's own Python is the packaged app (a Microsoft
  Store Python: every window has the same private copy), the advice is another Python, not another window
  (`paths.private_copy_advice`).
- `scripts/e2e.py` makes the same check, and checks PresentMon, before its UAC prompt (exit 3).

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
scripts/    redfirst.py e2e_truth.py (done) e2e.py etw_access.py (E1: who may enable the trace providers)
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
def self_check(snapshotter=None) -> None          # compare our own process with GetProcessTimes /
                                                  # GetProcessIoCounters; raise if the layout drifted
# CreateTime is raw FILETIME ticks (100 ns since 1601 UTC), the number GetProcessTimes returns.

# winapi/windows.py (no process handles) ---------------------------------------------------------------
def top_windows() -> list[dict]   # visible, uncloaked top-level windows:
                                  # {hwnd, pid, title, cls, area, minimized, monitor_device}
                                  # (cls lets target.py skip console windows, which name their client's PID)
def foreground_pid() -> int | None
def window_monitor(hwnd: int) -> str | None       # "\\.\DISPLAY1" via MonitorFromWindow + GetMonitorInfoW

# winapi/dxgi.py (WT 5) ---------------------------------------------------------------------------------
def list_adapters() -> list[dict]
# {luid: "0xHHHHHHHH_0xLLLLLLLL" (lower-case, PDH's order: HighPart then LowPart), name, vendor:
#  "nvidia"|"amd"|"intel"|"microsoft"|"other", vendor_id, vram_mb, integrated: bool|None (DXCore),
#  software: bool, hags: bool|None (D3DKMT HwSchEnabled), hybrid: "discrete"|"integrated"|None,
#  pci: "BB:DD.F" upper-case hex or None (D3DKMT gives no domain; match NVML by its bus id's tail)}
# PDH spells LUIDs in upper case: compare through inventory.norm_luid.

# winapi/display.py (WT 6; LC 9) -------------------------------------------------------------------------
def list_displays() -> list[dict]
# {device: "\\.\DISPLAY1", name, adapter_luid, internal: bool|None, primary: bool, width, height,
#  hz: float (QueryDisplayConfig rational, e.g. 144.003), max_hz: int (best at this resolution),
#  modes_hz: list[int] (offered at this resolution)}
def current_mode(device: str) -> dict             # {width, height, hz: int|None} (None = "hardware default")
def stored_mode(device: str) -> dict | None       # the profile's mode (ENUM_REGISTRY_SETTINGS)
def monitor_path(device: str) -> str | None       # the monitor's stable identity (QDC monitorDevicePath)
def set_refresh(device: str, hz: int, test_only: bool) -> int   # DISP_CHANGE_* code; CDS_TEST or CDS_UPDATEREGISTRY

# winapi/power.py (WT 7) --------------------------------------------------------------------------------
def active_scheme() -> tuple[str, str]            # (guid, friendly name)
def list_schemes() -> list[tuple[str, str]]
def set_active_scheme(guid: str) -> None
def power_status() -> dict                        # {on_battery: bool|None, battery_pct: int|None, saver: bool|None}
def battery_present() -> bool | None              # inventory.laptop
def slider_mode(on_ac: bool) -> str | None        # "best_efficiency"|"balanced"|"best_performance"|None;
                                                  # None also on a plan without the slider (personality not Balanced)
def scheme_personality(scheme: str, on_ac: bool) -> str | None   # "balanced"|"high_performance"|"power_saver"|None:
                                                  # the base plan it is most like (Balanced-derived = "balanced"),
                                                  # its index looked up in Windows' list of possible values
def effective_mode(timeout_s: float = 1.0) -> str | None
# EFFECTIVE_POWER_MODE V2, hard-coded 0..6: "battery_saver","better_battery","balanced",
# "high_performance","max_performance","game_mode","mixed_reality"
def plan_settings(scheme: str) -> dict            # {max_cpu_ac/dc, max_cpu1_ac/dc (P-cores): 0..100 %,
                                                  #  boost_ac/dc: 0 Disabled .. 6, cooling_ac/dc: 0 Passive,
                                                  #  1 Active}; None = not readable

# winapi/storage.py (WT 9.5) ---------------------------------------------------------------------------
def media_type(path: str) -> str                  # "SSD" | "HDD" | "Unknown"  (seek penalty, access 0)
def free_gb(path: str) -> float | None
def new_file_path(folder: str) -> str             # "\\?\..." where a new file in folder really lands (probe,
                                                  # deleted on close); NT form on a volume without drive-letter
                                                  # paths; OSError if it cannot be made
def own_package_family() -> str | None            # this process's package family (a Store Python), else None

# winapi/nvml.py (WT 4.4, 4.5) -------------------------------------------------------------------------
class Nvml:
    @classmethod
    def open(cls) -> "Nvml | None"                # None when nvml.dll is missing or init fails (why: last_open_error)
    def devices(self) -> list[dict]               # {index, name, pci_bus_id, mem_total_mb, clock_max_mhz, temp_target_c}
    def sample(self, index: int) -> dict          # {util_pct, mem_used_mb, temp_c, power_w, power_limit_w,
                                                  #  clock_mhz, pstate, reasons: int, throttle_ns: {reason: cumulative ns}}
    def refresh_mark(self, index: int)            # ~10 us fingerprint of the values the driver refreshes together
    def close(self) -> None
# nvml.dll and nvidia-smi.exe load from GetSystemDirectoryW() by absolute path. Failures raise NvmlError
# (an OSError) carrying the NVML return code in .code; .winerror is None.

# winapi/icmp.py (NE 1) ---------------------------------------------------------------------------------
def ping(ip: str, timeout_ms: int = 1000, ttl: int = 128) -> tuple[int, float | None, str | None]
# (ICMP status, RTT in ms timed with perf_counter around the call, replying address); IPv4 and IPv6;
# success means status == 0, never just a non-zero return value; network outcomes come back as the status
# with RTT None; ValueError for an unspecified, multicast or broadcast target. Pinger reuses handles and
# buffers per worker; self_check(4|6) proves the reply layout on loopback.

# winapi/iphlp.py (NE 1.5) -----------------------------------------------------------------------------
def best_route(dest_ip: str) -> dict | None       # {if_index, next_hop, source}; GetBestRoute2
def if_row(if_index: int) -> dict                 # {in_octets, out_octets, type, description, oper_status, tunnel,
                                                  #  alias, guid, media_type, physical_medium, hardware,
                                                  #  tx_link_bps, rx_link_bps}
def default_gateway() -> str | None               # next hop toward 1.1.1.1; a tunnel route falls back to the
                                                  # physical /0 route

# winapi/wlan.py (NE 3) ---------------------------------------------------------------------------------
class Wlan:
    @classmethod
    def open(cls) -> "Wlan | None"                # why it failed: open_error
    def interfaces(self) -> list[dict]            # {guid, description, connected}
    def quality(self, guid: str) -> dict | None   # opcode 19: {rssi_dbm, signal_pct, rx_mbps, tx_mbps, freq_mhz,
                                                  #  channel, phy (a name such as "802.11ac")}
    def retries(self, guid: str) -> int | None    # driver statistics, cumulative
    def bssid(self, guid: str) -> str | None      # queried only when location consent already says Allow
    def on_scan_complete(self, callback) -> None  # ACM notification 7: callback(t, guid), t on the raw shared
                                                  # clock, called on a foreign thread
    def close(self) -> None

# winapi/etw.py (NE 5) ----------------------------------------------------------------------------------
def stop_session(name: str) -> int                # ControlTraceW STOP by name; 0 stopped, 4201 none
def query_session(name: str) -> int
class RealtimeSession:                            # controller + consumer thread, one named session
    def __init__(self, name, providers: list[tuple[str, int, int]], on_event, private: bool = False)
    def start(self) -> None                       # stops an orphan of the same name first
    def stop(self) -> None
    def health(self) -> dict                      # {events, lost, callback_errors, pump_alive, process_trace_rc, ...}
# on_event gets EtwEvent(timestamp, provider, id, version, opcode, level, task, keyword, process_id, thread_id,
# data); timestamps are raw QPC ticks (ClientContext 1 + PROCESS_TRACE_MODE_RAW_TIMESTAMP), on the capture
# clock. private=True (tests, self-tests) logs to a temp .etl and delivers everything inside stop(): private
# sessions cannot be real time and are invisible to QUERY/STOP by name. Provider wraps EventRegister/EventWrite.

# winapi/elevation.py (PM 6) ----------------------------------------------------------------------------
def is_elevated() -> bool
def in_performance_log_users() -> bool
def can_trace() -> bool
def token_user_sid() -> str
def group_member_sids(group_sid: str) -> list[str]   # SIDs of a local group's direct members (its translated name
                                                     # is looked up from the SID); OSError when Windows cannot say
def trace_rights() -> str                         # "now" this token may trace; "filtered" Performance Log Users is
                                                  # in this token but not enabled (deny-only); "after_sign_in" the
                                                  # account, or a group enabled in its token, joined after this
                                                  # sign-in began; "no"; "unknown" Windows could not say (the
                                                  # token, its groups or the group's members unreadable)
def launch_elevated(argv: list[str], cwd: str) -> ElevatedChild   # ShellExecuteExW "runas", new console;
                                                                  # PermissionError when declined (1223);
                                                                  # ValueError for a relative program or folder
class ElevatedChild:
    def wait(self, timeout_s: float | None = None) -> int | None  # exit code; a Ctrl+C leaves the child running
    def close(self) -> None
def relaunch_elevated(argv: list[str], cwd: str) -> int   # launch + wait + close (scripts/e2e.py)
def hold_named_mutex(name: str)                   # handle, or None when another process holds it
def release_named_mutex(handle) -> None

# winapi/process.py (WT 9) ------------------------------------------------------------------------------
# Every call takes an optional create_time; when given, identity is checked WITHOUT a handle (an
# NtQuerySystemInformation snapshot) before OpenProcess, and again on the handle (ProcessGone otherwise).
def priority_class(pid: int, *, create_time=None) -> int     # GetPriorityClass via PROCESS_QUERY_LIMITED_INFORMATION
def set_priority_class(pid: int, cls: int, *, create_time=None) -> None  # read back: PriorityNotApplied if it differs
def same_user(pid: int, *, create_time=None) -> bool          # token user SID equals ours
def is_service_or_protected(pid: int, *, create_time=None) -> bool   # a service PID, session 0, or access refused
def is_running(pid: int, create_time: int) -> bool            # handle-free
def running_name(pid: int, create_time: int) -> str | None    # handle-free image name
def creation_time(pid: int) -> int; user_sid(pid) -> str; session_id(pid) -> int; service_pids() -> dict[int, list[str]]
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
| `frames`, capture/presentmon.py, `PresentMonSource(pid, exe=None, etl_file=None, *, is_alive=None)` | `columns` (F, every swap chain of the target) → `frames`; `swap_chains` `{"0x…": {"frames", "displayed", "runtime"}}` (an unfiltered replay keys them `"<pid>:0x…"`), `main_chain` (most displayed frames), `presentmon` `{version, exe, args, exit_code, stop_path, rows, malformed, rejoined, headers, stderr_tail}` → `target["presentmon"]`; `live()` = `{frames, fps, malformed}` |
| `system`, capture/sysmon.py, `SystemSampler(target)` | `columns` (S) → `system`; `others` → `others`; `game_adapter_luid` (adapter whose 3D engine the game used most) → `target["adapter_luid"]`; `game_pids` (game + children seen) → `target["pids"]`; `cost` `{ticks, median_ms, max_ms, mean_ms}` (also in the status detail) |
| `gpu`, capture/gpumon.py, `NvidiaSampler(adapter)` | `columns` (G) → `gpu`; `device` `{name, clock_max_mhz, mem_total_mb, temp_target_c, power_default_w}` or None → `inventory["nvidia"]`; `live()` adds `source` ("nvml"/"nvidia-smi") |
| `network`, capture/netmon.py, `NetworkSampler(server_ip=None, *, steam_log=None)` | `columns` (N) → `network`; `targets` `{gateway, internet, server}` (IPs or None), `probes` `{target: {"sent", "lost", "from_t"}}`, `lost` `{target: [t, …]}`, `wifi_scans` `[t, …]`, `route` `{if_index, description, vpn: bool \| None, toward}` → `net_flows[…]` under the same keys; `interface` `{name, kind: "wifi"/"ethernet"/"other", band, channel}` → `inventory["network"]`; `downloads` `[{t, source, mbps}]` from the Steam log, or None when no download log was read, with `download_sources` (the logs read) → `net_flows[…]` |
| `net_flows`, capture/netflows.py, `FlowTracker(game_pids or a callable)` | `server` `{ip, port, proto, pid, rx_packets, tx_packets, rx_bytes, tx_bytes, tick_ms, first_rx_t, last_rx_t}` or None; `talkers` `[{pid, name, rx_bytes, tx_bytes}]` top 8; `gaps` `[[t, ms, kind], …]` (t = the last packet before the silence; kind: "delay" burst after the gap / "loss" no burst / "local" our own tx paused too / "open" the server went silent for good while we kept sending / "unknown" a late delivery hides which); `server_rx` `[[t, pps], …]` (t = end of each 1 s bin); talkers, gaps and server_rx are None when the session never ran; `layout` "verified"/"unverified"; `health` → `net_flows[…]` |

`others` rows are `[t, [[pid, name, cpu_pct, io_bps, gpu_pct, create_time, gpu_game_pct], …]]`: up to 8
non-game processes that did something that tick, ranked by CPU, `cpu_pct` with 100 = one logical CPU, `io_bps`
read + write bytes, `gpu_pct` their busiest engine on any adapter, `create_time` raw FILETIME ticks (so a fix can
prove a PID was not reused), `gpu_game_pct` their busiest engine on the game's adapter (None while that adapter
is unknown). lag-doctor's own process is included: its load is real, and `target["tool_pids"]` (written by the
session from handle-free snapshots: this process, its venv launcher, its PresentMon and nvidia-smi children, and
in the elevated flow the waiting parent) lets the analysis keep it out of the blame.

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
  (`MsCPUBusy`, `MsCPUWait`, `MsBetweenAppStart`; `MsGPUBusy` and all CPU columns on frames whose `FrameType`
  is not `Application`).
- Only the pinned 2.6.0 bytes ever run: before every launch (the clean-up helper too) the exe's SHA-256 is
  checked through a handle that refuses writers, held until the process has started, and the launch uses that
  handle's final path (GetFinalPathNameByHandleW), so a folder junction re-pointed after the check cannot
  redirect it.
- Stop (PM 7.4): `CTRL_BREAK_EVENT` to the `CREATE_NEW_PROCESS_GROUP` child when we share a console; else, or
  if it has not exited in 10 s, a second PresentMon `--terminate_existing_session --session_name lagdoctor`
  (exit 0 stopped, 7 none); only then `kill()` plus the same cleanup. Never `terminate()` first: it leaks the
  ETW session. Keep draining both pipes until EOF.
- Start failure: exit code 6 is ambiguous (PM verify log); classify by stderr text ("access denied" →
  `unavailable` "needs admin rights or Performance Log Users"; "already running" / "error code 183" → retry
  once after `--terminate_existing_session`). A crash exit (outside 0–7) gets the same session clean-up as a
  kill.
- Watch the game ourselves (`target.is_alive`), because `--terminate_on_proc_exit` cannot fire if anti-cheat
  strips PresentMon's handle (PM 8.1). PresentMon ending by itself with exit 0 while the game still runs
  (its session was stopped from outside) makes the source `partial`, with the time frames stopped.
- `etl_file=` runs `--etl_file <path>` instead of `--process_id` (no rights needed): used by tests against
  the gold captures in PresentMon's repo.

**system (WT 11).** One sampler thread at 4 Hz (the first tick is a baseline, so rows start about 0.25 s in;
about 16 ms and 5–6 % of one CPU per tick here):
- One `PdhQuery` with the WT 11.1 counter list plus `\GPU Process Memory(*)\Shared Usage` and
  `\PhysicalDisk(*)\Current Disk Queue Length`, which S needs. Never `% Processor Time`, `% Idle Time`,
  `\Process(*)`, `\Thread(*)`, `\System\*` or `\Network Interface(*)` (WT 11.3). Items with CStatus NEW_DATA
  count as valid.
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
unknown (do not wake a sleeping dGPU, WT 11.5.7). NVML refreshes its values together every ~500 ms, so each row
is read just after a refresh (a rate between free-running reads is quantised). `nv_throttle_pct` =
Δ(cumulative sw_thermal + hw_thermal + hw_power_brake ns) / Δt, and `nv_power_cap_pct` the same for
sw_power_cap, which during GPU-bound play is the design limit, not throttling (LC 4.1, 4.2); a share over an
unaligned read is None. `nv_reasons` names the active reason bits; `nv_power_w` at or below 1 W is None.
Fallback when NVML is unavailable: `nvidia-smi --query-gpu=… -lms 1000` streaming (never one-shot calls), in a
kill-on-close job so it dies with lag-doctor; `status` says which ran. `nvmlDeviceGetDisplayActive` stays out
of the loop.

**network (NE 1–3, 8).** One row per 0.5 s probe round: the gateway, the internet anchor (`1.1.1.1`,
configurable) and the game server (once `net_flows` reports one, or `--server`) are each pinged once per
round on their own worker (blocking `IcmpSendEcho2`, so use enough workers that a lost probe never delays the
next round). RTT `None` = no reply within 1000 ms; `probes`/`lost` count them, `from_t` says when probing a
target began, so "not probed" never reads as "lost". Wi-Fi quality, retries and channel and the NIC octets
(`GetIfEntry2`) are read every round; `best_route` every 5 s (a VPN egress is reported); ACM
`scan_complete` notifications are timestamped into `wifi_scans` (None when the notifications could not be
registered, so "no scan" is never claimed unmeasured). Steam `logs\content_log.txt` is tailed
for "Current download rate" lines (each placed at its own time stamp); `steam_log=` overrides where that log
is (the elevated child gets the user's). Delivery Optimization is not read (it would mean a PowerShell spawn
during play). Location-gated calls are never made: the BSSID column holds per-session labels ("AP 1"), never
a MAC address, and only when the OS already allows location. `nic_rx_bps`/`nic_tx_bps` are bytes per second.

**net_flows (NE 4, 5).** One real-time session `lag-doctor-net` with Microsoft-Windows-Kernel-Network
(IPv4 + IPv6 keywords, level 4); orphan stopped by name at every start and in `finally`. Filter by the
payload PID (a PID scope filter does not work for kernel providers, NE 4.5), compute inter-arrival from
`EventHeader.TimeStamp` with a 2 s reorder buffer, classify the remote end against the local address set,
and pick the server flow and gaps per NE 4.6. Until an elevated self-test (one UDP and one TCP DNS query for
example.com to 1.1.1.1:53, sent once when the real session starts) proves which field is the remote address,
`layout` is "unverified" and the report says so.
- Only UDP flows of the game's PIDs are timed; TCP counts toward per-process bytes, retransmits to health.
- Gap kinds when several apply: local over delay over loss (local = our own send silence covers at least
  half of the receive gap). A packet delivered later than the reorder window cannot make a phantom gap: a gap
  whose span holds one is left out (and counted), and the status turns partial. A consumer that died before
  `stop()` keeps the final status partial, with when (`health.consumer_stopped_t`).
- At the end, a server whose last packet is older than its gap threshold while our own packets went on for at
  least 1 s more gets an "open" gap: the connection was lost. If our sending stopped too, the game left the
  match (no gap).

## The capture file (model.py)

`Capture.save` writes gzip-compressed JSON atomically (temp file + replace; a failed save leaves no temp
file). `Capture.load` checks the gzip magic, decompresses at most 2 GiB, and raises `CaptureError` (a
ValueError) with `.kind` = "foreign" (another schema family), "newer" (`lagdoctor.capture/N` above 1),
"damaged" or "too_large". Unknown keys from a newer build are dropped with a note in `capture.notes`, so an
older lag-doctor can still read a newer capture of the same format.

## Inventory keys (capture/inventory.py)

`collect(target=None, process_names=None, *, registry=None) -> dict`. `None` means unknown. Rules and the
report read exactly these keys, plus `errors` `{section: reason}` for each section that could not be read
(the session copies them into the capture's notes). `process_names` are the running image names from the
session's snapshot (for `oem.tools`; None without them). `registry` lets tests point every read at the sandbox.
`settings.game_mode` is True when AutoGameModeEnabled is absent (the Windows default, WT 8);
`background_recording` is None when HistoricalCaptureEnabled is absent.

```
os        {name, build}                                  e.g. {"name": "Windows 11 Home", "build": 26200}
cpu       {name, cores, threads, hybrid, p_threads, e_threads}   GetSystemCpuSetInformation EfficiencyClass
ram_gb    float
laptop    bool                                           a battery is present
adapters  [ … winapi.dxgi.list_adapters() … ]            software adapters dropped
displays  [ … winapi.display.list_displays() … ]
power     {scheme_guid, scheme_name, schemes: [[guid, name], …], slider, personality, effective_mode, on_battery,
           battery_pct, saver, plan}                     slider = slider_mode() for the current power source;
          personality = scheme_personality() of the active plan for that source: only "balanced" has the slider,
          so on the others slider is None and the report says the plan has no power mode (None = not read);
          plan = power.plan_settings() of the active plan: rules judge a plan by these, by its name only when
          a capture has none (an ASUS plan named "Silent" set the CPU exactly as Balanced does here)
settings  {game_mode, game_dvr, background_recording, hags, windowed_optimizations, vrr, memory_integrity}
          bools or None; hags = for the game's adapter (else any discrete adapter)
game      {exe, drive, media, free_gb, gpu_preference, monitor_device, launcher}
          gpu_preference: 0 let Windows decide, 1 power saving, 2 high performance, None not set;
          launcher: "steam"/"epic"/"other"/None from the exe path
oem       {tools: [names of running OEM utilities, e.g. "ArmouryCrate.Service.exe"]}
network   {name, kind, band, channel}                    filled by the session from the network collector
nvidia    {name, clock_max_mhz, mem_total_mb, temp_target_c, power_default_w}   from the gpu collector
          power_default_w: the GPU's default power limit (NVML), so a mode that lowered the enforced limit shows
```

## Target selection (capture/target.py)

```python
def find_target(name: str | None = None, pid: int | None = None, auto: bool = False,
                sample_s: float = 2.0) -> dict
# {pid, create_time, name, exe, window_title, monitor_device, adapter_luid, how: "pid"|"name"|"auto",
#  candidates: [{pid, name, gpu_pct, has_window}]}; raises TargetNotFound with the reason
def is_alive(pid: int, create_time: int) -> bool        # NtQuerySystemInformation snapshot, no handle;
                                                        # OSError when Windows cannot say (the session then
                                                        # keeps recording and asks again a second later)
def game_pids(pid: int, create_time: int) -> set[int]   # the target and its descendants
```
- by `pid`; by exe `name` (case-insensitive; several matches → the one with the most 3D-engine use); PID 4 and
  lag-doctor's own process are refused, and name/auto modes skip lag-doctor and its ancestors;
- `auto`: highest 3D-engine use over a `sample_s` PDH sample among processes that own a visible,
  uncloaked top-level window of non-zero area (console windows do not count: they name their client's PID)
  and are not on the ignore list (shell, terminals, browsers and web views, launchers, chat, capture and
  overlay tools, OEM utilities, the tool itself; python.exe stays allowed, LagLab is one). It needs at least
  2 % mean 3D use, and candidates within 1 point are tied (both inferred). A busy 3D engine alone is not a
  game: CUDA compute shows as `engtype_3D` (WT verify log). Ties go to the foreground window's process.

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
  tracking is missing; `stats["frames_source"]` says which), LC 2.4. Bound and pacing use app frames only
  (`frame_type` "Application" or None).
- A stall is a jump in `t` between consecutive rows of the chain that the row's own frame time covers less than
  half of (PresentMon can leave only such a gap when several chains exist, PM 10 rule 7). One helper,
  `frames.stall_gaps`, feeds both the events and the stats: each stall is one interval of the series behind
  `avg_fps`, the lows, the worst frame, the hitch/severe/freeze counts and `time_beyond_50_ms`.
- `low1_fps` / `low01_fps` = FPS of the average of exactly `ceil(p·n)` slowest frames (never "all frames ≥ the
  quantile", which sweeps in ties, LC 2.5); `p1_fps` = 1000 / P99 frame time, reported separately and labelled.
  Below 1,000 frames the 1% figures are `None` ("not enough frames"), below 10,000 the 0.1% figure (LC 2.6).
  `hitch_rate_bound` (rule of three, 3/n) exists only with zero hitches and no stall.
- Every value read from a capture is bounded (`frames._num_col`, `_bounded`: magnitudes past 1e7 in seconds, ms
  or % are missing), so a damaged capture degrades instead of producing infinite evidence.

`Diagnosis.stats` keys (missing or None = not measured; the report shows "-"):

```
frames, duration_s                         int, float      frames analysed (main chain), capture seconds
avg_fps, low1_fps, low01_fps, p1_fps       float
median_frame_ms, p99_frame_ms, worst_frame_ms   float
stutters, stutters_per_min                 int, float      stutter events (any class); None without frames
hitches, severe_hitches, freezes           int             frames >= 50 / 150 / 500 ms (LC 2.4 classes, nested)
time_beyond_50_ms                          float           sum of (D - 50) over D > 50
hitch_rate_bound                           float | None    3/n when zero hitches and no stall in n > 30 frames
dropped_pct                                float           % of frames never displayed
frames_source                              str             "display" or "present"
bound, bound_r, gpu_busy_pct               str, float, float   verdict; r; 100 x median GPU busy / median frame time
refresh_hz, present_mode, runtime          float, str, str     the game's display; most common mode and runtime
swap_chains, main_chain                    int, str        chains seen; the address the analysis used (the
                                                           HTML frame chart draws only that chain)
ping_gw_ms, ping_inet_ms, ping_server_ms   float           medians
jitter_ms, loss_pct                        float           internet anchor: mean consecutive difference; lost / sent
marks                                      list            [{t, events: [ids], causes: [<= 2]}], one per lag-key press
```

### The bound verdict (LC 3.4)
`bound()` judges steady stretches only (frames outside stutters and FPS drops, at least 10 s and 300 app
frames), in order: VSync (displayed intervals quantised on whole refresh periods, SyncInterval not 0 and
AllowsTearing not 1; with k = round(FT/P) >= 2 the hardware is tested against one period: GPU busy past
(k-1)·P gives `gpu`, a saturated busiest thread `cpu_main`, with a note that VSync rounds the rate down to
refresh/k), a frame cap (a regular present cadence: robust CoV, (IQR/1.349)/median per 5 s window, under 3 %,
away from k·P, with r < 0.75 and an unsaturated thread), `gpu` (r >= 0.90, or r above 0.75 with FT − G <= 1 ms;
G minus 0.5 ms when HAGS is on for the game's adapter), `cpu_main` (r <= 0.75 and busiest thread >= 0.85 of a
CPU over >= 1 s), `cpu_all`, `waiting`, else `mixed`. Without frames, a 3D engine at >= 95 % gives a probable
`gpu`. While other programs keep the game's GPU >= 10 % busy (`frames.gpu_shared`, LC 4.6), a `capped` or
`waiting` verdict becomes `unknown`: a shared GPU paces a game as evenly as a cap. `bound` also returns
`steady_fps`, the rate outside stutters and drops.

### Lag events (LC 2.4; NE 2.2, 4.6)
- **stutter**: `D ≥ 2.5 × M` and `D − M ≥ 8 ms`, where `M` = median of the previous
  `clamp(round(10·√median_ms), 20, 120)` frames. On a quantised display (refresh known, ≥ 90 % of D on whole
  periods) the excess is measured against the longest hold the regular cadence produces:
  `D − max(M, E) ≥ 8 ms` with `E = ceil(Mp/P − 0.005) × P`, Mp the trailing median MsBetweenPresents and P the
  refresh period (DECISIONS 2026-11-04; the 0.005 slack only absorbs measured jitter). Events closer than
  250 ms merge; stalls are stutters too. `severity` = worst frame ms; `detail` = `{worst_t, worst_ms,
  baseline_ms, frames, class: "stutter"|"hitch"|"severe"|"freeze", stall, source}` (hitch ≥ 50 ms and ≥ 2 M,
  severe ≥ 150 ms, freeze ≥ 500 ms). The span is `[t − max(frame_ms, D)/1000, t]` of its frames, never before
  the chain's first row.
- **fps_drop**: 1 s rolling displayed FPS below 80 % of the target for ≥ 2 s; target = the cap if one is
  detected, else the refresh rate (refresh/k) if VSync-locked, else the run's p90 FPS, and never above the
  refresh rate on a fixed-refresh display. `severity` = % below target; `detail` `{target_fps, target_from, fps,
  min_fps}`.
- **pacing**: over 2 s, more than 10 % of `|D_i − D_(i−1)| ≥ 4 ms`, or p95 `|anim_error_ms|` > 2 ms, with frames
  inside stutter and stall spans left out (a hitch must not create the pacing that outranks it).
  `detail["criterion"]` = "swing" / "anim" / "both"; `severity` = the median swing ≥ 4 ms, or the animation-error
  p95 when only that fired; `detail` also `{swing_pct, anim_p95_ms, present_swing_pct, quantised_pct, frames}`.
- **net_spike**: internet (or server) RTT ≥ 30 ms above its rolling 120 s minimum and ≥ 2× the median, on at
  least 2 of 3 consecutive probe rounds, or one reply ≥ 100 ms over the minimum (NE 2.2's levels are 15 s window
  figures; one slow ICMP reply is often just deprioritised, NE 2.4).
- **net_loss**: at least 2 lost probes and ≥ 1 % in a 15 s window; "burst" = at least 2 consecutive lost rounds.
- **net_gap**: each `net_flows` gap ≥ 100 ms, spanning `[t, t + ms]` (t = the last packet before the silence),
  with its kind.
- **mark**: each hotkey press; its window is `[mark − 3 s, mark + 0.5 s]` (LC 2.4); `detail` `{frame_events
  (stutter and fps_drop ids), pacing_events, net_events, visible_in_frames}`.

### Attribution (LC 5.2)
For each stutter/fps_drop event, the slowest *presented* frame in its span says which side was slow (CPU busy
vs GPU busy vs waiting). When no presented frame holds at least half of the lost time (a stall, or the screen
stopping while the game kept presenting), the side is None: the bound verdict never stands in for it, except
for a slow frame that simply has no GPU timing (`detail["side_source"]` = "verdict", which rules do not count
as frame-timing evidence). Each 4 Hz signal is then "active" when its busiest 1 s mean in
`[t_start − 0.5 s, t_end + 0.25 s]` clears both a robust z-score (≥ 3.5 against the median/MAD of quiet 1 s
windows with no event within 2 s; the guard relaxes to 1 s, then 0.5 s, when fewer than 8 quiet windows exist)
and an absolute floor. Lift test: a cause *supports* when `a ≥ 3`, `a/n ≥ 0.30` and `lift ≥ 2` (n ≥ 5 events).
Each event is then attributed to its single best cause (explaining away), first in `ev.causes`; when nothing
moved, the residual is `gpu_spike` (GPU side), `game_cpu_spike` (a saturated thread) or `game_stall`, and no
cause at all when the sampler did not measure that moment.

| side | signal that moved | cause id |
|---|---|---|
| CPU | game's busiest thread up | `game_cpu_spike` (the game's own work: loading, shader compiles, logic) |
| CPU | game I/O reads up (disk busy or read latency up) | `asset_streaming` |
| CPU | hard page faults up, RAM low | `memory_paging` |
| CPU | another process's CPU up (or machine-wide CPU beyond the game) | `background_cpu` |
| any | another process's disk or GPU up | `background_disk` / `background_gpu` |
| any | DPC/interrupt time up on the busiest CPU | `driver_dpc` (capped at "possible": PDH cannot name the driver) |
| CPU | `cpu_perf_pct` or `cpu_perf_limit_pct` down | `cpu_throttle` |
| GPU | GPU busy long and `nv_throttle_pct` up | `gpu_throttle` |
| GPU | VRAM near full and `game_shared_mb` rising | `vram_overflow` |
| GPU | GPU busy long, nothing else moved | `gpu_spike` |
| none | game not focused (`game_focused` = 0) | `not_focused` |
| none | nothing moved, thread idle | `game_stall` (the game waited on itself) |

Blame for the background causes names the processes whose own rise clears the cause's floor (CPU: 25 % of a
CPU; disk and GPU: the cause floors), ordered by rise (`detail["process_rises"]`), never by PID. Every process
has a role (`detail["process_role"]`): lag-doctor's own processes (`target["tool_pids"]`, PresentMon by its exe,
nvidia-smi) are never named; the game's own children (`target["pids"]`) are named as the game's helpers, never
with "pause or close" advice. `background_gpu` blames by each process's GPU on the game's adapter (others field 7)
when the capture has it.

### Session rules (analysis/rules.py, LC 4, NE 7)
Each rule is a pure function `fn(cap, ctx) -> Finding | Cleared | Unchecked | None` (None: measured, no
evidence either way, silent), declares the sources it `needs`, its family and category, and for side-specific
causes a `gate` on the bound verdict (`gpu`: "gpu" or "mixed"; `cpu`: "cpu_main", "cpu_all", "mixed" or
"unknown", LC 5.1; `gpu_event`: anything but CPU-bound). A gate failure turns a Finding into Cleared (a definite
verdict) or Unchecked (verdict unknown). A rule whose source was not measured is `unchecked` with the reason.
Some clearances are held open (`_holds_open`): `gpu_limit` while other programs share the game's GPU, and
`cpu_limit` while the busiest thread is in LC 3.2's grey zone (0.6–0.85 of a CPU); and pacing causes stay open
while pacing events that no finding claims cover ≥ 10 % of the capture.

The 36 rules: `wrong_gpu`, `hybrid_copy`, `gpu_limit`, `cpu_limit`, `gpu_contention`, `gpu_throttle`,
`cpu_throttle`, `vram_overflow`, `ram_pressure`, `power_plan`, `refresh_rate`, `vsync_judder`, `dropped_frames`,
`uneven_pacing`, `frame_cap`, `windowed_composition`, `game_dvr`, `game_mode_off`, `not_focused`,
`background_hogs`, `hdd_streaming`, `shader_compile`, the stutter causes `game_cpu_spike`, `asset_streaming`,
`driver_dpc`, `gpu_spike`, `game_stall` (from the lift test), and the network rules `wifi_weak`, `wifi_roaming`,
`wifi_scan_spikes`, `bufferbloat`, `isp_latency`, `server_lag`, `packet_loss`, `vpn_route`, `bandwidth_hog`.
- `wrong_gpu`: the game rendered (by its 3D engine's LUID) on an integrated adapter while a discrete one
  exists; likely when GPU-bound, possible otherwise.
- `hybrid_copy`: the game rendered on the discrete adapter but its window is on a display driven by the
  integrated one (`HybridPresent = 1` is strong evidence, 0 is not evidence).
- `gpu_contention`: other programs keep the game's GPU ≥ 10 % busy (median of steady 1 s windows); stronger when
  the game looks starved; names the top other GPU user on the game's adapter.
- `gpu_throttle`: `nv_throttle_pct` (heat and power brake only); the power cap is context, the power-limit
  variant only when the enforced limit is below `inventory.nvidia.power_default_w`. A capture from before the
  split (no `nv_power_cap_pct` column) counts the share only with a thermal or brake reason active. The text
  says "heat" when the readings named heat and never the brake or HW slowdown (`attribute.held_by`), else
  "heat or its power brake".
- `power_plan`: battery, battery saver, a low power mode, or a plan whose own settings (`inventory.power.plan`)
  cap the CPU below 100 % (the performance cores too, on a hybrid CPU), turn boost off, or cool passively while
  plugged in; by its name (whole words of `QUIET_PLAN_WORDS`) only in a capture without those settings. And the
  maker's quiet mode: Armoury Crate switches the Windows plan to one named for its mode (163 switches in this
  laptop's System log), so a quiet-named plan while Armoury Crate runs is that mode ("Armoury Crate's quiet mode is
  holding the laptop back"), with the GPU's power limit held below its default as evidence and Armoury's own
  setting as the advice. GPU heat is never its evidence (that is `gpu_throttle`'s); the switch to Balanced is
  offered only for a plan whose own settings hold the PC back. `gpu_throttle` takes the same problems as a hint,
  but never a CPU-only plan setting (a cap, boost off). The power mode (the slider) is cited and advised only where
  the plan has one: never on a High performance or Power saver type plan (`inventory.power.personality`;
  `labels.plan_has_power_mode`), whatever slider position the capture holds.
- `vsync_judder` (real VSync judder) and `dropped_frames` (the game presents faster than a fixed refresh and
  frames are dropped, `rules._dropped_judder`); `uneven_pacing`: pacing events with uneven *presents*
  (present_swing_pct > 10 %) covering ≥ 10 % of the capture, weighed as a steady metric.
- `game_dvr`: background recording known off concludes against; unknown is advice only (no auto-fix);
  `game_dvr_off` is offered only when background recording is known on.
- `background_hogs`: processes named with their rise; `background_priority` is offered only for a (pid, name)
  pair whose create time is unambiguous, never for a process with the game's image name, with the game's PIDs in
  `exclude`.
- `packet_loss`: loss events, and a steady overall loss ≥ 1 % from at least 2 lost probes; never "no ping was
  lost" when some were. `server_lag`: argued only when a server flow covers most of the capture; no server flow
  is unchecked (a TCP-only or offline game); an "open" gap is a lost connection; "local" gaps are the game
  pausing its own sending. `bandwidth_hog`: checked only from a source that sees all traffic (ETW talkers, or the
  NIC counters).

### Scoring (LC 5)
- Evidence families: frame timing, GPU telemetry, GPU engine/memory, CPU threads, CPU frequency/limits,
  memory, disk, interrupts, background processes, static configuration, network path, Wi-Fi, foreground. Per
  family only the strongest supporting and the strongest contradicting item count. Weights: 1 a setting that
  only makes a cause likelier, 2 a steady metric past threshold, 3 event-linked with lift ≥ 4, 4 a direct
  readout (a throttle counter, `FrameType`, a static readout that is itself the cause). Static configuration adds
  at most +2 and never makes a cause likely on its own.
- Labels → `Finding.confidence`: `high` = likely (S ≥ 4, key family measured and supporting), `medium` =
  possible (2 ≤ S < 4), `low` = worth checking. "Checked and fine" (S ≤ −2 with coverage) → `Diagnosis.cleared`;
  "not enough data" or not measured → `Diagnosis.unchecked`; inconclusive stays silent.
- Rank by label, then S, then explained share of events, then whether an auto-fix exists.

### Lag type and headline (analysis/diagnose.py)
- Lag time per category: **stutter** = stutter spans; **low FPS** = fps_drop spans, plus the steady time when the
  steady rate (`bound.steady_fps`, so freezes never lower it) is under 80 % of the target (the refresh rate; 60 Hz
  when unknown) for every verdict except `capped` and `vsync`, whose own cap or lock is the target; **pacing** =
  pacing claimed by a confident pacing finding or dropped-frame judder, minus stutter and fps_drop spans;
  **network** = network events without router-only losses (unless an internet, server or non-local gap overlaps)
  and without "local" gaps, named only with ≥ 2 events or ≥ max(1 s, 1 % of the capture).
- Lag type = the category with the most lag time, "mixed" when the runner-up has ≥ 50 % of it, "none" with
  frames measured and no lag, "unknown" without frames.
- The headline names the top finding in one sentence; whenever the lag type is not stutter and there were
  severe hitches or freezes it adds "; also N severe hitches, worst X ms"; an open gap reads "the game server
  stopped answering at m:ss"; with zero hitches it says "no hitches ≥ 50 ms in N frames (at most 3/N at 95 %)";
  without frames it opens "Frame timing was not measured (why), so stutter and frame rate could not be checked".

## Fixes (fixes/)

`Fix` subclasses implement `target`, `read`, `write`, `wanted` (and `readback`/`describe` when the effective
state is read differently from the written one) on top of `fixes/base.py`: plan → journal first → write →
read back → `applied`, or `failed` with the original restored; `undo` restores the journaled value and reads
it back; `heal` resolves `pending` entries left by a crash. Backends are injectable so tests never touch real
settings (`reg_sandbox` for the registry, fakes for power/display/process).

| fix id | params | what it changes | readback | needs restart |
|---|---|---|---|---|
| `power_plan` | `{"scheme": guid}` | active power scheme (`PowerSetActiveScheme`, allowed for standard users, WT 7.5) | `active_scheme()` | no |
| `refresh_rate` | `{"device": "\\.\DISPLAY1", "hz": int}` (plan() adds `monitor`, the monitor's stable path) | display refresh at the current resolution: `CDS_TEST` first, then `CDS_UPDATEREGISTRY`; journals `{width, height, hz}` | `current_mode()`, `list_displays()` hz, and the stored mode equal to the screen | no |
| `gpu_preference` | `{"exe": path, "preference": 2}` | `HKCU\Software\Microsoft\DirectX\UserGpuPreferences\<exe>` field `GpuPreference=N;`, other fields kept | registry, then the game's LUID next run | game |
| `game_dvr_off` | `{}` | `HKCU\System\GameConfigStore\GameDVR_Enabled` = 0 and `HKCU\…\GameDVR\HistoricalCaptureEnabled` = 0 | registry | game |
| `game_mode_on` | `{}` | `HKCU\Software\Microsoft\GameBar\AutoGameModeEnabled` = 1 | registry; `effective_mode()` = game_mode while playing | no |
| `windowed_optimizations_on` | `{}` | `DirectXUserGlobalSettings` field `SwapEffectUpgradeEnable=1;`, other fields kept | registry | game |
| `background_priority` | `{"pid": int, "create_time": int, "class": "below_normal", "exclude": [pids], "name": str, "game_name": str \| None}` | priority class of one of the user's own non-game processes, for this session only | `priority_class()` | no |

- Registry value semantics marked *unverified* in WT 8 are community-sourced; each such fix says so in its
  description and plan notes. The string-field fixes journal and restore only their own `key=value;` field.
  `game_mode_on` writes 1 even when the value is absent (absent reportedly means on, unverified).
- `background_priority` refuses the game and its children (`exclude`, required and non-empty), a process whose
  image name is the game's (`game_name`), a live (pid, create_time) whose image name is not `name` (checked
  without a handle), services, protected processes and other users' processes; `class` is `below_normal` or
  `idle`. Undo restores the journaled class if the same (pid, create_time) still runs; a session-only change
  whose process has ended is settled as undone.
- A change that is not session-only and whose subject is gone at undo (an unplugged display) is left in the
  journal, `applied`, with "left alone; run undo again when it is connected" (ok False). A refresh rate that
  Windows stored for after a restart (`DISP_CHANGE_RESTART`) is put back in the profile and confirmed, or the
  entry stays `applied` so undo can act. `conflict` entries (neither the original nor lag-doctor's value) are
  listed by `undo` and restored only with `--force`, with a readback.
- The journal keeps unknown entry keys in `extra` (a newer build's fields survive); an unreadable journal is
  copied aside once and fixes pause with a plain message.
- Advice only (never automated): hardware GPU scheduling, Memory Integrity / VBS (information only), the MUX
  / "Ultimate" GPU mode and the OEM fan/power mode (Armoury Crate), drivers, in-game settings, frame caps,
  cooling, Wi-Fi band and placement, pausing downloads, closing apps, the Wi-Fi background-scan setting.
- Never offered: disabling security features, killing or suspending processes, HKLM or policy changes,
  anything that touches the game process (including its priority: that needs a handle, WT 9.2).

## Report

- **Console**: the headline, key stats ("limited by" the bound verdict), up to three findings with their top
  evidence lines and fixes, the moments the user marked with the lag key (how many matched a lag event, which
  showed nothing in what was measured), then "checked and fine" (neutral topic names: "Game Mode", never "Game
  Mode off"), "not measured" and "could not check", grouped by the missing source. Fits one terminal screen; a
  fix command is never wrapped; control characters from capture strings never reach the terminal. `check`
  prints a settings check-up instead (no verdict line, one line for what needs a recording).
- **Both reports** list the same "not measured" sources (`labels.not_measured`: the capture's own source list,
  then the standard sources it never recorded), each named in words (`labels.SOURCES`: "the game's own network
  traffic (ETW)", never `net_flows`) with its reason. Text a player reads never carries the research notes'
  citations (`WT 8`, `LC 4.9`, `CC-5`): they stay in comments. A fix's `detail` never repeats the restart note
  the HTML report adds for `needs_restart` (both checked by `tests/test_user_text.py`).
- **HTML** (`report/html.py`): one self-contained file (inline CSS + SVG, no script, no URL), light/dark via
  `prefers-color-scheme`: verdict, stat tiles, frame-time chart (main swap chain only) with stutters and marks,
  CPU/GPU chart, VRAM chart, ping chart (lost pings from the collector's own record), marks on every time chart,
  finding cards, marked moments, worst moments, checked-and-fine, system snapshot, not measured. Each section
  falls back to a note on odd values instead of failing the page.

## CLI

```
lagdoctor watch [--game NAME | --pid PID | --auto] [--duration S] [--server IP] [--no-network]
                [--elevate | --no-elevate] [--out DIR] [--hotkey KEY | --hotkey none]
lagdoctor analyze CAPTURE            re-run the analysis on a saved capture
lagdoctor check                      settings check-up without a game running
lagdoctor fix [ID ...] [--list]      apply fixes suggested by the last report (asks first)
lagdoctor undo [ID | --all]          restore from the journal
lagdoctor setup                      fetch + verify PresentMon, explain how to get ETW rights
```

- Each `watch` writes `capture.ldcap.json.gz`, `diagnosis.json` and `report.html` into a new folder under
  `paths.sessions_dir()` and remembers it (absolute) in `last-session.txt`; `check` writes its diagnosis into a
  `<stamp>-check-up` folder and remembers it in `last-checkup.txt`. `fix` offers what the last watch and the
  last check-up suggested, newest report per fix id, saying where each offer comes from; a remembered folder
  that is gone is reported, never replaced by another session's.
- `analyze` writes into a fresh session folder (with `analyzed-capture.txt` naming its source), never beside
  the capture, and never changes what `fix` uses. A damaged, foreign or newer-format file gets one plain line.
- The elevated child (see Privileges and elevation) records into the file the parent chose and exits; it never
  analyses, fixes or touches the journal. `watch` exit codes: 2 bad arguments or game not found, 3 the PID now
  belongs to another process, 4 another recording is running, 5 its files would land in another app's private
  copy (any command that saves; see Where files go), 6 frame timing was wanted but PresentMon is missing or
  failed its check, 130 stopped waiting.
- The live status line shows `-` for a value not in yet and `off` for a source this recording does not run
  (frame timing off, `--no-network`). With frame timing on, one line on its own says "Frame timing is not
  working" as soon as PresentMon fails or its run ends mid-recording in anything but "ok" (a crash or a trace
  session stopped from outside settles "partial"; the game's own exit settles "ok"), or once the game window was
  in front for `session.NO_FRAMES_WARN_S` (10 s) without a first frame; time behind other windows does not count
  (a minimised game draws nothing). Frames that stop while PresentMon still runs are a frozen game, not a fault.
  Once PresentMon has ended the frame rate shows `-`, never its last value. `--no-network` records the network and
  net_flows sources as "turned off for this recording (--no-network)".
- Stopping: the first Ctrl+C stops the recording and saves it ("Stopping and saving the capture..."); later
  presses during the stop and the save are only counted. Ctrl+Break stops and saves too. Closing the window
  stops, ends the `lag-doctor-net` session at once, and waits up to 4.5 s for the save (Windows gives a closing
  console 5 s). Signing out is not covered (a process that loads user32 never gets CTRL_LOGOFF). In the
  parent window, the first Ctrl+C says the elevated recording goes on in its window and where it will be saved;
  a second one stops waiting and prints the `analyze` command.
- `fix` applies only offered fixes, with the offered params, after showing the plan and its notes; it refuses
  to run elevated. A plan-time refusal is reported, never a traceback. Every command except `setup` first
  settles journal entries a crash left `pending`, and marks session-only changes whose process has ended as
  undone (checked without opening the process).

## Testing

- **Unit**: pure functions on literal sample points; parsers against fixture text taken from real tool output
  (PresentMon CSV from replays of the gold ETL captures in PresentMon's repo); rules on synthetic captures
  built through `SeriesBuilder`.
- **Live, read-only**: ctypes wrappers run against this machine and are compared with an independent readout
  (psutil on our own process, typeperf, `powercfg`, `nvidia-smi`, `Get-NetRoute`).
- **ETW without rights**: a private in-process session (allowed for standard users) proves the consumer
  path (NE 5.6); PresentMon's `--etl_file` replay proves the frames path.
- **Red first**: every guard is sabotaged once and must turn its test red (`scripts/redfirst.py`). The runner
  copies the tree to a temp folder and mutates only the copy, so a killed run can never leave a sabotaged guard
  in the working tree; a test that reads the real repo by absolute path is reported UNISOLATED, and one that
  writes into its own tree stops the run.
- **No handles**: `tests/test_no_handles.py` fails if `psutil.Process`, `process_iter` or `pid_exists` is
  used outside the allow-list, and if `OpenProcess` appears outside `winapi/process.py`.
- **End to end**: `scripts/e2e.py` runs each LagLab mode with a real capture and judges the diagnosis against
  LagLab's ground-truth log (`scripts/e2e_truth.py`). It needs ETW rights, so it runs on demand: without them
  it relaunches itself once through a UAC prompt and prints the verdicts back in the original window. The
  `smooth` run also exercises the real ping and Kernel-Network ETW paths.
