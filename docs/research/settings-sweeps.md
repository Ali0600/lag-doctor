# Settings sweeps: can lag-doctor test a game's settings by itself?

Research date: 2026-11-08. Question: could lag-doctor run short automated runs of a single-player game with different
settings (config-file edits, or a mod's files present or absent), record each run and rank the settings? Online games
with anti-cheat are out of scope. The owner chose the assisted sweep on 2026-11-09 (`docs/DECISIONS.md`): lag-doctor
edits the settings file and ranks the runs, and the player plays each run.

Method: one research workflow of 9 agents on main at 4e93ada. One mapped lag-doctor's code and docs. Four searched the
web: FINAL FANTASY VII REBIRTH; unattended benchmarks and existing tools; repeatability, heat and statistics; scripted
input, config files and DLL levers. A separate fact-checker re-read each web track's claims at their sources
(verification log at the end). Then, without changing anything, the game's `GameUserSettings.ini` was read on the
owner's PC and its save and `Saved` folders were listed. A second reviewer checked this note against the fact-checked
claims, and its 38 corrections are applied. The code was read at 4e93ada; #26, merged since, adds the game-side files
of section 6 (read again at 8a4780a).

**Tags.**
- `[verified: …]`: re-read at that source by the fact-checker (its corrections applied), or seen in the local read.
- `[forum: …]`: players' reports (Steam discussions, Nexus Mods comments), re-read but never tested.
- `[inferred]`: reasoned, not observed.
- `[unverified]`: could not be checked.

## 1. The short answer

- Possible for single-player games, with the game restarted for every run: Unreal Engine reads its config files at
  startup [verified: Epic's "Configuration Files" docs].
- For FINAL FANTASY VII REBIRTH the ini reaches four menu options only: Background and Character Model Detail, Texture
  Resolution and Shadow Quality. The DLSS level, anti-aliasing and the frame cap live elsewhere, and whether an edit
  holds at the next launch is untested (section 2).
- lag-doctor already has most of the measuring half (section 6). It records for a fixed length from code, with GPU heat
  and clocks per run and the conditions of each run, and since #26 the game-side files on disk at each recording.
  What that half lacks is frame timing for many launches without a UAC prompt each: until the owner joins Performance
  Log Users (untested for an administrator's account, E2) or Stage 4's capture service exists, every run asks once.
- The other half is missing: launching through the store, reaching the same scene in every run, ending the game,
  applying each variant reversibly, and ranking runs against their own noise.
- Reaching the same scene is the hard part. Few games start their benchmark from the command line, and current
  benchmark harnesses drive menus with OCR and key presses (section 3). FINAL FANTASY VII REBIRTH has no benchmark at
  all (section 2).
- Noise sets the run count, and it is not measured yet. If runs spread by 1 to 3 %, as Gamers Nexus's fixed scenes
  did, a 3 % difference in average FPS needs about 3 to 16 runs per setting; 1 % lows need several times more
  (section 4).
- Numbers cannot rank image quality, so a sweep reports what each setting costs and the player chooses.

## 2. FINAL FANTASY VII REBIRTH

**No benchmark.**
- No built-in benchmark mode and no standalone Square Enix benchmark [verified: seasonedgaming.com PC review, 30 Jan
  2025; the Steam store page].
- Reviewers play a route of their own: PC Gamer outside Kalm with a few battles; Seasoned Gaming the opening, Kalm and
  the Grasslands, Junon and Mount Nibel; TechSpot and Hardware Unboxed several minutes of play [verified: pcgamer.com
  best-settings analysis; seasonedgaming.com; techspot.com/review/3189].
- None of them reports how many runs it made or how far they varied [verified: the same pages].

**Where its settings live.**
- Config: `%USERPROFILE%\Documents\My Games\FINAL FANTASY VII REBIRTH\Saved\Config\WindowsNoEditor\`, holding
  `GameUserSettings.ini` (plain text). Saves: `...\FINAL FANTASY VII REBIRTH\Steam\<Steam user id>\` [verified:
  PCGamingWiki; pupuweb.com]. Documents may sit under OneDrive; `lagdoctor/winapi/folders.py` (#26) finds it.
- Read on a real install: the file has two sections [verified: the local read, 2026-11-08].
  - `[ScalabilityGroups]`: `sg.ResolutionQuality`, `sg.ViewDistanceQuality`, `sg.AntiAliasingQuality`,
    `sg.ShadowQuality`, `sg.PostProcessQuality`, `sg.TextureQuality`, `sg.EffectsQuality`, `sg.FoliageQuality`,
    `sg.ShadingQuality`, `sg.SkeletalMeshQuality`, `sg.StaticMeshQuality`.
  - `[/Script/Engine.GameUserSettings]`: `bUseVSync`, `bUseDynamicResolution`, the resolution and its confirmed and
    desired copies, the fullscreen mode, `FrameRateLimit`, the hardware benchmark's results and HDR.
  - There is no `Engine.ini`.
- The resolution read 1280x720 and `FrameRateLimit` 0 [verified: the local read]. The menu offers no unlimited frame
  rate (30 to 120), so `FrameRateLimit` does not hold the menu's cap [verified: seasonedgaming.com]. One player says
  the file goes back to 1280x720 and the game reverts to 720p at launch unless the window mode is toggled each time
  [forum: Steam thread 720115720988454698, one post, no replies].
- A modder's map from menu options to engine variables [verified: the marcValdz/FF7-Rebirth-Optimized-Clarity README;
  Nexus Mods mod 38]:

| menu option | engine variable | in GameUserSettings.ini |
|---|---|---|
| Background Model Detail | `sg.StaticMeshQuality` | yes |
| Character Model Detail | `sg.SkeletalMeshQuality` | yes |
| Texture Resolution | `sg.TextureQuality` | yes |
| Shadow Quality | `sg.ShadowQuality` (0-3; Best and Extreme only by editing the ini) | yes |
| Dynamic Resolution Scaling max and min | `r.DynamicRes.MaxScreenPercentage`, `r.DynamicRes.MinScreenPercentage` | no (only the on/off `bUseDynamicResolution`) |
| Anti-aliasing (TAA, TAAU, DLSS) | `r.TemporalAA.Method` | no |
| Frame rate (30, 60, 90, 120) | `t.MaxFPS` | no (`FrameRateLimit` stays 0) |
| Display sync | `r.VSync` | no (`bUseVSync` is in the file; whether the menu writes it is untested) |
| Ocean detail, Fog quality | `r.Ocean.LevelShift`, `r.VolumetricFog.CheckerBoard` | no |

- Only Background Model Detail, Character Model Detail and Texture Resolution go through Unreal's scalability system.
  The game's own code sets the other options' variables at runtime, and many standard UE LOD and shadow variables do
  nothing in its custom renderer [verified: Nexus Mods mod 38].
- So the options kept outside the ini (resolution scale and with it the DLSS level, anti-aliasing, the frame cap, fog,
  ocean) most likely live in the binary save data that Steam Cloud syncs [inferred]. Square Enix's 1.001 notes imply
  that graphics settings travel with cloud saves [verified: Steam news 518578271369035851]. The save folder holds
  `ff7rebirthcommon.sav`, `ff7rebirthdevice.sav` and `steam_autocloud.vdf` [verified: the local read]. Which file holds
  which setting is untested.
- Players report hand edits back at the game's defaults after a session [forum: Nexus Mods mod 245, posts]; whether the
  game rewrites the file at launch, on a change or at exit is unknown. On the install read, the file was last written
  within a minute of the latest saves, which fits a write when the game exits [verified: the local read; inferred].
- FFVIIHook's author says the game resets many console variables when it loads [verified: Nexus Mods mod 4]. Whether an
  ini edit made with the game closed holds at the next launch is untested; the modder's route to Shadow Best and
  Extreme says it does for `sg.ShadowQuality` [verified: the README above].
- `Engine.ini` is read only through FFVIIHook, an `xinput1_3.dll` proxy in `End\Binaries\Win64` that also unlocks the
  console, with the variables under `[ConsoleVariables]` [verified: Nexus Mods mod 4; forum: Steam thread
  597390757221907584]. Other players report effects, good and bad, from an `Engine.ini` with a `[SystemSettings]`
  section; the thread does not say whether they had the hook [forum: Steam thread 597390757221912751]. A measured
  A/B with and without the hook would settle it.

**Settings worth sweeping.**
- Background Model Detail costs the most: Ultra to Low gained 17 % on an RTX 4090 (resolution not stated), while
  Effects cost about 2 % and Shadow Quality nothing notable [verified: pcoptimizedsettings.com].
- PC Gamer ranks Background Model Detail and Shadow Quality highest, and finds that Fog Quality and the number of
  characters shown matter too on weaker systems in some areas [verified: pcgamer.com].
- On 8 GB of VRAM, Texture Resolution High overfilled an RTX 3070 by Kalm in one modder's test, probably run with that
  mod's larger streaming pools [verified: Nexus Mods mod 38].
- Dynamic Resolution Scaling needs its minimum equal to its maximum, or the render scale follows the frame cap and runs
  stop comparing. 100, 66, 50 and 33 % roughly match native, DLSS Quality, Performance and Ultra Performance
  [verified: pcgamer.com]. The game runs DLSS 3.7's model E in every preset [verified: PCGamingWiki, checked
  2026-06-03]. DLSS frame generation needs an RTX 40 or 50 card [verified: nvidia.com].
- No reviewer's measurement of an RTX 30 laptop GPU in this game was found; TechPowerUp, Tom's Hardware, Notebookcheck
  and Digital Foundry were not checked [unverified]. CheckFPS labels some of its RTX 3070 Laptop figures as estimates
  yet claims real in-game FPS, and lists an Ultra preset the game lacks [verified: checkfps.io].

**Warm-up and clean runs.**
- A short shader precompile runs at first launch and again after game or driver updates: about 15 s on a 7800X3D with
  an RTX 4090. After an NVIDIA driver update, later areas still stuttered with the fresh cache [verified:
  seasonedgaming.com; pcgamer.com]. On an RTX 5070 with the standard driver, 1 % lows fell into single digits until
  SCSKiller precompiled the shaders [verified: techspot.com/review/3189].
- Whether changing a setting triggers new compiles is undocumented [unverified], so a sweep warms each configuration up
  once [inferred].
- Leave out the 30 FPS cinematics, the 60 FPS cutscenes and the slow stretch just after a save loads [verified:
  pcgamer.com]. Restart the game between runs: players report stutter growing over 20 to 60 minutes [forum: Steam
  thread 597392414383057353].
- The game keeps its own D3D shader cache in `Saved` (`D3DDriverByteCodeBlob_*.ushaderprecache`, 256 MiB on the
  install read) [verified: the local read]; players report that deleting it forces a rebuild [forum: the same thread].
  A sweep never touches it.

**Starting and ending it.**
- DirectX 12 only. No launch option is known to skip the intro or load a save; the only built-in fixed start is
  Chapter Selection, unlocked after finishing the game [verified: Steam threads; gameinformer.com; Nexus Mods searches].
- The menus are driven by keyboard or pad: Enter, Esc, Tab and the arrows cannot be rebound, and the menus show no
  mouse cursor [verified: PCGamingWiki].
- Players use Alt+F4 to get out of freezes and softlocks, and none of 51 posts mentions a quit dialog. Two say it left
  the game running, and others had to stop the game in Steam [forum: 51 Steam posts read]. A sweep confirms the exit
  by the process leaving the snapshot (pid and create time).
- The store page shows no anti-cheat notice [verified: the game's Steam store page].
- Update 1.005 (2026-06-03) added AMD FSR and Streamlined Progression, which includes unlimited HP [verified:
  rpgsite.net] and should make a game over during an unattended loop less likely [inferred].

## 3. Unattended benchmarks and the tools that run them

**Games that start their benchmark from the command line**, all from Phoronix Test Suite (PTS) profiles [verified:
github.com/phoronix-test-suite/test-profiles]:
- F1 22: `steam.exe -applaunch 1692250 -benchmark example_benchmark.xml`, settings in `hardware_settings_config.xml`,
  results in `My Games\F1 22\benchmark`.
- HITMAN 3: `-SKIP_LAUNCHER -ao START_BENCHMARK true -ao BENCHMARK_SCENE_INDEX 1 -ao AUTO_QUIT_ENGINE 120`, with
  settings as `ConsoleCmd settings_*` arguments, results in `%USERPROFILE%\hitman\profiledata.txt`.
- Cyberpunk 2077: its exe with `--launcher-skip -benchmark -skipStartScreen`, settings in `%LOCALAPPDATA%\CD Projekt
  Red\Cyberpunk 2077\UserSettings.json`, results in `benchmarkResults\*\summary.json`.
- Also: Ashes of the Singularity: Escalation (`-nolauncher -benchmark benchfinal`, through Proton), BioShock Infinite
  (`-unattended`), the Counter-Strike 2 timedemo (`+timedemoquit`, "flakey" by PTS 1.1.0) and Civilization VI
  (`-benchmark`, Linux build only).
- Whether these still work on current builds is unchecked.

**Most harnesses drive the menus instead.**
- LTT Labs' MarkBench harnesses (GPL-3.0, 28 games) start the game, mostly through Steam, find menu words by OCR, press
  keys and wait fixed times. Some (Shadow of the Tomb Raider, F1 25) then read the game's own results file. Each
  writes its start and end times to `report.json`, and a TOML flag switches PresentMon on. This covers RDR2, Total
  War: WARHAMMER III, and even HITMAN 3 and Cyberpunk 2077 [verified: github.com/LTTLabsOSS/markbench-tests].
- Their gotchas: turn Steam Cloud off, since its pop-up interrupts runs [verified: the harnesses' Shadow of the Tomb
  Raider README]; skip launchers and intro videos; dismiss warning dialogs by OCR [verified: the harness scripts].
- Hardware Busters' suite (.NET 9) runs its OCR, navigation and motion detection on a capture card's video of the
  screen, which is never used for frame rate; that comes from PresentMon 2.6.0 and RTSS. It does warm-up passes and
  rejects invalid runs [verified: hwbusters.com, 2026-09-01].

**How they handle noise.**
- PTS runs each test 3 times and adds runs while the standard deviation is over 3.5 %, up to twice the count, for runs
  of 20 minutes or less. It can ignore a run position, and its Shadow of the Tomb Raider profile ignores the first run
  since 1.0.1 [verified: phoronix-test-suite.md; openbenchmarking.org].
- CapFrameX (MIT, bundles PresentMon 2.6.0) pools the raw frame times of N runs. It flags a run whose chosen metric is
  more than a set percentage from the runs' median, and in its "Mark & replace" mode takes more runs until none is
  flagged [verified: github.com/CXWorld/CapFrameX; PCGH].
- PTS's F1 22 and HITMAN 3 wrappers poll for a results file with no timeout, and its test timeout needs PHP's PCNTL,
  which Windows lacks [verified: their scripts; phoronix-test-suite.md]. A hung game would wait forever.

**Choosing settings automatically.**
- NVIDIA's 2012 GeForce Experience FAQ: optimal settings target a 40 FPS average in demanding scenes. They come from
  lab tests across GPUs, CPUs and resolutions and are applied by editing the game's config files. Today's NVIDIA app
  page gives no method [verified: Guru3D's copy of the FAQ; nvidia.com].
- A Dell patent (US11413542B2) moves one setting a step at a time toward a target FPS and scores each run as FPS times
  the sum of the setting levels; image quality is never measured [verified: patents.google.com].
- Unreal's own auto-detect turns a CPU and a GPU performance index into quality levels (`RunHardwareBenchmark`,
  `PerfIndexThresholds_*`) [verified: Epic's API docs; tomlooman.com].
- No published Bayesian, genetic or reinforcement-learning search over a PC game's graphics settings was found
  [unverified: the search budget ran out before ACM and thesis searches].

**Steam launch.**
- `steam://run/<appid>//<args>/` exists, and since July 2013 Steam has asked the player to confirm arguments that
  arrive in a URL [verified: partner.steamgames.com ISteamApps; Steam news 11052]. A September 2017 update stopped
  showing extra arguments when a local third-party tool launches a game [verified: Steam news 32384], and a 2023 user
  says `steam://run` ignored the arguments [forum: Steam discussions]. What the current client does needs one test.
- `steam://rungameid/<appid>` takes no arguments [forum: Steam discussions].
- `steam.exe -applaunch <appid> <args>` is what PTS and MarkBench use unattended, and users report it avoids the prompt
  [verified: their scripts; forum: Steam discussions].
- Per-game launch options sit in `userdata\<id>\config\localconfig.vdf`. Of three community tools that edit it, one
  refuses to run while Steam is open, one applies its changes only after Steam closes, and one rewrites the file
  without checking [verified: the three tools' code].

## 4. Repeatability, heat and statistics

**How noisy runs are.**
- Gamers Nexus measured run-to-run standard deviations on average FPS in built-in or fixed scenes of mostly 0 to 1.5
  FPS, up to about 2.6 (Hellblade at 4K): roughly 0 to 3 % of the average at 50 to 100 FPS. Over 35 GTA V passes it
  was 0.1 FPS [verified: gamersnexus.net guides 3240 and 3487].
- 0.1 % lows were usually noisier; 1 % lows not always (Destiny 2: 0.7 FPS against 1.0 for the average). Manual play
  reached 3 to 4 FPS: Destiny 2's campaign gave 3.4, 2.9 and 3.9 on average, 1 % and 0.1 % [verified: gamersnexus.net
  guides 3240 and 3219].
- All of these are passes within one session, on desktops. The spread between relaunches of a game, which a settings
  sweep has, was not found [unverified]. In MPI benchmarking, launches of one program differed by up to about 11 %
  [verified: arXiv 1505.07734].
- Reviewers run 3 to 5 passes. PCGH takes the mean of three 20-second runs after 5 minutes of play; UL asks for at
  least 3 with the first dropped [verified: pcgameshardware.de; support.benchmarks.ul.com].

**First runs.**
- First runs were lower or noisier in the sources: Metro Exodus's first run sometimes came out 8 to 10 FPS lower, and
  FFXV's variance seemed to come from its first pass [verified: gamersnexus.net 3440, 3220]. None of them gives the
  cause. Shader or cache warm-up is a guess, and whether a settings change brings it back is undocumented.
- Compiled shaders are cached per game, GPU and driver, and a driver update invalidates them [verified: Microsoft's
  DirectX blog, August 2025]. Steam's shader pre-caching covered OpenGL and Vulkan when it shipped in 2017 [verified:
  gamingonlinux.com]. UE4 saves PSOs it missed into a local cache in `Saved` and merges it at the next start
  [verified: Epic's PSO-cache docs].
- Microsoft's Advanced Shader Delivery reached the Microsoft Store and the Xbox app on 2026-10-21 and names Steam and
  Epic as coming [verified: devblogs.microsoft.com/directx]. A sweep records whether it was in play.
- So: one discarded burn-in run for each new configuration, and a new baseline after any driver or game update
  [inferred].

**Heat.**
- On a 2022 ASUS TUF Gaming F15 (i7-12700H, RTX 3060 at 140 W), Notebookcheck measured Armoury Crate's Performance mode
  5 to 10 % below Turbo on the GPU; Silent caps CPU and GPU power harder [verified: notebookcheck.net review 609347].
- 10 to 15 minutes of stress usually exposes a laptop's throttling [verified: notebookcheck.net]. Over a 30-minute
  Cinebench loop, three thin laptops lost up to about 35 %: the MSI GS73VR 30 %, while the Dell Precision 5520 dipped
  from 710 to 460 [verified: notebookcheck.net, 2017]. 3DMark's stress test passes only when its worst loop is within
  97 % of its best [verified: benchmarks.ul.com].
- Intel's boost budget depends on how long the CPU idled: repeated Cinebench runs on a power-limited i9-10900K fell
  from 6,200 to 5,400 with 2-second pauses and to 5,700 with 30-second ones. In the two games ComputerBase tried,
  power stayed below the long-term limit, so the limits had no effect; that was a desktop [verified: computerbase.de,
  May 2020].
- So: one fixed mode for the whole sweep, warm up until the GPU's temperature and clock level off, and keep the pause
  between runs the same [inferred].

**Run order.**
- Running every repeat of one setting back to back puts any steady drift (heat, caches) into the difference
  [inferred]. In cloud benchmarks, single or back-to-back trials falsely reported a 38 % difference between identical
  systems at 95 % confidence; randomised multiple interleaved trials fixed it [verified: Abedi and Brecht, ICPE 2017].
- NIST adds control runs at the standard setting at the start and the end, and a time trend if they drift; it checks
  first that the measurement can see the change sought [verified: NIST/SEMATECH e-Handbook 5.2.1, 5.2.2].
- So: interleave the variants (A B C, then B C A, ...) with the baseline in every block, and measure the noise floor
  with identical runs (A/A) before trusting a difference [inferred].

**How many runs.**
- Per setting, n is about 15.7 × (CV / d)² at 5 % significance and 80 % power. A 1 % run-to-run spread and a 3 %
  difference need about 3 runs, 1.5 % about 4 to 6, 3 % about 16; a 4 % spread, as 1 % lows on a played route may
  have, needs about 28 [inferred from NIST's formula, e-Handbook 7.2.2.2]. The spreads are assumptions until measured.
- At 56 FPS a 60-second run holds about 3 frames in its 0.1 % tail, so 0.1 % lows and stutter counts are warnings, not
  ranking metrics; rank on average FPS and the 1 % percentile [inferred; verified: CapFrameX's metrics post].
- With 3 to 10 runs, use t intervals: bootstrap percentile intervals are too narrow at that size [verified: Hesterberg,
  arXiv 1411.5279]. Two alternatives can also be compared with a rank test on per-run results, as MPI benchmarking
  does [verified: arXiv 1505.07734].

**Many settings.**
- A full factorial needs 2^k runs, and NIST advises against it from 5 factors on [verified: e-Handbook 5.3.3.3].
- Changing one setting at a time from a baseline and keeping each change that helps captures most of the gain when
  noise is small against the effects [verified: Frey and Wang, Technometrics 2006]. Test only known pairs together,
  such as the upscaler and the resolution [inferred].
- Bayesian optimisation suits continuous spaces of under 20 dimensions [verified: Frazier, arXiv 1807.02811]; game
  menus are mostly short ordered lists.
- Each run that restarts the game costs minutes, so ten settings at five runs each take an evening or more [inferred].

**Image quality.**
- Frame numbers cannot judge it. Reviewers compare matched scenes by eye or by blind vote (ComputerBase's test drew
  6,747 votes), or in NVIDIA's ICAT viewer [verified: computerbase.de; nvidia.com/icat].
- Image metrics (FLIP, SSIM, LPIPS, ColorVideoVDP) need a pixel-aligned reference, and all but SSIM need numpy or
  PyTorch, outside lag-doctor's runtime [verified: the metrics' own repositories].

## 5. Driving a game from outside its process

**Keys.**
- `SendInput` adds keyboard and mouse events to the system's input stream, and they go to whichever window has the
  keyboard [verified: learn.microsoft.com SendInput; About Keyboard Input].
- When UIPI blocks it (the game runs at a higher integrity level), neither the return value nor GetLastError says so
  [verified: SendInput].
- Every injected key carries `LLKHF_INJECTED`, so a game can ignore it [verified: KBDLLHOOKSTRUCT].
- With `KEYEVENTF_SCANCODE` a key is sent as its hardware scan code [verified: KEYBDINPUT]; that games need this rests
  on one Stack Overflow comment [forum: Stack Overflow question 17295374].
- Gamepads cannot be injected this way, and ViGEmBus, the virtual-gamepad driver, was archived on 2023-11-02 [verified:
  INPUT; github.com/nefarius/ViGEmBus].
- Windows does not let a program take the foreground at will [verified: SetForegroundWindow].
- Whether FF7 Rebirth accepts injected keys is untested.

**Ending a game without a handle.**
- Posting `WM_CLOSE`, or `WM_SYSCOMMAND` with `SC_CLOSE` (the path Alt+F4 takes), asks the window to close; it can
  refuse, and Unreal games can veto it in `WindowCloseRequested` [verified: learn.microsoft.com WM_CLOSE and
  WM_SYSCOMMAND; Raymond Chen, 2014 and 2019; Epic's API docs].
- A post to a higher-integrity window fails with error 5 [verified: PostMessageW].
- Nothing ends a process without a handle to it, and lag-doctor never takes one.

**Knowing what the game shows.**
- The window's title (`GetWindowText` sends no message, so it cannot hang), PresentMon's cadence and `FrameType`, and
  per-process GPU use say whether the game draws. They cannot tell a menu from play [verified: learn.microsoft.com;
  PresentMon's README; inferred].
- Windows.Graphics.Capture can capture the game's window by its HWND (Windows 10 1903 and later, frames stamped in QPC
  units), and Windows.Media.Ocr can read the image [verified: learn.microsoft.com].
- It draws a yellow border unless the user consents to borderless capture, and asking needs a package capability that
  an unpackaged app like lag-doctor may not be able to declare [verified: IsBorderRequired; unverified for
  lag-doctor]. Its cost on the game's frames is unmeasured. One report links a visible cursor in a capture to a
  borderless game losing independent flip, which PresentMon's present mode would show [forum:
  microsoft/Windows.UI.Composition-Win32-Samples issue 128].
- Desktop duplication needs the adapter that drives the panel: on the owner's laptop the Intel GPU in its usual GPU
  mode (its MUX switch changes that path). Desktop duplication cannot see the secure desktop of a UAC prompt
  [verified: DuplicateOutput; AcquireNextFrame]; Windows.Graphics.Capture there is unchecked.

**Config files elsewhere.**
- Unreal reads its ini files at startup, the user's last [verified: Epic's configuration docs]. A console variable from
  `Engine.ini` `[SystemSettings]` outranks the in-game menu in UE5's priority order; UE4.26's is unchecked [verified:
  Epic's console-variables docs].
- Steam Cloud syncs a game's cloud files before and after each session, and Valve advises developers not to sync video
  settings [verified: partner.steamgames.com, Steam Cloud].
- Unity keeps PlayerPrefs in `HKCU\Software\<company>\<product>` [verified: docs.unity.com].
- Games can reset settings after a driver update, as Talos Principle 2 did [forum: Steam thread 3936769163103222016].

**DLL levers.**
- The NVIDIA driver's DLSS override (the NVIDIA app, or Profile Inspector, which needs administrator rights to write)
  changes no file in the game folder, though it does change which DLSS model runs in the game. Its upscaling models
  work on every RTX card and its frame-generation upgrades on RTX 40 and 50. NVIDIA suggests preset K on RTX 20 and 30,
  which lack native FP8 [verified: nvidia.com news, 2025-01-30 and 2026-01-14;
  github.com/Orbmu2k/nvidiaProfileInspector].
- DLSS DLL swaps (NVIDIA's own code in another version), OptiScaler, DLSSTweaks, Special K and ASI plugins all add or
  replace DLLs that the game loads into its own process [verified: the projects' repositories; inferred].
- OptiScaler loads as a proxy `dxgi.dll` (or another listed name) beside the shipping exe, adds FSR 3 or XeSS frame
  generation in DX12 only, is prone to crashes there, and lists FF7 Rebirth as working [verified:
  github.com/optiscaler/OptiScaler wiki]. XeSS frame generation should run on an RTX 30 card, since Intel names any
  GPU with Shader Model 6.4 [inferred from github.com/intel/xess]; FSR 3's on RTX 30 is unverified.
- DXVK does not apply to D3D12 games [verified: github.com/doitsujin/dxvk].
- A Steam update or file verification restores replaced shipped files; an added proxy DLL probably survives
  [inferred].

## 6. What lag-doctor has and lacks (main at 8a4780a)

**Has:**
- A fixed-length recording from code, `Session(target, SessionOptions(duration_s=...)).run()` as `scripts/e2e.py` does,
  with `request_stop` for an early stop and `CaptureLock` for one recording at a time.
- Saving and analysing a run without repointing `lagdoctor fix`: `cli.analyze_and_report(remember=False)` is the
  default, as `scripts/e2e.py` uses it; only `watch` remembers.
- `windows.foreground_pid()`, a handle-free check that the game's window is in front, and the `game_focused` column.
- The conditions to hold fixed per run: power plan and mode, refresh rate, the NVIDIA power limit, NVML temperature,
  clocks and throttle share, and other programs using the game's GPU (`gpu_contention`).
- `analysis.window.window()` with `frames.frame_stats()`, which give a time window's stats.
- The fix journal (`fixes/base.py`, `fixes/journal.py`). Its plan, journal-first write, readback, undo and heal fit two
  new kinds of change: one ini key edited in place (as `gpu_preference` edits one string field), and a mod's files
  moved aside and back, checked by hash. Limits:
  - the journal stores values, not file bytes;
  - a move of several files needs per-file state, so that heal can settle a half-done move;
  - `os.replace` is atomic only within one volume, and the aside folder must not be one a mod loader scans;
  - a game under Program Files may need rights that fixes never take, and fixes refuse to run elevated.
- Compare's game-side files (#26): drop-in DLLs, `.asi` plugins, Unreal ini files and `~mods` paks, by SHA-256 up to
  32 MB each within 256 MB and 5 s per recording; larger or later files, and OneDrive files kept online only, are
  compared by size and date. So a run records which variant was on disk.
- LagLab and `scripts/e2e.py`: a fake game with known answers, to build and test a sweep runner without a real game.
  LagLab takes its settings on the command line only.

**Lacks:**
- M6's store launch and handle-free wait for the game, and M5's start key and `crop` (`docs/LAUNCHER-PLAN.md`).
- Frame timing for N launches without N UAC prompts. The relay is approved for one game process (`--game
  pid:create_time`) with one start and one stop, and each relaunch is a new process. A member of Performance Log Users
  records frame timing with no prompt, without the network tracker that an offline sweep does not need. Whether that
  holds for an administrator's filtered token is E2 in `docs/INSTALLER-PLAN.md`, not yet run. Stage 4's capture service
  is the other way.
- A Compare for N runs of K variants with their spread: today's marks the better side on any raw difference.
- Labels for each run's variant, and rates over the window instead of the capture's length (`Capture.duration_s`
  includes the stop time).

**Rules it meets** (owner decisions, none taken yet):
- In-game settings are advice only (DESIGN.md, Fixes).
- Never touch the game. DESIGN.md's principle 1 has no exception for mods the owner installs; a sweep would never
  install or download one, and at most would set aside and put back files the owner installed [inferred].
- Nothing kills a game and nothing types on the owner's PC (CLAUDE.md). Reaching the scene and ending the game stay
  the player's, unless the owner allows key presses and a close request.
- An agent never runs a live sweep. In the agent's shell, watch, fix and undo refuse; a config edit under
  `%LOCALAPPDATA%` would land in another app's private copy, and one under Documents (FF7 Rebirth's config) would
  change the owner's real file.

## 7. What a sweep would have to do [inferred from sections 2 to 6]

1. Refuse without a fixed power mode and mains power, and record the driver and game versions; a change in either
   ends the sweep's baseline.
2. Apply each variant through the journal with the game closed; check the files' hashes before each run; undo
   everything at the end, and on any failure.
3. Start the game through the store, wait for it handle-free, and discard a burn-in run for each new configuration.
4. Reach the same scene each time: the game's own benchmark, the player at a save and spot pressing the start key, or
   key presses sent only while the game's window is in front.
5. Record a fixed window once the GPU's temperature and clock have levelled off; keep the pauses between runs equal.
6. Interleave the variants in blocks, the baseline in each, after a first A/A block that measures the noise.
7. Reject runs whose conditions moved (throttling, another program on the GPU, the game behind another window).
8. Rank on average FPS and the 1 % percentile with t intervals, say "no measurable difference" inside the noise, and
   leave image quality to the player.

## 8. Open questions, to test on a real install

1. With the game closed, does an edited `sg.StaticMeshQuality` in `GameUserSettings.ini` show in the menu and change
   the frame rate at the next launch, and does the game keep it?
2. Which save file holds the resolution scale, the anti-aliasing method and the frame cap, and does Steam Cloud bring
   them back?
3. How far apart are two recordings of the same save, spot and settings on this laptop (A/A)? That number sets the
   repeats.
4. Does the game accept `SendInput` keys at its title screen, and what does a posted `WM_CLOSE` do?
5. Does `steam.exe -applaunch <appid> <args>` prompt on the current Steam client?
6. What does a Windows.Graphics.Capture of the game's window cost in frame time and present mode?

## Verification log (2026-11-08)

A separate fact-checker re-read each web track's claims at their sources:

| track | claims | confirmed | corrected | unverifiable |
|---|---|---|---|---|
| FINAL FANTASY VII REBIRTH | 57 | 42 | 12 | 3 |
| Unattended benchmarks and tools | 66 | 56 | 10 | 0 |
| Repeatability, heat and statistics | 60 | 42 | 18 | 0 |
| Input, config files and DLL levers | 88 | 77 | 11 | 0 |
| total | 271 | 217 | 51 | 3 |

The three unverifiable claims are inferences, marked so above: where the menu settings live, whether a setting change
recompiles shaders, and how mods meet the never-touch rule. A second reviewer then checked this note against the
fact-checked claims and found 38 problems (5 that changed a conclusion, 24 wrong details, 9 tags), all applied.
Corrections that changed a conclusion:

| as first written | corrected to |
|---|---|
| Players see GameUserSettings.ini stay at 720p | One post with no replies; it says the game itself reverts to 720p at launch |
| TechSpot watched the lows because of the 120 FPS cap | The lows were its focus anyway; the cap was no concern; its run count is not stated |
| On NVIDIA the startup precompile misses pipelines | TechSpot does not say so: that is PCGH's reading of Wccftech's test, and Seasoned Gaming's guess. TechSpot tested one NVIDIA GPU on one driver, and its Radeon also stuttered in its 0.1 % lows |
| FFVIIHook breaks with every patch (a July 2026 report) | That report was traced to vJoy; the hook did need a June 2026 update for 1.005 |
| 1 % lows are noisier than averages | Not in GN's own Destiny 2 data (0.7 against 1.0 FPS); usually true for 0.1 % lows |
| Steam's D3D12 games cannot get precompiled shaders | Advanced Shader Delivery reached the Microsoft Store on 2026-10-21; Steam and Epic are named as coming |
| One relay session can span many runs | The relay takes one start and one stop, for one approved game process |
| Desktop duplication knocks borderless games off independent flip | The report blamed a visible cursor in a capture, on one setup |
| Thin laptops lost 12 to 36 % (Notebookcheck's percentages) | The article's own scores give about 11 to 35 % |
| (this note) 4 to 6 runs per setting see a 3 % difference | Only at an assumed 1.5 % spread; GN's fixed scenes spread 0 to 3 %, so 3 to 16 runs |
| (this note) First runs are slow after a settings change | The sources show slow first runs but give no cause; a settings change's effect is undocumented |
| (this note) The ini lever covers the game's settings | It reaches four menu options; the DLSS level, anti-aliasing and frame cap live elsewhere |
| (this note) The measuring half exists | It still needs one UAC prompt per run, until Performance Log Users (E2) or Stage 4 |
