# The lag-doctor app: the plan

Goal (DECISIONS 2026-11-08): lag-doctor as a desktop app you start like Steam or any other game launcher. It has its
own icon in the Start menu and on the taskbar and its own window, and its library lists your games. Play starts a game
with lag-doctor recording it, and the results open in screens you can explore and compare. The owner asked for the plan
first and said go on 2026-11-08: M0's spike passed the owner's own run the same day, so M1 can begin. Facts and
sources are in [research/desktop-app.md](research/desktop-app.md). The recording side builds the capture service's
protocol early ([INSTALLER-PLAN.md](INSTALLER-PLAN.md), Stage 4).

## Status (2026-11-08)

- [x] Decided by the owner: a desktop app, not a page in a browser, and it launches the games (DECISIONS 2026-11-08).
- [x] Researched: the window technology, launching and finding a game without touching it, the screens, and the
  record flow against main at 4f081ad (research note).
- [x] The owner said go (2026-11-08).
- [x] M0: the window spike (`scripts/webview_spike.py`) passed the owner's own run on 2026-11-08: 34 passed, 0
  failed, 0 not run. The first two runs never counted step 2 (keys and right-click); the owner chose to rebuild it
  (DECISIONS 2026-11-08, PR #20), and the third run completed it (research note, section 11).
- [x] M1: the app with your results: `lagdoctor app` and the MSI's Start-menu shortcut, merged 2026-11-08 (PR #21).
  Its window tests drive the real window on a demo library, here and on CI's runner; the owner tried `lagdoctor app`
  in their own terminal before the merge ("It works, merge it").
- [ ] M2: the recording seams. The locked pipe is done (`lagdoctor/winapi/pipe.py`, PR #19, 2026-11-08), and so are
  tool roots whose whole tree joins `tool_pids` (M3 needed them for the relay); the other seams are not.
- [x] M3: the relay (`lagdoctor/relay/`, 2026-11-08). `watch --elevate` records in the player's own window; only
  PresentMon and the network trace run elevated, in a windowless relay (DESIGN.md, "The relay").
- [ ] M4: recording from the app.
- [ ] M5: measuring the right part.
- [ ] M6: Play.
- [ ] Stage 4 (INSTALLER-PLAN): the relay becomes the capture service, and the prompt goes away.

## What it looks like

Seven screens, all built from data already on the PC. Nothing is downloaded.

1. **Library**: cover tiles for installed Steam and Epic games, for games added by hand and for games recorded before.
   It has search, All / Installed / Recorded filters, a Recent row and a one-line readiness strip ("Frame timing ready
   · each recording asks Windows once").
2. **Game page**: the art, the name and the store; one big **Play with lag-doctor** button with the length it measures
   and the two hotkeys; the last recording's summary on top (verdict, average FPS, 1 % low, stutters a minute, top
   cause); the game's recordings, any two of which can be compared.
3. **Running state**, on the game page. It moves through Launching, Waiting for the game, Startup, Measuring 07:12 of
   10:00, Measured and Analysing, with Cancel or Stop. It stays static while a game records: no live numbers and no
   Mark button, because a window in front of the game changes what is measured (in the first real capture, 2 of 3
   marks fell while another window was in front: FIRST-CAPTURE-REVIEW.md).
4. **Results**:
   - the verdict and the causes with their evidence, in the reports' own words (`report/labels.py`);
   - a zoomable frame-time chart showing the startup band and the marks;
   - NVIDIA clock, temperature and throttling, which the static report does not chart; CPU and GPU; ping;
   - a table of lag moments (click one to see the CPU, GPU, disk and network around it);
   - checked and fine; not measured.

   Fixes are shown read-only, with their `lagdoctor fix` command, in the first versions. The command appears only on
   the last recording made, of any game: `fix` takes its offers from that one and the last check-up, so on an older
   recording it would act on another's. `report.html` stays the shareable page.
5. **Compare**: two recordings of a game side by side, with their conditions next to the numbers: power plan or mode,
   refresh rate, frames source and measured length.
6. **Add a game**: pick an exe; the name and icon come from the file. lag-doctor never starts an exe itself (see
   "Launching and finding the game"), so a game added by hand, or recorded before from another store, has no Play.
   Its page offers the folder wait instead ("start the game yourself; lag-doctor finds it"), as `watch --wait-folder`.
7. **First run and settings**: readiness, the hotkeys, a sound test, the default length, the library sources and the
   sessions folder.

## Play with lag-doctor, step by step

1. Click Play. A pre-flight check takes about a second: frame timing is ready, no other recording runs, and the store
   client is running ("Start Steam, then press Play" otherwise).
2. Windows asks for permission once, now, on the desktop, before the game exists. A member of Performance Log Users
   skips this for frame timing only: the game-server tracker still needs it. Once Stage 4 ships, nobody sees it.
3. lag-doctor asks the store to start the game and minimizes itself.
4. It waits, without opening any process, for a process from the game's install folder that has a window and draws.
   Then it plays a "ready" chime.
5. The player gets through shader preparation, menus and loading, then presses **Ctrl+Alt+S** once in gameplay. A
   rising chime plays and the measured window starts: 10 minutes by default. Ctrl+Alt+L marks a lag, with a blip.
6. When the window ends, a falling chime plays: play on, recording has stopped.
7. When the game closes, the save and the analysis run. The window comes back, or flashes on the taskbar if Windows
   will not bring it forward, with a "results ready" sound. The new recording is at the top of the game page.

## How it works

### The window

- **What it is.** lag-doctor's own top-level window, created by its own Python: `pythonw.exe -m lagdoctor app`, the
  PSF-signed runtime the MSI already installs. The window holds the WebView2 control, driven through ctypes.
  - WebView2 is the Chromium engine Windows provides for apps: preinstalled on Windows 11 and present on almost every
    Windows 10 PC.
  - The screens are HTML, CSS and plain JavaScript, with charts from uPlot 1.6.32 (MIT, 51 KB, vendored and pinned by
    SHA-256). Its LICENSE ships beside it: the minified file carries no copyright notice, and MIT requires one.
- **Why HTML screens.** The big launchers are built this way: Steam draws its windows with an embedded Chromium, GOG
  Galaxy and the EA app pair Qt with one, and Heroic is Electron. Playnite is the exception: native WPF, with CefSharp
  only for its web views. The app is a desktop window whose screens happen to be HTML. It is not a browser tab: there
  is no browser window, address bar or Edge profile.
- **No server.**
  - The bundled files load from a virtual host: `app.lagdoctor.example` is mapped to the app's folder in the install,
    with access DENY.
  - Commands and results travel as JSON web messages, and the sender's origin is checked on every message.
  - Text from a library file, a capture or the PC (game, process and window names) reaches the page as text
    (`textContent`), never as HTML. A Content-Security-Policy meta tag lets the page load nothing from outside
    lag-doctor's own hosts, and no workers (`worker-src 'none'`). A script slipped in through a game's name would run
    in the app's own origin and pass the origin check, so M1 tests it: a game and a process named like markup appear
    as text, and `innerHTML` in their place turns the test red.
  - Large frame arrays go through a shared buffer.
  - Cover art comes from a second host that the app answers itself (`WebResourceRequested`), by library id only, with
    bytes it read from a store's cache or an exe and recognised as an image. Those folders are never mapped.
  - Everything else is off: navigation to any other address, new windows, permission requests, downloads, script
    dialogs, DevTools, the default context menu, the browser's keys and zoom. Each handler refuses first and reads the
    event afterwards, so a failure while reading leaves the refusal standing.
- **Opening WebView2.** lag-doctor uses its own small loader: it calls `CreateWebViewEnvironmentWithOptionsInternal`
  in the installed runtime's `EmbeddedBrowserWebView.dll`. webview/webview (MIT) does the same by default, and Wails
  does too.
  - This ships no Microsoft binary, so SignPath's rule against "any proprietary, non open-source component" never
    comes up.
  - Microsoft's reference says the function "may be modified or removed in future versions" and recommends its loader
    DLL. So one create-environment function keeps Microsoft's `WebView2Loader.dll` as the documented swap. That DLL
    ships only if SignPath agrees in writing.
  - Nothing runs a canary yet. The owner's PC, whose runtime updates itself, would meet a break first, and M1's window
    tests catch one on CI as well: GitHub's `windows-2025` runner has the runtime and opens the window (M0's probe,
    2026-11-08). If the window cannot open, the app shows a plain message and opens `report.html` instead; the CLI
    keeps working either way.
- **Identity.**
  - The MSI adds a Start-menu shortcut: target `pythonw.exe`, arguments `-m lagdoctor app`, lag-doctor's icon, and a
    `System.AppUserModel.ID` set through WiX's ShortcutProperty.
  - The process sets the same ID before any window opens, and sets its window icon. The taskbar then shows
    lag-doctor's icon and pins and relaunches the app.
  - Task Manager still says "Python", from `pythonw.exe`'s own description. Only an exe that owns the window would
    change that: the DECISIONS backlog's host exe for the app's window.
- **Start order.**
  1. The ID.
  2. Per-monitor DPI awareness: `pythonw.exe` declares none.
  3. One instance only: a second start brings the first window forward.
  4. Refusals shown in a message box: started elevated, running under another app's private copy of AppData, or no
     WebView2 Runtime.
  5. `faulthandler` to a log.
  6. STA COM, the window, then the environment. Before it is created, WebView2's five own variables are set in-process
     as Wails sets them: `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`, `WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER`,
     `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` and `WEBVIEW2_USER_DATA_FOLDER` empty, and
     `WEBVIEW2_RELEASE_CHANNEL_PREFERENCE` to 0. WebView2 reads them even without Microsoft's loader, and a stray user
     setting could add a debugging port. They go through `SetEnvironmentVariableW`, never `os.environ`, which removes
     a variable it is given empty (M0 measured it). Once the view exists they are put back as they were, so the
     stores and games the app starts later (M6) inherit the user's own environment; a recovery does the same again.
- **Rules for the code.**
  - Every ctypes callback stays referenced for the window's whole life, the environment's options object included,
    and is wrapped so it can never raise. Under pythonw an exception there is silent and leaves the return value
    unset. An access violation that ctypes turns into an `OSError` inside a callback is no ordinary error: it fails
    and stops the run.
  - The UI thread never blocks. Loading captures, analysis and recording run in other processes, or in worker threads
    that post their results to the window, never to the thread: Windows' own modal loops drop thread messages (M0:
    of about 450 posted to the thread during a 2.5 s move, 8 or 9 arrived).
  - The app never calls `OpenDevToolsWindow`: it opens DevTools even with DevTools turned off (M0).
  - A recovery waits for `BrowserProcessExited` before creating the environment again: the data folder is exclusive
    and is free only then. It never retries without the options object.
