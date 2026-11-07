# Installer and capture service: the plan

Goal (DECISIONS 2026-11-06): a stranger installs lag-doctor with one UAC prompt, then records with no prompt and
no sign-out. The route is a signed per-machine MSI that installs a small capture service; signing is free through
SignPath Foundation; the licence is MIT. Facts, sources and the experiments' details are in
[research/install-and-rights.md](research/install-and-rights.md); the experiment ids (E1 to E8) are defined there.

## Status (2026-11-06)

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
- [ ] Stage 1: public repo and CI.
- [ ] Stage 2: the service experiments on CI's throwaway Windows machines.
- [ ] Stage 3: the MSI.
- [ ] Stage 4: the capture service.
- [ ] Stage 5: unsigned preview release, SignPath, winget.

## E2: the group fallback

Only setup's promise to a Performance Log Users member ("After that, frame timing needs no prompt") depends on E2;
the service does not. It needs a fresh sign-in after joining, so it runs in one of two places:
- the owner's PC, with one sign-out, only with the owner's go (the group is broad: research note section 1);
  `Remove-LocalGroupMember` and another sign-out undo it;
- a CI runner (Stage 2) with a throwaway administrator account and UAC's token filtering on, read back first: the
  premise is about an administrator's filtered token, so a runner with UAC off would pass without testing it.
  Frames there need the same stand-in for a game as E3.

Should the premise fail, `trace_rights()` answers `filtered`, which keeps setup from asking for a sign-out that would
not help.

## Stage 1: public repo and CI

1. Scrub personal identifiers from the tree (16 lines in 4 research notes carry this PC's user name in paths) and
   decide how the history goes public: rewritten to drop them (commit ids change; the docs quote some), or one fresh
   commit with the full history kept locally. The owner decides; nothing is pushed before that.
2. Create the GitHub repository (owner's go), push, and add a `SECURITY.md`.
3. CI on GitHub-hosted Windows runners: the unit tests (the live tests that need this laptop's GPU or PresentMon
   skip with their reason), SHA-pinned actions, least-privilege `permissions:`. Standard runners are free for public
   repositories [verified: GitHub Actions billing page, 2026-11-06]; a private repository on GitHub Free gets
   US$12 a month of compute, about 1,200 Windows minutes.

## Stage 2: the service experiments, on CI

GitHub's Windows runners are disposable virtual machines with administrator rights, so the experiments that need a
throwaway service never touch the owner's PC.
- **E3**: a throwaway service as `NT AUTHORITY\LocalService`, privileges trimmed to SeChangeNotifyPrivilege and a
  restricted service SID (both read back from the running token), runs the pinned PresentMon and `lag-doctor-net`:
  every provider enables, the session starts, Kernel-Network events arrive. If something is refused, a second run
  without the restricted SID tells whether that SID is the cause. Open: a runner has no GPU, so getting real
  presents needs a WARP-rendered D3D11 app or similar.
- **E3b**: the named-pipe trigger starts the service, it stops itself when idle, and the next connect starts it again.
- **E3c**: python.org's embeddable runtime runs as an SCM service through ctypes, with `sys.path` holding only its
  own folder, and every `.exe`, `.pyd` and `.dll` in it signed (or the service becomes a small native helper).
- **E1 on the runner** too, for comparison with the owner's Windows 11 Home result.

## Stage 3: the MSI

- A build backend and a `lagdoctor` console entry point in `pyproject.toml` (today `package = false`).
- Replace psutil, whose native module is unsigned (Smart App Control would block it): the product has one call
  site each, `capture/presentmon.py` (`pids`), `capture/netflows.py` (`net_if_addrs`) and `capture/inventory.py`
  (`virtual_memory`), all available from ctypes. The tests may keep it as a dev dependency for their independent
  readouts.
- WiX v7 per-machine MSI built in CI: the embeddable Python, lag-doctor's code, the byte-identical Intel-signed
  PresentMon 2.6.0 with Intel's licence text, `lagdoctor` on PATH, and lag-doctor looking for PresentMon under
  Program Files first. CI installs, runs and uninstalls it on the runner (part of E7).
- What SignPath requires, prepared in the repo: a "Code signing policy" section with "Free code signing provided by
  SignPath.io, certificate by SignPath Foundation", a privacy statement (lag-doctor sends no data anywhere; while
  recording it sends pings to the router, the internet and the game server, which `--no-network` turns off), the
  same product name and version in every signed file, and the MSI's uninstall.

## Stage 4: the capture service

- WiX `ServiceInstall` with `Account="NT AUTHORITY\LocalService"`, a quoted Program Files path, required privileges
  trimmed, a restricted service SID (if E3 passed with it), the named-pipe trigger and idle self-stop; users never
  get change-config or stop.
- The pipe: explicit DACL, PIPE_REJECT_REMOTE_CLIENTS, FILE_FLAG_FIRST_PIPE_INSTANCE; the CLI connects at
  identification level; one recording at a time; a protocol version handshake.
- The target must be in the caller's own session, checked with the session id from `winapi/ntquery.py`'s
  handle-free snapshot (ProcessIdToSessionId would open the game). Only that process's frame rows and network
  events are relayed. No file writes; nothing loaded from a user-writable folder.
- The CLI uses the service when it answers and falls back to `watch --elevate` when it does not (git checkouts).
- E8: each guard ships red-first, as every guard here does.

## Stage 5: release

1. An unsigned preview release of the MSI, service included, on GitHub Releases (SignPath signs only a project
   "already released in the form that should be signed").
2. Apply to SignPath Foundation. Its approval is manual and may be declined for reputation; then releases stay
   unsigned, and the README says what SmartScreen and Smart App Control will show.
3. With SignPath: signing in the release workflow, each request approved by the owner.
4. winget: a hand-written first manifest (`wix`, machine scope, `elevationRequired`), then automated updates.
5. Before announcing: E5 (Administrator protection on), E6 (a Smart App Control machine), E7 (winget from elevated,
   unelevated and standard-account shells, and Windows 10).
