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
   - **No psutil, no handles.** PIDs, names, paths, CPU, threads, I/O and parent PIDs come from
     `winapi/ntquery.py`, which opens no process; `OpenProcess` exists only in `winapi/process.py`.
     `tests/test_no_handles.py` enforces this. `lagdoctor/` does not use psutil at all (principle 5); the tests
     still read it as an independent readout, and there its trap stands: `psutil.Process(pid)` (the
     constructor itself), all its methods, `psutil.process_iter()` and `psutil.pid_exists()` open the process
     (WT 9.4), and only `psutil.pids()` does not.
2. **Evidence or silence.** Every finding carries the numbers behind it. A source that did not run is
   reported as *not measured*, never as *fine*; a cause that was checked and ruled out is listed as
   *checked and fine* with what was measured.
3. **Fixes are opt-in, reversible and journaled.** The original value is written to the journal before
   anything changes; the change is verified by reading the effective state back through an independent
   path; `undo` restores from the journal, never from an assumed default.
4. **Least privilege.** Only frame timing and the ETW network flows need ETW rights. Everything else runs
   as a normal user, fixes never need elevation, and the tool says plainly what it cannot see.
5. **Standard library only, as the installed runtime has it.** The installer ships python.org's embeddable
   runtime, which holds no package beyond the standard library and leaves part of that out (tkinter and Tcl/Tk,
   IDLE, turtle, venv, ensurepip and the tests), and Smart App Control, which blocks unknown unsigned code, would
   block psutil's unsigned native module (`docs/research/install-and-rights.md`, sections 5 and 10). So `lagdoctor/`
   imports only itself and the modules that runtime imports, by its own answer (`scripts/runtime_modules.json`), and
   reads Windows through `winapi/` (ctypes); `tests/test_stdlib_only.py` enforces it.

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
- E3 (2026-11-07, on a GitHub runner; research note section 8): a real LocalService token with its privileges
  trimmed to SeChangeNotifyPrivilege enables every provider and runs the tracker and PresentMon; with a restricted
  service SID it can start no session at all. The capture service (`docs/INSTALLER-PLAN.md`) is built on that.

- `winapi/elevation.can_trace()` = `CheckTokenMembership(Administrators S-1-5-32-544)` OR
  `CheckTokenMembership(Performance Log Users S-1-5-32-559)`; both count enabled SIDs only, so a UAC-filtered
  admin token answers False (PM 6.3, verify log).
- `lagdoctor watch` without ETW rights asks: start the relay (one UAC prompt), or continue without frame
  timing (frames reported *not measured*). `--elevate` / `--no-elevate` answer it up front. A member of
  Performance Log Users is not asked (frame timing works unelevated), but an explicit `--elevate` still elevates
  them, for the game-server tracker; a member who declines records frame timing here. An administrator window is
  never elevated again. Without rights, the tracker's not-measured reason names what helps: `--elevate`, and for a
  member, that the group cannot start it (`netflows._refused_why`).
- The recording stays in the user's own window: the live line, Ctrl+C, the inventory, the samplers, the save,
  the analysis and the fixes all run there, in the user's account. Only PresentMon and the Kernel-Network session
  run in the relay (next section), with administrator rights; their data comes back over a locked pipe. So an
  administrator approving the prompt for a standard user changes only who runs those two: the inventory, the Steam
  log and the capture file are the user's.
- The relay runs as `pythonw -B -E -s -m lagdoctor.relay`: no console window, no bytecode written (it writes no
  file at all), and the user's PYTHON* variables and user site-packages left out. From a checkout it still runs
  the checkout's own code, which the user can write: UAC shows the signed Python launcher, not the code it runs,
  and is not a security boundary for same-user code. Installed by the MSI (Stage 3), its interpreter is the
  bundled runtime's `pythonw.exe`, whose `._pth` gives `sys.path` its own ZIP and folder only, so the relay runs
  only code under Program Files, which only an administrator can change. The capture service (Stage 4) takes the
  prompt away too (DECISIONS 2026-11-06, `docs/INSTALLER-PLAN.md`).
- One recording at a time: the window that records holds the named mutex `Local\lag-doctor-capture`
  (`session.CaptureLock`) for the whole recording, the relay's part included; a second `watch` refuses before
  starting PresentMon or any ETW session.
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
  frame timing says so before "Recording.", and when the relay's own check of PresentMon fails, nothing is recorded
  (exit 6). A 14-minute recording of a real game was once made with every frame missing and the reason only in the
  report afterwards.

### The relay (lagdoctor/relay/)

The recording's administrator part (LAUNCHER-PLAN M3), and the protocol of Stage 4's capture service. `client.py`
is the recorder's side, `server.py` the relay process, `protocol.py` the messages.

- **What the prompt approves** is the whole command line (`client.relay_argv`): `--pipe` a fresh random name
  (`lagdoctor-relay-` and 32 hex digits), `--recorder pid:create_time` (the process running `watch`, never a venv
  launcher), `--sid` the recorder's account, `--game pid:create_time`, and `--frames --presentmon-exe X` (the
  PresentMon the recorder verified, a plain local drive path: never a share or a device path, which the relay would
  open with an administrator's credentials) and/or `--network`. The relay parses it strictly (`server.parse_args`,
  also run by `relay_argv` before any prompt); from the pipe it takes `start` then `stop`, once each, and nothing
  after `stop`, so no PresentMon or ETW argument, and its queue of commands never grows.
- **Its checks, before any pipe exists**, on its own handle-free snapshot: the recorder is running as that exact
  process in the relay's Windows session, and `target.accept_target` accepts the game: running as that exact
  process, in that session, the one approved. The recorder calls the same predicate before asking for the prompt,
  so they cannot drift. Then PresentMon must be on a fixed or removable drive (`storage.drive_type`) and pass
  `setup.verify` again.
- **Its pipe** (`winapi/pipe.py`): first instance only, remote clients refused, a DACL for the recorder's SID
  (`client_dacl`), a medium label, and Administrators as its owner (`O:BA`), which Windows refuses an unelevated
  token (error 1307, measured). It serves the first client whose pid and session (read from the pipe, no handle)
  are the recorder's, and lets anyone else go.
- **The recorder's checks** (`client.launch`): the server's pid is the pid of the process it launched (the
  process handle it holds since the launch keeps Windows from giving that number away), the pipe's owner is
  Administrators, and the relay's hello names that pid, the game and what it runs.
- **What leaves the relay**, after `start` (which carries only the capture clock's origin):
  - the game's frame rows, only the columns the recorder reads (`protocol.FRAME_COLUMNS`); its PresentMon runs as
    `PresentMonSource(..., forward=)`, which passes rows on and keeps none, and starts only if `accept_target` still
    accepts the game on a fresh snapshot (minutes may have passed since the first check);
  - the Kernel-Network events of the game's processes and of the recorder (the tracker's DNS self-test), whole. The
    game's processes are its tree while it runs plus those seen before that still run as the same process, each
    in the recorder's session, from a snapshot once a second on a thread of its own (a slow reader cannot stretch
    it): a pid Windows gives to another program is dropped at once. A recorder whose pid names another process
    ends the recording;
  - for every other program, bytes received and sent as running totals, only for processes in the recorder's
    Windows session (DECISIONS 2026-11-08), the largest 64; bytes of programs gone before their session could be
    read, or in another session, are only counted;
  - statuses: PresentMon's (`frames`, final once its run is over), the ETW session's start result and health
    (`net`), with the relay's own counts (`health.relay`: events it did not decode, rows or events it could not
    pass on, bytes it kept back). An event of an unknown layout is only counted: whose it was cannot be read.
- **Stopping**: on `stop`, or when the recorder hangs up, the relay stops the network trace, then PresentMon, sends
  what is left and each final status, then `end`; the recorder lets it go as soon as the recording stops, before
  the save. Messages are one JSON object per pipe message of at most 64 KiB; each side checks every field
  (`protocol.from_relay`, `protocol.from_recorder`), and a message the protocol does not allow ends the link with
  the reason.
- **Exit codes of the relay**, read by the recorder when it ends before the pipe can say why
  (`protocol.EXIT_*`): 2 a command line lag-doctor does not build, 3 the game or the recorder not running as
  approved or in another session, 4 its pipe could not be made, 5 the recorder did not connect within 30 s, 6
  PresentMon failed its check, 7 the recorder broke the protocol, 1 anything else.
- **In the recorder**, `session.SessionOptions(relay=link)` makes the session's "frames" collector
  `client.RelayedFrames` (PresentMonSource fed from the pipe: the same storage, live frame rate and result, with
  `presentmon.relay` `{pid, rows_received, rows_refused}`) and hands the link to `FlowTracker(relay=)`, whose ETW
  session it stands in for (`client.RelayedEtwSession`). `tool_procs` holds the relay, and its whole process tree
  (its PresentMon) joins `target["tool_pids"]`. A link that breaks ends both collectors' data there, with the
  reason, and the rest of the recording goes on. The relay is asked to stop only by a collector that is stopping
  (a tracker that fails never ends frame timing), and the stop is announced to the tracker first
  (`netflows.expect_external_stop`), so the relay's consumer ending is never read as dying by itself. Network
  messages that come before the tracker has made its session (the frames may start the relay first) are kept for
  it, up to 50,000 events, and the rest counted (`health.early_dropped`).