- **Its folder.** WebView2's data folder is `webview` under lag-doctor's home, `paths.home()`: so
  `%LOCALAPPDATA%\lag-doctor\webview`, and a temp folder in tests, which redirect `LAGDOCTOR_HOME`. The default, next
  to `pythonw.exe` in Program Files, cannot be created. Uninstall leaves the folder, and the README will say so.
- **Privacy.**
  - WebView2 sends Windows' required diagnostic data whatever the app does.
  - The app turns off the embedded browser's own SmartScreen check (`IsReputationCheckingRequired`), which judges the
    pages it opens: those are only lag-doctor's own files, and Windows' SmartScreen is untouched. Crash dumps stay on
    the PC (custom crash reporting) instead of going to Microsoft.
  - The privacy statement changes in the PR that ships the window (see Open questions).

### Three processes, one recording path

| process | runs as | does | never |
|---|---|---|---|
| **app**: the window (`pythonw.exe`) | the user | the library, Play, results, compare; asks for UAC with its own window | records, holds the capture lock, touches the game, draws anything that moves while a game records |
| **recorder**: `lagdoctor watch` in a control mode, started by the app with no window | the user | the lock, readiness, the wait for the game, the inventory, the Session (system sampler, NVIDIA, pings, hotkeys), save, analysis, report; status to the app | elevates, opens the game |
| **relay** | an administrator, through one UAC prompt, today; the LocalService capture service from Stage 4 | runs PresentMon on the approved game and the Kernel-Network session, and forwards only that game's frame rows and network events (plus the recorder's own, for the tracker's DNS self-test) | writes a file, takes PresentMon's or ETW's arguments from the pipe, serves two recordings at once |

