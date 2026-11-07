# Learnings

### A write-restricted service token is checked twice, and ETW's descriptors fail the second check
`sc sidtype <service> restricted` puts the service SID, Everyone, the logon SID and WRITE RESTRICTED on the token's
restricted-SID list, and a write access must then be granted by that list as well as by the token's groups. ETW's
descriptors grant Everyone only RegisterGuids and JoinGroup, so `StartTraceW` answers access denied, and creating
even a message-only window fails (PresentMon exit 4).
**Why it came up**: the installer plan hoped a restricted service SID would narrow the capture service; E3 on a
GitHub runner showed it stops every ETW session and PresentMon, while the unrestricted SID with one privilege was
refused nothing.
**Takeaway**: a restricted service SID suits a service that only touches objects it owns; measure every system
object the service must write with a real token before planning on it.

### A trigger-started service's client must retry its first open
With `sc triggerinfo <service> start/namedpipe/<name>` (the bare pipe name), a client's first `CreateFileW` on the
pipe fails with ERROR_PIPE_BUSY (231); the SCM starts the service, whose ServiceMain receives `TriggerStarted 6`
(the trigger type) and no arguments of its own; a retry about 0.1 s later connects.
**Why it came up**: E3b expected file-not-found and a retry loop; the measured first error was "busy", from
Windows' own trigger plumbing, in nine cycles out of nine.
**Takeaway**: treat 231 and 2 alike on the first open and retry with a deadline; a trigger-started service reads its
settings from its command line, since the start arguments are Windows' own.

### PresentMon on a GPU-less VM writes rows only without display tracking
On a `windows-2025` runner (Basic Render and Basic Display drivers) a WARP Direct3D 11 window presented 60 times a
second, and PresentMon with lag-doctor's flags wrote no row for any process in 8 s, administrator or service; with
`--no_track_display --no_track_gpu` it wrote 801 rows, 401 for the window.
**Why it came up**: E3's "rows arrive for the game's PID" read as a service problem until the control run by the
administrator got the same nothing.
**Takeaway**: on a machine with no real display adapter, judge "PresentMon sees the presents" with display tracking
off and leave display timing to a PC with a GPU; and run the control as the account known to work before reading a
zero as the experiment's answer.

### Windows PowerShell 5.1 started from PowerShell 7 inherits a module path it cannot use
A `powershell.exe` child of a pwsh 7 job shell got pwsh's `PSModulePath`, so `Get-AuthenticodeSignature` failed
with "the module could not be loaded" (Microsoft.PowerShell.Security), and a silent JSON parse turned that into
"every binary unsigned".
**Why it came up**: the first Stage 2 run reported python.org's signed runtime as unsigned; WinVerifyTrust, the
reading lag-doctor itself uses, said signed all along.
**Takeaway**: from pwsh, run pwsh; if 5.1 is needed, drop `PSModulePath` from its environment; and a cross-check
that could not run is reported as such, never counted as a verdict.

### A venv's python.exe on Windows is only a launcher
`.venv\Scripts\python.exe` starts the base interpreter (`C:\Python313\python.exe`) as a child process.
**Why it came up**: LagLab's frames come from the child, so a tool targeting the launcher's PID or exe
path (for frame capture, or a per-app GPU preference) would watch the wrong process.
**Takeaway**: identify a Python program by the PID it reports itself, or by `QueryFullProcessImageNameW`,
never by the command you launched.

### Hybrid laptops run unknown OpenGL apps on the integrated GPU
On an NVIDIA Optimus laptop, a program with no driver profile renders on the Intel GPU even when a
discrete GPU is available. `SHIM_MCCOMPAT=0x800000001` in the process environment moves an OpenGL app to
the NVIDIA GPU (undocumented, but confirmed here by the GL renderer string).
**Why it came up**: LagLab rendered on "Intel(R) UHD Graphics" by default, which is the exact "game on
the wrong GPU" problem lag-doctor must detect.
**Takeaway**: on a hybrid machine, check which adapter a game renders on before tuning anything else.

