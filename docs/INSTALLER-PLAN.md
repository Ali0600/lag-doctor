# Installer and capture service: the plan

Goal (DECISIONS 2026-11-06): a stranger installs lag-doctor with one UAC prompt, then records with no prompt and
no sign-out. The route is a signed per-machine MSI that installs a small capture service; signing is free through
SignPath Foundation; the licence is MIT. Facts, sources and the experiments' details are in
[research/install-and-rights.md](research/install-and-rights.md); the experiment ids (E1 to E8) are defined there.

## Status (2026-11-08)

- [x] Decisions recorded; MIT `LICENSE`; `license` in `pyproject.toml`.
- [x] Stage 0: `setup` names the account by its SID in the group command and tells "joined, sign out once" and "in
  the group but not enabled in this token" apart (`elevation.trace_rights`: `after_sign_in`, `filtered`).
- [x] E1, run by the owner on 2026-11-06 (`scripts/etw_access.py`: the elevated copy reads the descriptors, the
  normal window judges them with Windows' own Authz check). Performance Log Users would get frame timing (without
  process start and stop events) but **not** the game-server tracker: Kernel-Network and Kernel-Process let only
  SYSTEM, LocalService and Administrators enable them. A LocalService service would get both (a modelled token,
  so E3 confirms it with a real one). Details in the research note, section 1.
- [x] After E1, done: setup says the group covers frame timing only; the tracker's not-measured reason names
  `--elevate` and, for a member, the group's limit; an explicit `--elevate` elevates a group member.
- [ ] E2, the group fallback's sign-in test (below). Nothing in the service depends on it.
- [x] Stage 1: public repo and CI (2026-11-07). Public after a history rewrite (DECISIONS 2026-11-07); CI on
  GitHub's Windows runners.
- [x] Stage 2 (2026-11-07): E1, E3, E3b and E3c on GitHub's throwaway runners (`.github/workflows/experiments.yml`,
  `scripts/stage2`; research note section 8). The account is LocalService with an unrestricted service SID: a
  restricted one refuses every ETW session (DECISIONS 2026-11-07).
