# Other hardware: can lag-doctor be tested on AMD, Intel and other NVIDIA PCs for free?

Research date: 2026-11-09. Question: can lag-doctor be tested on other GPUs (AMD Radeon, Intel Arc or an Intel
integrated GPU alone, other NVIDIA cards), other CPUs (AMD Ryzen including X3D, older Intel, ARM64) and other makers'
laptops without paying, and how? No plan is built on this yet: the owner has not decided.

Method: one research workflow of 9 agents; the code was read on main at 00e2435. One agent read the code and docs to
map what lag-doctor does on each kind of machine (section 2). Four searched the web: cloud machines with GPUs; CI
services and volunteer-hosted runners; volunteer testing; GPU readings on AMD, Intel and NVIDIA from ctypes. Four
fact-checkers, one per web track, re-read its claims at their sources (verification log at the end). Read-only probes
of DXCore, D3DKMT and Intel's Graphics Control Library (IGCL) ran unelevated on the development laptop, called "this
laptop" below: Windows 11 build 26200, an RTX 3070 Laptop GPU on NVIDIA driver 591.86, and Intel UHD graphics. The
main claim the fact-checkers disagreed on, whether AWS's Free plan can start a GPU machine, was read again at AWS's
own pages and searched again. Two reviewers then checked this note against the fact-checked claims and the code, and
their corrections are applied. #34 (headroom) merged after the research; section 2 was checked again on main at
0b24f61 and still holds.

**Tags.**
- `[verified: …]`: re-read at that source by a fact-checker (its corrections applied), or, for AWS's Free plan pages,
  in the second reading after the fact-check.
- `[measured: …]`: read on this laptop or a GitHub runner, in this research or an earlier note.
- `[forum: …]`: users' reports, not the vendor's own pages.
- `[inferred]`: reasoned, not observed.
- `[unverified]`: could not be checked.

## 1. The short answer

- Partly. Four things cost nothing:
  - GitHub's free runners can add Server 2022 and Windows 11 on an ARM64 CPU. The x64 jobs already land at random on
    AMD and Intel server CPUs, which a step logging the CPU would record. None has a GPU (section 3).
  - People with other PCs, friends first and then volunteers, can run lag-doctor and send back a file. This is the
    only free way to reach consumer Radeon cards on AMD's consumer driver (Adrenalin), Intel Arc, Ryzen X3D, other
    makers' laptops and real heat throttling (section 4).
  - Windows has one vendor-neutral way to ask a GPU's driver for its temperature and clocks, so one piece of code
    could serve AMD, Intel and NVIDIA. So far only NVIDIA cards are known to answer it: this laptop's Intel UHD gave
    nothing, and no AMD or Arc card has been tried (section 5.1).
  - What those PCs record can be replayed as tests in this repository, with no hardware (section 5.3).
- No free cloud GPU was found. AWS's Free plan reportedly refuses every machine type outside its free list, Google's
  trial cannot add GPUs, Azure's trial cannot raise its quotas, and Oracle's trial terms could not be read. Every
  other GPU offer costs money, and all but TensorDock's prepaid one can bill past what was planned (section 6).
- On a PC with an AMD or Intel GPU (main at 0b24f61), frame timing, GPU load, the frame-rate verdict (GPU-, CPU- or
  V-sync-bound), the wrong-GPU check and the settings checks use vendor-neutral sources and should work, but have
  never run on one. GPU heat, clocks, power and throttling come only from NVIDIA's driver, so those players get "could
  not check", lines that name NVIDIA, and three empty NVIDIA charts in the app (section 2).
- The research's pick for the most useful single piece to build is a one-command hardware readout. It would need no
  administrator rights, take under a minute and write one file that a friend attaches to an issue, which would become
  a replay test in this repository (section 4) [inferred].

## 2. What lag-doctor does on other machines today (main at 0b24f61)

Read from the code; none of these machines has run it [inferred, except where marked].