### Windows frees a thread's hotkeys when the thread exits
A sabotage that removed `UnregisterHotKey` left the key free after the listener thread stopped.
**Why it came up**: that mutation survived the red-first check, so the explicit unregister is the
documented pairing rather than the only release.
**Takeaway**: a surviving mutant can mean the guarded behaviour is guaranteed elsewhere; find out which
before writing a test for it.

### Windows PowerShell 5.1 traps
`Out-File -Encoding utf8` writes a byte-order mark (it ended up as the first character of a git commit
subject), and `Get-Process` objects have no `CommandLine` property, so a filter on it silently matches
nothing.
**Why it came up**: a commit message and a "no leftover processes" check both went wrong here.
**Takeaway**: write files with `[IO.File]::WriteAllText(..., UTF8Encoding($false))`, query processes with
`Get-CimInstance Win32_Process`, and prove a "found nothing" check can find something.

### A console program's output encoding can depend on where its output goes
PresentMon writes narrow Latin-1 text with no BOM into a pipe, UTF-16LE with a BOM into an empty disk file
(PowerShell's `>` redirection), and UTF-8 with a BOM through its own `--output_file`. An earlier note here
said "PresentMon's console text is UTF-16LE": true only for the redirect it was seen through.
**Why it came up**: the research replayed captures through every output path and the bytes differed.
**Takeaway**: sniff the BOM on the exact handle type production uses (a pipe, here), and never generalise
an encoding from one redirect.

### psutil opens a handle just by being asked about a process
Constructing `psutil.Process(pid)` already calls `OpenProcess` (it reads the create time), and so do
`process_iter()` and `pid_exists()`. Only `psutil.pids()` does not.
**Why it came up**: lag-doctor promises never to open the game; a one-line `psutil.Process(game_pid).name()`
would have broken that silently. The research traced it through psutil 7.2.2's source.
**Takeaway**: when a library's "read-only" call has side effects on another process, read its source once;
guard the rule with a test that greps for the call, not with a comment.

### Fast performance counters can be wrong, not just noisy
At 4 Hz, PDH's idle-based `% Processor Time` failed whole calls and items and read 0–73 % under a steady
load, and per-engine `GPU Engine` values reached 3,652 % for long GPU submissions.
**Why it came up**: the sampler design measured every counter at its real rate before trusting it.
**Takeaway**: treat a failed counter read as a missing sample (never 0), average raw values over a window
before clamping, and prefer counters that stayed valid at the rate you sample (`% User Time` +
`% Privileged Time`, `% Processor Utility`).

### Thread CPU times move in 15.625 ms steps
Windows accounts thread and process CPU time on the clock tick, so a 250 ms window can only tell a thread's
load to about ±6 % of a core.
**Why it came up**: every thread time in a 4 Hz snapshot was an exact multiple of 156,250 × 100 ns.
**Takeaway**: decide "this thread is saturated" over windows of 1 s or more.

### Max-per-pixel downsampling must not treat empty buckets as gaps
The report's charts keep the maximum of each pixel column so one stutter among 100,000 frames still reaches
the top. Marking every empty bucket as a gap turned a 1 Hz ping line into dots.
**Why it came up**: the first rendered report drew dotted lines for continuous data.
**Takeaway**: a gap is a bucket whose samples were all missing, not a bucket with no samples.

### Headless Edge screenshots follow the OS theme and have a minimum width
Headless Edge lays out at 492 px or wider whatever size it is given, and renders `prefers-color-scheme`
from the Windows theme. `--blink-settings=preferredColorScheme=1` forces light.
**Why it came up**: both "light" and "dark" report screenshots came out dark, and the phone-width check
measured 492 px.
**Takeaway**: check narrow layouts inside a fixed-width iframe, and set the colour scheme explicitly.

### A second Ctrl+C lands in your cleanup, not in the wait it interrupted
On Windows, a SIGINT that arrives while the main thread sits in `Thread.join()` or `Event.wait()` is held
until that wait returns, then raised as `KeyboardInterrupt` right there. A tool that stops quietly for a few
seconds after the first Ctrl+C invites a second one, which escapes the `finally` doing the stopping and saving.
**Why it came up**: the review reproduced a whole recording lost to a second Ctrl+C during the stop phase.
**Takeaway**: say "stopping and saving..." at once, and defer SIGINT (count the presses) around cleanup and
saves you cannot afford to lose.

### Closing a console window gives you five seconds, and sign-out gives you nothing
Clicking a console's X sends CTRL_CLOSE_EVENT; Windows terminates the process when the handler returns, or
after 5 s. A process that loads user32.dll never receives CTRL_LOGOFF or CTRL_SHUTDOWN at all, and without a
handler neither `finally` nor `atexit` runs.
**Why it came up**: the elevated recording runs in its own console window, and closing it lost the capture
and left a kernel ETW session running until reboot.
**Takeaway**: register a native console control handler; on close, release external state (trace sessions,
child processes) first, then save within the budget; put helper children in a kill-on-close job object.

### Private ETW sessions are a different animal
A private (in-process) ETW session is allowed unelevated, but it cannot be real time (it logs to a file,
delivered when you stop), it is invisible to QUERY/STOP by name (4201), and a same-name start returns 183.
Skipping CloseTrace leaks 2 handles per session while the .etl file still deletes fine.
**Why it came up**: the network tracker's test suite proves the consumer path with private sessions,
because the real Kernel-Network session needs admin rights.
**Takeaway**: a private session tests the consumer, not the controller; count handles to catch leaks that
a file deletion would not show.

### A process handle outlives the process
`OpenProcess` keeps succeeding on a process that has exited for as long as anyone holds a handle to it, and
`SetPriorityClass(REALTIME)` unelevated reports success while setting HIGH.
**Why it came up**: the priority fix spawns, lowers and restores a test child, and must never act on a PID
Windows has handed to another process.
**Takeaway**: check identity (create time) and liveness (exit code) on the handle you then act through,
better still without any handle first, and verify a write by reading the effective value back.

### Rates from a counter that refreshes on its own grid are quantised
NVML refreshes utilisation, power and its cumulative throttle counters together every ~500 ms. A rate taken
between free-running reads read 35 % instead of 40 %, or 50 %/150 % of continuous throttling, depending on
phase.
**Why it came up**: the GPU sampler's throttle share wandered until it read just after each refresh.
**Takeaway**: find a source's refresh grid before computing rates from it, then align reads to the grid or
average over several periods.

### A median baseline collapses on a quantised series
When a series can only take whole multiples of a period (displayed frame intervals on a fixed-refresh
screen), its median is the lower mode: one refresh at 120 FPS on 144 Hz, though the regular cadence also
holds frames for two. Any "N x baseline" rule then fires on a one-refresh slip.
**Why it came up**: the smooth control case got "stutters" from single frames held three refreshes.
**Takeaway**: on quantised data, compare against the longest regular value the cadence produces, not the
median alone.

### A fake that replaces the consumer hides the consumer's own checks
The parent built the elevated child's command with `--steam-log ""` for "no Steam log", and its test checked
that list. But the elevation fixture replaced `launch_elevated` whole, and the real one refuses empty
arguments: on every PC without Steam, `watch --elevate` crashed before the UAC prompt.
**Why it came up**: a verifier drove `cli.main(['watch', '--elevate'])` through the real `launch_elevated`
with only `ShellExecuteExW` faked, and it raised.
**Takeaway**: fake the outermost side effect (the OS call), not the function that validates what you built;
test a writer through its real reader.

### Inline citations leak from comments into user text
The code cites its research notes beside each constant ("WT 8", "LC 4.9"), and the habit crossed into strings:
eleven report and fix texts told players "(WT 8)" or "(NE 7 row 9)", which they cannot look up.
**Why it came up**: the README's picture of the HTML report showed "community-documented, not
Microsoft-documented (WT 8)".
**Takeaway**: keep provenance in comments, and scan string literals on the syntax tree (docstrings excluded);
a plain text grep would flag every comment too.

### A packaged app's shell can save into that app's private copy of AppData
A program a packaged (MSIX) app starts can have what it writes in `%LOCALAPPDATA%` and `%APPDATA%` kept in
`%LOCALAPPDATA%\Packages\<app>\LocalCache\...`. It sees the files at the usual path; other windows never do.
**Why it came up**: `setup` ran in the agent's shell inside the Claude desktop app, so PresentMon was "not
installed" for the user's own terminal and the admin window, and a 14-minute game recording had no frame timing.
That shell even reported no package identity, so asking "am I packaged?" would have missed it.
**Takeaway**: ask the handle where a file is (`GetFinalPathNameByHandleW`), not the path, and check an install from
the context that will use it.

### A power plan's name says nothing about its settings, but can say which mode the maker's tool is in
An ASUS plan called "Silent" set the CPU exactly as Balanced does (same maximum state, boost and cooling), yet
Armoury Crate switches the Windows plan to "Silent", "Performance" or "Turbo" with its own operating mode, and that
mode sets the fans and power limits that no plan setting shows.
**Why it came up**: the first real report blamed "Silent" and offered a switch to Balanced that would only have
changed the screen brightness; then the review found 163 plan switches by Armoury Crate in the System log, so the
name is how its quiet mode shows.
**Takeaway**: read a plan's settings (`PowerReadACValueIndex`, the P-core cap `PROCTHROTTLEMAX1` too) before judging
it, match names as whole words ("eco" is inside "Recommended"), and look for who switches the plan before deciding
what its name means.

### Python's gzip.open compresses at level 9 unless told otherwise
`gzip.open(path, "wt")` defaults to compresslevel 9. For a 47 MB JSON capture that was 24 s of a 36 s save;
streaming the JSON column by column at level 1 saves the same capture in 2.6 s, 12 MB instead of 9 MB.
**Why it came up**: closing the recording window gives a console 5 s to finish, and long captures were lost.
**Takeaway**: when a save sits on a deadline, measure each level; level 1 is usually most of the size win at a
tenth of the time.

### A group that may start trace sessions can still be refused a provider
Windows checks an ETW recording twice: who may start and read a session (the default descriptor), and who may
enable each provider into it (that provider's own descriptor, when it has one).
**Why it came up**: Performance Log Users may start sessions and enable every provider PresentMon needs, but
Kernel-Network and Kernel-Process carry their own descriptor (SYSTEM, LocalService and Administrators only). So a
member gets frame timing but never the game-server tracker, measured with `scripts/etw_access.py` (E1).
**Takeaway**: before promising that an account or group can trace something, read each provider's own descriptor,
not only the session rights.

### Ask Windows for a permission verdict instead of re-implementing it
AuthzAccessCheck evaluates a security descriptor for a chosen set of SIDs as Windows does: entry order, denies,
inherit-only entries, integrity labels and conditional entries included.
**Why it came up**: the probe's first verdicts walked the entries by hand, with each account's own SID plus Everyone;
the adversarial review measured them against AccessCheck and found them wrong in both directions (inherit-only
entries counted; Authenticated Users, Users and INTERACTIVE, which every real token carries, missed).
**Takeaway**: build an Authz client context holding the groups to model and let AuthzAccessCheck answer; cross-check
it once against AccessCheck on a real token.

### A handle-count leak test must collect garbage before it counts
On Python 3.11 each stopped ETW session left 4 handles in cyclic garbage until the collector ran: over 40 sessions the
count climbed in steps of 4 and fell back every 18 or so, stayed flat with a `gc.collect()` before each count, and
never moved on 3.13.
**Why it came up**: `test_stopping_closes_the_consumer_handle` failed on CI's Python 3.11 job with 40 extra handles,
and every time when run alone on 3.11 here, while the real leak it guards (no CloseTrace) costs 2 a session.
**Takeaway**: collect before both counts; a sawtooth is garbage waiting for the collector, and a leak is what still
stands after a collection.

### whoami does not list every group in a token
On a built-in Administrator's token (CI's runner), `whoami /groups` left out the machine's "None" group (RID 513)
as well as the logon SID, though the token held both as enabled groups: 14 groups against whoami's 12.
**Why it came up**: the test that checks lag-doctor's token reader against whoami failed only on the runner; a
temporary CI step printed both lists, and this laptop's token holds no such group.
**Takeaway**: before trusting a tool's output as a test's oracle, compare it with the raw source once on every kind
of machine the test runs on, and name each known gap in the test.

### Rewrite history by swapping whole blobs, then prove the rewrite against the original
`git filter-branch --index-filter` with a reviewed map of old blob to new blob changes only the file versions that
held a detail, byte for byte, while a tree filter re-adds every file and can renormalise line endings on the way.
**Why it came up**: removing the owner's personal details before the first public push took three passes. Each
built its map from exact text rules, refused to write if anything personal survived, re-pointed cited commit ids
with `--msg-filter` and `map`, and was checked by a script that paired every original commit with its rewrite:
same author, dates and parents, trees differing only by mapped blobs, nothing personal in any blob, message or path.
**Takeaway**: rewrite from a map you can review, and push only after a check against the original commit by commit
plus a scan of a fresh clone.

### Skip a test on a declared absence, never a detected one
A test that skips when it cannot find its hardware also skips when a bug hides that hardware, and stays green.
**Why it came up**: the NVIDIA tests skipped whenever `Nvml.open()` returned None; the mutation "NVML never opens"
survived on main. CI's runner lacks a GPU, so its workflow now declares what is missing (`LAGDOCTOR_ABSENT`), the
tests that need it skip there by name, and an undeclared absence fails.
**Takeaway**: let whoever runs the suite say what the machine lacks; when a capability is not declared absent, its
absence is a failure.

### A setting's getter still answers where the setting does not exist
Windows shows the power-mode slider only on Balanced and plans derived from it, yet its getters
(`PowerGetUserConfigured{AC,DC}PowerMode`) answer on every plan, with a position nobody can see or set. Whether a plan
has the slider is its personality (`GUID_POWERSCHEME_PERSONALITY`, "the base plan I am most like"): an index whose
possible values Windows lists as the three base plans' GUIDs (`PowerReadPossibleValue`).
**Why it came up**: on CI's High performance plan lag-doctor reported "power mode balanced" while Windows' effective
mode said high performance. The first fix declared the slider absent on CI, which hid a wrong answer any player on
such a plan would also have got.
**Takeaway**: before reporting a setting, check the condition under which it applies; and before declaring a
capability absent to quiet a test on a new machine, ask what the product would tell a user of that machine.

### Git Bash rewrites arguments that look like POSIX paths
Before Git Bash (MSYS) starts a native program, it turns any argument that begins with `/` into a Windows path.
`git sparse-checkout set --no-cone /Tests/Gold/` stored `C:/Program Files/Git/Tests/Gold/`, and the checkout after it
wrote no files and reported no error.
**Why it came up**: trying the gold-capture fetch for CI from the agent's Git Bash shell. From Python's `subprocess`
or PowerShell, the same command stores the pattern as typed.
**Takeaway**: from Git Bash, set `MSYS_NO_PATHCONV=1` for a native program's slash-led argument, and read back what a
silent tool stored (here `.git/info/sparse-checkout`) before trusting an empty result.

### `pytest.raises(pytest.fail.Exception)` lets a skip through
`pytest.skip` and `pytest.fail` raise different exceptions. A test that expects the failure but gets the skip does not
fail: the skip escapes `pytest.raises` and the test is reported as skipped, which reads as fine.
**Why it came up**: red-first on `tests/redfirst/downloads.json`. Turning "fail when the run requires the download"
into "skip" left the unit test skipped, not red ("1 failed, 1 skipped"); only its twin through pytest itself caught it.
**Takeaway**: when a test checks which outcome a helper raises, catch both and assert the kind; and read red-first's
counts, not only its CAUGHT verdict.
