# PresentMon 2.6.0 as lag-doctor's frame-timing source

Research date: 2026-11-04. Scope: the PresentMon **console** exe only (not the Intel PresentMon GUI or its service).

- Binary used for every test: `%LOCALAPPDATA%\lag-doctor\presentmon\2.6.0\PresentMon-2.6.0-x64.exe`,
  980,320 bytes, SHA-256 `B2A706BC6AD475749E3B7E3409263AA1E6906D45BDCF993F6DBC0F660188F1AF`, Authenticode `Valid`,
  `CN=Intel Corporation` (timestamped by Sectigo) [verified: `Get-FileHash` and `Get-AuthenticodeSignature` on that
  path]. It is byte-identical to the official release asset: the GitHub release `v2.6.0` (published
  2026-09-21T17:30:36Z, target branch `release/2.6.0`) lists `PresentMon-2.6.0-x64.exe`, size 980320,
  digest `sha256:b2a706bc…188f1af` [verified: `curl https://api.github.com/repos/GameTechDev/PresentMon/releases/tags/v2.6.0`, V1].
  The file has no version resource; `--help` prints `PresentMon 2.6.0` [verified: V1, V5].
- Source: tag `v2.6.0` = commit `e13fce6acdb55a808fd8318175a56863e532d95f` (authored 2026-08-27 16:50 −07:00,
  committed 2026-08-28 11:10 −07:00; a lightweight tag; also the current head of branch `release/2.6.0`)
  [verified: `git ls-remote https://github.com/GameTechDev/PresentMon.git`, `git cat-file -t refs/tags/v2.6.0` → `commit`, V1].
  That the release exe was built from exactly this commit is [inferred] (release target = that branch, whose head
  is the tag; every replay below behaves as the source at the tag says).
  In this document **`SRC/`** means `https://github.com/GameTechDev/PresentMon/blob/v2.6.0/`; line numbers are at that tag.
- v2.6.1 exists: tag `v2.6.1` = `95d0312ab564d89f4b98278a3501ba347259f6cb`, committed 2026-11-03 14:31 −08:00
  [verified: V1]. It changes **no file** under `PresentMon/`, `PresentData/` or `IntelPresentMon/CommonUtilities/mc/`;
  under `CommonUtilities/` only `log/TextFormatter.cpp` changed, plus `Version.props`; the rest is service, GUI,
  installer, ControlLib and tests [verified: `git diff --name-only v2.6.0 v2.6.1`, V1]. The console exe's log channel
  is a null stub (`SRC/PresentMon/LogSetup.cpp`), so the 2.6.1 console exe should behave the same apart from its
  version string [inferred; the 2.6.1 binary was not tested].

**Tags.** `[verified: …]` = read in the source/doc at that URL, or observed by the local command named (E-numbers
point to the evidence index at the end, which gives each exact command; V-numbers point to the independent re-runs
in the verification log after it). `[inferred]` = reasoned from verified facts but not observed directly.
`[unverified]` = could not be checked here.

