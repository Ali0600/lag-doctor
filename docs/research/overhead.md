# What lag-doctor costs a game while it records

Research date: 2026-11-08. Question: what does lag-doctor take from a game while it is attached, that is, while
`lagdoctor watch` records it?

Method: five code readers (one per collector, plus the session), two outside-research agents, two analysts of the
owner's two real recordings, an attach/detach benchmark on LagLab, offline timings of the frame path, and an
adversarial pass in which two independent skeptics re-read every outside source (verification log at the end).
Measured on this laptop: Windows 11 build 26200, 16 logical CPUs, RTX 3070 Laptop plus Intel UHD, NVIDIA driver
591.86, Python 3.13.3, main at 1e0ff72. `<scratch>` in a source tag is this research's working folder under %TEMP%;
its scripts and outputs are not in the repo.

**Tags.**
- `[measured: …]`: run or counted in this research; the source says where.
- `[documented: …]`: a document or the code says so.
- `[research: …]`: found at that source by a research agent and re-read by two skeptics (verification log).
- `[inferred]`: reasoned, not observed.
- `[unverified]`: could not be checked here.

## 1. The answer

While it records, lag-doctor's own process uses about **5-10 % of one logical CPU** (perhaps 15 % more by cycle
count, section 3.1), which is 0.3-0.6 % of this 16-thread laptop and about 2-3 % of what the game itself used in the
real recordings (up to 6 % with PresentMon at its upper bound). PresentMon adds an estimated 0.2-0.6 % of one CPU by
the replays, and used at least 0.4 % in the real recording. Neither real recording ran the game-server tracker: in an
elevated recording with network on it adds about 1-2 % of one CPU, and an estimated 10-40 % while something else on
the PC downloads fast (section 3.5). Memory grows with the frame rate: about **0.2 GB per hour at 60 FPS** and
0.4-0.45 GB per hour at 144 FPS.

On LagLab, attaching the recorder without admin rights, so without PresentMon or the tracker, left the mean frame time
within the noise between two detached windows: −0.009 % in a CPU-bound workload at 159 FPS (95 %: −0.10 to +0.09 %),
+1.0 % uncapped at about 1,580 FPS (−1.3 to +3.0 %) and +0.002 % at a 144 FPS cap on the NVIDIA GPU, where the cap
absorbs small per-frame costs. Pacing showed one hint: in the CPU-bound workload 5 frames ran 10-20 ms attached and
none detached (section 4.1). In the owner's real recording, no link between lag-doctor's sampling moments and longer
frames or stutters reached significance: the 1 Hz NVIDIA reads came closest (1.5 times the stutters, about one extra a
minute if real), the test could not have seen up to 2-3 extra stutters a minute at the 4 Hz sampler, and it cannot see
a cost that is not locked to those moments (section 4.2).

The cost that may have mattered most was not measured, and it was not lag-doctor's CPU at all. Until 2026-11-09
lag-doctor started PresentMon with `--track_pc_latency`, which switches on the latency-ping code inside games built
with NVIDIA's PC Latency (Reflex) markers, and one report, by a CapFrameX developer, ties stutter in FINAL FANTASY VII
REBIRTH to that switch; he blamed the game's own latency code, and no maintainer confirmed it (section 5.1). The flag
was dropped on 2026-11-09 (DECISIONS).

| Cost | Who pays | How much | Evidence |
|---|---|---|---|
| lag-doctor's recording process | CPU, a normal-priority process | 6.2-7.7 % of one CPU in the real recording without frame timing; 5.3-9.9 % with it; 4.5-5.5 % on LagLab on a quiet PC, 7.2 % with NVML on the NVIDIA GPU (3-6 CPUs busy with other work in 9 of 10 windows), 10.1 % with 13-15 CPUs busy in 8 of 10 windows | measured (section 3) |
| PresentMon | CPU | about 0.2-0.6 % of one CPU by the replays, but at least 0.4 % in the real recording; its consumer thread runs at priority 15 | inferred from replays; real-recording bounds 0.4-7.2 % (section 3.4) |
| The game-server tracker (elevated, network on) | CPU | 8.2 ms/s at rest plus 2-6 ms/s for the game's packets (1-2 % of one CPU); an estimated 0.1-0.4 s/s during a big download | measured on a private session; the download's event rate inferred (section 3.5) |
| ETW events written on the game's own threads | the game | about 9-23 events per frame at 0.15-0.5 µs each: a few µs per frame, under 0.1 % of a 60 FPS frame | inferred from measured parts (section 3.4) |
| Kernel events for every process's GPU work | kernel | about 1,600-4,700 events/s, about 0.2-1.4 ms of CPU per second | inferred (section 3.4) |
| lag-doctor's memory | RAM | 30 MB at start (44 MB with NVML), then about 0.2 GB per hour at 60 FPS and 0.42-0.45 GB per hour at 144 FPS | measured (sections 3.2 and 3.6) |
| PresentMon's trace buffers | nonpaged kernel RAM | 16-64 MiB requested | documented, not measured |
| Network probes | network | 4-6 ICMP echoes a second, about 2-3 kbit/s each way | inferred from the code |
| Frame rate and pacing | the game | no measurable change in 3 LagLab workloads with the unelevated recorder (no PresentMon, no tracker); a hint of a rare 4-13 ms hitch in the CPU-bound one; frame timing's own effect not measured | measured (section 4) |

