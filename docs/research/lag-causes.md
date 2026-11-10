# lag-doctor rule book: causes of game lag, their signatures, thresholds and fixes

Research note, 2026-11-04. It answers four questions for lag-doctor, which watches a PC game from
outside the game process:

1. How to detect lag events in a frame-time series.
2. How to tell CPU-bound from GPU-bound from neither.
3. Each cause of low FPS or stutter: its signature, confounders, honest confidence, fixes.
4. How to combine the evidence into a ranked, honest diagnosis.

Network lag belongs to another note. §6 is a one-paragraph pointer to it. The summary table
(cause → signature → default thresholds → fix → sources) is §10. The fact-check's
"Verification log" follows it, at the end.

## How to read the tags

Every fact carries one of these tags:

- **[verified: Sx]**: checked against source Sx in §8. GitHub sources are pinned to a tag and a
  commit.
- **[verified: Lx]**: reproduced on this machine with command Lx in §9, which also has the real
  output.
- **[inferred]**: my reasoning or design choice, built on verified facts. Every threshold tagged
  [inferred] is a default to tune with real captures, not a measured constant.
- **[unverified]**: I could not check it here (the source was unreachable, or it was not tested).
  Check it before building on it.

An independent fact-check (2026-11-04, about 16:30–17:15) re-fetched every reachable source at
the pinned tag or commit and re-ran every local command. Corrections are made in place; the
Verification log at the end lists each checked claim with its outcome. Commands L21–L27 in §9
come from that pass.

**The machine at probe time** (2026-11-04, about 15:45–16:15 local time; re-checked 16:35–17:10):

- ASUS TUF Dash F15 FX517ZR [verified: L11].
- i7-12650H: 6 P-cores on logical processors 0–11 (EfficiencyClass 1), 4 E-cores on logical
  processors 12–15 (EfficiencyClass 0) [verified: L4].
- RTX 3070 Laptop GPU, driver 591.86, plus Intel UHD [verified: L4, L5].
- Only one display path was active: the internal panel, 1920x1080 at 144.003 Hz, driven by the
  **Intel** adapter [verified: L4]. The 2560x1440 external display in the task brief was not
  connected during the probes.
- A background compute job (python.exe) held the dGPU 3D engine at about 81%. The GPU sat at
  82–83 °C with *SW Thermal Slowdown* active [verified: L5, L6]. At the re-check it was still
  running (77% of the 3D engine, 97–100% `utilization.gpu`), the GPU read 75–78 °C, SW Thermal
  Slowdown was still active and `clocks.gr` was pinned at 780 MHz of a 2100 MHz maximum
  [verified: L22]. None of the "idle" readings below are truly idle.
- The re-check again found one active display path (the internal panel) and `display_attached=No`
  on the NVIDIA GPU, so the external 2560x1440 display was still not connected [verified: L21, L22].
- D3DKMT enumerates **three** adapters, not two: NVIDIA, Intel, and a software render adapter
  (LUID 0x00010EF4, `SoftwareDevice` bit set, no display sources). It also appears in the GPU
  Engine counters [verified: L21, L6].

---

## 0. Ten things the implementer must not miss

1. **PresentMon does not run unelevated here.** PresentMon 2.6.0, run unelevated by a user who is
   not in *Performance Log Users*, exits with code **6**. It prints
   `error: failed to start trace session: access denied.` to stderr, encoded UTF-16LE
   [verified: L10, S7]. When that happens, the whole frame-timing evidence family is
   **not measured**, not "fine" (§5). Exit code 6 alone does not mean "access denied": the same
   code is returned when a session with the same name is already running and for every other
   start failure. Classify by the stderr text [verified: S2 `MainThread.cpp`].
2. **The default CSV header is not what the README shows.** It has `TimeInMs` and
   `CPUStartTimeInMs` by default, or `TimeInQPC` and `CPUStartQPC` with `--qpc_time`. The README
   says `CPUStartTime` [verified: S2, S1]. Parse columns by header name. A `0` in
   `MsBetweenAppStart`, `MsCPUBusy` or `MsCPUWait` can mean "missing": the writer prints missing
   values there as 0, not `NA` [verified: S2 `CsvOutput.cpp`, S3].
3. **`MsCPUBusy` is not CPU utilisation.** It is the wall time from the end of the previous
   `Present()` to the start of this one, so an in-game frame limiter's sleep counts as "CPU busy"
   [verified: S3]. Never call a game CPU-bound from `MsCPUBusy` alone.
4. **psutil hides protected processes.** `psutil.Process(pid).threads()` silently returns an empty
   or partial list when it cannot open threads. It returned 0 threads for MsMpEng.exe and dwm.exe
   [verified: S43, L7, L23]. Use `NtQuerySystemInformation(SystemProcessInformation)` instead.
   It needs no handle, saw 78 threads in MsMpEng.exe and 39 in dwm.exe, and agreed with
   `GetThreadTimes` within 0.01 on an open thread [verified: L8, L23]. The same call also returns
   each process's I/O counters without a handle (§1.3) [verified: L24].
5. **Thread CPU time moves in 15.625 ms ticks** on this machine [verified: L9]. A 250 ms sample of
   a thread's busy fraction is only good to ±6%. Decide "saturated thread" over windows of 1 s or
   more.
6. **One saturated thread is 6.25% of this CPU.** The machine has 16 logical processors. The
   measured saturated thread showed a busy fraction of 0.99, while total CPU% moved by noise only
   [verified: L7].
7. **On this laptop's internal panel, every dGPU game pays for a cross-adapter copy** in the
   current MUX setting. The panel is driven by the iGPU [verified: L4, L21], so a game rendered on
   the dGPU has to be copied across [inferred]. The model has a MUX switch (§4.11) [verified: S52]. PresentMon's `HybridPresent` column only turns 1 if it sees the swap chain being
   created or resized *during* the capture [verified: S6]. A 0 is weak evidence.
8. **Report both "1% low" conventions, labelled.** When hitches make up less than 1% of frames,
   the percentile version hides them. In a worked example, the percentile gave 126 FPS where the
   slowest-1% average gave 29 FPS [verified: L14, S16].
9. **NVIDIA throttling has exact counters.** nvidia-smi has cumulative microsecond counters for
   five reasons (`clocks_event_reasons_counters.sw_power_cap`, `.sync_boost`,
   `.sw_thermal_slowdown`, `.hw_thermal_slowdown`, `.hw_power_brake_slowdown`). Their deltas give
   the share of each second spent throttled: while SW thermal capping was continuous, the counter
   rose by about 1,000,000 µs per 1 s sample [verified: S48, L5, L22]. There is no counter for
   `hw_slowdown`; read its flag.
10. **Score by evidence families.** Count at most one item per family in each direction. Keep
    "not measured" separate from "measured and fine". Require at least 5 events before claiming
    a stutter cause. Use the rule of three to bound a zero count (§5).

---

## 1. Data contract: what lag-doctor can read, exactly

### 1.1 PresentMon 2.6.0 console CSV

**Privilege.** PresentMon needs administrator rights, or a user in the *Performance Log Users*
group. Without either, the trace session fails with "access denied" [verified: S7, L10].

A non-elevated run with a unique session name (to avoid touching any other session) exited with
code 6 and printed this to stderr [verified: L10]:

```
warning: PresentMon requires elevated privilege in order to query processes that are
         short-running or started on another account. ...
error: failed to start trace session: access denied.
       PresentMon requires either administrative privileges or to be run by a user in the
       "Performance Log Users" user group.  View the readme for more details.
```

The re-check reproduced this exactly: exit 6, stderr starting with the BOM `FF FE` [verified: L10].

**Exit codes** (from `wmain` in `MainThread.cpp`) [verified: S2]:

- 6: the trace session could not start. The stderr reason is `access denied.`, `file not found.`,
  `path not found.`, `invalid --session_name.`, `invalid --etl_file.` or `error code N.`. The
  second line about Performance Log Users is printed only for access denied when the user is not
  in that group. Exit 6 is **also** returned when a session with the same name is already running
  and `--stop_existing_session` was not given; stderr then starts
  `error: a trace session named "<name>" is already running`.
- 7: `--terminate_existing_session` found no session of that name, or failed to stop it.
- 1: the command line did not parse, or the NVIDIA display-driver ETW manifest embedded in the exe
  failed to load.
- 3, 4, 5: the hidden message window or the hotkey could not be created or registered.
- 0: normal exit, including after Ctrl+C or a stopped session.

**Process names and exit detection need a process handle.** The console opens each presenting
process with `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` to read its name and to poll
`GetExitCodeProcess`. When that fails, the `Application` column reads `<unknown>`, and the
warning above says such processes "can't be targeted by --process_name nor trigger
--terminate_on_proc_exit" [verified: S2 `OutputThread.cpp`, `MainThread.cpp`; L10]. MsMpEng.exe
and dwm.exe already refuse that access to this user [verified: L18], and an anti-cheat-protected
game may too [unverified]. So:

- target the game with `--process_id`, and filter rows by `ProcessID`, never by `Application`;
- do not rely on `--terminate_on_proc_exit`: lag-doctor should watch the game PID itself and stop
  the capture.

**Stopping a capture cleanly.** PresentMon handles every console control event (Ctrl+C,
Ctrl+Break, close, logoff, shutdown) by stopping the recording and exiting normally. It also exits
normally when another controller stops its ETW session, because `ProcessTrace` then returns and
the consumer thread calls `ExitMainThread()` ("there may be a several second delay") [verified:
S2 `MainThread.cpp`, `ConsumerThread.cpp`]. A console-independent way to stop lag-doctor's own
capture is therefore a second run with
`--terminate_existing_session --session_name lagdoctor_<pid>` [inferred from that source; not run,
because it needs the same privilege as a capture].

