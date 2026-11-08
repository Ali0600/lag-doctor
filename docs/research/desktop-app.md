# A desktop app for lag-doctor: window, launching and recording

Research date: 2026-11-08. Question: how can lag-doctor become a desktop app the owner starts like Steam? That means an
icon, its own window and a library whose Play button starts a game with lag-doctor recording it. Its screens must
also show results that can be explored. It has to do this without breaking the project's rules: the standard library
only at runtime, signed binaries only, never touching the game. The plan built on it is
[../LAUNCHER-PLAN.md](../LAUNCHER-PLAN.md).

Method: three research workflows. The first used 8 agents: 4 mapped the code and 4 swept the web. The second used 4
adversarial reviewers on a first, web-page draft. The third used 4 agents on the desktop-app question. A separate reader
re-checked the key claims (verification log at the end).

The first two rounds read main at b472ba4 and the third read 4f081ad. Code references below name functions, not
lines. Measured on the owner's PC: Windows 11 Home, build 26200.

**Tags.**
- `[verified: …]`: re-read at that source by a separate reader, or observed by the local command named.
- `[research: …]`: found at that source by a research agent, not re-read since.
- `[inferred]`: reasoned, not observed.
- `[unverified]`: could not be checked.

## 1. What game launchers are built with

- Steam's client draws its windows with an embedded Chromium (`steamwebhelper.exe` on `libcef.dll`). Valve's notes:
  "Updated embedded Chromium build in Steam" [research: store.steampowered.com/news/101607; the files observed
  read-only].
