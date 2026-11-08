# Installing lag-doctor on other people's PCs: ETW rights and distribution

Research date: 2026-11-06. Question: how can other people install lag-doctor so that frame timing (PresentMon) and
the network tracker (`lag-doctor-net`) work with as little input from them as possible? Method: a research workflow
(5 sourced sweeps, 3 designs under different lenses, 1 judge), then every claim the recommendation rests on re-read
from its source by a separate reader (verification log at the end). Measured on this PC: Windows 11 Home (EditionID
`Core`) 25H2 build 26200.9711, UAC `TypeOfAdminApprovalMode` = 1 (classic Admin Approval Mode), an administrator
account with a UAC-filtered token, Performance Log Users with 0 members.

**Tags.** `[verified: …]` = re-read at that source in the verification pass, or observed by the local command named.
`[research: …]` = found at that source by a research sweep, not re-read since. `[inferred]` = reasoned, not
observed. `[unverified]` = could not be checked.

## 1. The floor: one administrator approval, once

- Only administrators, members of Performance Log Users, and services running as LocalSystem, LocalService or
  NetworkService can control event tracing sessions [verified: StartTraceW, also in `network-and-etw.md` 5.7]. So every
  route needs an administrator's approval once (a UAC click, or an administrator's password on a standard account).
  A tool that needs none on a fresh PC would be using a vulnerability [inferred].
