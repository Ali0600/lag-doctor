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
- Microsoft-Windows-Kernel-Process (and Kernel-File) carry their own descriptor granting only SYSTEM, LOCAL SERVICE
  and Administrators [research: github.com/CrankyAnt/SharedWorkflows/issues/30, a third party, measured on build
  26300; re-read, still secondhand]. PresentMon 2.6.0 tolerates ERROR_ACCESS_DENIED on Kernel-Process only (no
  process start/stop events then); any other provider failing aborts the session [research:
  `PresentData/PresentMonTraceSession.cpp` at v2.6.0]. So a Performance Log Users member most likely loses
  Kernel-Process, and a LocalService service most likely keeps it [inferred; E1 settles it].
- Microsoft-Windows-Kernel-Network's descriptor (lag-doctor's network tracker) is unknown: `EventAccessQuery` answers
  5 unelevated for every GUID [verified: run in the sweep and in `network-and-etw.md` 5.7]. Whether Performance Log
  Users can enable it decides whether the group covers the network half [unverified; E1].
- Group membership is in the logon token, so a new member needs a new sign-in [research: Microsoft "Access tokens"];
  `klist purge` only drops Kerberos tickets and does not help [research: Microsoft klist doc].
- Performance Log Users is not on the list of groups UAC filters out of an administrator's standard token, so it
  should stay enabled unelevated [research: 2007 MSDN Magazine and TechNet Magazine UAC articles; inferred for
  Windows 11; E2]. setup's "After that, frame timing needs no prompt" rests on this and on E1 (no provider
  PresentMon needs may refuse the group); for the game-server tracker setup says "Not checked yet". If this
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
| C. Performance Log Users from setup's printed steps (today) | 1 admin command | yes | nothing, if E1 and E2 pass | the account, and all it runs, controls every default-descriptor ETW session | keep as the explained opt-in |
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
  RegisterGuids (`0x800`; `0x1800` adds JoinGroup) [verified: SDK `wmistr.h`], so whether trace control passes
  that second list is unknown [inferred].
- **Start on demand**: trigger type SERVICE_TRIGGER_TYPE_NETWORK_ENDPOINT with subtype NAMED_PIPE_EVENT_GUID
  `1F81D131-3FAC-4537-9E0C-7E7B0C2F4B55` starts the service "when a request is made to open the named pipe", and is
  "commonly used to start a service that has stopped itself after an idle time-out" [verified: SERVICE_TRIGGER].
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
  [research: PresentMon `Privilege.cpp`]; whether the capture still ends cleanly when the game exits is untested (E3).

## 5. Shipping

- **Installer**: WiX v7 (v7.0.1 released 2026-10-26) [research: github.com/wixtoolset/wix/releases]. Under the Open
  Source Maintenance Fee EULA v1.1 no fee is due below US$10,000 a year of revenue, and builds must accept the EULA
  explicitly (`-acceptEula`) [research: docs.firegiant.com/wix/whatsnew; the re-read got only a summary of that section].
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
  signpath.org/terms]. An OV certificate costs about US$150 to 300 a year and needs a hardware token or HSM
  [research: Microsoft code-signing options].
- **Smart App Control** blocks "unknown, unsigned code" by default, and "there is currently no way to bypass" it for
  individual apps [verified: Smart App Control overview and support FAQ]. Recent updates let it be re-enabled without
  a clean install, except when optional diagnostic data is off [verified: support FAQ]. It is off on this PC
  [research: `VerifiedAndReputablePolicyState` = 0]. Signed: python.org's `python.exe` and stdlib `.pyd` files (PSF),
  PresentMon (Intel). Unsigned: uv-managed `python.exe`, `uv.exe`, psutil's `.pyd` [research: Get-AuthenticodeSignature
  here]. Whether python.org's embeddable ZIP binaries are signed is open. Python scripts run by a trusted `python.exe`
  are not checked; native `.pyd` and `.dll` files are [research: App Control script enforcement].
- **Administrator protection** is off by default and available with KB5120998; where an organisation uses the Windows
  security baselines it is on by default from the October 2026 update. Elevated processes then run under "a hidden,
  system-generated, profile-separated user account", and every admin operation needs Windows Hello [verified:
  Administrator protection overview]. Microsoft advises a services-based design for work that must run elevated
  [research: Windows Developer blog, 2025-05-19]. `--elevate` children would run in that separate profile (E5).

## 6. Experiments before building

| Id | What | Gates |
|---|---|---|
| E1 | Elevated `EventAccessQuery` dump as SDDL: Kernel-Network `{7DD42A49-5329-4832-8DFD-43D979153A88}`, Kernel-Process `{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}`, PresentMon's other providers, the default GUID. Read-only, one UAC. Written as `scripts/etw_access.py`: the elevated copy reads the descriptors, and the unelevated window judges them with Windows' own access check (Authz) for "you, after joining Performance Log Users", a modelled LocalService token and a modelled SYSTEM token | setup's wording; the service account |
| E2 | Join Performance Log Users, sign out and in: `whoami /groups` shows it enabled unelevated, and an unelevated `watch` records frames and network flows. If it shows deny-only instead, setup already handles it (`trace_rights()` answers `filtered`: one UAC prompt per recording, no sign-out asked for) | option C's "no prompts" |
| E3 | Throwaway LocalService service with privileges trimmed to SeChangeNotifyPrivilege and a restricted service SID (both read back from the running token) on a VM: every PresentMon provider enables, rows arrive for the game's PID, Kernel-Network events arrive, the capture ends when the game exits | option A's account |
| E3b | Named-pipe trigger on Windows 11 Home 25H2 and Windows 10 22H2: start, idle self-stop, restart, start latency | option A's start mode |
| E3c | python.org's embeddable runtime as an SCM service from Program Files: `sys.path` holds only Program Files entries, every `.exe`/`.pyd`/`.dll` signed | Python vs a small native helper |
| E5 | Administrator protection on: `--elevate`'s waitable child, the shared capture mutex, the capture landing in the user's folder | the public `--elevate` fallback |
| E6 | Smart App Control enforcing VM: the MSI layout runs, inventory's PowerShell child returns JSON in Constrained Language Mode, Defender scan clean | the public release |
| E7 | winget install, upgrade, uninstall from elevated, unelevated and standard-account shells, and on Windows 10; uninstall removes the service | the public release |
| E8 | Red-first tests of each service guard: another session refused, a squatted pipe fails loudly, a PID in another session refused, no other process's network events relayed, nothing loaded from a user-writable path | shipping the service |

## 7. Before anything is public

- The repo has no git remote [verified: `git remote`, 2026-11-06]. It is MIT-licensed, as PresentMon is: `LICENSE`
  and `license` in `pyproject.toml` were added on 2026-11-06.
- 16 lines in 4 research notes carry this PC's user name in paths [verified: `git grep`]; scrub them, and the history,
  before publishing. Commits are authored with a GitHub noreply address [verified: `git log`].

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

Still open: Python on the embeddable runtime or a small native helper for the service (after E3c and E6).

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