- **AMD Radeon desktop.** Frame timing, GPU load and video memory, the wrong-GPU check and the GPU-bound verdict
  work. No GPU temperature, clock, power or throttling is read, so a card slowed by heat shows only as "GPU at its
  limit". Both reports list "NVIDIA telemetry (NVML): not recorded in this capture" and never say that the PC has no
  NVIDIA GPU. The app draws three "NVIDIA GPU …" charts that read "Not measured." The headroom sentence (#34) is
  built from frame timing and the CPU counters, so it should appear as well; its NVIDIA-only part, how far the GPU's
  clock was held down, does not [inferred].
- **AMD APU, alone or with an AMD dGPU.** The same. The hybrid checks read D3DKMT and DXCore flags, not the vendor, so
  they should work as on this laptop. Power that AMD SmartShift moves between the CPU and the GPU, and a dGPU that
  throttles, go unseen.
- **Intel Arc.** The same as Radeon. Also, the end-to-end test's judge (`scripts/e2e_truth.py`) treats any GL
  renderer that names Intel as the integrated GPU, so it wrongly fails a LagLab run on Arc.
- **Intel integrated GPU alone.** The best-covered path after NVIDIA, since this laptop's UHD engine names were
  measured. It lacks only the heat check, and its lines name NVIDIA.
- **NVIDIA desktop, or an older GeForce.** Telemetry runs. Desktop players get laptop advice ("Give the laptop more
  air"), and a power cap is "the design limit of a GPU-bound laptop". GPU heat becomes "could not check" where NVML
  refuses the throttle counters (field ids 269-271 and 74), even if the card still gives NVML's throttle-reason
  bitmask, which lag-doctor records but does not use there. That may happen on GeForce cards: NVML's own header gives
  them only limited support [verified: nvml.h in go-nvml].
- **Ryzen CPU with an NVIDIA laptop GPU.** Works as on this laptop on the GPU side. The CPU caveat of the AMD Ryzen
  bullet below applies, and so does the other-makers bullet unless the laptop is an ASUS.
- **Two dGPUs of different vendors, or an eGPU.** If the game's GPU is unknown when the recording starts, NVML watches
  the first NVIDIA GPU, and nothing later checks that it was the game's. An eGPU user is told to use "the laptop's
  MUX / dGPU-only mode".
- **Windows on ARM (Snapdragon).** Unknown. Everything shipped is x64 (the MSI, the embeddable Python, PresentMon
  2.6.0), so Windows 10 on ARM cannot run it, and Windows 11 runs it only under emulation, untested.
- **AMD Ryzen CPU.** Correctly "not hybrid". The CPU-limit evidence (`% Performance Limit`, `% Processor
  Performance`) was measured only on this laptop's Intel CPU. If AMD leaves the limit at 100 while firmware limits
  bite, the CPU check reads "checked and fine". On dual-chiplet X3D parts (such as the 7950X3D and 9950X3D), whose
  cores without the extra cache are parked during games, the all-core CPU-bound verdict cannot be reached.
- **Older Intel, or another hybrid Intel.** Should work. An older Intel CPU reports one EfficiencyClass, so it is
  correctly not hybrid, and the same counters are read. On another hybrid Intel the top EfficiencyClass counts as the
  performance cores, so on a CPU that reports three classes, the power plan's performance-core cap could be misnamed.
- **A desktop with no ACPI thermal zone.** The system sampler ends "partial", and both reports list it under "Not
  measured" although every other system column was recorded.
- **Another maker's laptop (Lenovo, MSI, Dell, HP, Razer).** Only Armoury Crate's quiet mode is recognised, and OEM
  tools are matched by exact process name. A quiet-named plan with Balanced settings reads "checked and fine".
- **Non-English Windows.** The counters are fine: every one is added with `PdhAddEnglishCounterW`. Power-plan names
  are matched as English words, so the plan checks that go by name miss.
- **A monitor at 240 Hz or more.** "Low frame rate" for most uncapped games: the rule (steady FPS under 80 % of the
  refresh rate) is not tuned above 120 Hz.
- **A standard (non-administrator) account.** Frame timing is off, with the reason (it needs an administrator or
  Performance Log Users); fixes still work.

**The most important unknown.** Picking the game automatically, and every GPU-load signal, depend on the engine type
`3D` in Windows' GPU Engine counters. Only NVIDIA, Intel UHD and Basic Render engine lists were measured [measured:
windows-telemetry.md, section 2.2]. If an AMD or Arc driver names its render engine otherwise, auto mode refuses to
pick a game and the GPU-load evidence is empty.

**Tests.** 10 tests need this laptop's hardware: 7 the NVIDIA driver, 2 a GPU, 1 a thermal zone. All 10 skip on CI
(`LAGDOCTOR_ABSENT`). Apart from PresentMon's own gold captures, whose hardware is not recorded, no fixture comes from
another machine. A test can already run without the hardware on hand-written stand-ins for NVML (`FakeLib` in
`tests/test_nvidia.py`, with this laptop's values) and PDH (`FakePdh` in `tests/test_sysmon_sampler.py`), and on
recorded data for nvidia-smi (`tests/fixtures/nvidia/fake_smi.py`), PresentMon (ETL replay) and saved captures
(`Capture.load`). Missing: a recorded DXGI/DXCore/D3DKMT adapter list, display layout, power plans, CPU sets and PDH
instance names from other hardware.

## 3. GitHub's free runners

- Free and unlimited for public repositories [verified: GitHub's hosted-runner reference and changelog, the
  runner-images readmes, Arm's learning path]:
  - `windows-2025`: Server 2025, build 26100; on 2026-11-09 the label served the VS 2026 image.
  - `windows-2022`: Server 2022, build 20348.
  - `windows-11-arm`: Windows 11 Enterprise 10.0.26200 on Azure Cobalt 100 (ARM64), 4 CPUs and 16 GB, generally
    available for public repositories since 2025-08-07.

  `ci.yml` runs only `windows-2025`.
- `windows-11-arm` is GitHub's only free Windows client image; an x64 Windows 11 image exists only as a partner image
  for the paid GPU runners. Its preinstalled Python reports AMD64 [verified: runner-images issue 14071], so it runs
  x64 under emulation, as lag-doctor's own x64 runtime would on a Snapdragon PC.
- The x64 runners land at random on AMD EPYC 7763, Intel Xeon Platinum 8573C or Intel Xeon 8370C [verified:
  runner-images issues 14774 and 14483]. A job cannot choose, and its log names no CPU; a step running
  `Get-CimInstance Win32_Processor` would record it.
- None has a GPU or an NVIDIA driver [verified: GitHub's hosted-runner reference]. On `windows-2025` there is also no
  thermal zone, and Direct3D sees only the Basic Render Driver (WARP) [measured: install-and-rights.md, section 8];
  `windows-2022` and `windows-11-arm` were not probed. With display and GPU tracking off, PresentMon still sees a WARP
  window's presents on `windows-2025`. So CI can test presents by PID and the analysis, but never `MsGPUBusy` or
  display timing [measured: learnings.md, the WARP entry; install-and-rights.md, section 8].
- Server 2025 creates and deletes small files about twice as slowly as Server 2022 on the same CPU [forum: a
  runner-images contributor, issue 14774]. So judge timing-sensitive tests per image [inferred].
- GitHub's GPU runners (one NVIDIA T4) cost $0.102 a minute on Windows and need an organization on a paid plan (Team
  or Enterprise Cloud) with a card [verified: GitHub's runner pricing and larger-runner reference; the cloud
  fact-checker found no organization rule on the pages it read].

## 4. People with other PCs: a hardware readout

### 4.1 What other free tools do

- LibreHardwareMonitor: users attach a saved text report to issues. It prints the motherboard, chassis and CPU serial
  numbers unscrubbed [verified: its SMBios.cs]. So copy the one-file idea, not the fields [inferred].
- Intel PresentMon asks for an ETL trace it can replay [verified: its CONTRIBUTING.md]. Its collection script turns on
  kernel loader and process events [verified: its start_etl_collection.cmd], so the trace holds every process and DLL
  path on the PC [inferred].
- Intel's Arc forum asks for at least three CapFrameX captures with each performance report [verified: Intel's Arc
  bug-report guide, 2023-06-09].
- gpuinfo.org keeps one report per GPU, OS and driver, is run by one person, and leaves the submitter's name optional
  [verified: its about page and client source].
- Phoronix Test Suite asks before each upload (automatic upload is off by default) and strips chosen strings from its
  hardware info [verified: its documentation].
- hw-probe hides user and host names, IP and MAC addresses and serial numbers, and warns that the hiding may fail in
  certain edge cases [verified: its README].
- A salted hash of an identifier is still personal data for whoever can link it back, while anonymous data is outside
  the GDPR [verified: GDPR Recital 26]. So drop identifiers; never hash them [inferred]. This is not legal advice.

### 4.2 What a readout would record [inferred from sections 2 and 4.1]

- Adapters: name, vendor and device ids, memory, integrated or not, hybrid role, hardware-accelerated GPU scheduling
  (HAGS) and driver version. Adapter LUIDs (ids Windows assigns at each boot) are replaced by made-up values, which
  also replace them in the PDH instance names.
- PDH: for each counter lag-doctor reads, whether it could be added, its instance names and its values, idle and
  while a test load runs. Other processes' PIDs are replaced, and their names never recorded. LagLab ships in neither
  the wheel nor the MSI and needs pyglet (`pyproject.toml`), so the readout would have to ship it or draw its own load.
- The CPU's name and, per logical processor, its group, core, cache, NUMA node, EfficiencyClass and parked flag.
- The Windows build and display language; the power plans (Windows' built-in GUIDs kept, others replaced) and the
  power mode; whether a battery is present.
- Displays: refresh rates and modes, with no device paths and never the raw EDID, which holds a serial number.
- NVIDIA, where present: which NVML fields answer (the throttle counters 269-271 and 74, and the temperature
  thresholds 7, the target, and 5, the acoustic one) and how often its readings refresh. Never
  `nvmlDeviceGetSerial`, the UUID or `nvidia-smi -q`.
- The raw bytes of each new GPU query (section 5) with its return code, minus the BIOS version in the GPU-version
  query, and the vendor app's own reading in the same seconds.
- The names of allowlisted OEM tools only, never the process list.

**Never recorded:** user and machine names, SIDs, profile paths, the game library (exe paths, store app ids), the
process list, IP and MAC addresses, Wi-Fi names, serial numbers, GPU UUIDs, the GPU's BIOS version, custom power-plan
GUIDs, network-adapter GUIDs, mod and drop-in file names, the player's note and exact times.

A saved capture (`.ldcap.json.gz`) replays everything after recording, but it holds the game's path, the window
title, other programs' names, IP addresses, the player's note, the game folder's mod, drop-in and ini file names,
paths and hashes, and times with the PC's UTC offset. So it cannot be committed as it is.

### 4.3 Getting it to volunteers

- `docs/INSTALLER-PLAN.md` Stage 5 already plans the release side: an unsigned preview release of the MSI, then the
  SignPath application, and a test on a Smart App Control machine (E6) before announcing.
- A GitHub Release is needed for the readout too. CI artifacts need a GitHub sign-in and expire after 90 days: an
  unauthenticated download of one returned 401, while a release asset returned a redirect [measured: 2026-11-09].
- Smart App Control blocks unsigned code that has no reputation, and cannot allow a single app [verified: Microsoft's
  Smart App Control pages].
- Microsoft's page for App Control for Business says it does not check scripts that Python runs [verified:
  Microsoft's script-enforcement page]; that Smart App Control does the same is inferred. python.org's `python.exe`
  is signed by the PSF [measured: `Get-AuthenticodeSignature` on python.org 3.13.3].
- So `py -m pip install` of the release's wheel, then `py -m lagdoctor`, is the likely route on PCs with Smart App
  Control on, and the unsigned MSI is probably blocked there [inferred; untested]. Avoid uv, a uv-managed Python,
  console-script `.exe` launchers and a source ZIP there: `pip.exe`, uv and uv's Python are unsigned [measured:
  `Get-AuthenticodeSignature`], and Smart App Control blocks of a downloaded `.bat` file and of a venv's `python.exe`
  have been reported [forum: two projects' issue trackers].
- SignPath Foundation signs open-source releases for free, but wants an existing release, MFA, manual approval of each
  release and "a certain verifiable reputation" [verified: signpath.org terms]. It refused a 24-star project on
  2026-10-07 for lack of visibility [forum: that project's issue tracker]. So apply once testing has produced public
  mentions [inferred].
- Intake: an issue form with an upload field and a required consent box [verified: GitHub's form-schema docs].
  Filing it needs a GitHub account, though downloading a Release does not.
- Attachments are capped at 25 MB; `.json`, `.zip` and `.gz` are accepted and `.etl` is not [verified: GitHub's
  attachment docs]. Uploads to a public repository are public.
- Discussions were off on this repository on 2026-11-09.

### 4.4 Where to ask

- People the owner knows come first: the owner can sit with them through the UAC and SmartScreen prompts.
- Frame-pacing communities:
  - r/allbenchmarks, the English venue CapFrameX names [verified: capframex.com/support];
  - the Blur Busters forums [unverified: their rules page refused the fetch];
  - CapFrameX's threads on ComputerBase, PCGH Extreme and Hardwareluxx, in German [verified: capframex.com/support].
- A general technical forum: Level1Techs, whose rules remove self-promotion "with the intention of making revenue"
  and say nothing of free projects [verified: its rules].
- Hacker News's Show HN only once real-hardware results exist. It asks for something people can try, and says not to
  post quickly made one-offs [verified: news.ycombinator.com/showhn.html].
- Skip:
  - r/buildapc: "No self-promotion, advertising or surveys" [unverified: search snippet];
  - r/techsupport, whose sidebar says it is not for recommendations or adverts [unverified: search snippet];
  - r/opensource: new rules exclude projects made with AI and want a year of maintenance [verified: a news report,
    2026-10-25];
  - Steam groups [unverified: Steam's conduct page did not render], and Discord direct messages, which Discord counts
    as spam [verified: Discord's spam page];
  - BetaList, which charged every submission on 2026-11-09, and Beta Family, free only with testers a project brings
    itself [verified: BetaList's support page and Beta Family's pricing page].
- Who to chase first: in Steam's October 2026 survey, AMD makes 46.51 % of Windows PCs' CPUs; GPUs are about 71 %
  NVIDIA, 14 % AMD (about 4 points of it integrated) and 6 % Intel; Windows 10 is on 21.11 % [verified: Steam's
  hardware survey; the vendor shares are sums of the listed models].

### 4.5 Live CI on a friend's PC

Registering a volunteer's PC as a runner of this public repository is unsafe. GitHub says self-hosted runners should
almost never serve public repositories, because a pull request can run code on them [verified: GitHub's secure-use
reference]. A just-in-time runner registered to a separate private repository, started only by hand and running only
a released build, is possible [verified: GitHub's runner REST and workflow_dispatch docs] and would suit a trusted
friend [inferred]. But it needs an interactive desktop session on their PC, run elevated or by a member of
Performance Log Users (only the network tracker needs elevation), and gives little the readout does not. So it would
wait until the readout has run [inferred].

## 5. GPU readings on AMD, Intel and other NVIDIA cards

### 5.1 From Windows itself: one query for every vendor

- **DXCore** `IDXCoreAdapter::QueryState` gives temperature (a float, in °C), engine and memory clocks against their
  maximum, and the processes using the adapter, each behind `IsQueryStateSupported` [verified: dxcore_interface.h in
  SDK 10.0.26100; the Learn page is marked prerelease].
  - Measured unelevated on this laptop: the RTX 3070 Laptop GPU gave 62.0 °C and its clocks; the Intel UHD and Basic
    Render answered "unsupported" [measured].
  - It has no fan, power or throttle-start temperature.
  - Its per-process states take a process id. Whether dxcore.dll opens a handle to that process inside is unknown, so
    lag-doctor must not ask them about the game.
- **D3DKMT** `D3DKMTQueryAdapterInfo`, types 61 (an engine's perf data), 62 (the adapter's perf data), 63 (its caps)
  and 64 (the GPU version, with its BIOS string) [verified: d3dkmthk.h and the Learn pages].
  - Types 61 to 63 give the temperature in tenths of a degree, the temperature where throttling starts and the
    maximum, the fan's RPM, power as tenths of a percent (not watts), and clocks.
  - It needs a driver for WDDM 2.4 or newer (Windows' display driver model; Windows 10 1803 or later) but no
    administrator rights, and it uses the adapter handle `lagdoctor/winapi/dxgi.py` already opens.
  - A field the driver does not fill reads 0, so 0 has to be read as "not reported"; a real 0, such as a fan stopped
    at idle, reads the same [verified: windows_exporter's implementation].
  - Measured unelevated on this laptop: the RTX 3070 Laptop GPU gave 59.2 °C, a 105 °C maximum and its clocks, with
    the throttle-start temperature, power and fan at 0. The Intel UHD gave zeros everywhere. Basic Render refused
    types 61 and 62 with STATUS_INVALID_PARAMETER and answered 63 with zeros [measured].
  - A desktop GTX 1070 did report a throttle-start temperature, 83 °C [verified: windows_exporter's pull request].
  - Pitfall: the Learn page gives `D3DKMT_NODE_PERFDATA` 64 bytes, the header 56. A 64-byte buffer fails with the
    same error as "no more engines" [measured].
- Neither gives a throttle reason, and neither has run on an AMD or Arc card. Whether polling either wakes a sleeping
  hybrid dGPU is untested. So, until it is known, read them only while the game renders on that GPU or its GPU is
  unknown, as lag-doctor does with NVML [inferred].

### 5.2 From each vendor's driver

- **AMD Display Library, ADL (`atiadlxx.dll`).** Its Overdrive 8 sensor log (PMLog) gives edge, hotspot and memory
  temperatures, clocks, activity and power, and `THROTTLER_STATUS` bits for power, heat and current: AMD's equivalent
  of NVML's throttle reasons. ADL is superseded by ADLX, its PMLog query is marked deprecated, and it is untested in
  this research. Its headers are MIT [verified: AMD's display-library repo].
- **Its successor, ADLX (`amdadlx64.dll`).** The richest AMD metrics, callable from ctypes through its C vtables, but
  no throttle or limit metric. Its licence allows derivative works for internal use only and distribution in object
  code only, which is at odds with publishing bindings in this MIT repository [verified: the ADLX SDK License
  Agreement].
- **Intel IGCL (`ControlLib.dll`).** Clocks, activity, temperature where supported, and five limit flags (power,
  temperature, current, voltage, utilisation) with no support bit. It is 64-bit only, and calls under 50 ms apart
  return the same data. Its licence allows use and redistribution "solely for use on Intel platforms", with Intel's
  notice [verified: its LICENSE].
  - On this laptop's Intel UHD, its clocks and activity work and its temperature is unsupported. Its "GPU energy"
    counter tracked CPU-package power (checked against Windows' Energy Meter counters), so it must never be shown as
    GPU power on an integrated GPU [measured].
- **NVIDIA NVML**, already used: temperature, power, clocks and throttle reasons; GeForce cards get limited support.
- **NVIDIA NVAPI, `NvAPI_GPU_GetPerfDecreaseInfo`.** Thermal, power-cap and power-supply slowdown bits: a fallback for
  GeForce cards where NVML's throttle counters and reasons are unsupported, untested in this research. Its licence is
  MIT [verified: NVIDIA's nvapi repo].

PresentMon reads AMD through ADL and Intel through IGCL in its service, not in the console that lag-doctor ships, and
the console's `--track_hw_measurements` adds no telemetry column [verified: PresentMon's source on main, 2026-11-06].

Windows' Energy Meter counters (RAPL, the CPU's own energy counters) list the CPU package's, cores', graphics' and
DRAM power through PDH, unelevated, on this laptop [measured]. There the graphics one read 0 while the package read
28.9 to 35.3 W. Whether Ryzen PCs have them is unverified.

### 5.3 Testing without the hardware

- Header checks: fetch each vendor's header at a pinned commit with a SHA-256 pin, as `tests/downloads.py` does for
  `WebView2.idl`, and compare struct sizes, offsets, enum values and vtable slots: d3dkmthk.h and dxcore_interface.h
  from microsoft/win32metadata, igcl_api.h, the ADL headers and nvml.h. Leave the ADLX headers out: fetching them is
  use under its licence [inferred]. The runner's Visual Studio and LLVM could also compile generated `offsetof`
  assertions against the real headers [verified: the windows-2025-vs2026 image readme].
- Fakes: function tables like `FakeLib` for the flow, the units and every "unsupported" answer; ctypes-callback
  vtables for DXCore, with slot numbers taken from the header, never from the module under test.
- Replay: the readout's raw bytes, copied into the caller's buffer by a stand-in, checked against the vendor app's own
  reading taken at the same time.
- CI's Basic Render adapter as a live "unsupported" case on every pull request [inferred: its presence on the runner
  is measured, its D3DKMT answers are not].

## 6. Routes that need a card or money

- **AWS.**
  - The Free plan is for accounts opened on or after 2025-07-15. It gives a $100 sign-up credit and up to $100 more,
    for 6 months, and never charges. It reportedly refuses every instance type outside its Free Tier eligible list
    (t3.micro, t3.small, t4g.micro, t4g.small, c7i-flex.large and m7i-flex.large), and none of those has a GPU
    [verified: AWS's EC2 Free Tier page for the list; forum: "not eligible for Free Tier" refusals in three projects'
    reports].
  - AWS's own plan pages say the Free plan gives "access to select AWS services and features" and name no instance
    types [verified: AWS's Free Tier plans page, in the second reading].
  - Nobody who has, or ever had, an AWS account can get the Free plan or its credits [verified: AWS's Free Tier FAQ].
    Joining AWS Organizations or setting up Control Tower upgrades a Free-plan account and ends use of the credits
    [verified: AWS's Free Tier terms and FAQ].
  - The Paid plan keeps the same credits for 12 months after sign-up, then bills the card [verified: AWS's Free Tier
    plans page and terms]. The quota for G-family instances starts at 0 vCPUs and needs a request [verified: AWS's
    EC2 quota page].
  - Windows prices in us-east-1: g4ad.xlarge (AMD Radeon Pro V520) $0.56 an hour, g4dn.xlarge (NVIDIA T4) $0.71,
    g6.xlarge (NVIDIA L4) $0.99, and g6f.large (1/8 of an L4 as a vGPU slice, where NVML hides its sensors) $0.294
    [verified: AWS's price list].
  - A Budgets action can stop chosen instances at a threshold, but it is no cap: disks, public IPv4 addresses ($0.005
    an hour) and data transfer keep billing [verified: AWS's Budgets and VPC pricing pages].
  - On such a VM, use AWS's AMD driver, or NVIDIA's GRID or gaming driver, never the Tesla driver, which cannot render
    [verified: AWS's driver pages]. Record through Amazon DCV rather than RDP, which renders on the CPU by default and
    caps at 30 FPS [verified: Microsoft's RDP pages].
- **Azure.**
  - Free-trial subscriptions cannot raise quotas [verified: Azure's subscription limits], so they get no GPU VM
    [inferred: no primary source says GPU quotas start at 0 on a trial]. Pay-as-you-go has no spending limit
    [verified: Azure's spending-limit page].
  - NV4ads_V710_v5 (1/6 of an AMD Radeon PRO V710, RDNA 3) costs $0.517 an hour on demand and $0.096 as a spot VM in
    East US. NG8ads_V620 (part of a Radeon PRO V620, a series sold from 1/4 of a card to a whole one, on AMD's
    cloud-gaming driver) costs $1.003 an hour in East US 2 [verified: Azure's retail price API and size pages].
  - The A10 slices are vGPU, where NVML hides temperature, power, clocks and throttle reasons [verified: NVIDIA's vGPU
    guide]. NVv4 (MI25) was retired on 2026-09-30 [verified: Azure's NVv4 retirement notice].
- **Google Cloud.** The trial cannot add GPUs or create Windows Server VMs, and Compute Engine has no spend cap
  [verified: Google's free-trial and budget pages].
- **Oracle Cloud.** Its limits table lists GPUs as "Contact Us" for trial and pay-as-you-go accounts alike, and Windows
  runs only on its A10 VM shapes, with NVIDIA's vGPU driver [verified: Oracle's service-limits and A10 pages], which
  needs an NVIDIA vGPU licence [unverified]. The trial's own terms could not be read [unverified: oracle.com/cloud/free
  refused the fetch].
- **TensorDock.** Consumer GeForce cards, for example an RTX 3090 from $0.20 an hour for the GPU alone (CPU, memory
  and disk cost extra) [verified: its pricing page, dated 2024-07-24]. Windows 10 VMs are named in its driver guide,
  not on that page [unverified]. It is prepaid from $5, and a VM is deleted when the balance reaches $0, so spending
  cannot pass the deposit [verified: its pricing page].
- **Shadow PC.** Windows 11 on NVIDIA pro cards, from €39.99 a month [verified: Shadow's offer page].
- **What no cloud VM tests:** laptop hybrid graphics, heat throttling, OEM tools, thermal zones, the power-mode
  slider, consumer Radeon drivers, and X3D or hybrid Intel CPUs [inferred]. Windows Server also lacks the Store and
  Game Bar [unverified: a 2018 Microsoft post about Server 2019], so use LagLab there rather than store games
  [inferred].

## 7. Dead ends

- Hosted GPUs: CircleCI's Windows T4 (Scale plan only), GitLab's GPU runners (Linux only) and GitHub's T4 (paid
  organizations only) [verified: their pricing and docs pages].
- Other free CI: Azure Pipelines (public projects retired in April 2026), AppVeyor (its newest OS is Server 2019),
  Cirrus CI (shut down on 2026-06-01), Travis CI (Server 1809), and Semaphore and Codemagic (no free Windows)
  [verified: their pricing and docs pages].
- Blacksmith gives 3,000 free minutes a month, with Windows in beta on what it calls bare-metal gaming CPUs (model
  unconfirmed). But it needs a GitHub organization, so the repository would have to move or be mirrored, and it has no
  GPU [verified: its pricing and docs pages].
- Windows Sandbox (not on Home [verified: Microsoft's Sandbox pages]; it would measure the VM, not the PC [inferred])
  and Hyper-V GPU partitioning (a Server 2025 host and datacenter GPUs only) [verified: Microsoft's GPU-partitioning
  page].
- Paperspace (no Windows for new users), RunPod and Vast.ai (no Windows) [verified: their docs]. Lambda (no Windows),
  game-streaming services (no desktop) and Intel Tiber AI Cloud (no Windows desktop with a GPU) [unverified: not read
  at their own pages]. NVIDIA LaunchPad, whose pages redirected to NVIDIA's AI model catalogue on 2026-11-09, and AMD
  Developer Cloud, which offers Instinct accelerators rather than Radeon cards [verified: their pages].
- Credit programmes: Microsoft for Startups gives a startup without an investor $200 for 90 days, then $4,800 more
  after business verification, and needs a for-profit company, a credit card and a Microsoft account never used with
  another Azure offer [verified: its FAQ]. AWS's open-source credits would need a Paid-plan account [inferred from
  AWS's Free Tier FAQ], and AWS's research credits need a university post [verified: AWS's research-credits page].

## 8. A suggested order, not a plan [inferred from sections 2 to 7]

1. Add `windows-2022` and `windows-11-arm` jobs to CI, each logging its CPU.
2. Make an AMD or Intel PC's report say what it lacks in its own words ("this PC has no NVIDIA GPU"), and drop the
   NVIDIA charts there. Give desktops desktop advice (the inventory's laptop flag is read by no rule yet). Stop listing
   a PC without thermal zones as "Not measured". Check the low-FPS rule on screens above 120 Hz.
3. The hardware readout, an issue form and a first GitHub Release.
4. Vendor-neutral GPU temperature and clocks: DXCore where it says supported, and D3DKMT on every build for the fan,
   power and throttle-start temperature that DXCore lacks; header-checked and red-first.
5. AMD's throttle bits through ADL (MIT headers), and Intel's limit flags through IGCL once its licence question
   (question 7) is settled; NVAPI as the GeForce fallback.
6. Ask for testers once steps 1 to 3 exist.

## 9. Open questions

1. Which DXCore and D3DKMT fields do AMD drivers (RDNA 2 to 4, APUs) and Intel Arc drivers fill?
2. Do AMD and Arc drivers name the render engine `3D` in the GPU Engine counters?
3. Does polling DXCore or D3DKMT wake a sleeping hybrid dGPU?
4. Does `% Performance Limit` drop on a Ryzen when its power or heat limits bite?
5. Do older GeForce cards answer NVML's throttle counters (269-271 and 74)?
6. Does Smart App Control block the MSI and `lagdoctor.cmd`, and does `py -m lagdoctor` pass (the installer plan's
   E6)?
7. Is a ctypes binding transcribed from IGCL's header acceptable in an MIT repository under "solely for use on Intel
   platforms"? (ADL's headers are MIT. ADLX's licence allows derivative works only for internal use, so the research
   would leave it out; the owner has not decided.)
8. Does the AWS console refuse a G instance on a Free-plan account, as the forum reports say?
9. Does the `windows-11-arm` VM have Windows 11's power-mode slider and Game Mode?
10. Does a file attached to a GitHub issue stay reachable after its comment is deleted?

## Verification log (2026-11-09)

Four fact-checkers, one per web track, re-read its claims at their sources:

| track | claims | confirmed | corrected | unverifiable |
|---|---|---|---|---|
| Cloud machines with GPUs | 57 | 36 | 15 | 6 |
| CI and volunteer runners | 83 | 61 | 20 | 2 |
| Volunteer testing | 67 | 45 | 14 | 8 |
| GPU readings from ctypes | 59 | 45 | 14 | 0 |
| total | 266 | 187 | 63 | 16 |

Corrections that changed a conclusion:

| as first written | corrected to |
|---|---|
| AWS's Free plan runs a GPU instance on its credits with no bill: the one zero-bill cloud GPU | The Free plan reportedly refuses every type outside its free list, so G instances would need the Paid plan and a card. No zero-bill cloud GPU was found |
| Microsoft for Startups gives $1,000 | $200 without an investor, and only to a for-profit company |
| One D3DKMT code path gives AMD, Arc and NVIDIA cards' temperature and clocks, so it settles the question without new hardware | Only NVIDIA cards (this laptop's and a desktop GTX 1070) are known to fill it, and the Intel UHD gave zeros; what AMD and Arc drivers fill needs their hardware |
| IGCL's energy counter gives an Intel GPU's power | On this laptop's Intel UHD it tracked CPU-package power, checked against Windows' Energy Meter counters |
| NVAPI is of no use | It is MIT and has thermal and power slowdown bits: a GeForce fallback where NVML's throttle counters and reasons are unsupported |
| DXCore makes D3DKMT a fallback for older Windows | DXCore has no fan, power or throttle-start temperature, so D3DKMT is needed on every build |
| A runner installed as a service captures no frames | Session 0 can capture another session's presents (experiment E3, install-and-rights.md section 8); what fails is a test window started in session 0 |
| CI's software renderer tests the frame pipeline | Only with display and GPU tracking off: presents by PID and the analysis, never `MsGPUBusy` |
| Test ARM64 with an ARM64 Python | lag-doctor ships an x64 Python and an x64-only PresentMon, so the runner's preinstalled x64 Python is the representative test |
| The readout should scrub the Wi-Fi BSSID (the access point's MAC address) | lag-doctor already records only a per-session label ("AP 1"), never the address; the scrub list needs the player's note, other programs' names and mod files instead |
| PresentMon's issue 142 shows AMD dual-GPU telemetry failing | Its subject was non-English Windows, fixed by switching to `PdhAddEnglishCounter`, which lag-doctor already uses (as `PdhAddEnglishCounterW`); the AMD dual-GPU case came from a comment and was root-caused before the issue closed |

The cloud fact-checker's refutation of the AWS Free plan rested on the eligible-type list and one forum thread. AWS's
own pages, read again, neither confirm nor deny it; a second search found the same refusal in two more projects'
reports.

Two reviewers then checked this note against the fact-checked claims and the code, and found 96 problems between
them, many found by both. None was a privacy problem; the rest were overstated or mistagged claims, wrong details and
unclear wording, all applied. Those that changed a conclusion: Windows' GPU sensors were said to work for every vendor
(the D3DKMT row above); "no free cloud machine has a GPU" was stated flatly although Oracle's trial was never read;
the readout's scrub list missed the GPU's BIOS version; and a friend's runner was said to need elevation, which only
the network tracker does.