- The default ETW security descriptor (GUID `0811c1af-7a07-4a06-82ed-869455cdf713`, used by every provider and
  session without one of its own) is
  `O:BAG:BAD:(A;;0x800;;;WD)(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)(A;;0x120fff;;;NS)(A;;0x120fff;;;BA)(A;;0xee5;;;LU)(A;;LC;;;MU)(A;;0x800;;;AC)`:
  full access for SYSTEM, LocalService, NetworkService and Administrators; Performance Log Users get `0xee5` = Query,
  Notification, CreateRealtime, CreateOndisk, Enable, LogEvent, AccessRealtime, RegisterGuids [verified: Sergey
  Babkin's Microsoft blog, "Security descriptors, part3"]. A 2025 dump shows `0x1800` for WD and AC on newer builds
  [research: Jonathan Johnson's dump, via the etw-rights sweep].
- **E1, measured on this PC (build 26200, 2026-11-06, `scripts/etw_access.py`, run by the owner; the elevated copy
  read the descriptors, the normal window judged them with Windows' Authz access check).** Microsoft-Windows-
  Kernel-Network (the game-server tracker) and Microsoft-Windows-Kernel-Process each have a descriptor of their own:
  `O:BAG:BAD:(A;;0x120fff;;;SY)(A;;LO;;;LS)(A;;0x1207ff;;;BA)`. SYSTEM and Administrators get full rights,
  LocalService exactly TRACELOG_GUID_ENABLE (`LO` = 0x80), and Performance Log Users nothing. Every other provider
  PresentMon needs has none, so the default applies, read here as
  `O:BAG:BAD:(A;;0x1800;;;WD)(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)(A;;0x120fff;;;NS)(A;;0x120fff;;;BA)(A;;0xee5;;;LU)(A;;LC;;;MU)(A;;0x1800;;;AC)(A;;0x1800;;;<a capability SID>)`
  (the 0x1800 of the 2025 dump above). The verdicts: as a member of Performance Log Users the owner would get frame
  timing, without process start and stop events, and **not** the game-server tracker; a LocalService service would
  get both, frame timing and the tracker [verified: the probe's output and its saved descriptors; the LocalService
  verdict uses a modelled token, so E3 confirms it with a real one]. E3 did, on a GitHub runner (section 8): a real
  LocalService token enables every one of them, and E1 on that runner (Windows Server 2025, build 26100) read the
  same descriptors [verified: section 8]. This agrees with the third-party reading of
  Kernel-Process on build 26300 [research: github.com/CrankyAnt/SharedWorkflows/issues/30]. PresentMon 2.6.0
  tolerates ERROR_ACCESS_DENIED on Kernel-Process only; any other provider failing aborts the session [research:
  `PresentData/PresentMonTraceSession.cpp` at v2.6.0].
- Group membership is in the logon token, so a new member needs a new sign-in [research: Microsoft "Access tokens"];
  `klist purge` only drops Kerberos tickets and does not help [research: Microsoft klist doc].
- Performance Log Users is not on the list of groups UAC filters out of an administrator's standard token, so it
  should stay enabled unelevated [research: 2007 MSDN Magazine and TechNet Magazine UAC articles; inferred for
  Windows 11; E2]. setup's "After that, frame timing needs no prompt" rests on this and on E1 (no provider
  PresentMon needs refuses the group); for the game-server tracker setup says it still needs `--elevate`. If this
  premise fails, `elevation.trace_rights()` reads a group that is in the token but not enabled (deny-only) as
  `filtered`, and setup says each recording asks for one UAC prompt instead of asking for a sign-out, which would
  not help there [inferred: a new sign-in gets the same filtered token].
- The group is broader than lag-doctor needs: a member controls every session under the default descriptor, and
  membership was the trigger condition in the proof of concept for CVE-2025-47985 (ETW, elevation to SYSTEM, published
  and patched 2025-07-08) [verified: STAR Labs advisory 25-47985]. Visual Studio's installer adds interactive users to
  the group [verified: same advisory].

## 2. How other tools do it

| Tool | Mechanism | Source |
|---|---|---|
| Xbox Game Bar FPS counter | "Request access" adds the account to Performance Log Users (an administrator account, or one an administrator added), then a restart | [verified: Microsoft Answers thread quoting the support.xbox.com Performance page, which renders by script] |
| Intel PresentMon app | MSI installs `PresentMonSharedService` ("PresentMon Shared Service"), `Start="auto"`, `Type="ownProcess"`, no `Account` attribute, so LocalSystem | [verified: `IntelPresentMon/PMInstallerLib/Library.wxs` on main; MSI ServiceInstall table: null StartName means LocalSystem] |
| NVIDIA FrameView SDK | service `FvSvc`, LocalSystem, demand start, under Program Files, with NVIDIA's own PresentMon build | [verified: `sc.exe qc FvSvc` on this PC] |
| AMD OCAT | `requireAdministrator` on every launch | [research: OCAT `Frontend/app.manifest`] |

Vendors take one approval at install for a privileged service; Microsoft's own Game Bar takes one approval for a
group join plus a restart.

## 3. Options for lag-doctor

| Option | Approvals at install | Sign-out | Per recording | Standing capability | Verdict |
|---|---|---|---|---|---|
| A. Signed per-machine MSI with a capture service | 1 | no | nothing | an interactive user can ask the service, over a locked pipe, to trace a process in their own session | recommended end state |
| B. Signed per-machine MSI alone (code and PresentMon under Program Files) | 1 | no | 1 UAC | none | build first: the vehicle A needs |
| C. Performance Log Users from setup's printed steps (today) | 1 admin command | yes | nothing for frame timing, if E2 passes; the game-server tracker still needs 1 UAC (E1) | the account, and all it runs, controls every default-descriptor ETW session | keep as the explained opt-in |
| D. lag-doctor runs the group join itself (Game Bar style) | 1 UAC | yes | nothing after the sign-out | as C | only if the "never changes group membership" decision is reversed |
| E. Per-recording UAC (today's `--elevate`) | none | no | 1 UAC, forever | none between runs; elevates user-writable code from a checkout | keep as the fallback for git checkouts |
| F. Intel's service through PresentMonAPI2 | 1 (Intel's MSI) | no | frames: nothing; network: still needs rights | Intel's resident LocalSystem service, its pipe open to all authenticated users at v2.6.0 | reject: frames only, API coupling, a 169,672,704-byte MSI [research] |
| G. Elevated scheduled task | 1 | no | nothing | a no-prompt elevation path, the UAC-bypass pattern Administrator protection is built to remove | reject [research: Microsoft's Administrator protection introduction] |
| H. MSIX package | 1 | no | depends | n/a | reject: AppData virtualization is the private-copy failure DESIGN.md describes |
| I. Frozen exe (PyInstaller, Nuitka) | 0 | no | 1 UAC | none | reject: antivirus false positives, which PyInstaller's maintainers say they cannot fix and for which Nuitka points to its paid plan [research] |
| J. `uv tool install` from PyPI | 0 | no | 1 UAC | none | developer channel only: unsigned uv-managed Python [research] |

Estimates from the designs, unsourced: B about 1.5 to 2.5 weeks, A about 2 to 4 weeks on top of B, D about 1 to 2 days.

## 4. Constraints for the capture service (option A)

- **Account**: NT AUTHORITY\LocalService (LocalSystem only if E3 shows a provider refusing it). LocalService holds
  SeImpersonatePrivilege by default, the privilege "potato" attacks turn into SYSTEM [verified: Microsoft "Impersonate
  a client after authentication"; research: potato write-ups]. Trim it: with SERVICE_CONFIG_REQUIRED_PRIVILEGES_INFO
  the SCM removes every unlisted privilege when the process starts, and never removes SeChangeNotifyPrivilege
  [verified: SERVICE_REQUIRED_PRIVILEGES_INFOW]. ETW control is an ACL check, not a privilege, so the trim should
  not stop it [research: etw-rights sweep; E3]. Add a restricted service SID, but it is untested with ETW until E3
  runs with it: it puts the service SID, World, the service logon SID and the write-restricted SID S-1-5-33 on the
  token's restricted SID list [verified: SERVICE_SID_INFO], and the default descriptor above grants World only
  RegisterGuids (`0x800`; `0x1800` adds JoinGroup) [verified: SDK `wmistr.h`]. Measured (E3, section 8): trace
  control does not pass that second list. A write-restricted service token gets ERROR_ACCESS_DENIED from
  `StartTraceW` for every session and cannot create PresentMon's message-only window, so the service gets an
  unrestricted service SID (DECISIONS 2026-11-07); its privileges are still trimmed.
- **Start on demand**: trigger type SERVICE_TRIGGER_TYPE_NETWORK_ENDPOINT with subtype NAMED_PIPE_EVENT_GUID
  `1F81D131-3FAC-4537-9E0C-7E7B0C2F4B55` starts the service "when a request is made to open the named pipe", and is
  "commonly used to start a service that has stopped itself after an idle time-out" [verified: SERVICE_TRIGGER].
  Measured (E3b, section 8): the trigger takes the bare pipe name, starts the service in about 0.1 s, and the
  client's first open fails with ERROR_PIPE_BUSY meanwhile, so the CLI retries with a deadline.
  Fallback: grant interactive users SERVICE_START (`RP` for `IU` in the service's SDDL); by default local users can
  query a service but not start it [research: Microsoft service security and access rights]. Never grant users
  change-config or stop.
- **Pipe**: explicit DACL, PIPE_REJECT_REMOTE_CLIENTS, FILE_FLAG_FIRST_PIPE_INSTANCE against squatting; the CLI
  connects at identification level (SECURITY_SQOS_PRESENT) so a squatting server cannot impersonate it; read the
  pipe's real descriptor back in a test [research: service-design sweep].
- **Same-session check without touching the game**: ProcessIdToSessionId needs PROCESS_QUERY_INFORMATION on the
  target [verified: its Learn page], which would open the game. Use the session id from `winapi/ntquery.py`'s
  handle-free snapshot and compare it with the pipe client's session [inferred; to verify].
- **Relay only what was asked**: one recording at a time; only the requested PID's PresentMon rows and that PID's
  Kernel-Network events (the provider is system-wide) [inferred].
- **No user-writable code**: only Program Files binaries, the bundled PresentMon (never `%LOCALAPPDATA%` or a winget
  portable folder), DLLs by absolute path, no file writes.
- **The game is another account's process to LocalService**: its name shows as `<unknown>`; targeting by PID works
  [verified: `IsTargetProcess` in `PresentMon/OutputThread.cpp` at v2.6.0 compares the PID alone; measured in E3,
  section 8]. The capture ends about 0.6 s after the game exits, stopped by lag-doctor's own watch: PresentMon's
  `--terminate_on_proc_exit` cannot fire without the handle it could not get [verified: E3, section 8].

## 5. Shipping

- **Installer**: WiX v7.0.1, released 2026-10-26; its .NET tool is the NuGet package `wix` 7.0.1 (.NET 8 or later,
  4,586,487 bytes, SHA-512 `9c72a17d…17b37d70`) [verified: nuget.org's catalog entry, re-read 2026-11-08]. Its EULA,
  `OSMFEULA.txt` in the repository at v7.0.1, is the Open Source Maintenance Fee template v1.1, read in full on
  2026-11-08: a fee is owed only by a user whose use is part of revenue-generating activities and whose gross revenue
  reaches US$10,000 a year; the fee "is not a license fee"; not paying may suspend access to WiX's binaries; WiX's
  MS-RL licence governs where the two conflict; nothing in it binds the MSIs built or the people who install them,
  and it has no governing-law, audit or telemetry clause [verified]. Every `wix` command but help, version and eula
  refuses (WIX7015) until the run passes `-acceptEula wix7` or a remembered `wix eula accept wix7` (a file in the
  user's profile) [verified: WiX source at v7.0.1, `Program.cs`, `CommandLine.cs`]. The owner accepted it for CI's
  builds (DECISIONS 2026-11-08). WiX v5 (MS-RL only) has had no security fixes since 2026-02-05 [research:
  FireGiant's lifecycle page].
- **winget**: the InstallerUrl must be the publisher's own release location (GitHub Releases qualifies), with its
  SHA-256 and a silent install; the policies require no signature [research: Windows Package Manager policies].
  `ElevationRequirement: elevationRequired` makes winget raise the UAC prompt itself [research: manifest schema
  1.12.0]. winget re-marks a hash-matched installer from a trusted source as trusted, so no SmartScreen download prompt
  [research: winget-cli `DownloadFlow.cpp`]. Open bug microsoft/winget-cli#6173 (April 2026, winget 1.28): an MSI
  started after UAC from an unelevated terminal failed with "no app associated", so test from both kinds of shell
  [research].
- **Signing**: Microsoft's Artifact Signing (formerly Trusted Signing) gives individuals a public-trust identity only in
  the United States or Canada; organisations qualify in more countries [verified: Artifact Signing quickstart, updated
  2026-10-08]. Its price, about US$9.99 a month, is not on that page [research]. A Microsoft Q&A claim that individual
  onboarding is paused comes from an "AI answer" [verified as such; the claim itself unverified, not used]. SignPath
  Foundation signs open-source projects free of charge, under its own name [verified: signpath.org]; its conditions
  (an OSI licence, an existing release, a verifiable build and more) are listed under "Decided" below [verified:
  signpath.org/terms]. It signs only binaries built from the project's own source, lets upstream open-source binaries
  ride inside a signed MSI (verified, not re-signed), holds every signed file to the project's product name and one
  version, and signs PE files inside an MSI and the MSI itself in one request [verified: signpath.org/terms;
  docs.signpath.io artifact configuration, 2026-11-08]. An OV certificate costs about US$150 to 300 a year and
  needs a hardware token or HSM [research: Microsoft code-signing options].
- **Smart App Control** blocks "unknown, unsigned code" by default, and "there is currently no way to bypass" it for
  individual apps [verified: Smart App Control overview and support FAQ]. Recent updates let it be re-enabled without
  a clean install, except when optional diagnostic data is off [verified: support FAQ]. It is off on this PC
  [research: `VerifiedAndReputablePolicyState` = 0]. Signed: python.org's `python.exe` and stdlib `.pyd` files (PSF),
  PresentMon (Intel). Unsigned: uv-managed `python.exe`, `uv.exe`, psutil's `.pyd` [research: Get-AuthenticodeSignature
  here]. python.org's embeddable ZIP (3.13.17): every one of its 30 `.exe`, `.dll` and `.pyd` files is signed, 28
  by the PSF and the two `vcruntime140` DLLs by Microsoft [verified: E3c, section 8]. Python scripts run by a
  trusted `python.exe` are not checked; native `.pyd` and `.dll` files are [research: App Control script enforcement].
  Batch files are not controlled either, only what they run [verified: Microsoft Learn, App Control script
  enforcement, 2026-10-28], so the MSI's `lagdoctor.cmd` gives Smart App Control nothing to check but the signed
  `python.exe`.
- **Administrator protection** is off by default and available with KB5120998; where an organisation uses the Windows
  security baselines it is on by default from the October 2026 update. Elevated processes then run under "a hidden,
  system-generated, profile-separated user account", and every admin operation needs Windows Hello [verified:
  Administrator protection overview]. Microsoft advises a services-based design for work that must run elevated
  [research: Windows Developer blog, 2025-05-19]. `--elevate` children would run in that separate profile (E5).

## 6. Experiments before building

| Id | What | Gates |
|---|---|---|
| E1 | Elevated `EventAccessQuery` dump as SDDL: Kernel-Network `{7DD42A49-5329-4832-8DFD-43D979153A88}`, Kernel-Process `{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}`, PresentMon's other providers, the default GUID. Read-only, one UAC. Written as `scripts/etw_access.py`: the elevated copy reads the descriptors, and the unelevated window judges them with Windows' own access check (Authz) for "you, after joining Performance Log Users", a modelled LocalService token and a modelled SYSTEM token. **Done 2026-11-06 (section 1): the group gets frame timing, not the tracker; LocalService gets both** | setup's wording (now measured); the service account |
| E2 | Join Performance Log Users, sign out and in: `whoami /groups` shows it enabled unelevated, and an unelevated `watch` records frames and network flows. If it shows deny-only instead, setup already handles it (`trace_rights()` answers `filtered`: one UAC prompt per recording, no sign-out asked for) | option C's "no prompts" |
| E3 | Throwaway LocalService service with privileges trimmed to SeChangeNotifyPrivilege and a restricted service SID (both read back from the running token) on a VM: every PresentMon provider enables, rows arrive for the game's PID, Kernel-Network events arrive, the capture ends when the game exits. **Done 2026-11-07 on a GitHub runner (section 8): with a restricted service SID no session starts at all; unrestricted, every provider enables, Kernel-Network events arrive, presents are seen by PID and the capture ends 0.55 s after the game exits. Rows with display tracking need a PC with a GPU, which the runner is not** | option A's account |
| E3b | Named-pipe trigger on Windows 11 Home 25H2 and Windows 10 22H2: start, idle self-stop, restart, start latency. **Done 2026-11-07 on Windows Server 2025 (section 8): start in 0.08 to 0.12 s, idle self-stop, restart, three cycles; Home 25H2 and Windows 10 22H2 not measured** | option A's start mode |
| E3c | python.org's embeddable runtime as an SCM service from Program Files: `sys.path` holds only Program Files entries, every `.exe`/`.pyd`/`.dll` signed. **Done 2026-11-07 (section 8): it runs as a service through ctypes, `sys.path` holds its own folder and ZIP only, all 30 binaries signed** | Python vs a small native helper |
| E5 | Administrator protection on: `--elevate`'s waitable child, the shared capture mutex, the capture landing in the user's folder | the public `--elevate` fallback |
| E6 | Smart App Control enforcing VM: the MSI layout runs, inventory's PowerShell child returns JSON in Constrained Language Mode, Defender scan clean | the public release |
| E7 | winget install, upgrade, uninstall from elevated, unelevated and standard-account shells, and on Windows 10; uninstall removes the service. **MSI part done 2026-11-08 (section 9): the MSI installs, runs from a fresh shell and uninstalls with nothing left, from an elevated shell that never prompts, on Windows Server 2025; winget, unelevated and standard-account shells, an upgrade, and Windows 10 not yet** | the public release |
| E8 | Red-first tests of each service guard: another session refused, a squatted pipe fails loudly, a PID in another session refused, no other process's network events relayed, nothing loaded from a user-writable path | shipping the service |

## 7. Going public

- Public at https://github.com/Ali0600/lag-doctor since 2026-11-07. It is MIT-licensed, as PresentMon is: `LICENSE`
  and `license` in `pyproject.toml` were added on 2026-11-06.
- Scrubbed on 2026-11-07, before the first push, from the tree and, by a history rewrite the owner chose, from
  every commit (DECISIONS 2026-11-07): this PC's user name in paths, the owner's machine-learning job, this PC's
  adapter and power-plan GUIDs, the home network's addresses, other software named as installed here, and the
  Steam log's dates, times and AppIDs. No commit holds a SID, the PC name, a public IP, a real MAC address, a Wi-Fi
  name or a serial, and the only binary is the synthetic report picture [verified: a scan of every blob, message,
  author field and path; a five-angle review and a recheck that searched for this PC's own identifiers; the picture
  viewed]. Commits are authored with a GitHub noreply address [verified: `git log`].

## 8. The service experiments on GitHub's runners (E1, E3, E3b, E3c; 2026-11-07)

Run by `.github/workflows/experiments.yml` on `windows-2025` runners (`scripts/stage2`; three runs, 42398897435,
42399952827 and 42400758567; the numbers below are the last run's unless said otherwise, and every result file is
in each run's `stage2-results` artifact). The runner: Windows Server 2025 Datacenter 24H2, build 26100.33674 (image
`win25-vs2026` 20261103.298.1), no GPU; the job runs as the built-in Administrator (`runneradmin`, RID 500),
elevated, with UAC on (`EnableLUA` 1) but never prompting (`ConsentPromptBehaviorAdmin` 0), in session 2, where
`dwm.exe` runs [verified: `facts.json`]. The folder the MSI would install was built under
`C:\Program Files\lagdoctor-stage2`: python.org's embeddable 3.13.17 (`python-3.13.17-embed-amd64.zip`, SHA-256
`9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38`, 11,441,267 bytes; 3.13.18 is source-only),
lag-doctor's code, psutil 7.2.2 and the pinned PresentMon, verified again there [verified: `layout.json`]. Each
service was created with `sc.exe` (`type= own start= demand obj= NT AUTHORITY\LocalService`,
`privs SeChangeNotifyPrivilege`, `sidtype restricted` or `unrestricted`) and read back with `sc qc`, `qprivs`,
`qsidtype` and `showsid`; the token facts below come from the running service's own token (`GetTokenInformation`
in `scripts/stage2/tokeninfo.py`), never from its configuration. A WARP-rendered Direct3D 11 window
(`scripts/stage2/standin.py`: 640x480, flip-model swap chain, about 60 presents a second on the Microsoft Basic
Render Driver, one DNS query a second over UDP) stood in for the game, in the job's own session.

- **E1 on the runner.** Kernel-Network and Kernel-Process carry the laptop's descriptor byte for byte
  (`O:BAG:BAD:(A;;0x120fff;;;SY)(A;;LO;;;LS)(A;;0x1207ff;;;BA)`), the default descriptor is the laptop's too
  (its last entry a capability SID, which differs by machine), the same three rows have descriptors of their own,
  and the modelled verdicts are the laptop's [verified: `e1.json`, `e1-descriptors.json`; build 26100 against the
  laptop's 26200].
- **E3c, the runtime.** The SCM ran `python.exe service_host.py` as a service through `StartServiceCtrlDispatcherW`,
  `RegisterServiceCtrlHandlerExW` and `SetServiceStatus` from ctypes (`scripts/stage2/service_host.py`); it reported
  running and stopped with its own exit code. Read inside the service: `sys.path` at start was exactly
  `<install>\python313.zip` and `<install>`, the script's own folder not among them, `site` not imported, and
  `sys.flags` had `isolated`, `ignore_environment`, `no_site` and `safe_path` set, from the shipped `python313._pth`
  (`python313.zip`, `.`, `#import site`) alone; the working folder was `C:\Windows\system32` [verified:
  `e3c-service.json`]. Every one of the ZIP's 30 `.exe`, `.dll` and `.pyd` files passes WinVerifyTrust: Python
  Software Foundation on 28, Microsoft Windows Software Compatibility Publisher on `vcruntime140.dll` and
  `vcruntime140_1.dll`; `python.cat` is PSF-signed too; PowerShell 7's `Get-AuthenticodeSignature` agrees on each
  (`Valid`, `Authenticode`) [verified: `layout.json`, run 42400758567]. The token: user `S-1-5-19`, the one
  privilege `SeChangeNotifyPrivilege` (enabled by default), session 0, System integrity, groups Everyone, Users,
  SERVICE, CONSOLE LOGON, Authenticated Users, This Organization, LOCAL, the logon SID and `NT SERVICE\<name>`
  [verified: `e3c-service.json`].
- **E3, the restricted service SID.** With `sidtype restricted` the token is write-restricted (`IsTokenRestricted`
  true; restricted SIDs: the service's own `NT SERVICE\lagdoctor-e3-restricted`, Everyone, the logon SID and
  `WRITE RESTRICTED`, as SERVICE_SID_INFO says). `StartTraceW` then returns ERROR_ACCESS_DENIED for every real-time
  session (13 probe sessions, one per provider, and `lag-doctor-net`), before any provider is reached; Windows' own
  access check on that real token (E1's method, authz) agrees on every row, the default row's real-time session
  included; and PresentMon exits with code 4, `error: failed to create hotkey window.` (a message-only window it
  creates unconditionally: `wmain` in `PresentMon/MainThread.cpp` at v2.6.0). The restricted SID is the cause: the
  same service with `sidtype unrestricted` was refused nothing [verified: `e3.json`, three runs].
- **E3, unrestricted LocalService**, privileges still trimmed to SeChangeNotifyPrivilege. Every one of the 13
  providers enabled (`EnableTraceEx2`), Kernel-Network and Kernel-Process included, and authz on the real token
  agreed on each. lag-doctor's own collectors then ran as in a capture: the tracker's session started, 151
  Kernel-Network events arrived, its self-test verified the address layout (UDP and TCP, both ways), the stand-in's
  56 UDP events were counted under its PID, and process names came from the handle-free snapshot (the top talkers
  were named). PresentMon saw the stand-in's presents by PID: 401 rows in 8 s for the stand-in, named `<unknown>`
  since LocalService cannot open another account's process, and 400 for `dwm.exe`, with
  `--no_track_display --no_track_gpu`. With lag-doctor's own flags (display tracking on) it wrote no row for any
  process, and the control, the same capture run by the job's administrator account in the desktop session, got
  the same: 0 rows with display tracking, 726 without (369 for the stand-in), so that is the GPU-less runner, whose
  Basic Display driver gives a present no display event [inferred from the two probes], not the service. The capture
  ended 0.55 s after the stand-in exited (0.6 and 0.81 s in the earlier runs), stopped by lag-doctor's own watch of
  the PID (`terminate_session` path): PresentMon's `--terminate_on_proc_exit` did not fire, as it needs the handle
  it could not get. ETW delivered the tracker's packets up to 2.2 s late on the VM (2.7 s in the control), so 6 to
  9 of them fell outside the 2 s reorder window [verified: `e3.json`].
- **E3b, the named-pipe trigger.** `sc triggerinfo lagdoctor-e3b start/namedpipe/lagdoctor-e3b` (the bare name, as
  Windows' own `srvsvc` and `winreg` triggers carry it) read back as `NETWORK EVENT 1f81d131-… [NAMED PIPE EVENT]
  DATA lagdoctor-e3b`. Three cycles, each from `stopped`: the client's first `CreateFileW` on
  `\\.\pipe\lagdoctor-e3b` failed with ERROR_PIPE_BUSY (231), not file-not-found; its second attempt connected
  0.080 to 0.088 s after the first (0.11 to 0.12 s in the earlier runs); the service's `ServiceMain` received the
  arguments `TriggerStarted 6` (6 = SERVICE_TRIGGER_TYPE_NETWORK_ENDPOINT), had its pipe ready 2 ms later, served
  the request, stopped itself 10.1 s after its last client (a 10 s idle limit polled every 0.25 s) with exit code 0,
  and the next connect started a new process, three distinct PIDs in all. The pipe was created under the
  write-restricted token with `D:P(A;;GA;;;SY)(A;;GA;;;<service SID>)(A;;GRGW;;;BA)(A;;GRGW;;;IU)`,
  `PIPE_REJECT_REMOTE_CLIENTS` and `FILE_FLAG_FIRST_PIPE_INSTANCE`, and the client connected at identification level
  [verified: `e3b.json`, `e3b/e3b-<pid>.json`, three runs].

What a runner cannot show: PresentMon rows with display tracking, so frame timing under LocalService is confirmed up
to the presents being seen by PID; the first install on a PC with a GPU (Stage 4) shows the rest. E2 did not run
there either: the job is the built-in Administrator, elevated, so a filtered token would need an account of its own.

## 9. The MSI on GitHub's runners (Stage 3; 2026-11-08)

Run by `ci.yml`'s msi job on `windows-2025` (`scripts/wix_tool.py`, `scripts/msi_build.py`, `scripts/msi_check.py`):
runs 42471213097 and 42472399937 for the clean build, 42472425233, 42472467137 and 42472537599 for the sabotages.
The numbers are the job summaries' and logs' [verified].

- **The build.** WiX `7.0.1+642f1a0`, from the pinned package; `wix build -arch x64 -wx` with the payload bound, then
  `wix msi validate`, whose one warning on the first build was ICE61 (same-version upgrades, allowed on purpose; now
  suppressed alone, every other warning an error). The MSI holds 152 files: the runtime ZIP's 34, the 57 files git
  tracks under `lagdoctor/` and their 57 `.pyc` files, PresentMon, Intel's licence, `bin\lagdoctor.cmd` and
  lag-doctor's licence, in 153 components (one per file, plus the PATH entry's). Read back from it: product name,
  manufacturer, summary subject and author all `lag-doctor`, version 0.1.0, the PATH row `=-*PATH` = `[~];[BINFOLDER]`,
  and a File table that places exactly the payload's files.
- **The install** (`msiexec /i /qn`, exit 0), as Windows Installer reports it: the product installed, the one product
  under lag-doctor's upgrade code, each of the 153 components in `C:\Program Files\lag-doctor`, the Apps entry naming
  lag-doctor, 0.1.0 and its publisher, exactly 5 keys added (its Uninstall entry and its Installer `Products`,
  `Features`, `UpgradeCodes` and `UserData\S-1-5-18\Products` keys), 15 of its folders in Installer's `Folders` key,
  and the system PATH's 69 entries followed by `C:\Program Files\lag-doctor\bin\`, once. Every installed file is its
  source byte for byte, and PresentMon passes lag-doctor's own check there.
- **From a fresh shell**, in the environment a new sign-in gets (`CreateEnvironmentBlock`, built from the registry,
  not the job's): `where lagdoctor` finds `bin\lagdoctor.cmd` alone, `where python` nothing installed;
  `lagdoctor --version` says `lag-doctor 0.1.0` from cmd and from Windows PowerShell 5.1; `--help` starts; `setup`
  verifies `C:\Program Files\lag-doctor\PresentMon\PresentMon-2.6.0-x64.exe` although a verified per-user copy is
  there too; `analyze` writes its 42,441-byte report. All 56 modules import in the installed runtime from the install
  folder, `sys.path` holding its ZIP and folder only (isolated), each `.pyc` compiled from the `.py` beside it. After
  all that, run as an administrator, the 152 files are unchanged.
- **The uninstall** (`msiexec /x /qn`, exit 0): every file and the folder gone, the system PATH exactly as before,
  the product, its components and its upgrade code unknown to Windows Installer, the 5 keys gone and none of its
  folders left in the `Folders` key.
- **Sabotages**, each a commit reverted at once: (1) the payload without `lagdoctor/fixes/power.py`, after the build's
  own checks: `files` failed naming it and `imports` with its ModuleNotFoundError, while `--help`, `setup` and
  `analyze` passed, which is why those two checks exist; (2) the PATH entry `Permanent="yes"` (the build's own row
  check loosened to let it through): every check passed until `gone`, which found the PATH still naming `bin\`;
  (3) no bytecode shipped: an administrator's run wrote `lagdoctor\__pycache__\*.pyc` into Program Files
  (`untouched`), and the uninstall left the folder holding them, its folders still in the `Folders` key (`gone`).

What a runner cannot show: the UAC prompt (the job is elevated, and UAC never prompts there), SmartScreen on a
downloaded MSI, Smart App Control (E6), Windows 10, and a standard account's install (E7's other parts).

## 10. What the embeddable runtime imports (2026-11-08)

Measured on this PC by `scripts/runtime_modules.py`: the pinned `python-3.13.17-embed-amd64.zip` (a cached copy whose
SHA-256 and size matched the pin, section 8) unpacked into a new folder, and its own `python.exe`, isolated by its
`python313._pth` (`sys.path` its ZIP and folder only; `isolated`, `ignore_environment`, `no_site` and `safe_path`
set), asked to look up and import each of the 290 names on its own `sys.stdlib_module_names`. The answer agreed with
the ZIP's listing before it was kept, and is `scripts/runtime_modules.json`, which `tests/test_stdlib_only.py` reads
[verified: the runtime asked twice, the same bytes both times; the same census by hand]. On CI's runner (Windows
Server 2025) the same runtime gave the same answer byte for byte, and the same JSON with one module's kind changed by
hand failed the msi job's check, naming that module [verified: runs 42484065202 and 42484607546].

- **The ZIP**: 34 files at its top: `python.exe`, `pythonw.exe`, `python313.dll`, `python3.dll`, the two
  `vcruntime140` DLLs, 20 extension modules (`.pyd`: `_asyncio`, `_bz2`, `_ctypes`, `_decimal`, `_elementtree`,
  `_hashlib`, `_lzma`, `_multiprocessing`, `_overlapped`, `_queue`, `_socket`, `_sqlite3`, `_ssl`, `_uuid`, `_wmi`,
  `_zoneinfo`, `pyexpat`, `select`, `unicodedata`, `winsound`), OpenSSL's two DLLs, `libffi-8.dll`, `sqlite3.dll`,
  `python313.zip` (541 entries, 181 at its top), `python313._pth`, `python.cat` and `LICENSE.txt`. No `_tkinter.pyd`,
  `tcl86t.dll` or `tk86t.dll`, and no `tkinter`, `idlelib`, `turtle`, `turtledemo`, `venv`, `ensurepip` or `test` in
  `python313.zip`; its `site-packages` folder holds only a `README.txt`.
- **Imports**: 264 of the 290: 68 built into `python313.dll`, 15 frozen, the 20 `.pyd` files and 161 from
  `python313.zip`. Among them `http` (with `http.server`), `socketserver`, `webbrowser`, `ctypes` (with
  `ctypes.wintypes`), `sqlite3` (SQLite 3.50.4) and `winsound`.
- **Found but failing to import**: `curses` (no `_curses`), `pty` and `tty` (no `termios`) and `_ios_support` (no
  Objective-C runtime).
- **Not there**: 22: the 7 that python.org's full installer has (`tkinter`, `_tkinter`, `idlelib`, `turtle`,
  `turtledemo`, `venv`, `ensurepip`), and 15 that no Windows build has (`posix`, `pwd`, `grp`, `fcntl`, `termios`,
  `resource`, `syslog`, `readline`, `_curses`, `_curses_panel`, `_dbm`, `_gdbm`, `_posixsubprocess`, `_posixshmem`,
  `_scproxy`). The `test` package is not on the list at all.
- **Against the full installer** (python.org's 3.13.3 here, the interpreter the tests run on): the same 290-name list
  and the same 69 built-in modules (`xxsubtype`, a test module, is the one not on the list); it imports the same 264
  plus exactly those 7, fails on the same 4, and has the `test` package too. Under every package the runtime ships it
  has each submodule the full install has (every module under those packages in 3.13.3's `Lib` is in
  `python313.zip`), so a scan of top-level names is complete for this runtime.

So the test interpreter's `sys.stdlib_module_names` names 7 modules the installed lag-doctor could not import (and on
Python 3.11 also modules that 3.13 removed): `tests/test_stdlib_only.py` used it until 2026-11-08, and now allows only
what the JSON lists as importable. When the pin moves, `scripts/runtime_modules.py --write` asks the new runtime, and
CI's msi job asks again on every push and fails unless the JSON is its answer.

## 11. The locked pipe (2026-11-08)

Measured on this PC (build 26200, unelevated, medium integrity) with throwaway probes that made only their own pipes,
before and while `lagdoctor/winapi/pipe.py` was written; an adversarial review of the module (three reviewers, each
finding checked by a skeptic: 23 of 25 confirmed) added the last six points. `tests/test_pipe.py` checks each on every
run, here and on CI, whose suite runs elevated at High integrity (section 8):
- **Squatting is real.** With another process's pipe already on the name (maximum instances 2 or 255, DACL
  `D:(A;;GA;;;WD)`), a server created without `FILE_FLAG_FIRST_PIPE_INSTANCE` becomes that pipe's second instance,
  although it asked for one instance, and the pipe keeps the first creator's descriptor (read back:
  `D:(A;;FA;;;WD)`). With the flag the create fails with ERROR_ACCESS_DENIED; with the other pipe at one instance,
  with ERROR_PIPE_BUSY whether or not the flag is set [verified: probe; the test repeats both].
- **Remote clients**: `FilePipeLocalInformation`'s `NamedPipeType` reads 3 (message type plus
  `FILE_PIPE_REJECT_REMOTE_CLIENTS`) with `PIPE_REJECT_REMOTE_CLIENTS` and 1 without. An open through
  `\\localhost\pipe\<name>`, which comes in as a remote client over the loopback, is refused (ERROR_ACCESS_DENIED)
  with the flag and connects without it [verified: probe].
- **The client's access**: a client asking for `GENERIC_READ | FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES` opens a pipe
  whose DACL grants it `0x12018b` and nothing more; `SetNamedPipeHandleState` (message read mode) needs the
  write-attributes right and fails with ERROR_ACCESS_DENIED without it. A client asking for `GENERIC_WRITE`, which
  maps to `FILE_GENERIC_WRITE` and so includes `FILE_CREATE_PIPE_INSTANCE` and `FILE_WRITE_EA`, is refused by that
  DACL [verified: probe].
- **The integrity label must deny every right, not just writing.** With `S:(ML;;NW;;;ME)` a client impersonating a
  low-integrity copy of the same token is refused a read-write open but admitted to a read-only one (`GENERIC_READ`),
  and so can take the pipe's one instance and read what the server writes first. With `NWNR`, `CreateFileW` asking
  only for `SYNCHRONIZE` is refused, because `CreateFileW` always adds `FILE_READ_ATTRIBUTES`, but `NtOpenFile` asking
  for `SYNCHRONIZE` alone is admitted. `S:(ML;;NWNRNX;;;ME)` refuses all three opens and admits a medium client. A
  pipe without a label counts as medium with `NW` only [verified: probes; the review; the test's controls].
- **Identification level**: a client opened with `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION` shows level 1
  (SecurityIdentification) to a server that impersonates it and reads its thread token; without the flags, level 2
  (SecurityImpersonation) [verified: probe].
- **Names**: `CreateFileW` on `\\.\pipe\..\C:\<path>` opened that ordinary file: Windows folds `..` in a `\\.\` path
  before the open. `pipe.path` therefore takes 1 to 100 of `[A-Za-z0-9._-]`, starting with a letter or a digit
  [verified: probe and the test's control].
- **`OVERLAPPED.Internal`**: 0x103 (STATUS_PENDING) while a call runs, its final status once it ends (0xC0000120
  when cancelled), and 0x103 also after a call that failed at once (a bad handle; ERROR_BROKEN_PIPE when the other end
  had gone), with nothing left to complete it. `CancelIoEx` answers ERROR_NOT_FOUND (1168) for a call that has ended
  or never ran. A value set before the call (bits above 31) survives only if the call never reached Windows. So
  "running" is read from both, never from Internal alone [verified: probe].
- **A thread's I/O ends with the thread**: an overlapped `ConnectNamedPipe` that a thread started and left pending was
  cancelled (ERROR_OPERATION_ABORTED) when that thread exited. Windows cancels the pending I/O a thread issued when it
  exits, for handles not bound to a completion port, so no wait in `pipe.py` outlives the call that started it
  [verified: an early version's own test].
- **Ctrl+C mid-wait**: Python raises KeyboardInterrupt in the main thread when a blocking call returns, here right
  after `WaitForSingleObject`, before any cancel. An early version kept each call's OVERLAPPED and buffer in the
  function's locals, so the call went on with them freed: the next read was woken by the old call's completion and
  failed with ERROR_IO_INCOMPLETE, messages landed in freed memory, and a process later crashed (the review's probes:
  a segfault in 2 of 3 runs). Waits also ended only at their timeout. Now each direction's memory belongs to the
  pipe, waits run in 100 ms slices, and an interrupted call is cancelled and waited out first [verified: the review;
  the Ctrl+C tests, through `_thread.interrupt_main()`].
- **Aliases in a read-back descriptor**: `ConvertSecurityDescriptorToStringSecurityDescriptorW` writes the local
  built-in Administrator (RID 500, CI's job account) as `LA`, where `ConvertSidToStringSidW` writes the full SID; a
  test compares a read-back DACL with the same DACL as Windows spells it [verified: the review; the test runs the alias
  case on this PC with its own RID-500 SID].

## Decided (2026-11-06) and still open

Decided by the owner (DECISIONS 2026-11-06; the work is tracked in `docs/INSTALLER-PLAN.md`):
1. Route A: the signed MSI with a capture service. D is rejected, so lag-doctor still never changes group
   membership itself; C and E stay as fallbacks.
2. MIT licence; signing through SignPath Foundation (free). Its conditions [verified: signpath.org/terms]: an OSI
   licence and no proprietary component; "already released in the form that should be signed"; "a certain
   verifiable reputation" for downloadable programs; a verifiable CI build; MFA for every team member; a code signing
   policy and a privacy statement on the home page; manual approval of every signing request. Upstream open-source
   binaries may ride unsigned inside a signed MSI (the terms warn that a later policy may require signed files
   only). That the first public release is an unsigned preview already carrying the service was not a separate
   decision by the owner: it follows from the "already released" condition and the plan's order
   (`docs/INSTALLER-PLAN.md`, Stage 5).

Still open: a small native helper instead of the embeddable runtime only if E6 (Smart App Control) objects; E3c
found the runtime isolated and every binary signed (section 8). Decided 2026-11-07 from E3: an unrestricted service
SID (DECISIONS 2026-11-07).

## Verification log (2026-11-06)

Each claim below was re-read from its source by a reader separate from the sweep that found it.

| Claim | Result | Source read |
|---|---|---|
| Default ETW descriptor SDDL and Performance Log Users' `0xee5` decode | confirmed, verbatim | Babkin blog post |
| Named-pipe trigger GUID `1F81D131-3FAC-4537-9E0C-7E7B0C2F4B55`, start on pipe open; idle-stop sentence | confirmed; the idle sentence belongs to the network-endpoint trigger type as a whole | SERVICE_TRIGGER |
| Required-privileges trim removes unlisted privileges; SeChangeNotifyPrivilege never removed | confirmed | SERVICE_REQUIRED_PRIVILEGES_INFOW |
| ProcessIdToSessionId needs PROCESS_QUERY_INFORMATION | confirmed | its Learn page |
| PresentMonSharedService: auto start, no Account attribute | confirmed; the install folder is set by each consuming MSI (example `ProgramFiles64Folder\Intel\PresentMon\Service`) | Library.wxs on main |
| Kernel-Process grants SYSTEM, LOCAL SERVICE, Administrators | the issue exists and says so; measured by its author on build 26300, not by us | CrankyAnt/SharedWorkflows#30 |
| CVE-2025-47985 needed Performance Log Users | confirmed for the advisory's trigger path; impact SYSTEM; published 2025-07-08 | STAR Labs advisory |
| Game Bar FPS: "Request access", Performance Log Users, restart | confirmed through a Microsoft Answers thread quoting the support page; the support page itself returned no text to the reader | answers.microsoft.com |
| Artifact Signing: individuals in the US or Canada only | confirmed; no price on the page; the "onboarding paused" claim is an AI answer | Artifact Signing quickstart; Microsoft Q&A |
| SignPath Foundation free for OSS, signs under its own name | confirmed; its conditions are the next row | signpath.org |
| SignPath Foundation's conditions: an OSI licence and no proprietary component; "already released in the form that should be signed"; "a certain verifiable reputation" for downloadable programs; a verifiable build; MFA for every team member; a code signing policy and a privacy statement on the home page; manual approval of every signing request; unsigned upstream open-source binaries allowed in a signed package | confirmed, the quoted phrases verbatim; the terms add that a later policy may require signed files only, and that some constraints are not yet enforced automatically | signpath.org/terms (fetched 2026-11-06) |
| A restricted service SID puts the service SID, World, the service logon SID and S-1-5-33 on the token's restricted SID list | confirmed; the page does not say how access checks use that list | SERVICE_SID_INFO (updated 2024-02-22) |
| `0x800` = TRACELOG_REGISTER_GUIDS, `0x1000` = TRACELOG_JOIN_GROUP | confirmed | SDK 10.0.19041 `shared\wmistr.h`:467-468 |
| Smart App Control: blocks unknown unsigned code, no per-app bypass, re-enable without clean install | confirmed (with the diagnostic-data caveat) | overview; support FAQ |
| Administrator protection: off by default, KB5120998, on by default with the October 2026 update for security-baseline users, hidden profile-separated account, Windows Hello | confirmed | Administrator protection overview |
| WiX v7 and the US$10,000 OSMF threshold | confirmed only in the page's summary; the EULA switch name not stated there | docs.firegiant.com/wix/whatsnew |
| NVIDIA `FvSvc`: LocalSystem, demand start | confirmed | `sc.exe qc FvSvc` here |
| Performance Log Users has 0 members here | confirmed | `Get-LocalGroupMember -SID S-1-5-32-559` |
| E1: Kernel-Network and Kernel-Process grant SYSTEM, LocalService (enable only) and Administrators; the default descriptor admits Performance Log Users; the group would get frame timing but not the tracker, LocalService both | measured on this PC, build 26200 | `scripts/etw_access.py` run by the owner, 2026-11-06 23:31; its saved descriptors re-read |
| E1 on a GitHub runner: Kernel-Network, Kernel-Process and the default descriptor are the laptop's | measured on `windows-2025`, build 26100, in three runs | `scripts/stage2/drive.py e1`, runs 42398897435, 42399952827, 42400758567 (2026-11-07); `e1.json` compares the SDDL text |
| E3c: the embeddable 3.13.17 runs as a LocalService service from Program Files; `sys.path` is its ZIP and folder only; all 30 binaries signed | measured, three runs; the PowerShell cross-check agreed in the last run (Windows PowerShell 5.1 could not load its security module under the job's PowerShell 7 module path in the first two) | `e3c.json`, `e3c-service.json`, `layout.json` of run 42400758567 |
| E3: a restricted service SID gets ERROR_ACCESS_DENIED from StartTraceW for every session and PresentMon exit 4; unrestricted, 13 of 13 providers enable, the tracker records (151 events, self-test verified), PresentMon writes 401 rows by PID without display tracking and none with it, the capture ends 0.55 s after the game exits | measured, three runs, with an administrator control that got the same PresentMon result | `e3.json`, `e3-*-service.json`, `standin-*.json` of run 42400758567; `IsTargetProcess` and the lazy CSV header re-read in `OutputThread.cpp` and `CsvOutput.cpp` at v2.6.0 |
| E3b: the named-pipe trigger starts the service in 0.08 to 0.12 s (the client's first open gets ERROR_PIPE_BUSY), ServiceMain gets `TriggerStarted 6`, idle self-stop after 10 s, the next connect starts it again | measured, three cycles in each of three runs | `e3b.json`, `e3b/e3b-<pid>.json` of run 42400758567; `sc qtriggerinfo` read back |
| WiX v7.0.1's EULA (OSMF v1.1): a fee only for revenue-generating use at US$10,000 a year or more; not a licence fee; nothing binds the MSIs built or their users | confirmed, read in full on 2026-11-08 | `OSMFEULA.txt` at tag v7.0.1 (the same bytes at v7.0.0 and main) |
| The `wix` 7.0.1 NuGet package: 4,586,487 bytes, SHA-512 `9c72a17d…17b37d70`, .NET 8 or later | confirmed; CI's download matched it | nuget.org catalog entry `2026.10.26.22.50.24/wix.7.0.1.json`; `scripts/wix_tool.py` on run 42471213097 |
| Every `wix` command but help, version and eula needs `-acceptEula wix7` (or a remembered acceptance), else WIX7015 | confirmed in the source; CI's builds pass with the switch | WiX v7.0.1 `Program.cs`, `CommandLine.cs`; runs 42471213097, 42472399937 |
| App Control does not control batch files, only what they run | confirmed | Microsoft Learn, App Control script enforcement (updated 2026-10-28) |
| SignPath signs only binaries built from the project's own source; upstream open-source binaries may ride unsigned in a signed MSI; one product name and version on every signed file | confirmed | signpath.org/terms; docs.signpath.io artifact configuration (2026-11-08) |
| The MSI installs, runs from a fresh shell and uninstalls with nothing left: 152 files, 153 components, 5 Installer keys, the PATH entry | measured | `scripts/msi_check.py`, runs 42471213097 and 42472399937 |
| Sabotaged MSIs fail the check where expected: a missing module (files, imports), a kept PATH entry (gone), no bytecode (files, imports, untouched, gone) | measured | runs 42472425233, 42472467137, 42472537599 |
| The embeddable 3.13.17 imports 264 of the 290 names on its own standard-library list; `tkinter`, `_tkinter`, `idlelib`, `turtle`, `turtledemo`, `venv` and `ensurepip` are not in it; `curses`, `pty`, `tty` and `_ios_support` are found but fail to import; the full installer's 3.13.3 imports the same plus those 7 | measured on this PC, 2026-11-08, by the runtime itself and checked against the ZIP's listing; the same answer on CI's runner, where a hand-edited JSON failed the check | `scripts/runtime_modules.py` and its `scripts/runtime_modules.json`; the dev interpreter's census by hand; the msi job of runs 42484065202 (the same answer) and 42484607546 (`zipapp`'s kind changed by hand: refused, naming it) |