- [x] Stage 3: the MSI (2026-11-08, PRs #6, #8, #12 and #13). A WiX v7 per-machine MSI built in CI, installed, run
  from a fresh shell and uninstalled on a throwaway runner with nothing left; nothing published or signed yet.
- [x] Stage 4: the capture service (done 2026-11-25: the owner's first recording through it, from the MSI on their own PC,
  timed 47,548 frames with no prompt; research note section 13). Begun 2026-11-08 with its channel, the locked pipe (`lagdoctor/winapi/pipe.py`, PR
  #19; research note section 11), and the relay core it will host (`lagdoctor/relay/`, `docs/LAUNCHER-PLAN.md` M3,
  PR #23, merged 2026-11-08): its protocol, its checks and its client. Resumed 2026-11-25 with E9 (research note,
  sections 6 and 12), the questions only a runner can answer before the service is built, answered there: it reads
  its caller's account and session through the pipe, a standard account may start it once its descriptor grants
  interactive users SERVICE_START (and nothing more), it owns its pipe as its service SID, and nothing tells it a
  game's account without a handle (so the game's tree is judged by its folder, DECISIONS 2026-11-25). Then, each its
  own pull request: the service host and its caller policy over the relay core (`lagdoctor/service/`, DESIGN.md "The
  capture service", tested on a thread here, never installed; PR #72); the MSI's ServiceInstall with its check on the
  runner (PR #73: a standard account started it and recorded a stand-in game through it, research note section 13);
  the recorder and the app trying the service first (PR #74: `watch` asks it before the prompt, the app through its
  recorder); and the owner's own install and recording, frame timing's first check under the service.
- [ ] Stage 5: unsigned preview release, SignPath, winget.

## E2: the group fallback

Only setup's promise to a Performance Log Users member ("After that, frame timing needs no prompt") depends on E2;
the service does not. It needs a fresh sign-in after joining, so it runs in one of two places:
- the owner's PC, with one sign-out, only with the owner's go (the group is broad: research note section 1);
  `Remove-LocalGroupMember` and another sign-out undo it;
- a CI runner with a throwaway administrator account and UAC's token filtering on, read back first: the
  premise is about an administrator's filtered token, so a runner with UAC off would pass without testing it. The
  job itself runs as the built-in Administrator, elevated (Stage 2 measured it), so the account must be a new one.
  Frames there need the same stand-in for a game as E3, with display tracking off.

Should the premise fail, `trace_rights()` answers `filtered`, which keeps setup from asking for a sign-out that would
not help.

## Stage 1: public repo and CI

1. Done (2026-11-07): personal details scrubbed from the tree and, by the history rewrite the owner chose, from
   every commit (research note section 7; DECISIONS 2026-11-07).
2. Done (2026-11-07): public at https://github.com/Ali0600/lag-doctor; `.github/SECURITY.md` asks for reports through
   GitHub's private vulnerability reporting.
3. Done (2026-11-07): CI on `windows-2025` for Python 3.13 and 3.11, SHA-pinned actions, a read-only token (since
   2026-11-08 for 3.13 alone, the version the MSI ships, with pushes that change only docs skipped: DECISIONS). Tests
   that need what the runner lacks skip by declaration (`LAGDOCTOR_ABSENT`, `tests/capabilities.py`). Standard
   runners are free for public repositories [verified: GitHub Actions billing page, 2026-11-06].
4. Done (2026-11-07): of the 32 tests that skipped there for want of PresentMon's download or its gold captures, 31
   run in CI; the 32nd needs a token without ETW rights, and the runner's can trace. CI gets PresentMon through
   `lagdoctor setup --download` and the captures at the pinned commit
   (`tests/downloads.py`), checks both against the tests' own pins and caches them; `LAGDOCTOR_REQUIRE` makes a
   missing one fail those tests rather than skip them.

## Stage 2: the service experiments, on CI (done 2026-11-07)

GitHub's Windows runners are disposable virtual machines with administrator rights, so the experiments that need a
throwaway service never touch the owner's PC. `.github/workflows/experiments.yml` runs them by hand and on pull
requests that change it, `scripts/stage2` or a module the experiments import (`tests/test_side_workflows.py` keeps
that list whole); `scripts/stage2/drive.py` builds the folder the MSI would install
under Program Files (the embeddable runtime, lag-doctor's code, the pinned PresentMon) and refuses to run anywhere
but a GitHub-hosted runner. Measured (research note section 8):
- **E3**: a throwaway `NT AUTHORITY\LocalService` service, privileges trimmed to SeChangeNotifyPrivilege, read back
  from the running token. With a restricted service SID nothing starts: `StartTraceW` refuses every session and
  PresentMon cannot create its window. Unrestricted, every one of the 13 providers enables, `lag-doctor-net` records
  (self-test verified, the stand-in's UDP events counted by PID), PresentMon sees the stand-in's presents by PID
  (`<unknown>` name), and the capture ends 0.55 s after the stand-in exits, by lag-doctor's own watch. The runner
  question is answered: a WARP-rendered Direct3D 11 window (`scripts/stage2/standin.py`) gives PresentMon presents,
  but only with display tracking off; with lag-doctor's flags no process yields a row there, administrator or
  service, so frame timing with display tracking is checked on the first install on a PC with a GPU (Stage 4).
- **E3b**: the trigger starts the service in 0.08 to 0.12 s of the client's first open (which fails with
  ERROR_PIPE_BUSY and is retried), the service stops itself after 10 s idle, and the next connect starts it again;
  three cycles, three processes.
- **E3c**: python.org's embeddable 3.13.17 runs as an SCM service through ctypes with `sys.path` holding only its
  own ZIP and folder (`._pth` sets isolated, no site, safe path), and all 30 of its `.exe`, `.pyd` and `.dll` files
  are signed, so the service stays Python unless E6 objects.
- **E1 on the runner**: the same descriptors as on the owner's Windows 11 Home (build 26100 against 26200).

## Stage 3: the MSI

- [x] A build backend and a `lagdoctor` console entry point in `pyproject.toml`. Done 2026-11-07: uv_build
  (DECISIONS 2026-11-07); the wheel holds the `lagdoctor` package alone. CI's wheel job builds it, installs it into a
  fresh venv and runs it outside the checkout (`scripts/wheel_check.py`: the files git tracks, every module imported,
  `--help`, `--version`, `analyze`); seven wheels broken on purpose each failed it.
- [x] Replace psutil, whose native module is unsigned (Smart App Control would block it). Done 2026-11-07:
  `capture/presentmon.py` reads the PID list from `winapi/ntquery.pids()` (the handle-free snapshot),
  `capture/netflows.py` this PC's addresses from `winapi/iphlp.adapter_addresses()` (GetAdaptersAddresses) and
  `capture/inventory.py` the RAM size from `winapi/memory.py` (GlobalMemoryStatusEx), each checked against psutil
  on the live machine. `tests/test_stdlib_only.py` keeps every module under `lagdoctor/` on the standard library;
  since 2026-11-08, on the part of it the embeddable runtime imports, by that runtime's own answer
  (`scripts/runtime_modules.json`): it leaves out tkinter, IDLE, turtle, venv and ensurepip, which the interpreter
  running the tests has. The tests keep psutil for their independent readouts: since 2026-11-08
  it is in the dev group, the wheel declares no dependency at all, and LagLab's pyglet is a dependency group rather
  than an extra the wheel would advertise without shipping `laglab/`.
- [x] WiX v7 per-machine MSI built in CI. Done 2026-11-08 (PR #13; DECISIONS 2026-11-08): the embeddable Python
  3.13.17, lag-doctor's code from the wheel with unchecked-hash bytecode compiled by that runtime, the byte-identical
  Intel-signed PresentMon 2.6.0 with Intel's licence text, and `bin\lagdoctor.cmd` alone on the system PATH, in
  `C:\Program Files\lag-doctor` (`installer/lag-doctor.wxs`, `scripts/msi_build.py`). lag-doctor looks for PresentMon
  there first (`setup.bundled`). CI's msi job installs it on the runner, runs `lagdoctor` from a fresh shell (cmd and
  Windows PowerShell, `where`, `--version`, `--help`, `setup`, `analyze`, every module imported), uninstalls it, and
  reads back that no file, PATH entry or Windows Installer key is left (`scripts/msi_check.py`; part of E7). Three
  sabotaged builds each failed it where they should: a missing module, a PATH entry the uninstall keeps, no
  bytecode (research note section 9).
- [x] The leftovers PRs #6 and #8 reported, done 2026-11-08 (PR #12): psutil in the dev group (the tests' readouts
  still use it); `scripts/stage2/drive.py` no longer copies psutil into its install folder; the `laglab` extra is a
  dependency group (DECISIONS 2026-11-08), since the wheel advertised an extra that installs pyglet but ships no
  `laglab/`.
- [x] WiX v7 builds must accept its Open Source Maintenance Fee EULA explicitly (research note section 5: no fee below
  US$10,000 a year of revenue). The owner accepted it for CI's builds on 2026-11-08: `--accept-eula wix7`, once, in
  `ci.yml`.
- [x] What SignPath requires, prepared in the repo (2026-11-08): the README's "Code signing policy" section (with
  "Free code signing provided by SignPath.io, certificate by SignPath Foundation", the team roles, what is signed) and
  its privacy statement (no data sent anywhere; while recording, pings to the router, 1.1.1.1 and the game server and
  the tracker's one DNS self-test, which `--no-network` turns off); the product name `lag-doctor` and the version in
  the MSI's properties and summary, read back by the build (the MSI is the only file lag-doctor signs: the runtime
  and PresentMon are signed upstream, the `.cmd` cannot be); and the MSI's uninstall, checked to leave nothing.

## Stage 4: the capture service

- WiX `ServiceInstall` with `Account="NT AUTHORITY\LocalService"`, a quoted Program Files path, required privileges
  trimmed to SeChangeNotifyPrivilege, an unrestricted service SID (E3: a restricted one cannot start an ETW session
  or PresentMon's window), demand start with SERVICE_START for interactive users in the service's own descriptor
  (`PermissionEx` under `ServiceInstall`, Windows Installer's MsiLockPermissionsEx table; the client calls
  StartServiceW, E9) instead of the named-pipe trigger, which Windows Installer's tables cannot set (DECISIONS
  2026-11-25), and idle self-stop; users never get change-config or stop. Windows Installer's page for
  MsiServiceConfig says its settings take effect "the next time the system is started": the MSI's check reads
  them back right after the install.
- The pipe: explicit DACL, PIPE_REJECT_REMOTE_CLIENTS, FILE_FLAG_FIRST_PIPE_INSTANCE; the CLI connects at
  identification level and retries its first open, which fails with ERROR_PIPE_BUSY while the service starts (E3b);
  the service names its own service SID as the pipe's owner (by default LocalService owns it, E9) and reads its
  caller's account and session from the caller's token through the pipe (E9); one recording at a time; a protocol version handshake. Done 2026-11-08 for the pipe itself:
  `lagdoctor/winapi/pipe.py` makes the first and only instance under a DACL that grants each named SID exactly what a
  client asks for (read, write data, write attributes) plus a medium label that denies anything below medium every
  right (`NWNRNX`: a write-only label still let a low process open the pipe to read and take its one instance); the
  client connects at identification level and retries a missing or busy pipe to a deadline; both ends read the other's
  PID and session, and the pipe's owner, without a handle; Ctrl+C or close() mid-call leaves nothing running. Each
  lock has a test whose control shows the hazard on the machine (research note section 11). The service's own DACL
  (which group may connect: Interactive Users, `S-1-5-4`), the handshake and the one-recording rule belong to the
  relay protocol, and so does a short deadline for a client's first request: any interactive user's
  medium-integrity process may connect and would otherwise hold the one instance.
- Frame timing's first real check: PresentMon under the service with display tracking, on the owner's PC at the
  first install (a runner has no GPU, E3).
- The target must be in the caller's own session, checked with the session id from `winapi/ntquery.py`'s
  handle-free snapshot (ProcessIdToSessionId would open the game). Only that process's frame rows and network
  events are relayed. No file writes; nothing loaded from a user-writable folder.
- The CLI uses the service when it answers and falls back to `watch --elevate` when it does not (git checkouts).
- The service hosts the relay core that `docs/LAUNCHER-PLAN.md` builds first as a relay elevated per recording (its
  M3): one protocol, one client for the CLI and the app. Stage 4 changes where the relay runs and its policy: a fixed
  pipe name its trigger starts, not a random one; the caller's own session and target, not a command line approved at
  a prompt (the target comes in a `game` message, judged on the service's own snapshot, as Play's relay takes it since
  M6d: the service's policy goes in `relay/server.Relay._judge` and its deadlines, `_out_of_turn`'s folder-only
  `game`, the hello's game-or-folder rule and `RelayLink.aim`, and `since` there cannot be its own start time); the
  installer's PresentMon only; and the
  client checks the server's PID against the service's, and the pipe's owner (`owner_sid()`, which a squatter cannot
  set to the service's), since any process could create that name first, for instance in the moments after the
  service stops itself when idle.
- E8: each guard ships red-first, as every guard here does.
- What M3's review (2026-11-08) left for the service, where any local user is a caller:
  - **One recording machine-wide.** The ETW session names (`lagdoctor`, `lag-doctor-net`) are global, and starting
    either stops another Windows session's recording; the capture lock is per session. An elevated relay in another
    session can already do this; the service must refuse a second caller instead. The service host (2026-11-25)
    takes one caller at a time on its one pipe instance, and a recorder that finds it busy records nothing rather
    than start a relay that would stop that recording (PR #74). Done 2026-11-25: the machine-wide trace lock
    (`capture/tracelock.py`), which the service, the elevated relay and an elevated `watch` in any session hold
    while they trace, and which the recorder checks before it asks the service or raises the prompt (DESIGN.md,
    Privileges and elevation; DECISIONS 2026-11-25).
  - **How long one caller may hold it.** Nothing caps a recording through the service yet: a caller that keeps
    reading holds the one instance (watch's own caps bound an honest recorder), and another account's process that
    holds it keeps everyone else from the service and its prompt alike. Done 2026-11-25: one ask holds it for at
    most `protocol.SERVICE_HOLD_S` (2 hours), and `watch` stops itself 3 minutes before that. An account that asks
    again at once can still keep it busy: accepted (DECISIONS 2026-11-25). The recorder falls back to the prompt when
    the service's pipe is not the service's, never when it is busy (PR #74).
  - **The caller's session, from the caller.** The relay filters per-program totals by its own session, which equals
    the recorder's only because it checks that at start; the service runs in session 0 and must use the client's.
  - **The caller's account, from its token**, never a command line, for the pipe's DACL and the checks.
  - **The game's tree can hold another account's process**: a caller naming Explorer as its game would get the
    whole events of an administrator's elevated programs started from it. Before a descendant's events leave the
    service, it must run as the caller's account, read without opening the game.

## Stage 5: release

1. An unsigned preview release of the MSI, service included, on GitHub Releases (SignPath signs only a project
   "already released in the form that should be signed").
2. Apply to SignPath Foundation. Its approval is manual and may be declined for reputation; then releases stay
   unsigned, and the README says what SmartScreen and Smart App Control will show.
3. With SignPath: signing in the release workflow, each request approved by the owner.
4. winget: a hand-written first manifest (`wix`, machine scope, `elevationRequired`), then automated updates.
5. Before announcing: E5 (Administrator protection on), E6 (a Smart App Control machine), E7 (winget from elevated,
   unelevated and standard-account shells, and Windows 10).