**Recommended invocation.** This is my choice of flags, each of which exists in 2.6.0 [inferred;
flags verified: S1, and all present in the shipped exe's `--help` (L10)]:

```
PresentMon-2.6.0-x64.exe --process_id <pid> --output_file <csv> --qpc_time --no_console_stats
    --session_name lagdoctor_<pid> --track_hybrid_present [--track_frame_type]
```

(`--terminate_on_proc_exit` may be added as a backstop, but see above.)

Never pass `--stop_existing_session` or `--terminate_existing_session` with a session name that
another tool may own [inferred].

**The header this produces.** I derived it from `WriteCsvHeader<pmon::util::metrics::FrameMetrics>`
in the source. I could not observe it live because a capture needs elevation [verified: S2,
CommandLine.cpp in S2's commit]:

```
Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,
PresentMode,[FrameType],HybridPresent,TimeInQPC,MsBetweenSimulationStart,MsBetweenPresents,
MsBetweenDisplayChange,MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,CPUStartQPC,
MsBetweenAppStart,MsCPUBusy,MsCPUWait,MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,
MsAnimationError,AnimationTime,MsFlipDelay,MsAllInputToPhotonLatency,MsClickToPhotonLatency
```

The re-check derived the same 30 columns (29 without `--track_frame_type`) from that function and
the defaults in `CommandLine.cpp`: display, GPU and input tracking are on by default; frame type,
hybrid present, PC latency and app timing are off [verified: S2].

Header variants:

- Without `--qpc_time`, the two time columns are `TimeInMs` and `CPUStartTimeInMs`, because the
  default unit is milliseconds [verified: S2].
- With `--qpc_time_ms` the first column is labelled `TimeInSeconds` (the switch has no case for
  that unit) although the row writer prints milliseconds, and the CPU column is
  `CPUStartQPCTimeInMs` [verified: S2]. Do not use this flag.
- Optional columns, each only with its flag: `VidPnSourceId,LayerIndex` (after `PresentFlags`) and
  `PresentId` (near the end, after `FrameId`) with `--write_display_metadata`; `MsPCLatency` (after
  `MsUntilDisplayed`) with `--track_pc_latency`; `MsVideoBusy` (after `MsGPUWait`) with
  `--track_gpu_video`; `MsInstrumentedLatency` with `--track_app_timing`; `DisplayTimeAbs`
  with `--write_display_time` and `FrameId` with `--write_frame_id` (both parsed by
  `CommandLine.cpp` but not listed in `--help`) [verified: S2].
- `--v2_metrics` switches to a different header: `CPUStartTime` (or `CPUStartQPC`), `FrameTime`,
  `CPUBusy`, `CPUWait`, `GPULatency`, `GPUTime`, `GPUBusy`, `GPUWait`, `DisplayLatency`,
  `DisplayedTime`, `AnimationError`, `AnimationTime`, `MsFlipDelay`, `AllInputToPhotonLatency`,
  `ClickToPhotonLatency`, after the same leading columns [verified: S2].

**Clock alignment.** `CPUStartQPC` is a raw QueryPerformanceCounter value [verified: S2]. The
chain is: PresentMon starts its session with `Wnode.ClientContext = TIMESTAMP_TYPE_QPC` (1) and
opens it with `PROCESS_TRACE_MODE_RAW_TIMESTAMP` [verified: S2 `PresentMonTraceSession.cpp`];
for ClientContext 1 the stored time stamp is "equivalent to the value returned from the
QueryPerformanceCounter API" [verified: S49]; the CSV prints those ticks unconverted
(`%llu`) [verified: S2]. On this machine, Python's `time.perf_counter` *is*
QueryPerformanceCounter. QPF is 10,000,000, and `perf_counter_ns() == QPC × 100` with the same
epoch (the re-check measured a 1.2 µs gap between two back-to-back calls) [verified: L13, L25].
Stamp every 4 Hz system sample with `time.perf_counter_ns()`; frames and samples then share one
clock with no offset.

**Column semantics** (all times in ms) [verified: S1, S3, S4, S2]:

| Column | Meaning, from the source | Notes for rules |
|---|---|---|
| `MsBetweenPresents` | Time between this `Present()` and the previous one. | The rendered cadence. Wrong for frame pacing under DLSS-FG [verified: S17]. |
| `MsBetweenDisplayChange` | Screen time of this frame minus screen time of the previous displayed frame. `NA` if not displayed. | **What the user saw.** NVIDIA FrameView and Streamline say to use this for pacing [verified: S16, S17]. |
| `MsUntilDisplayed` | `Present()` start to screen time. `NA` if not displayed. | A dropped frame has `NA` here [verified: S2]. |
| `MsBetweenAppStart` | Written as `msCPUBusy + msCPUWait`. | The app (CPU) frame time [verified: S2]. Printed as `0`, not `NA`, when either part is missing (`WriteMetricOrZero`) [verified: S2]. |
| `MsCPUBusy` | From the previous app present's (start + time in present) to this present's start. | **Includes any wait the game does between presents**: frame-limiter sleeps, Reflex sleep, waitable-object waits [verified: S3 `CalculateCPUStart`; consequence inferred]. Missing (printed `0`) on frames that are not app frames [verified: S3]. |
| `MsCPUWait` | Time spent inside `Present()` (`timeInPresent`). | [verified: S3] Large when `Present()` blocks, as with VSync or a full queue [inferred]. |
| `MsGPULatency` | CPU start to GPU start. | [verified: S3] Grows when the render queue is deep [inferred]. |
| `MsGPUBusy` | Time with at least one GPU engine running this process's work. | The key input to the bound test (§3). `0.0` on non-app frames [verified: S3]. |
| `MsGPUTime`, `MsGPUWait` | `MsGPUWait = max(0, (ready − GPU start) − MsGPUBusy)`; the `MsGPUTime` column is written as `MsGPUBusy + MsGPUWait`. | [verified: S3, S2] |
| `MsAnimationError` | Between consecutive displayed app frames: CPU-start delta minus screen-time delta (positive = the CPU clock advanced more than the display). `NA` on non-app or undisplayed frames. | A pacing metric [verified: S1, S3 `MetricsCalculatorAnimation.cpp`]. |
| `PresentRuntime` | `DXGI`, `D3D9` or `Other`. | Vulkan and OpenGL show `Other` (see caveats) [verified: S2, S7]. |
| `MsFlipDelay` | NVIDIA driver flip delay, from the `FlipRequest` event (ID 1) of the NVIDIA display-driver provider `{AE4F8626-8265-40D1-A70B-11B64240E8E9}` (`PresentData/ETW/NV_DD.h`), which PresentMon always enables. `NA` when the delay is 0 or the frame was not displayed. | Non-`NA` on NVIDIA when the driver meters flips [verified: S2 `NvidiaTraceConsumer.cpp`, `PresentMonTraceSession.cpp`, `MetricsCalculatorDisplay.cpp`; DLSS-FG link inferred]. |
| `SyncInterval` | The value the application passed. | "this value may be modified later by the driver, e.g., based on control panel overrides" [verified: S1]. |
| `AllowsTearing` | 1 if partial frames can be displayed. | [verified: S1] |
| `PresentMode` | One of the strings below. | |
| `HybridPresent` | 1 if the swap chain's `HybridPresentMode` ≠ `NOT_HYBRID`. | The value comes from the DXGI `SwapChain_Start` / `ResizeBuffers_Start` events, so it is only set if the capture saw one of them [verified: S6]. |
| `FrameType` | `Application`, `Intel XeSS-FG` or `AMD AFMF`. `NotSet` and `Repeated` also print as `Application`, because the `DEBUG_FRAME_TYPE` define that would name them is commented out (it is not tied to debug builds). Any other value prints `Unknown`. | Needs Intel-PresentMon provider instrumentation. The enum (`NotSet 0, Unspecified 1, Application 2, Repeated 3, Intel_XEFG 50, AMD_AFMF 100`) has **no NVIDIA DLSS-FG value** [verified: S5, S2]. |

**`PresentMode` strings**, exactly as written by `PresentModeToString` [verified: S2]:

- `Hardware: Legacy Flip`
- `Hardware: Legacy Copy to front buffer`
- `Hardware: Independent Flip`
- `Composed: Flip`
- `Composed: Copy with GPU GDI`
- `Composed: Copy with CPU GDI`
- `Hardware Composed: Independent Flip`
- `Other`, for anything unknown

**PresentMon caveats:**

- **HAGS inflates GPU busy.** With Hardware-Accelerated GPU Scheduling on, "in a GPU-bound
  scenario the frame's *msGPUActive* may be reported ~0.5ms larger than the true GPU work
  duration", and the README goes on: "the specific amount of the inaccuracy will be workload- and
  GPU-dependent" [verified: S7]. The README uses the 1.x names; `msGPUActive` corresponds to
  `MsGPUBusy`, and the same note covers `msUntilRenderStart` (now part of `MsGPULatency`)
  [inferred]. Treat 0.5 ms as an example margin, not a constant. HAGS is on here
  [verified: L4, L21].
- **OpenGL and Vulkan get less detail.** These apps report runtime `Other` and have less
  instrumentation [verified: S7].
- **PSO compile metrics are not in the console CSV.** v2.6.0 added per-frame metrics for D3D12
  pipeline-state compiles: `PM_METRIC_PSO_COMPILE_COUNT`, `..._TIME`, `..._BUSY_PERCENT`. They come
  from `Microsoft-Windows-Direct3D12` ETW events `CreatePipelineStateObject_Start/Stop` (IDs
  0x9B/0x9C) [verified: S8, S9, S10]. But neither the 2.6.0 nor the 2.6.1 console sources write
  any PSO column; grep for `pso` found nothing in `PresentMon/*.cpp` [verified: S2, L16]. The
  service/API path has them; the console exe does not. The v2.6.0 release note says the PSO
  metrics are "attributed into frame/CSV capture" [verified: S9]; that must mean the service and
  Capture Application path, because the console's CSV has no such column [verified: S2; which
  CSV the note means is inferred]. The console never sets the consumer's
  `mTrackD3D12ShaderCompilation` flag, which defaults to `false`, so it does not even enable the
  Direct3D12 provider [verified: S2 `PresentMonTraceConsumer.hpp`, `PresentMonTraceSession.cpp`].
  A tool that runs its own ETW session (same privilege as PresentMon) could enable provider
  `{5D8087DD-3A9B-4F56-90DF-49196CDC4F11}`, keyword `0x2000000000000400`, events 0x9B/0x9C itself
  [inferred from S10].

### 1.2 System counters

All of these exist on this machine under these English names. They are read with
`PdhAddEnglishCounterW` [verified: L2, L1]. The explain texts are quoted from PDH [verified: L1].

| Counter (English path) | Unit / meaning (PDH explain text) | Use |
|---|---|---|
| `\Processor Information(0,N)\% Processor Time` | "percentage of elapsed time that the processor spends to execute a non-Idle thread". Idle accounting is per clock tick, so it "can... underestimate" [verified: L20]. | Per-core saturation. |
| `\Processor Information(0,N)\% Processor Utility` | "work... as a percentage of... nominal performance"; may exceed 100%. | Do not use for saturation. |
| `\Processor Information(0,N)\% Processor Performance` | "the average performance of the processor while it is executing instructions, as a percentage of the nominal performance of the processor"; may exceed 100% [verified: L26]. | Effective-frequency %. |
| `\Processor Information(0,N)\Actual Frequency` | "frequency... as measured by the OS" (MHz). | Equals `Processor Frequency × % Processor Performance` here [verified: L3]. |
| `\Processor Information(0,N)\Processor Frequency` | Requested frequency ("only reflects the frequency the OS requested"). Read 2300 on both a P-core and an E-core [verified: L3, L26]. | Not a nominal value per core class. The set also has `% of Maximum Frequency`, which PDH itself says to replace with `% Processor Performance` [verified: L26]. |
| `\Processor Information(0,N)\% Performance Limit` | "performance the processor guarantees... Performance can be limited by Windows power policy, or by the platform as a result of a power budget, overheating, or other hardware issues." | Below 100 means limited. |
| `\Processor Information(0,N)\Performance Limit Flags` | "reasons why the processor performance was limited". | Bit meanings are undocumented [unverified]. Treat any non-zero value as "limited". |
| `\Processor(N)\% DPC Time`, `% Interrupt Time`, `DPCs Queued/sec` | Aggregates only. | §4.10 |
| `\Thermal Zone Information(*)\% Passive Limit` | "A limit of 100% indicates the devices are unconstrained." | ACPI thermal throttling. |
| `\Thermal Zone Information(*)\Temperature` / `High Precision Temperature` | Kelvin / tenths of a Kelvin. The zone here is `\_TZ.THRM` and read 359 K; at the re-check 352 K and 3522 [verified: L3, L26]. | Which sensor this is depends on the OEM [unverified]. Use it as a trend only. |
| `\Thermal Zone Information(*)\Throttle Reasons` | "reasons why the thermal zone is limiting performance". | Bits undocumented [unverified]. Non-zero means limited. |
| `\Energy Meter(rapl_package0_pkg)\Power` (also `_pp0`, `_pp1`, `_dram`) | "average power over each sampling interval in milliwatts". Read 14,948 mW for the package at the time [verified: L3, L1]. | CPU package power plateau. |
| `\GPU Engine(pid_<pid>_luid_0x<hi>_0x<lo>_phys_<n>_eng_<n>_engtype_<type>)\Utilization Percentage`, `\Running Time` | Engine types seen: NVIDIA `3d, copy, legacyoverlay, ofa_0, security, videodecode, videoencode, vr`; Intel `3d, copy, gdi render, videodecode, videoprocessing`, plus one instance with an empty type [verified: L6]. A third LUID, `0x00000000_0x00010ef4` (the software adapter), has a `3d` engine used by explorer.exe [verified: L6, L21]. The instance names use **lower-case** 8-digit hex; format LUIDs read from D3DKMT or QueryDisplayConfig the same way before matching [verified: L4, L6]. | Which adapter the game renders on; GPU contention; encoders. Ignore adapters whose D3DKMT type has `SoftwareDevice` set [inferred]. |
| `\GPU Adapter Memory(luid_..._phys_0)\Dedicated Usage`, `Shared Usage` | Bytes. | VRAM pressure (whole adapter). |
| `\GPU Process Memory(pid_..._luid_..._phys_0)\Dedicated Usage`, `Shared Usage`, `Local Usage`, `Non Local Usage` | Bytes [verified: L1, L6]. | Per-process VRAM and spill. |
| `\Memory\Available MBytes` | "standby (cached), free and zero page lists". | RAM pressure. |
| `\Memory\Pages Input/sec`, `\Memory\Page Reads/sec` | Pages / read operations "to resolve hard page faults". | Hard faults. |
| `\Memory\% Committed Bytes In Use`, `Committed Bytes`, `Commit Limit` | Read 34.7 GB of 40.4 GB committed (86%) [verified: L3]. | Commit pressure. |
| `\Paging File(_Total)\% Usage` | Page-file use. | |
| `\PhysicalDisk(*)\Avg. Disk sec/Read`, `% Idle Time`, `Current Disk Queue Length`, `Disk Read Bytes/sec` | Seconds per read, and so on. Instances here are `0 c:`, `1 e:` and `_Total` [verified: L26]. | Streaming stalls. |
| `\LogicalDisk(*)\Avg. Disk sec/Read`, `% Idle Time` | The same per drive letter. Instances here are `c:`, `e:` and `_Total` [verified: L26]. | PAL's disk thresholds are defined on **LogicalDisk**, not PhysicalDisk [verified: S30]. The game's drive letter maps straight to an instance. |
| `\Process V2(*)\IO Read Bytes/sec` etc. | Per process: "reading data from file, network, or other device handles" [verified: L20]. `GetProcessIoCounters` "Retrieves accounting information for all I/O operations", but needs a `PROCESS_QUERY_(LIMITED_)INFORMATION` handle [verified: S40]. | Game reads / background I/O (not disk-only). Handle-free alternative: §1.3. |

### 1.3 Per-thread CPU of a protected game

Three findings shape how thread data must be read:

- **psutil 7.2.2 drops threads it cannot open.** On Windows, `threads()` walks a
  `CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)` and opens each thread with
  `THREAD_QUERY_INFORMATION` (not the weaker `..._LIMITED_...` right). If `OpenThread` fails, it
  executes `continue` with the comment "thread has disappeared on us" [verified: S43]. A protected
  process therefore returns `[]` instead of raising `AccessDenied`: 0 threads for MsMpEng.exe
  (78 seen by NtQSI) and dwm.exe (39 seen by NtQSI) [verified: L7, L23].
- **NtQuerySystemInformation does not need a handle.** `NtQuerySystemInformation(5 = SystemProcessInformation)`
  returns kernel and user time for every thread of every process. The full snippet is §9 L8. The
  structure layout comes from the SDK's `winternl.h`. It has a 256-byte
  `SYSTEM_PROCESS_INFORMATION` followed by 80-byte `SYSTEM_THREAD_INFORMATION` entries; the first
  two `LARGE_INTEGER` fields hold the times [verified: S44]. The names of those two fields are
  undocumented, but they matched `GetThreadTimes` (busy fraction 0.95 against 0.96; at the
  re-check 0.98 against 0.97) [verified: L8, L23]. The x64 sizes and offsets were re-derived with
  ctypes from the `winternl.h` layout: 256 bytes, `UniqueProcessId` at 80, `Reserved7` at 208;
  80 bytes per thread, `ClientId.UniqueThread` at 48 [verified: L24]. Microsoft says the function
  and its structures are "internal to the operating system and subject to change", and advises
  reaching it through run-time dynamic linking [verified: S50]. Keep it behind an interface.
- **The same snapshot carries per-process I/O counters.** `SYSTEM_PROCESS_INFORMATION.Reserved7[6]`
  (offset 208) holds `ReadOperationCount, WriteOperationCount, OtherOperationCount,
  ReadTransferCount, WriteTransferCount, OtherTransferCount` in `IO_COUNTERS` order. They were
  identical to `GetProcessIoCounters` for two openable processes, and readable for MsMpEng.exe,
  where `OpenProcess` fails with error 5 [verified: L24]. The field is undocumented ("Reserved7"),
  so treat it like the thread times: behind an interface, checked at start-up against
  `GetProcessIoCounters` on lag-doctor's own process [inferred].
- **A venv `python.exe` on Windows is a launcher.** It starts the real interpreter as a child
  process (`sys.executable` is `.venv\Scripts\python.exe`; `sys._base_executable` is
  `C:\Python313\python.exe`). Measuring the launcher's PID showed a busy fraction of 0.00
  [verified: L7]. At the re-check, `psutil.Process(launcher).children()` returned one
  `python.exe` child [verified: L23]. This matters if lag-doctor tests itself against a Python
  synthetic game. Watch the child PID.

### 1.4 nvidia-smi 591.86

Field names come from `nvidia-smi --help-query-gpu` [verified: S48]. One query on this laptop
returned [verified: L5]:

```
NVIDIA GeForce RTX 3070 Laptop GPU, WDDM, P3, 780 MHz, 2100 MHz, 5470 MHz, 6001 MHz, 83, [N/A],
40.90 W, 1.00 W, 40.90 W, [N/A], 88.93 W, 80.00 W, 105.00 W, 51 %, 9 %, 0 %, 8192 MiB, 4163 MiB,
3856 MiB, 0x00000000000001FF, 0x0000000000000020, Not Active, Not Active, Not Active, Not Active,
Not Active, Active, 116278807 us, 18990152843 us, 0 us, 4, 4, 8, 16,
[Requested functionality has been deprecated], Disabled
```

The fields were `name, driver_model.current, pstate, clocks.gr, clocks.max.gr, clocks.mem,
clocks.max.mem, temperature.gpu, temperature.gpu.tlimit, power.draw, power.draw.average,
power.draw.instant, power.limit, enforced.power.limit, power.default_limit, power.max_limit,
utilization.gpu, utilization.memory, utilization.encoder, memory.total, memory.used, memory.free,
clocks_event_reasons.supported, clocks_event_reasons.active, ...gpu_idle, ...sw_power_cap,
...hw_slowdown, ...hw_thermal_slowdown, ...hw_power_brake_slowdown, ...sw_thermal_slowdown,
clocks_event_reasons_counters.sw_power_cap, ...sw_thermal_slowdown, ...hw_thermal_slowdown,
pcie.link.gen.current, pcie.link.gen.max, pcie.link.width.current, pcie.link.width.max,
display_mode, display_active`.

The re-check ran the same fields plus `display_attached` [verified: L22]:

```
NVIDIA GeForce RTX 3070 Laptop GPU, WDDM, P3, 780 MHz, 2100 MHz, 5470 MHz, 6001 MHz, 75, [N/A],
33.21 W, 33.21 W, 33.21 W, [N/A], 88.82 W, 80.00 W, 105.00 W, 32 %, 2 %, 0 %, 8192 MiB, 4122 MiB,
3897 MiB, 0x00000000000001FF, 0x0000000000000020, Not Active, Not Active, Not Active, Not Active,
Not Active, Active, 215499825 us, 22670374809 us, 0 us, 4, 4, 8, 16,
[Requested functionality has been deprecated], Disabled, No
```

`display_mode` is documented as "Deprecated, do not use."; use `display_attached` ("whether a
physical display ... is currently connected to any of the GPU's connectors") and `display_active`
instead [verified: S48].

What this shows:

- **Event-reason bits** (from `nvml.h`) [verified: S24]:
  - GpuIdle `0x1`
  - ApplicationsClocksSetting `0x2`
  - SwPowerCap `0x4`
  - HwSlowdown `0x8`
  - SyncBoost `0x10`
  - SwThermalSlowdown `0x20`
  - HwThermalSlowdown `0x40`
  - HwPowerBrakeSlowdown `0x80`
  - DisplayClockSetting `0x100`
  - BoardLimit `0x200` and Reliability `0x400` also exist in this header, but this GPU's
    `clocks_event_reasons.supported` is `0x1FF`, so they are not reported here [verified: S24, L5].

  Here `active = 0x20` matched `sw_thermal_slowdown = Active` [verified: L5, L22].
- **HW slowdowns are severe.** `hw_slowdown`, `hw_thermal_slowdown` and `hw_power_brake_slowdown`
  each mean "reducing the core clocks by a factor of 2 or more" [verified: S48].
  The help text says `sw_thermal_slowdown` reduces clocks "because GPU temperature is higher than
  Max Operating Temp" [verified: S48]. On this laptop that wording is not what happens: the flag
  was active at 82–83 °C and again at 75–78 °C, far below the 105 °C Max Operating Temp
  [verified: L5, L22].
- **Thresholds on this laptop** (`nvidia-smi -q -d TEMPERATURE`): GPU Target Temperature 75 C,
  Slowdown 98 C, Shutdown 101 C, Max Operating 105 C. SW Thermal Slowdown was active at 82 °C
  [verified: L5], and at the re-check at 75–78 °C with `clocks.gr` pinned at 780 MHz while
  `utilization.gpu` read 97–100% [verified: L22]. So SW thermal capping here holds the **target**
  temperature [inferred from the co-occurrence; consistent across both probes].
- **`*_counters` are cumulative microseconds.** The help text: "Retrieves counters in
  microseconds for events which have reduced the frequency of clocks" [verified: S48]. Counters
  exist for `sw_power_cap`, `sync_boost`, `sw_thermal_slowdown`, `hw_thermal_slowdown` and
  `hw_power_brake_slowdown`; there is none for `hw_slowdown` [verified: S48]. The fraction of an
  interval spent throttled is `Δcounter_us / Δt_us` [inferred method]. The re-check confirmed it
  empirically: across 12 samples at `-l 1`, `sw_thermal_slowdown` rose by 996,292–1,003,077 µs
  between samples 1.002–1.020 s apart (98–100% of each interval) while the flag stayed active
  [verified: L22]. The counter seems to advance in about 1 s steps, so use windows of several
  seconds [inferred].
- **Laptop quirks:**
  - `power.draw.average` returned `1.00 W` once while `-q` showed an average of 42.29 W
    [verified: L5]. The re-check did **not** reproduce it: in 13 queries it always equalled
    `power.draw` [verified: L22]. The help text says `power.draw` already "returns average power
    draw over 1 sec" on Ampere or newer [verified: S48]. Use `power.draw`, and treat a reading at
    or below the 1.00 W `Min Power Limit` as invalid [inferred].
  - `enforced.power.limit` moved (88.93 W, then 88.54 W, then 88.82 W) above the 80 W default and
    below the 105 W maximum [verified: L5, L22]. ASUS specifies this GPU as "80W+25W Dynamic
    Boost" [verified: S52], which explains the 105 W maximum.
  - `display_active` read `Disabled` and `display_attached` read `No` [verified: L5, L22]. The
    only active panel hangs off the iGPU [verified: L4, L21].

### 1.5 Static settings readouts (all run unelevated)

| Setting | API / location | This machine | Tag |
|---|---|---|---|
| P- and E-cores | `GetLogicalProcessorInformationEx(RelationProcessorCore)` → `EfficiencyClass` | 1 = P-cores (logical processors 0–11), 0 = E-cores (12–15) | [verified: L4] |
| AC / battery / saver | `GetSystemPowerStatus` | AC=1, BatteryFlag=1, 100%, saver flag 0 | [verified: L4, S36] |
| Power plan | `PowerGetActiveScheme` + `PowerReadFriendlyName` | `64a64f24-65b9-4b56-befd-5ec1eaced9b3` "**Silent**" (OEM plan). Installed: Balanced, Silent, ASUS Recommended. | [verified: L4, L17] |
| Windows 11 power mode | `PowerGetUserConfiguredACPowerMode` / `...DCPowerMode` (powrprof.dll, Windows 11+; not in the 19041 SDK headers, but exported here) | AC `00000000-...` (`GUID_POWER_MODE_NONE`, Balanced), DC `961cc777-...` (Best power efficiency) | [verified: L4, L21, S34] |
| Effective power mode | `PowerRegisterForEffectivePowerModeNotifications(EFFECTIVE_POWER_MODE_V2=2, cb)`. "This will also be called once upon registration to supply the current mode"; callbacks "can be called concurrently", on another thread. Keep a reference to the ctypes callback and unregister with `PowerUnregisterFromEffectivePowerModeNotifications`. | 2 = Balanced (again at the re-check). Enum: 0 BatterySaver, 1 BetterBattery, 2 Balanced, 3 HighPerformance, 4 MaxPerformance, 5 GameMode, 6 MixedReality. | [verified: L4, L21, S35, S44] |
| GPUs, hybrid role, HAGS | `D3DKMTEnumAdapters2` + `D3DKMTQueryAdapterInfo(KMTQAITYPE_ADAPTERTYPE=15, KMTQAITYPE_WDDM_2_7_CAPS=70)`; names via `KMTQAITYPE_ADAPTERREGISTRYINFO=8` | NVIDIA LUID 0x00010F83 `HybridDiscrete`, `HwSchEnabled=1`. Intel 0x00010C49 `HybridIntegrated`. A third adapter, 0x00010EF4, has type 0x105 (`RenderSupported`, `SoftwareDevice`, `ACGSupported`), 0 sources, and its registry-info query fails with 0xC0000034 (STATUS_OBJECT_NAME_NOT_FOUND). | [verified: L4, L21, S44] |
| HAGS registry value | `HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\HwSchMode` | 2 | Value read [verified: L4]. That 2 means on and 1 means off [unverified]. Prefer the D3DKMT bit. |
| Display path, panel type, refresh rate | `QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS=2)` + `DisplayConfigGetDeviceInfo` (type 1 = source name, 2 = target name) | `\\.\DISPLAY1`, tech INTERNAL (0x80000000), source and target adapter = Intel LUID, 342060000/2375360 = 144.003 Hz. Same at the re-check. | [verified: L4, L21, S44] |
| Refresh rates offered | `EnumDisplaySettingsExW` (iterate modes at the current resolution) | Current 144 Hz; offered [60, 144] | [verified: L4] |
| Per-app GPU preference | `HKCU\Software\Microsoft\DirectX\UserGpuPreferences`, value = full exe path, data `GpuPreference=N;` | ASUS background tools set to `GpuPreference=1;` (one as `'GpuPreference=1; '` with a trailing space). Some game exes carry `AppStatus=N;` (0 and 4 seen). The key also holds a non-path DWORD value, `GraphicsFeaturesNotificationConfig = 1`. | Values read [verified: L4, L21]. Meaning of N (0 let Windows decide, 1 power saving, 2 high performance) is community-documented only [unverified]; it matches the numbering of the documented `DXGI_GPU_PREFERENCE` enum (0 unspecified, 1 minimum power, 2 high performance) [verified: S51; the link is inferred]. `AppStatus` meaning [unverified]. A parser must skip non-path values and tolerate spaces. |
| Game DVR | `HKCU\System\GameConfigStore\GameDVR_Enabled` | 1. No `AppCaptureEnabled` or `HistoricalCaptureEnabled` under `...\CurrentVersion\GameDVR`. | Values read [verified: L4]. Semantics [unverified]. |
| Game Mode | `HKCU\Software\Microsoft\GameBar\AutoGameModeEnabled` | Absent (the default applies) | Read [verified: L4]. Default and meaning [unverified]. Measure it instead through the effective power mode `GameMode` (5) while the game runs. |
| Memory Integrity / VBS | WMI `root\Microsoft\Windows\DeviceGuard:Win32_DeviceGuard` | `VirtualizationBasedSecurityStatus=2` (running). `SecurityServicesRunning=2` (memory integrity running). `AvailableSecurityProperties` includes 7 (MBEC). Works unelevated, although the doc says to use an elevated shell. | [verified: L11, S31] |
| HVCI registry value | `HKLM\...\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity\Enabled` | 1 | [verified: L4, S31] |
| Virtual Machine Platform | WMI `Win32_OptionalFeature` `InstallState` (1 Enabled, 2 Disabled, 3 Absent, 4 Unknown) | `VirtualMachinePlatform=1`. `Get-WindowsOptionalFeature` needs elevation. | [verified: L11, S32] |
| Game drive type | `MSFT_PhysicalDisk` (`root\Microsoft\Windows\Storage`): MediaType 3 HDD, 4 SSD, 0 Unspecified; BusType 17 NVMe | Both disks MediaType 4, BusType 17. `Get-Partition` maps C: to disk 0 and E: to disk 1. | [verified: L11, S38] |
| Defender scan settings | `Get-MpPreference` | ScanAvgCPULoadFactor 50, ScanOnlyIfIdleEnabled True, daily (0) at 02:00 | [verified: L11, S39] |
| Foreground window | `GetForegroundWindow` + `GetWindowThreadProcessId` + `IsIconic` | Works across processes unelevated | [verified: L15] |
| EcoQoS / priority of a process | `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetProcessInformation(ProcessPowerThrottling=4)` + `GetPriorityClass` | Worked on explorer.exe (masks 0/0, priority 0x20) and OneDrive.exe. msedgewebview2.exe read `ControlMask=0x4, StateMask=0x0`, priority 0x8000 (ABOVE_NORMAL). **Access denied (5)** on MsMpEng.exe, dwm.exe and SearchIndexer.exe. `OpenProcess(PROCESS_SET_INFORMATION)`, the right `SetProcessInformation` needs, succeeded for the same-user processes only (handles opened and closed, nothing set). | [verified: L18, L27, S44, S54] |

---

## 2. Detecting lag events in a frame-time series (question 1)

### 2.1 Which series to analyse

- **Use `MsBetweenDisplayChange` for "what the player saw".** NVIDIA FrameView 2.0 says
  MsBetweenDisplayChange (Displayed FPS) "should be used to chart displayed FPS", and calls
  MsBetweenPresents the "Rendered FPS" from the start of the pipeline [verified: S16]. The DLSS-G
  guide says `MsBetweenPresents` "is not suitable for measuring frame pacing quality because DLSS-G
  uses specialized hardware to delay the image after Present()" [verified: S17].
- **Use `MsBetweenAppStart` (CPU frame time) and `MsBetweenPresents` to reason about causes**
  (§3), because they describe the app's own cadence [inferred].
- **Drop not-displayed frames** (`MsUntilDisplayed == NA`) from the displayed series. Count them
  separately [verified: S2]. With tearing allowed and FPS above refresh, frames are routinely
  "dropped" because they were replaced before scan-out. That is not lag on its own [inferred].

### 2.2 What reputable tools and sources do

| Source | Definition | Tag |
|---|---|---|
| **CapFrameX** v1.9.1 | **Stutter**: frame time > `StutteringFactor × moving average`. The default factor is **2.5**. The moving average is *trailing* over `N = Convert.ToInt32(√avg_ms × 10)` samples (avg = the whole run's mean frame time), about 41 frames at 60 FPS and 26 at 144 FPS; the window **includes the current frame**. Inside the window, a sample > 3× its predecessor is replaced by the predecessor. Reported as % of time (moving average). The **count %** (`GetStutteringCountPercentage`) instead compares each frame with 2.5 × the run's overall average. **Low-FPS time**: non-stutter frames below `StutteringThreshold` = **25 FPS**. | [verified: S11, S12, S13] |
| CapFrameX | **Variance**: consecutive frame-time differences, binned as <2, <4, <8, <12 and ≥12 ms. | [verified: S11] |
| CapFrameX | **P1** = 1st-percentile FPS (MathNet R-8 quantile). **1% low average** = mean of frame times ≥ the 99th-percentile frame time, as FPS. **1% low integral** = "equivalent... to MSI Afterburner": the frame time at which the cumulative time of the slowest frames reaches 1% of total time. Also 0.2% and 0.1% variants. Adaptive std dev uses a 500 ms time window. | [verified: S11, S12, S14] |
| **OCAT** v1.6.3 | Summary: average FPS = frames / seconds, plus frame-time percentiles 0.1, 1, 5, 25, 75, 95, 99 and 99.9. The percentile is linear interpolation, `rank = p/100·(n−1)+1` (R-7, numpy's default); it returns −1 for fewer than 2 frames. `Recording.cpp` has no stutter metric (no occurrence of "stutter"). | [verified: S15] |
| **NVIDIA FrameView 2.0** | **1% Low FPS** "takes the slowest 1% frames and averages them"; NVIDIA calls it the best quick stutter metric. **1% FPS** = "The FPS separating the slowest 1%... from the fastest 99%" (a percentile). Also 0.1% Low, and 0.1/1/5/10% FPS percentiles. Average FPS uses displayed frames. | [verified: S16] |
| **Intel PresentMon** | Per-frame `MsAnimationError` (pacing). FPS-Presents, FPS-Display and FPS-App indicators, which differ under frame generation. 2.6 adds a "Game Experience" overlay preset. | [verified: S1, S9] |
| **Unreal Engine** | The engine marks a PSO compile as a hitch when it takes longer than a threshold set by `r.PSO.RuntimeCreationHitchThreshold`: "The default threshold is 20 milliseconds." | [verified: S21] |
| Tech Report "Inside the second" (2011) | "Time spent beyond 50 ms" | [unverified]: the article URL now 404s, and web.archive.org is blocked for my fetcher. |
| Digital Foundry | Judges by frame-time graphs and separates "shader compilation stutter" from "traversal stutter". No numeric definition found. | [unverified]: secondary sources only. |

### 2.3 How big a hitch players perceive

- **Platformer study, FDG '26** (Tokey, Boudaoud, Spjut, Claypool; 31 players; spikes of 0, 75,
  150 and 225 ms; a 2D platformer at a fixed 60 f/s on a 240 Hz monitor; each spike was triggered
  a random delay after a task event) [verified: S19]:
  - "frametime spikes smaller than 50 ms will always be acceptable, whereas frametime spikes
    225 ms or larger will always be unacceptable".
  - Tasks that need precise timing show "a more dramatic drop in QoE once spike magnitudes
    surpass 150 ms". This is said of precise-timing tasks, not of all tasks. Success rates
    dropped for spikes of 150 ms or more, not for 75 ms.
  - Spikes of 75 ms had mean QoE "just under 4", which led to the advice to keep spikes "below
    about 50 ms".
  - The participants' mean reaction time was 211.86 ms (SD 47.64).
  - The authors note fixed spike sizes and action-triggered spikes as limitations [verified: S19].
- **Frame-rate variation study, CHI '23** (Liu, Claypool, with Intel; 33 players; 60 and 120 Hz)
  [verified: S18]:
  - Average frame rate alone is a poor predictor of QoE.
  - Frame-time standard deviation predicts the overall average but not every game.
  - The **95% frame-rate floor** (the 5th-percentile FPS) predicts QoE overall and per game.
  - "QoE is always acceptable when the 95% frame rate floor is within about 80% of the target
    frame rate." The paper also says a floor within about 40% of the target "will almost always
    be acceptable", and limits the model to the 60–120 Hz range it tested.
  - The variation came from CPU-bound Python "counter" processes (4 and 8 in its example figures)
    competing with the game under an RTSS frame cap; frame times were PresentMon's time between
    display changes. That is background CPU interference (§4.6) [verified: S18].
- **Variable frame timing, Klein, Spjut, Boudaoud, Kim 2023:** "frequent but relatively small
  (4-12 ms)" variations around refresh rates of 30–240 Hz, studied in the VRR context in an FPS
  game, affected perceived smoothness, but not task completion time at equal rated smoothness
  [verified: S20].

### 2.4 Recommended defaults

The thresholds are [inferred] except where a source is named.

```
series  D = MsBetweenDisplayChange over displayed frames (fallback: MsBetweenPresents if display
            tracking is off); A = MsBetweenAppStart
baseline M_i = median of the previous N frames of D, N = clamp(round(10*sqrt(median_ms)), 20, 120)
            (CapFrameX window sizing [S11]; median instead of CapFrameX's guarded mean for robustness)

per-frame events (D_i):
  stutter      : D_i >= 2.5*M_i  AND  D_i - M_i >= 8 ms          (2.5x: CapFrameX [S12]; 8 ms: middle of
                                                                   the 4-12 ms band of [S20])
  hitch        : D_i >= 50 ms    AND  D_i >= 2*M_i                (50 ms: [S19])
  severe hitch : D_i >= 150 ms                                    ([S19] success rates fall from 150 ms;
                                                                   precise-timing QoE drops sharply past it)
  freeze       : D_i >= 500 ms
  -> merge events closer than 250 ms (one 4 Hz system sample) into one episode; keep max D, start, end

windowed events:
  low-FPS episode : 1 s rolling displayed FPS < 0.80*target for >= 2 s   (80%: [S18])
        target = cap if a cap is detected, else refresh rate if VSync-locked, else the run's p90 FPS
  pacing episode  : over 2 s, > 10% of |D_i - D_(i-1)| >= 4 ms (CapFrameX bins [S11]), or
                    p95(|MsAnimationError|) > 2 ms
  VSync judder    : VSync on, no VRR (§3.3), median D == 1 refresh period and >= 10% of intervals at
                    2 periods; more generally, holds alternating between k and k + 1 periods with
                    >= 10% on the side away from the usual one, at any k (58 FPS on 144 Hz: 2P and 3P)
                    [inferred: the same one-refresh swing, measured on a real recording 2026-11-09]
```

Summary numbers for every run:

- **avg FPS** = frames ÷ total time (OCAT's definition [verified: S15]).
- **1% low** and **0.1% low**, as the *average of the slowest* 1% and 0.1% (FrameView [S16]).
  Take exactly `ceil(p·n)` frames. Do not take "all frames ≥ quantile": with repeated values that
  sweeps in every frame. My constant-series test gave a "1% low" equal to the average
  [verified: L14].
- **P1 FPS** (= 1000/P99 frame time, R-7) and **P5 FPS** (the "95% floor" of CHI '23). Label
  both as percentiles. CapFrameX instead takes the R-8 quantile of the per-frame FPS values,
  which can differ slightly because of the interpolation [verified: S11, S14].
- **Stutter time %**, CapFrameX-compatible.
- **Hitches per minute** by class.
- **Time beyond 50 ms** = Σ(D_i − 50) over D_i > 50.

**User hotkey marks.** Search `[mark − 3 s, mark + 0.5 s]` for events. A player presses a key some
hundreds of milliseconds after noticing; the measured mean reaction time is 212 ms [verified: S19].
The window size itself is [inferred].

### 2.5 The two "1% low" conventions, worked example

Same data, different answers [verified: L14]:

```
A: 1990 frames ~6.94 ms + 10 hitches of 60 ms (0.5%)
   average FPS (frames / total time)        138.7
   '1% FPS' percentile = 1000/P99 (R-7)      126.2     <- hides the hitches
   '1% Low' = avg of slowest 1% of frames     29.4
   '1% low integral' (time-weighted)          16.7
   '0.1% Low' = avg of slowest 0.1%           16.7
B: 1960 frames ~6.94 ms + 40 hitches of 60 ms (2%)
   every low metric                            16.7
```

The percentile convention only "sees" hitches once they make up more than 1% of frames. This is
the gap FrameView describes ("the old 1% FPS metric would miss capturing the worst stutter")
[verified: S16].

The re-check reproduced case A independently (jitter SD 0.4 ms, seed 1): 138.6 / 126.1 / 29.4 /
16.7 / 16.7, and case B: 16.7 for every low metric [verified: L14]. With a constant base frame
time, CapFrameX's "≥ R-8 quantile" selection returned 138.7 FPS, the run average, while the
`ceil(1%·n)` average returned 29.9 [verified: L14]. Ties are not exotic: VSync-locked
`MsBetweenDisplayChange` values cluster within a few QPC ticks of whole refresh periods
[inferred], so a tie-sweeping implementation can drift towards the average on VSync-locked runs.

### 2.6 Minimum sample sizes

All of these are [inferred] arithmetic, except the rule of three.

- A P99 or 1%-low figure needs at least 1,000 frames (at least 10 frames in the tail). A 0.1%
  figure needs at least 10,000 frames, which is about 70 s at 144 FPS. Below that, print
  "not enough frames", not a number.
- A claim of no hitches needs a bound. With 0 hitches in n frames (n > 30), the 95% upper bound
  on the hitch rate is 3/n: for example, at most 0.03% in 10,000 frames [verified: S45].
- A bound classification (§3) needs at least 10 s and at least 300 app frames of steady state,
  excluding the frames of events.
- An event-correlated cause (§5) needs at least 5 events, with at least 3 of them co-occurring
  with the signal.

---

## 3. CPU-bound vs GPU-bound vs neither (question 2)

### 3.1 The GPU Busy rule

Intel introduced GPU Busy so that frame time can be compared with the time the GPU actually
works:

- If frame time is well above GPU Busy, the GPU waits on something else: usually the CPU, or a cap.
- If GPU Busy is close to frame time, the GPU is the limit.

Sources: the PresentMon definition of `MsGPUBusy` [verified: S1]. Intel's own PresentMon page
says only that the "GPU Busy" metric shows "real time CPU + GPU balance" for evaluating
bottlenecks; it states no numeric rule [verified: S53]. The frame-time comparison is Intel's
framing as reported by Club386 on 2023-08-18 [verified: S23, secondary]. Club386 also shows the
most important confounder: in its Enlisted test GPU Busy was far below frame time, "which is
correct since I capped frame rates to 140fps" [verified: S23].

Default rule, per steady window of at least 10 s with event frames excluded [inferred thresholds]:

```
FT = median(MsBetweenAppStart > 0)      G = median(MsGPUBusy) - (0.5 ms if HAGS on [S7]; an example
                                            margin, "workload- and GPU-dependent")
r  = G / FT
GPU-bound      : r >= 0.90  (or FT - G <= 1.0 ms)
CPU-or-other   : r <= 0.75
borderline     : 0.75 < r < 0.90  -> report as "mixed", never as a firm verdict
```

### 3.2 Confirming with thread and utilisation data

**Total CPU% misleads.** On 16 logical processors, one saturated thread is at most 6.25% of the
machine. In the measured case, system CPU% read 46.0% against a 44.9% baseline: noise from the
background job. The thread itself showed a busy fraction of **0.99** of one logical processor
[verified: L7]. The busiest core read 81% because Windows migrates threads between cores
[verified: L7 output; cause inferred]. The re-check gave the same picture: busy fraction 0.98
(NtQSI) / 0.97 (psutil), system CPU% 20.8%, busiest core 85.5% [verified: L23].

Confirm "CPU-or-other" before calling it CPU-bound [inferred thresholds]:

- **Main-thread bound:** the game's busiest thread runs at ≥ 0.85 of a logical processor over
  windows of 1 s or more (from NtQSI deltas, §1.3).
- **All-core bound:** game process CPU ≥ 0.85 × number of logical processors (rare).
- **Neither:** the game's busiest thread is below 0.6 *and* r is low. That points to "waiting"
  (cap, VSync, synchronisation, I/O, a stall in the driver or OS), not to "CPU too slow".

Which core class runs the thread (P or E) is **not observable** from outside without ETW
[inferred]. Do not claim "main thread on an E-core". At most, note when the game is not the
foreground window, because Windows' QoS rules then lower its class (§4.19) [verified: S33].

### 3.3 "Neither-bound": frame caps, VSync, VRR

When something other than the CPU or GPU paces the frames, PresentMon shows it like this
[inferred tests from verified fields]:

- **VSync on, fixed refresh (no VRR).**
  - `SyncInterval ≥ 1` and `AllowsTearing = 0` (SyncInterval is the app's value; the driver may
    override it [verified: S1]).
  - Displayed intervals cluster on whole multiples of the refresh period P = 1000/refresh. Take
    P from `QueryDisplayConfig`'s rational: 6.9443 ms here [verified: L4].
  - Test: share of `D_i` within ±max(0.25 ms, 4% of P) of k·P is ≥ 90%. Continuous (VRR-like)
    intervals land in that band by chance only about 7% of the time (0.5/6.94).
  - The waiting shows as large `MsCPUWait`, or as a deep queue (`MsGPULatency` growing).
- **VRR active** (G-SYNC or FreeSync): below the refresh rate, displayed intervals follow the
  presented intervals continuously and are *not* quantised. At the ceiling they quantise again.
- **Frame cap (in-game, driver or RTSS).**
  - `MsBetweenPresents` is very regular: coefficient of variation < 3% over 5 s, at a value C
    that is not within 3% of k·P.
  - r < 0.75 and the busiest thread < 0.85.
  - With an in-game limiter, the sleep lands in `MsCPUBusy` [verified: S3]. The CPU *looks*
    busy, but the thread CPU time shows it is not.
- **Present mode.**
  - `Composed: Copy with GPU GDI` and `Composed: Copy with CPU GDI` are blt-model windowed
    presents, composed by DWM.
  - `Composed: Flip` is windowed flip, also composed.
  - `Hardware: Independent Flip` and `Hardware Composed: Independent Flip` bypass composition
    [verified: S1 mode descriptions; S26: DirectFlip/Independent Flip can "bypass desktop
    composition entirely"].
  - Microsoft says flip model "will give better performance, lower power usage". Blt presents at
    high framerates across hybrid GPUs need "additional work" that "may affect overall performance"
    [verified: S26].

### 3.4 Classifier, in order

The order and thresholds are [inferred].

```
if frame family not measured           -> bound = "unknown" (fall back to engine utilisation: game 3D engine
                                           >= 95% on its adapter => "GPU-limited (probable)")
elif VSync-quantised and FT ≈ k*P      -> "display-limited (VSync)"   [check judder, refresh rate, VRR]
elif cap-regular and r < 0.75 and busiest_thread < 0.85 -> "frame-capped at 1000/C FPS"
elif r >= 0.90                          -> "GPU-bound"                 -> §4.1-4.3, 4.11
elif r <= 0.75 and busiest_thread >= 0.85 -> "CPU-bound (main thread)" -> §4.4-4.6, 4.19
elif r <= 0.75 and process_cpu >= 0.85*NLP -> "CPU-bound (all cores)"
elif r <= 0.75                           -> "waiting (not CPU, not GPU)" -> §4.7-4.10, stalls
else                                     -> "mixed"
```

---

## 4. Cause catalogue (question 3)

Each cause lists its signature, confounders, honest confidence, an automated fix (only if it is
reversible and done with consent) and manual advice.

- "Steady" means a 10 s or longer window.
- "Event-linked" means correlated with the events of §2 by the lift test of §5.2.
- All thresholds are [inferred] unless they carry a source.

### 4.1 GPU at its limit (settings or resolution too high)

**Signature:**

- Bound is "GPU-bound" (r ≥ 0.90).
- Game 3D engine utilisation ≥ 90% on its render adapter, or `utilization.gpu` ≥ 95%.
- No throttling: no `sw_thermal_slowdown` or `hw_*` reasons, and `clocks.gr` ≥ 0.9 × the run's
  own p95 clock.
- `sw_power_cap` active is normal for a GPU-bound laptop. It is not a fault [inferred].

**Confounders:**

- HAGS adds about 0.5 ms to GPU Busy in the README's example; the amount is "workload- and
  GPU-dependent" [verified: S7].
- A background GPU user (§4.6): on this machine, an ML job held the 3D engine at 81% [verified: L6].
- Frame generation (§4.16).
- VRAM spill (§4.3) also raises GPU time.

**Confidence:** high when PresentMon is measured; "probable" from utilisation alone.

**Auto-fix:** none. In-game settings live inside the game.

**Manual advice:**

- Lower the render resolution, or use an upscaler preset (DLSS/FSR/XeSS).
- Reduce the heaviest settings (ray tracing, shadows, volumetrics).
- Cap the frame rate a few FPS below refresh with VRR (§4.14).

### 4.2 GPU thermal or power throttling (laptop)

**Signature:**

- GPU-bound windows in which `Δclocks_event_reasons_counters.sw_thermal_slowdown / Δt ≥ 0.3`, or
  any `hw_thermal_slowdown` or `hw_power_brake_slowdown` counter time, or the `hw_slowdown` flag
  in any 1 Hz sample (it has no counter). The HW reasons each mean clocks reduced by a factor of
  2 or more [verified: S48].
- `temperature.gpu` ≥ the target temperature from `nvidia-smi -q -d TEMPERATURE` (75 °C here
  [verified: L5, L22]). On this laptop SW thermal capping engaged at the target temperature, not
  at the documented Max Operating Temp (§1.4).
- `clocks.gr` falls ≥ 15% below the run's first-minute p95 while utilisation stays ≥ 95%.
- Event-linked variant: FPS sags in step with clock drops.

**Power-limit variant:**

- `sw_power_cap` is active with `enforced.power.limit < power.default_limit`, for example on
  battery or in an OEM quiet mode [inferred]. Here enforced was 88.9 W against an 80 W default
  [verified: L5], within ASUS's "80W+25W Dynamic Boost" envelope [verified: S52], so on AC an
  enforced limit above the default is normal here.

**Confounders:**

- `sw_power_cap` during GPU-bound play is the design limit, not throttling.
- Idle clocks are low by design (`gpu_idle`).
- nvidia-smi is sampled at 1 Hz, so a dip shorter than a second may show only in the `*_counters`
  deltas, not in the instantaneous bitmask [inferred].

**Confidence:** high on NVIDIA. NVML exposes the reasons directly [verified: S24, S48]. Intel and
AMD paths are not covered by nvidia-smi.

**Auto-fix:** none safe from user mode. Raising power limits needs admin and changes hardware
behaviour [inferred].

**Manual advice:**

- Raise the laptop for airflow; clean the vents.
- Use the OEM "performance" mode instead of "silent". The active Windows plan here is the OEM
  plan named "Silent" [verified: L4].
- Plug in AC.
- Cap FPS to reduce heat.

### 4.3 VRAM overflow (spill to shared memory)

**Signature:**

- Adapter `Dedicated Usage` ≥ 90% of VRAM (`memory.total` 8192 MiB here [verified: L5]).
- The game's `\GPU Process Memory(pid_<game>_luid_<dGPU>...)\Shared Usage` > 512 MiB, or rising
  ≥ 256 MiB per minute, while its dedicated usage is pinned.
- Event-linked hitches rise after the spill begins.

Microsoft: when a process's usage exceeds its OS budget, the app "may incur stuttering or
performance penalties" [verified: S25]. The budget is per process and only visible inside the
game. From outside, use the adapter totals as a proxy [inferred].

**Confounders:**

- Other processes' VRAM. On this machine, the ML job (3.87 GB), DWM (570 MB) and a WebView2
  process (184 MB) all held NVIDIA dedicated memory [verified: L19].
- Integrated GPUs normally use shared memory.
- A rise right after loading is normal residency.

**Confidence:** medium. The budget itself is not observable from outside [inferred].

**Auto-fix:** none by default. Offer to *close* named non-game GPU-memory users, with consent.
That is not reversible within the session [inferred].

**Manual advice:**

- Lower texture quality and resolution, or turn off ray tracing or frame generation (FG uses VRAM
  [unverified]).
- Close GPU-heavy apps.

### 4.4 CPU main-thread limit

**Signature:**

- Bound is "CPU-bound (main thread)": r ≤ 0.75 and the busiest thread ≥ 0.85 of a logical
  processor over ≥ 1 s windows, measured through NtQSI.
- No cap or VSync pattern.
- Clocks normal (§4.5 negative).

**Confounders:**

- Frame limiters: their sleeps fill `MsCPUBusy` [verified: S3].
- Shader compilation and streaming: those are bursts, not steady.
- Background CPU load: compare against the game's own threads.
- psutil's empty thread list: that means "not measured", not "idle" [verified: L7].

**Confidence:** high with thread data; "probable" without it.

**Auto-fix:** none. Changing the game's priority or affinity touches the game process and may
trip anti-cheat [inferred].

**Manual advice:**

- Lower CPU-heavy settings (crowd density, view distance, simulation detail, physics).
- Turn on frame generation only if the base FPS is acceptable; latency stays tied to the base
  rate [inferred].
- Close CPU-heavy background apps.

### 4.5 CPU thermal or power throttling (laptop, hybrid P/E cores)

**Signature** (CPU-bound or near it, busiest thread ≥ 0.7), plus any of:

- `% Performance Limit` < 100 on ≥ 25% of samples on busy cores. This counter covers power
  policy, power budget and overheating [verified: L1 explain text].
- `Performance Limit Flags` ≠ 0 [unverified bit meanings].
- `\Thermal Zone Information(*)\% Passive Limit` < 100 [verified meaning: L1].
- `% Processor Performance` on busy P-cores ≥ 20% below the run's first two minutes at a
  similar load.
- The package power counter `Energy Meter(rapl_package0_pkg)\Power` flat-tops at a constant
  value while frequency falls (a power limit).

**Confounders:**

- The Windows power plan and power mode (§4.12).
- Battery.
- Low load lowers frequency legitimately. Gate on load.
- E-cores have their own clocks; compare each core with its own earlier readings
  [inferred, L3].

**Confidence:** medium. There is no CPU temperature without a driver, and the bits are
undocumented [inferred].

**Auto-fix:** reversible plan or mode changes only (§4.12).

**Manual advice:** cooling, AC power, OEM performance mode, and capping FPS.

### 4.6 Background interference (CPU, disk, GPU)

**Signature (event-linked or steady):**

- **CPU:** non-game processes together use ≥ 2 logical processors' worth, or any one uses ≥ 1,
  co-occurring with events (lift ≥ 2).
- **Disk:** a non-game process does ≥ 20 MB/s on the game's disk while the game's reads stall
  (§4.8).
- **GPU:** a non-game process uses the game adapter's `3d` engine ≥ 10% or `videoencode` > 0 (see
  §4.15).

**Known offenders.** These executables exist on this machine: TiWorker.exe, TrustedInstaller.exe,
usoclient.exe, CompatTelRunner.exe, SearchIndexer.exe, MsMpEng.exe, MpCmdRun.exe, steam.exe,
steamwebhelper.exe, EpicGamesLauncher.exe, `NVIDIA Overlay.exe` [verified: L12]. These services
were running: wuauserv, DoSvc (Delivery Optimization), BITS, TrustedInstaller, UsoSvc, WSearch,
WinDefend [verified: L12]. Steam also downloads "Shader pre-caching update" content [verified: S46].

**Defender:**

- Scheduled scans default to an average CPU guidance of 50% (`ScanAvgCPULoadFactor`), "not a hard
  limit" [verified: S39].
- This machine scans daily at 02:00, only when idle [verified: L11].
- Real-time scanning of game file reads is the likelier in-game interference [inferred].

**Confounders:**

- The game's own helper processes (launchers, crash handlers, anti-cheat services).
- `MsMpEng.exe` and `dwm.exe` cannot be opened, even with QUERY_LIMITED access [verified: L18].
  Their CPU still comes from NtQSI or counters.

**Confidence:** high when the lift is strong; otherwise possible.

**Auto-fix (reversible, with consent):** lower the *offending* non-game process.

- Use `SetPriorityClass(BELOW_NORMAL_PRIORITY_CLASS=0x4000)`, or EcoQoS through
  `SetProcessInformation(ProcessPowerThrottling=4, {Version=1, ControlMask=1, StateMask=1})`
  [constants verified: S44, S54; QoS semantics S33]. Both need a handle with
  `PROCESS_SET_INFORMATION` [verified: S54].
- Record the original priority and **both masks exactly** first, and restore them when the game
  exits. Masks need not be 0/0: msedgewebview2.exe already had `ControlMask=0x4` (a mechanism the
  19041 SDK does not name; the current docs describe
  `PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION`, whose value they do not print) [verified:
  L27, S54; the 0x4 ↔ name link is unverified]. Restoring "system managed" (0/0) would discard it.
- Microsoft: EcoQoS "should not be used for performance critical or foreground user experiences"
  [verified: S54]. That is one more reason never to apply it to the game.
- **What an unelevated lag-doctor can actually act on:** only processes of the same user. On this
  machine `OpenProcess` was refused (error 5) even for query access on MsMpEng.exe, dwm.exe and
  SearchIndexer.exe, while explorer.exe, OneDrive.exe and msedgewebview2.exe opened with
  `PROCESS_SET_INFORMATION` [verified: L18, L27]. Windows Update workers (TiWorker.exe,
  TrustedInstaller.exe), Defender and the indexer run as services, so for them the only honest
  output is advice [inferred].
- Never touch the game, anti-cheat, system, security or protected processes [inferred].

**Manual advice:**

- Pause downloads (Steam, Epic, browser).
- Schedule Windows Update active hours.
- Close the browser or turn off its GPU acceleration.

### 4.7 RAM pressure and page-file thrash

**Signature:**

- `\Memory\Available MBytes` < 10% of RAM is a warning; < 5% (or < 64 MB) is critical
  [verified: S30, PAL thresholds].
- `\Memory\Pages Input/sec` > 1000 is a warning (PAL); in-game, use event-linked spikes with
  lift ≥ 2 [inferred]. PAL's separate `\Memory\Pages/sec` > 1000 rule warns that this "only
  indicates 4 MB/s or more of disk activity" [verified: S30].
- `% Committed Bytes In Use` > 80% is a warning and > 90% critical [verified: S30]. It was 86%
  here at probe time [verified: L3].
- The game's working set drops while hard faults rise [inferred].

**Confounders:**

- File-backed reads also count as hard faults ("includes read operations to satisfy faults in the
  file system cache") [verified: L1 explain text]. Asset streaming (§4.8) therefore looks similar.
- The standby list is part of "Available".

**Confidence:** medium.

**Auto-fix:** none safe (trimming working sets is not a fix) [inferred]. Closing apps needs
consent and is not reversible.

**Manual advice:** close memory-heavy apps, add RAM, keep the page file system-managed.

### 4.8 Disk and asset-streaming stalls (HDD vs SSD)

**Signature (event-linked):**

- Game `IO Read Bytes/sec` spikes in the hitch windows.
- The game drive's `\LogicalDisk(<letter>:)\Avg. Disk sec/Read` > 15 ms is a warning and > 25 ms
  critical (PAL; it also flags > 600 ms, "Slower than a 3.5 inch floppy drive") [verified: S30].
  PAL defines these on LogicalDisk, not PhysicalDisk.
- `\LogicalDisk(<letter>:)\% Idle Time` < 10% ("the disk queue has at least 1 outstanding I/O 90%
  of the time") [verified: S30].
- Hard faults are up.
- The prior is strong when the game's disk is `MediaType=3` (HDD) [verified: S38].

**Telling it apart from shader compiles (§4.9):**

- Streaming hitches follow disk reads.
- They recur at the same places on a second run, unless the OS file cache is still warm
  [inferred].

**Confounders:**

- Defender scanning the same reads.
- Background disk users (§4.6).
- Process I/O counters include non-disk I/O [verified: S40], and reads the file cache served from memory: a
  buffered 64 MiB read of a cached file grew the reading process's `ReadTransferCount` by 64.0 MiB while
  `\PhysicalDisk(_Total)\Disk Read Bytes/sec` showed 0.00 MiB; the same read unbuffered showed 64.22 MiB on the
  disk [verified: own process, 2026-11-09]. So game reads are disk reads only up to what the disks read.

**Confidence:** medium-high on HDD; medium on NVMe. NVMe drives rarely exceed these latencies, so
the stall is more often CPU-side decompression [unverified, secondary].

**Auto-fix:** none.

**Manual advice:** move the game to an SSD, keep 10–15% free space [unverified], close other
disk-heavy work.

### 4.9 Shader-compilation stutter (DX12 / Vulkan, first run)

**Mechanism:**

- An engine that compiles a pipeline just before drawing with it stalls that frame.
- Epic counts a runtime PSO compile as a hitch above 20 ms. Even after precaching, "the first
  retrievals from these caches can take a few milliseconds" [verified: S21].
- Epic also warns that hitches "may be masked by the PSO cache built by the graphics driver and
  left over from the previous runs", and recommends force-clearing the driver cache to test a
  first-time startup [verified: S21]. That is the mechanism behind the repeat-run test below.
- Epic's precaching skips the draw or substitutes a default material while the PSO compiles
  [verified: S21].
- Epic's 2025 tech blog describes compiles that cost "tens of milliseconds or more" per draw, and
  PSO precaching from UE 5.2 [unverified: S22 returned 403; known only from a search summary].

**Signature (event-linked):**

- Hitches cluster in the first minutes, in new areas and on new effects.
- They coincide with CPU bursts on many *game* threads (driver compile threads run inside the game
  process [unverified]), with low game disk reads.
- On a **repeat run** of the same route, hitches fall ≥ 50%.
- The first run after a game patch or GPU driver update is worse (cache invalidation [unverified]).

**Cache evidence:**

- `%LOCALAPPDATA%\NVIDIA\DXCache` held 109 `<hex>.nvph` files, 5.5 GiB, preallocated (sizes of
  exactly 2048 and 512 MiB). Watch file **mtimes**, not sizes [verified: L12, re-counted
  identically at the re-check; newest mtime 15:29 that day].
- When the driver writes during a session is [unverified].
- For D3D12, the authoritative per-frame signal is PresentMon's PSO compile count, but only through
  the PresentMon service API, not the console CSV (§1.1) [verified: S8, S10].

**Telling it apart from streaming:**

| Pattern | Points to |
|---|---|
| Disk reads low in both runs, hitches drop on run 2 | Shader cache |
| Disk reads high in run 1 and low in run 2, hitches drop | The OS file cache: ambiguous |
| Hitches persist at the same places with reads | Streaming |

[inferred]

**Steam shader pre-caching**: Steam's own client text names only **Vulkan and OpenGL** games
("This allows Vulkan and OpenGL games to load faster and improves framerate stability")
[verified: S46]. Steam may also run "Processing Vulkan shaders..." at launch, and its download
list labels this content "Shader pre-caching update" [verified: S46].

**Confounders:** streaming, garbage collection, and first-time audio and asset loads.

**Confidence:** medium. It rises to high with PSO counts or a clean repeat-run comparison.

**Auto-fix:** none. Deleting caches makes it worse.

**Manual advice:**

- Let the game's shader pre-compile step finish.
- Play through once; stutter fades.
- After driver updates, expect a one-time re-compile [unverified].
- For Vulkan and OpenGL Steam titles, keep Shader Pre-Caching on.

### 4.10 DPC/ISR latency from drivers (Wi-Fi, audio, RGB tools)

**Guideline:** "a typical DPC routine should run for no more than 100 microseconds"
[verified: S28]. LatencyMon repeats the 100 µs guideline [verified: S29].

**Signature (from outside, without ETW):**

- Per logical processor `% DPC Time` > 10% (PAL: "likely busy or poorly written drivers"), or
  `% Interrupt Time` > 5% warning / > 10% critical (PAL, Processor Information) [verified: S30].
  PAL is not consistent here: its `\Processor(*)\% Interrupt Time` rule uses 10% / 20%
  [verified: S30]. Use the Processor Information object and its 5% / 10%.
- Event-linked spikes of `DPCs Queued/sec` or of DPC time on one core.

**Honest limit:**

- A 2 ms DPC inside a 250 ms sample moves `% DPC Time` by only 0.8%. Aggregates can see sustained
  DPC load, not individual long DPCs.
- Per-driver attribution needs a kernel ETW trace (admin), for example with LatencyMon or WPA
  [inferred].

**Confounders:**

- High network or USB traffic legitimately raises DPC load.
- The GPU driver's DPCs scale with FPS.

**Confidence:** low. lag-doctor can at most say "possible".

**Auto-fix:** none.

**Manual advice:** update or temporarily disable Wi-Fi, audio and RGB utilities, then re-test, or
run LatencyMon as admin.

### 4.11 Wrong GPU on a hybrid laptop, and cross-adapter copy

**(a) The game runs on the iGPU.**

- The game's PID has `\GPU Engine(pid_<game>_luid_<iGPU>...engtype_3d)` busy and ≈0 on the dGPU.
  The iGPU is identified by `HybridIntegrated` [verified: L4, L6].
- FPS is low with the iGPU 3D engine pegged.

**(b) The game is on the dGPU, but the display hangs off the iGPU.**

- The window's monitor maps to a display path whose `targetInfo.adapterId` is the iGPU LUID. That
  is true for this laptop's internal panel [verified: L4].
- The game window's monitor can be found with `MonitorFromWindow` + `GetMonitorInfoW` (szDevice)
  and matched to `viewGdiDeviceName` [inferred: standard Win32, not run here].
- The adapter LUID in the game's `GPU Engine` instances must be compared in the same lower-case
  hex format as QueryDisplayConfig's `adapterId` (§1.2); skip the software adapter [inferred].
- `HybridPresent=1` confirms it when present; a 0 is weak (§0.7) [verified: S6].
- DWM uses 3D on the iGPU while the game renders on the dGPU. Here DWM used 11% of the Intel 3D
  engine while a dGPU app rendered [verified: L6].

**Cost:** NVIDIA reports frame-rate gains "from 11% to 27%" and latency "up to 22%" lower "in
popular esports titles" when Advanced Optimus lets the dGPU drive the panel directly
[verified: S42]. Blt-model presents across hybrid GPUs cost extra again [verified: S26].

**Confounders:**

- Games that deliberately use the iGPU (light titles).
- External monitors wired to the dGPU have no copy.
- A MUX "dGPU only" mode changes the path after a reboot.

**Confidence:** high for (a). High for (b)'s existence, medium for how much it costs.

**Auto-fix (a), reversible, with consent:**

- Write `HKCU\Software\Microsoft\DirectX\UserGpuPreferences\<exe path> = "GpuPreference=2;"`.
  Merge with the other existing `key=value;` pairs, and save the old value for restore.
- It takes effect at the next game launch [unverified].
- The format is community-documented, not Microsoft-documented [unverified]. The Settings UI
  (System > Display > Graphics) is the documented route.

**Manual advice (b):**

- Use the OEM's dGPU/MUX mode. ASUS's tech-spec page for the TUF Dash F15 (2022) lists every
  display option as "MUX Switch + Optimus" and refers to the Armoury Crate GPU mode "Ultimate";
  it does not mention Advanced Optimus [verified: S52; the page is per family, not per SKU]. A
  reboot when switching is typical [unverified].
- Or play on a monitor wired to the dGPU. The task brief reports the external 2560x1440 display on
  the NVIDIA adapter, which would mean no copy there; it was not connected during either probe
  [unverified here].

### 4.12 Power plan, battery, Windows 11 power mode

**Signature (static, plus clock evidence):**

- `ACLineStatus=0` (battery), or `SystemStatusFlag=1` (battery saver) [verified: S36].
- The effective power mode is BatterySaver (0) or BetterBattery (1) [verified: S35, S44].
- The user power mode for the current source is `961cc777-2547-4f9d-8174-7d86181b8a7a`
  (best efficiency) [verified: S34].
- An OEM quiet plan, such as "Silent" here [verified: L4].
- Plus CPU or GPU clock evidence from §4.2 and §4.5.

**Confounders:**

- The OEM utility can also impose fan and power limits outside the Windows plan [unverified].
- Laptops on battery cap the dGPU [unverified magnitude].

**Confidence:** high for the setting itself. Medium for "this caused the lag" unless clocks or
limits confirm it.

**Auto-fix (reversible):**

- `PowerSetUserConfiguredACPowerMode(&GUID_POWER_MODE_BEST_PERFORMANCE)`, GUID
  `ded574b5-45a0-4f42-8737-46345c09c238`, on AC. Read the old GUID first and restore it after
  the session [verified API: S34]. Whether it works without admin is [unverified]. Microsoft calls
  the user mode "a vote on system behavior from the user" that "can be overridden by other
  system signals", so verify the result through the effective-mode callback, not the setter's
  return code [verified: S34, S35; method inferred].
- Or `PowerSetActiveScheme` back to Balanced. Store the previous GUID.

**Manual advice:**

- Plug in.
- Switch the OEM mode from Silent to Performance.
- Turn off battery saver while gaming.

### 4.13 Refresh rate set below the panel's maximum

**Signature (static):** `EnumDisplaySettingsExW(ENUM_CURRENT_SETTINGS).dmDisplayFrequency` is
below the highest rate offered at the current resolution. Here the panel offers [60, 144] and runs
at 144 [verified: L4]. Displayed FPS is capped at the lower refresh rate under VSync.

**Confounders:**

- A deliberate 60 Hz choice to save battery.
- HDR or bandwidth limits on external monitors.

**Confidence:** high.

**Auto-fix (reversible):**

- `ChangeDisplaySettingsExW(name, &mode, NULL, CDS_TEST, NULL)` first, then flags `0`, which
  changes the mode "dynamically" without persisting it.
- To revert: `ChangeDisplaySettingsExW(name, NULL, NULL, 0, NULL)`, "the easiest way to return to
  the default mode after a dynamic mode change" [verified: S37].
- Add a 15 s keep-or-revert prompt [inferred].
- Persist with `CDS_UPDATEREGISTRY` only after the user confirms [verified: S37].

**Manual advice:** Settings > Display > Advanced display > refresh rate.

### 4.14 VSync without VRR, a misconfigured frame cap, and blt-model windowed presents

**VSync judder:**

- VSync is quantised (§3.3) and FPS sits below refresh, so intervals alternate between 1P and 2P;
  below refresh/2 they alternate between kP and (k + 1)P the same way (58 FPS on 144 Hz: 2P and 3P)
  [inferred].
- Manual: enable VRR where available; otherwise cap at refresh/2 (refresh/(k + 1) below it), or turn
  VSync off.

**Misconfigured cap:**

- A cap is detected at C < 0.9 × refresh, with r < 0.7 and an unsaturated thread.
- The fix is to raise it. Blur Busters recommends a cap "(a minimum of) 3 FPS" below max refresh
  with G-SYNC plus control-panel VSync [unverified: the page returned 403 to my fetcher, so this
  is from a search snippet].
- Manual only. Caps live in the game, the driver or RTSS.

**Blt-model windowed present:**

- `PresentMode` is `Composed: Copy with GPU GDI` or `Composed: Copy with CPU GDI` [verified: S1].
- Fix (manual, reversible): Windows 11 "Optimizations for windowed games". It moves compatible
  DX10/DX11 windowed and borderless games from blt to flip model, "reducing frame latency", and
  enables Auto HDR and VRR on supported displays. It is a global switch under Settings > System >
  Display > Graphics, which can be turned off per app under "Custom options for apps"; the game
  must be restarted [verified: S27]. Its registry encoding is [unverified]; don't automate it.

**Confidence:** high for the patterns. The latency cost should be *measured*: compare
`MsUntilDisplayed` before and after (`DisplayLatency` exists only with `--v2_metrics`
[verified: S2]) [inferred method].

### 4.15 Overlays and recording (Game Bar DVR, NVIDIA Instant Replay, Discord)

**Signature:**

- A non-game process uses the game adapter's `videoencode` engine. NVIDIA exposes `videoencode`
  [verified: L6].
- Or `utilization.encoder` > 0 from nvidia-smi [verified field: S48] while the user is not
  streaming. Its sample period "may be between 1 second and 1/6 second depending on the product"
  [verified: S48].
- Static clue: `GameDVR_Enabled=1` [verified: L4], though its semantics are [unverified].
- Overlay hosts that exist on this machine: `NVIDIA Overlay.exe`, `gameoverlayui64.exe` (Steam),
  and a ShadowPlay folder [verified: L12].

**Injected overlays** (Discord, Steam, RTSS) render *inside* the game process. Seeing them would
need reading the game's module list, which the no-memory-reads rule forbids. Report them as
"present on system" only [inferred].

**Confidence:** medium for encoders, low for overlays.

**Auto-fix:** none. Their settings live in their own apps.

**Manual advice:** turn off background recording or "Record what happened" [UI label
unverified] and Instant Replay,
then compare.

### 4.16 Frame generation (DLSS-FG, FSR-FG, AFMF, XeSS-FG)

**How it shows up:**

- PresentMon tags `Intel XeSS-FG` and `AMD AFMF` frames in `FrameType`, only with
  instrumentation and `--track_frame_type`. **DLSS-FG has no FrameType value** [verified: S5, S1].
- DLSS-G presents asynchronously through the "SL pacer", which schedules both real and generated
  frames; `numFramesActuallyPresented` (2 for 2x) is visible only inside the game [verified: S17].
- A likely NVIDIA tell: `MsFlipDelay` populated (driver flip metering) [verified field: S2; link
  to FG inferred].
- Presents arrive at about 2x, 3x or 4x the simulation cadence. `MsBetweenSimulationStart` is `NA`
  unless the game emits PC Latency markers (`--track_pc_latency`) or Intel-PresentMon app-timing
  events (`--track_app_timing`) [verified: S2 `MetricsCalculatorInstrumented.cpp`]; "SL Reflex is
  required" for DLSS-G [verified: S17]; the cadence test is [inferred].

**Rules:**

- Judge smoothness on `MsBetweenDisplayChange` [verified: S16, S17].
- Judge responsiveness and bound on the *app* cadence, not presents [inferred].

**Requirement:** the DLSS-G guide lists "Hardware-accelerated GPU Scheduling (HWS) must be
enabled" and says that if any requirement is not met, "DLSS-G will be unavailable" [verified: S17].

**Confidence:** high with FrameType; medium for DLSS-G heuristics.

**Fix:** manual. FG on for a smooth base frame rate; FG off if latency feels bad
[inferred, general].

### 4.17 HAGS (Hardware-Accelerated GPU Scheduling)

**What it is:** "offload most of GPU scheduling to a dedicated GPU-based scheduling processor",
introduced as "a user opt-in, but off by default option", with "transparent" impact
[verified: S41].

**State here:** NVIDIA `HwSchEnabled=1` with `HwSchEnabledByDefault=0`; Intel supported but off
[verified: L4].

**Rules:**

- HAGS off and the game offers DLSS-FG → FG is unavailable [verified: S17].
- HAGS on → apply the 0.5 ms GPU-Busy margin [verified: S7], and widen the "borderline" band
  rather than trusting the margin to the tenth of a millisecond [inferred].
- HAGS is otherwise neutral. Do not score it as a lag cause without A/B evidence [inferred].

**Fix:** manual (Settings > System > Display > Graphics; a reboot is typical [unverified]).

### 4.18 Memory Integrity (HVCI) and VBS

**State here:** VBS running, HVCI running (`SecurityServicesRunning=2`), MBEC available
[verified: L11].

**Performance:** Microsoft says processors without MBEC/GMET emulate it and "will have a bigger
impact on performance". This CPU has MBEC [verified: S31, L11]. Microsoft's gaming support article
(id a255f612...) no longer renders its text to my fetcher. Secondary coverage says it recommended
turning HVCI and the Virtual Machine Platform off for gaming and back on afterwards [unverified].

**How a responsible tool presents it:**

- Show it as **information, not a cause**, unless the user runs an A/B test.
- State the security trade-off plainly: memory integrity protects kernel code integrity
  [verified: S31].
- Never toggle it. Microsoft's registry method writes `HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\...`
  keys, so it needs admin, and the doc warns that incompatible drivers "in rare cases may result
  in a boot failure (blue screen)" [verified: S31]. A restart is needed for it to take effect
  [unverified; the doc only says to restart or run `gpupdate /force` for the Group Policy route].
- Offer a link to Windows Security > Device security > Core isolation, with the advice "turn it
  back on afterwards".

**Confidence:** n/a. It cannot be attributed to a single lag event.

### 4.19 The game is not the foreground window (QoS demotion)

This is an addition: it is cheap to measure and real.

**What Windows does:** windowed processes get QoS by window state: in focus → **High**; visible →
**Medium**; minimised or fully occluded → **Low**. On battery, Low runs at the most efficient
frequency on efficient cores [verified: S33]. Windows may also lower a foreground app to Medium
after inactivity, on battery only [verified: S33].

**Signature:** at the time of the event, `GetForegroundWindow` belongs to another PID, or the game
is iconic [verified API: L15, L21].

**Test-rig caveat:** the inactivity demotion is a battery-only feature, and Microsoft warns that
"Automated tests lacking user input may trigger this feature, lowering QoS and skewing results"
[verified: S33]. laglab scenarios run on battery without input can therefore be demoted. Microsoft
documents the switch as `HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling`
`DisableUserPresenceQos` (REG_DWORD 1 = disabled) [verified: S33]; it is a machine-wide setting
for a test rig's owner to set, never something lag-doctor writes.

**Confidence:** high for the state; medium for its effect.

**Fix:** manual (click into the game).

### 4.20 Network

See §6.

---

## 5. Scoring: a ranked, honest diagnosis (question 4)

### 5.1 Evidence model

The whole scheme is [inferred design].

- Each evidence item has a **status**:
  - `supports`
  - `contradicts`
  - `neutral` (measured, in range)
  - `not_measured`, with a reason code:
    - `no_presentmon_access`
    - `protected_process`
    - `counter_missing`
    - `vendor_tool_missing`
    - `too_few_samples`
    - `not_applicable`
- Each item belongs to one **family**:
  - frame timing (PresentMon)
  - GPU telemetry (nvidia-smi)
  - GPU engine and memory (PDH)
  - CPU threads
  - CPU frequency and limits
  - memory
  - disk
  - interrupts
  - background processes
  - static configuration
- Items carry weights in points, where 1 point ≈ a modest likelihood ratio. Typical weights:
  - 1 for a static setting
  - 2 for a steady-state metric past its threshold
  - 3 for an event-linked signal with lift ≥ 4
  - 4 for a direct readout of the cause (for example `hw_thermal_slowdown` time, or `FrameType`)
- **No double counting.** Per family, take only the strongest supporting item and the strongest
  contradicting item:
  `S(c) = Σ_f max_supports(f) − Σ_f max_contradicts(f)`.
- **The bound classification is a gate, not evidence.** GPU-side causes (4.1, 4.2, 4.3, 4.11b)
  require "GPU-bound or mixed". CPU-side causes (4.4, 4.5, 4.19) require "CPU-bound, mixed or
  unknown". The classifier's output is therefore never added as points to several causes.
- **Static configuration** can add at most +2 and can never make a cause "Likely" by itself.

### 5.2 Event-linked evidence (stutter causes)

For hitch episodes H (n_H ≥ 5) and a signal s sampled at 4 Hz:

```
a    = # episodes with s "active" in [t_start - 0.5 s, t_end + 0.25 s]
b    = share of baseline windows (no event within 2 s) with s active
lift = (a / n_H) / max(b, 0.02)
supports    if a >= 3 and a/n_H >= 0.30 and lift >= 2   (weight 2; 3 if lift >= 4)
contradicts if s measured, n_H >= 10 and a/n_H <= 0.10   (weight 2)
```

- **Explaining away.** Attribute each episode to the cause with the highest per-episode evidence.
  Report each cause's *explained share* of episodes and of the hitch time.
- **User marks.** Each hotkey mark becomes an episode window. If frames are measured and the
  window holds no frame event, report "lag not visible in frame timing" and route to the network
  and input rules (§6). If frames are not measured, run the lift test on marks only, at half
  weight.

### 5.3 Labels, coverage, minimum data

```
coverage C(c) = Σ max-weight of measured families / Σ max-weight of all families for c
Likely          : S >= 4, the key family (marked per cause in §10) measured and supporting, C >= 0.6
Possible        : 2 <= S < 4
Checked, fine   : S <= -2 and C >= 0.6      ("measured and fine" - say what was measured)
Not enough data : key family not measured, or C < 0.5, or below the §2.6 minimums
Inconclusive    : otherwise
```

### 5.4 Ranking and reporting

- Rank by label tier, then S, then explained share, then by whether a reversible auto-fix exists.
- Show at most 3 causes. Give each its top 3 evidence lines with numbers ("sw_thermal_slowdown 64%
  of GPU-bound seconds; clock 1.31 → 1.02 GHz").
- Then list "**checked and fine**" and "**could not check (why)**".
- Zero events: say "no hitches ≥ 50 ms in N frames (≤ 3/N at 95%)" [verified rule: S45].

---

## 6. Network lag: pointer only

Network lag (rubber-banding, delayed hits, warping while the frame rate is fine) is covered by the
network researcher's note. The hand-off here: when a user mark or a complaint has **no matching
frame event** in a measured frame series (§5.2), the frame-side rule book should report "frame
delivery was fine at that moment". It should then pass the time window to the network rules, not
guess a local cause [inferred].

## 7. Open questions and unverified items to settle with real captures

These need real captures, not more desk research:

- **Live CSV header.** The default PresentMon 2.6.0 header has not been observed in a live
  capture, which needs elevation or Performance Log Users membership. Confirm `TimeInQPC`,
  `CPUStartQPC` and `HybridPresent` on the first elevated run.
- **DLSS-FG in the CSV.** Exactly how DLSS-FG presents appear: per-present rows, `MsCPUBusy` on
  generated presents, `MsFlipDelay`. Capture a DLSS-FG title.
- **Bit meanings** of `Performance Limit Flags` and `Thermal Zone Throttle Reasons`.
- **Cache write timing.** When NVIDIA DXCache `.nvph` writes happen relative to compiles.
- **Registry encodings.** The value encodings of `UserGpuPreferences` (`GpuPreference`,
  `AppStatus`), `GameDVR_*`, `AutoGameModeEnabled` and `HwSchMode`. Prefer the APIs listed in §1.5
  where they exist.
- **Power-mode write access.** Whether `PowerSetUserConfiguredACPowerMode` works without admin.
- **Stopping a capture.** Whether `--terminate_existing_session` with lag-doctor's session name
  stops a running capture with its CSV fully flushed (it needs the same privilege as a capture).
- **Anti-cheat handles.** Whether protected games refuse `PROCESS_QUERY_LIMITED_INFORMATION`, which
  would make PresentMon print `<unknown>` for them (§1.1).
- **The default thresholds** in §2.4, §3 and §4 are starting points. Calibrate them with laglab
  scenarios and real games.

---

## 8. Sources

GitHub items are pinned to a tag and its commit.

- **S1:** PresentMon v2.6.0 (tag → commit `e13fce6acdb55a808fd8318175a56863e532d95f`),
  `README-ConsoleApplication.md`:
  https://github.com/GameTechDev/PresentMon/blob/e13fce6acdb55a808fd8318175a56863e532d95f/README-ConsoleApplication.md
- **S2:** Same commit, `PresentMon/CsvOutput.cpp`, `PresentMon/CommandLine.cpp`,
  `PresentMon/OutputThread.cpp`, `PresentMon/MainThread.cpp` (exit codes, Ctrl handler),
  `PresentMon/ConsumerThread.cpp`, `PresentData/NvidiaTraceConsumer.cpp`,
  `PresentData/ETW/NV_DD.h`, `PresentData/PresentMonTraceSession.cpp` and `.hpp` (timestamp type,
  raw timestamps, provider enabling), `PresentData/PresentMonTraceConsumer.hpp`:
  https://github.com/GameTechDev/PresentMon/tree/e13fce6acdb55a808fd8318175a56863e532d95f/PresentMon .
  v2.6.1 is commit `95d0312ab564d89f4b98278a3501ba347259f6cb`; its `PresentMon/CsvOutput.cpp` is
  byte-identical to v2.6.0's.
- **S3:** Same commit, `IntelPresentMon/CommonUtilities/mc/MetricsCalculatorCpuGpu.cpp`,
  `MetricsCalculator.cpp` (`CalculateCPUStart`), `MetricsCalculatorAnimation.cpp`,
  `MetricsCalculatorInstrumented.cpp` (`ComputeMsBetweenSimulationStarts`), `MetricsTypes.h`.
- **S4:** Same commit, `IntelPresentMon/CommonUtilities/mc/MetricsCalculatorDisplay.cpp`.
- **S5:** Same commit, `PresentData/PresentEventEnums.hpp` (PresentMode, FrameType).
- **S6:** Same commit, `PresentData/PresentMonTraceConsumer.cpp` (hybrid present from
  `SwapChain_Start` / `ResizeBuffers_Start`) and `PresentData/ETW/Microsoft_Windows_DXGI.h`
  (`HybridPresentMode`: NOT_HYBRID 0, TWO_COPY_PATH 1, ONE_COPY_PATH_CASO 2).
- **S7:** Same commit, `README.md` (Performance Log Users; the HWS accuracy note).
- **S8:** Same commit, `IntelPresentMon/metrics.csv` (`PM_METRIC_PSO_COMPILE_*`, FPS-Display, and
  others).
- **S9:** PresentMon release notes v2.3.0 (2024-12-17), v2.3.1 (2025-06-10) and v2.6.0
  (2026-09-21), read through the GitHub releases API:
  https://github.com/GameTechDev/PresentMon/releases
- **S10:** Same commit, `PresentData/InterPresentActivity.hpp` and
  `PresentData/ETW/Microsoft_Windows_Direct3D12.h` (CreatePipelineStateObject Start/Stop
  0x009b/0x009c, provider {5D8087DD-3A9B-4F56-90DF-49196CDC4F11}).
- **S11:** CapFrameX v1.9.1 (commit `631f2b2f1a93c0cbad94ff4b098e64ce23f02ec0`),
  `source/CapFrameX.Statistics.NetStandard/FrametimeStatisticProvider.cs`:
  https://github.com/CXWorld/CapFrameX/blob/631f2b2f1a93c0cbad94ff4b098e64ce23f02ec0/source/CapFrameX.Statistics.NetStandard/FrametimeStatisticProvider.cs
- **S12:** Same commit, `source/CapFrameX.Configuration/CapFrameXConfiguration.cs`
  (StutteringFactor 2.5, StutteringThreshold 25, IntervalAverageWindowTime 500).
- **S13:** Same commit, `source/CapFrameX.Statistics.NetStandard/SampleBasedMovingAverage.cs`.
- **S14:** MathNet.Numerics v5.0.0 `src/Numerics/Statistics/ArrayStatistics.cs` (Quantile = R-8),
  https://github.com/mathnet/mathnet-numerics/blob/v5.0.0/src/Numerics/Statistics/ArrayStatistics.cs ;
  CapFrameX pins MathNet.Numerics 5.0.0 in `source/Directory.Packages.props` at S11's commit.
- **S15:** OCAT v1.6.3 (commit `a7652f6ecb313342a7be05b6ba8d030452aa0600`),
  `PresentMonInterface/Recording.cpp`:
  https://github.com/GPUOpen-Tools/OCAT/blob/a7652f6ecb313342a7be05b6ba8d030452aa0600/PresentMonInterface/Recording.cpp
- **S16:** NVIDIA FrameView 2.0 User Guide (web version), with text extracted locally:
  https://images.nvidia.com/content/geforce/technologies/frameview/frameview-2-0-user-guide-web-version.pdf
- **S17:** NVIDIA Streamline v2.14.16 (commit `625db07d4bca5abc67d781c42af7e7fb0761f1b6`),
  `docs/ProgrammingGuideDLSS_G.md`:
  https://github.com/NVIDIA-RTX/Streamline/blob/625db07d4bca5abc67d781c42af7e7fb0761f1b6/docs/ProgrammingGuideDLSS_G.md
- **S18:** S. Liu, M. Claypool, A. Kuwahara, J. Scovell, "The Effects of Frame Rate Variation on
  Game Player Quality of Experience", CHI '23, doi:10.1145/3544548.3580665:
  https://web.cs.wpi.edu/~claypool/papers/frame-variation-chi-23/paper.pdf
- **S19:** S. S. Tokey, B. Boudaoud, J. Spjut, M. Claypool, "Impact of Frametime Spikes on
  Performance and Quality of Experience in Platformer Games", FDG '26:
  https://web.cs.wpi.edu/~claypool/papers/spikes-fdg-26/paper.pdf ; abstract:
  https://research.nvidia.com/publication/2026-08_impact-frametime-spikes-performance-and-quality-experience-platformer-games
- **S20:** D. Klein, J. Spjut, B. Boudaoud, J. Kim, "The Influence of Variable Frame Timing on
  First-Person Gaming", arXiv:2306.01691: https://arxiv.org/abs/2306.01691
- **S21:** Epic, "PSO Precaching" (UE 5.8 docs):
  https://dev.epicgames.com/documentation/en-us/unreal-engine/pso-precaching-for-unreal-engine
- **S22:** Epic tech blog, "Game engines and shader stuttering" (2025-02-04). It returned 403 to
  my fetcher, and again to the fact-check; the date comes from a search result (a second search
  only confirmed "February 2025"):
  https://www.unrealengine.com/en-US/tech-blog/game-engines-and-shader-stuttering-unreal-engines-solution-to-the-problem
- **S23:** Club386, F. Temsamani, "Intel's updated PresentMon frame analysis tool will help you
  identify gaming bottlenecks", 2023-08-18:
  https://www.club386.com/intel-sets-sights-on-identifying-gaming-bottlenecks-with-updated-presentmon-frame-analysis-tool
- **S24:** `nvml.h` from NVIDIA/go-nvml v0.13.4-1 (commit
  `1a1e86da474d340d7cf58274588f4a4afc8772e7`), `gen/nvml/nvml.h`:
  https://github.com/NVIDIA/go-nvml/blob/1a1e86da474d340d7cf58274588f4a4afc8772e7/gen/nvml/nvml.h
- **S25:** Microsoft, `DXGI_QUERY_VIDEO_MEMORY_INFO`:
  https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_4/ns-dxgi1_4-dxgi_query_video_memory_info
- **S26:** Microsoft, "For best performance, use DXGI flip model":
  https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/for-best-performance--use-dxgi-flip-model
- **S27:** Microsoft Support, "Optimizations for windowed games in Windows 11":
  https://support.microsoft.com/en-us/topic/3f006843-2c7e-4ed0-9a5e-f9389e535952
- **S28:** Microsoft, "Guidelines for Writing DPC Routines":
  https://learn.microsoft.com/en-us/windows-hardware/drivers/kernel/guidelines-for-writing-dpc-routines
- **S29:** Resplendence, LatencyMon (v7.40 page): https://www.resplendence.com/latencymon
- **S30:** PAL (Performance Analysis of Logs) `QuickSystemOverview.xml`, commit
  `7cfe6d8d238db7edd68f13116d1909cfe685c8ff`. These are server-oriented thresholds; adapt them for
  games:
  https://github.com/clinthuffman/PAL/blob/7cfe6d8d238db7edd68f13116d1909cfe685c8ff/PAL2/PALWizard/bin/Debug/QuickSystemOverview.xml
- **S31:** Microsoft, "Enable memory integrity" (includes the Win32_DeviceGuard tables):
  https://learn.microsoft.com/en-us/windows/security/hardware-security/enable-virtualization-based-protection-of-code-integrity
- **S32:** Microsoft, `Win32_OptionalFeature`:
  https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-optionalfeature
- **S33:** Microsoft, "Quality of Service":
  https://learn.microsoft.com/en-us/windows/win32/procthread/quality-of-service
- **S34:** Microsoft, `PowerGetUserConfiguredACPowerMode`:
  https://learn.microsoft.com/en-us/windows/win32/api/powrprof/nf-powrprof-powergetuserconfiguredacpowermode ;
  `PowerSetUserConfiguredACPowerMode`:
  https://learn.microsoft.com/en-us/windows/win32/api/powrprof/nf-powrprof-powersetuserconfiguredacpowermode
- **S35:** Microsoft, `EFFECTIVE_POWER_MODE`:
  https://learn.microsoft.com/en-us/windows/win32/api/powersetting/ne-powersetting-effective_power_mode ;
  `PowerRegisterForEffectivePowerModeNotifications`:
  https://learn.microsoft.com/en-us/windows/win32/api/powersetting/nf-powersetting-powerregisterforeffectivepowermodenotifications
- **S36:** Microsoft, `SYSTEM_POWER_STATUS`:
  https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-system_power_status
- **S37:** Microsoft, `ChangeDisplaySettingsExW`:
  https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw
- **S38:** Microsoft, `MSFT_PhysicalDisk`:
  https://learn.microsoft.com/en-us/windows-hardware/drivers/storage/msft-physicaldisk
- **S39:** Microsoft, `Set-MpPreference`:
  https://learn.microsoft.com/en-us/powershell/module/defender/set-mppreference
- **S40:** Microsoft, `GetProcessIoCounters`:
  https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getprocessiocounters
- **S41:** Microsoft DirectX blog, S. Pronovost, "Hardware Accelerated GPU Scheduling",
  2020-06-30: https://devblogs.microsoft.com/directx/hardware-accelerated-gpu-scheduling/
- **S42:** NVIDIA, A. Burnes, "How Advanced Optimus Delivers Max Performance With Optimal Battery
  Life", 2022-02-11: https://www.nvidia.com/en-gb/geforce/news/rtx-laptops-advanced-optimus.md
- **S43:** psutil `release-7.2.2`, `psutil/arch/windows/proc.c` (`psutil_proc_threads`):
  https://github.com/giampaolo/psutil/blob/release-7.2.2/psutil/arch/windows/proc.c
- **S44:** Windows SDK 10.0.19041.0 headers on this machine, under
  `C:\Program Files (x86)\Windows Kits\10\Include\10.0.19041.0\`: `um\powersetting.h`
  (EFFECTIVE_POWER_MODE, V1=1, V2=2), `um\winternl.h`, `um\wingdi.h`, `um\processthreadsapi.h`,
  `um\WinBase.h`, `shared\d3dkmthk.h`, `shared\d3dkmdt.h`.
- **S45:** "Rule of three (statistics)": https://en.wikipedia.org/wiki/Rule_of_three_(statistics)
- **S46:** Steam client strings on this machine:
  `C:\Program Files (x86)\Steam\public\steamui_english.txt` (lines 4566–4569, 215) and
  `steamui\localization\steamui_english-json.js`.
- **S47:** Blur Busters "G-SYNC 101: Optimal G-SYNC Settings". It returned 403 to my fetcher, so
  only a search snippet was seen (403 again at the fact-check):
  https://www.blurbusters.com/gsync/gsync101-input-lag-tests-and-settings/14/
- **S48:** `C:\Windows\system32\nvidia-smi.exe --help-query-gpu` (591.86), saved in the scratch
  folder as `nvsmi-help-query-gpu.txt`. The fact-check re-captured it (exit 0) as
  `research-lag-causes-verify\nvsmi-help-query-gpu.txt`.
- **S49:** Microsoft, `WNODE_HEADER` (ClientContext 1 = QPC):
  https://learn.microsoft.com/en-us/windows/win32/etw/wnode-header
- **S50:** Microsoft, `NtQuerySystemInformation`:
  https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation
- **S51:** Microsoft, `DXGI_GPU_PREFERENCE`:
  https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_6/ne-dxgi1_6-dxgi_gpu_preference
- **S52:** ASUS, "ASUS TUF Dash F15 (2022)" tech specs (family page covering FX517ZR, ZC, ZE, ZM):
  https://www.asus.com/laptops/for-gaming/tuf-gaming/asus-tuf-dash-f15-2022/techspec/
- **S53:** Intel, "Intel® Arc™ Graphics - PresentMon" (Intel Gaming Access), the download page the
  PresentMon README links: https://game.intel.com/story/intel-presentmon/
- **S54:** Microsoft, `SetProcessInformation` (access right, EcoQoS examples, reset to system
  managed): https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessinformation

## 9. Local commands and their real output

Commands ran unelevated, from the repo folder, with `uv run python`
unless shown otherwise. The throwaway scripts were in a temporary folder,
`%TEMP%\claude\…\scratchpad\research-lag-causes\scripts\`.
Outputs are pasted as printed. The host name and personal exe paths are redacted.

**L1: PDH explain texts** (`pdh_explain.py`: `PdhAddEnglishCounterW` + `PdhGetCounterInfoW(…, TRUE)`).
Excerpts are quoted in §1.2. One example line:

```
\Energy Meter(*)\Power
   type=0x40020500 defaultScale=0
   The Power counter measures average power over each sampling interval in milliwatts.
```

**L2: counter names:**

```powershell
Get-Counter -ListSet 'Processor Information','Thermal Zone Information','GPU Engine','GPU Adapter Memory','GPU Process Memory','Memory','Paging File','PhysicalDisk','Processor','Power Meter','Energy Meter','Process V2'
```

Each set was found, with the counters listed in §1.2. The set `Processor Performance` does not
exist.

**L3: one sample** (`Get-Counter … -SampleInterval 1 -MaxSamples 2`):

```
thermal zone information(\_tz.thrm)\% passive limit            100.00
thermal zone information(\_tz.thrm)\temperature                359.00
processor information(_total)\% processor performance           54.38
processor information(_total)\% performance limit              100.00
processor information(_total)\performance limit flags             0.00
processor(_total)\% dpc time                                     1.74
memory\available mbytes                                      15,844.00
memory\committed bytes                                 34,706,042,880.00
memory\commit limit                                    40,434,671,616.00
energy meter(rapl_package0_pkg)\power                         14,948.22
processor information(0,0)\processor frequency               2,300.0
processor information(0,12)\processor frequency              2,300.0
processor information(0,0)\% processor performance             52.7
processor information(0,0)\actual frequency                  1,212.1
```

**L4: static probe** (`probe_static.py`). Core of the D3DKMT, display and power parts as run:

```python
class D3DKMT_ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wt.UINT), ("AdapterLuid", LUID), ("NumOfSources", wt.ULONG),
                ("bPresentMoveRegionsPreferred", wt.BOOL)]
class D3DKMT_ENUMADAPTERS2(ctypes.Structure):
    _fields_ = [("NumAdapters", wt.ULONG), ("pAdapters", ctypes.POINTER(D3DKMT_ADAPTERINFO))]
class D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wt.UINT), ("Type", ctypes.c_int), ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", wt.UINT)]
KMTQAITYPE_ADAPTERTYPE, KMTQAITYPE_WDDM_2_7_CAPS = 15, 70      # d3dkmthk.h (SDK 19041)
ea = D3DKMT_ENUMADAPTERS2(0, None); gdi.D3DKMTEnumAdapters2(ctypes.byref(ea))
arr = (D3DKMT_ADAPTERINFO * ea.NumAdapters)(); ea.pAdapters = arr; gdi.D3DKMTEnumAdapters2(ctypes.byref(ea))
for a in arr[:ea.NumAdapters]:
    caps = wt.UINT(0)
    q = D3DKMT_QUERYADAPTERINFO(a.hAdapter, KMTQAITYPE_WDDM_2_7_CAPS, ctypes.cast(ctypes.byref(caps), ctypes.c_void_p), 4)
    gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q))          # bit0 HwSchSupported, bit1 HwSchEnabled, bit2 EnabledByDefault
    gdi.D3DKMTCloseAdapter(ctypes.byref(wt.UINT(a.hAdapter)))
# QueryDisplayConfig: PATH_INFO is 72 bytes, MODE_INFO 64 bytes (asserted); target name via
# DisplayConfigGetDeviceInfo(type=2), source GDI name via type=1; refresh = targetInfo.refreshRate.
CB = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
pp.PowerRegisterForEffectivePowerModeNotifications(2, _cb, None, ctypes.byref(handle))   # callback fires once
```

Output (exe paths redacted):

```
EfficiencyClass=0: 4 cores, logical processors [[12], [13], [14], [15]]
EfficiencyClass=1: 6 cores, logical processors [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11]]
{'ACLineStatus': 1, 'BatteryFlag': 1, 'BatteryLifePercent': 100, 'SystemStatusFlag': 0, ...}
rc 0 active scheme 64a64f24-65b9-4b56-befd-5ec1eaced9b3   friendly name: Silent
PowerGetUserConfiguredACPowerMode: rc=0 guid=00000000-0000-0000-0000-000000000000
PowerGetUserConfiguredDCPowerMode: rc=0 guid=961cc777-2547-4f9d-8174-7d86181b8a7a
hr 0 callbacks [(2, 'Balanced')]
luid=0x00000000_0x00010F83 sources=4 name='NVIDIA GeForce RTX 3070 Laptop GPU'
   ADAPTERTYPE=0x2313 ['RenderSupported', 'DisplaySupported', 'HybridDiscrete', 'ACGSupported', 'SupportSetTimingsFromVidPn']
   WDDM_2_7_CAPS=0xB {'HwSchSupported': 1, 'HwSchEnabled': 1, 'HwSchEnabledByDefault': 0}
luid=0x00000000_0x00010C49 sources=4 name='Intel(R) UHD Graphics'
   ADAPTERTYPE=0x232B ['RenderSupported', 'DisplaySupported', 'PostDevice', 'HybridIntegrated', 'ACGSupported', 'SupportSetTimingsFromVidPn']
   WDDM_2_7_CAPS=0x1 {'HwSchSupported': 1, 'HwSchEnabled': 0, 'HwSchEnabledByDefault': 0}
QueryDisplayConfig rc 0 paths 1
\\.\DISPLAY1: monitor='' tech=INTERNAL target adapter luid=0x00000000_0x00010C49 refresh=342060000/2375360=144.003 Hz
\\.\DISPLAY1: current 1920x1080@144 Hz; rates offered at this resolution: [60, 144]; modes enumerated: 66
HKCU\System\GameConfigStore  'GameDVR_Enabled' = 1
HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers  'HwSchMode' = 2
HKCU\Software\Microsoft\DirectX\UserGpuPreferences  '<ASUS tool>.exe' = 'GpuPreference=1;'  '<game>.exe' = 'AppStatus=4;'
HKLM\...\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity  'Enabled' = 1
HKLM\SYSTEM\CurrentControlSet\Control\Power  'DefaultOverlayDcPowerScheme' = '961cc777-2547-4f9d-8174-7d86181b8a7a'
```

Bit 13 of ADAPTERTYPE is set on both GPUs, but the 19041 SDK header defines no name for it
(it falls inside `Reserved`) [verified: S44, L21]. This original probe listed two adapters; the
re-check (L21) also lists the software adapter 0x00010EF4.

**L5: nvidia-smi.** The `--query-gpu` output is in §1.4. From `nvidia-smi -q -d TEMPERATURE,PERFORMANCE,POWER`:

```
    Clocks Event Reasons ... SW Thermal Slowdown : Active
    Clocks Event Reasons Counters  SW Power Capping : 116298831 us   SW Thermal Slowdown : 19018143862 us
    GPU Current Temp 82 C | GPU Shutdown Temp 101 C | GPU Slowdown Temp 98 C | GPU Max Operating Temp 105 C | GPU Target Temperature 75 C
    Average Power Draw 42.29 W | Current Power Limit 88.54 W | Default Power Limit 80.00 W | Max Power Limit 105.00 W
```

**L6: GPU engine use** (`Get-Counter '\GPU Engine(*)\Utilization Percentage'`, grouped by pid,
LUID and engine type):

```
instance name example: pid_12148_luid_0x00000000_0x00010f83_phys_0_eng_0_engtype_3d
0x00000000_0x00010f83: 3d, copy, legacyoverlay, ofa_0, security, videodecode, videoencode, vr
0x00000000_0x00010c49: , 3d, copy, gdi render, videodecode, videoprocessing
656,   0x00000000_0x00010f83, 3d   80.6%  python      <- background compute job
28732, 0x00000000_0x00010c49, 3d   11.1%  dwm
28732, 0x00000000_0x00010f83, 3d    8.4%  dwm
gpu adapter memory(luid_0x00000000_0x00010f83_phys_0)\dedicated usage   4,382,486,528
```

Re-run at the fact-check (same command, 484 instances), which also shows the software adapter:

```
0x00000000_0x00010f83: 3d, copy, legacyoverlay, ofa_0, security, videodecode, videoencode, vr
0x00000000_0x00010c49: , 3d, copy, gdi render, videodecode, videoprocessing
0x00000000_0x00010ef4: 3d
656, 0x00000000_0x00010f83, 3d      77.3%  python
28732, 0x00000000_0x00010c49, 3d    11.3%  dwm
28732, 0x00000000_0x00010f83, 3d     8.7%  dwm
37360, 0x00000000_0x00010ef4, 3d     0.0%  explorer
luid_0x00000000_0x00010f83_phys_0  dedicated usage 4,335,894,528
luid_0x00000000_0x00010ef4_phys_0  shared usage 8,192 ; total committed 6,483,968
```

**L7: the one-saturated-thread demo** (`one_thread_demo.py`). The child is spawned with
`sys._base_executable`; per-thread times come from `psutil.Process.threads()`:

```
logical processors: 16
baseline system CPU over 1 s (before child): 44.9%
system CPU% over the 2 s window: 46.0%  (one saturated thread = 100/16 = 6.25% of the machine)
busiest core in window: 81.1%
child threads: 5; busiest thread 10796 busy fraction = 0.99 of one logical CPU
MsMpEng.exe: threads() OK, 0 threads          <- silent: no AccessDenied
explorer.exe: threads() OK, 141 threads
```

The first run, which spawned `sys.executable` (the venv launcher), measured the launcher's busy
fraction as `0.00`.

**L8: handle-free per-thread CPU** (`ntqsi_threads.py`, the core as run):

```python
ntdll = ctypes.WinDLL("ntdll")
def snapshot():
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size); ret = ctypes.c_ulong(0)
        st = ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(ret)) & 0xFFFFFFFF
        if st == 0xC0000004:                     # STATUS_INFO_LENGTH_MISMATCH
            size = max(size * 2, ret.value + 65536); continue
        assert st == 0, hex(st); raw = buf.raw; break
    procs, off = {}, 0
    while True:
        nxt, nthreads = struct.unpack_from("<II", raw, off)
        pid = struct.unpack_from("<Q", raw, off + 80)[0]          # UniqueProcessId
        threads = {}
        for i in range(nthreads):
            t = off + 256 + i * 80                                 # SYSTEM_THREAD_INFORMATION[i]
            k, u = struct.unpack_from("<qq", raw, t)               # kernel, user (100 ns)
            tid = struct.unpack_from("<Q", raw, t + 48)[0]         # ClientId.UniqueThread
            threads[tid] = (k + u) / 1e7
        procs[pid] = threads
        if nxt == 0: break
        off += nxt
    return procs