- **The app never runs a Session.** A crash or a closed window cannot lose a recording, and the samplers do not share
  an interpreter with the UI. In the CLI, the recorder runs in the user's own console: one recording path.
- **The relay decides from what the prompt approved.** Its command line, approved at the prompt, carries:
  - a random pipe name;
  - the recorder's pid, create time and SID;
  - either the approved running game or the approved install folder.
- **It accepts a game only on its own evidence.** In its own handle-free snapshot, the proposed game must be in the
  same Windows session as the relay and the recorder. Then it must either be the approved running game, matched by pid
  and create time (M3's mode, where the game started before the relay), or have started after the relay and run from
  under the approved folder (Play, M6). One `accept_target` predicate in `target.py` is called by both sides, so they
  cannot drift: M3 ships it with the running-game rule, and M6 adds the folder rule.
- **It talks only over a pipe it creates**: first instance only, remote clients refused, a DACL for the recorder's SID,
  a medium integrity label, and the client's pid and session checked. Installed, it reads nothing the user can write
  after approval: its code and PresentMon are under Program Files. From a git checkout its code is the user's to
  change, as today's elevated child's is (DESIGN, Privileges and elevation), and its PresentMon is the hash-pinned
  per-user copy.
- **This is the capture service's protocol** (INSTALLER-PLAN Stage 4: the target in the caller's session, one game's
  rows and events, no file writes), built once. Stage 4 moves the same core into the service and changes its policy:
  a fixed pipe name its trigger starts, the target judged by the caller's own session, and the client checking the
  server's pid.
