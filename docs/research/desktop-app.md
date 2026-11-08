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

A WebView2 window opens no socket, so none of this applies.

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
- **M0** on the owner's PC.
- **The runner's WebView2 Runtime**, read and used in a CI probe.
- **The relay pipe's integrity label** with an unelevated client.
- **Where the UAC prompt appears** when the app's window asks for it.
- **Who parents a store-launched game, and how long the handler stub lives.**
- **Whether `rungameid` shows a launch-options dialog.**
- **A static, hidden app window's effect on `gpu_other_pct` and `missed_ticks`.**
- **Load and analysis time for long captures.**
- **Audible cues during a fullscreen game.**
- **SignPath's answer on `WebView2Loader.dll`**, only if the own loader ever breaks.

## Verification log (2026-11-08)

| Claim | Result | Source read |
|---|---|---|
| `CreateWebViewEnvironmentWithOptionsInternal`: signature, "may be modified or removed in future versions", no channel fallback, and none of the environment variables or policy keys that choose the runtime | confirmed, quoted | learn.microsoft.com webview2-idl reference (1.0.4314.35), re-read by a separate reader |
| The pinned embeddable 3.13.17 has `http`, `webbrowser`, `socketserver`, `secrets`, `ctypes`, `winsound`, `pythonw.exe`, and no `tkinter`, `idlelib`, `turtle`, `_tkinter.pyd` or `tcl86t.dll` | confirmed | the ZIP's listing read in memory; SHA-256 equal to the pin |
| `tests/test_stdlib_only.py` allows only what the pinned runtime imports (`scripts/runtime_modules.json`), since PR #16 | confirmed | main at d7ceb05 |
| The static report charts no NVIDIA column (`html.py` imports F, N and S, not G) | confirmed | main |
| In the first real capture, 2 of 3 marks fell while another window was in front | confirmed | `docs/FIRST-CAPTURE-REVIEW.md` |
| The Stage 3 MSI has no Start-menu shortcut and puts `bin\lagdoctor.cmd` on PATH | confirmed | `installer/lag-doctor.wxs`, `installer/lagdoctor.cmd` on main |