```

```
child tid 32804: NtQSI busy=0.95  psutil(GetThreadTimes) busy=0.96
MsMpEng.exe pid 23740: NtQSI sees 78 threads; psutil.threads() sees 0
```

**L9: GetThreadTimes granularity** (a busy loop reading `GetThreadTimes(GetCurrentThread())`
for 0.5 s):

```
distinct GetThreadTimes values in 0.5 s busy loop: 32
step sizes (ms): [15.625]
```

**L10: PresentMon unelevated.** SHA-256 checked first
(`b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af`):

```
PresentMon-2.6.0-x64.exe --session_name lagdoctor-research-probe --process_name no-such-game.exe --timed 2 --terminate_after_timed --no_csv --no_console_stats
```

It exited with **6**. Stderr (UTF-16LE) is quoted in §1.1.

Re-run at the fact-check after re-checking the hash (`Get-FileHash` matched; Authenticode `Valid`,
`CN=Intel Corporation`), from an unelevated shell (Medium Mandatory Level, not in Performance Log
Users), with `--session_name lagdoctor-verify-probe` and otherwise the same flags: exit 6, stderr
1,056 bytes starting `FF FE`, decoded text identical to §1.1 (warning, then
`error: failed to start trace session: access denied.`, then the Performance Log Users line).
The shipped exe's `--help` (saved as `docs\research\presentmon-2.6.0-help.txt`) lists every flag
recommended in §1.1.

**L11: WMI readouts:**

```
VirtualizationBasedSecurityStatus=2 SecurityServicesConfigured=2 SecurityServicesRunning=2
AvailableSecurityProperties=1,2,3,5,6,7,8
Manufacturer=ASUSTeK COMPUTER INC. Model=ASUS TUF Dash F15 FX517ZR_FX517ZR
MSFT_PhysicalDisk: 0 NVMe CT1000T500SSD8 MediaType 4 BusType 17 SpindleSpeed 0; 1 NVMe INTEL SSDPEKNU512GZ 4 17 0
Partitions: C -> disk 0, E -> disk 1
Defender: RealTimeProtectionEnabled=True; ScanAvgCPULoadFactor=50 ScanOnlyIfIdleEnabled=True ScanScheduleDay=0 ScanScheduleTime=02:00:00
Win32_OptionalFeature: VirtualMachinePlatform 1, HypervisorPlatform 1
```

**L12: files, processes and caches** (existence checks, `Get-Service`, and a listing of
`%LOCALAPPDATA%`):

```
TiWorker.exe, TrustedInstaller.exe, usoclient.exe, CompatTelRunner.exe, SearchIndexer.exe, MsMpEng.exe,
MpCmdRun.exe, steam.exe, steamwebhelper.exe, EpicGamesLauncher.exe, NVIDIA Overlay.exe: FOUND
Steam dir: gameoverlayui64.exe, GameOverlayRenderer64.dll ; NVIDIA app dir: ShadowPlay\
Services running: BITS, DoSvc, TrustedInstaller, UsoSvc, WinDefend, WSearch, wuauserv
%LOCALAPPDATA%\NVIDIA\DXCache: 109 files, 5585.4 MiB, all '<hex>.nvph', largest 2048.0/2048.0/512.0 MiB
%LOCALAPPDATA%\NVIDIA\GLCache: 16 files, 27.2 MiB ; %LOCALAPPDATA%\D3DSCache: 185 files, 33.8 MiB
Steam steamapps\shadercache: 4 files, 0.9 MiB (per-appid subfolders)
```

**L13: clocks:**

```
time.get_clock_info('perf_counter') -> implementation='QueryPerformanceCounter()', resolution=1e-07
QPF 10000000 ; QPC 15207175948964 ; perf_counter_ns 1520717594898900
```

**L14: the 1% low conventions.** This is pure Python with Gaussian-jittered base frames
(seed 1). The output is in §2.5. A constant-valued variant shows that the
"≥ quantile" implementation includes ties: case A gave a "1% low average" of 138.8, equal to the
overall average.

The fact-check re-implemented it independently (`one_percent_low_verify.py`: R-7 for P1, MathNet's
R-8 formula `h = (n + 1/3)·τ + 1/3` for the CapFrameX selection, base 1000/144 ms, jitter SD
0.4 ms, seed 1):

```
A jitter    n=2000 avgFPS= 138.6 P1(R-7)= 126.1 1%Low(ceil)= 29.4 CapFrameX-1%Low(>=R-8 q)=  29.4 1%Integral= 16.7 0.1%Low= 16.7
B jitter    n=2000 avgFPS= 124.8 P1(R-7)=  16.7 1%Low(ceil)= 16.7 CapFrameX-1%Low(>=R-8 q)=  16.7 1%Integral= 16.7 0.1%Low= 16.7
A constant  n=2000 avgFPS= 138.7 P1(R-7)= 144.0 1%Low(ceil)= 29.9 CapFrameX-1%Low(>=R-8 q)= 138.7 1%Integral= 16.7 0.1%Low= 16.7
```

The constant case's tie-swept value equals the run average either way (138.7 with a 6.944 ms base,
138.8 with 6.94 ms).

**L15: foreground process.** `GetForegroundWindow` → `GetWindowThreadProcessId` → psutil printed
`pid 28748 name claude.exe iconic False`.

**L16: PSO columns in the console.** `grep -n -i "pso" PresentMon/*.cpp PresentMon/*.hpp` on
v2.6.0 found no matches. `CsvOutput.cpp` at v2.6.1 is byte-identical in size (48,966) and also has
no matches. The fact-check fetched all nine `PresentMon/*.cpp`/`*.hpp` files at v2.6.0 and six at
v2.6.1 (`95d0312…`): `grep -q -i "pso\|ShaderCompil"` exited 1 (no match) on both sets, and
`cmp` of the two `CsvOutput.cpp` files exited 0 (byte-identical, not just equal in size).

**L17: power plans** (`powercfg /list`, read-only):

```
381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
64a64f24-65b9-4b56-befd-5ec1eaced9b3  (Silent) *
xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  (ASUS Recommended; GUID left out, probably unique to this PC)
```

**L18: EcoQoS and priority read** (`OpenProcess(0x1000)` + `GetProcessInformation(4)` +
`GetPriorityClass`):

```
explorer.exe: GetProcessInformation ok=True err=0 ControlMask=0x0 StateMask=0x0 PriorityClass=0x20
MsMpEng.exe: OpenProcess(QUERY_LIMITED) failed, err 5
dwm.exe: OpenProcess(QUERY_LIMITED) failed, err 5
```

**L19: GPU process memory** (`Get-Counter '\GPU Process Memory(*)\Dedicated Usage','…\Shared Usage'`):

```
pid_656_luid_0x00000000_0x00010f83_phys_0  dedicated 3,868,364,800  python
pid_28732_luid_0x00000000_0x00010f83_phys_0  dedicated 569,651,200  dwm
pid_28732_luid_0x00000000_0x00010c49_phys_0  shared    382,210,048  dwm
pid_2984_luid_0x00000000_0x00010f83_phys_0  dedicated 184,225,792  msedgewebview2
```

**L20: more PDH explain texts** (`pdh_explain2.py`, the same code with five more paths). Quoted
in §1.2. Also: `Current Disk Queue Length` is an instantaneous snapshot, not an average (the
explain text reads "This is a instantaneous snapshot"); requests minus spindles
"should average less than two".

### Fact-check commands (L21–L27)

All read-only and unelevated, run with `uv run python -I <script>` from
the repo folder (PowerShell where shown). Scripts were in the temporary folder
`%TEMP%\claude\…\scratchpad\research-lag-causes-verify\scripts\`;
outputs sat next to that folder. Exe paths are redacted.

**L21: static probe re-written from scratch** (`probe_static_verify.py`). The D3DKMT part, as run:

```python
KMTQAITYPE_ADAPTERREGISTRYINFO, KMTQAITYPE_ADAPTERTYPE, KMTQAITYPE_WDDM_2_7_CAPS = 8, 15, 70
assert ctypes.sizeof(D3DKMT_ADAPTERINFO) == 20
ea = D3DKMT_ENUMADAPTERS2(0, None); gdi.D3DKMTEnumAdapters2(ctypes.byref(ea))
arr = (D3DKMT_ADAPTERINFO * ea.NumAdapters)(); ea.pAdapters = arr; gdi.D3DKMTEnumAdapters2(ctypes.byref(ea))
def qai(h, typ, obj):
    q = D3DKMT_QUERYADAPTERINFO(h, typ, ctypes.cast(ctypes.byref(obj), ctypes.c_void_p), ctypes.sizeof(obj))
    return gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q)) & 0xFFFFFFFF
reg = (ctypes.c_wchar * (260 * 4))()      # D3DKMT_ADAPTERREGISTRYINFO: 4 x WCHAR[MAX_PATH]
```

Output (registry values for exe paths redacted):

```
EfficiencyClass=0: 4 cores, logical processors [[12], [13], [14], [15]]
EfficiencyClass=1: 6 cores, logical processors [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11]]
{'ACLineStatus': 1, 'BatteryFlag': 1, 'BatteryLifePercent': 100, 'SystemStatusFlag': 0, 'BatteryLifeTime': 4294967295, 'BatteryFullLifeTime': 4294967295}
PowerGetActiveScheme rc=0 64a64f24-65b9-4b56-befd-5ec1eaced9b3 friendly name: Silent
PowerGetUserConfiguredACPowerMode: rc=0 guid=00000000-0000-0000-0000-000000000000
PowerGetUserConfiguredDCPowerMode: rc=0 guid=961cc777-2547-4f9d-8174-7d86181b8a7a
PowerRegisterForEffectivePowerModeNotifications(V2) hr=0 callbacks=[(2, 'Balanced')]
D3DKMTEnumAdapters2 status=0x00000000 adapters=3
luid=0x00000000_0x00010F83 sources=4 name='NVIDIA GeForce RTX 3070 Laptop GPU'
   ADAPTERTYPE status=0x00000000 value=0x2313 ['RenderSupported', 'DisplaySupported', 'HybridDiscrete', 'ACGSupported', 'SupportSetTimingsFromVidPn'] unnamed bits [13]
   WDDM_2_7_CAPS status=0x00000000 value=0xB HwSchSupported=1 HwSchEnabled=1 HwSchEnabledByDefault=0 IndependentVidPnVSyncControl=1
luid=0x00000000_0x00010C49 sources=4 name='Intel(R) UHD Graphics'
   ADAPTERTYPE status=0x00000000 value=0x232B ['RenderSupported', 'DisplaySupported', 'PostDevice', 'HybridIntegrated', 'ACGSupported', 'SupportSetTimingsFromVidPn'] unnamed bits [13]
   WDDM_2_7_CAPS status=0x00000000 value=0x1 HwSchSupported=1 HwSchEnabled=0 HwSchEnabledByDefault=0 IndependentVidPnVSyncControl=0
luid=0x00000000_0x00010EF4 sources=0 name='<status 0xC0000034>'
   ADAPTERTYPE status=0x00000000 value=0x105 ['RenderSupported', 'SoftwareDevice', 'ACGSupported'] unnamed bits []
   WDDM_2_7_CAPS status=0x00000000 value=0x0 HwSchSupported=0 HwSchEnabled=0 HwSchEnabledByDefault=0 IndependentVidPnVSyncControl=0
QueryDisplayConfig rc=0 paths=1 modes=2
\\.\DISPLAY1: monitor='' tech=INTERNAL source adapter=0x00000000_0x00010C49 target adapter=0x00000000_0x00010C49 refresh=342060000/2375360=144.003 Hz
   current 1920x1080@144 Hz; rates offered at this resolution: [60, 144]; modes enumerated: 66
HKCU\System\GameConfigStore 'GameDVR_Enabled' = 1 (type 4)
HKCU\Software\Microsoft\Windows\CurrentVersion\GameDVR 'AppCaptureEnabled': ABSENT
HKCU\Software\Microsoft\Windows\CurrentVersion\GameDVR 'HistoricalCaptureEnabled': ABSENT
HKCU\Software\Microsoft\GameBar 'AutoGameModeEnabled': ABSENT
HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers 'HwSchMode' = 2 (type 4)
HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity 'Enabled' = 1 (type 4)
HKLM\SYSTEM\CurrentControlSet\Control\Power 'DefaultOverlayDcPowerScheme' = '961cc777-2547-4f9d-8174-7d86181b8a7a' (type 1)
HKCU\Software\Microsoft\DirectX\UserGpuPreferences: 7 values
   <exe> = 'GpuPreference=1;' (x2) / 'GpuPreference=1; ' / 'AppStatus=4;' (x2) / 'AppStatus=0;' ; GraphicsFeaturesNotificationConfig = 1
foreground hwnd=0x806A0 pid=28748 iconic=False
```

The bit names come from `D3DKMT_ADAPTERTYPE` in the 19041 `shared\d3dkmthk.h` (bit 2 `SoftwareDevice`;
bit 13 is inside `Reserved`), the caps bits from `D3DKMT_WDDM_2_7_CAPS` in `shared\d3dkmdt.h`
[verified: S44]. A separate check found `PowerGet/SetUserConfiguredAC/DCPowerMode` exported by
powrprof.dll here although no 19041 `um\*.h` header declares them.

**L22: nvidia-smi re-run.** `--query-gpu=<the §1.4 fields>,display_attached --format=csv,noheader`
(output in §1.4), `-q -d TEMPERATURE,PERFORMANCE,POWER`, and a 12-second
`--query-gpu=timestamp,power.draw,power.draw.average,power.draw.instant,temperature.gpu,clocks.gr,utilization.gpu,clocks_event_reasons.active,clocks_event_reasons_counters.sw_thermal_slowdown,clocks_event_reasons_counters.sw_power_cap --format=csv,noheader -l 1`
(stopped by `timeout 12`, hence exit 124):

```
GPU Current Temp 75 C | GPU Target Temperature 75 C | GPU Max Operating Temp 105 C | SW Thermal Slowdown : Active
Average Power Draw 33.21 W | Current Power Limit 88.82 W | Default Power Limit 80.00 W | Min Power Limit 1.00 W | Max Power Limit 105.00 W
2026/11/04 16:47:50.867, 61.37 W, 61.37 W, 61.37 W, 77, 780 MHz, 99 %, 0x0000000000000020, 22684873607 us, 215758142 us
2026/11/04 16:47:51.874, 52.70 W, 52.70 W, 52.43 W, 77, 780 MHz, 97 %, 0x0000000000000020, 22685869899 us, 215799048 us
2026/11/04 16:47:52.879, 44.17 W, 44.17 W, 41.66 W, 76, 780 MHz, 58 %, 0x0000000000000020, 22686869499 us, 215799048 us
...
2026/11/04 16:48:01.970, 55.53 W, 55.53 W, 55.53 W, 78, 780 MHz, 100 %, 0x0000000000000020, 22695866613 us, 216100066 us
```

**L23: thread CPU re-check** (`threads_verify.py`: spawns `sys.executable` to inspect the launcher,
then a 4 s busy loop under `sys._base_executable`, and compares NtQSI snapshots 2 s apart with
`psutil.threads()`):

```
logical processors: 16
launcher pid 25528 name python.exe children [(28340, 'python.exe')]
child pid 11636: 4 threads; busiest tid 29872: NtQSI busy=0.98 psutil busy=0.97
system CPU% over window: 20.8  busiest core 85.5%
MsMpEng.exe pid 23740: NtQSI sees 78 threads; psutil.threads(): 0 threads (no exception)
dwm.exe pid 28732: NtQSI sees 39 threads; psutil.threads(): 0 threads (no exception)
explorer.exe pid 37360: NtQSI sees 144 threads; psutil.threads(): 143 threads (no exception)
```

**L24: handle-free per-process I/O** (`ntqsi_io_verify.py`; the snapshot loop of L8, reading
`struct.unpack_from("<6Q", raw, off + 208)` per process and comparing with
`GetProcessIoCounters`). The struct sizes were first re-derived by declaring the `winternl.h`
layouts in ctypes: `sizeof SYSTEM_PROCESS_INFORMATION 256 UniqueProcessId@ 80 Reserved7@ 208`,
`sizeof SYSTEM_THREAD_INFORMATION 80 ClientId.UniqueThread@ 48`.

```
pid 21056 python.exe:
   NtQSI Reserved7 = (87, 0, 1170, 945380, 0, 98948)
   GetProcessIoCounters = (87, 0, 1170, 945380, 0, 98948)
pid 23740 MsMpEng.exe:
   NtQSI Reserved7 = (344626, 81604, 72506408, 11042144784, 1574772593, 14046483885)
   GetProcessIoCounters = OpenProcess failed err 5
pid 37360 explorer.exe:
   NtQSI Reserved7 = (363221, 46942, 3603280, 215290458, 17669997, 1981084400)
   GetProcessIoCounters = (363221, 46942, 3603280, 215290458, 17669997, 1981084400)
```

**L25: GetThreadTimes granularity and clocks** (`gtt_qpc_verify.py`; `GetThreadTimes` needs
`argtypes = [HANDLE, ...]` in ctypes because the pseudo-handle -2 overflows the default int):

```
distinct GetThreadTimes values in 0.5 s busy loop: 32; step sizes (ms): [15.625]
perf_counter clock info: namespace(implementation='QueryPerformanceCounter()', monotonic=True, adjustable=False, resolution=1e-07)
QPF 10000000; QPC 15231724960160; perf_counter_ns 1523172496017200; perf_counter_ns - QPC*100 = 1200 ns
```

**L26: counters** (PowerShell `Get-Counter -ListSet <13 sets>`; one 2-sample `Get-Counter` of the
§1.2 paths; `pdh_explain_verify.py`, which calls `PdhAddEnglishCounterW` + `PdhGetCounterInfoW`
with `bRetrieveExplainText=TRUE` and asserts `sizeof(PDH_COUNTER_INFO_W) == 112`). All sets exist
except `Processor Performance`. Excerpts:

```
\thermal zone information(\_tz.thrm)\temperature                    352.00
\thermal zone information(\_tz.thrm)\high precision temperature    3,522.00
\processor information(0,0)\processor frequency                     2,300.00
\processor information(0,0)\% processor performance                    43.63
\processor information(0,0)\actual frequency                        1,003.62
\memory\% committed bytes in use                                       85.06
\energy meter(rapl_package0_pkg)\power                             11,534.26
LogicalDisk instances: c:, e:, _total      PhysicalDisk instances: 0 c:, 1 e:, _total
\Processor Information(0,0)\% of Maximum Frequency   type=0x00010000
   ... Use % Processor Performance instead.
```

**L27: what this user may open** (`ecoqos_read_verify.py`: `OpenProcess(0x1000)` +
`GetProcessInformation(4)` + `GetPriorityClass`, then `OpenProcess(PROCESS_SET_INFORMATION=0x0200)`
and an immediate `CloseHandle`; nothing is set):

```
msedgewebview2.exe: GetProcessInformation ok=True err=0 ControlMask=0x4 StateMask=0x0 PriorityClass=0x8000; OpenProcess(PROCESS_SET_INFORMATION) OK
SearchIndexer.exe: OpenProcess(QUERY_LIMITED) failed, err 5
OneDrive.exe: GetProcessInformation ok=True err=0 ControlMask=0x0 StateMask=0x0 PriorityClass=0x20; OpenProcess(PROCESS_SET_INFORMATION) OK
MsMpEng.exe: OpenProcess(QUERY_LIMITED) failed, err 5
dwm.exe: OpenProcess(QUERY_LIMITED) failed, err 5
explorer.exe: GetProcessInformation ok=True err=0 ControlMask=0x0 StateMask=0x0 PriorityClass=0x20; OpenProcess(PROCESS_SET_INFORMATION) OK
```

---

## 10. Summary table

The default thresholds are [inferred] unless a source is given.

| Cause | Signature (key family in **bold**) | Default thresholds | Fix | Sources |
|---|---|---|---|---|
| Lag event detection | **frame timing** | stutter ≥ 2.5× trailing median and ≥ +8 ms; hitch ≥ 50 ms; severe ≥ 150 ms; low-FPS < 80% of target for 2 s; 1% low = average of slowest 1% | n/a | S11, S12, S16, S18, S19, S20 |
| Bound classification | **frame timing** + CPU threads | r = GPUBusy/FT: ≥ 0.90 GPU; ≤ 0.75 and thread ≥ 0.85 CPU; cap: CoV < 3%; VSync: ≥ 90% of intervals quantised | n/a | S1, S3, S7, S23, L7 |
| 4.1 GPU at its limit | **frame timing**, GPU engine | r ≥ 0.9; 3D ≥ 90%; no throttle; clocks ≥ 0.9 × p95 | Manual: resolution, upscaler, settings | S1, S7, S23 |
| 4.2 GPU thermal / power throttle | **GPU telemetry** | Δsw_thermal/Δt ≥ 0.3 (counter, over several seconds); any hw_thermal / hw_power_brake counter time; hw_slowdown flag (no counter); temperature ≥ target (75 °C here); clock −15% | Manual: cooling, OEM mode, AC | S24, S48, L5, L22 |
| 4.3 VRAM overflow | **GPU memory** | adapter dedicated ≥ 90% of VRAM; game shared > 512 MiB or +256 MiB/min; event lift ≥ 2 | Manual: textures, resolution; close GPU apps (consent) | S25, L19 |
| 4.4 CPU main-thread limit | **CPU threads**, frame timing | r ≤ 0.75; busiest thread ≥ 0.85 over ≥ 1 s; no cap or VSync | Manual: CPU-heavy settings | S3, S43, L7, L8, L9 |
| 4.5 CPU thermal / power throttle | **CPU frequency and limits** | % Performance Limit < 100 on ≥ 25% of samples; flags ≠ 0; passive limit < 100; % Processor Performance −20% vs start | Auto: power mode (reversible). Manual: cooling, AC | L1, L3 |
| 4.6 Background interference | **background processes** | others ≥ 2 logical processors, or one ≥ 1; ≥ 20 MB/s on game disk; other 3D ≥ 10% or encoder > 0; lift ≥ 2 | Auto: below-normal priority or EcoQoS on a same-user offender (reversible; restore exact masks). Services (Defender, Update, indexer) cannot be opened unelevated: advice only. Manual: pause downloads | S33, S39, S44, S54, L12, L18, L27 |
| 4.7 RAM pressure | **memory** | Available < 10% (warn) / < 5% (critical); Pages Input/sec > 1000 or event lift ≥ 2; commit > 80% / 90% | Manual: close apps, add RAM | S30, L1 |
| 4.8 Disk / streaming stalls | **disk** | game reads in hitches (NtQSI I/O counters work without a handle); LogicalDisk sec/Read > 15 / 25 ms; LogicalDisk idle < 10%; HDD prior | Manual: SSD | S30, S38, S40, L24 |
| 4.9 Shader compilation | **frame timing** + CPU threads | first-minutes clustering; multi-thread CPU bursts with low reads; repeat run −50%; DXCache mtimes; PSO count (API only) | Manual: pre-compile, play through; Steam pre-caching (Vulkan/GL) | S8, S10, S21, S46, L12 |
| 4.10 DPC/ISR latency | **interrupts** | per-core % DPC > 10%; % Interrupt > 5% / 10%; event lift ≥ 2 | Manual: update or disable drivers; LatencyMon (admin) | S28, S29, S30 |
| 4.11a Game on the iGPU | **GPU engine** | game 3D on the HybridIntegrated LUID, ≈0 on the dGPU | Auto: UserGpuPreferences `GpuPreference=2;` (reversible; next launch) | L4, L6 |
| 4.11b Cross-adapter copy | **static config** + GPU engine | game on the dGPU; window's display target on the iGPU LUID; HybridPresent=1 | Manual: MUX/dGPU ("Ultimate") mode — this model family lists "MUX Switch + Optimus"; dGPU-wired monitor | S6, S26, S42, S52, L4 |
| 4.12 Power plan / battery / mode | **static config** + CPU/GPU limits | AC=0; saver=1; effective mode 0/1; power mode best efficiency; OEM "Silent" | Auto: Best Performance power mode on AC (reversible). Manual: plug in, OEM mode | S34, S35, S36, L4 |
| 4.13 Refresh below max | **static config** | current Hz < max Hz at the current resolution | Auto: dynamic mode change + timed revert (reversible) | S37, L4 |
| 4.14 VSync judder / bad cap / blt present | **frame timing** | quantised ≥ 90% and ≥ 10% at 2P (at (k + 1)P or kP around the usual hold, any k [inferred]); cap C < 0.9 × refresh; PresentMode `Composed: Copy with *` | Manual: VRR, cap = refresh − 3 [unverified], Optimizations for windowed games | S1, S26, S27, S47 |
| 4.15 Overlays / recording | **GPU engine** | other process `videoencode` > 0; encoder util > 0; DVR on | Manual: turn off background recording | S48, L4, L6, L12 |
| 4.16 Frame generation | **frame timing** | FrameType ≠ Application; MsFlipDelay set; presents ≈ k × simulation rate | Manual (judge on displayed and app cadence) | S1, S5, S16, S17 |
| 4.17 HAGS | static config | HwSchEnabled from D3DKMT | Manual; info only (needed for DLSS-FG) | S7, S17, S41, L4 |
| 4.18 Memory Integrity / VBS | static config | Win32_DeviceGuard SecurityServicesRunning contains 2 | Info only; never toggle | S31, L11 |
| 4.19 Game not in focus | **foreground** | foreground PID ≠ game, or iconic, at the event | Manual | S33, L15 |

## Verification log

Independent fact-check, 2026-11-04 16:30–17:15, unelevated (Medium integrity), read-only. Every
GitHub file was fetched again with `curl` from `raw.githubusercontent.com` at the pinned commit
(tags resolved through `api.github.com/repos/<owner>/<repo>/git/refs/tags/<tag>`), PDFs were
re-downloaded and parsed with a new extractor (`pdf_extract.py`), web pages were re-read, and
every local command was re-run with new scripts (L21–L27). Outcomes: **confirmed**, **corrected**
(old → new, fixed in place above), **added** (a fact the implementation needs that was missing),
**unverifiable**.

### PresentMon 2.6.0 (S1–S10, L10, L16)

| Claim | Result | Evidence |
|---|---|---|
| Tag v2.6.0 = commit `e13fce6…` | confirmed (lightweight tag) | refs API |
| Default header uses `TimeInMs`, `CPUStartTimeInMs`; `--qpc_time` gives `TimeInQPC`, `CPUStartQPC`; README says `CPUStartTime` | confirmed | `WriteCsvHeader<pmon::util::metrics::FrameMetrics>`; `CommandLine.cpp` sets `TimeUnit::MilliSeconds` by default; README-ConsoleApplication.md |
| The 30-column header in §1.1 | confirmed | same function with defaults `mTrackDisplay/Input/GPU = true` |
| `--v2_metrics` header "`CPUStartTime,FrameTime,CPUBusy,...,DisplayedTime`" | corrected: it continues past `DisplayedTime` with `AnimationError, AnimationTime, MsFlipDelay` and the input columns; exact list now given | `CsvOutput.cpp` |
| (`--qpc_time_ms` labels its column `TimeInSeconds`) | added | `CsvOutput.cpp` header switch has no case for that unit |
| `MsCPUBusy` = previous app present's start + time-in-present → this present's start | confirmed | `CalculateCPUStart`, `ComputeMsCpuBusy` |
| `MsBetweenAppStart` = `msCPUBusy + msCPUWait` | confirmed; added that missing values print as `0` | `WriteMetricOrZero` in the row writer |
| `MsCPUWait` = time in `Present()` | confirmed | `ComputeMsCpuWait` |
| `MsGPUWait` = `GPUTime − GPUBusy`; `MsGPUTime` = GPU start to ready | confirmed as an identity of the printed columns; refined: `MsGPUWait = max(0, (ready − GPU start) − GPUBusy)` and `MsGPUTime` is printed as busy + wait, i.e. max(start→ready, busy) | `ComputeMsGpuWait`, row writer |
| `MsAnimationError` = previous frame's CPU delta minus display delta | confirmed; exact formula and `NA` cases added | `MetricsCalculatorAnimation.cpp` |
| `MsFlipDelay` from the NV_DD FlipRequest event | confirmed; provider GUID, event ID 1, always enabled | `NvidiaTraceConsumer.cpp`, `ETW/NV_DD.h`, `PresentMonTraceSession.cpp` |
| `SyncInterval` quote "may be modified later by the driver, e.g., control panel overrides" | corrected: "…e.g., based on control panel overrides" | README-ConsoleApplication.md |
| `AllowsTearing` meaning | confirmed | README-ConsoleApplication.md |
| `HybridPresent` set only from DXGI `SwapChain_Start` / `ResizeBuffers_Start` (IDs 0x000A / 0x002D) seen during the capture; enum NOT_HYBRID 0, TWO_COPY_PATH 1, ONE_COPY_PATH_CASO 2 | confirmed; those events are enabled only with `--track_hybrid_present` | `PresentMonTraceConsumer.cpp` lines 413–431 and 2757–2766; `ETW/Microsoft_Windows_DXGI.h`; `PresentMonTraceSession.cpp` |
| `FrameType`: `NotSet`/`Repeated` print as `Application` "in release builds" | corrected: because the `DEBUG_FRAME_TYPE` define is commented out, regardless of build type; unknown values print `Unknown` | `FrameTypeToString` |
| `PresentMode` strings | confirmed | `PresentModeToString`, `PresentEventEnums.hpp` |
| HAGS: GPU busy "~0.5ms larger" | confirmed; added the README's "workload- and GPU-dependent" | README.md |
| OpenGL/Vulkan report runtime `Other` | confirmed | README.md |
| PSO metric names, Direct3D12 events 0x9B/0x9C, provider GUID | confirmed | `metrics.csv` lines 108–110, `ETW/Microsoft_Windows_Direct3D12.h` |
| No PSO column in the console at 2.6.0 or 2.6.1; v2.6.1 `CsvOutput.cpp` same size | confirmed and sharpened: byte-identical (`cmp` exit 0); console never enables the D3D12 provider | L16 re-run; `PresentMonTraceConsumer.hpp` `mTrackD3D12ShaderCompilation = false` |
| Release notes: FPS-Presents/Display/App (v2.3.1), Game Experience preset (v2.6.0) | confirmed; dates added | GitHub releases API |
| Exit code 6 + `access denied.` on stderr, UTF-16LE | confirmed by re-run (BOM `FF FE`) | L10 re-run |
| (exit 6 also means "session name already running" and other start failures) | added | `MainThread.cpp` lines 336–382 |
| (`<unknown>` names and `--terminate_on_proc_exit` need `PROCESS_QUERY_LIMITED_INFORMATION`) | added | `OutputThread.cpp` `QueryProcessName`; stderr warning |
| (clean stop on any console control event or external session stop) | added | `HandleCtrlEvent`, `ConsumerThread.cpp` |
| Recommended flags exist | confirmed | `CommandLine.cpp` help table; shipped `--help` |
| `CPUStartQPC` is raw QPC, same clock as `perf_counter_ns()/100` | confirmed with the full chain | `ClientContext = TIMESTAMP_TYPE_QPC`, `PROCESS_TRACE_MODE_RAW_TIMESTAMP`, S49, L25 |
| `MsBetweenSimulationStart` "needs `--track_pc_latency` and PCL Stats" | corrected: PC Latency markers **or** Intel-PresentMon app timing (`--track_app_timing`) | `ComputeMsBetweenSimulationStarts` |

### Frame-time tools (S11–S16)

| Claim | Result | Evidence |
|---|---|---|
| CapFrameX v1.9.1 = commit `631f2b2…` | confirmed (annotated tag `240b023…` → commit) | refs + tag API |
| StutteringFactor 2.5, StutteringThreshold 25, IntervalAverageWindowTime 500 | confirmed | `CapFrameXConfiguration.cs` |
| Trailing window `N = round(10·√avg_ms)`; > 3× predecessor replaced | confirmed; added that avg is the whole run's mean and the window includes the current frame | `GetMovingAverage`, `SampleBasedMovingAverage.cs` |
| Stutter reported "as % of time (and count %)" | corrected: the count % compares with 2.5 × the run's overall average, not the moving average | `GetStutteringCountPercentage` |
| Low-FPS time = non-stutter frames below 25 FPS | confirmed | `GetLowFPSTimePercentage` |
| Variance bins < 2, < 4, < 8, < 12, ≥ 12 ms | confirmed | `GetVariancePercentages` |
| P1 = MathNet R-8 quantile; MathNet 5.0.0 pinned | confirmed; no competing `Quantile` in `CapFrameX.Extensions.NetStandard` | `ArrayStatistics.QuantileInplace` (v5.0.0), `Directory.Packages.props` |
| 1% low average = mean of frame times ≥ the 0.99 quantile; integral = Afterburner-style | confirmed | `GetPercentageHighAverageSequence`, `GetPercentageHighIntegralSequence` |
| OCAT v1.6.3 = `a7652f6…`; percentiles 0.1…99.9; R-7; avg FPS = frames/seconds | confirmed | `Recording.cpp` lines 726–738, 811–818, 877 |
| FrameView 2.0: "1% Low FPS takes the slowest 1% frames and averages them"; 1% FPS is a percentile; MsBetweenDisplayChange "Should be used to chart displayed FPS"; "old 1% FPS metric would miss…" | confirmed | PDF re-downloaded (SHA-256 `595a4fa4…`), text re-extracted |

### Perception studies (S18–S20)

| Claim | Result | Evidence |
|---|---|---|
| FDG '26: 31 players; spikes 0/75/150/225 ms; "< 50 ms … always acceptable", "225 ms or larger … always unacceptable"; 75 ms "just under 4"; RT 211.86 ms (SD 47.64) | confirmed | paper text; NVIDIA Research page |
| FDG '26: QoE drops "more dramatically" past 150 ms | corrected: the paper says precise-timing tasks show "a more dramatic drop"; success rates fall from 150 ms | paper text (Figures 4, 7) |
| CHI '23: authors, 33 players, 60/120 Hz, 95% floor within about 80% always acceptable, DOI | confirmed; added "within about 40% … almost always" and the 60–120 Hz validity range; variation came from CPU-bound processes | paper text |
| Klein et al. 2023: 4–12 ms variations at 30–240 Hz affect smoothness, not completion time | confirmed | arXiv export API abstract |

### Engines and vendors (S17, S21–S24, S42, S52, S53)

| Claim | Result | Evidence |
|---|---|---|
| Streamline v2.14.16 = `625db07…`; `MsBetweenPresents` "not suitable for measuring frame pacing quality…" | confirmed | `ProgrammingGuideDLSS_G.md` line 829 |
| DLSS-G "requires... Hardware-accelerated GPU Scheduling (HWS)" | corrected: stitched quote replaced by the guide's own words | same file, lines 41–46 |
| `numFramesActuallyPresented` = 2 for one generated frame; Reflex required | confirmed | same file, lines 798–801 and 769 |
| DLSS-G "presents from its own pacer thread" | corrected: "asynchronous presentation mechanism called the 'SL pacer'" (no thread stated) | §13.1 |
| UE 5.8 PSO precaching: hitch threshold 20 ms, first driver-cache retrievals "a few milliseconds", skip draw / default material | confirmed (no longer via a summary) | page re-read |
| Epic tech blog, "tens of milliseconds", UE 5.2 | unverifiable (403 again) | — |
| Club386 Enlisted test "because the frame rate was capped" | corrected: "which is correct since I capped frame rates to 140fps"; title added | article re-read |
| Intel's own GPU Busy article "not found" | partly resolved: Intel's PresentMon page describes GPU Busy but states no rule (S53) | page read |
| NVML reason bits 0x1…0x100 | confirmed; added BoardLimit 0x200, Reliability 0x400 | go-nvml v0.13.4-1 (`251a2f6…` → `1a1e86d…`) `nvml.h` |
| Advanced Optimus 11–27% FPS, up to 22% lower latency; the `.md` URL | confirmed; latency figure is "in popular esports titles" | article read via that URL |
| This laptop's MUX / Advanced Optimus status (open question) | resolved: ASUS lists "MUX Switch + Optimus", GPU mode "Ultimate"; Advanced Optimus not mentioned; GPU "80W+25W Dynamic Boost" | S52 |

### nvidia-smi 591.86 (S48, L5)

| Claim | Result | Evidence |
|---|---|---|
| Field names, query output | confirmed | L22 re-run (exit 0) |
| HW reasons "reducing the core clocks by a factor of 2 or more"; SW thermal "higher than Max Operating Temp" | confirmed as help text; noted that the SW thermal flag fires at the 75 °C target here | help text; L5, L22 |
| Counters are cumulative µs; Δ/Δt method | confirmed, and empirically (≈1,000,000 µs per 1 s sample under continuous capping) | help text; L22 |
| (no counter exists for `hw_slowdown`) | added; §4.2 and §10 corrected | help text "Section about clocks_event_reasons_counters" |
| Target 75 °C, Slowdown 98, Shutdown 101, Max Operating 105 °C | confirmed | `-q` re-run |
| `power.draw.average` reads 1.00 W, so avoid it | corrected: not reproduced in 13 reads; `power.draw` is already a 1 s average on Ampere | L22; help text |
| `enforced.power.limit` above the 80 W default | confirmed (88.82 W); explained by ASUS Dynamic Boost | L22, S52 |
| `display_mode` field | corrected: deprecated ("do not use"); `display_attached` added (`No` here) | help text; L22 |

### Microsoft and other documentation

| Claim | Result | Evidence |
|---|---|---|
| Over budget "may incur stuttering or performance penalties" | confirmed | S25 |
| Flip model "better performance, lower power usage"; hybrid blt "additional work … may affect overall performance" | confirmed; also "bypass desktop composition entirely" for DirectFlip | S26 |
| Optimizations for windowed games (DX10/11, blt → flip, VRR, global and per-app) | confirmed; corrected the quote "lower frame latency" → "reducing frame latency"; per-app control is an opt-out; restart the game | S27 |
| DPC routine "no more than 100 microseconds" | confirmed | S28 |
| LatencyMon repeats ~100 µs | confirmed (page v7.40) | S29 |
| PAL thresholds (DPC > 10%, Processor Information interrupt 5/10%, Available < 10% / < 5% or 64 MB, Pages Input > 1000, commit 80/90%, sec/Read 15/25 ms, idle < 10%) | confirmed; corrected scope: disk rules are on **LogicalDisk**; added PAL's Processor-object interrupt rule 10/20% | `QuickSystemOverview.xml` at `7cfe6d8…` parsed |
| Memory integrity: emulation without MBEC/GMET "bigger impact on performance"; Win32_DeviceGuard values; elevated-shell note; boot-failure warning | confirmed | S31 |
| Memory integrity "needs a reboot" | unverifiable as stated; softened | S31 does not say it for the UI path |
| Microsoft gaming article a255f612 | unverifiable (page renders no article text) | fetch |
| `Win32_OptionalFeature.InstallState` 1–4 | confirmed | S32 |
| QoS: focus High / visible Medium / minimised or occluded Low; Low on battery uses efficient cores; foreground → Medium after inactivity on battery | confirmed; added the automated-test warning and its registry switch | S33 |
| Power-mode GUIDs and APIs | confirmed; added "a vote … can be overridden" | S34 |
| `EFFECTIVE_POWER_MODE` 0–6, V2 = 2, callback once at registration | confirmed | S35, `powersetting.h` |
| `SYSTEM_POWER_STATUS` values | confirmed | S36 |
| `ChangeDisplaySettingsExW` quotes; `CDS_TEST` 0x2, `CDS_UPDATEREGISTRY` 0x1 | confirmed | S37, `winuser.h` |
| `MSFT_PhysicalDisk` MediaType 0/3/4(/5), BusType 17 NVMe | confirmed | S38 |
| Defender `ScanAvgCPULoadFactor` default 50, "not a hard limit"; `ScanScheduleDay` 0 = everyday | confirmed | S39 |
| `GetProcessIoCounters` counts "all I/O operations" | confirmed; handle requirement added | S40 |
| HAGS blog: "offload", "user opt-in, but off by default", "transparent" | confirmed | S41 |
| NtQuerySystemInformation "subject to change" [unverified wording] | corrected to verified | S50 |
| Rule of three: 3/n, n > 30 | confirmed | S45 |
| Steam: pre-caching text names Vulkan and OpenGL; "Processing Vulkan shaders..."; "Shader pre-caching update" | confirmed (lines 215, 4566–4569; `Downloads_ContentType_Shader`) | S46 |
| Blur Busters "3 FPS below" | unverifiable (403 again) | — |
| Tech Report "time beyond 50 ms"; Digital Foundry | unverifiable (not re-fetched: no primary URL in the note) | — |

### Local readouts (L1–L20)

| Claim | Result | Evidence |
|---|---|---|
| P-cores = LPs 0–11 (class 1), E-cores = 12–15 (class 0) | confirmed | L21 |
| AC, 100%, saver off; plan "Silent"; AC mode Balanced, DC best efficiency; effective mode 2 | confirmed | L21, `powercfg /list` |
| D3DKMT shows NVIDIA (HybridDiscrete, HwSchEnabled 1) and Intel (HybridIntegrated) | confirmed; corrected the count: a third, software adapter (0x00010EF4) is enumerated | L21 |
| `ADAPTERTYPE` bit 13 unnamed in the 19041 SDK | confirmed (inside `Reserved`) | `d3dkmthk.h` |
| One display path: internal panel on the Intel LUID, 144.003 Hz, offers [60, 144] | confirmed | L21 |
| Registry values (GameDVR, HwSchMode 2, HVCI 1, overlay DC scheme, UserGpuPreferences) | confirmed; parser pitfalls added | L21 |
| Win32_DeviceGuard 2/2/2, MBEC available; model FX517ZR; both disks NVMe SSD; Defender 50%/idle/daily 02:00; VMP 1 | confirmed | WMI re-run |
| Update/Defender/indexer executables and services; DXCache 109 files 5585.4 MiB; GLCache; D3DSCache; Steam shadercache | confirmed | file and service re-check |
| psutil returns 0 threads for MsMpEng.exe | confirmed; dwm.exe too | L23 |
| NtQSI thread times match `GetThreadTimes` | confirmed (0.98 vs 0.97) | L23 |
| 256/80-byte structs, offsets 80 and 48 | confirmed | L24 ctypes layout |
| (NtQSI `Reserved7[6]` = `IO_COUNTERS`, no handle needed) | added | L24 |
| `GetThreadTimes` steps of 15.625 ms | confirmed | L25 |
| `perf_counter` = QPC, QPF 10 MHz, `perf_counter_ns = QPC × 100` | confirmed | L25 |
| One saturated thread hides in total CPU% | confirmed | L23 |
| venv `python.exe` is a launcher with a child interpreter | confirmed | L23 |
| PDH explain texts and counter sets; `Processor Performance` set absent | confirmed | L26 |
| `% Processor Performance` quote "average performance... while executing instructions…" | corrected: the text reads "while it is executing instructions"; full phrase now quoted | L26 |
| `Current Disk Queue Length` is "an instantaneous snapshot" | corrected: the PDH text reads "a instantaneous snapshot"; now paraphrased | L26 |
| Game Bar setting name "Record what happened" | unverifiable (UI label, no source); tagged | — |
| `Actual Frequency = Processor Frequency × % Processor Performance` | confirmed (2300 × 0.4363 ≈ 1003.6) | L26 |
| GPU engine types, lower-case LUIDs, per-process VRAM | confirmed; software LUID added | L6 re-run |
| EcoQoS/priority readable on explorer.exe, denied on MsMpEng.exe and dwm.exe | confirmed; extended (SearchIndexer denied; `ControlMask=0x4` seen; SET access only for same-user processes) | L27 |
| Foreground PID readable unelevated | confirmed | L21 |
| Worked "1% low" example and the tie-sweep effect | confirmed by an independent re-implementation | L14 re-run |
| The external 2560x1440 display "reported on the NVIDIA adapter" (task brief) | unverifiable here: not connected in either probe | L21, L22 |

Totals, counted from the rows above (111): 82 confirmed (3 of them with a corrected detail),
14 corrected, 6 added, 7 unverifiable, 2 open questions resolved or partly resolved.