- **What counts as lag-doctor's own in the analysis**, so it is never named as a cause or counted as background CPU:
  - the app, by its exact pid and create time;
  - its WebView2 group, about six processes per window (measured for other apps);
  - the relay and its PresentMon.

  A store client the app happened to start does not count. The GPU share of other programs (`gpu_other_pct`) still
  counts every process but the game, the app's WebView2 GPU process included. So while a game records, the app hides
  its view and suspends it (`put_IsVisible` off, then `TrySuspend`, from M4 on), and the running state's clock moves
  only while the window is shown.
- **Network traffic per program comes only from the player's own session** (the owner's choice, DECISIONS
  2026-11-08). The relay forwards the game's network events in full, plus the recorder's own for the tracker's DNS
  self-test (it matches its packets by the recorder's pid). For every other program it passes back only bytes received
  and sent, and only for processes in the recorder's Windows session. So `bandwidth_hog` can still name a program of the
  player's, but services (session 0) are not among them, and ruling a download out also needs the adapter's own
  counters.

### Launching and finding the game

- **Only through the store**, opened with ShellExecuteEx without `SEE_MASK_NOCLOSEPROCESS`, so the store creates the
  game and lag-doctor never holds a handle to it:
  - Steam: `steam://rungameid/<appid>`, with no arguments;
  - Epic: `com.epicgames.launcher://apps/<namespace>%3A<item>%3A<app>?action=launch&silent=true`.
- **Never the game's exe.** ShellExecute of an exe runs CreateProcess inside lag-doctor, which would make lag-doctor
  the game's parent with full access to it. Anti-cheat bootstrappers, Steam's DRM and Epic's online services need
  their launcher anyway.
- **The library files are untrusted**: every local account can write them.
  - Each URI is built from a fixed template, and only after every field passes a strict grammar (Steam: digits only).
  - Library paths must be plain local-drive paths, the rule netmon already applies to the Steam log.
  - File sizes and nesting depth are capped.
  - The parsed library stays in the app; the page names entries only by id.
- **The store client must already be running**, checked handle-free in the user's own session. Otherwise opening the
  URI would make the client lag-doctor's child. Starting it through Explorer is a later option.
- **The wait.**
  - For each new process created after the click: its image path, read once without a handle.
  - A PDH sample runs only once a process from the folder owns a visible window.
  - The same process must draw, with at least 2 % 3D use, on two samples in a row.
  - Stubs, anti-cheat bootstrappers and Steam relaunches never draw, or they exit, so they are skipped.
  - A launcher in the folder that draws can still win. The recorder therefore re-arms when the chosen process exits
    within about 20 s, and shows a picker when two qualify.
  - A deadline of a few minutes ("Steam may be updating it") and Cancel.
- **The handle check grows.** `tests/test_no_handles.py` learns to refuse Popen, CreateProcess*, ShellExecuteEx with
  NOCLOSEPROCESS, and `os.startfile` outside an allow-list, red-first.

### Measuring the right part

- **The problem.** A recording that starts when the game first draws includes shader preparation, splash screens,
  menus and loading. Today every statistic, baseline and rule covers the whole capture. Among them: the target FPS
  drops are judged against, the stutter baseline, attribution's quiet windows, shader_compile's first third, and the
  CPU speed level.
- **The remedy.**
  - Record from the moment the game is found, but analyse from the press of Ctrl+Alt+S, for a fixed window: 10
    minutes by default.
  - "Until I quit" is a choice that shows its memory cost. It is capped at 1 hour until compact frame storage exists
    (DECISIONS backlog).
  - The startup shows as a shaded band and is never counted.
  - Results have trim handles for a late or missed press. A guessed start is only ever proposed, labelled as a guess.
  - No press at all, from a missed key or from a game that turns off other programs' hotkeys while it has focus
    (`RIDEV_NOHOTKEYS`, research note section 7): the time before a press is capped (20 minutes in the first version,
    a feel number), and Results analyse nothing until the player sets the start.
- **One pure seam**: `crop(cap, t0, t1)` returns a Capture cut to the span, and then the unchanged `diagnose()` runs.
  - shader_compile counts its first third from the span's start.
  - Per-program network totals become None, because they cover the whole recording.
  - The charts and the header say which part was analysed.
  - The CLI gets it too: `lagdoctor analyze CAPTURE --from S --to S`. The capture file always stays whole.
- **Feedback during play is sound**: ready, start, mark, done and problem. `winsound` ships in the embeddable runtime.
  Taskbar progress and a flash help only outside the game. No toasts in the first version: Do-not-disturb sends them
  to the notification center while a game is in front.

### Library, art and demo data

- **Library**: Steam (the SteamPath registry value, then `libraryfolders.vdf`, then `appmanifest_*.acf`, installed
  when StateFlags & 4) and Epic (`%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests\*.item`). Also games added by
  hand, and games recorded before.
- **Art, never downloaded**, in this order:
  1. Steam's own `appcache\librarycache`. It is undocumented and its layout changed in 2026, so the lookup searches
     each app's folder for the portrait names and detects the format from the bytes.
  2. GOG Galaxy's local cache, best effort.
  3. The exe's own icon, read from the file's resources, never from the process.
  4. A generated tile.
- **Demo data only in the repo.** Screenshots and fixtures come from a demo library: made-up games, generated tiles,
  synthetic or LagLab sessions. The repo is public, and a library page shows what its owner owns.

## Milestones

Each one is a pull request carrying its tests (red-first), its docs and its DESIGN.md changes. Other sessions often
edit the analysis and the docs, so re-read main before cutting each one.

### M0: the window spike

`scripts/webview_spike.py` (stdlib ctypes only, built 2026-11-08) opens lag-doctor's window with WebView2 through the
own loader. Its vtable slots and IIDs are counted from Microsoft's `WebView2.h` (SDK 1.0.4314.35), whose 297
interfaces agree with the package's `WebView2.idl`. Each check prints PASS, FAIL or NOT RUN with what it measured, and
a check that needs a control runs it: a control that does not fail as predicted fails its check.
- The runtime and the export are found, and the DLL and `msedgewebview2.exe` are both signed by Microsoft.
- WebView2's own environment variables are set in-process before the runtime loads, and are back as they were once
  the view exists. The controls: the reader tells an absent variable from an empty one, and an empty `os.environ`
  value removes the variable instead.
- Window, environment, controller and first page are up within 10 s of the process starting.
- The runtime asks lag-doctor's options object (a data folder of its own, crash dumps kept on the PC), and asks again
  after a recovery; every setting reads back as set, before and after.
- The view fills the window at the display's scale, Windows' text size included.
- Assets come from the virtual host, and none of 9 path escapes reads the file beside its folder.
- No listening TCP socket or bound UDP endpoint in the app or its WebView2 processes. The control: a listener, a
  connected pair and UDP endpoints of the spike's own are read back with their states. WebView2's own outgoing
  connections are counted, not failed.
- The page's CSP blocks an inline handler and an outside fetch, each with its violation report.
- 1,000 messages round-trip in order from the right origin; p50 and p95 latency printed.
- A window watch sees browser-owned windows. The control: DevTools, opened on purpose and closed.
- Navigation to another site, to another page of its own or to another app is refused; so are new windows,
  permission requests (with no prompt), downloads (and no file is written) and script dialogs.
- Worker results posted to the window during Windows' move loop, entered through `SC_MOVE` and left with Esc, all
  arrive. The control: posted to the thread meanwhile, nearly all are lost.
- The view hides and suspends. The control: hidden but not suspended, the page's timers still run.
- A browser process the spike ends itself is recovered within 10 s.
- Under pythonw, a guarded handler's exception is logged and the next message works; an unguarded one goes nowhere.
  The control: the same unguarded raise under python.exe reaches stderr.
- The drop-refs sabotage: kept, a handler answers 20 of 20; with its references dropped, it breaks (here, an access
  violation ends its process). So the keep-alive matters.
- Closing the window ends every WebView2 process within 5 s (one run of eight before the review took 5.16 s; each
  process's exit time is now recorded, to show which one lingers if it happens again).
- No access violation anywhere in the spike's own process.
- The footprint (process count and memory) is recorded twice: at the first page and after the page checks.

The owner's run adds what only a person can do or see: drag the window, press the browser's keys (no F-keys: on many
laptops they are hardware keys) and right-click, change the display scale or move the window to a second monitor (the
spike reads the view while the new scale is on, and reminds the owner to set the scale back), and say what the
taskbar shows. The owner runs it in their own terminal. `--auto` runs the automated checks alone (agents and CI);
`--rehearse` walks the four steps with automatic answers, so their code runs without a person and checks nothing. A
run cut short (the window closed, Ctrl+C) says so and exits 4, listing every check it never reached.

Step 2 (keys and right-click) lists each action on its own line, Alt+Left last, and ticks each off as it arrives. A
Ctrl key counts when the page or the view's AcceleratorKeyPressed event saw it, and "Nothing happened" counts with 6 of
the 7 Ctrl keys and a right-click; Alt+Left is reported, never counted, since it can show no effect here. Any
navigation, zoom change or browser window during the step fails it, and Skip keeps the record. The record holds every
key press the view saw, the page's keys and right-clicks with their times, and the window's focus, menu and key
messages, so the result says what followed Alt+Left: whether Windows' menu mode started and the view lost the
keyboard (research note, section 12). On GitHub's runner, and only there, CI's rehearsal types the actions with
SendInput, then a letter after Alt+Left, and adds a check of its own (`keys-typed`) that fails unless every typed
action arrived and nothing acted. CI's probe
(`.github/workflows/webview-spike.yml`) asks whether the runner has the WebView2 Runtime and can open the window; a
"no runtime" answer counts only when a second reading (the runtime's registry version and its exe) agrees. It does:
on PR #18 the runner had 156.0.4314.58, and all 30 automated checks passed there.

The gate: the owner's run passes on their PC, the drop-refs sabotage fails as predicted, and no control misses.
Otherwise the owner decides on the fallback (DECISIONS 2026-11-08): a small host exe showing the same screens. It is
compiled code with a second toolchain, so it would reverse "Python with ctypes, no compiled code" (DECISIONS
2026-11-04). Passed on 2026-11-08, on the owner's third run: every check, every control and all four of the owner's
steps, so the fallback is not needed.

### M1: the app with your results

`lagdoctor app` and the MSI's Start-menu shortcut. Screens 1, 2, 4, 5 and 7, without Play: recorded games, a game's
page with its recordings (where Results and Compare start), Results, Compare, Settings. Screen 3 comes with M4, and
Play and screen 6 with M6. It needs:
- `lagdoctor/winapi/webview2.py`, with its vtable slots and IIDs pinned in a test against Microsoft's `WebView2.idl`
  (fetched by `tests.downloads` with a SHA-256 pin);
- `lagdoctor/app/`: the window, the bridge, the view-models and the assets;
- a small session summary written at analysis time (game, exe, start, tool version, kind), so the library never loads
  every capture. Older sessions fall back to the folder name;
- `analysis.window(cap, t0, t1)` for the samples around a lag moment, and labels for every column a chart can show;
- `app` in `cli.PRIVATE_COPY_POLICY` among the commands that refuse, and a CLAUDE.md rule that an agent never starts
  the app: its shell has the private copy, and from M4 on the app raises a UAC prompt. The spike keeps its data in
  `%TEMP%`, which the private copy leaves alone, so an agent may run its `--auto` and `--rehearse`; its interactive
  run is the owner's.

Built (2026-11-08), as above, with these choices and limits:
- Results read each session's saved `diagnosis.json` back (`Diagnosis.from_json`) and analyse the capture again only
  when it cannot be read: the screen shows what the report showed, and a long capture is not analysed twice.
- The summary holds more than who and when: the verdict's numbers and the conditions Compare shows (power plan and
  mode, refresh rate, how frames were timed, measured length), so the library, a game's page and Compare read no
  capture. Older sessions have none: they show by folder name, their numbers come from `diagnosis.json`, and Compare
  shows their conditions as "-".
- The library holds recorded games only: All and Recorded filters, no Installed one until Play brings the stores
  (M6). Tiles are generated (initials on a colour from the name); the Recent row shows once there are more games than
  it holds.
- Settings show readiness, the lag key, a sound test (Windows' own asterisk sound: the recording sounds come with
  M4 and M5), the library's sources and the sessions folder with a button that opens it. There is no setting to
  change yet: the default length and the start key come with M5.
- The page names sessions and games only by id; the app answers on two worker threads, at most 32 requests waiting.
  The window comes back from a crashed page (the entry page again) and from an ended browser process (a new
  environment once BrowserProcessExited says the data folder is free), both driven in its tests.

Tests:
- view-models in plain pytest;
- the page driven through the host's own script hook, behind a declared `webview2` capability, which CI's runner has
  (M0). WebDriver for WebView2 has been broken since runtime 133;
- the bridge's origin and navigation policy as pure functions, with negative cases;
- names with markup shown as text (the `textContent` rule above);
- `test_user_text` extended to the app's HTML, JS and CSS;
- the uPlot pin, and its LICENSE beside it;
- the MSI check reads the installed shortcut's AppUserModel ID back.

### M2: the recording seams (no change in behaviour)

- An external stop, `say` and `interactive` for `cmd_watch` and `_record`. Session observers (`on_tick`, `on_event`),
  contained the way the printer is. A public `Session.mark()`.
- Tool roots: processes whose whole tree joins `target["tool_pids"]` (lag-doctor's own, kept out of blame), sighted
  again on every status tick: each WebView2 browser process of the app, the relay, and its PresentMon. The app itself
  joins by its exact pid and create time, never as a root, so a store client or a browser it starts stays countable.
  A note when the PID cap is reached.
- `launch_elevated(..., hwnd=)`, so the UAC prompt belongs to the app's window.
- `lagdoctor/winapi/pipe.py`: thin wrappers for the locked pipe (created with its DACL and label; connect; read and
  write with a size cap) and for the client's and server's pid and session queries. The checks that use them live in
  `lagdoctor/relay/` (DESIGN.md keeps policy out of `winapi/`). Done 2026-11-08: its interface is in DESIGN.md
  (winapi interfaces) and what each lock was measured to do in `docs/research/install-and-rights.md`, section 11.
  The relay builds its descriptor with `client_dacl([recorder SID])`, makes the pipe under a random name, compares
  `client_pid()` and `client_session()` with what the prompt approved, and the recorder compares `server_pid()` with
  the relay it launched and checks `owner_sid()`.

### M3: the relay

`lagdoctor/relay/` (protocol, server, client) replaces today's elevated child for `lagdoctor watch --elevate` on a
running game.
- The recording comes back to the user's own window: the live line and Ctrl+C.
- Frame timing and the game-server tracker are relayed.
- Gone: the inventory handover file, a capture written by an administrator, and the second console window.
- Installed, a Ctrl+C stop in that window now ends with cmd's "Terminate batch job (Y/N)?". The owner accepted that
  cost of the `.cmd` shim as rare while recordings ran in their own window (DECISIONS 2026-11-08, the MSI); here it
  comes due before Stage 4. The backlog's launcher exe removes it.
- About 31 red-first anchors on the child protocol in `cli.py` move or retire.

Done 2026-11-08. The relay runs under `pythonw` (no window; the launched process is the relay itself, never a venv's
launcher), writes no file, and checks the recorder and the game on its own snapshot with `target.accept_target`, which
the recorder calls before the prompt. The recorder proves who answered: the server's pid is the relay's, and the
pipe is owned by Administrators, which Windows refuses an unelevated token (error 1307, measured). PresentMon in the
relay forwards the game's rows and keeps none; its frames reach the capture exactly as a local run's do (a gold
replay through the relay gives the same columns). Per-program traffic comes only from the player's session
(DECISIONS 2026-11-08), so `bandwidth_hog` rules a download out only with the adapter's counters. On GitHub's runner,
where UAC is off, `tests/test_relay_live.py` starts the real relay through `launch_elevated` and checks real
PresentMon and Kernel-Network data come back. Three adversarial reviews (privilege boundary, concurrency, integration)
raised 21 distinct findings. 18 were fixed before merge; two concern Stage 4's service (INSTALLER-PLAN, Stage 4), and
PresentMon still outlives a relay that is killed, as it outlived a killed recorder before. Among the fixes, the game's
and the recorder's pids are now kept by identity and
session, so a number Windows gives to another program never carries its traffic out; PresentMon is never opened
from a share; the relay's stop no longer reads as its trace dying; and the game's own traffic is taken off the
adapter's total before `bandwidth_hog` judges what is left. Of the child's red-first anchors, 43 retired with the
code they guarded and 12 moved; `tests/redfirst/relay.json` holds the relay's own. Still the owner's: one real
`lagdoctor watch --elevate` with a game, to see the prompt, no second window, and frames in the report.

Of the two checks once left to the owner, the first is answered on CI, where the suite runs elevated (High
integrity): a medium client reaches a pipe made there with or without a label, since an unlabelled object counts as
medium, so the recorder never needed the label to get in. The pipe's label is there to keep anything below medium out
entirely (`tests/test_pipe.py`, whose integrity test is named "server at High" in CI's log; install-and-rights.md,
section 11). The owner still checks where the UAC prompt appears when the app's window asks for it.

### M4: recording from the app

`watch --control stdio`: JSON lines both ways between the app and the recorder.
- To the app: ready, elevate, waiting, target, recording, saving, done (with a reason id).
- To the recorder: elevated (with the relay's pid, which the recorder checks against the pipe's server) or declined,
  stop, cancel. Declined, the recording goes on without frame timing and the game-server tracker, as `watch` does
  today, and the running screen says so.
- At done, the app comes back, or flashes its taskbar button if Windows will not bring it forward, and plays the
  "results ready" sound (Play's step 7).

The app gets "Record the game that's running": screen 3, static and with the view suspended while the game records,
the sounds and Stop.

### M5: measuring the right part

- `crop`, plus `analyze --from/--to`.
- The start-measuring hotkey, a second key on the existing listener, with the fixed window and the sounds.
- Trim handles in Results.

### M6: Play

- The library readers and the art.
- The store launch: a policy module that builds each URI from its fixed template, requires the store client to be
  running and refuses when elevated, over a thin `winapi/launch.py` that only calls ShellExecuteExW without
  `SEE_MASK_NOCLOSEPROCESS` (DESIGN.md keeps policy out of `winapi/`). The `test_no_handles` extension ships with it.
- The folder wait: `in_folder` and `wait_for_target` in `target.py`, and the folder rule in `accept_target`, with
  `find_target` untouched.
- The relay's folder policy, so the prompt comes at Play, before the game starts.
- Screen 6, and the folder wait for games without Play. The CLI gets it too: `watch --wait-folder`.

### After that

- **Stage 4** (INSTALLER-PLAN): the relay core becomes the LocalService service. The recorder tries the service first,
  and there is no prompt.
- **Later**:
  - GOG, Xbox and other stores;
  - Apply and Undo in the app, once the fix flow has moved out of `cli.py`;
  - a plain Play without recording;
  - starting the store client through Explorer;
  - a live view on a second monitor;
  - the backlog's host exe for the app's window, so Task Manager says lag-doctor.

## Open questions for the owner

- **Feel numbers**, to judge on screen: the 10-minute default window, the start key (Ctrl+Alt+S), the 20-minute cap
  before a press, and the 1-hour cap for "until I quit".
- **Fixes from a trimmed span.** Should a trimmed span feed `lagdoctor fix`? Today `analyze` never does.
- **Two forks the planning session settled** (DECISIONS 2026-11-08), open to the owner:
  - measuring from a start key, rather than the whole recording or a fixed startup skipped per game;
  - refusing Play while the store client is closed, rather than starting it through Explorer.
- **The privacy statement.** It gains a sentence about WebView2's diagnostic data, and SignPath requires the
  statement. Its terms add that software which "collects user data and transfers it to systems not specified by the
  user" must show that policy during installation, with an option there to turn it off. The MSI has no dialogs, and
  WebView2's required diagnostic data cannot be turned off, so ask SignPath whether the clause covers the runtime's own
  data. M0 adds a measurement: the page itself cannot reach the network, but in one run of five a WebView2 process
  held an HTTPS connection (port 443, closing) at the first reading. What the runtime contacts on its own belongs in
  the statement too.
- **The loader DLL.** If the own loader ever breaks: ask SignPath whether Microsoft's BSD-licensed, binary-only
  `WebView2Loader.dll` may ship inside the signed MSI.

## Never

- Start the game's exe, or hold any handle to the game. PresentMon's query handle stays the one exception.
- Run the app elevated, start the recorder elevated from the app, or start a game from an elevated process. `watch`
  typed in an administrator's own window still records there, as today (DESIGN, Privileges and elevation).
- Download anything (art, store data) or open a network socket from the app.
- Put the owner's games, library or sessions in the repo: demo data only.