**How the CSV facts were tested without admin rights.** Live capture needs admin (section 6), but
`--etl_file` replay does not. The repo ships six real captures with their expected CSVs
(`Tests/Gold/test_case_0..5.etl/.csv`). I replayed them through the exact 2.6.0 binary with every flag
combination. With the flags the gold files were generated with, the output matches `Tests/Gold/test_case_0.csv`
except for the 15th significant digit of four values (floating-point noise) [verified: E4; re-run V2: 357 rows,
identical header, 353 identical rows, 4 fields differing by at most 5.7e-14]. The CSV writer is the
same code for ETL and live capture (`UpdateCsv` is called from the one output thread) [verified:
SRC/PresentMon/OutputThread.cpp#L407-L564].

**Metric definitions** are paraphrased (not quoted) from `SRC/IntelPresentMon/metrics.csv` and
`SRC/README-ConsoleApplication.md#L82-L146`; the exact formula next to each one is read from the code.

---

## 0. Key facts in one screen

1. Default 2.6.0 header has 28 columns and uses **`TimeInMs` and `CPUStartTimeInMs`** (not `CPUStartTime` as the
   README table says). Columns move when flags change, so parse **by header name**, never by position [verified: E4, V2].
2. `--output_stdout` into a **pipe** (what Python's `subprocess.PIPE` gives) is **plain narrow text, no BOM, CRLF**;
   characters U+0000–U+00FF come out as that single byte (Latin-1) and anything above becomes `?` [verified: V5, V9].
   It is UTF-16LE with a BOM only when stdout is an **empty disk file**; `--output_file` is **UTF-8 with a BOM**
   [verified: E3, E6, V5]. (The repo's `docs/learnings.md` line 28 says the console text is UTF-16LE with a BOM;
   that is true only for disk-file redirection.)
3. `--output_stdout` calls `fflush` after the header and after **every row**; `--output_file` has no `fflush`
   (CRT block buffering, tail written at `fclose`) [verified: SRC/PresentMon/CsvOutput.cpp#L1348-L1350, #L1421-L1434].
   Rows still arrive in bursts: the writer thread wakes every 100 ms, ETW delivers real-time buffers at least
   once a second, each displayed frame waits for the next one, and a stuck present can hold back every later row
   for up to 2 s (2.4).
4. Live capture needs **an elevated admin token or membership of Performance Log Users**. Unelevated here it fails
   in 0.05 s with exit code **6** and `error: failed to start trace session: access denied.` on stderr [verified: E7, V6].
5. Clean stop = **CTRL_BREAK** to a child started with `CREATE_NEW_PROCESS_GROUP` in the **same console**, or a second
   instance with `--terminate_existing_session --session_name <name>`. If the Python parent has **no console**,
   `send_signal(CTRL_BREAK_EVENT)` raises `OSError` (WinError 6) and only the second path works [verified: V8].
   `Popen.terminate()`/`kill()` is `TerminateProcess`: the ETW session survives the kill and keeps running until
   stopped or reboot [verified: SRC/PresentMon/MainThread.cpp#L150-L168; StartTraceW docs; E10, V8].
6. A dropped (never displayed) frame has `MsUntilDisplayed` = `NA` (and `MsBetweenDisplayChange` = `NA`) in
   default mode [verified: E4, V2].
7. Rows are **not** in global time order across swap chains, but are in order within one
   `(ProcessID, SwapChainAddress)` [verified: E5, V3].
8. `--track_hybrid_present` adds a `HybridPresent` 0/1 column, but it only learns the hybrid state from a DXGI
   **swap-chain creation** seen during the capture; 0 can mean "not known" [verified: E11 + source, section 9].

---

## 1. The CSV header in 2.6.0

### 1.1 Default (no metrics flag, no optional flags): 28 columns

```
Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,TimeInMs,MsBetweenSimulationStart,MsBetweenPresents,MsBetweenDisplayChange,MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,CPUStartTimeInMs,MsBetweenAppStart,MsCPUBusy,MsCPUWait,MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsAnimationError,AnimationTime,MsFlipDelay,MsAllInputToPhotonLatency,MsClickToPhotonLatency
```
[verified: E4, `etl_default.csv`; writer: SRC/PresentMon/CsvOutput.cpp#L530-L672]

The default time unit is milliseconds since the trace session started, which is why the time columns are
`TimeInMs`/`CPUStartTimeInMs` [verified: SRC/PresentMon/CommandLine.cpp#L633-L636,
SRC/PresentData/PresentMonTraceSession.cpp#L573-L581].

### 1.2 What each flag does to the header (default metrics mode)

Insertion points, in column order: `VidPnSourceId,LayerIndex` after `PresentFlags`; `FrameType`, then
`HybridPresent` after `PresentMode`; `MsPCLatency` after `MsUntilDisplayed`; `MsVideoBusy` after `MsGPUWait`;
`MsInstrumentedLatency` last of the metrics; `PresentId` at the very end (only the hidden `--write_frame_id` extras
`AppFrameId`/`PCLFrameId` can follow it) [verified: SRC/PresentMon/CsvOutput.cpp#L530-L672 and E4, V2].

| Flag | Effect on the default header | Columns |
|---|---|---|
| (none) | as 1.1 | 28 |
| `--qpc_time` | `TimeInMs`→`TimeInQPC`, `CPUStartTimeInMs`→`CPUStartQPC` (raw QPC ticks, integers) | 28 |
| `--qpc_time_ms` | `TimeInMs`→**`TimeInSeconds`** (mislabelled: values are still ms since session start), `CPUStartTimeInMs`→`CPUStartQPCTimeInMs` (absolute QPC converted to ms) | 28 |
| `--date_time` | `TimeInDateTime`, `CPUStartDateTime`, local time like `2020-5-25 3:04:10.765667300` (month, day, hour not zero-padded; 9 fractional digits) | 28 |
| `--track_frame_type` | + `FrameType` | 29 |
| `--track_hybrid_present` | + `HybridPresent` | 29 |
| `--track_app_timing` | + `MsInstrumentedLatency` | 29 |
| `--track_pc_latency` | + `MsPCLatency` | 29 |
| `--write_display_metadata` | + `VidPnSourceId`, `LayerIndex`, `PresentId` | 31 |
| `--track_gpu_video` | + `MsVideoBusy` | 29 |
| `--no_track_gpu` | − `MsGPULatency`, `MsGPUTime`, `MsGPUBusy`, `MsGPUWait`; `--track_gpu_video` is then dropped with `warning: ignoring --track_gpu_video due to --no_track_gpu.` | 24 |
| `--no_track_input` | − `MsAllInputToPhotonLatency`, `MsClickToPhotonLatency` | 26 |
| `--no_track_display` alone | **ignored**, stderr: `warning: ignoring --no_track_display because display tracking is required when GPU tracking is enabled.` | 28 |
| `--no_track_display --no_track_gpu` | − `AllowsTearing`, `PresentMode`, `MsBetweenDisplayChange`, `MsUntilDisplayed`, `MsAnimationError`, `AnimationTime`, `MsFlipDelay` and the 4 GPU columns; also fewer rows (324 vs 357) | 17 |
| `--exclude_dropped` | header unchanged; removes rows that were not displayed (357 → 349) | 28 |
| hidden `--write_display_time --write_frame_id` | + `DisplayTimeAbs` (screen time, ms since start), `FrameId` | 30 |

All rows of this table [verified: E4; re-run independently, same headers, row counts and stderr text: V2]. Hidden
options are parsed in SRC/PresentMon/CommandLine.cpp#L479-L486. `--qpc_time`, `--qpc_time_ms` and `--date_time` are
mutually exclusive: giving two prints `error: only one of the following options may be used: --qpc_time --date_time`
plus the usage text and exits 1 [verified: V2; SRC/PresentMon/CommandLine.cpp#L497-L506].

All optional flags at once (`--track_frame_type --track_hybrid_present --track_app_timing --track_pc_latency
--write_display_metadata --track_gpu_video --qpc_time`), 36 columns [verified: E4]:
```
Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,VidPnSourceId,LayerIndex,AllowsTearing,PresentMode,FrameType,HybridPresent,TimeInQPC,MsBetweenSimulationStart,MsBetweenPresents,MsBetweenDisplayChange,MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,MsPCLatency,CPUStartQPC,MsBetweenAppStart,MsCPUBusy,MsCPUWait,MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsVideoBusy,MsAnimationError,AnimationTime,MsFlipDelay,MsAllInputToPhotonLatency,MsClickToPhotonLatency,MsInstrumentedLatency,PresentId
```

### 1.3 `--v2_metrics` and `--v1_metrics`

`--v2_metrics` (the 2.0–2.3 column set; `FrameType`/`HybridPresent`/display metadata still apply), 23 columns
[verified: E4, V2]:
```
Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,CPUStartTime,FrameTime,CPUBusy,CPUWait,GPULatency,GPUTime,GPUBusy,GPUWait,DisplayLatency,DisplayedTime,AnimationError,AnimationTime,MsFlipDelay,AllInputToPhotonLatency,ClickToPhotonLatency
```
`--v2_metrics` with `--qpc_time` / `--qpc_time_ms` / `--date_time` names the time column `CPUStartQPC` /
`CPUStartQPCTime` / `CPUStartDateTime`; `--track_gpu_video` adds `VideoBusy`; `--track_app_timing` adds
`InstrumentedLatency` [verified: SRC/PresentMon/CsvOutput.cpp#L581-L616, E4 `v2_everything`].

`--v1_metrics` (PresentMon 1.x format), 19 columns; with `--qpc_time --track_gpu_video` it adds
`msGPUVideoActive` and a trailing `QPCTime` (21 columns) [verified: E4, V2]:
```
Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,Dropped,TimeInSeconds,msInPresentAPI,msBetweenPresents,AllowsTearing,PresentMode,msUntilRenderComplete,msUntilDisplayed,msBetweenDisplayChange,msFlipDelay,msUntilRenderStart,msGPUActive,msSinceInput
```
What v1 changes: an explicit `Dropped` 0/1 column; `TimeInSeconds` in seconds; every number printed with 14
decimals and no `NA` (a dropped frame has `msUntilDisplayed` = 0); `SwapChainAddress` zero-padded to 16 hex digits
(`0x00000224B280A1C0`); `--track_frame_type` and `--write_display_metadata` are ignored with a warning; one row
per present with no look-ahead (368 rows vs 357 for the same capture) [verified: E4;
SRC/PresentMon/CsvOutput.cpp#L153-L208, #L308-L386; SRC/PresentMon/CommandLine.cpp#L587-L601].
If both `--v1_metrics` and `--v2_metrics` are given, v2 wins [verified: SRC/PresentMon/CommandLine.cpp#L593-L596].

### 1.4 Header quirks in 2.6.0 that the parser must survive

| Quirk | Evidence |
|---|---|
| `--qpc_time_ms` labels the present-time column `TimeInSeconds` but writes milliseconds since start | [verified: E4, `etl_qpc_time_ms.csv` shows `349.0030` under `TimeInSeconds`] |
| `--no_track_display --no_track_gpu --track_pc_latency`: header has 17 columns, every row has 18 (an unlabelled `MsPCLatency` value) | [verified: E14, E13 counted 324/324 rows malformed] |
| The README's CSV table (`CPUStartTime`, `VideoBusy`, `DisplayLatency`, `DisplayedTime`) describes `--v2_metrics` names, not the default | [verified: E4 vs SRC/README-ConsoleApplication.md#L82-L120] |
| A second header line is written into the same stdout stream whenever recording restarts (`--hotkey`): stopping recording sets the CSV handle to null without closing stdout, and the next row writes a header again | [verified: SRC/PresentMon/OutputThread.cpp#L368-L388, SRC/PresentMon/CsvOutput.cpp#L1421-L1434, #L1465-L1473] |
| `--terminate_existing_session --session_name X` always prints `warning: --terminate_existing_session exits without capturing anything; ignoring all other options.` (the argc check counts the name's value as an extra arg); with stdout not a console it also prints `warning: --no_console_stats added because stdout does not support statistics reporting.` Judge it by the exit code only | [verified: E7, V6; SRC/PresentMon/CommandLine.cpp#L577-L585, #L613-L616] |
| Hidden `--track_etw_status`: with default metrics it adds no column; with `--v1_metrics` the header gains 6 ETW columns (25) but rows keep 19 fields; live, it also `wprintf`s `[ETW Status] …` lines to **stdout** every second, which would corrupt an `--output_stdout` stream. Never use it | [verified: V2 replay (`--v1_metrics --track_etw_status` → header 25, row 19); SRC/PresentMon/CsvOutput.cpp#L195-L202 vs #L308-L386; MainThread.cpp#L84-L100, #L407-L412] |
| The README's option table says `--no_track_display` requires `--no_track_gpu` **and** `--no_track_input`; the code only requires `--no_track_gpu` | [verified: SRC/README-ConsoleApplication.md#L131 vs SRC/PresentMon/CommandLine.cpp#L571-L575; V2] |

---

## 2. What the columns mean

### 2.1 Identity and enum columns

| Column | Meaning | Values / format |
|---|---|---|
| `Application` | Exe name of the presenting process. Live: basename from `QueryFullProcessImageNameW` (needs `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`), or from `Microsoft-Windows-Kernel-Process` start/rundown events; `<unknown>` if neither worked [verified: SRC/PresentMon/OutputThread.cpp#L294-L319, SRC/PresentData/PresentMonTraceConsumer.cpp#L2834-L2860] | string, never quoted, may itself contain a comma (section 10, parser rule 3); in pipe mode U+0000–U+00FF come out as that one byte and anything above becomes `?` [verified: E8, V9]. In ETL replays (only) the name comes from the ANSI `NT_Process` event and is widened byte by byte with sign extension (`std::wstring(str.begin(), str.end())`), so a byte ≥ 0x80 turns into U+FF80–U+FFFF; E8's `Pr\uffe9senter.exe` was produced that way from an ETL patched with byte 0xE9. Live names come from `QueryFullProcessImageNameW`/Kernel-Process as real UTF-16 [verified: SRC/PresentData/PresentMonTraceConsumer.cpp#L2893-L2904; researcher's `nonascii_name.py`] |
| `ProcessID` | PID | decimal |
| `SwapChainAddress` | Swap-chain pointer from the DXGI/D3D9 `Present_Start` event; the per-process key of a swap chain | `0x` + uppercase hex, no padding (`0x224B280A1C0`); `0x0` for runtime `Other` (OpenGL/Vulkan) unless an MPO flip assigns a VidPn layer id [verified: E4, SRC/PresentMon/CsvOutput.cpp#L1131, SRC/PresentData/PresentMonTraceConsumer.cpp#L960-L976] |
| `PresentRuntime` | API that presented | `DXGI`, `D3D9`, `Other` (anything seen only by the kernel, e.g. OpenGL/Vulkan) [verified: SRC/PresentMon/CsvOutput.cpp#L39-L46] |
| `SyncInterval` | Sync interval the app passed; the driver may override it later (e.g. control-panel V-Sync) | integer; `-1` = unknown (D3D9 unless `D3DPRESENT_FORCEIMMEDIATE`, then 0; `Other` runtime); overwritten by the kernel's `FlipInterval` on fullscreen/independent flips [verified: SRC/PresentData/PresentMonTraceConsumer.cpp#L321-L341, #L915-L956] |
| `PresentFlags` | Present flags the app passed | decimal `DXGI_PRESENT_*` bitmask with the Windows SDK `dxgi.h` values: 0x2 `DO_NOT_SEQUENCE`, 0x4 `RESTART`, 0x8 `DO_NOT_WAIT`, 0x10 `STEREO_PREFER_RIGHT`, 0x20 `STEREO_TEMPORARY_MONO`, 0x40 `RESTRICT_TO_OUTPUT`, 0x100 `USE_DURATION`, **0x200 (512) `ALLOW_TEARING`** [verified: `C:\Program Files (x86)\Windows Kits\10\Include\10.0.19041.0\shared\dxgi.h`, V4]. PresentMon's own enum has the same values (SRC/PresentData/ETW/Microsoft_Windows_DXGI.h#L51-L59). The learn.microsoft.com DXGI_PRESENT table is the one that is wrong: it names 0x10 `RESTRICT_TO_OUTPUT`, 0x20 `STEREO_PREFER_RIGHT`, 0x40 `STEREO_TEMPORARY_MONO`; trust the header [verified: https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-present, V4]. D3D9 flags are translated into the DXGI bits 0x2/0x4/0x8. Presents with `DXGI_PRESENT_TEST` (0x1) are dropped entirely, so bit 0 never appears [verified: SRC/PresentData/PresentMonTraceConsumer.cpp#L331-L334, #L2679-L2683]. Observed in the gold captures: 0 (1,266 rows), 2 (181), 512 (1) [verified: E5, V3]. |
| `AllowsTearing` | 1 if partial frames might be shown (the frame was flipped immediately, not on a vblank), 0 if only whole frames | 0/1. Set to 1 by: blt to an on-screen surface, MMIO flip with `FlipImmediate`, flip with `FlipInterval` 0, MPO flip with a non-vsync status [verified: SRC/PresentData/PresentMonTraceConsumer.cpp#L450-L470, #L715-L736, #L923-L932, #L1066-L1075] |
| `PresentMode` | How the system displayed the frame | exactly one of: `Hardware: Legacy Flip`, `Hardware: Legacy Copy to front buffer`, `Hardware: Independent Flip`, `Composed: Flip`, `Composed: Copy with GPU GDI`, `Composed: Copy with CPU GDI`, `Hardware Composed: Independent Flip`, `Other` (mode not determined) [verified: SRC/PresentMon/CsvOutput.cpp#L25-L37; `Other` observed 2× in E5]. Meaning and latency in 8.6 |
| `FrameType` (`--track_frame_type`) | Rendered by the app, or generated by a driver/SDK | `Application` (also printed for not-set and *Repeated* frames), `Intel XeSS-FG`, `AMD AFMF`, `Unknown` (unspecified) [verified: SRC/PresentMon/CsvOutput.cpp#L54-L70]. There is **no NVIDIA value** in the enum [verified: SRC/PresentData/PresentEventEnums.hpp#L36-L43, SRC/PresentData/ETW/Intel_PresentMon.h#L52-L58] |
| `HybridPresent` (`--track_hybrid_present`) | 1 if the present is a hybrid present doing a cross-adapter copy | 0/1 (section 9) |
| `VidPnSourceId`, `LayerIndex`, `PresentId` (`--write_display_metadata`) | Display source, plane index, kernel present id | unsigned integers; 0 in the (older) gold captures [verified: E4] |

### 2.2 Timing columns (all milliseconds, `double`)

Notation: `presentStart` = QPC of the app's `Present()` call; `inPresent` = time spent in that call; `ready` = GPU
finished the frame; `gpuStart` = GPU started it; `screen` = when the frame reached the screen;
`cpuStart` = previous app frame's `presentStart + inPresent` (the moment the previous `Present()` returned)
[verified: SRC/IntelPresentMon/CommonUtilities/mc/MetricsCalculator.cpp#L332-L353]. Deltas use
`DeltaUnsignedMilliSeconds`, which returns 0 when either end is 0 or end ≤ start, unless marked "signed"
[verified: SRC/IntelPresentMon/CommonUtilities/Qpc.cpp#L121-L140].

| Column | Official meaning (paraphrased) | Formula in 2.6.0 | Printed as / missing |
|---|---|---|---|
| `TimeInMs` | when `Present()` was called | `presentStart` − session start | 4 dp |
| `MsBetweenPresents` | time since the previous `Present()` on this swap chain: the classic frame time / presented FPS | Δ(prev `presentStart`, this `presentStart`) | 14 dp |
| `MsInPresentAPI` | time inside `Present()` | `inPresent` | 14 dp; 0 for runtime `Other` |
| `MsRenderPresentLatency` | `Present()` call → GPU finished the frame | **signed** Δ(`presentStart`, `ready`); can be negative | 14 dp |
| `MsBetweenDisplayChange` | how long the previous displayed frame stayed on screen before this one replaced it: displayed frame time | Δ(prev displayed `screen`, this `screen`) | 14 dp; `NA` if 0 (not displayed, or no previous) |
| `MsUntilDisplayed` | `Present()` call → on screen | Δ(`presentStart`, `screen`) | 4 dp; **`NA` = frame not displayed** |
| `CPUStartTimeInMs` | when the CPU started this frame | `cpuStart` − session start | 4 dp |
| `MsCPUBusy` | CPU time spent on the frame before presenting it | Δ(`cpuStart`, `presentStart`) | 4 dp; generated frames print `0.0000` |
| `MsCPUWait` | CPU time spent waiting before the next frame | `inPresent` | 4 dp; generated frames `0.0000` |
| `MsBetweenAppStart` | frame start → CPU starts the next frame (v2's `FrameTime`) | `MsCPUBusy + MsCPUWait` | 4 dp; generated frames `0.0000` |
| `MsGPULatency` | frame start → GPU starts on it | Δ(`cpuStart`, `gpuStart`) | 4 dp; 0 when unknown/generated |
| `MsGPUBusy` | time at least one GPU engine ran **this process's** work for the frame | sum of engine-busy time (`gpuDuration`) | 4 dp |
| `MsGPUWait` | GPU idle inside the frame's GPU span | max(0, Δ(`gpuStart`, `ready`) − `MsGPUBusy`) | 4 dp |
| `MsGPUTime` | total span the GPU worked on the frame | `MsGPUBusy + MsGPUWait` (= max(busy, ready − gpuStart)) | 4 dp |
| `MsVideoBusy` (`--track_gpu_video`) | video encode/decode engines busy for the frame | `gpuVideoDuration` | 4 dp |
| `MsAnimationError` | previous frame's CPU-side delta minus its display delta | (sim-start now − sim-start of last displayed) − (`screen` now − `screen` of last displayed app frame); sim start = `cpuStart` unless app/PCL instrumentation exists | 4 dp, **signed**; `NA` for not displayed, generated, first frame, source switch |
| `AnimationTime` | when the CPU started animation work for this frame | ms from the first sim start (or session start) to this frame's sim start | 4 dp; `NA` if not displayed or generated; `0.0000` (not `NA`) when the value is missing on a displayed app frame, e.g. a source-transition frame [verified: SRC/PresentMon/CsvOutput.cpp#L1284-L1294, MetricsCalculatorAnimation.cpp#L97-L124] |
| `MsFlipDelay` | delay added to when the present was displayed (NVIDIA driver flip scheduling) | NVIDIA `FlipRequest` proposed flip time − event time; `screen` already includes it | 4 dp; `NA` unless NVIDIA reports one |
| `MsAllInputToPhotonLatency` | earliest keyboard/mouse input that fed this frame → on screen | Δ(input time, `screen`); input from dropped frames carries to the next displayed one | 4 dp; `NA` without input |
| `MsClickToPhotonLatency` | earliest mouse click → on screen | Δ(click time, `screen`) | 4 dp; `NA` without click |
| `MsBetweenSimulationStart` | delta between simulation starts | Δ(previous sim start, this sim start); sim start from NVIDIA PCL events, else Intel app-provider events. Those providers are enabled only by `--track_pc_latency` / `--track_app_timing`, so without one of those flags it is always `NA` [verified: MetricsCalculatorInstrumented.cpp#L98-L126; SRC/PresentData/PresentMonTraceSession.cpp#L964-L974, #L986-L990] | 4 dp; `NA` without them |
| `MsPCLatency` (`--track_pc_latency`) | PC input → frame sent to display | NVIDIA PC Latency (Reflex) ETW events | 4 dp; `NA` without them |
| `MsInstrumentedLatency` (`--track_app_timing`) | instrumented frame start → on screen | Δ(app sleep-end or sim-start, `screen`) | 4 dp; `NA` without instrumentation |
| v2 `DisplayLatency` | frame start → on screen | Δ(`cpuStart`, `screen`) | `NA,NA` (with `DisplayedTime`) when not displayed |
| v2 `DisplayedTime` | how long this frame stayed on screen | Δ(`screen`, next frame's `screen`) | |

Formulas [verified: SRC/IntelPresentMon/CommonUtilities/mc/MetricsCalculatorCpuGpu.cpp#L14-L164,
MetricsCalculatorDisplay.cpp#L14-L88, MetricsCalculatorAnimation.cpp#L59-L178,
MetricsCalculatorInput.cpp#L14-L80, MetricsCalculatorInstrumented.cpp#L14-L126,
SRC/PresentData/NvidiaTraceConsumer.cpp]. Printing and `NA`/`0` rules [verified:
SRC/PresentMon/CsvOutput.cpp#L10-L18, #L1121-L1351]. Official one-line definitions:
SRC/IntelPresentMon/metrics.csv (e.g. lines 17, 22, 97, 107) and SRC/README-CaptureApplication.md#L35-L125.

Notes:
- Precision differs by column: 14 decimals for `MsBetweenPresents`, `MsBetweenDisplayChange`,
  `MsInPresentAPI`, `MsRenderPresentLatency`; 4 decimals for the rest. Decimal point is always `.` (C locale);
  no thousands separators [verified: E4 sample rows].
- `MsBetweenAppStart` ≈ `MsBetweenPresents` shifted by the difference in `Present()` duration between consecutive
  frames (`MsBetweenPresents − inPresent(prev) + inPresent(this)`) [inferred from the formulas].
- 2.6.0 has **no `MsDisplayLatency` or `MsDisplayedTime` column**; those values exist only as v2's `DisplayLatency`
  and `DisplayedTime` [verified: E4 headers]. In default mode: display latency = `MsCPUBusy + MsUntilDisplayed`
  (cpuStart→presentStart→screen), and a frame's displayed time = the next displayed frame's
  `MsBetweenDisplayChange` on the same swap chain [verified: E4, first dwm row: v2 `DisplayLatency` 32.7296 =
  default `MsCPUBusy` 16.3000 + `MsUntilDisplayed` 16.4296; v2 `DisplayedTime` 33.3323 = the next default row's
  `MsBetweenDisplayChange` 33.33230000000000. Re-checked on every row of test_case_0: the first identity holds on
  all 349 displayed rows (worst error 6e-14 ms), the second on all 339 rows that have a later displayed frame on the
  same chain: V3].
- Runtime `Other` rows have `MsInPresentAPI` = 0, so `MsCPUWait` = 0 and `MsCPUBusy` = `MsBetweenAppStart`
  [verified: E4 sample `Presenter.exe,3976,0x0,Other,…`]. The README says CPU timing is less accurate for `Other`
  [verified: SRC/README.md#L67-L73].

### 2.3 `NA`, zero and dropped frames

- A missing metric is NaN internally; the writer prints `NA` for it, except `MsBetweenAppStart`, `MsCPUBusy`,
  `MsCPUWait`, which print `0.0000` (`WriteMetricOrZero`) [verified: SRC/PresentMon/CsvOutput.cpp#L10-L18,
  #L1255-L1257]. Several metrics are written as `NA` when exactly 0.0 (`MsBetweenDisplayChange`,
  `MsUntilDisplayed`, v2 `DisplayLatency`/`DisplayedTime`) [verified: same file #L1193-L1214, #L1268-L1277].
- **Dropped frame** (`Discarded`, or presented but never reached the screen) in default mode: `MsUntilDisplayed`
  = `NA`, `MsBetweenDisplayChange` = `NA`, `MsAnimationError` = `NA`, `AnimationTime` = `NA`. Check: test_case_0
  has 8 rows with `MsUntilDisplayed` = `NA`, and `--exclude_dropped` removes exactly 8 rows (357 → 349)
  [verified: E4]. v1 instead prints `Dropped` = 1 [verified: E4].
- NA frequencies across all six gold captures (1,448 rows): `MsBetweenSimulationStart` and `MsFlipDelay` NA in
  every row (no PCL/app instrumentation, no NVIDIA flip delay), input latencies NA in 1,444, `MsUntilDisplayed` NA
  in 104, `AnimationTime` 104, `MsBetweenDisplayChange` 109, `MsAnimationError` 139 [verified: E5, V3].
- `MsGPULatency`, `MsGPUTime`, `MsGPUBusy`, `MsGPUWait` and `MsVideoBusy` never print `NA`: unknown is `0.0000`
  [verified: SRC/PresentMon/CsvOutput.cpp#L1259-L1267; V2: test_case_0 with `--track_gpu_video` has `MsVideoBusy` =
  `0.0000` in all 357 rows and no `NA` in any GPU column].

### 2.4 When rows are emitted (matters for a live reader)

- The **first present on each swap chain never produces a row**: it only seeds that chain's state
  [verified: SRC/PresentMon/OutputThread.cpp#L355-L360].
- Default/v2 mode **holds each displayed frame back until the next displayed frame on the same swap chain is known**
  (needed for `DisplayedTime`); the last displayed frame of a capture is never written. That is why v1 gives 368
  rows and the default 357 for the same capture [verified: SRC/IntelPresentMon/CommonUtilities/mc/MetricsCalculator.cpp#L181-L237,
  MetricsCalculatorDisplay.cpp#L138-L190; E4].
- Dropped frames that arrive while a displayed frame is waiting are queued behind it and released, in order, when
  the next displayed frame arrives; so at the end of a capture the dropped frames after the last displayed frame
  are lost as well [verified: SRC/IntelPresentMon/CommonUtilities/mc/UnifiedSwapChain.cpp#L76-L111].
- Presents leave the consumer's completed-present ring in order. One that is still waiting (e.g. for its
  `Present_Stop`) holds back every later present of every process until it completes or until
  `mDeferralTimeLimit` has passed; the console sets that limit to 2 s of QPC ticks, after which the stuck present
  is force-completed (marked lost if it never saw `Present_Stop`) [verified: SRC/PresentMon/MainThread.cpp#L385-L388,
  SRC/PresentData/PresentMonTraceConsumer.cpp#L2449-L2466]. So a row can arrive up to ~2 s late even with fast
  ETW delivery [inferred].
- With `--track_frame_type` and frame-generation instrumentation, one present can produce **several rows** (one per
  displayed frame, each with its own `FrameType`) [verified: SRC/IntelPresentMon/CommonUtilities/mc/MetricsCalculator.cpp#L187-L227].
- Rows from different swap chains interleave and are **not globally time-ordered** (`TimeInMs` went backwards 54
  times in test_case_0, 255 times in test_case_5); within one swap chain they never went backwards [verified: E5;
  re-run V3: 54/10/19/20/20/255 backward steps in test_case_0..5, 0 within any chain].
- Live only: swap-chain state with no present for 4 s is pruned; the next present re-seeds it (and is not
  emitted) [verified: SRC/PresentMon/OutputThread.cpp#L247-L292, #L264]. The 4 s is measured against the latest
  present time seen on a *target* chain in the same output batch (non-target presents, e.g. DWM under
  `--process_id`, do not advance it). Consequence: with several target chains (unfiltered discovery capture, or a
  game with two chains), a chain that stalls for more than 4 s loses the frame before the stall and the first frame
  after it, so the stall shows up only as a gap in `TimeInQPC`, not as one long `MsBetweenPresents` row. With one
  `--process_id` target on one chain nothing is pruned and the stall appears as one long `MsBetweenPresents`
  [inferred from SRC/PresentMon/OutputThread.cpp#L321-L366, #L434-L555; not observed live].
- Lost or failed presents are skipped silently [verified: SRC/PresentMon/OutputThread.cpp#L437-L440].

---

## 3. GPU-bound or CPU-bound: the GPU Busy metric

### 3.1 What Intel says

- Intel's PresentMon page describes GPU Busy only qualitatively: the metric "shows real time CPU + GPU balance"
  and helps evaluate bottlenecks; it gives **no numeric threshold** [verified: https://game.intel.com/us/intel-presentmon/].
- Intel's launch article (2023-08-18) frames it as how much of the frame time the GPU spends rendering versus
  sitting idle; again no thresholds; it defers to a video [verified: https://game.intel.com/stories/intel-arc-graphics-dx11-intel-presentmon-beta/].
- The repo's docs define the metric (GPU Busy = time at least one GPU engine executes the target process's work)
  but give no interpretation rule [verified: SRC/IntelPresentMon/metrics.csv line 22].
- **The only Intel numeric rules found are from other Intel tools:** Intel's Unreal Engine optimisation guide treats
  **95 % GPU utilisation and above** as GPU-bound [verified: https://www.intel.com/content/www/us/en/developer/articles/technical/unreal-engine-optimization-profiling-fundamentals.html];
  Intel GPA defines its GPU Busy as 100 % for GPU-bound workloads, and anything lower as GPU idle time waiting on the
  CPU [verified: https://www.intel.com/content/www/us/en/docs/gpa/user-guide/2022-4/gpu-metrics.html].
  The same UE guide (Szymon Nowacki, Intel, dated 2025-03-17) also gives a ratio rule on Unreal's own stat numbers:
  Draw time ≈ 95 % of frame time indicates GPU-bound; Game time close to frame time with Draw time under 90 % of frame
  time indicates it can be CPU-bound. It also reads 98.6 % GPU utilisation in PresentMon's (GUI) charts as GPU-bound
  [verified: same UE-guide URL]. None of these is a threshold on PresentMon's `MsGPUBusy`, but the 95 % / 90 % pair is
  the closest Intel-published ratio rule to `MsGPUBusy / frame time`.
- Secondhand: press coverage of Intel's launch material reads it as GPU Busy close to frame time ⇒ GPU-limited, a
  large gap ⇒ GPU idle (CPU-limited or capped), and warns that DLSS 3 frame generation and Reflex make GeForce
  readings unreliable [unverified as Intel's own words: https://www.club386.com/intel-sets-sights-on-identifying-gaming-bottlenecks-with-updated-presentmon-frame-analysis-tool].

### 3.2 Caveats visible in the code and docs

- `MsCPUBusy` is the time from the previous `Present()` returning to this `Present()` call. It includes any waiting the
  game does **outside** `Present()` (frame limiter sleeps, Reflex, waitable swap chains). So it stays close to the
  frame time unless `Present()` itself blocks; it is not proof of a CPU limit [verified: formula in 2.2; the same
  point is made by a third party in https://github.com/GameTechDev/PresentMon/issues/362 (not an Intel maintainer)].
- With Hardware-Accelerated GPU Scheduling, GPU timings can be later/larger than true, e.g. about **+0.5 ms** of GPU
  activity in a GPU-bound scene [verified: SRC/README.md#L75-L84]. This machine has
  `HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\HwSchMode` = 2 [verified: E16, V10]; no Microsoft
  documentation defines that value (only community answers on Microsoft Q&A say 2 = on, 1 = off) [unverified].
  **Do not read the registry; ask the kernel per adapter.** `D3DKMTQueryAdapterInfo(KMTQAITYPE_WDDM_2_7_CAPS = 70)`
  returns `D3DKMT_WDDM_2_7_CAPS` with bit 0 `HwSchSupported`, bit 1 `HwSchEnabled`, bit 2 `HwSchEnabledByDefault`
  [verified: Windows SDK 10.0.19041.0 `shared\d3dkmthk.h` line 2071 and `shared\d3dkmdt.h` `_D3DKMT_WDDM_2_7_CAPS`].
  Run here unelevated (read-only): RTX 3070 Laptop GPU `HwSchSupported=1 HwSchEnabled=1`; Intel UHD
  `HwSchSupported=1 HwSchEnabled=0` [verified: V10]. So HAGS is on for the GPU that renders games on this laptop.
  Snippet and output in 9.
- `MsGPUBusy` counts only the target process's GPU work; work from other processes shows up as lower busy and/or
  higher `MsGPULatency` [inferred from the definition].
- A frame cap or V-Sync also makes GPU Busy ≪ frame time without any CPU limit [inferred; consistent with the
  secondhand coverage above].

### 3.3 Proposed lag-doctor rule [inferred — these thresholds are ours, to be tuned on LagLab]

Per window (e.g. 2 s) on the main swap chain, application frames only (`FrameType` = `Application` when present):
1. `ft` = median `MsBetweenPresents`; `gb` = median `MsGPUBusy`; subtract 0.5 ms from `gb` when HAGS is on for the
   render adapter (`HwSchEnabled` = 1 from `D3DKMTQueryAdapterInfo`, 3.2; the README calls the 0.5 ms an example,
   workload- and GPU-dependent).
2. `gb / ft ≥ 0.90` → **GPU-bound** (Intel's 95 % rules, minus slack for per-frame noise; the UE guide's
   "below 90 % → maybe CPU-bound" line sits inside our 0.75–0.90 grey zone).
3. `gb / ft < 0.75` → GPU is not the limit. Then:
   - **refresh/V-Sync-bound** if `SyncInterval ≥ 1`, `AllowsTearing` = 0 and `MsBetweenDisplayChange` sits within
     3 % of k × refresh period (6.94 ms at 144 Hz, 6.99 ms at 143 Hz);
   - **capped** if `ft` is steady (coefficient of variation < 5 %) at a round cap;
   - otherwise **CPU-bound**, confirmed when a game thread runs near 100 % of one core (system sampler).
4. 0.75–0.90 → mixed/indeterminate.

---

## 4. Encoding and buffering

How the bytes look depends on what the standard handle **is**, because `InitConsole()` runs at startup for
stdout and stderr: a console gets `_setmode(_O_U16TEXT)`; an **empty disk file** gets a `FF FE` BOM written with
`WriteFile` and then `_O_U16TEXT`; anything else (**a pipe**) is left in the CRT's default narrow text mode. There
is no `setlocale` call anywhere, so narrow output goes through the C locale [verified:
SRC/PresentMon/Console.cpp#L13-L55; `grep -rn setlocale|_setmode|setvbuf` over PresentMon/, PresentData/,
IntelPresentMon/CommonUtilities/ finds only Console.cpp#L25, #L40]. The CSV itself is written with `fwprintf`.

| Where the CSV goes | Bytes | Line ends | Flushing |
|---|---|---|---|
| `--output_stdout`, stdout = **pipe** | narrow text, **no BOM**; U+0000–U+00FF are written as that single byte (Latin-1, *not* code page 1252: `€` U+20AC, which cp1252 has as 0x80, became `?`); everything above U+00FF becomes `?` (0x3F) | CRLF | `fflush` after header and every row |
| `--output_stdout`, stdout = **empty disk file** (`> out.csv`) | **UTF-16LE with BOM `FF FE`** (no BOM if the file was not empty) | CRLF | `fflush` after every row |
| `--output_stdout`, stdout = console | wide console output | — | — |
| `--output_file path` | **UTF-8 with BOM `EF BB BF`** (`_wfopen_s(…, L"w,ccs=UTF-8")`) | CRLF | **no `fflush`**: the CRT writes when its buffer fills, the tail only at `fclose` on exit (buffer size not measured) |

Evidence: pipe, file and `--output_file` bytes [verified: E6: `A pipe: 12296 bytes, no BOM, CRLF=51, bare LF=0,
first bytes=41 70 70 6c…`; `B stdout->disk file: 24594 bytes, UTF-16LE with BOM FF FE`;
`C file: 12299 bytes, UTF-8 with BOM EF BB BF`; re-run byte-for-byte identical sizes, and the UTF-16 forms decode
to CRLF line ends: V5]; `?` substitution [verified: E8: `b'Pr?senter.exe'` in the pipe,
`'Pr\uffe9senter.exe'` in the UTF-16 and UTF-8 files — a replay-only name, see 2.1]; Latin-1 pass-through
[verified: V9: `--terminate_existing_session --session_name lagdoctor_éÿ€Ā中_x` (U+00E9 U+00FF U+20AC U+0100 U+4E2D; no such session; exit 7) writes
`… 5f e9 ff 3f 3f 3f 5f 78` into the stderr pipe, while stderr redirected to an empty file holds the name intact in
UTF-16LE]. That probe goes through PresentMon's own `vfwprintf(stderr, …)` (SRC/PresentMon/Console.cpp#L309-L352);
the CSV uses `fwprintf(stdout, …)` of the same statically linked CRT (the exe's import table lists only
`KERNEL32.dll`, V9), so both convert alike [inferred]. The CRT documents that wide output to a text-mode,
non-Unicode stream converts as if by `wctomb` [verified: https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/fopen-wfopen];
flush code [verified: SRC/PresentMon/CsvOutput.cpp#L1348-L1350
(and the same `if (args.mCSVOutput == CSVOutput::Stdout) fflush(fp);` at #L206, #L304, #L384, #L526, #L670,
#L892, #L1117); `_wfopen_s` at #L1382 and #L1425; files closed only in `CloseCsv` #L1465-L1473]. The BOM for
`ccs=UTF-8` is documented: files opened for writing in Unicode mode get a BOM automatically
[verified: https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/fopen-wfopen].

stderr follows the same handle rules. `--help` (printed to stderr, exit code 1) measured three ways
[verified: E3]:
```
--- stderr=PIPE: exit=1 bytes=5435 no BOM
    first 24 bytes: 50 72 65 73 65 6e 74 4d 6f 6e 20 32 2e 36 2e 30 0d 0a 0d 0a 43 61 70 74
    decoded first line: 'PresentMon 2.6.0'; CRLF=86 bare-LF=0
--- stderr=empty disk file: exit=1 bytes=10872 UTF-16LE BOM FF FE
    first 24 bytes: ff fe 50 00 72 00 65 00 73 00 65 00 6e 00 74 00 4d 00 6f 00 6e 00 20 00
--- stderr=non-empty disk file: exit=1 bytes=10871 first 24 bytes: 58 50 00 72 00 65 00 …
```
(`docs/research/presentmon-2.6.0-help.txt` in the repo is 5,349 bytes of ASCII with LF endings, i.e. the pipe
form with CRLF converted; it is not UTF-16.)

**Live arrival timing.** Each row is flushed immediately, but rows are produced in batches: the output thread
sleeps 100 ms between passes [verified: SRC/PresentMon/OutputThread.cpp#L635]; the real-time ETW session leaves
`FlushTimer` at 0, which for real-time sessions means buffers are delivered when full or after a default 1 s
[verified: SRC/PresentData/PresentMonTraceSession.cpp#L492-L500; https://learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-event_trace_properties];
each displayed frame waits for the next displayed frame; and a present stuck in the consumer's ring holds back every
later row for up to 2 s (2.4). Expect rows **0.1–1 s after the frame, occasionally ~2–3 s** [inferred; not measured
live]. Use the timestamps in the rows, never arrival time, to measure gaps. PresentMon's ETW consumer thread runs at
`THREAD_PRIORITY_TIME_CRITICAL` [verified: SRC/PresentMon/ConsumerThread.cpp#L11]; it mostly waits on ETW, but it is
a time-critical thread on the same CPU as the game [inferred].

**Back-pressure.** `fwprintf` to a full pipe blocks the output thread; at shutdown that thread is joined, so a
parent that stops reading can hang PresentMon's exit on CTRL_BREAK (no timeout for that signal)
[inferred from SRC/PresentMon/MainThread.cpp#L441-L446 and the HandlerRoutine timeouts table]. Drain stdout and
stderr continuously until EOF.

---

## 5. Option semantics (2.6.0)

Options may be written `--opt`, `-opt` or `/opt`; option names are case-insensitive [verified:
SRC/PresentMon/CommandLine.cpp#L193-L217]. Numeric values go through `wcstoul(…, 10)` with no validation
[verified: #L244-L250].

| Option | Exact behaviour |
|---|---|
| `--process_name name` | Repeatable (union). Compared after canonicalising both sides: directory removed, **only the last extension** removed, lower-cased with `towlower`. `Presenter.exe`, `PRESENTER.EXE`, `presenter`, `C:\some\dir\Presenter.exe` all match; `Presenter.exe.extra` matches nothing. So for names with dots (`Game.Win64.Shipping.exe`) always pass the full `.exe` name [verified: E9, V7; SRC/PresentMon/OutputThread.cpp#L94-L145; the option names themselves also accept `-process_name` and `/PROCESS_NAME`, V7]. Cannot match a process whose name is `<unknown>` (unelevated, other account) [verified: E7 warning text]. |
| `--exclude name` | Repeatable, same canonicalisation, checked **first**: it beats both `--process_name` and `--process_id` (`--process_id 3976 --exclude presenter.exe` → 0 rows) [verified: E9]. |
| `--process_id id` | One PID; a repeat **overwrites** (`--process_id 3976 --process_id 5988` → only 5988). It also turns on consumer-level PID filtering, so with it, `--process_name` can only add DWM (always tracked): `--process_id 3976 --process_name Presenter.exe` gives only PID 3976. `abc` and `0` parse to 0 = **no filter (every process)**; `3976x` parses as 3976; `-1` wraps to 4294967295 and matches nothing (0 rows) [verified: E9; re-run V7, including `--process_id 1268 --process_name Presenter.exe` → only PID 1268; SRC/PresentMon/MainThread.cpp#L322-L325; SRC/PresentData/PresentMonTraceConsumer.cpp#L3609-L3618]. **Validate the PID before passing it.** |
| no target filter | Every presenting process is a target, **including `dwm.exe`** (several swap-chain keys, 8.3) [verified: E4, E5, E13, V7; SRC/PresentMon/OutputThread.cpp#L127-L130]. |
| `--output_stdout` | CSV to stdout; console output set to *None* (nothing else on stdout); `--multi_csv` ignored with a warning [verified: SRC/PresentMon/CommandLine.cpp#L553-L563; E6]. If `--output_file` is also given, the file wins and stdout is ignored with a warning [verified: E6 case E]. |
| `--output_file path` | UTF-8 BOM file (4); with stdout not a console, PresentMon prints `Started recording.` to **stdout** and `warning: --no_console_stats added because stdout does not support statistics reporting.` to stderr [verified: E6 case C]. |
| `--no_console_stats` | Console mode *Simple*: no live stats; `Started recording.`/`Stopped recording.` lines on stdout when writing a CSV file. No effect with `--output_stdout` [verified: SRC/PresentMon/CommandLine.cpp#L444, MainThread.cpp#L110-L112, #L145-L147]. |
| `--exclude_dropped` | Drops rows whose present was not `Presented` [verified: E4; SRC/PresentMon/CsvOutput.cpp#L1410-L1414]. |
| `--delay s` | Recording starts s seconds after start-up (or after each hotkey press); the ETW session runs meanwhile [verified: SRC/PresentMon/MainThread.cpp#L196-L209]. |
| `--timed s` | Stops **recording** s seconds after recording started (after `--delay`). Without `--terminate_after_timed` the process keeps running, not recording, until killed — always pair them [verified: SRC/PresentMon/MainThread.cpp#L120-L123, #L183-L188]. |
| `--terminate_after_timed` | With `--timed`: normal shutdown after the timer [verified: same lines]. |
| `--terminate_on_proc_exit` | Exits when the count of target processes drops to 0. A target is counted only once its first present (or ProcessStart event) is seen; exit is detected through the `PROCESS_QUERY_LIMITED_INFORMATION` handle or `ProcessStop` events, so without elevation it may never fire [verified: SRC/PresentMon/OutputThread.cpp#L147-L162, #L207-L231; E7 warning]. |
| `--session_name name` | ETW session name (default `PresentMon`); case-insensitive; distinct names allow parallel captures [verified: help text; SRC/PresentMon/CommandLine.cpp#L384]. |
| `--stop_existing_session` | If a session with that name exists: warning, `ControlTraceW(STOP)` by name, retry start. If that session belongs to another **running** PresentMon, that instance shuts down [verified: SRC/PresentMon/MainThread.cpp#L333-L359; inferred for the second sentence from #L22-L37 of ConsumerThread.cpp]. Without it: exit 6 with `error: a trace session named "…" is already running…` [verified: same lines]. |
| `--terminate_existing_session` | Runs before any privilege handling: stop the named session and exit — 0 on success, 7 with `error: no existing sessions found: <name>` (verified unelevated) or `error: failed to terminate existing session (<name>): <code>` [verified: E7; re-run V6 with an absent name and with no `--session_name` (targets `PresentMon`); SRC/PresentMon/MainThread.cpp#L244-L253]. Stopping an existing session needs the same rights as starting one (StartTraceW docs: only admins, Performance Log Users and the service accounts "can control event tracing sessions") [verified: StartTraceW docs; not run against a live session]. |
| `--restart_as_admin` | Only acts for live capture when `SeDebugPrivilege` cannot be enabled: relaunches itself with `ShellExecuteEx` verb `runas` (UAC prompt), args minus this flag, `SW_SHOWDEFAULT`, waits forever, returns the child's exit code; if the launch fails (e.g. UAC cancelled) prints `error: failed to elevate privilege: …` and returns 2 [verified: SRC/PresentMon/Privilege.cpp#L96-L156, MainThread.cpp#L266-L270]. The elevated child does **not** inherit the parent's pipes, so `--output_stdout` would print into the child's own console [inferred: `ShellExecuteEx` passes no handles]. **Not usable for streaming.** Not tested (no elevation allowed). |
| `--hotkey KEY` | Toggles recording; each start re-emits a header on stdout; one file per recording with `--output_file`. Not needed by lag-doctor. |
| `--scroll_indicator` | Uses `SendInput` (synthetic Scroll Lock presses) [verified: SRC/PresentMon/MainThread.cpp#L42-L77]. Avoid near anti-cheat [inferred]. |
| `--set_circular_buffer_size n` | Power of 2, default 2048 presents [verified: SRC/PresentMon/CommandLine.cpp#L520-L526]. |
| `--etl_file path` | Offline analysis, needs no privilege [verified: E4]; missing file → exit 6 `error: failed to start trace session: file not found.` [verified: E6 case G]. |

**Exit codes** [verified: SRC/PresentMon/MainThread.cpp#L220-L475, Privilege.cpp#L125; observed values marked E]:

| Code | Meaning |
|---|---|
| 0 | normal end: ETL finished (E4), Ctrl event, `--terminate_after_timed`, `--terminate_on_proc_exit`, session stopped from outside, `--terminate_existing_session` success |
| 1 | bad command line or `--help` (E3, E6 case F: `error: unrecognized option '--no_such_option'.`; also two time-format flags (V2), a `--set_circular_buffer_size` that is not a power of 2, a reserved or unknown `--hotkey`, CommandLine.cpp#L497-L526); also failure to load its embedded NVIDIA manifest |
| 2 | `--restart_as_admin` could not launch the elevated copy |
| 3 / 4 / 5 | message window class / window / hotkey registration failed |
| 6 | trace session not started: access denied (E7), already exists without `--stop_existing_session`, ETL not found (E6), other |
| 7 | `--terminate_existing_session` failed (E7) |

---

## 6. Privileges

### 6.1 What is required
- `StartTraceW` succeeds only for: administrators, members of **Performance Log Users** (S-1-5-32-559), and
  LocalSystem/LocalService/NetworkService; otherwise `ERROR_ACCESS_DENIED` [verified:
  https://learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-starttracew]. An admin account running
  with its **filtered UAC token is not an administrator** for this check (6.2, 6.3).
- PresentMon's own message names exactly these two options [verified: E7; SRC/PresentMon/MainThread.cpp#L373-L376].
  Joining the group needs an admin once and a sign-out/sign-in [verified: SRC/README.md#L49-L60].
- Elevation additionally gives `SeDebugPrivilege`, which PresentMon enables to open processes of other accounts;
  without it those processes are `<unknown>`, cannot be matched by `--process_name`, and cannot trigger
  `--terminate_on_proc_exit` [verified: E7 warning text; SRC/PresentMon/Privilege.cpp#L27-L66, README.md#L62-L65].
- Enabling `Microsoft-Windows-Kernel-Process` is allowed to fail with access denied (no process start/stop events)
  [verified: SRC/PresentData/PresentMonTraceSession.cpp#L813-L820]; whether a Performance Log Users member hits that
  case is [unverified] (not a group member here).
- Performance Log Users members cannot use the "Windows Kernel Trace" provider in Data Collector Sets
  [verified: https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-security-groups#performance-log-users];
  PresentMon does not use that provider.

### 6.2 Negative test (unelevated, this machine) [verified: E7]
Command (shell PID 32412):
```
PresentMon-2.6.0-x64.exe --process_id 32412 --timed 2 --terminate_after_timed --output_stdout --no_console_stats --stop_existing_session --session_name lagdoctor_probe
```
Result: **exit code 6** after **0.05 s**; **stdout 0 bytes**; **stderr 527 bytes**, plain ASCII (pipe, no BOM):
```
warning: PresentMon requires elevated privilege in order to query processes that are
         short-running or started on another account.  Without it, those processes will
         be listed as '<unknown>' and they can't be targeted by --process_name nor trigger
         --terminate_on_proc_exit.
error: failed to start trace session: access denied.
       PresentMon requires either administrative privileges or to be run by a user in the
       "Performance Log Users" user group.  View the readme for more details.
```
Afterwards `ControlTraceW(EVENT_TRACE_CONTROL_QUERY, "lagdoctor_probe")` returns 4201
(`ERROR_WMI_INSTANCE_NOT_FOUND`): nothing was left behind [verified: E12].
Re-run independently from another shell (PID 16920, same argv with that PID): identical — exit 6 after 0.05 s,
stdout 0 bytes, stderr 527 bytes of ASCII with CRLF and no BOM, the same text; `QUERY "lagdoctor_probe"` → 4201
before and after; no `PresentMon*` process left (`Get-CimInstance Win32_Process`) [verified: V6].

### 6.3 This shell's token (read-only ctypes probe) [verified: E2]
```
TokenElevationType=3 (1=Default,2=Full,3=Limited)  TokenIsElevated=0  IntegritySid=S-1-16-8192
CheckTokenMembership BUILTIN\Administrators S-1-5-32-544 = False
CheckTokenMembership BUILTIN\Performance Log Users S-1-5-32-559 = False
TokenPrivileges (5): SeShutdownPrivilege, SeChangeNotifyPrivilege, SeUndockPrivilege, SeIncreaseWorkingSetPrivilege, SeTimeZonePrivilege
SeDebugPrivilege present in token: False
ControlTraceW(QUERY, 'lagdoctor_probe_does_not_exist') -> 4201
ControlTraceW(QUERY, 'Eventlog-Security') -> 5
QueryAllTracesW -> rc=0 sessions_returned=1
```
Re-run with my own probe: `TokenElevationType=3 TokenElevation=0 IntegritySid=S-1-16-8192`, Administrators
`False`, Performance Log Users `False`, `QUERY` → 4201 for `lagdoctor_probe`/`lagdoctor`/`PresentMon`, 5 for
`Eventlog-Security`, `QueryAllTracesW` → rc 0 with 1 session (`MSDTC_TRACE_SESSION`) although `Eventlog-Security`
exists, `sizeof(EVENT_TRACE_PROPERTIES)` = 120 on x64 [verified: V6].

So `can_trace()` can be implemented without side effects as: `CheckTokenMembership(NULL, S-1-5-32-544)` **or**
`CheckTokenMembership(NULL, S-1-5-32-559)` is true. That mirrors StartTraceW's rule (administrators or Performance
Log Users) exactly, because `CheckTokenMembership` counts a SID only when it is present **and enabled**, so the
filtered UAC token's deny-only Administrators SID answers False and the elevated token's answers True [verified:
https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-checktokenmembership].
`TokenElevation` ≠ 0 is only a proxy: an elevated token need not contain Administrators [inferred]. Unelevated, a
by-name `QUERY` distinguishes "no such session" (4201) from "exists but not yours" (5), while `QueryAllTracesW` lists
only sessions the caller may see — use the by-name query to detect an orphaned lag-doctor session [verified: E2, V6;
inferred that an elevated-created session answers 5].

Snippet (the functions below ran as part of `neg_live.py`, V6; output pasted):
```python
import ctypes, ctypes.wintypes as wt
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True); kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
advapi32.CheckTokenMembership.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.BOOL)]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]

def in_group(sid_str):            # True only if the SID is present AND enabled (deny-only => False)
    p = ctypes.c_void_p()
    assert advapi32.ConvertStringSidToSidW(sid_str, ctypes.byref(p))
    ok = wt.BOOL()
    r = advapi32.CheckTokenMembership(None, p, ctypes.byref(ok))
    kernel32.LocalFree(p)
    return bool(ok.value) if r else f"err{ctypes.get_last_error()}"

def can_trace():                  # StartTraceW: administrators or Performance Log Users
    return in_group("S-1-5-32-544") is True or in_group("S-1-5-32-559") is True
```
```
CheckTokenMembership S-1-5-32-544 (Administrators) = False
CheckTokenMembership S-1-5-32-559 (Performance Log Users) = False
```
(`can_trace()` itself was not run as a separate call; it is the `or` of the two lines above, i.e. False here.)

### 6.4 Elevation strategy for lag-doctor (a decision for the user; see "Still open" at the end of 10)
| Option | Streams live to Python? | Stop path | Cost |
|---|---|---|---|
| A. lag-doctor itself runs elevated (one UAC prompt per run) | yes (normal child + pipes) | CTRL_BREAK, `--terminate_existing_session`, `--timed` | whole tool elevated |
| B. user joins Performance Log Users once | yes, unelevated | same as A | admin action once + sign-out; names of elevated/other-account processes `<unknown>` (target by PID) |
| C. `--restart_as_admin` | **no** (new console) | none from the unelevated parent | only good with `--output_file` + `--timed` |
| D. elevated helper (UAC once) that owns PresentMon and relays rows | yes | helper sends CTRL_BREAK | extra process and IPC |
| E. `ShellExecuteEx(runas)` of PresentMon with `--output_file … --timed S --terminate_after_timed` | no (file flushed at exit) | timer | one prompt per capture; post-hoc analysis only |

[inferred from sections 4–7; A, B match DESIGN.md's planned "relaunch elevated / join the group" offer.]

---

## 7. Clean shutdown

### 7.1 What PresentMon does on each path
`SetConsoleCtrlHandler(HandleCtrlEvent, TRUE)` is installed before the session starts. The handler (any ctrl type)
stops recording, posts `WM_QUIT` to PresentMon's message-only window, then sleeps forever and returns `TRUE`. The
main thread then: `pmSession.Stop()` (disable providers, query lost-event counts, `ControlTraceW(STOP)`,
`CloseTrace`) → join the consumer thread (`ProcessTrace` finishes the buffered events) → join the output thread
(processes the remaining events, writes their rows, `fclose`s CSV files) → prints lost-event warnings to stderr →
returns 0 [verified: SRC/PresentMon/MainThread.cpp#L150-L168, #L215-L218, #L303-L304, #L431-L474;
SRC/PresentData/PresentMonTraceSession.cpp#L598-L635; SRC/PresentMon/OutputThread.cpp#L579-L652].

| Path | Handler runs? | ETW session stopped? | CSV complete? | Notes |
|---|---|---|---|---|
| Ctrl+C typed in its console | yes | yes | yes | Ctrl+C is **disabled** in a child started with `CREATE_NEW_PROCESS_GROUP` |
| `CTRL_BREAK_EVENT` via `GenerateConsoleCtrlEvent` | yes | yes | yes | no timeout for CTRL_C/CTRL_BREAK, so a blocked writer can hang the exit |
| console window closed / Task Manager *End task* (`CTRL_CLOSE_EVENT`) | yes | yes (first step) | yes if done within **5 s** (`SPI_GETHUNGAPPTIMEOUT`), then the system kills it | |
| logoff / shutdown | interactive apps are terminated before these signals | no — after logoff the session lives on until stopped or reboot [inferred] | file tail lost [inferred] | |
| `TerminateProcess` (`Popen.terminate()`/`kill()`, `taskkill /F`) | **no** | **no** | stdout rows already written are whole; `--output_file` loses the unflushed tail | orphan session (7.2) |
| 2nd instance `--terminate_existing_session --session_name X` | n/a | yes | yes: the 1st instance's `ProcessTrace` returns, it calls `ExitMainThread()` and shuts down normally | needs the same privilege as the capture |
| `--timed`+`--terminate_after_timed`, `--terminate_on_proc_exit`, ETL end | n/a | yes | yes | |

Sources: handler and timeouts [verified: https://learn.microsoft.com/en-us/windows/console/handlerroutine];
CTRL+C disabled by `CREATE_NEW_PROCESS_GROUP` (0x00000200), flag ignored with `CREATE_NEW_CONSOLE`
[verified: https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags]; CTRL_C cannot be
limited to a group and is not received by it [verified: https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent];
external stop path [verified: SRC/PresentMon/ConsumerThread.cpp (ProcessTrace returns when the controller stops the
session, then `ExitMainThread()`)]; `Popen.terminate`/`kill` = `TerminateProcess` [verified:
https://docs.python.org/3.13/library/subprocess.html]. Posting `WM_QUIT` to the `PresentMon`/`PresentMonWnd`
message-only window from a same-or-higher-integrity process would also stop it cleanly [inferred from
MainThread.cpp#L280-L288, #L418-L429; untested].

### 7.2 Hard kill leaves the ETW session running
A session stays active until something stops it, the machine restarts, or an I/O error occurs; its creator
exiting is not one of those [verified: StartTraceW remarks,
https://learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-starttracew]. A real-time
session with no consumer keeps its providers enabled and buffers events until a limit, wasting CPU and memory
[verified: EVENT_TRACE_PROPERTIES `LogFileMode` docs]. PresentMon asks for 64 KB buffers, 256–1024 of them
(16–64 MiB) [verified: SRC/PresentData/PresentMonTraceSession.cpp#L498-L500; ETW may adjust the numbers]. Most
systems allow 64 sessions in total [verified: StartTraceW docs]. Microsoft's guidance is to use a descriptive fixed
name and clean up an orphaned session at next start instead of creating another [verified: StartTraceW docs].
After a hard kill the next start with the same name fails with exit 6 unless `--stop_existing_session` is used
[verified: SRC/PresentMon/MainThread.cpp#L336-L353].

### 7.3 Stand-in test of the Python stop mechanics [verified: E10]
PresentMon cannot run live here, so a stand-in child registered a native `SetConsoleCtrlHandler` handler (as
PresentMon does) and the signals were sent from a controller running in its own hidden console:
```
CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT: os.kill ok; worker said 'ready console_cp=437' then 'handler got ctrlType=[1]'; exit=0 after 0.02s
CREATE_NEW_PROCESS_GROUP + CTRL_C_EVENT: os.kill ok; worker said 'ready console_cp=437' then 'handler got ctrlType=[]'; exit=3 after 6.02s
CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT: os.kill ok; worker said 'ready console_cp=437' then 'handler got ctrlType=[]'; exit=3 after 6.03s
controller's own handler saw: []
```
So: CTRL_BREAK reaches a `CREATE_NEW_PROCESS_GROUP` child in the **same console** in 20 ms; CTRL_C does not; and
with `CREATE_NO_WINDOW` the child gets its own console and **`os.kill` reports success while nothing is
delivered**. Re-run with my own stand-in (V8, same three cases, controller in a hidden console): identical —
`ctrlType=[1]` and exit 0 after 0.03 s; CTRL_C nothing (exit 3 after 6.02 s); `CREATE_NO_WINDOW` nothing
(exit 3 after 6.03 s) with `os.kill ok`; the controller's own handler saw nothing.
A parent **without a console** cannot use this path, and it fails loudly rather than silently: after
`FreeConsole()`, `os.kill(pid, signal.CTRL_BREAK_EVENT)` raised `OSError(9, 'The handle is invalid', None, 6)`
and the child (started with `CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP` so no window popped up) got nothing
[verified: V8]. Note for testing: `.venv\Scripts\python.exe` is a launcher, so creation flags such as
`DETACHED_PROCESS` apply to the launcher, not to the real interpreter it starts (docs/learnings.md); test the
no-console case with `FreeConsole()` inside the real process [verified: V8, a first attempt with `DETACHED_PROCESS`
still reported a console].

### 7.4 Recommended stop sequence
1. Start: `creationflags=CREATE_NEW_PROCESS_GROUP` (not `CREATE_NO_WINDOW`, not `DETACHED_PROCESS`), parent attached
   to a console, `stdin=DEVNULL`, stdout and stderr pipes each drained by a thread. Test for a console with
   `kernel32.GetConsoleCP() != 0` (437 with a console, 0 after `FreeConsole()`; `GetConsoleWindow()` is NULL even
   for a windowless console, so do not use it) [verified: V8]. A parent with no console must add
   `CREATE_NO_WINDOW` (else the child gets a console window of its own [inferred]) and stop via step 3.
2. Stop: `proc.send_signal(signal.CTRL_BREAK_EVENT)` inside `try/except OSError` (no console → WinError 6, go
   straight to step 3); keep reading stdout to EOF; `proc.wait(timeout=10)`.
3. If still running: run `PresentMon-2.6.0-x64.exe --terminate_existing_session --session_name lagdoctor` (same
   privilege; `stdin=DEVNULL`, `capture_output=True`, `creationflags=CREATE_NO_WINDOW`) and wait again; judge it by
   the exit code only (its two warnings are expected, 1.4): 0 = stopped, 7 = no such session. The first instance
   then sees `ProcessTrace` return and shuts down normally [verified from source: ConsumerThread.cpp#L30-L38; not
   run against a live capture].
4. Last resort: `proc.kill()`, then step 3 again to remove the orphaned session.
5. Every start uses the fixed name plus `--stop_existing_session`, which also cleans up after a previous crash.

---

## 8. Limitations that matter for games

### 8.1 Anti-cheat
- PresentMon reads ETW events only. Its only access to other processes is
  `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` for the name and exit code; no memory reads, no injection, no
  hooks (`SendInput` only for `--scroll_indicator`, `RegisterHotKey` only for `--hotkey`) [verified:
  `grep -rnE "OpenProcess|ReadProcessMemory|WriteProcessMemory|CreateRemoteThread|VirtualAllocEx|SetWindowsHookEx|LoadLibrary|DuplicateHandle|OpenThread|NtQuery|EnumProcessModules|SendInput|RegisterHotKey"
  PresentMon/ PresentData/` → OutputThread.cpp#L303, MainThread.cpp#L70, #L296; the other hits are
  `LoadLibraryExA` of system DLLs into PresentMon itself and `OpenProcessToken` on its own process].
- PresentMon opens that handle for every new PID whose presents reach its output thread, *before* the target
  check: with `--process_id` that is the game plus `dwm.exe` (DWM presents always pass the consumer filter, which is
  why `--process_name dwm.exe` can add it, V7); unfiltered, every presenting process. Each handle is held until that
  process exits or PresentMon stops [verified: SRC/PresentMon/OutputThread.cpp#L321-L349 (`QueryProcessName` runs
  before `IsTargetProcess`), #L294-L319, #L216-L230, #L638-L646; SRC/PresentData/PresentMonTraceConsumer.cpp#L3609-L3618].
  So the PresentMon child does hold a query-only handle to the game. DESIGN.md principle 1 allows no handles "beyond
  what Task Manager uses" and its sampler section says lag-doctor never calls `OpenProcess` on the game; whether this
  child's `PROCESS_QUERY_LIMITED_INFORMATION` handle fits that rule is a design call to record, not a verified fact.
- Elevated, PresentMon enables `SeDebugPrivilege` in its own token [verified: SRC/PresentMon/Privilege.cpp#L27-L66].
- The repo's docs make **no statement about anti-cheat** (`grep -rniE "anti-?cheat" --include=*.md` over every
  checked-out Markdown file — root, Provider/, Tests/, Tools/generate/ — no hit) [verified: E1]. Whether
  a given anti-cheat strips the query handle (→ `<unknown>` name; target by PID) is [unverified].

### 8.2 OpenGL and Vulkan
Presents not made through DXGI/D3D9 are reconstructed from kernel (DxgKrnl) events: `PresentRuntime` = `Other`,
`SwapChainAddress` usually `0x0`, `SyncInterval` often -1, `MsInPresentAPI`/`MsCPUWait` = 0, less accurate CPU
timing [verified: E4 rows; SRC/README.md#L67-L73; SRC/PresentData/PresentMonTraceConsumer.cpp#L2580-L2620].
LagLab (pyglet/OpenGL) will therefore show as `Other`; target the PID that actually renders (the base interpreter
child, see docs/learnings.md) [inferred].

### 8.3 Several swap chains per process
Rows are keyed by `(ProcessID, SwapChainAddress)`. In test_case_5 `dwm.exe` had **four** keys, not two: two DXGI
chains with 174 rows each (`0x19D7EF5E390`, `0x19D7F1BA8F0`; probably one per display [inferred]) plus two
kernel-only keys with runtime `Other` and 5 rows each, `0x0` and `0x100000000` (= VidPnSourceId 1, layer 0 from
`GenerateVidPnLayerId`); the game `PresentBench.exe` had its DXGI chain (258 rows) plus a `0x0`/`Other` key with 7
rows [verified: V7 replay tally; SRC/PresentData/PresentMonTraceConsumer.cpp#L29-L32, #L960-L976]. Such small
`Other` keys are presents whose runtime event was never matched (e.g. lost events) [inferred from the comment at
SRC/PresentData/PresentMonTraceConsumer.cpp#L2614-L2620]. Choose the game's main swap chain by most displayed frames
over the window (as DESIGN.md plans) and ignore the small keys; launcher/overlay chains have few rows [inferred].

### 8.4 Frame generation
- `FrameType` labels generated frames only for **Intel XeSS-FG** and **AMD AFMF**, and only when the driver/SDK
  emits Intel-PresentMon provider events; it needs `--track_frame_type` [verified: help text,
  SRC/PresentData/PresentMonTraceSession.cpp#L950-L977, CsvOutput.cpp#L54-L70].
- **DLSS frame generation and FSR 3 frame generation have no `FrameType` value**; their generated frames appear as
  ordinary presents [inferred from the enum; behaviour with a real DLSS-FG title unverified]. Expect
  `MsBetweenPresents` near half the rendered frame time; GPU Busy ratios are then not comparable.
- `MsFlipDelay` exposes NVIDIA driver flip scheduling (frame pacing) when reported [verified:
  SRC/PresentData/NvidiaTraceConsumer.cpp].
- For generated frames CPU columns print `0.0000` and GPU columns 0; use app frames only for CPU/GPU-bound
  decisions [verified: SRC/IntelPresentMon/CommonUtilities/mc/MetricsCalculatorCpuGpu.cpp#L14-L117,
  CsvOutput.cpp#L1255-L1257].

### 8.5 VRR (G-SYNC/FreeSync)
No column reports VRR [verified: header lists]. Tearing (`DXGI_PRESENT_ALLOW_TEARING`, sync interval 0, swap chain
created with `DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING`, windowed/borderless only) is a requirement of VRR displays
[verified: DXGI_PRESENT docs]. Heuristic: `AllowsTearing` = 1 with a hardware/independent-flip mode and
`MsBetweenDisplayChange` varying continuously (not quantised to the refresh period) ⇒ VRR or V-Sync off;
quantised to multiples of the refresh period with `SyncInterval` ≥ 1 ⇒ fixed-refresh V-Sync [inferred].

### 8.6 Present modes: what they imply

| `PresentMode` | Situation | Latency / pacing |
|---|---|---|
| `Hardware: Legacy Flip` | fullscreen exclusive, app owns the screen, flips every frame | lowest; tears only if V-Sync off |
| `Hardware: Legacy Copy to front buffer` | fullscreen exclusive, blt into an on-screen surface | can tear |
| `Hardware: Independent Flip` | flip-model window covering the screen, DWM bypassed | as efficient as fullscreen exclusive; can use tearing/VRR |
| `Hardware Composed: Independent Flip` | flip-model swap chain on a hardware overlay plane (MPO) | like independent flip |
| `Composed: Flip` | windowed flip model, DWM composes the frame | DWM composition step in the path; no tearing |
| `Composed: Copy with GPU GDI` / `… CPU GDI` | windowed blt model ("legacy") | extra copies plus composition; worst; on hybrid laptops blt across GPUs costs extra |
| `Other` | mode not determined | — |

Descriptions [verified: SRC/README-ConsoleApplication.md#L135-L146]; independent flip matching fullscreen-exclusive
efficiency, MPO, waitable-object latency and the hybrid-GPU blt cost [verified:
https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/for-best-performance--use-dxgi-flip-model]; the
"composition adds latency" and "worst" judgements are [inferred]. `MsUntilDisplayed`/`MsBetweenDisplayChange`
already include the composition delay because `screen` is the real flip time [inferred from 2.2].

---

## 9. `--track_hybrid_present`

- **Column:** `HybridPresent`, inserted after `PresentMode`/`FrameType`; printed with `%d` from a `bool`: `0` or `1`
  [verified: SRC/PresentMon/CsvOutput.cpp#L552-L554, #L1149-L1151; E4].
- **Source:** the flag adds DXGI `SwapChain_Start` (event 10) and `ResizeBuffers_Start` (event 45) to the session
  [verified: SRC/PresentData/PresentMonTraceSession.cpp#L918-L930]. When one arrives with both fields
  `pIDXGISwapChain` and `HybridPresentMode`, the value is stored per (PID, swap chain); a present on that swap
  chain gets `1` when the value is not `NOT_HYBRID` (0). Values: 0 `NOT_HYBRID`, 1 `TWO_COPY_PATH`,
  2 `ONE_COPY_PATH_CASO` [verified: SRC/PresentData/PresentMonTraceConsumer.cpp#L413-L433, #L2757-L2767;
  SRC/PresentData/ETW/Microsoft_Windows_DXGI.h#L61-L65].
- **Meaning:** on a hybrid system the render GPU (here the RTX 3070) draws, and the frame is copied to the display
  GPU (here the Intel UHD) for scan-out: two copies, or one with cross-adapter scan-out (CASO, Windows 11 /
  WDDM 2.9+). Microsoft says the copies can limit performance, especially for low-latency apps [verified:
  https://learn.microsoft.com/en-us/windows-hardware/drivers/display/supporting-caso].
- **This machine's DXGI manifest** has `HybridPresentMode` on both events, but the swap-chain field of
  `ResizeBuffers_Start` is spelled **`pIDXGISwapchain`** (lower-case c), while PresentMon looks up
  `pIDXGISwapChain` with case-sensitive `wcscmp` [verified: E11 output below; re-run V11 with my own TDH script,
  same names (event 10 has 46 top-level properties, event 45 has 25); SRC/PresentData/TraceConsumer.cpp#L248
  (`wcscmp`), #L200-L204 (found count returned), PresentMonTraceConsumer.cpp#L421-L430 (stored only if both found)].
  So on this Windows build only swap-chain **creation** can set the flag [inferred from both].
  ```
  event id=10 … properties=['pIDXGISwapChain', …, 'HybridPresentMode', 'ScanoutEligible', 'CrossAdapterTierSupport', 'CASOFallbackReason', …, 'InternalDisplay', 'PluggedIn']
  event id=45 … properties=['pIDXGISwapchain', …, 'HybridPresentMode', 'ScanoutEligible', 'CrossAdapterTierSupport', 'CASOFallbackReason', …]
  ```
- **Caveats:** DXGI gets no rundown/capture-state request, so a swap chain created **before** the capture started
  is never flagged: `HybridPresent` = 0 then means "unknown" [verified: SRC/PresentData/PresentMonTraceSession.cpp
  requests CAPTURE_STATE only for DxgKrnl (#L867-L874) and, when `mTrackProcessState` is set, Kernel-Process
  (#L815-L820); the console exe never sets `mTrackProcessState` (default `false`,
  SRC/PresentData/PresentMonTraceConsumer.hpp#L341; not assigned in SRC/PresentMon/MainThread.cpp#L312-L325), so in
  practice only DxgKrnl gets a rundown]. The flag is applied in `RuntimePresentStop`, keyed by (PID, swap-chain
  pointer), so only DXGI swap chains seen in `SwapChain_Start` can be 1; D3D9 and `Other` (OpenGL/Vulkan) never are
  [verified: SRC/PresentData/PresentMonTraceConsumer.cpp#L2714, #L2757-L2767]. All 1,448 gold rows were 0
  [verified: E5, V3]. Microsoft implements CASO (the one-copy path) only for the DXGI flip model, available from
  Windows 11 / WDDM 2.9 [verified: https://learn.microsoft.com/en-us/windows-hardware/drivers/display/supporting-caso].
- **How to use it on this laptop:** start the capture before the game creates its swap chain (before launch, or
  ask for a display-mode change that recreates it); `HybridPresent` = 1 on the game's chain ⇒ rendering on one GPU
  and copying to a display driven by the other — expected when a dGPU-rendered game is on the internal 1920×1080
  panel (Intel adapter), not on the 2560×1440 monitor (NVIDIA adapter) [inferred]. Cross-check independently: the
  adapter whose 3D engine the game uses (PDH, planned in DESIGN.md) versus the adapter that drives the monitor the
  game window is on [inferred].
- **Adapter roles and HAGS without ETW or admin.** `D3DKMTEnumAdapters2` + `D3DKMTQueryAdapterInfo` (gdi32.dll)
  with `KMTQAITYPE_ADAPTERREGISTRYINFO = 8` (name), `KMTQAITYPE_ADAPTERTYPE = 15` (`D3DKMT_ADAPTERTYPE` bits:
  0 `RenderSupported`, 1 `DisplaySupported`, 2 `SoftwareDevice`, 4 `HybridDiscrete`, 5 `HybridIntegrated`) and
  `KMTQAITYPE_WDDM_2_7_CAPS = 70` (bit 1 `HwSchEnabled`) [verified: Windows SDK 10.0.19041.0
  `shared\d3dkmthk.h` lines 1992, 1999, 2071 and `_D3DKMT_ADAPTERTYPE`, `_D3DKMT_ADAPTERINFO` (`hAdapter`
  UINT, `AdapterLuid`, `NumOfSources`, `bPresentMoveRegionsPreferred`), `_D3DKMT_QUERYADAPTERINFO`; `shared\d3dkmdt.h`
  `_D3DKMT_WDDM_2_7_CAPS`]. Core of `hags_caps.py` (ran unelevated, V10):
  ```python
  class D3DKMT_ADAPTERINFO(ctypes.Structure):
      _fields_ = [("hAdapter", wt.UINT), ("AdapterLuid", LUID), ("NumOfSources", wt.ULONG),
                  ("bPresentMoveRegionsPreferred", wt.BOOL)]
  class D3DKMT_ENUMADAPTERS2(ctypes.Structure):
      _fields_ = [("NumAdapters", wt.ULONG), ("pAdapters", ctypes.POINTER(D3DKMT_ADAPTERINFO))]
  class D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
      _fields_ = [("hAdapter", wt.UINT), ("Type", ctypes.c_int), ("pPrivateDriverData", ctypes.c_void_p),
                  ("PrivateDriverDataSize", wt.UINT)]
  def q(h, typ, buf):   # returns NTSTATUS
      qi = D3DKMT_QUERYADAPTERINFO(h, typ, ctypes.addressof(buf), ctypes.sizeof(buf))
      return gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(qi)) & 0xFFFFFFFF
  # D3DKMTEnumAdapters2 twice (count, then fill); per adapter q(h, 15, UINT) and q(h, 70, UINT); D3DKMTCloseAdapter
  ```
  ```
  D3DKMTEnumAdapters2 status=0x00000000 adapters=3
  LUID 00000000-00010F83 sources=4 name='NVIDIA GeForce RTX 3070 Laptop GPU'
     ADAPTERTYPE status=0x00000000 value=0x00002313 Render=1 Display=1 Software=0 HybridDiscrete=1 HybridIntegrated=0
     WDDM_2_7_CAPS status=0x00000000 value=0x0000000B HwSchSupported=1 HwSchEnabled=1 HwSchEnabledByDefault=0
  LUID 00000000-00010C49 sources=4 name='Intel(R) UHD Graphics'
     ADAPTERTYPE status=0x00000000 value=0x0000232B Render=1 Display=1 Software=0 HybridDiscrete=0 HybridIntegrated=1
     WDDM_2_7_CAPS status=0x00000000 value=0x00000001 HwSchSupported=1 HwSchEnabled=0 HwSchEnabledByDefault=0
  LUID 00000000-00010EF4 sources=0 name='(status 0xC0000034)'
     ADAPTERTYPE status=0x00000000 value=0x00000105 Render=1 Display=0 Software=1 HybridDiscrete=0 HybridIntegrated=0
     WDDM_2_7_CAPS status=0x00000000 value=0x00000000 HwSchSupported=0 HwSchEnabled=0 HwSchEnabledByDefault=0
  ```
  (The third adapter is the software render device; its registry-info query failed with 0xC0000034.) LUIDs change
  across reboots, so match adapters by name or by LUID read in the same boot [inferred].

---

## 10. Recommended invocation for lag-doctor

### (a) Live capture of one game, by PID, streamed to Python
Run in a context that can trace (6.4 A or B). Pass an argv list (no shell):
```
%LOCALAPPDATA%\lag-doctor\presentmon\2.6.0\PresentMon-2.6.0-x64.exe --process_id <PID> --output_stdout --no_console_stats --qpc_time --track_frame_type --track_hybrid_present --session_name lagdoctor --stop_existing_session --terminate_on_proc_exit
```
Header these flags produce (30 columns) [verified 2026-11-09: ETL replays of test_case_5 and test_case_0 with the
same flags minus the live-only `--process_id`/session/terminate options, `tests/fixtures/presentmon/regenerate.py`;
until that day lag-doctor also passed `--track_pc_latency`, which adds `MsPCLatency` after `MsUntilDisplayed` (31
columns, V2 and V7)]:
```
Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,PresentMode,FrameType,HybridPresent,TimeInQPC,MsBetweenSimulationStart,MsBetweenPresents,MsBetweenDisplayChange,MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,CPUStartQPC,MsBetweenAppStart,MsCPUBusy,MsCPUWait,MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsAnimationError,AnimationTime,MsFlipDelay,MsAllInputToPhotonLatency,MsClickToPhotonLatency
```
Why each flag: PID filter (validated positive integer first, 5); `--qpc_time` makes `TimeInQPC`/`CPUStartQPC` raw
QPC ticks of the same clock as `QueryPerformanceCounter` (the session's `ClientContext` is QPC; frequency here
10,000,000/s; the trace is opened with `PROCESS_TRACE_MODE_RAW_TIMESTAMP`) so frames align with the other collectors
[verified: SRC/PresentData/PresentMonTraceSession.hpp#L39, PresentMonTraceSession.cpp#L494, #L522; E12, V6];
`--track_frame_type` only adds data when the driver or game provides it [inferred; it enables the Intel-PresentMon
provider, PresentMonTraceSession.cpp#L950-L977];
`--track_hybrid_present` for this hybrid laptop (9); fixed session name + `--stop_existing_session` (7.2);
`--terminate_on_proc_exit` is a convenience only — lag-doctor should still watch the game PID itself (5).
Do **not** use: `--track_pc_latency` (it enables NVIDIA's PC Latency provider, which switches on the latency-ping
code inside games built with its markers, for data lag-doctor does not keep: `docs/research/overhead.md`, section
5.1; dropped 2026-11-09, DECISIONS), `--exclude_dropped` (drops are a stutter signal), `--v1_metrics`/`--v2_metrics`,
`--hotkey`, `--scroll_indicator`, `--restart_as_admin`, `--no_track_display --no_track_gpu --track_pc_latency`
together, the hidden `--track_etw_status` (1.4), or more than one of `--qpc_time`/`--qpc_time_ms`/`--date_time`
(exit 1).

### (b) Finding the game automatically
Primary: DESIGN.md's PDH method (highest 3D-engine use + visible window). PresentMon cross-check / fallback, a
bounded unfiltered capture:
```
%LOCALAPPDATA%\lag-doctor\presentmon\2.6.0\PresentMon-2.6.0-x64.exe --output_stdout --no_console_stats --qpc_time --exclude dwm.exe --timed 5 --terminate_after_timed --session_name lagdoctor --stop_existing_session
```
Rank `(ProcessID, Application)` by displayed frames per second over the last 3 s; drop the ignore list (browsers,
`steamwebhelper.exe`, Discord, OBS, `explorer.exe`, the tool itself); prefer the foreground window's PID and
hardware/independent-flip modes; then restart with (a) and that PID. A PID that PDH picks but that has no rows here
is not presenting (launcher) [inferred]. The `--exclude` behaviour [verified: E9 / ETL test_case_5 run: only
`PresentBench.exe` and `steamwebhelper.exe` rows remained; re-run V7: PresentBench 258 + 7 rows, steamwebhelper 24].
Two costs of the unfiltered capture: PresentMon opens a query handle to **every** presenting process (8.1), and with
several target chains the 4 s pruning can hide a long stall (2.4) — use it only for the 5 s discovery window.

### Parser rules
1. **Bytes:** read stdout as binary lines. If the stream starts with `FF FE` decode UTF-16LE; with `EF BB BF`
   UTF-8; otherwise (the pipe case) decode **latin-1**: it never raises and is exact, because PresentMon writes
   U+0000–U+00FF as single bytes; a `?` in a name means a character above U+00FF was lost. Strip the trailing CR
   [verified: section 4, V5, V9].
2. **Header:** a line starting with `Application,ProcessID,` is a header (every mode starts that way [verified:
   E4]; a data row has a number in the second field); build `name → index` from it and re-build it whenever another
   header line appears (recording restarts) [verified: 1.4]. Require the columns you use; never use positions.
3. **Rows:** split on `,`. Nothing is quoted [verified: format strings in CsvOutput.cpp#L1131-L1346]; the only field
   that can contain a comma is `Application`, because a comma is legal in a Windows file name [verified:
   https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file — not in the reserved list]. So when a row
   has k extra fields, rejoin the first k+1 fields into `Application` **only if** the next two fields still look like
   a PID and `0x…`; otherwise (and for any other count mismatch) count the row as malformed and skip it. This keeps
   the 2.6.0 extra-column bug (1.4) detected [verified: E13 self-test and replays below]. `Application`,
   `ProcessID`, `SwapChainAddress` are the first three columns in every mode [verified: E4].
4. **Values:** `NA` → missing. `SwapChainAddress` → `int(v, 16)`. `ProcessID`, `SyncInterval` (−1 allowed),
   `PresentFlags`, `AllowsTearing`, `HybridPresent`, `TimeInQPC`, `CPUStartQPC`, `VidPnSourceId`, `LayerIndex`,
   `PresentId` → `int`. Everything else `float`, negatives allowed (`MsRenderPresentLatency`, `MsAnimationError`).
   `Application`, `PresentRuntime`, `PresentMode`, `FrameType` → strings.
5. **Dropped frame** ⇔ `MsUntilDisplayed` is missing. For generated frames (`FrameType` ≠ `Application`) treat CPU
   columns as missing even though they print `0.0000`.
6. **Swap chains:** keep state per `(ProcessID, SwapChainAddress)`; sort within a chain by `TimeInQPC` only if
   needed (already ordered); never assume global order; `0x0` is a valid key, and a game can have a few-row
   `0x0`/`Other` key next to its real DXGI chain (8.3): pick the main chain by displayed frames.
7. **Gaps and stalls:** measure from row timestamps, not arrival times (rows arrive 0.1–1 s late in bursts,
   occasionally ~2–3 s). Detect a stall as a jump in `TimeInQPC` between consecutive rows of the chain as well as a
   large `MsBetweenPresents`: with several target chains a > 4 s stall leaves only the gap (2.4) [inferred].
8. **Keep draining** stdout and stderr until EOF, also after asking it to stop.

Reference reader, run against ETL replays and a synthetic self-test [verified: E13]:
```
selftest: rows=[('My,Game.exe', 4242, '0x1a2b')] rejoined=1 malformed=1
--- default flags, test_case_5:
exit=0 headers=1 malformed_rows=0 stderr=[]
  PresentBench.exe pid=24892 swapchain=0x2A70D2CAC00 rows=258 dropped=84 median MsBetweenPresents=11.051 median MsGPUBusy=0.936 GPUBusy/frame=0.08 modes=['Composed: Flip']
  dwm.exe pid=2656 swapchain=0x19D7EF5E390 rows=174 dropped=0 median MsBetweenPresents=16.663 median MsGPUBusy=0.504 GPUBusy/frame=0.03 modes=['Hardware: Legacy Flip']
  dwm.exe pid=2656 swapchain=0x19D7F1BA8F0 rows=174 dropped=0 median MsBetweenPresents=16.658 median MsGPUBusy=0.562 GPUBusy/frame=0.03 modes=['Hardware: Legacy Flip']
  steamwebhelper.exe pid=3980 swapchain=0x21C48E8A710 rows=24 dropped=0 median MsBetweenPresents=16.631 median MsGPUBusy=0.932 GPUBusy/frame=0.06 modes=['Composed: Flip']
--- known-bad combo (header/row mismatch), test_case_0:
exit=0 headers=1 malformed_rows=324 stderr=[]
```
(The numbers above re-check exactly with an independent script, V7. The printout shows only the four large chains;
the same replay also has three small `Other` keys — PresentBench `0x0` 7 rows, dwm `0x0` and `0x100000000` 5 rows
each — see 8.3.)
Core of that reader (`pm_reader_demo.py`):
```python
INT_COLS = {"ProcessID", "SyncInterval", "PresentFlags", "AllowsTearing", "HybridPresent", "VidPnSourceId",
            "LayerIndex", "PresentId", "FrameId", "TimeInQPC", "CPUStartQPC"}
STR_COLS = {"Application", "PresentRuntime", "PresentMode", "FrameType", "TimeInDateTime", "CPUStartDateTime"}

def convert(col, v):
    if v == "NA": return None
    if col == "SwapChainAddress": return int(v, 16)
    if col in STR_COLS: return v
    if col in INT_COLS: return int(v)
    return float(v)

def iter_rows(stream, stats):                 # stream = proc.stdout opened in binary mode
    cols = None
    for raw in stream:
        line = raw.decode("latin-1").rstrip("\r\n")
        if not line: continue
        if line.startswith("Application,ProcessID,"):
            cols = line.split(","); stats["headers"] += 1; continue
        fields = line.split(",")              # nothing is quoted; only Application may contain a comma
        if cols is not None and len(fields) > len(cols):
            k = len(fields) - len(cols)
            cand = [",".join(fields[:k + 1])] + fields[k + 1:]
            if cand[1].isdigit() and cand[2].startswith("0x"):
                fields = cand; stats["rejoined"] += 1
        if cols is None or len(fields) != len(cols):
            stats["malformed"] += 1; continue
        yield {c: convert(c, v) for c, v in zip(cols, fields)}
```

### Start and shutdown sequence
```python
import ctypes, os, signal, subprocess
PM = os.path.expandvars(r"%LOCALAPPDATA%\lag-doctor\presentmon\2.6.0\PresentMon-2.6.0-x64.exe")
if not (isinstance(pid, int) and 0 < pid <= 0xFFFFFFFF):   # "0"/"abc" would mean "every process", "-1" nothing
    raise ValueError(f"bad PID {pid!r}")
has_console = ctypes.WinDLL("kernel32").GetConsoleCP() != 0   # 0 = no console (V8)
flags = subprocess.CREATE_NEW_PROCESS_GROUP | (0 if has_console else subprocess.CREATE_NO_WINDOW)
args = [PM, "--process_id", str(pid), "--output_stdout", "--no_console_stats", "--qpc_time",
        "--track_frame_type", "--track_hybrid_present",
        "--session_name", "lagdoctor", "--stop_existing_session", "--terminate_on_proc_exit"]
proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=flags)
# reader thread: for row in iter_rows(proc.stdout, stats): ...   stderr thread: keep the tail for diagnostics

def terminate_session():                           # second instance stops the ETW session by name
    return subprocess.run([PM, "--terminate_existing_session", "--session_name", "lagdoctor"],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                          creationflags=subprocess.CREATE_NO_WINDOW).returncode   # 0 stopped, 7 none

# stop:
try:
    proc.send_signal(signal.CTRL_BREAK_EVENT)      # handler -> stop session -> flush -> exit 0
    proc.wait(timeout=10)                          # reader thread keeps draining stdout meanwhile
except (OSError, subprocess.TimeoutExpired):       # OSError = no console (WinError 6)
    terminate_session()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()                                # TerminateProcess: leaves the session ...
        terminate_session()                        # ... so remove it
```
What was run: the `Popen` flags, binary pipe reading, stderr thread and `iter_rows` [verified: E13 on ETL replays];
CTRL_BREAK delivery, the CTRL_C / `CREATE_NO_WINDOW` failures, the `OSError` without a console and the
`GetConsoleCP()` test [verified: E10, V8 with a stand-in]; `--terminate_existing_session` exit 7 for an absent
session [verified: V6]. The whole block was not run as one program, and the live CTRL_BREAK → exit 0 path and the
`--terminate_existing_session` fallback against a running capture need admin and were not run [unverified live;
behaviour read from source, 7.1]. Exit code 6 at start-up means "cannot trace": read stderr and report frames as
*not measured*.

### Still open (needs one elevated run, a real game, or the user)
1. Elevation strategy (6.4: A relaunch elevated, B Performance Log Users, D elevated helper, E per-capture file)
   — a decision for the user.
2. Live behaviour not testable here: CTRL_BREAK → exit 0 with a complete CSV; `--terminate_existing_session`
   against a running capture; real row latency; whether a Performance Log Users member gets
   `Microsoft-Windows-Kernel-Process` events; `HybridPresent` and `MsFlipDelay` values on this RTX 3070 laptop
   [unverified].
3. How DLSS-FG / FSR3 frame generation shows up on a real title (doubled presents? `MsFlipDelay`?) [unverified].
4. Whether EAC/BattlEye/Vanguard strip PresentMon's `PROCESS_QUERY_LIMITED_INFORMATION` handle (→ `<unknown>`
   names, no `--terminate_on_proc_exit`; PID targeting still works) [unverified].

---

## Evidence index

Scratch folder (temporary): `%TEMP%\claude\…\scratchpad\research-presentmon`
(below: `<S>`). Python commands were run as
`cd <repo>; uv run --no-sync --frozen python -I <script> <args>` (`<repo>` = the lag-doctor folder).
Gold captures: `<S>\src-v2.6.0\Tests\Gold\test_case_N.etl`.

| Id | What | Command (script in `<S>\scripts\`) | Output |
|---|---|---|---|
| E1 | v2.6.0 source, blobless sparse clone at `e13fce6…` (no executables checked out) | `git clone --filter=blob:none --no-checkout --depth 1 --branch v2.6.0 https://github.com/GameTechDev/PresentMon.git src-v2.6.0`, then `git sparse-checkout set --no-cone` for `/*.md /PresentMon/ /PresentData/ /Tests/ /Tools/generate/ /Provider/ /IntelPresentMon/CommonUtilities/ /IntelPresentMon/metrics.csv /IntelPresentMon/docs/ /IntelPresentMon/PresentMonAPI2/ /Version.props` | `<S>\src-v2.6.0` |
| E2 | token + ETW query probe (read-only) | `priv_probe.py` | pasted in 6.3 |
| E3 | `--help` bytes by handle type | `help_encoding.py` | pasted in 4 |
| E4 | header for every flag set; gold comparison; NA counts | `etl_matrix.py <S>\src-v2.6.0\Tests\Gold` | `<S>\out\etl_matrix.log`, `<S>\out\etl_*.csv` |
| E5 | value survey of all six captures, ordering | `etl_survey.py <Gold>` | `<S>\out\etl_survey.log` |
| E6 | pipe / disk-file / `--output_file` bytes, multi_csv names, errors | `output_modes.py <Gold>\test_case_1.etl` | `<S>\out\output_modes.log` |
| E7 | negative live test + `--terminate_existing_session` probe | `negative_live.py $PID` (PowerShell `$PID` = 32412) | `<S>\out\negative_live.log` |
| E8 | non-ASCII name through pipe vs files | `nonascii_name.py <Gold>\test_case_2.etl` | `<S>\out\nonascii.log` |
| E9 | target filters | `target_filters.py <Gold>\test_case_0.etl`; plus PowerShell `& $exe --etl_file … --output_stdout --process_id 3976 --process_name Presenter.exe` and `--process_id abc/0/3976x` | `<S>\out\target_filters.log` |
| E10 | CTRL_BREAK/CTRL_C delivery to a stand-in in a hidden console | `ctrl_outer.py` (runs `ctrl_controller.py`, `ctrl_worker.py`) | `<S>\out\ctrl_results.txt` |
| E11 | DXGI manifest fields on this Windows build | `dxgi_manifest.py` (`TdhEnumerateManifestProviderEvents`, `TdhGetManifestEventInformation`) | pasted in 9 |
| E12 | no leftover sessions; QPC frequency | `final_check.py` → 4201 for `lagdoctor_probe`, `lagdoctor_probe_never_started`, `PresentMon`; `QueryPerformanceFrequency = 10000000` | — |
| E13 | reference reader on ETL replays + synthetic self-test | `pm_reader_demo.py --selftest`; `pm_reader_demo.py <Gold>\test_case_5.etl [flags]`; `pm_reader_demo.py <Gold>\test_case_0.etl --no_track_display --no_track_gpu --track_pc_latency` | pasted in 10 |
| E14 | header/row mismatch | PowerShell: `& $exe --etl_file <Gold>\test_case_0.etl --output_stdout --no_track_display --no_track_gpu --track_pc_latency` → `header fields=17`, `row1 fields=18` | — |
| E15 | v2.6.0 → v2.6.1 diff | `git fetch --filter=blob:none --depth 1 origin tag v2.6.1; git diff --no-renames --name-only v2.6.0 v2.6.1` → nothing under PresentMon/, PresentData/, CommonUtilities/mc/ | — |
| E16 | HAGS registry value | `Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers` → `HwSchMode : 2`; `EtwMaxLoggers` not set | — |

---

## Verification log

Adversarial fact-check of this document, 2026-11-04, unelevated, read-only on the system. Each concrete claim
(names, values, cited lines, URLs, numbers) was re-checked against the source at tag `v2.6.0` in a **fresh,
independent** checkout (not the researcher's), against the fetched primary docs, or by re-running the local test
with my own scripts; `[inferred]` statements were checked for consistency with that evidence only. Scratch folder (below `<V>`):
`%TEMP%\claude\…\scratchpad\research-presentmon-verify`.
Python ran as `cd <repo>; uv run --no-sync python -I <V>\scripts\<script> <args>`;
`<exe>` = the PresentMon 2.6.0 path above; `<Gold>` = `<V>\src\Tests\Gold`.

| Id | What | Command | Output |
|---|---|---|---|
| V1 | provenance | `git ls-remote --tags` and `'refs/heads/release/2.6*'`; `git clone --filter=blob:none --no-checkout --depth 1 --branch v2.6.0 … <V>\src` + sparse checkout of `/*.md /PresentMon/ /PresentData/ /Tests/ /IntelPresentMon/CommonUtilities/ /IntelPresentMon/metrics.csv /IntelPresentMon/docs/ /Version.props /Provider/` (no file in it was executed); `git log -1 --format='%H %cI %aI'`; `git cat-file -t refs/tags/v2.6.0`; `git fetch … tag v2.6.1`; `git diff --no-renames --name-only v2.6.0 v2.6.1`; `curl https://api.github.com/repos/GameTechDev/PresentMon/releases/tags/v2.6.0`; `Get-FileHash`, `Get-AuthenticodeSignature` | `<V>\out\release_v2.6.0.json` |
| V2 | 30 flag sets replayed on test_case_0 (headers, row counts, malformed rows, stderr); gold comparison | `replay_matrix.py <exe> <Gold> <V>\out\matrix`; `gold_cmp.py <V>\out\matrix\gold_flags.csv <Gold>\test_case_0.csv`; bash loop for `--track_etw_status` | `<V>\out\matrix.log`, `<V>\out\matrix\*.csv` |
| V3 | value/NA/ordering survey of all six ETLs; v2 identities on every row | `survey.py <exe> <Gold>`; `v2_identity.py <V>\out\matrix\default.csv <V>\out\matrix\v2.csv` | `<V>\out\survey.log` |
| V4 | `DXGI_PRESENT_*` values | `Select-String 'DXGI_PRESENT_'` on `C:\Program Files (x86)\Windows Kits\10\Include\10.0.19041.0\shared\dxgi.h`; fetched the learn.microsoft.com DXGI_PRESENT page | — |
| V5 | output bytes by handle type (pipe, empty file, non-empty file, `--output_file`, both, `--help`, bad option, missing ETL) | `encoding_check.py <exe> <Gold>\test_case_1.etl <V>\out\enc` | `<V>\out\enc.log` |
| V6 | negative live test, token, by-name QUERY, `QueryAllTracesW`, `--terminate_existing_session` on absent names | `neg_live.py <exe> $PID` (PowerShell `$PID` = 16920); `terminate_probe.py <exe>`; `queryall.py` | `<V>\out\neg_live.log`, `<V>\out\terminate_probe.log` |
| V7 | target filters; per-chain tallies of test_case_5 | `filters_check.py <exe> <Gold>`; `chain_stats.py <exe> <Gold>\test_case_5.etl [flags]` | `<V>\out\filters.log` |
| V8 | ctrl-signal delivery stand-in (hidden console; then parent after `FreeConsole()`) | `ctrl_outer.py <V>\scripts <V>\out\ctrl`, `ctrl_outer2.py …` (run `ctrl_controller.py`, `ctrl_worker.py`) | `<V>\out\ctrl.log`, `<V>\out\ctrl2.log` |
| V9 | non-ASCII through PresentMon's own CRT; PE import table | `nonascii_probe.py <exe> <V>\out\nonascii`; `pe_imports.py <exe>` | `<V>\out\nonascii.log` |
| V10 | per-adapter HAGS state and hybrid role; `HwSchMode` | `hags_caps.py`; `Get-ItemProperty …\GraphicsDrivers` | `<V>\out\hags_caps.log` |
| V11 | DXGI manifest property names of events 10, 42, 45 on this build | `dxgi_fields.py` (`TdhEnumerateManifestProviderEvents`, `TdhGetManifestEventInformation`) | `<V>\out\dxgi_fields.log` |

### Corrected (old → new)
| # | Claim | Correction | Evidence |
|---|---|---|---|
| C1 | `PresentFlags`: 0x10 = `RESTRICT_TO_OUTPUT`, 0x20 = `STEREO_PREFER_RIGHT`, 0x40 = `STEREO_TEMPORARY_MONO`; "PresentMon's enum mislabels them, use Microsoft's values" | backwards: SDK `dxgi.h` has 0x10 `STEREO_PREFER_RIGHT`, 0x20 `STEREO_TEMPORARY_MONO`, 0x40 `RESTRICT_TO_OUTPUT`, as PresentMon does; the learn.microsoft.com table is the wrong one | V4 |
| C2 | v2.6.0 "committed 2026-08-27" | authored 2026-08-27 16:50 −07:00, committed 2026-08-28 11:10 −07:00 | V1 |
| C3 | v2.6.1 "changes installer, service, GUI and a log-timezone fix only; everything holds for 2.6.1 [verified]" | the no-change list is verified, but `CommonUtilities/log/TextFormatter.cpp` and `Version.props` also changed and the 2.6.1 binary was never run → "behaves the same" retagged [inferred] | V1 |
| C4 | "`dwm.exe` alone had two swap chains (one per display) in test_case_5" | four keys: two DXGI chains (174 rows each) + `0x0` and `0x100000000` (runtime `Other`, 5 rows each); PresentBench also had a 7-row `0x0` key | V7 |
| C5 | "PresentMonTraceSession.cpp enables CAPTURE_STATE only for Kernel-Process and DxgKrnl" | Kernel-Process rundown only if `mTrackProcessState`, which the console never sets → in practice DxgKrnl only (conclusion about DXGI unchanged) | source, 9 |
| C6 | E8 `Pr\uffe9senter.exe` presented as what a non-ASCII name looks like | it is an ETL-replay artefact (ANSI `NT_Process` name widened with sign extension, from a patched ETL); live names are real UTF-16 | source #L2893-L2904, researcher's script |
| C7 | `can_trace()` = `TokenElevation ≠ 0` or PLU | exact rule = `CheckTokenMembership(544)` or `CheckTokenMembership(559)` (enabled-SID semantics); `TokenElevation` is a proxy [inferred] | CheckTokenMembership docs, V6 |
| C8 | stop code called `proc.send_signal(CTRL_BREAK_EVENT)` unguarded | raises `OSError` WinError 6 when the parent has no console → wrapped; helper call gets `CREATE_NO_WINDOW`; PID validated (`-1` matches nothing, `0`/`abc` everything) | V8, V7 |
| C9 | `--output_file` "never flushes until the file is closed" | no `fflush`; CRT block buffering writes as the buffer fills, tail at `fclose` (buffer size not measured) | source (no `fflush` for `File`); buffering behaviour [inferred] |
| C10 | Rows arrive "0.1–1+ s late" (only three causes listed) | plus a stuck present holding back all later rows for up to 2 s (`mDeferralTimeLimit`), and dropped frames held behind a waiting displayed frame | source, 2.4 |
| C11 | Pipe-mode Latin-1 pass-through "[unverified]" (open question) | verified: U+00E9/U+00FF → bytes E9/FF; U+20AC, U+0100, U+4E2D → `?` (so C-locale Latin-1, not cp1252) | V9 |
| C12 | HAGS state only from `HwSchMode` = 2 "(meaning unverified)" | measured per adapter via `D3DKMTQueryAdapterInfo(KMTQAITYPE_WDDM_2_7_CAPS)`: RTX 3070 `HwSchEnabled=1`, Intel UHD 0; registry meaning still undocumented | V10 |
| C13 | 6.4 heading pointed to non-existent "open questions" | added "Still open" at the end of 10 | — |

### Confirmed
| Claim (section) | Evidence |
|---|---|
| Binary SHA-256, size, Authenticode `Valid` Intel; equals the release asset digest (header) | V1 |
| Tag `v2.6.0` = `e13fce6…`, lightweight; head of `release/2.6.0`; `v2.6.1` = `95d0312…` 2026-11-03 | V1 |
| Gold flags reproduce `test_case_0.csv` except 4 values in the 15th digit (intro) | V2 (357 rows, 4 fields, ≤ 5.7e-14) |
| Default 28-column header verbatim (1.1) | V2 |
| Every row of the 1.2 flag table: `--qpc_time`, `--qpc_time_ms`, `--date_time` (format), `--track_frame_type`, `--track_hybrid_present`, `--track_app_timing`, `--track_pc_latency`, `--write_display_metadata` (31), `--track_gpu_video`, `--no_track_gpu` (24), `--no_track_input` (26), `--no_track_display` alone (ignored, exact warning), with `--no_track_gpu` (17 cols, 324 rows), `--exclude_dropped` (349 rows), hidden `--write_display_time --write_frame_id` (30) | V2 |
| 36-column all-flags header verbatim (1.2) | V2 |
| `--v2_metrics` 23 columns, time-column names, `VideoBusy`, `InstrumentedLatency` (1.3) | V2 |
| `--v1_metrics` 19 columns, 21 with `--qpc_time --track_gpu_video`, 368 rows, `Dropped`, 14 dp, zero-padded address, frame-type/metadata ignored with the two warnings; v1+v2 → v2 with warning (1.3) | V2 |
| Quirks: `--qpc_time_ms` writes ms under `TimeInSeconds` (`349.0030`); 17-column header with 18-field rows (324/324); README table uses v2 names; `--hotkey` re-emits the header (source only); spurious `--terminate_existing_session` warning (1.4) | V2, V6, source |
| `Application`, `SwapChainAddress`, `PresentRuntime`, `SyncInterval`, `AllowsTearing`, `PresentMode`, `FrameType` (no NVIDIA value), `HybridPresent` value sets and sources (2.1) | source at the cited lines; V3 value counts |
| `DXGI_PRESENT_TEST` presents dropped; D3D9 flag translation (2.1) | source #L320-L341, #L2676-L2683 |
| `cpuStart` definition; `DeltaUnsignedMilliSeconds` semantics; every formula in the 2.2 table | source: MetricsCalculator*.cpp, Qpc.cpp |
| Precision 14 dp vs 4 dp; `.` decimal point (2.2) | source; V2 rows |
| Display latency = `MsCPUBusy + MsUntilDisplayed`; displayed time = next displayed `MsBetweenDisplayChange` (2.2) | V3: 349/349 and 339/339 rows |
| Runtime `Other` rows: `0x0`, `MsInPresentAPI` = `MsCPUWait` = 0, `SyncInterval` −1 or kernel-overwritten (2.2, 8.2) | V2: 52 `Other` rows in test_case_0 |
| `NA` vs `0.0000` rules, `WriteMetricOrZero` (2.3) | source #L10-L18, #L1255-L1257 |
| Dropped frame ⇔ `MsUntilDisplayed` = `NA`; 8 NA rows = 8 rows removed by `--exclude_dropped` (2.3) | V2 |
| NA frequencies over 1,448 rows (2.3) | V3 |
| First present per chain seeds only; displayed frame held for the next; 368 vs 357; several rows per present with FG; 4 s pruning (live only); lost/failed skipped (2.4) | source; V2 |
| Not globally time-ordered (54 / 255), ordered within a chain (2.4) | V3 |
| Intel PresentMon page and 2023-08-18 launch article: GPU Busy qualitative, no threshold (3.1) | fetched both URLs |
| UE guide: GPU-bound at ≥ 95 % GPU utilisation; GPA: GPU Busy 100 % when GPU-bound (3.1) | fetched both URLs |
| club386 (2023-08-18) secondhand reading and DLSS 3/Reflex caveat; issue #362 point made by a non-Intel commenter (3.1, 3.2) | fetched both URLs |
| README HAGS caveat ~0.5 ms; `HwSchMode` = 2 on this machine (3.2) | README L75-L84; V10 |
| `InitConsole` handle rules; no `setlocale`/other `_setmode` anywhere (4) | source; grep |
| Pipe 12,296 B no BOM CRLF; empty file 24,594 B UTF-16LE BOM (CRLF); non-empty file no BOM; `--output_file` 12,299 B UTF-8 BOM; `--help` 5,435 / 10,872 B; repo help.txt 5,349 B ASCII LF (4) | V5 |
| `fflush` after header and every row only for stdout, at the cited lines; `_wfopen_s(…, "w,ccs=UTF-8")`; BOM written automatically (4) | source; fopen docs |
| 100 ms output-thread sleep; `FlushTimer` 0 = 1 s default for real-time sessions (4) | source; EVENT_TRACE_PROPERTIES docs |
| Option prefixes `--`/`-`/`/`, case-insensitive; `wcstoul` without validation (5) | source; V7 |
| `--process_name` canonicalisation and union; `--exclude` wins; `--process_id` overwrite, `abc`/`0` = all, `3976x` = 3976; with a PID only DWM can be added; no filter = every process incl. DWM (5) | V7 |
| `--output_stdout` disables console output and `--multi_csv`; `--output_file` wins over it; `Started recording.` on stdout plus the `--no_console_stats added` warning (5) | V5; source |
| `--no_console_stats`, `--exclude_dropped`, `--delay`, `--timed`/`--terminate_after_timed`, `--terminate_on_proc_exit`, `--session_name` default, `--stop_existing_session`, `--restart_as_admin` (ShellExecuteEx `runas`, exit 2), `--scroll_indicator` (`SendInput`), circular buffer 2048 / power of 2 (5) | source at the cited lines |
| Missing ETL → exit 6 `file not found.`; unknown option → exit 1; exit-code table (5) | V5; source |
| StartTraceW access rule, 64-session default, fixed-name guidance, session survives its creator (6.1, 7.2) | fetched StartTraceW docs |
| Performance Log Users SID S-1-5-32-559 and the Windows Kernel Trace limitation (6.1) | fetched security-groups doc |
| Negative test: exit 6, 0.05 s, 0 B stdout, 527 B ASCII stderr, exact text, nothing left behind (6.2) | V6 (PID 16920) |
| Token probe values; QUERY 4201 vs 5; `QueryAllTracesW` returns 1 session (6.3) | V6 |
| Ctrl handler: any type → stop recording, `WM_QUIT`, `Sleep(INFINITE)`; shutdown order (7.1) | source |
| HandlerRoutine timeouts (CLOSE 5 s, CTRL_C/BREAK none; logoff/shutdown only for services); `CREATE_NEW_PROCESS_GROUP` 0x200 disables CTRL+C and is ignored with `CREATE_NEW_CONSOLE`; CTRL_C cannot target a group; `CREATE_NO_WINDOW` 0x08000000; Python `terminate`/`kill` = `TerminateProcess` (7.1) | fetched the four docs |
| External session stop → `ProcessTrace` returns → `ExitMainThread()` (7.1) | ConsumerThread.cpp#L30-L38 |
| Real-time session without consumer buffers events and wastes resources; 64 KB × 256–1024 buffers (7.2) | EVENT_TRACE_PROPERTIES docs; source #L498-L500 |
| Stand-in: CTRL_BREAK delivered in 20–30 ms, CTRL_C not, `CREATE_NO_WINDOW` not while `os.kill` succeeds (7.3) | V8 |
| Only `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` toward other processes; `SendInput`/`RegisterHotKey` only for their options; no anti-cheat statement in any `.md` (8.1) | grep over PresentMon/, PresentData/; grep `*.md` |
| FG labelling only Intel XeSS-FG / AMD AFMF via Intel-PresentMon provider; NVIDIA `FlipRequest` → `MsFlipDelay` (8.4) | source |
| `ALLOW_TEARING` is a VRR requirement and windowed-only; independent flip = fullscreen-exclusive efficiency; waitable object latency; hybrid blt cost (8.5, 8.6) | fetched DXGI_PRESENT and flip-model docs |
| CASO two-copy vs one-copy, copies limit low-latency apps, Windows 11 / WDDM 2.9 (9) | fetched supporting-caso doc |
| `HybridPresent` position and `%d`; events 10/45 added by the flag; values 0/1/2; `pIDXGISwapchain` spelling on event 45 here vs case-sensitive `wcscmp` (9) | source; V2; V11 |
| Recommended header: 30 columns since 2026-11-09, without `--track_pc_latency`; 31 with it, as lag-doctor passed until then (V2); QPC 10,000,000/s; session `ClientContext` = QPC (10) | 2026-11-09 replays of test_case_0 and test_case_5 by `tests/fixtures/presentmon/regenerate.py` (30 columns); V2 (31); V6; PresentMonTraceSession.hpp#L39 |
| Reference-reader numbers for test_case_5 (258 rows, 84 dropped, medians 11.051 / 0.936; dwm 174 + 174; steamwebhelper 24) and `--exclude dwm.exe` result (10) | V7 (the reader's printout simply omitted the small `Other` keys, C4) |
| `docs/learnings.md` line 28 says UTF-16LE with BOM; DESIGN.md has a "no handles to the game" rule (lines 10–11, 94–97) | read both files |

### Added (not in the original)
`--track_etw_status` hazards; README's wrong `--no_track_display` requirement; time-format flags mutually exclusive
(exit 1); `--terminate_existing_session` second warning; `-1` PID matches nothing; `AnimationTime` prints `0.0000`
for some missing values; `MsBetweenSimulationStart` needs `--track_pc_latency`/`--track_app_timing`; GPU columns
never `NA`; blocked dropped frames and lost trailing frames; 2 s deferral; 4 s-pruning consequence for long stalls
[inferred]; consumer thread `THREAD_PRIORITY_TIME_CRITICAL`; query handles opened to every presenting process seen;
UE guide's 95 % / 90 % Draw-time rule; D3DKMT HAGS and hybrid-role query with tested ctypes and output;
`GetConsoleCP()` console test; no-console `OSError`; venv launcher caveat for creation-flag tests; release-asset
digest match. Measured on GitHub's runner (2026-11-08, PR #23, `tests/test_relay_live.py`): live on a process that
never presents and stopped by `--terminate_existing_session` with no console, PresentMon 2.6.0 wrote no CSV at all,
not even the header: the header waits for something to report.

### Unverifiable here (left tagged [unverified] or [inferred])
Live CTRL_BREAK → exit 0 with a complete CSV; `--terminate_existing_session` against a running capture; real row
latency; Kernel-Process events for a Performance Log Users member; `HybridPresent`/`MsFlipDelay` on this laptop;
DLSS-FG/FSR3 representation; anti-cheat handle stripping; `--restart_as_admin` not passing pipes; whether an
unelevated QUERY of an elevated-created session returns 5; the meaning of `HwSchMode` = 2 (no Microsoft doc); the
2.6.1 binary's behaviour; the buffer sizes ETW actually grants; the CRT buffer size for `--output_file`.