## 2. What runs while a recording is on

| Thread or process | Rate | Work each time |
|---|---|---|
| `lagdoctor-system` (system sampler) | 4 Hz | one PDH collect of about 750 counter instances (about 500 of them GPU Engine), one whole-system process snapshot (NtQuerySystemInformation with the game's threads), the S row and the top-8 `others` row |
| `MainThread` (session) | 1 Hz | the liveness check (a whole-system snapshot), the live status line |
| `lagdoctor-frames` (PresentMon reader) | once per frame | parse one CSV row (30 columns; 31 until 2026-11-09, when `--track_pc_latency` was dropped) and store 19 values |
| `lagdoctor-frames-watch` | 1 Hz | another whole-system snapshot, to see the game exit |
| PresentMon-2.6.0-x64.exe | per ETW buffer, and 10 Hz output | consumes the trace session `lagdoctor`, writes one CSV row per frame |
| `lagdoctor-gpu` (NVML) | 1 row a second, polling 2-49 times a second before each row (about 25 on average, depending on where NVML's refresh falls; 34.6 in the one measured run) | waits for NVML's ~500 ms refresh, then makes 9 NVML calls (12 values); only when the game renders on the NVIDIA GPU or its GPU is unknown |
| `lagdoctor-network` and `lagdoctor-ping-*` | 2 Hz | one ICMP echo per target (the router, 1.1.1.1, the game server once known), Wi-Fi quality and retries, NIC counters, the Steam download log |
| `lagdoctor-net_flows` and `etw-lag-doctor-net` | 4 Hz, and per ETW buffer | the game-server tracker: every TCP and UDP send and receive on the PC reaches Python, filtered there by process; a third whole-system snapshot a second |
| `lagdoctor-hotkey` | only on a press | Ctrl+Alt+L, registered for the whole recording |

PresentMon needs ETW rights (an elevated token or membership of Performance Log Users) and the tracker an elevated
token. So an unelevated `watch --no-elevate` on an account outside Performance Log Users runs only the sampler, the
session, NVML and the network probes (the tracker starts, reports itself unavailable and stops); a member of
Performance Log Users also records frame timing unelevated. [documented: docs/DESIGN.md, "Privileges and elevation";
`cli.cmd_watch`]

Since M3, merged while this research ran, an elevated recording is no longer one elevated process. `watch` records in
the player's own window, and when frame timing or the tracker needs rights it starts the relay through the UAC
prompt: a second process (`pythonw -m lagdoctor.relay`, no window) that runs PresentMon with its reader and watcher,
and the tracker's ETW session with a snapshot of the game's processes once a second. The relay sends the game's frame
rows and network events to the recorder in 0.1 s batches, and the recorder stores them. [documented: docs/DESIGN.md,
"The relay"] The measurements here were made on main at 1e0ff72, before M3, and the two real recordings earlier
still; the relay's own cost was not measured.

## 3. lag-doctor's own CPU and memory

### 3.1 In the owner's real recordings

The `others` rows of every capture list lag-doctor's own processes (target `tool_pids`) when they are among the 8
busiest that tick. A process missing from a tick's top 8 used at most what the 8th used, so whole-run figures are
ranges, and each tick moves in 6.25 % steps (one 15.625 ms clock tick per 0.25 s). [measured: the owner's two game
captures, `others` against `tool_pids`; the first is reviewed in docs/FIRST-CAPTURE-REVIEW.md] The
ranges below count the processes that were ever in a top 8 (the recording process, and PresentMon in the second).
`tool_pids` also lists processes that only wait and never were: the venv launcher in the first; that launcher, the
elevated recording's waiting window and its launcher in the second. Each used no CPU in any of the 1,702 and 904
ticks (first and second) whose top 8 had an empty or zero slot, so they are left out [inferred]; counted at the 8th's
bound they would raise the upper ends to 11.5 % and 38.6 % of one CPU. Both recordings predate M3 (section 2), when
an elevated recording ran everything in one elevated process.

| Recording | What ran | lag-doctor | Share of the machine | Share of the game's CPU |
|---|---|---|---|---|
| 14 min, no frame timing, no network | sampler, NVML, session | 6.2-7.7 % of one CPU | 0.39-0.48 % | 2.1-2.6 % |
| 13 min, elevated, frame timing, no network | the above plus PresentMon and its reader | recording process 5.3-9.9 %, PresentMon 0.4-7.2 % | 0.36-1.07 % | 1.9-5.7 % |

The game used about 3 logical CPUs on average in both (game CPU mean 295 % and 298 %), and the second ran at 56.3 FPS
on average. The sampler's own sentence in each capture reads 5.7 % and 5.3 % of one CPU (median tick 15.6 ms and
13.6 ms). That figure comes from `time.thread_time`, which counts whole 15.6 ms clock ticks. In one 60 s run on this
PC with no game running, counting CPU cycles gave 4.1-4.2 % where `thread_time` gave 3.5 %, about 15 % less
[measured: `QueryThreadCycleTime` against `time.thread_time`, 59.7 s, one run]. The in-game sentences probably read
low by a similar fraction, and so may the process figures above, which come from the same clock-tick accounting
[inferred: not checked on a game run].

### 3.2 On LagLab, thread by thread

The benchmark (section 4) read each recorder thread's CPU from handle-free snapshots of its own process over each
10 s window, median over the windows, in % of one logical CPU. Thread times count whole 15.6 ms clock ticks, so each
cell moves in steps of 0.16 % (the network, ping and NVML cells are 0-2 ticks), and like section 3.1's figure they
may read about 15 % low:

| Workload | system sampler | session | NVML | network | pings | total |
|---|---|---|---|---|---|---|
| NVIDIA GPU, capped at 144 FPS | 5.70 | 0.78 | 0.31 | 0.16 | 0.08 | 7.19 |
| Intel GPU, CPU-bound, 159 FPS | 3.75 | 0.63 | off | 0.16 | 0.16 | 4.53 |
| Intel GPU, uncapped, about 1,580 FPS | 4.22 | 0.63 | off | 0.16 | 0.00 | 5.47 |

NVML runs only in the NVIDIA rows: it stays off for a game on the Intel GPU. The Intel rows come from a quiet PC
(1.2-3.3 CPUs busy); the NVIDIA row from a first pass with 3-6 CPUs busy with other work in 9 of its 10 windows. In
that first pass the CPU-bound workload, with 13-15 CPUs busy with other work in 8 of its 10 attached windows, read
10.1 % (the sampler 8.5 %). Private memory at the end of a window: 30 MB without NVML, 44 MB with it (about 14 MB
more with NVML loaded [inferred: the NVIDIA run's 44.1 MB against 29.9-30.4 MB on the Intel runs]). [measured:
`<scratch>\bench\bench.py`, `recorder.py`]

### 3.3 Where the sampler's time goes

With no game running (other research scripts were running alongside; the load was not logged) a tick took 11.0 ms
median (wall), 10.1-10.3 ms of it on the CPU: [measured: the shipped sampler with timing wrappers passed in through its
constructor hooks, `<scratch>\sysmon-cost\measure_sampler.py 60 103`, 238 ticks; the process and thread counts below
are from a snapshot taken six minutes earlier]

| Part | Median | Share |
|---|---|---|
| NtQuerySystemInformation (393 processes, 6,730 threads, 795 KB) | 5.42 ms | 49 % |
| PDH collect of the 25 paths | 1.46 ms | 13 % |
| PDH formatting and parsing (GPU Engine alone 0.95 ms for 503 items) | 1.49 ms | 14 % |
| parsing the snapshot | 0.82 ms | 7 % |
| the rest in Python (GPU sample, process rates, top 8, the row) | 1.67 ms | 15 % |

During the real game ticks took 13.6-15.6 ms median, with a maximum of 184-284 ms.

### 3.4 The frame path: lag-doctor's reader and PresentMon

- **lag-doctor's reader**: 33 µs of CPU per frame on a quiet PC (one run) and 62-66 µs with 11 of the 16 logical
  CPUs busy (three runs), through the real parser and `_add` (per-operation CPU time roughly doubles under that load:
  shared cores and lower clocks); through a real pipe paced like PresentMon's it took 78-100 µs per row, load not
  logged. That is 0.2-0.6 % of one CPU at 60 FPS and 0.5-1.4 % at 144 FPS. [measured:
  `<scratch>\bench\pm_parse_bench.py` on rows PresentMon wrote with lag-doctor's flags from its gold capture
  test_case_5; `<scratch>\presentmon-cost\ld_parse_cost.py` for the pipe] Since M3 this parsing runs in the relay,
  whose rows then cross a pipe to the recorder in 0.1 s batches; that leg was not measured.
- **The watcher**: a whole-system snapshot every second, 7.4 ms each, about 0.7 % of one CPU. [measured]
- **PresentMon's process**: replaying its six gold captures cost about 1.4 µs of CPU per delivered event (a fit over
  the replays), and those captures hold 1,555-4,675 enabled events per second system-wide, so live it would use about
  0.2-0.6 % of one CPU [inferred]. In the real recording PresentMon was in the top 8 in only 277 of 3,217 ticks, which
  bounds it at 0.4-7.2 % [measured]. Its ETW consumer thread runs at THREAD_PRIORITY_TIME_CRITICAL (base priority 15)
  [research: PresentMon v2.6.0 `ConsumerThread.cpp` line 11], so when a buffer arrives and no logical CPU is idle, it
  pre-empts whatever runs there, game threads at 8-10 included, for about 1 ms [inferred]; the game in the real
  recording used about 3 of the 16, which makes that rare there. It asks for 64 KB buffers, 256 to 1,024 of them (16-64 MiB of nonpaged memory),
  delivered at least once a second [research: `PresentMonTraceSession.cpp` lines 492-500].
- **Inside the game**: the DXGI present events and the kernel events logged while the game's threads are in the
  graphics kernel come to 8.7-22.9 enabled events per frame in the gold captures [measured: event counts]. One
  `EventWrite` costs 165 ns here while a private in-process session listens and 19 ns with no listener, so writing the
  event itself takes about 145 ns [measured: a C# P/Invoke loop, 7 rounds of 200,000 events]; PresentMon's real-time
  session also takes each call into the kernel, and one minimal system call costs 328 ns here, so about 0.5 µs an event
  [inferred; the real-time write needs elevation and was not measured]. Published figures are 1.6-4 µs per event on the older hardware
  they came from (2.5-4 µs on the 2005 post's 500 MHz Pentium III, 1.6 µs on the 2012 post's 2 GHz laptop), or about
  0.3-0.5 µs if the 2005 cycle count held at a modern 4 GHz [research: section 6; the conversion inferred]. At this
  laptop's 0.15-0.5 µs per event the game pays roughly 1-11 µs per frame, under 0.1 % of a 16.7 ms frame; even at
  1.6 µs it would be about 37 µs, 0.2 % [inferred]. The gold captures are PresentMon's own short test recordings
  (2-6.5 s, from other machines, settings unknown), and one of these events comes once per GPU submission, so a game
  that submits more work per frame pays proportionally more [inferred]. With no process filter, dwm.exe and every
  other program that presents pay the same per-event cost (236-835 events a second in DWM in the gold captures)
  [measured: event counts].
- **In the kernel, for everyone**: the DxgKrnl queue-packet and DMA-packet events cover every process's GPU work, and
  the VSync events every vblank; in the gold captures all enabled events come to 25-259 per present across all
  processes, 13-187 of them queue and DMA packets [measured], about 0.2-1.4 ms of CPU per second at 0.15-0.3 µs each
  [inferred]. PresentMon clears the Performance keyword bit from every DxgKrnl event it
  enables because its source warns that filtering DxgKrnl events by that keyword can have side effects that hurt
  performance on some versions of Windows [research: `PresentMonTraceSession.cpp` lines 52-82].

### 3.5 NVML and the network side

- **NVML**: 0.60 % of one CPU over a 60 s run, by cycle count. Before each 1 s row it polls a five-call fingerprint
  every 10 ms until NVML's ~500 ms refresh shows, 34.6 polls a second in that run, so about 180 NVML calls and 35
  power readings a second. The row reads its four throttle counters in one call that takes about 285 µs in the
  driver, nearly all of it for the two hardware counters (each about 280 µs when read alone). No rise in
  DPCs or interrupts could be detected even at 3,200 full samples a second. [measured: `<scratch>\nvidia-sampler`]
- **Probes**: 1.2-1.8 ms/s for the probe sampler pinging the router and 1.1.1.1, its Wi-Fi and adapter reads
  included (0.12-0.18 % of one CPU: 1.2 from 90 s of thread times charged in 15.6 ms ticks, 1.6-1.8 from each call's
  exact cycles), plus 0.6-1.8 ms/s in the WLAN service answering 6 queries a second. Nothing in lag-doctor starts a Wi-Fi scan; it only listens for the scans Windows makes.
  [measured; the scan check is a grep]
- **The tracker** (elevated recordings with network on): 8.2 ms/s at rest, nearly all of it the whole-system snapshot
  for the game's process tree, plus 7-11 µs of Python for every other process's packet event and 22-28 µs for each
  of the game's. A game alone at 30-128 packets a second each way (60-256 events a second) adds about 2-6 ms/s. During a big download (98-266 Mbit/s)
  the event rate may reach 8,400-46,000 a second and the tracker 0.1-0.4 s of CPU a second [inferred: per-event cost
  measured, the event rate per packet unverified]. If its consumer falls behind, ETW drops events and the tracker's
  data gets holes [research: Microsoft's Sessions page]; since the write fails at once instead of waiting, the game's
  networking should not be slowed [inferred]. Since M3 the tracker's ETW session runs in the relay, which decodes every
  event and passes on whole only the game's and the recorder's, so the per-event cost of other programs' traffic is
  paid there.

### 3.6 Memory

Every value is kept as a Python object for the whole recording. Private bytes per stored row: a frame about 0.7 KB
(688-729 B; tracemalloc counts 574 B of requested objects), an S row 1.4 KB, an `others` row 3.0 KB, an NVML row
0.4 KB, a network row 0.4 KB. A synthetic hour held **+215 MB at 60 FPS
and +420 MB at 144 FPS**; the real 13-minute recording held 45 MB once loaded, about 0.2 GB per hour at 56 FPS.
After the stop, `result()` copies every column (+79 MB for the frame columns of an hour at 144 FPS, about 85 MB with
the rest). Before M3 the waiting window of an
elevated recording then loaded the saved file again, peaking at +0.3-0.5 GB per recorded hour; since M3 the recorder
holds and saves the frames itself. [measured: synthetic captures and the real one] Compact frame storage is already
on the backlog (docs/DECISIONS.md).

## 4. Does attaching it change the game's frames?

### 4.1 LagLab, attached and detached in turns

LagLab, the repo's fake game, logs the start time of every frame itself, so its frame times do not depend on
lag-doctor. One LagLab process ran for 380 s. After a 60 s warm-up, windows of 10 s alternated: none attached (A),
then the recorder (B), launched 6.5 s before its window (8.5 s when it followed another attached window) and stopped
at its end, the order flipping every cycle. Each
window's frame times, LagLab's CPU, the recorder's CPU and the whole machine's busy time were read for exactly that
window. B is `watch --no-elevate` minus its console: the real target lookup, inventory, session and collectors
(sampler, network probes, NVML when the game is on the NVIDIA GPU, and the tracker, which reports itself unavailable
without rights), with no hotkey and no status line drawn. PresentMon could not run here (section 7).

| Workload | Pairs | Frame time, B against A | p99 frame time | Stutters | Recorder |
|---|---|---|---|---|---|
| NVIDIA GPU, capped at 144 FPS (light) | 10 | +0.002 % (95 %: −0.002 to +0.008) | +0.000 ms (−0.011 to +0.012) | none in either | 7.2 % |
| Intel GPU, CPU-bound: 6 ms of main-thread work a frame, 159 FPS | 11 | −0.009 % (−0.098 to +0.089) | −0.006 ms (−0.045 to +0.076) | 2 attached, 0 detached; frames over 10 ms: 5 attached, 0 detached | 4.5 % |
| Intel GPU, uncapped, about 1,580 FPS | 11 | +1.0 % (−1.3 to +3.0) | +0.013 ms (−0.018 to +0.034) | no frame over 10 ms in either | 5.5 % |

Medians of the per-cycle differences with bootstrap intervals; an attached window whose recording had begun less
than 1 s before it is dropped with its pair (one NVIDIA cycle, hence 10 pairs); a stutter is a frame over twice the
median of the 31 around it, which means nothing at the uncapped 0.6 ms frames. [measured: `<scratch>\bench\main`
(NVIDIA row) and `<scratch>\bench\rerun` (Intel rows), cycles.jsonl and cycles-summary.json]

None of the three moved beyond the noise between two consecutive detached windows (median absolute change 0.003 %
capped, 0.31 % CPU-bound, 3.1 % uncapped). The one hint is in the CPU-bound workload: 5 frames ran over 10 ms (10.3
to 19.5 ms against 6.3 ms) in the 110 s attached and none in the 110 s detached, and 2 of the 5 overlapped a sampler
tick's work. So a CPU-bound game may see a rare hitch of 4-13 ms while the recorder is attached: 5 long frames in 3-4
bursts over 110 s here, the two longest 0.1 s apart. That count was noticed after the fact, so it is a hint, not a
finding. The first pass of the same workload, run while 12-15 CPUs were busy with other work in 17 of its 21 windows,
leaned the same way (65 frames over 10 ms in 100 s attached, 17 in 110 s detached).

### 4.2 The real recording, tick by tick

Do frames that overlap a sampler tick or an NVML read run long? The test compares those frames against the same
statistic with all events moved together by one random offset (which keeps the game's own rhythm and the bias that a
long frame overlaps more moments). Its calibrated p ranks the real phase among all phases of the period; 100-400
decoy event trains at other periods measured its false-positive rate on this recording, and planted effects its
sensitivity.
[measured: `<scratch>\phaselock\phase_lock.py` on the 13-minute recording, 44,560 frames]

- **Sampler ticks (4 Hz)**: the frames overlapping a tick ran 0.31 ms longer than chance would give (calibrated
  p 0.02-0.06, above the bar of 0.017 for one of a source's 3 statistics, and of about 0.008 over all 6 tests), the
  same in both halves of the recording. Even if real, that is
  0.26 % of frame time, about 0.15 FPS. No significant excess of stutters (+0.7 a minute, calibrated p 0.33); up to
  2-3 extra a minute could not be excluded.
- **NVML reads (1 Hz)**: frames within 5 ms of a read were stutters 1.54 times as often as chance would give (35
  against 22.8; calibrated p 0.025-0.04; among 100-400 decoy trains the raw z ranked at 0.01 and the calibrated p at
  0.01-0.05), the same in both halves. That is borderline: not significant, not ruled out. If real, it is about one
  extra stutter a minute. The window holds the 1 Hz row read, which also reads the two hardware throttle counters
  (0.3-0.7 ms in the driver, up to about 9 ms seen); the power polls every ~10 ms before it come too often for this
  test to isolate.
- Network rounds could not be tested: that recording ran with `--no-network`.
- What the test cannot see: only a cost locked to the sampler's and NVML's own moments shows in it. A cost spread over
  every frame (PresentMon's ETW writes) or at random times (the PC-latency pings, section 5.1) cancels out, and with
  VSync holding 91 % of frames to the refresh, a stall that does not push a frame past the next refresh leaves no
  trace [inferred]. On this recording the calibrated verdict still flagged 5-12 % of the decoy trains, against the 5 %
  intended, and the phase profile up to 16 % [measured: `<scratch>\phaselock\real_1106_decoys2.txt`,
  `real_1106_gpu_decoys400.txt`].

## 5. Risks beyond plain CPU

### 5.1 PC-latency tracking switches on code inside some games

- Until 2026-11-09 lag-doctor passed `--track_pc_latency` on every PresentMon capture [documented: `presentmon.FLAGS`
  at 1e0ff72]. PresentMon 2.6.0 then enables NVIDIA's PC Latency provider at verbose level with keyword masks of 0, which ETW treats as every
  keyword, and with no event-ID filter and no process filter, so every PCL event from every process on the PC reaches
  the session [research: `PresentMonTraceSession.cpp` lines 986-990].
- A game built with NVIDIA's PCL Stats markers (Reflex or Streamline titles, on any GPU) starts a ping thread at launch
  that does nothing until a trace session enables that provider. Once one does, every 100-299 ms it writes an ETW event
  and posts a ping: a thread message to a thread the game named, focused or not, or else, only while the game's own
  window is in front, a window message or F13-F15 key messages to that window; with no named thread and the game in
  the background, a wake does nothing. Every marker the game writes also becomes a real ETW write [research: NVIDIA's
  `pclstats.h` in Streamline and the Reflex SDK; NVIDIA's "Understanding and Measuring PC Latency"].
- PresentMon issue #570 (filed 2026-01-25 by CapFrameX's lead developer): FINAL FANTASY VII REBIRTH showed frame-time
  inconsistency and stutter only while PresentMon ran, on Windows 11 build 26200 with an RTX 5090, and the reporter's
  follow-ups tie it to PC-latency tracking being on. It stayed when his own PresentMon build only counted the PCL
  events (about 970 marker events a second), and another PCL game (Dying Light: The Beast, about 1,300 a second) had
  no problem. He put the cause in the game's own PCL implementation or its interaction with NVIDIA's driver, not in
  PresentMon's code or ETW overhead in general, and closed the issue on 2026-04-21 with no maintainer reply.
  [research: issue #570 and its timeline]
- What lag-doctor got from the flag (until 2026-11-09): no stored column (it keeps no `MsPCLatency`). For PCL-instrumented games only,
  PresentMon computes `MsAnimationError` from the game's own simulation-start markers instead of the CPU start, and
  `anim_error_ms` feeds the pacing test in `analysis/events.py`, which can add or remove pacing events and the findings
  built on them. Every other game records the same data without the flag. [documented: `presentmon.frame_row`,
  `model.F`, `analysis/events.py`; the switch to PCL markers: docs/research/presentmon.md section 2 and PresentMon
  2.6.0's source, verification log]
- So, until 2026-11-09, recording a PCL game with frame timing changed what that game executed, and for FINAL
  FANTASY VII REBIRTH that change is reported to cause the very stutter lag-doctor is there to measure.
  docs/research/presentmon.md called the flag cheap [inferred there]; for PCL games it was not free. Not measured here:
  the only recording with frame timing ran with the flag, and none was made without it.
- lag-doctor stopped passing the flag on 2026-11-09 (DECISIONS), and `test_pc_latency_tracking_stays_off` keeps it
  out. A PCL game's `anim_error_ms` now comes from PresentMon's CPU-start estimate, as every other game's does. In a
  Reflex game that estimate is weaker: it takes the frame's simulation start to be the moment the previous Present()
  returned, and NVIDIA places Reflex's sleep between the two, so the animation error also carries how much that sleep
  changes from frame to frame [inferred from PresentMon 2.6.0's source and NVIDIA's Reflex guide, not measured]. The
  pacing events built on it (and the findings that use them) are less trustworthy there, and a recording of such a game
  made before 2026-11-09 (its `target["presentmon"]["args"]` hold the flag) is not like-for-like with a later one.

### 5.2 NVML power readings

NVIDIA's driver 388.31 (2017) fixed a bug in which games micro-stuttered while monitoring tools read GPU power
[research: NVIDIA's release notes RN-08399-388.31, bug 2016377]. HWiNFO's author wrote in December 2023 that NVML,
especially power monitoring, had become problematic a couple of driver versions back, after a user found that
switching off HWiNFO's NVML support stopped V-Sync frame drops even with every sensor disabled; in December 2025 he
suggested power monitoring as a possible cause of one game's stutter [research: hwinfo.com forum]. MSI Afterburner
reads power through NVAPI, not NVML (only NVIDIA's OC Scanner component, which it runs, uses NVML), and users have
tied its NVAPI power reads to stutter too [research: Afterburner's developer on the hwinfo.com forum, 26 Dec 2023;
user reports there and on Guru3D]. lag-doctor reads NVML power about 35 times a second while it waits for each
refresh, against HWiNFO's default of once every 2 s [measured: `<scratch>\nvidia-sampler`; HWiNFO's default from a
later post in hwinfo.com forum thread 9376, found by one research agent and not re-read by the skeptics]. On LagLab,
NVML ran only in the light workload capped at 144 FPS on the NVIDIA GPU, where a cap hides small costs, and attaching
changed nothing there (section 4.1). In the real recording, frames near the 1 Hz row read are the borderline signal of
section 4.2; the power polls come too often for that test to isolate. Each row read spends 0.3-0.7 ms in the driver's
kernel path, up to about 9 ms seen [measured: `<scratch>\nvidia-sampler`]; NVML most likely reaches the driver through
graphics-kernel escapes, and an escape flagged as needing the hardware runs only while the GPU is idle [documented:
Microsoft's D3DDDI_ESCAPEFLAGS page; whether NVML's escapes are flagged so is unknown].

### 5.3 Smaller ones

- **Shared power budget** on a laptop: lag-doctor's CPU work draws package power that a power-limited game shares
  [inferred].
- **Repeated whole-system snapshots**: the sampler takes 4 a second, and the session, PresentMon's watcher and the
  tracker each take another every second, at 6-9 ms each: about 2-3 % of one CPU, of which one shared per-second
  snapshot could save 1.2-1.8 % and reusing the sampler's own snapshot all of it [measured per snapshot; inferred
  total]. Since M3 the relay takes one more a second for the game's process tree.
- **A leaked trace session**: if PresentMon is killed hard (with lag-doctor's whole process tree, say), its session
  keeps its providers on in every presenting process until the next recording stops it by name or the PC restarts
  [documented: docs/research/presentmon.md, section 7.2]. Meanwhile it holds up to 64 MiB of nonpaged memory and one of
  the PC's roughly 64 trace sessions, and once its unread buffers fill, failed writes can also cost the same providers'
  other sessions (another overlay's) their events, since PresentMon does not set independent-session mode [documented:
  Microsoft's Logging Mode Constants page]. The tracker's `lag-doctor-net` session can leak the same way, leaving
  every packet on the PC logged. If only lag-doctor dies, PresentMon records on until the game exits [inferred:
  `presentmon.spawn` starts it as a plain child, in no kill-on-close job].
- **A sleeping NVIDIA GPU kept awake**: NVML stays on when the game's GPU is unknown at the start (no 3D use in the 2 s
  sample) and is never re-checked, so it polls the NVIDIA GPU 25-35 times a second for the whole recording, about 86
  if its readings change less often than every 0.9 s. Whether that wakes a sleeping hybrid-laptop GPU is unverified;
  if it does, the GPU never idles back, and its watts and heat come out of the cooling an Intel-GPU game shares
  [inferred].
- **Start-up**: `watch` samples the GPU for 2 s (before M3, twice in an elevated recording) and runs a PowerShell query
  (0.6 s of CPU, plus WMI work not measured) while the game already runs, and PresentMon asks the graphics kernel for
  a rundown of every GPU device, context and queue when its session starts; an elevated recording also shows a UAC
  prompt on the secure desktop, which takes the foreground from the game (since M3 the relay it starts has no window;
  before, the elevated recording opened its own console) [measured: timings; inferred: the focus]. None of this was
  measured against frames: the benchmark's attached windows began 6.5-8.5 s after the recorder launched. In the one
  run whose recorder launched with LagLab, the frame rate fell from about 1,900 to about 1,050 FPS while it started,
  but the end of the boost window and twice the background load overlapped it [measured: `<scratch>\bench\smoke`].
- **After the stop**: the save takes 1.6-4.9 s (1.9-5.3 s of CPU) per recorded hour and the analysis about 12 s per
  hour of real-like data, both in competition with the game if it is still running [measured].
- **The lag key**: Ctrl+Alt+L is registered for the whole recording, so a game bound to it should not receive it, and
  a game that turns application hotkeys off while in front drops the player's marks without a word [documented:
  `hotkey.py`, docs/research/desktop-app.md; the swallowing inferred].

### 5.4 Known PresentMon 2.6.0 problems

- Issue #695 (filed 2026-10-04 by the CapFrameX developer): with `--track_frame_type --track_app_timing`, PresentMon
  2.6.0 grew to about 260 MB and a full CPU core during a UE5 game, with frame-time spikes in the game, because
  app-timing entries were never pruned. lag-doctor passes `--track_frame_type` but not `--track_app_timing`, the only
  flag that requests those events, so it probably does not hit this [inferred]. The same issue saw output stall for
  about 60 s after frame generation was switched off: a data cost, not a game cost.
- The fix, PR #702 (merged 2026-10-16, in no release yet), also removes stale PC-latency data from other processes.
  lag-doctor's pinned 2.6.0 lacks it, but since 2026-11-09 lag-doctor no longer enables PC latency, so that data does
  not arise. In the one real 13-minute recording, made with the flag, PresentMon stayed at 0.4-7.2 % of one CPU.
- Issue #417: PresentMon's CPU rose from about 0.04 % to 4.5-10 % when an app's present mode changed from independent
  flip to composed (windowed, or forced by an overlay), work its priority-15 consumer thread does.
- lag-doctor leaves PresentMon's input tracking on (CapFrameX passes `--no_track_input`), so a 1,000 Hz mouse may add
  up to about 1,000 events a second to the session [inferred].

[research: one agent's reading of the issues and the PR; not re-read by the skeptics]

## 6. Outside figures on ETW's own cost

- A 2005 MSDN post by Ryan Myers (Windows Client Performance Team): delivering an event takes 1,500-2,000 cycles,
  depending on settings; 20,000 events a second cost 5 % of a 500 MHz Pentium III. Pre-Vista, order of magnitude only.
- A 2012 MSDN post by Vance Morrison (.NET runtime performance architect): about 1.6 µs per EventSource event, read
  off 10 back-to-back events on a 2 GHz laptop.
- Microsoft's Sessions page (Windows Performance Toolkit): each processor has its own buffer, writing is lock-free,
  and when no buffer is free the write fails with ERROR_NOT_ENOUGH_MEMORY and the event is lost. The page does not say
  the writer is not blocked; Microsoft's TraceEvent (classic API) reference says that call discards the event rather
  than blocking until buffers become available, and the EventWrite reference says only that the event is dropped.
- No vendor publishes a measured overhead for PresentMon's console, FrameView, CapFrameX or OCAT capture; NVIDIA calls
  FrameView "minimal overhead" without a figure, and NVIDIA's FrameView guide and OCAT's release notes treat the
  in-game overlay, which lag-doctor does not have, as the overhead to remove; the only figure on a benchmark is one
  2019 forum user's (FrameView cost 0.5-1 % of CPU performance in 3DMark), and PresentMon's own issue tracker holds
  user reports of larger costs, one of them in 2.6.0 (section 5.4). [research: one agent's search (NVIDIA's
  reviewer-toolkit page and FrameView guide, OCAT v1.6.0's release notes, a 2019 Guru3D post); not re-read by the
  skeptics]

## 7. Not measured

- **PresentMon live**: its exact CPU and any frame-time effect need ETW rights, which the agent's shell lacks. The
  scratch benchmark can also alternate none, PresentMon alone and everything, run from an administrator terminal.
  LagLab is OpenGL and not PCL-instrumented, so even that would not test section 5.1.
- **The tracker live**, the tracker's event rate during a download, and the console window's drawing cost.
- **Power**: package watts and C-state residency need a driver or an administrator.
- **Other games and frame rates**: the real-recording figures are one game at about 56 FPS with VSync on 91 % of
  frames.
- **Section 5.1's risk**, no longer lag-doctor's since the flag was dropped: measuring it needs a game built with PC
  Latency markers, recorded over the same scene with and without `--track_pc_latency`. The report it rests on used PresentMon 2.4.1, and its title blames gamepad input [research:
  one agent's reading of issue #570].

## 8. How the benchmark was kept honest

- **The turbo window.** Uncapped LagLab starts fast and then settles: in the quiet 380 s run it held about 2,050 FPS
  for 22 s and then about 1,600 FPS, and in a 70 s run with 5-7 CPUs of other work it fell after 12 s from about 1,950
  to 600-1,200 FPS, before the recorder started. A boost or power limit is the likely cause [inferred: no power
  readout, and the one limit counter recorded, % Performance Limit, read no limit], and the boost lasted differently
  from run to run (the first 22 s run never dropped; the one after it fell after about 3 s). Two separate 22 s runs, A
  then B, showed a 44 % "slowdown" that mixed that drop with B's own start-up (its recorder launched 0.36 s after
  LagLab) and nearly twice A's background load (6.3 CPUs against 3.4); hence one long run, a 60 s warm-up and
  alternating windows.
- **Background load is logged per window.** The first pass ran while one of this research's own analysis agents was
  running a permutation test: other work kept 12-15 CPUs busy through most of the CPU-bound run but only 2-6 through
  most of the NVIDIA run (the NVIDIA rows above come from this pass); the CPU-bound and uncapped workloads were run
  again on a quiet PC (1.2 CPUs busy at the start) and those are the Intel rows above.
- **CPU time depends on load.** The same parsing work cost 33 µs per frame on a quiet PC and 62-66 µs with 11 CPUs
  busy, so CPU costs here are ranges.
- **Handle-free accounting.** Every CPU figure for LagLab and the recorder comes from NtQuerySystemInformation
  snapshots; no process was opened.

## Verification log (2026-11-08)

Six outside claims were each given to two skeptics working independently, told to refute them from primary sources,
and all twelve verdicts came back "confirmed with corrections"; the corrections are in the text above. HWiNFO's 2 s
default (section 5.2) and the last bullet of section 6 were not given to them: they rest on one research agent's
reading. The last row is the recomputation of the real-recording figures. Afterwards four checkers recomputed every
number in this note from its raw source and a fifth read it for misleading claims; their corrections are applied.

| Claim | Result | Source read |
|---|---|---|
| PresentMon issue #570: FINAL FANTASY VII REBIRTH stutters only while PresentMon runs with PC-latency tracking | confirmed with corrections: the reporter blames the game's PCL implementation, not PresentMon; the gamepad link is not consistent across his data; closed by the reporter on 2026-04-21, no maintainer reply | the issue, its comments, timeline and edit history (GitHub API) |
| `pclstats.h`: the ping thread starts at launch, idles until a session enables the provider, then pings every 100-299 ms | confirmed with corrections: the wait is 100 + rand() % 200 ms; with the game in the background and no named thread a wake does nothing | Streamline 2.11.1 and Reflex SDK 1.8 headers; NVIDIA's PC Latency article |
| PresentMon 2.6.0 enables the PCL provider unfiltered at verbose level; TIME_CRITICAL consumer thread; 64 KB × 256-1,024 buffers; FlushTimer 0; DxgKrnl Performance keyword cleared | confirmed with corrections: keyword masks of 0 mean every keyword; FlushTimer 0 is a 1 s flush for a real-time session; the buffer counts are requests ETW may adjust; the source warns only about filtering DxgKrnl events by that keyword, on some Windows versions | tag v2.6.0 (e13fce6) source; Microsoft's EnableTraceEx2 and EVENT_TRACE_PROPERTIES pages |
| lag-doctor keeps no PCL column; of its stored columns, only `anim_error_ms` is computed differently for PCL games | confirmed with corrections: true of PresentMon's arithmetic only, since for a PCL game the flag also changes what the game runs and any frame column can change; a refused PCL enable stops PresentMon's session; with the list of findings `anim_error_ms` feeds | `presentmon.py`, `model.py`, `analysis/events.py`, `analysis/rules.py`, PresentMon 2.6.0 source |
| NVIDIA 388.31 fixed power-monitoring micro-stutter; HWiNFO's author on NVML power (2023, 2025); Afterburner avoids NVML | confirmed with corrections: the 2025 remark was a suggested cause, not a diagnosis; Geeks3D mislabels the list as 388.13's; NVIDIA's acknowledgement covers only that 2017 bug; in the 2023 case NVML being loaded, with every sensor off, was enough; Afterburner's OC Scanner does use NVML, and users tied its NVAPI power reads to stutter too | NVIDIA's release notes PDF, Geeks3D, hwinfo.com forum, Guru3D |
| ETW cost figures (2005, 2012, the Sessions page) | confirmed with corrections: authors named, figures marked as informal, "not blocked" moved to the TraceEvent page | the archived MSDN posts, learn.microsoft.com |
| Real-recording CPU figures | per-process ranges and the game's CPU means recomputed by a second script reading the raw gzip JSON without lag-doctor's code: identical; its totals also count the never-listed waiting processes and it computes no share of the game's CPU, which a later standard-library recount checked (one rounding fix: 5.7 %, not 5.8 %) | `<scratch>\owncost\xcheck.py`, `<scratch>\verify-note\recompute.py` |