- Playnite is WPF on .NET Framework with CefSharp for its web views [research: Playnite's `.csproj` files on GitHub].
- Heroic is Electron, "built with Web Technologies" [research: its `package.json` and README].
- GOG Galaxy, the EA app and Battle.net pair Qt with an embedded Chromium [research: their install folders read-only;
  Blizzard forum post of 2023-10-03 on Smart App Control blocking Battle.net's unsigned `Qt5Gui.dll`; WineHQ bug 43910].

So "like Steam" means HTML screens drawn by an embedded browser engine inside the app's own window, with the app's
own icon. It is not a browser tab. lag-doctor's first draft used the owner's Edge as its window; that was the flaw.

## 2. Window technologies under lag-doctor's constraints

| option | feels like an app | new binaries | stdlib-only | verdict |
|---|---|---|---|---|
| WebView2 in lag-doctor's own window, driven by its Python through ctypes, with its own loader | yes: own window, icon, taskbar ID | none | passes | **chosen, gated by the M0 spike** |
| A small C# host (.NET Framework 4.8.1, preinstalled on Windows 11) with Microsoft's WinForms WebView2 control; Python as the backend | yes; Task Manager says lag-doctor | our exe (needs SignPath) plus 3 binary-only Microsoft DLLs | the Python side only | fallback if M0 fails |
| A C++ host on webview/webview (MIT, own loader), built in CI from our source | yes | our exe (needs SignPath) | n/a | fallback; fits the backlog's own-launcher-exe item |
| Edge `--app` window plus a localhost server | no: Edge's window, an "Open browser window" menu item that cannot be hidden, Edge's first run | none | passes | rejected (section 8) |
| tkinter with Tcl/Tk copied in | dated look; Tk cannot decode JPEG cover art | about 10 MB of Tk files | fails the scan (PR #16) | rejected |
| Pure Win32 through ctypes | everything hand-drawn | none | passes | rejected: no DOM to test, charts by hand |
| pywebview, PySide6, Electron, Tauri | yes | unsigned or prebuilt binaries, or 124-158 MB | fail | rejected |

Facts behind the table:
- **WebView2 Runtime.** It is preinstalled on all Windows 11 devices. Microsoft installed it on all eligible Windows 10
  devices, and "a small number of Windows 10 devices don't have" it. Microsoft Edge can be uninstalled without breaking
  it, but Edge Stable cannot serve as a runtime [research: learn.microsoft.com WebView2 distribution page and end-user
  FAQ].
- **The embeddable Python.** The package the MSI ships (3.13.17) has `http`, `webbrowser`, `socketserver`, `ctypes`,
  `sqlite3`, `winsound`, `pythonw.exe`. It has no `tkinter`, `idlelib` or `turtle`, and no `_tkinter.pyd` or
  `tcl86t.dll` [verified: the pinned ZIP's listing, SHA-256 matching `scripts/runtime_pin.py`].
  - Since PR #16, `tests/test_stdlib_only.py` allows only what the pinned runtime itself imports
    (`scripts/runtime_modules.json`, the runtime's own answer), so an `import tkinter` under `lagdoctor/` fails CI
    [verified: main at d7ceb05; `install-and-rights.md`, section 10, holds the measured listing].
- **tkinter's other costs.** Python's full installer "will not be produced for Python 3.16 or later" [research:
  docs.python.org Using Python on Windows]; the install manager's PythonCore packages still carry signed Tcl/Tk for
  3.13 and 3.15 [research: their ZIP listings]. Tk 8.6's photo image reads PNG, GIF and PPM only [research: Tk 8.6
  photo man page].
- **pywebview** needs pythonnet, then clr_loader, then cffi, and cffi's `_cffi_backend` `.pyd` is not
  Authenticode-signed [research: PyPI metadata; Get-AuthenticodeSignature on uv's cached wheel].
- **PySide6** is about 124 MB of wheels; Qt Charts and Qt Graphs are "Commercial or GPLv3" [research: PyPI; doc.qt.io].
- **Electron 45's** win32-x64 zip is 158,182,975 bytes [research: GitHub releases API].

## 3. WebView2 from lag-doctor's own Python

- **The entry point without Microsoft's loader DLL.** `CreateWebViewEnvironmentWithOptionsInternal(bool
  checkRunningInstance, int runtimeType, PCWSTR userDataFolder, IUnknown* environmentOptions, handler)` is "a DLL
  export out of `EmbeddedBrowserWebView.dll`" in the runtime's folder (`runtimeType` 0 is evergreen).
  - Microsoft's note: "This function may be modified or removed in future versions. It is recommended you use
    `CreateCoreWebView2EnvironmentWithOptions` instead."
  - Unlike the loader's function, it does not fall back to other channels or apply the developer environment variables
    and policy registry keys that choose the runtime [verified: learn.microsoft.com webview2-idl reference, version
    1.0.4314.35]. Wails still blanks `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`, `WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER` and
    `WEBVIEW2_RELEASE_CHANNEL_PREFERENCE` before calling the export, because WebView2 itself checks them [research:
    wailsapp/go-webview2 `webviewloader/env_create.go`].
  - The runtime's folder is the `EBWebView` value under `EdgeUpdate\ClientState\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`.
    The installed `EmbeddedBrowserWebView.dll` is signed by Microsoft and exports the function [verified: registry read
    and PE export table on the owner's PC, read-only].
- **Prior art for this path.**
  - webview/webview (MIT, the C/C++ core behind many language bindings) uses it by default and prefers
    `WebView2Loader.dll` when one is present [research: `loader.hh`].
  - Wails v2 defaults to its own Go port of this loader, and Wails v3 removed the native-loader option (PR #6031,
    2026-09-09) [research: Wails repository].
  - Pure-ctypes Python WebView2 hosts exist and use Microsoft's loader DLL. The cleanest, ida-slides (MIT), keeps every
    callback thunk referenced, holds live handlers in a module dictionary, and generates its vtable slots from
    `WebView2.h` [research: GitHub].
- **Microsoft's loader DLL.** `WebView2Loader.dll` must ship with an app that uses it [research: distribution page].
  Copies examined were Authenticode-signed by Microsoft Corporation [verified: Get-AuthenticodeSignature]. The NuGet
  package's licence is a BSD-3-Clause text, and no source is published [research: nuget.org licence page; Eclipse IP
  Lab request].
- **Threading.** WebView2 "must run on a Single Threaded Apartments (STA) thread" with a message pump. All callbacks
  arrive on that thread, and nested message loops inside handlers are unsupported [research: threading-model page].
  - Worker threads post to the window: PostThreadMessage messages are lost in modal loops [research: PostThreadMessageW
    page].
  - WinDLL and WINFUNCTYPE calls release the GIL [research: ctypes docs].
- **Local content without a server.**
  - `ICoreWebView2_3::SetVirtualHostNameToFolderMapping` serves a folder under an HTTPS origin, resolved in the WebView2
    processes. Microsoft advises RFC 6761 names (`.example`), one host per source and the DENY access kind; avoid
    `.local` [research: ICoreWebView2_3 page].
  - `NavigateToString` is capped at 2 MB [research: ICoreWebView2 page].
  - `CreateSharedBuffer` (SDK 1.0.1661.34 and later) hands script an ArrayBuffer [research:
    ICoreWebView2Environment12 page].
  - Microsoft's sample checks a web message's source before acting on it [research: communicate-btwn-web-native page].
- **Vtable order.** It comes from `WebView2.idl` or the C vtables in `WebView2.h`, never from learn.microsoft.com, whose
  member lists are alphabetical [research: SDK 1.0.3719.77 header mirror].
- **Python rules.**
  - An unreferenced ctypes callback object "may be garbage collected, crashing your program" [research: ctypes docs].
  - In CPython 3.13 an exception in a ctypes callback goes to the unraisable hook and leaves the C return value unset.
    Under pythonw the hook prints nothing, because `sys.stderr` is None [research: CPython `callbacks.c`, `errors.c`].
  - `python.exe` and `pythonw.exe` declare no DPI awareness [research: CPython `PC/python.manifest`; the manifests read
    on the owner's PC].
- **Identity.**
  - An app sets `SetCurrentProcessExplicitAppUserModelID` before any UI and puts the same ID on its shortcut; the
    shortcut then supplies the icon and command line for pinning and relaunch [research: learn.microsoft.com
    AppUserModelIDs].
  - An MSI writes the ID through the MsiShortcutProperty table, which WiX's `ShortcutProperty` fills (MSI 5.0+). A
    failed property is only warning 1946 [research: docs.firegiant.com; MSI table docs].
- **User data folder.** The default, `{exe}.WebView2` next to the exe, fails where the process cannot create a folder
  (Program Files). Uninstall does not remove it [research: user-data-folder page].
- **Privacy.**
  - WebView2 always sends Windows' required diagnostic data, and optional data per the Windows setting.
  - SmartScreen is on by default, and leaving it on needs a user notice.
  - Crash dumps go to Microsoft unless custom crash reporting is enabled [research: WebView2 data-privacy page].
- **CI.**
  - The windows-2025 runner image lists Edge but no WebView2 Runtime; maintainers say the runtime is present but "not
    installed or pinned by the runner image" [research: runner-images Windows2025-Readme.md; issues 14738, 9538].
  - WebView2 automation through msedgedriver has been broken since runtime 133 [research: issue 14738 citing
    WebView2Feedback #5415].
- **Footprint.** Another app's WebView2 window ran 6 processes (browser, GPU, renderer, utility, crashpad) at 181-298 MB
  [verified: handle-free process snapshot on the owner's PC].

## 4. Signing and the loader

- SignPath Foundation's terms, verbatim [research: signpath.org/terms, page marked Draft]:
  - "The project may not contain any proprietary, non open-source component."
  - "However, you may include System Libraries in signed packages." (GPLv3 section 1's term)
  - "You may include unsigned binaries of upstream OSS projects, e.g. DLL files, in your signed packages."
  - "The team must only sign software artifacts built from their own source code."
- Reading of the terms [inferred]:
  - The C runtime inside the embeddable Python (`vcruntime140.dll`) is a compiler's runtime, and the WebView2 Runtime
    is an operating-system component lag-doctor does not ship. Both fit System Libraries.
  - `WebView2Loader.dll` is an SDK redistributable without published source, so its status is SignPath's call, to be
    asked in writing before it ships. Calling the runtime's own export ships no Microsoft binary, so the question
    never arises.
- Smart App Control allows Microsoft-signed and PSF-signed code [research: SAC overview]. With the own loader, the app
  adds no binary at all: `pythonw.exe` is PSF-signed, and the runtime's files are Microsoft's.

## 5. Launching a game without touching it

- **ShellExecuteEx of a store URI** starts the scheme's handler with the URI on its command line. Without
  `SEE_MASK_NOCLOSEPROCESS` the caller gets no process handle at all. The handler (a second `steam.exe`, for example)
  passes the URI on to the running client, which creates the game [research: SHELLEXECUTEINFOW docs; URI-scheme docs;
  handlers read from HKCR on the owner's PC; the parent of a store-launched game is unmeasured].
- **ShellExecuteEx of an exe** runs CreateProcess inside the caller. The caller becomes the parent, and CreateProcess
  makes full-access handles [research: SHELLEXECUTEINFOW `SEE_MASK_NOASYNC` text; ICreatingProcess; CreateProcess
  docs].
  - `explorer.exe <path>` working is "basically a bug" per a Shell team developer, as reported secondhand [research:
    Eric Lawrence's blog, 2015].
  - The documented way to have Explorer start a program is the desktop's `IShellDispatch2::ShellExecute` [research:
    Raymond Chen, The Old New Thing, 2013-11-18].
- **Games need their launcher.**
  - `SteamAPI_RestartAppIfNecessary` relaunches a game "through Steam if it wasn't" [research: Steamworks API].
  - EOS games may relaunch through the Epic launcher, which passes auth arguments [research: EOS SDK text].
  - EOS Anti-Cheat reports protection unavailable unless the game "was started using the anti-cheat bootstrapper"
    [research: EOS SDK constant].
  - Playnite once fixed a game "not starting properly" by launching EasyAntiCheat's own executable [research: Playnite
    commit 29f6a6c672].
  - Xbox PC (GDK) games must be app-launched [research: MicrosoftGame.config overview].
- **Store URIs and files.**
  - Steam: `steam://rungameid/<appid>` (Playnite uses it). Valve's own page could not be fetched [unverified]. Users
    report a confirmation prompt when a launch carries extra arguments [research: Steam community, 2024].
  - The Steam folder comes from HKCU `Software\Valve\Steam` `SteamPath`; then `steamapps\libraryfolders.vdf`; then
    `appmanifest_<appid>.acf` (`appid`, `name`, `installdir`, StateFlags with FullyInstalled = 4) [research: Playnite's
    SteamLibrary; Lutris's StateFlags notes, reverse-engineered].
  - A Steam game's exe is not in the manifest, which is why launchers track the install folder [research: Steamworks
    upload docs; Playnite].
  - Epic: JSON `*.item` files in `%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests` (`AppName`, `DisplayName`,
    `InstallLocation`, `LaunchExecutable`, `CatalogNamespace`, `CatalogItemId`). Launch with
    `com.epicgames.launcher://apps/{CatalogNamespace}%3A{CatalogItemId}%3A{AppName}?action=launch&silent=true`
    [research: Playnite's EpicLibrary; legendary].
  - Both the Steam and Epic manifest folders grant BUILTIN\Users full control on the owner's PC [verified: Get-Acl,
    read-only].
- **Tracking the game.**
  - Every Playnite store plugin tracks the game by its install folder. Playnite's own matcher opens each process
    (OpenProcess plus QueryFullProcessImageName) [research: Playnite `ProcessMonitor.cs`, `ProcessExtensions.cs`].
  - lag-doctor gets the image path without a handle, through NtQuerySystemInformation class 88 (`winapi/ntquery.py`).
- **UAC.** A prompt switches the interactive desktop to the secure desktop [research: UAC how-it-works page].
  - DXGI swap chains give up full-screen when their window is occluded [research: DXGI programming guide].
  - So a prompt during play disturbs it [inferred], and the plan raises it before the game starts.
  - A bad or missing HWND can make the prompt only flash on the taskbar [research: CorrectShellExecuteHWND,
    Windows 7 docs].

## 6. The library and art without the network

- **Steam's local art** is in `appcache\librarycache`. The current layout is a folder per appid holding
  `library_600x900.jpg`, `header.jpg`, `library_hero.jpg`, `logo.png` and an icon, with content-hash subfolders for some
  apps [verified: the folder's structure read on the owner's PC, names and counts only].
  - Some `.jpg` files hold PNG data, so detect the format from the bytes.
  - Third-party projects report the change from flat `<appid>_library_600x900.jpg` files in September-October 2026
    [research: GitHub issues 4o66/sunshine-apps-ui#21, Nonary/Vibepollo#554].
- **Epic** manifests carry no images, and its catalog cache lists only https URLs [verified: field shapes read
  read-only].
- **GOG Galaxy** caches WebP covers indexed by `storage\galaxy-2.0.db` (undocumented) [verified: schema read read-only].
- **The exe's own icon** can be read from the PE's `RT_GROUP_ICON` and `RT_ICON` resources without running it. Most game
  executables checked carry a 256 px icon [verified: in-memory parse on the owner's PC].

## 7. Measuring gameplay, not startup

- **What covers the whole capture today** (main at 4f081ad) [research: code read]:
  - in `analysis/frames.py`: the stutter baseline and the frame statistics, the bound verdict's steady stretches,
    `probable_gpu`;
  - in `analysis/events.py`: the FPS-drop target (p90 when uncapped) and stutters per minute;
  - in `analysis/attribute.py`: the quiet-window baselines and each event's chance;
  - in `analysis/rules.py`: shader_compile's first third from t = 0, the CPU speed level and its first two minutes,
    the GPU heat share, the unfocused, uneven and server-cover shares;
  - in `report/html.py`: the charts start at 0.
  - The render gate passes menus: `AUTO_MIN_3D_PCT = 2.0`, "a game renders even in its menus".
- **Prior art.**
  - CapFrameX has a range slider "you can also use to cut a record" [research: CapFrameX README].
  - FrameView, OCAT, CapFrameX and PresentMon start captures by hotkey, use start and stop sounds, and hide their
    overlays while recording [research: their guides and READMEs; see the first round's prior-art notes].
- **In-game feedback.**
  - `winsound` ships in the embeddable runtime. `SND_MEMORY` with `SND_ASYNC` raises RuntimeError [research: winsound
    docs].
  - Windows' Do-not-disturb rules include "When playing a game", and they send notifications to the notification
    center [research: Microsoft support page].
  - Taskbar progress needs the app to own its window (TaskbarButtonCreated) [research: ITaskbarList3 docs].
- **Hotkeys.** A game that registers raw keyboard input with `RIDEV_NOHOTKEYS` turns off application hotkeys while it
  has focus [research: RAWINPUTDEVICE docs].

## 8. Why not a local web server in the user's browser

The first draft served a page from Python's `http.server` on 127.0.0.1 into an Edge `--app` window. Besides not being
an app (section 2), it brought a server to harden:
- CPython's `HTTPServer` sets `allow_reuse_address = 1`. On Windows, a second socket from another account that also
  sets SO_REUSEADDR can then bind the same port. Microsoft: "All server applications must set SO_EXCLUSIVEADDRUSE"
  [research: CPython `http/server.py` and `socketserver.py`; Microsoft's SO_EXCLUSIVEADDRUSE page].
- "Cookies do not provide isolation by port" [research: RFC 6265 section 8.5].
- Under pythonw, `BaseHTTPRequestHandler.log_message` writes to a `sys.stderr` that is None [research: CPython
  `http/server.py`; sys docs].
- Edge's `--app` window keeps an "Open browser window" menu item that cannot be hidden, a fresh profile shows Edge's
  first-run screens unless a policy hides them, and Edge can be uninstalled in the EEA [research: Microsoft Q&A
  1181132; HideFirstRunExperience policy page; Microsoft EU policy blog, 2024-03-07].

A WebView2 window needs no server: the spike found no listening socket in it (section 11), so none of
this applies.

## 9. The relay and its pipe

- `CreateNamedPipe`:
  - with `FILE_FLAG_FIRST_PIPE_INSTANCE`, a second instance fails; `PIPE_REJECT_REMOTE_CLIENTS` refuses remote clients;
  - a NULL security descriptor grants read access to Everyone, so an explicit DACL is needed;
  - `GetNamedPipeClientProcessId`, `GetNamedPipeClientSessionId` and `GetNamedPipeServerProcessId` take the pipe
    handle [research: learn.microsoft.com].
- Objects an elevated process creates get its integrity level with no-write-up, so the relay's pipe may need an
  explicit medium label for an unelevated recorder to write to it. E3b's client was elevated, so this is unmeasured
  [unverified: Mandatory Integrity Control page].
- The recorder-side seams:
  - `FlowTracker` already takes a `session_factory` and uses only start, stop and health on it;
  - `PresentMonSource` spawns, pins and stops PresentMon in three places, and already supports a parent without a
    console;
  - PresentMon's `--qpc_time` rows and ETW's raw QPC stamps are on the clock every process shares, so relayed data
    needs no translation [research: main at 4f081ad].
- `rules._all_traffic_quiet` clears a bandwidth hog whenever `talkers` is a list. A relay that forwards only the game's
  events must therefore report `talkers` as None [research: main at 4f081ad].

## 10. Still open

Each item is an owner run or a measurement, and the plan names it where it is needed:
- **M0** on the owner's PC: the automated checks pass, and the owner's two runs passed everything but step 2 (keys
  and right-click), which neither completed (section 11). Step 2 was rebuilt (section 12); a run that completes it is
  still open, and so is what Alt+Left does on the owner's PC (on GitHub's runner it kept the keyboard, section 12).
- **The relay pipe's integrity label** with an unelevated client.
- **Where the UAC prompt appears** when the app's window asks for it.
- **Who parents a store-launched game, and how long the handler stub lives.**
- **Whether `rungameid` shows a launch-options dialog.**
- **A static, hidden app window's effect on `gpu_other_pct` and `missed_ticks`.**
- **Load and analysis time for long captures.**
- **Audible cues during a fullscreen game.**
- **SignPath's answer on `WebView2Loader.dll`**, only if the own loader ever breaks.

## 11. Measured by the window spike (M0)

`scripts/webview_spike.py`, run from the agent's shell on the owner's PC (2026-11-08, WebView2 Runtime 157.0.4341.42,
display at 120 DPI, text size 100%). Five complete runs of the reviewed spike: three `--auto`, two `--rehearse`; none
was a cold start, since the runtime's files had been loaded before. Each line is [verified: the runs' results.json],
unless it says otherwise.
- **The own loader works from ctypes.** `CreateWebViewEnvironmentWithOptionsInternal(true, 0, folder, options,
  handler)` returned S_OK. The runtime asked the options object for 6 values, among them
  `ExclusiveUserDataFolderAccess` and `IsCustomCrashReportingEnabled`, and asked a new one again after the recovery.
- **Startup**: the page was ready 1.9 to 2.2 s after the Python process started, about 1.1 s after its imports.
- **The view**: at the first page, the view's rasterization scale and the page's devicePixelRatio were both 1.25 at
  120 DPI, and the view filled the window.
- **Messages**: 1,000 posted at once all came back in order, p50 41 to 53 ms and p95 72 to 100 ms (queueing); one at a
  time, p50 0.3 to 0.5 ms and p95 0.4 to 0.7 ms.
- **Footprint**: 6 WebView2 processes at the first page (250 MB working set, 83 to 84 MB private); 7 after the page
  checks, which had opened DevTools and asked for the microphone and location (368 to 383 MB, 186 to 189 MB private).
  Python itself, 50 to 52 MB.
- **Recovery**: with its browser process ended, a working page again 1.3 to 1.7 s later, the options asked again and
  every setting back.
- **Close**: every WebView2 process gone 0.36 to 1.68 s after closing. Before the review, one run of eight took
  5.16 s; which process lingered was not recorded then, and is now.
- **Sockets**: no listening TCP socket or bound UDP endpoint in the spike or its WebView2 processes, at the first page
  and after the recovery; the spike's own listener, connected pair and UDP endpoints were read back with their states.
  In one run of five, a WebView2 process held one outgoing HTTPS connection (port 443, closing) at the first reading;
  the page itself cannot reach the network (CSP, below).
- **Modal loops**: in about 2.5 s of Windows' move loop, every message posted to the window during it arrived (432 to
  463), and of 431 to 465 posted to the thread during it, 8 or 9 did.
- **Suspend**: `TrySuspend` succeeded, and the page's 50 ms timer ran 0 times in 3 s suspended, against 3 times in 3 s
  only hidden.
- **DevTools**: `OpenDevToolsWindow` opened DevTools with `AreDevToolsEnabled` off. Microsoft documents the setting as
  governing the keys and the menu [research: ICoreWebView2Settings reference]. In the owner's first run, Ctrl+Shift+I
  reached the page and no browser window appeared; in the second, 4 right-clicks reached the page and no menu
  appeared.
- **CSP**: an inline `onerror` was refused with a `script-src-attr` report, and a `no-cors` fetch of an outside site
  was refused with a `connect-src` report, so the CSP refused it, not CORS or the network.
- **Navigation**: to another site, to another page of the app's own, and to an unregistered scheme, each was refused
  in `NavigationStarting`; `LaunchingExternalUriScheme` was never raised, so its handler, refusal in depth, never ran.
- **Permissions**: geolocation, microphone and notifications reached the host and were refused; the clipboard read was
  refused inside the page. No prompt or other browser window appeared.
- **Downloads**: one was refused; no file appeared in the run folder (where the spike redirects it first) or in
  Downloads.
- **The virtual host**: 9 path escapes, among them encoded slashes and backslashes and the outside file's own absolute
  path, all failed to fetch, and none read the file. The browser's URL parser folds two of them (`/../`, `%2e%2e`)
  into the plain name before the mapping sees them.
- **`os.environ`**: a variable set to an empty string through `os.environ` was removed from the process environment
  (`GetEnvironmentVariableW`: error 203); `SetEnvironmentVariableW` keeps it present and empty. WebView2's five
  variables were back as they were (absent) once the view existed.
- **pythonw**: started with no standard handles, `sys.stderr` is None and the unraisable hook is the default, so an
  unguarded callback's exception went nowhere; the same raise under python.exe reached stderr ("Exception ignored").
  The next messages worked in both.
- **A dropped handler**: kept, it answered 20 of 20; dropped, its process ended with an access violation (reading
  0xFFFFFFFFFFFFFFFF). ctypes raised it as an `OSError`, so the exit code was 1, not 0xC0000005.
- **A run cut short**: closing the window midway gave exit 4 and listed the 16 checks it never reached.
- **GitHub's runner** (`windows-2025`, the Window spike workflow on PR #18, 2026-11-08): it has WebView2 Runtime
  156.0.4314.58, though its image lists none (section 3), and it opens the window. All 30 automated checks passed there
  at 96 DPI with no GPU: page ready 2.75 s after the process started, recovery 0.55 s, close 0.25 s, and of 480
  messages posted to the thread during the move loop, 3 arrived. The rehearsal ran the owner's steps' code. So M1's
  window tests can run on CI [verified: the job's log].
- **The owner's two runs** (in the owner's terminal, 2026-11-08; each INCOMPLETE: 33 passed, 0 failed, step 2 not
  run) [verified: each run's results.json and log]:
  - **Drag**: every result posted to the window arrived (1,717 of 1,717 in 9.5 s of dragging; 1,420 of 1,420 in
    7.6 s). Of those posted to the thread meanwhile (the control), 0 of 1,715 and 1 of 1,420 did.
  - **Display scale**: 2, then 4 scale changes between 120 and 96 DPI, each with a monitor change. At all 12
    readings, 4 of them at 96 DPI, the view's scale and the page's devicePixelRatio matched the window, and the view
    filled it.
  - **Taskbar**: the spike's own icon, the orange L, both times.
  - **Keys, first run**: 7 of the 8 reached the page, in the order the step lists them, and nothing opened,
    navigated or zoomed. Alt+Left, the last, and any right-click did not reach the page; the step counts only with a
    right-click, so it did not count.
  - **Keys, second run**: 4 right-clicks reached the page and no menu appeared, but of the keys only Ctrl+R, Ctrl+P,
    Ctrl+F and Ctrl+S did, and the step needs 6.
  - **Not known**: Alt+Left reached the page in neither run. The step kept only how often the host's key event fired
    (36 times, then 9), not for which keys, so it cannot tell a key not pressed from one the page never got, or show
    whether Alt+Left took the keyboard from the page.
  - **The rest** matched the agent's runs: page ready 1.91 and 1.51 s after the process started, recovery 1.65 and
    0.64 s, close 0.82 and 0.28 s. In the move loop, none of 462 and of 458 messages posted to the thread arrived (the
    agent's runs: 8 or 9).

## 12. Keys, focus and Windows' menu mode in WebView2 (step 2)

What the rebuilt step 2 rests on (`scripts/webview_spike.py`, 2026-11-08). The quotes were read from Microsoft's
WebView2 reference by this session's research agent and are marked [research: ...]; they were not re-read here.
- **The view's key event sees every accelerator key**: AcceleratorKeyPressed "runs when an accelerator key or key
  combo is pressed or released while the WebView is focused": a key pressed with Ctrl or Alt held, or one that maps to
  no character; never Shift, always Escape, and auto-repeats too. Turning the browser's keys off "has no effect on the
  `AcceleratorKeyPressed` event. The event will be fired for all accelerator keys, whether they are enabled or not"
  [research: the ICoreWebView2Controller and ICoreWebView2Settings3 references]. So a Ctrl key the window's event saw
  was pressed, whether or not the page got it.
- **The order a key travels**: the host's event, then the browser's own key handling, then the page; a key marked
  handled stops where it is. With the browser's keys off, that step is skipped "but the event propagation continues,
  and web content will receive the key combination" [research: the ICoreWebView2AcceleratorKeyPressedEventArgs2
  reference]. By that, Alt+Left should reach the page; in neither owner run did it (section 11). One report shows an
  Alt combination (Alt+Shift+S) raising the host's event and never reaching JavaScript, with the browser step skipped
  [research: WebView2Feedback issue 5549, runtime 148].
- **Modifiers**: "Verify whether Ctrl or Alt are pressed by running `GetKeyState(VK_CONTROL)` or
  `GetKeyState(VK_MENU)`", inside the handler; in windowed hosting the handler runs while the browser process waits
  [research: the ICoreWebView2AcceleratorKeyPressedEventArgs and ICoreWebView2Controller references]. On a non-US
  keyboard the right Alt (AltGr) arrives as Ctrl+Alt [research: the WM_SYSKEYUP reference], so the step names
  Ctrl+Alt+Left as Alt+Left.
- **Alt and menu mode**: DefWindowProc sends `WM_SYSCOMMAND` with `SC_KEYMENU` to the top-level window when Alt is
  released [research: the WM_SYSKEYUP reference]. In a WinForms host, Alt pressed and released inside WebView2
  activates the form's menu [research: WebView2Feedback issue 1703], and Chromium's own window handles `SC_KEYMENU`
  itself, to keep the focus [research: Chromium's `hwnd_message_handler.cc`]. The spike's window passes
  `WM_SYSCOMMAND` on, so a lone Alt can put it in menu mode, where keys go to the window menu until Esc or a click.
  Whether Alt+Left does is what step 2's record now shows: its focus, menu and key messages, with their times.
- **AllowHostInputProcessing** (`ICoreWebView2ControllerOptions4`, SDK 1.0.3351.48, runtime 138.0.3351.48 or later)
  passes input through the app's window before the view. It is set only when the controller is created, and reported
  side effects include Tab leaving the view and Alt opening the host's menus [research: its reference and release
  notes; a West Wind blog post]. M1's other road, should Alt need handling at all.
- **A right-click with the default menus off** still raises the page's own `contextmenu` event: run 2 counted 4
  [verified: run 2's results.json]. Microsoft documents only that the host's ContextMenuRequested is not raised then.
- **Typed on GitHub's runner** (PR #20's Window spike rehearsal, runtime 156.0.4314.58, 2026-11-08): all 11 actions
  went in with SendInput (41 of 41 input events taken). The view's key event named the 7 Ctrl keys and Alt+Left (a
  system key) from the keys held, and the page got each about 1 ms later. Alt+Left reached the page, no menu mode
  followed, and a letter typed 0.4 s after it reached the page too; the view's GotFocus fired as typing began, every
  line ticked, and nothing acted (no zoom change, navigation or browser window) [verified: the run's results.json].
  So there Alt+Left keeps the keyboard; the owner's PC has not shown it yet.

## Verification log (2026-11-08)

| Claim | Result | Source read |
|---|---|---|
| `CreateWebViewEnvironmentWithOptionsInternal`: signature, "may be modified or removed in future versions", no channel fallback, and none of the environment variables or policy keys that choose the runtime | confirmed, quoted | learn.microsoft.com webview2-idl reference (1.0.4314.35), re-read by a separate reader |
| The pinned embeddable 3.13.17 has `http`, `webbrowser`, `socketserver`, `secrets`, `ctypes`, `winsound`, `pythonw.exe`, and no `tkinter`, `idlelib`, `turtle`, `_tkinter.pyd` or `tcl86t.dll` | confirmed | the ZIP's listing read in memory; SHA-256 equal to the pin |
| `tests/test_stdlib_only.py` allows only what the pinned runtime imports (`scripts/runtime_modules.json`), since PR #16 | confirmed | main at d7ceb05 |
| The static report charts no NVIDIA column (`html.py` imports F, N and S, not G) | confirmed | main |
| In the first real capture, 2 of 3 marks fell while another window was in front | confirmed | `docs/FIRST-CAPTURE-REVIEW.md` |
| The Stage 3 MSI has no Start-menu shortcut and puts `bin\lagdoctor.cmd` on PATH | confirmed | `installer/lag-doctor.wxs`, `installer/lagdoctor.cmd` on main |
| `WebView2.h`'s C vtables (order, IIDs, parents) agree with `WebView2.idl` in Microsoft.Web.WebView2 1.0.4314.35 | confirmed: 297 interfaces, 0 mismatches | the nuget.org package, nupkg SHA-256 90778deb...44edc4, parsed by script |
| The spike's 134 slots and IIDs equal that header's | confirmed: 0 mismatches | the same parse, against `scripts/webview_spike.py` |
| `CreateWebViewEnvironmentWithOptionsInternal` takes (bool, runtime type, folder, options, handler), 0 = installed; the runtime's folder is the `EBWebView` value read with `KEY_WOW64_32KEY` | confirmed | webview/webview `loader.hh` and Wails `env_create.go`, `find_dll_installed.go` (master, 2026-11-08) |
| Step 2's additions: the controller's `add_GotFocus` 15 and `add_LostFocus` 17, `ICoreWebView2FocusChangedEventHandler` 05EA24BD-6452-4926-9014-4B82B498135D, and the key event's `get_KeyEventLParam` 5 | confirmed: 140 entries, 0 mismatches | the same header and script, against the spike's tables |
| Section 12's quotes (the key event, its order, GetKeyState, AltGr, `SC_KEYMENU`, AllowHostInputProcessing) | quoted, not re-read here | learn.microsoft.com references, WebView2Feedback issues 1703 and 5549 and Chromium's source, read by this session's research agent on 2026-11-08 |