### The control channel (lagdoctor/control.py)

`lagdoctor watch --control stdio` is a recording that lag-doctor's window drives (M4). The window starts it with no
console and two pipes, which carry JSON lines both ways: one object a line, ASCII, at most 64 KiB, each naming
itself in `t`. `control.from_app` and `from_recorder` check every message and field, and `control.py`'s docstring
lists them. The recorder runs `cli.cmd_watch` as a console does, through its seams: every line it says goes to the
window as `say`, the session's events (Who follows a recording) as messages of their own (a mark as `mark {at}`,
since `t` names the message), and nobody is asked anything. Whatever ends it, the last line is `done {reason, code,
session}`; `control.done_reason` maps watch's exit code, the events that went out and the stop's reason to one of
`control.REASONS`.
- **Nothing else reaches the channel.** Before anything can print, `serve` keeps a private copy of stdout's file
  for the channel and points stdout itself at stderr, the window's log file.
- **Nothing waits on the window.** One writer thread drains a bounded queue (`MAX_QUEUED` lines; past that they are
  dropped and counted). A write that fails ends the writing quietly (`broken`). stdin ending (`gone`) means the
  window has closed or died, and the recording goes on to its end and is saved: a closed window cannot lose a
  recording.
- **Stop and Cancel.** `stop` and `cancel` from the window both ask the session to stop
  (`session.request_stop`, reason `control.STOPPED_REASON`). Until `recording` has gone out, both also set watch's
  `cancel`. `cmd_watch` checks it before the game is looked for, once it is found, while the relay's prompt is up and
  before the recording starts, and the session checks it once everything has started, before it says `recording`
  (`SessionOptions.cancel`; the inventory and the relay's trace can take seconds). Any of these ends watch with
  nothing recorded and no session folder (`cli.EXIT_CANCELLED`, 130). After `recording`, it is a stop, and the
  capture is kept.
- **The command line comes first.** `main` parses it before the channel exists, so a value the parser refuses ends
  the recorder with exit 2 and the usage on stderr, and no `hello` or `done`. The window builds only command lines
  the parser takes (a note no longer than `model.NOTE_MAX`, say).
- **The window starts the recorder itself, with its base interpreter** (`Path(sys.base_exec_prefix) /
  "pythonw.exe"`, cwd at the tree, as `relay/client.relay_python` does for the relay), never a venv's
  `python.exe`, which is a launcher. The recorder is then the window's own child: its parent is the window, and the
  process handle the window holds is the recorder's, which the relay's handle is handed over into. The window
  checks `hello.pid` against that handle's pid before it hands anything over.
- **The relay's prompt is the window's.** Microsoft's guidance says the elevation mechanism uses the HWND passed to
  ShellExecuteEx to decide whether a prompt is foreground or background, and that without one it only blinks on
  the taskbar. It never says a window of another process counts, so the window raises the prompt itself. The
  recorder's launcher (`Channel.relay_launcher`, given to `relay/client.launch`) sends `elevate {argv, cwd}`: the
  command line `client.relay_argv` built, which the window checks before it raises the prompt with its own window
  (`launch_elevated(..., hwnd=)`). Then the window copies the relay's process handle into the recorder
  (`ElevatedChild.hand_over`, with the recorder's own process handle, which it holds from starting it) and sends
  `elevated {handle, pid}`. The recorder adopts the handle (`ElevatedChild.adopt`: a process handle here, for that
  pid), so from there everything is as in a console's recording: the handle pins the relay's pid, its exit codes
  reach `_relay_failure`, and `server_pid`, the pipe's owner and the hello are checked as before. `declined`,
  `elevate_refused` and `elevate_failed` raise what launch_elevated would raise (PermissionError, ValueError,
  OSError), and a Cancel or a window that went away ends the wait. A window that goes away before the recording
  begins (its end of stdin closes before `recording` went out) is a Cancel; after that, the recording goes on.
- **The window's processes are lag-doctor's own.** The recorder's parent, when it is a Python that started first,
  counts alone (`tool_alone`), and its children named `msedgewebview2.exe`, its WebView2 browser processes, are
  roots found again in each snapshot (`tool_roots`), only while the window is still that process: once it has
  closed, its number may name another program, whose WebView2 processes are not lag-doctor's.

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
  `cli.PRIVATE_COPY_POLICY`: `setup`, `watch`, `fix`, `undo` and `app` (its log and WebView2's data folder; from M4
  its recordings) save files other windows must find and refuse
  (exit 5, saying where the files would go and to open PowerShell from the Start menu); `check` and `analyze`
  only read the PC and print a one-line note, and the journal is not healed under a private copy. A probe that
  cannot be made refuses the savers too. The relay saves nothing, so it needs no probe. When lag-doctor's own
  Python is the packaged app (a Microsoft
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
            memory.py folders.py
            nvml.py                                     NVIDIA telemetry     (capture/gpumon)
            icmp.py iphlp.py wlan.py                    network probes       (capture/netmon)
            etw.py                                      ETW sessions         (capture/netflows)
            elevation.py                                rights + UAC relaunch
            pipe.py                                     the locked named pipe (the relay, the capture service)
            webview2.py                                 WebView2 from ctypes, through the runtime's own export (the app)
  relay/    protocol.py server.py client.py __main__.py   frame timing and the network trace with admin rights
            process.py                                  priority of other processes (fixes/priority)
  capture/  base.py (done) target.py sysmon.py gpumon.py netmon.py netflows.py presentmon.py inventory.py
            gamefiles.py (what lay beside the game, for the inventory) session.py
  analysis/ stats.py (done) frames.py events.py attribute.py rules.py diagnose.py
            window.py                                   the samples around a moment (the app's Results)
  fixes/    base.py journal.py registry.py catalog.py (done) + power.py refresh_rate.py gpu_preference.py
            gamebar.py windowed.py priority.py
  report/   charts.py labels.py console.py html.py (done) summary.py (summary.json, for the app's library)
  app/      main.py window.py bridge.py library.py views.py assets/ lag-doctor.ico     the desktop app (M1)
  setup.py  fetch + verify PresentMon
  control.py                                            `watch --control stdio`: a recording the app drives (M4)
  cli.py __main__.py
laglab/     the fake laggy game used by tests (done)
scripts/    redfirst.py e2e_truth.py (done) e2e.py etw_access.py (E1: who may enable the trace providers)
            stage2/ (the capture-service experiments E3, E3b, E3c: GitHub's runners only, never this PC)
            wheel_check.py; msi_build.py msidb.py wix_tool.py runtime_pin.py (the MSI); msi_check.py (installs it:
            GitHub's runners only, never this PC); runtime_modules.py + .json (what the pinned runtime imports);
            webview_spike.py (M0); make_icon.py (lag-doctor's icon)
installer/  lag-doctor.wxs (WiX v7, per-machine, with the app's Start-menu shortcut), lagdoctor.cmd (the PATH shim),
            PresentMon-LICENSE.txt
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
def pids(snapshotter: Snapshotter | None = None) -> frozenset[int]   # every running PID, 0 included, from one
                                                  # snapshot: the list EnumProcesses (psutil.pids) gives, no handle
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

# winapi/memory.py (SDK um\sysinfoapi.h) ---------------------------------------------------------------
def total_physical_bytes() -> int                 # GlobalMemoryStatusEx ullTotalPhys; OSError when Windows cannot say

# winapi/folders.py (SDK um\KnownFolders.h) ------------------------------------------------------------
def known_folder(folder_id: str) -> str           # SHGetKnownFolderPath for this process's user (freed with
                                                  # CoTaskMemFree); OSError (.winerror = the HRESULT) on failure
def documents() -> str                            # FOLDERID_Documents, wherever OneDrive moved it
def local_app_data() -> str                       # FOLDERID_LocalAppData

# winapi/storage.py (WT 9.5) ---------------------------------------------------------------------------
def media_type(path: str) -> str                  # "SSD" | "HDD" | "Unknown"  (seek penalty, access 0)
def free_gb(path: str) -> float | None
def drive_type(root: str) -> int                  # GetDriveTypeW of a drive root: 2 removable, 3 fixed, 4 network
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
def adapter_addresses() -> list[dict]             # [{alias, if_index, addresses: [text]}]: every adapter's unicast
                                                  # IPv4 and IPv6 addresses (GetAdaptersAddresses), adapters that are
                                                  # down included; an IPv6 link-local address carries "%zone"; a
                                                  # pointer that leads outside the returned buffer is an OSError

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
def launch_elevated(argv: list[str], cwd: str, *, hwnd: int | None = None) -> ElevatedChild
                                                  # ShellExecuteExW "runas", new console; PermissionError when
                                                  # declined (1223); ValueError, before any prompt, for a relative
                                                  # program or folder or a bad hwnd. hwnd: the window the prompt
                                                  # belongs to (M4: lag-doctor's window), None for none. It blocks
                                                  # until the prompt is answered: a window calls it off its UI thread
class ElevatedChild:
    pid: int                                      # the child's; the open handle keeps Windows from reusing it
    def hand_over(self, to_process) -> int        # a copy of the handle, same rights, put into the process that
                                                  # to_process names (DuplicateHandle); this one is closed. M4: the
                                                  # window gives the relay's handle to the recorder it started
    @classmethod
    def adopt(cls, handle: int) -> ElevatedChild  # the child whose handle was handed over to this process;
                                                  # ValueError unless it is a process handle here (left open then)
    def wait(self, timeout_s: float | None = None) -> int | None  # exit code; a Ctrl+C leaves the child running;
                                                                  # wait(0) only polls
    def close(self) -> None
def relaunch_elevated(argv: list[str], cwd: str) -> int   # launch + wait + close (scripts/e2e.py)
def hold_named_mutex(name: str)                   # handle, or None when another process holds it
def release_named_mutex(handle) -> None

# winapi/pipe.py (the relay's and the capture service's channel; install-and-rights.md section 11) --------
def path(name: str) -> str                        # \\.\pipe\<name>; ValueError unless 1-100 of [A-Za-z0-9._-], not
                                                  # starting with . _ or -: "..\C:\x" would open an ordinary file
def client_dacl(sids: list[str]) -> str           # "D:P(A;;0x12018b;;;<sid>)...S:(ML;;NWNRNX;;;ME)": exactly what
                                                  # connect() asks for, and a medium label denying everything below
                                                  # medium every right (NW alone still lets it open to read or wait)
class PipeServer:                                 # the first and only instance; the name taken already raises
    def __init__(self, name, sddl, max_message=65536)  # ERROR_ACCESS_DENIED (or ERROR_PIPE_BUSY when that pipe
                                                  # allows one instance); message mode; remote clients refused
    def wait_client(self, timeout_s) -> bool      # False on timeout; any thread may call it; a client that came and
                                                  # went is let go and the wait goes on
    def client_pid(self) -> int                   # GetNamedPipeClientProcessId: no handle on the client
    def client_session(self) -> int
    def disconnect(self) -> None                  # unread data is lost: the protocol ends a conversation first
def connect(name, timeout_s, max_message=65536) -> PipeClient   # identification level, message read mode; retries
                                                  # FILE_NOT_FOUND and PIPE_BUSY to the deadline (TimeoutError with
                                                  # .attempts, .errors), raises any other refusal at once
class PipeClient:
    def server_pid(self) -> int                   # the pipe's first creator (one instance: the server; maybe exited)
    def server_session(self) -> int
# both ends:
    def read(self, timeout_s) -> bytes            # one whole message; TimeoutError keeps the pipe usable; PEER_GONE
                                                  # winerrors once the other end has gone; a message over max_message
                                                  # raises ERROR_MORE_DATA, and so does every read until disconnect()
    def write(self, data, timeout_s) -> None      # 1..max_message bytes (ValueError first); after a timed-out or
                                                  # interrupted write every write raises ERROR_BROKEN_PIPE
    def owner_sid(self) -> str                    # the pipe's owner, from its creator's token: check it beside the PID
    def descriptor(self) -> str                   # the DACL and label Windows holds, in SDDL
    def remote_clients_rejected(self) -> bool     # FilePipeLocalInformation, read back
    def close(self) -> None                       # never waits: a running call sees it within a slice and raises
                                                  # ERROR_OPERATION_ABORTED; the last call to leave closes the handle
# One read and one write may run at once. Waits run in 100 ms slices, so Ctrl+C and close() land within one. Each
# direction's OVERLAPPED and buffer belong to the pipe, and a call an exception interrupts is cancelled and waited out
# before the exception goes on (a message it had received is the next read's): Windows never writes freed memory.

# winapi/webview2.py (the app's window; research note desktop-app.md, sections 3 and 11) -----------------
SLOTS: dict[str, dict[str, int]]; IIDS: dict[str, str]   # only what lag-doctor calls or implements, every entry
                                                  # checked against the SDK's WebView2.idl (tests/test_webview2_idl.py)
def find_runtime() -> dict | None                 # {folder, version, build, dll, exe} of the installed Evergreen
                                                  # runtime (EdgeUpdate ClientState EBWebView, HKLM then HKCU)
def environment_creator(dll) -> fn                # CreateWebViewEnvironmentWithOptionsInternal(check, 0, folder,
                                                  # options, handler): the runtime's own export, no loader DLL
class Com(ptr, iface, owned=True)                 # an interface pointer: call/do by method name, get_*/put_*, qi(),
                                                  # get_ptr(), release(); ComError (an OSError) with .hr on a failure
class Handler(iface, proto, fn, guard)            # one-method COM object; guard(iface, fn) must never raise into C
class EnvOptions(strings, bools)                  # EnvironmentOptions 1-3 as one object; .asked lists what the
                                                  # runtime read
def env_state(name) -> str | None; set_env(name, value | None)   # SetEnvironmentVariableW: "" stays present
# plus the user32/kernel32/ole32/shell32 calls a host window needs (CreateWindowExW ... MessageBoxW). Whoever makes a
# Handler or EnvOptions keeps it referenced for as long as WebView2 may call it.

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

### Who follows a recording (capture/session.py)

`Session(target, opts, ..., printer=None, observers=None).run(stop=None, ctrl_c=None) -> Capture`. Besides the
console's status line (`printer`), a caller follows the recording through `session.Observers(on_tick=None,
on_event=None)` (M4: the app's window, through `watch`):
- `on_tick(elapsed, live)` once a status period (1 s), after the status line, with `{collector name: live()}`.
- `on_event(kind, data)`: `waiting` `{}` (from `cli.cmd_watch`: the game is being looked for); `target` `{name,
  pid, how}` (from `cli.cmd_watch`: the game was chosen); `recording`
  `{duration_s, hotkey, frames, network}` (everything has started; the origin was set just before the collectors
  started); `mark` `{t}`; `frames_lost` `{why}` (the "Frame timing is not working" line); `stopping` `{reason}`
  (the stop began: "duration", "game exited", "Ctrl+C" or the reason given to `request_stop`); `saving` `{frames}`
  and `saved` `{session}` (from `cli._record`; `session` is the folder's name, which is the app's id for it).
- Each is contained as the status line is: one that raises is not called again, and the capture's notes say so in
  plain words (unless it first fails at `saved`, when the capture is on disk already); neither can stop the
  recording or lose it. They run on the thread where the thing happened (the lag key's own for a mark), so they
  must return at once: stop, duration and the game's exit are checked on the thread that calls `on_tick`, and
  `stopping` and `saving` come before the collectors stop and before the save.
- `Session.mark(when=None) -> float | None`: a mark at `when` (`clock` time, default now) in seconds from the
  start, also told as `mark`; None before the recording started, and nothing is kept. The lag key calls it.

### What each collector's `result()` returns

The session copies these into the `Capture`. Keys not listed here go nowhere, so add them here first.

| collector (`name`), module, class | `result()` keys → Capture field |
|---|---|
| `frames`, capture/presentmon.py, `PresentMonSource(pid, exe=None, etl_file=None, *, is_alive=None, forward=None)` (relayed: relay/client.py `RelayedFrames`) | `columns` (F, every swap chain of the target) → `frames`; `swap_chains` `{"0x…": {"frames", "displayed", "runtime"}}` (an unfiltered replay keys them `"<pid>:0x…"`), `main_chain` (most displayed frames), `presentmon` `{version, exe, args, exit_code, stop_path, rows, malformed, rejoined, headers, stderr_tail}` → `target["presentmon"]`; `live()` = `{frames, fps, malformed}` |
| `system`, capture/sysmon.py, `SystemSampler(target)` | `columns` (S) → `system`; `others` → `others`; `game_adapter_luid` (adapter whose 3D engine the game used most) → `target["adapter_luid"]`; `game_pids` (game + children seen) → `target["pids"]`; `cost` `{ticks, median_ms, max_ms, mean_ms}` (also in the status detail) |
| `gpu`, capture/gpumon.py, `NvidiaSampler(adapter)` | `columns` (G) → `gpu`; `device` `{name, clock_max_mhz, mem_total_mb, temp_target_c, power_default_w}` or None → `inventory["nvidia"]`; `live()` adds `source` ("nvml"/"nvidia-smi") |
| `network`, capture/netmon.py, `NetworkSampler(server_ip=None, *, steam_log=None)` | `columns` (N) → `network`; `targets` `{gateway, internet, server}` (IPs or None), `probes` `{target: {"sent", "lost", "from_t"}}`, `lost` `{target: [t, …]}`, `wifi_scans` `[t, …]`, `route` `{if_index, description, vpn: bool \| None, toward}` → `net_flows[…]` under the same keys; `interface` `{name, kind: "wifi"/"ethernet"/"other", band, channel}` → `inventory["network"]`; `downloads` `[{t, source, mbps}]` from the Steam log, or None when no download log was read, with `download_sources` (the logs read) → `net_flows[…]` |
| `net_flows`, capture/netflows.py, `FlowTracker(game_pids or a callable, *, relay=None)` | `server` `{ip, port, proto, pid, rx_packets, tx_packets, rx_bytes, tx_bytes, tick_ms, first_rx_t, last_rx_t}` or None; `talkers` `[{pid, name, rx_bytes, tx_bytes}]` top 8, with `talkers_scope` "all" (its own ETW session) or "session" (through the relay: the programs in this Windows session only); `game_wire_bytes` `[rx, tx]` the game's bytes that crossed the network (one end this PC's), never its loopback traffic; `gaps` `[[t, ms, kind], …]` (t = the last packet before the silence; kind: "delay" burst after the gap / "loss" no burst / "local" our own tx paused too / "open" the server went silent for good while we kept sending / "unknown" a late delivery hides which); `server_rx` `[[t, pps], …]` (t = end of each 1 s bin); talkers, game_wire_bytes, gaps and server_rx are None when the session never ran; `layout` "verified"/"unverified"; `health` → `net_flows[…]` |

`others` rows are `[t, [[pid, name, cpu_pct, io_bps, gpu_pct, create_time, gpu_game_pct], …]]`: up to 8
non-game processes that did something that tick, ranked by CPU, `cpu_pct` with 100 = one logical CPU, `io_bps`
read + write bytes, `gpu_pct` their busiest engine on any adapter, `create_time` raw FILETIME ticks (so a fix can
prove a PID was not reused), `gpu_game_pct` their busiest engine on the game's adapter (None while that adapter
is unknown). lag-doctor's own process is included: its load is real, and `target["tool_pids"]` lets the analysis
keep it out of the blame and out of the background-CPU signals (the listed totals, and the machine-wide reading
minus the game): its sampling is the measurement's cost, never "another program using the CPU". The session writes
it from handle-free snapshots, at every status tick from `TOOL_SIGHT_S` (0.5 s) on and once more just before the
stop, sharing each tick's snapshot with the liveness check (a whole-system snapshot costs several milliseconds:
docs/research/overhead.md, section 5.3): this process and its venv launcher with what they started (PresentMon,
nvidia-smi), each `SessionOptions.tool_procs` root (the relay) and each root `tool_roots(snapshot)` finds (the
window's WebView2 browser processes, which a recovery replaces) with what those started, and each `tool_alone`
process by itself (lag-doctor's window, whose other children stay countable);
never the game's own tree. Roots and lone processes count only while they are the process named (pid and create
time). It holds at most `MAX_TOOL_PIDS` (64); when one more is seen, the capture's notes say so. A `tool_roots` that
raises leaves the rest listed, and the notes say lag-doctor could not find some of its own processes.

### Collector specifics

**frames (PresentMon, PM 10).** Command (argv list, never a shell; PID validated `0 < pid <= 0xFFFFFFFF`,
because `0` or junk means *every process*):
`--process_id <PID> --output_stdout --no_console_stats --qpc_time --track_frame_type --track_hybrid_present
--session_name lagdoctor --stop_existing_session --terminate_on_proc_exit`. Never `--track_pc_latency`: it switches on
the latency-ping code inside games built with NVIDIA's PC Latency markers, for data lag-doctor does not keep
(`docs/research/overhead.md`, section 5.1; DECISIONS 2026-11-09).
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
- Which copy: the one lag-doctor's installer put beside the code first (`setup.bundled()`: `PresentMon\` next to
  the `lagdoctor` package, `C:\Program Files\lag-doctor\PresentMon` in an install), else the per-user download
  (`%LOCALAPPDATA%\lag-doctor\presentmon\2.6.0`, made by `lagdoctor setup`). `ensure_presentmon`, `watch` and a
  `PresentMonSource` given no exe all follow that order. An installer's copy that fails its check raises
  `BrokenInstall`: setup says to install lag-doctor again and offers no download, which would route around a damaged
  install.
- Stop (PM 7.4): `CTRL_BREAK_EVENT` to the `CREATE_NEW_PROCESS_GROUP` child when we share a console; else, or
  if it has not exited in 10 s, a second PresentMon `--terminate_existing_session --session_name lagdoctor`
  (exit 0 stopped, 7 none); only then `kill()` plus the same cleanup. Never `terminate()` first: it leaks the
  ETW session. Keep draining both pipes until EOF.
- Start failure: exit code 6 is ambiguous (PM verify log); classify by stderr text ("access denied" →
  `unavailable` "needs admin rights or Performance Log Users"; "already running" / "error code 183" → retry
  once after `--terminate_existing_session`). A crash exit (outside 0–7) gets the same session clean-up as a
  kill.
- Watch the game ourselves (`target.is_alive`; without an `is_alive`, the game's PID still being in
  `ntquery.pids()`), because `--terminate_on_proc_exit` cannot fire if anti-cheat strips PresentMon's handle
  (PM 8.1). PresentMon ending by itself with exit 0 while the game still runs
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
is. Delivery Optimization is not read (it would mean a PowerShell spawn
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

`note` (str or None, added after the format's first build) is the player's note from `watch --note`. A capture from
before it loads with None; one holding another type is "damaged", as any field of the wrong type is. Read back it goes
through `clean_note`, so text a file holds is shown only as printable characters, and a note longer than `NOTE_MAX`
(a newer build's) is cut to it, ending in "...", never refused.

`span` is not in the format: only a copy cut to part of the recording has one (analysis/crop.py), `save` refuses such a
copy, and a file holding a `span` key loads as the whole recording, the key named in the note above.

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
ram_gb    float                                          GiB to 0.1, GlobalMemoryStatusEx (winapi/memory.py)
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
game      {exe, drive, media, free_gb, gpu_preference, monitor_device, launcher, exe_bytes, exe_modified, drop_ins,
           unreal}
          gpu_preference: 0 let Windows decide, 1 power saving, 2 high performance, None not set;
          launcher: "steam"/"epic"/"other"/None from the exe path;
          exe_bytes, exe_modified: the exe's size and modified time (ISO 8601 with this PC's offset), so a game
          update between two recordings shows;
          drop_ins = capture/gamefiles.drop_ins(): {folder (the exe's), files, more}: the DLLs beside the exe named
          as one Windows keeps in System32 (gamefiles.PROXY_DLLS, none on KnownDLLs), the .asi plugins there and in
          the loader's scripts, plugins and update folders (one subfolder deep), and their settings
          (gamefiles.LOADER_CONFIGS, <dll>.ini, <plugin>.ini, UE4SS's Mods\mods.txt beside its dwmapi.dll);
          unreal = gamefiles.unreal(): {found, project, looked_in, config, config_more, mods, mods_more}: found
          False when the exe is not in <project>\Binaries\Win64|WinGDK beside the project's Content\Paks or the
          install's Engine\Binaries; config: every .ini in Saved\Config\<WindowsNoEditor|Windows|WinGDK> of each
          looked_in folder (LOCALAPPDATA's and Documents\My Games' folder of the project's and the install folder's
          name, and the project folder); mods: the .pak, .utoc and .ucas files in Content\Paks\~mods (two
          subfolders deep);
          a file: {path, name (relative to its folder; the bare name for config), bytes, modified, sha256, why}:
          sha256 None with why "larger than 32 MB", "past the 256 MB read for one recording", "out of time" (5 s
          for the whole check, from its start), "kept online only, so it was not read" (a OneDrive placeholder,
          judged from the folder listing, never opened), "it changed while it was read" or "could not be read
          (...)"; *_more and more: files past the 100 listed. A folder that exists but cannot be listed, or the
          user's folders that cannot be found, leave drop_ins or unreal None with the reason in errors
          ("game.drop_ins", "game.unreal", "game.exe_file"); a folder that does not exist holds nothing. None of
          it opens the game's process.
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
#  busiest_thread, cap_fps, refresh_hz, vsync: bool|None, vrr: bool|None, confidence, notes: [str],
#  gpu_probable: {seconds, share, windows, marks, marks_busy, holds} | None (no frame timing, verdict unknown)}
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
# analysis/crop.py (M5: the measured part, below)
def check_span(cap, t0, t1) -> tuple[float, float]  # the span crop cuts; ValueError (one clause) for anything else
def crop(cap, t0, t1, how="set") -> Capture         # `cap` cut to [t0, t1] for diagnose(); how: "key" or "set"
```
`ctx` = `{"frames": main chain, "stats", "bound", "events", "event_causes": attribute()'s summary}`.

### The measured part (analysis/crop.py)
A recording starts when the game is found, so it holds the game starting up (shader preparation, menus, loading).
`crop(cap, t0, t1)` returns a new Capture holding only what happened from t0 to t1, and the unchanged `diagnose()`
runs on it (`docs/LAUNCHER-PLAN.md`, "Measuring the right part"; DECISIONS 2026-11-09).
- **Times stay the recording's own** (seconds from its t = 0), so a cropped diagnosis's events, marks and moments land
  on the whole recording's charts as they are, and a time a report shows is the one the status line showed.
  `duration_s` becomes the span's length; the Capture's `span` is `{from_s, to_s, recorded_s, how}`, which diagnose
  copies into `stats["span"]`. Code that reads `duration_s` reads a length, never an end: the rules count "from the
  start" from `_Env.start` (shader_compile's first third), and their words name `_Env.part`, "the capture" until it
  is cut and "the measured part" after (`tests/test_crop.py` scans rules.py and diagnose.py for text naming the
  whole capture).
- **The cut**: the four tables, the other programs' samples and the lag-key presses keep their rows with
  t0 <= t <= t1, every column kept even where its cut is empty (`analysis.window`); a part that runs to the
  recording's end keeps a press up to 1 s past it, as the recording does. Each swap chain's first frame in the span
  is left out when its own interval (MsBetweenPresents, or how long the frame before it stayed on screen) began before
  t0 (a start set at the end of a loading screen would make the wait its first frame), except from the recording's
  own start. A table source with rows, but none in the span, becomes `unavailable`, "it recorded nothing in the
  measured part".
- **net_flows** (`crop.NET_FLOWS`, one decision per key the collectors write, which a test holds to
  `session.NETWORK_FLOW_KEYS` and `FLOW_KEYS`): the lists of times are cut (`lost`, `wifi_scans`, `downloads`,
  `server_rx`); each server gap is clipped to the span and one wholly outside it is gone; the server flow keeps its
  record with its first and last packet clipped, its four packet and byte totals None, and is None itself when its
  packets all came outside the span; `probes` are counted again (lost, the lost-probe times in the span; sent, those
  plus the span's rows that hold that target's round trip: exact, since the network collector counts a probe as sent
  only when it was answered or lost), None for a capture without the lost-probe times; the per-program and
  whole-recording byte totals (`talkers`, `game_wire_bytes`) become None, since they cannot be split by time, and
  `bandwidth_hog` then says so; a key this version does not know becomes None. None stays None (not recorded) and a
  list whose cut is empty stays a list (recorded, nothing in the span).
- **Refused** (`check_span`, the CLI and the app's Results through the same check): ends that are not numbers, a start
  before 0 or at or after the recording's end, an end not after the start, a part shorter than `MIN_SPAN_S` (10 s:
  frames.BOUND_MIN_S, shorter than any verdict on what limits the game); an end past the recording is its end.
- **Never saved**: `Capture.save` refuses a capture whose `span` is set, `span` is not part of the file format
  (`to_json` and `from_json` leave it out; a file holding one loads as the whole recording with a note), and the
  capture file always stays whole. Everything else (the inventory, the target, the notes, `started_at`,
  `clock_origin`) stays as recorded.

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
marks                                      list            [{t, events: [ids], causes: [every signal that moved, best
                                                           first]}], one per lag-key press
span                                       dict            {from_s, to_s, recorded_s, how}: the measured part analysed
                                                           (analysis/crop.py); absent for the whole recording
```

### The bound verdict (LC 3.4)
`bound()` judges steady stretches only (frames outside stutters and FPS drops, at least 10 s and 300 app
frames), in order: VSync (displayed intervals quantised on whole refresh periods, SyncInterval not 0 and
AllowsTearing not 1; with k = round(FT/P) >= 2 the hardware is tested against one period: GPU busy past
(k-1)·P gives `gpu`, a saturated busiest thread `cpu_main`, with a note that VSync rounds the rate down to
refresh/k), a frame cap (a regular present cadence: robust CoV, (IQR/1.349)/median per 5 s window, under 3 %,
away from k·P, with r < 0.75 and an unsaturated thread), `gpu` (r >= 0.90, or r above 0.75 with FT − G <= 1 ms;
G minus 0.5 ms when HAGS is on for the game's adapter), `cpu_main` (r <= 0.75 and busiest thread >= 0.85 of a
CPU over >= 1 s), `cpu_all`, `waiting`, else `mixed`. Without frames, a 3D engine at >= 95 % (median of 1 s
windows) gives a probable `gpu`; otherwise the verdict stays `unknown` and `gpu_probable` (`frames.probable_gpu`)
reads the same fallback per second: the 1 s windows with the engine at >= 95 %, and the marks whose span touches
one. It `holds` with >= 10 % of the windows and >= 10 s, or at half the marks or more; the first real capture's
median was 71 % while the GPU was pegged in its last minutes and at every mark. While other programs keep the
game's GPU >= 10 % busy (`frames.gpu_shared`, LC 4.6), a `capped` or
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
and an absolute floor. Where more than half the quiet windows share one value the MAD is 0, and the mean absolute
deviation × √(π/2) is the spread instead; only a baseline that never moves leaves the floor alone to decide (the
first real capture's disk reads were 0 in 54 % of its quiet seconds, and every read of 1 MiB/s counted). Lift
test: a cause *supports* when `a ≥ 3`, `a/n ≥ 0.30` and `lift ≥ 2` (n ≥ 5 events), with `b` the chance that a quiet
stretch as long as each event's window (judged, like the event, on its busiest 1 s) holds an active second,
averaged over the events (`attribute._expected`; a length too few quiet stretches reach is taken at the longest
that 8 do): a mark's 4.25 s window once met a plain 1 s share, 0.36 where its own length gave 0.64.
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
| GPU | VRAM near full and `game_shared_mb` rising (within 5 s, not a plateau over the median) | `vram_overflow` |
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
verdict) or Unchecked (verdict unknown), except that without frame timing a `gpu`-gated Finding passes where
`bound.gpu_probable` holds, with that reading as its last evidence line (`rules._probable_gpu`); when it does not
hold, the Unchecked reason gives the seconds and marks. A rule whose source was not measured is `unchecked` with
the reason.
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
  "heat or its power brake". Readings that name `gpu_idle` are left out of the heat share and of the per-event
  signal (`attribute.throttle_readings`: NVML's counters kept growing at idle, 99-100 % with no heat named), and
  a GPU idle in every reading is unchecked. The power-cap share counts a reading only where its reasons named
  `sw_power_cap` (`attribute.power_cap_readings`: on driver 591.86 the counter read 42 % over 169 readings that
  never named it), and the text says so when the counter moved without the reason.
- `cpu_throttle`: `% Performance Limit` under 100 on >= 25 % of samples, a passive thermal limit, a drop of
  >= 20 % from the first two minutes, or the level itself: a median `% Processor Performance` at or below 80 % of
  the base clock (a quiet mode set outside Windows held it at 70 % with no limit reported). A CPU held only by the
  level is titled "ran below its base clock", and "checked and fine" says the limit and the level measured, never
  "full speed".
- `ram_pressure`: free RAM under 10 % (5 % or 64 MB critical), hard faults, the commit charge over 80 / 90 %. A
  commit charge over 90 % while RAM stays free is the page file running short: free RAM is no evidence against it
  then (nor hard faults for it, being file reads), and the finding is titled for the page file.
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
- `not_focused`: the share of the capture behind another window (>= 10 %, on the CPU side), or the lift test.
  "The active window most of the time" counts against only when the lag moments did not come while the game was
  behind more often than that share says; with too few of them for the lift test it is unchecked, with the counts
  (2 of the first real capture's 3 marks fell in its one stretch behind other windows).
- With marks standing in for the frames, the marks are the events the rules judge (`_Env.judged`): a stutter
  cause they named first is unchecked with fewer than 5 ("only 3 marked moments"), and one they support is titled
  "Lag you marked ...", never "Stutters".
- `background_hogs`: processes named with their rise; `background_priority` is offered only for a (pid, name)
  pair whose create time is unambiguous, never for a process with the game's image name, with the game's PIDs in
  `exclude`, and only for a program the title names (its first three). Windows' own processes
  (`rules.WINDOWS_ITSELF`: System, the idle count, Registry, Memory Compression, Secure System, matched by exact name
  without case) stay in the evidence, but here and in `gpu_contention` they never get a priority fix or "pause or
  close" advice: titles name them "Windows itself (System)", a lag only they explain is titled "Windows itself got
  busy", and the advice says what their work means (System: a driver's work; Memory Compression: RAM running short).
- `packet_loss`: loss events, and a steady overall loss ≥ 1 % from at least 2 lost probes; never "no ping was
  lost" when some were. `server_lag`: argued only when a server flow covers most of the capture; no server flow
  is unchecked (a TCP-only or offline game); an "open" gap is a lost connection; "local" gaps are the game
  pausing its own sending. `bandwidth_hog`: checked only from a source that sees all traffic (ETW talkers, or the
  NIC counters); talkers that cover only the player's Windows session (`talkers_scope` "session", through the
  relay) can name a program but rule out only together with the NIC counters, since services run outside it: what
  the adapter moved less the game's own bytes over the network (`game_wire_bytes`) must stay under 1 MB/s.

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
  stopped answering at 13m 22s"; with zero hitches it says "no hitches ≥ 50 ms in N frames (at most 3/N at 95 %)";
  without frames it opens "Frame timing was not measured (why), so stutter and frame rate could not be checked",
  the why being the reason's first clause (`labels.short_reason`): never a path or a command, which the Not
  measured list gives once in full.

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
  fix command and the report's path are never wrapped (the path on a line of its own), no line breaks at a hyphen
  or inside a word; control characters from capture strings never reach the terminal. `check` prints a settings
  check-up instead (no verdict line, one line for what needs a recording).
- **Both reports** list the same "not measured" sources (`labels.not_measured`: the capture's own source list,
  a source whose state is "ok" but that the analysis had nothing of (`Diagnosis.measured`: "it ran but recorded
  nothing"), then the standard sources it never recorded), each named in words (`labels.SOURCES`: "the game's own
  network traffic (ETW)", never `net_flows`) with its reason, never opening with words that repeat the name
  ("frame timing was skipped:"). A cause a missing source blocked says "needs <source>, which was not recorded
  (see Not measured)" (`labels.needs_source`), grouped as "these need ..." for several. Both name the game by its
  window title, the exe beside it, and give times into the recording as "13m 22s", never "13:22" beside a
  start time (`charts.fmt_elapsed`); topics sort whatever their case. Text a player reads never carries the
  research notes' citations (`WT 8`, `LC 4.9`, `CC-5`): they stay in comments. A fix's `detail` never repeats
  the restart note the HTML report adds for `needs_restart`, nor its undo note, and no finding's title is one
  of its own evidence lines (checked by `tests/test_user_text.py` and the diagnosis tests). The HTML shows a
  `command` in backticks as code, "frame timing not recorded" instead of "0 frames", and "No lag events were
  found" only where frame timing or the network ran to look for them.
- **HTML** (`report/html.py`): one self-contained file (inline CSS + SVG, no script, no URL), light/dark via
  `prefers-color-scheme`: the player's note under the header when the capture has one ("Your note: ...", through
  `e()`, never as prose), verdict, stat tiles, frame-time chart (main swap chain only) with stutters and marks,
  CPU/GPU chart, VRAM chart, ping chart (lost pings from the collector's own record), marks on every time chart,
  finding cards, marked moments, worst moments, checked-and-fine, system snapshot, not measured. Each section
  falls back to a note on odd values instead of failing the page.
- **A measured part** (a diagnosis whose `stats["span"]` is set, `labels.measured_part`): both reports still draw the
  whole recording, every time a time into it. The header adds "10m 00s analysed" and one sentence
  (`labels.part_note`: "Analysed 10m 00s, from 3m 12s to 13m 12s of the recording (the part you chose); the rest
  is not counted, nor the 2 lag-key presses in it."). On the HTML page every time chart shades what was left out
  (class `skip`, its own neutral colour in both themes) and draws the presses outside the part apart ("not
  analysed"); the frame chart's scale and its "cut off at the top" note come from the part's frames, so a loading
  screen's long frame is drawn clamped and never named the worst. The footer's command picks the part out again
  (`--from 192 --to 792`, `html.part_args`). Without a part the reports are as before. Chart dots outside the
  chart's range are left out, as its lines, bands and vertical lines are.
- **The HTML never shows the Windows user name**: it is meant to be shared. `html.e()`, which every string on the
  page passes through (the footer, the game's exe in This PC, a reason quoting PresentMon's path, evidence),
  shows a folder under a user's profile by the variable that names it (`html.user_neutral`):
  `%LOCALAPPDATA%\lag-doctor\sessions\...`, `%APPDATA%\...`, `%USERPROFILE%\Games\...`. This PC's
  `LOCALAPPDATA`, `APPDATA` and `USERPROFILE` come first, the deepest folder winning and only as whole folders;
  then any `<drive>:\Users\<name>` (a capture from another PC, an 8.3 short name), but never the shared Public and
  Default profiles. Any case, `/` and a repr's doubled `\` count too. A path outside the profile (a custom
  `LAGDOCTOR_HOME`) is shown as it is. The footer's command is for Windows PowerShell (`html.powershell_path`):
  `lagdoctor analyze "$env:LOCALAPPDATA\lag-doctor\sessions\...\capture.ldcap.json.gz"`, with `${env:NAME}` before
  a character PowerShell would read as part of the name (`?`, `:`) and a backtick before `$`, a backtick and quotes
  in the rest. The console summary and the capture keep the real paths: the summary is the user's own terminal,
  where the commands around it print the same paths and its report path is pasted into a browser, which does not
  expand `%LOCALAPPDATA%`; the capture needs the game's exe for the GPU-preference fix.

## CLI

```
lagdoctor watch [--game NAME | --pid PID | --auto] [--duration S] [--server IP] [--no-network]
                [--elevate | --no-elevate] [--out DIR] [--hotkey KEY | --hotkey none] [--note TEXT]
lagdoctor analyze CAPTURE [--from TIME] [--to TIME]   re-run the analysis on a saved capture, or on part of it
lagdoctor check                      settings check-up without a game running
lagdoctor fix [ID ...] [--list]      apply fixes suggested by the last report (asks first)
lagdoctor undo [ID | --all]          restore from the journal
lagdoctor setup                      fetch + verify PresentMon, explain how to get ETW rights
lagdoctor app                        lag-doctor's window: recorded games and their results (see The app)
```

- Each `watch` writes `capture.ldcap.json.gz`, `diagnosis.json`, `report.html` and `summary.json` into a new folder under
  `paths.sessions_dir()` and remembers it (absolute) in `last-session.txt`; `check` writes its diagnosis into a
  `<stamp>-check-up` folder and remembers it in `last-checkup.txt`. `fix` offers what the last watch and the
  last check-up suggested, newest report per fix id, saying where each offer comes from; a remembered folder
  that is gone is reported, never replaced by another session's.
- `--note TEXT` is the player's own words about this recording ("installed FrameFix", "shadows to low"), for what the
  game-folder check cannot see (an in-game setting kept in the registry or a save file). It is cleaned as the capture
  keeps it (`model.clean_note`: each character that is not printable becomes a space, each run of spaces one, an empty
  note is none) and refused by the parser past `model.NOTE_MAX` (200) characters as kept, before anything starts,
  never cut. `_record` sets it on the capture once the session returns; the relay is never given it.
- `analyze` writes into a fresh session folder (with `analyzed-capture.txt` naming its source), never beside
  the capture, and never changes what `fix` uses. A damaged, foreign or newer-format file gets one plain line. The
  capture's note comes with it into the new report and summary.
- `analyze --from TIME --to TIME` analyses only that part of the recording (M5, "The measured part" under Analysis):
  times into the recording as the report and the status line show them (`192`, `3:12`, `1:03:12`, `3m12s`,
  `cli._time_into`, which refuses anything else before the capture is read). A missing end is the recording's own,
  an end past it is its end, and a part covering the whole recording is the whole recording. `crop.check_span`
  refuses the rest in one line, "That part of <path> cannot be analysed: <why>." (exit 2, no folder): a start
  before 0 or at the end, an end not after the start, a part under `crop.MIN_SPAN_S` (10 s). The capture file is
  never changed. `cli.analyze_and_report(cap, ..., span=(t0, t1, how))` crops before the diagnosis and hands the
  writers the whole recording.
- The relay (see The relay) writes no file; the window that started the recording saves, analyses and reports it.
  `watch` exit codes: 2 bad arguments, the game not found, or a game the relay would refuse (in another Windows
  session), 3 the game exited during the UAC prompt, 4 another recording is running, 5 its files would land in
  another app's private copy (any command that saves; see Where files go), 6 frame timing was wanted but
  PresentMon is missing or failed its check (here or in the relay), 1 the relay could not record for another
  reason (said in plain words), 130 stopped, or cancelled before the recording began (the app's Cancel).
- The live status line shows `-` for a value not in yet and `off` for a source this recording does not run
  (frame timing off, `--no-network`). It fits the console's width now (`session.console_width`): readouts drop
  from its end and it is padded, so `\r` rewrites one row (a 95-character line in a 71-column window scrolled a
  row a second); the keys are only in the "Recording." line before it. With frame timing on, one line on its
  own says "Frame timing is not working" as soon as PresentMon fails or its run ends mid-recording in anything
  but "ok" (a crash or a trace session stopped from outside settles "partial"; the game's own exit settles "ok"),
  or once the game window was in front for `session.NO_FRAMES_WARN_S` (10 s) without a first frame; time behind
  other windows does not count (a minimised game draws nothing). Frames that stop while PresentMon still runs are a frozen game, not a fault.
  Once PresentMon has ended the frame rate shows `-`, never its last value. `--no-network` records the network and
  net_flows sources as "turned off for this recording (--no-network)".
- Stopping: the first Ctrl+C stops the recording and saves it ("Stopping and saving the capture..."); later
  presses during the stop and the save are only counted. Ctrl+Break stops and saves too. Closing the window
  stops, ends the `lag-doctor-net` session at once, and waits up to 4.5 s for the save (Windows gives a closing
  console 5 s). Signing out is not covered (a process that loads user32 never gets CTRL_LOGOFF).
- For a caller other than the console (M4: the app's control channel), `cli.cmd_watch(args, *, stop=None,
  say=None, interactive=None, observers=None, launcher=None)`, each defaulting to the console's own behaviour:
  `stop` stops the recording when set through `session.request_stop` (its reason becomes `target["stop_reason"]`);
  `say` takes every line watch says, the summary too (each function on the path takes it, and defaults to `cli.say`
  as it is when that function runs, which tests replace), and must not raise: like `cli.say` for a console that went
  away, it drops a line it cannot deliver; `interactive` None asks the [Y/n] question only when
  stdin is a terminal (never without a stdin, as under pythonw), True asks anyway, and False never asks, draws no
  status line and installs no console handling; `observers` is a `session.Observers` (see Who follows a recording);
  `launcher` starts the relay elevated (default `elevation.launch_elevated`, through `relay/client.launch`);
  `cancel`, set before the recording begins, ends watch with nothing recorded (exit 130, `EXIT_CANCELLED`; see The
  control channel); `tool_alone` and `tool_roots` go to `SessionOptions` (the app's window and its WebView2
  processes). `watch --control stdio` is all of these, driven by the app (The control channel).
- `fix` applies only offered fixes, with the offered params, after showing the plan and its notes; it refuses
  to run elevated. A plan-time refusal is reported, never a traceback. Every command except `setup` first
  settles journal entries a crash left `pending`, and marks session-only changes whose process has ended as
  undone (checked without opening the process).

## The app (lagdoctor/app)

`lagdoctor app` opens lag-doctor's own window (`docs/LAUNCHER-PLAN.md`): the recorded games and their results (M1),
and a recording of the game that is running (M4). The window code is ported from M0's spike, which passed the
owner's run.

- **Start order** (`main.run`): the AppUserModel ID (`app.APP_ID`, also on the MSI's shortcut); per-monitor DPI
  awareness; one instance per lag-doctor home (the named mutex `Local\lag-doctor-app-<tag>`, the window class
  `LagDoctorApp-<tag>`, the tag 12 hex digits of the SHA-256 of the home's resolved path, so a second start brings the
  first window forward and a test's home never meets the owner's app); the refusals, said through `main.tell` (in the
  console that started it, else in a message box, as pythonw has no console): started elevated, no WebView2 Runtime, a
  runtime before build 1661 (CreateSharedBuffer) or one without the export. Another app's private copy of AppData is
  refused earlier, by `cli.main` (exit 5). Then faulthandler into `app.log` under the home (the previous run's kept as
  `app-previous.log`), STA COM, the window and the environment. If the view cannot be made, `App.on_failed` says why
  and opens the last watch's `report.html` instead.
- **The window** (`window.py`): every handler guarded and kept referenced; the settings in `window.SETTINGS`, each read
  back; the options object (exclusive data folder, crash dumps kept on the PC); WebView2's five variables set only
  around the environment's creation (`HARDENED_ENV`) and put back; each refusing handler (navigation, frame
  navigation, new window, permission, download, external scheme, script dialog) refuses first and counts what it
  refused in `refused`. The page's folder `lagdoctor/app/assets` is mapped to `https://app.lagdoctor.example` with
  access DENY; WebView2's data folder is `webview` under the home. A render process that exits gets the entry page
  again; a browser process that exits drops the view, and BrowserProcessExited (the data folder free) brings a new
  environment with a new options object. Two worker threads (at most 32 requests waiting) post their results through
  the window's own queue (`WM_APP + 1`), never to the thread.
- **The bridge** (`bridge.py`): `origin_ok`, `navigation_allowed` and `parse` are pure. A request is
  `{type: "request", id, kind, ...}` with exactly its fields: `library`; `game` (a game id); `results` and
  `open_report` (a session id); `moment` (a session id and an event id); `compare` (`a`, `b`); `settings`,
  `open_folder`, `test_sound`; `record`, `stop_recording`, `cancel_recording` and `recording` (M4, below). Notices:
  `ready`, `frames_done` (`token`). The answer is `{type: "reply", id, ok, data | error}`; a failure the app did not
  expect is logged with its traceback and answered with one sentence naming the log. The one message the app sends
  unasked is `{type: "push", kind: "recording", data}`: the running screen's newest view.
- **Recording** (`recording.py`, M4): `record` starts the recorder, the window's own child: `recording.python()`
  (pythonw.exe beside the base interpreter, as `relay/client.relay_python`) with `recording.RECORDER_HEAD` and
  `--duration` `LENGTH_S` (600), run from `recording.ROOT` (the folder holding `lagdoctor/`) with `LAGDOCTOR_HOME` set
  to the app's home and stderr in `recorder.log` there (the previous one kept as `recorder-previous.log`). A recorder
  that cannot start leaves Record free for the next press. A reader thread checks each line with
  `control.from_recorder` and posts it to the window's thread, with three messages of the app's own: `prompt {how}`
  (approved, declined, refused, failed, withdrawn), `fault {why}` and `exited {code}`. The first `hello` must name the
  child's pid, and a second one is a fault too: the app sends `cancel` and never raises the prompt for it. One
  `elevate` a recording gets a prompt; a second is answered `elevate_refused` at once. On it, a thread of its own checks
  the command with `recording.relay_problem` (argv[0] is `python()`, then `RELAY_HEAD`, the folder is `ROOT`,
  `relay.server.parse_args` takes the rest, its recorder is the child's pid and create time from a snapshot, its SID
  the app's) and answers `elevate_refused` when it fails. A Stop or Cancel sent before the prompt (or a recorder that
  has ended) means no prompt; one sent while Windows asks lets the relay go (its handle closed, `elevate_failed`).
  Otherwise it raises the prompt (`launch_elevated` with the window's HWND, STA COM on that thread) and answers
  `declined`, `elevate_failed`, or `elevated` once `ElevatedChild.hand_over` has put the relay's handle into the child
  (Popen's own handle to it). Every answer is cut to the protocol's length, so the recorder always hears one.
  `RecordingState.apply` turns each message into the running screen's state (phases `starting`, `waiting`, `asking`,
  `preparing` once the prompt is answered, `recording`, `stopping`, `saving`, `analysing`, `done`; nothing after
  `done` changes it; a failure after `saved` ends as `analysis_failed`, its session kept) and names the sounds it calls
  for; `views.recording_view` makes the screen (Cancel until `recording`, Stop while it runs, what is off: frame
  timing, the game-server check, the lag key; every `control.REASONS` in words of its own; when it failed, what the
  recorder said, less its console-only lines (`views.CONSOLE_ONLY`), and, when it ended without saying why
  (`RecordingState.unexplained`), the log's path), numbered by `seq`. Sounds
  (`sounds.py`): ask, start, mark, problem and done, 16-bit mono WAV files written once under the home's `sounds`
  folder and played with SND_FILENAME, SND_ASYNC and SND_NODEFAULT; one that cannot play is logged and left out. While
  the state is `recording`, `AppWindow.update_view` hides the view (`put_IsVisible` false, then `TrySuspend`) unless
  the window is shown (its last WM_ACTIVATE active, and not minimized): the window then draws one still line of its own
  (WM_PAINT, `HIDDEN_TEXT`, drawn again on every resize), its title reads `RECORDING_TITLE`, nothing is posted to the
  hidden page, and a view something woke is suspended again. It shows the view again (resumed) when the window is
  shown, and the app pushes
  the newest screen. A WebView2 call that fails there is logged, and the caller goes on. When the prompt is raised, or
  the recording ends, while another window is in front (`GetForegroundWindow`), the "ask" sound plays (for the prompt)
  and `want_attention` flashes the taskbar button (FLASHW_TRAY | FLASHW_TIMERNOFG) only while
  `SHQueryUserNotificationState` answers QUNS_ACCEPTS_NOTIFICATIONS: the heartbeat asks again, stops the flash
  (FLASHW_STOP) on any other answer and starts it on that one, until the window comes to the front. An answered
  prompt drops its wish (`drop_attention`). The window is never brought forward.
- **The library** (`library.py`): one Entry per session folder named as `session_dir` names them, from its
  `summary.json`, else from the folder's name (stamp and game stem). A recording is a `watch` folder holding its
  capture; check-ups and `analyze` folders are left out. A game's key is `paths.folder_stem` of its exe name, case
  folded; the page knows it only as `g-` and 16 hex digits of its SHA-256. A session id resolves only against the
  listing. `last-session.txt` marks the recording `lagdoctor fix` acts on: fix commands show on that one only.
- **summary.json** (`report/summary.py`, schema `lagdoctor.summary/1`, at most 64 KB), written by
  `cli.analyze_and_report`: `kind` (`watch` or `analyze`), `tool_version`, `capture_tool_version`, `game`, `exe`,
  `exe_name`, `started_at`, `duration_s`, `lag_type`, `headline`, `frames`, `avg_fps`, `low1_fps`, `stutters_per_min`,
  `worst_frame_ms`, `top_cause`, `frames_source`, `frames_measured`, `refresh_hz`, `power_plan`, `power_mode`,
  `launcher`; since M5 `duration_s` is the length the numbers cover (the measured part's when one was analysed), and the
  optional `recorded_s` (the whole recording's), `measured_from_s`, `measured_to_s` and `measured_how` (the part
  analysed, all None for the whole recording, and read back only when they make a part of it); and what lay beside
  the game (`summary.game_files`, from the inventory's `game`): `game_exe`
  `{state, why, bytes, modified}`, `drop_ins` and `unreal_config` `{state, why, files: [[name, sha (16 hex digits or
  null), bytes, modified], ...] (at most 30), more}`, with `state` `checked`, `not_checked` (the reason in `why`) or,
  for `unreal_config`, `not_unreal`; an Unreal game's `~mods` paks are among `drop_ins`; and `note`, the capture's
  note through `model.clean_note`. These four keys came after the schema's first build and are optional in it: a
  summary without them, or a capture from before the folder check or the note, reads back with each None. Read back
  field by field: one of the wrong type reads as None, and the note through `clean_note` again. A failed write is said
  and the analysis goes on.
- **The views** (`views.py`), plain dicts: `library_view`, `game_view`, `results_view` (the saved diagnosis through
  `Diagnosis.from_json`, analysed again only when it cannot be read), `moment_view` (`analysis.window` 5 s either side
  of an event, at most 120 s of it), `compare_view` (the numbers with the better side marked, beside the conditions:
  first "Your note", each recording's note or "-", differing only when both have one and they differ; then
  power plan and mode, refresh rate, how frames were timed, measured length, and what changed in the game itself:
  the exe by size and date, the drop-in files and the Unreal settings files by name, a name both sides hold in
  another copy (another SHA-256, else another size or date) marked "(different copy)"; a row differs only when both
  sides were read, and a summary from before the folder check shows "-"), `settings_view`, `readiness_view`.
  The note also shows on a game's recordings (`game_view`'s sessions, from the summary) and on Results (from the
  capture), each through `el()` as text.
  Chart series are named through `labels.COLUMNS`.
- **Frame times** reach the page in a read-only shared buffer (`ICoreWebView2Environment12::CreateSharedBuffer`,
  `ICoreWebView2_17::PostSharedBufferToScript`): little-endian doubles, the main chain's times then its frame times
  (NaN for none), at most 4,000,000 frames, with `{kind, session, count, request, token}` as the event's data. The page
  copies them, releases the buffer and sends `frames_done`, and the window closes it.
- **The page** (`assets/`): a CSP meta tag (`default-src 'none'`, scripts and styles from the app's own host only, no
  connections, workers or frames); text goes on the page through `el()`, which makes text nodes; uPlot 1.6.32 (MIT)
  vendored with its LICENSE and pinned by SHA-256.
- **The MSI's shortcut**: one Start-menu shortcut, `lag-doctor`, in every account's Start menu (ProgramMenuFolder under
  ALLUSERS=1), starting `[INSTALLFOLDER]pythonw.exe -m lagdoctor app` in the install folder with lag-doctor's icon
  (the Icon table, from the payload's `lagdoctor\app\lag-doctor.ico`) and `System.AppUserModel.ID` = `app.APP_ID`
  through ShortcutProperty. Its component is keyed by `HKCU\Software\lag-doctor\StartMenuShortcut` (DWORD 1).
  `msi_check`'s `registry` and `shortcut` verdicts read both back after the install (the .lnk through IShellLinkW and
  its own property store), and `gone` requires the .lnk, its icon file and the value gone, and the key as it was.

```python
# analysis/window.py
def window(cap, t0, t1, frames=None) -> dict      # {t0, t1, frames, system, gpu, network, others, marks}: each
                                                  # table's rows with t0 <= t <= t1, columns kept aligned; frames is
                                                  # the table to cut (the main chain; default the capture's own);
                                                  # ValueError unless t0 and t1 are finite and in order
```

## Testing

- **Unit**: pure functions on literal sample points; parsers against fixture text taken from real tool output
  (PresentMon CSV from replays of the gold ETL captures in PresentMon's repo); rules on synthetic captures
  built through `SeriesBuilder`.
- **Live, read-only**: ctypes wrappers run against this machine and are compared with an independent readout
  (psutil on our own process and its PID list, adapter addresses and RAM size, typeperf, `powercfg`,
  `nvidia-smi`, `Get-NetRoute`).
- **ETW without rights**: a private in-process session (allowed for standard users) proves the consumer
  path (NE 5.6); PresentMon's `--etl_file` replay proves the frames path.
- **The app's recording**: `tests/test_app_recording.py`, with `tests/fake_recorder.py` in the recorder's place (the
  recorder's side of the real control channel, through `control.Channel`, recording nothing) and a harmless process
  of the test's own in the relay's (`fake_recorder.stand_in_prompt`, handed over as the real one is); no test can reach
  the real UAC prompt. The window's own run (below) records through the same stand-ins.
- **The app**: the bridge, the library, the summary, the views and `analysis.window` in plain pytest; the window
  itself in `tests/test_app_page.py`, behind the declared `webview2` capability (CI's runner has it): a child process
  (`tests/app_harness.py`) opens the real window without the focus on a demo library of made-up games
  (`tests/app_demo.py`) and drives the page through the host's own script hook, `ExecuteScript` (WebDriver for
  WebView2 is broken since runtime 133): the library, a game named like markup shown as text, Results with every
  frame of the shared buffer drawn, a lag moment, Compare, Settings, a navigation, a new window and malformed
  messages refused; a recording through the stand-ins above, its game named like markup, with the view hidden and
  suspended (WebView2's own IsVisible and IsSuspended, read back) while the window is told it is not the active one
  and back when it is (the harness calls the window's WM_ACTIVATE branch itself, so the focus never moves); then a
  render process crashed (DevTools' `Page.crash`) and the browser process ended, each recovered.
- **WebView2's vtables**: `python -m tests.downloads fetch-webview2-idl` takes `WebView2.idl` out of
  Microsoft.Web.WebView2 1.0.4314.35's nupkg, opened only after its SHA-256 matches the pin and read by its exact name;
  the IDL has its own pin, checked on every use. `tests/webview2_idl.py` reads it, and `tests/test_webview2_idl.py`
  pins every vtable slot and IID `lagdoctor/winapi/webview2.py` names. The reader is proven offline on a literal IDL
  and against the spike's numbers counted from `WebView2.h`. The IDL's tests have their own line in the download
  tally, so the PresentMon count is unchanged.
- **Downloaded test data** (`tests/downloads.py`): the PresentMon release and its gold captures are not in the
  repo. `lagdoctor setup` and `python -m tests.downloads fetch-gold` fetch them; the fixtures check the exe
  against the tests' own pin, independent of `setup.py`'s. Without them those tests skip, except where
  `LAGDOCTOR_REQUIRE` names them (CI, which fetches and caches both): there they fail.
- **Red first**: every guard is sabotaged once and must turn its test red (`scripts/redfirst.py`). The runner
  copies the tree to a temp folder and mutates only the copy, so a killed run can never leave a sabotaged guard
  in the working tree; a test that reads the real repo by absolute path is reported UNISOLATED, and one that
  writes into its own tree stops the run.
- **No handles**: `tests/test_no_handles.py` fails if `psutil.Process`, `process_iter` or `pid_exists` is
  used outside the allow-list, and if `OpenProcess` appears outside `winapi/process.py`.
- **Standard library only**: `tests/test_stdlib_only.py` fails if a module under `lagdoctor/` imports anything
  but lagdoctor itself and the modules python.org's embeddable runtime imports (psutil named, with the reason, and
  a module that runtime leaves out or cannot import said to be one), in any form: inside a function or a try
  block, through `__import__` or `importlib.import_module`, or by a module name it cannot read. What that runtime
  imports is its own answer, `scripts/runtime_modules.json`: `scripts/runtime_modules.py` unpacks the pinned ZIP,
  has its `python.exe` look up and import every name on its standard-library list, checks the answer against the
  ZIP's listing and writes it; CI's msi job asks again and fails unless the JSON is the answer, byte for byte. The
  interpreter running the tests has the modules the runtime leaves out, so its `sys.stdlib_module_names` cannot be
  the check.
- **End to end**: `scripts/e2e.py` runs each LagLab mode with a real capture and judges the diagnosis against
  LagLab's ground-truth log (`scripts/e2e_truth.py`). It needs ETW rights, so it runs on demand: without them
  it relaunches itself once through a UAC prompt and prints the verdicts back in the original window. The
  `smooth` run also exercises the real ping and Kernel-Network ETW paths.
