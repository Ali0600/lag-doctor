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

### A test that never reads its subject's status can pass with the subject degraded
A component with a degraded mode says so (a status, a count of what it dropped). A test that checks only the outputs
passes whenever the degraded path still happens to produce them, and fails by chance when it does not.
**Why it came up**: the tracker's real-ETW test flaked on CI with `AssertionError: []` (no gap at all). Every run,
passing or not, the tracker had said "partial: 96 game packets arrived more than 2 s late" (up to 126): the private
session hands its events over inside `stop()`, and the live loop had already finalised all but the last 2 s. Whether
the server was picked before the planted gap then hung on the phase: on this laptop, delaying the writer by a fixed
0.05, 0.1, 0.15, 0.85 or 0.95 s failed every time, and 0, 0.2, 0.3, 0.5 or 0.7 s passed every time.
**Takeaway**: when a test flakes, read the subject's own status in a passing run first. Degraded there means the test
runs it in the wrong regime: fix the regime, assert the status, and sweep the suspected phase to show the failure is
gone, not rarer.

### A process handle outlives the process
`OpenProcess` keeps succeeding on a process that has exited for as long as anyone holds a handle to it, and
`SetPriorityClass(REALTIME)` unelevated reports success while setting HIGH. The process list does not wait for the
handles: an exited process leaves the NtQuerySystemInformation snapshot and EnumProcesses at once, while an open
handle only keeps its PID from being handed out again (measured 2026-11-07, a killed child with its Popen handle open).
**Why it came up**: the priority fix spawns, lowers and restores a test child, and must never act on a PID
Windows has handed to another process. Later, the PID-list liveness check that replaced psutil: it sees the game exit
even though PresentMon holds a handle to the game, and its test kills a child it still holds, so the PID cannot be
reused before the check looks.
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

### A child pytest run shares the parent's temp folder unless it gets its own `TEMP`
pytest keeps each run's temp folder in `%TEMP%\pytest-of-<user>\pytest-N` and, as a run ends, deletes all but the
newest three. A test that starts a child pytest with the inherited `TEMP` adds numbered folders there, so the suite's
own folder drops out of those three and the next pytest run deletes it, thousands of test folders at once.
**Why it came up**: after `tests/test_downloads.py` started five child runs, the timing-sensitive ETW tracker test
that CI runs after the suite took about 2 s longer and failed 3 times in 16 runs, while runs without the change in the
same hour were normal. A diagnostic branch showed it only right after the full suite, never after a minute idle, and
gone once the child runs got their own `TEMP` (12 of 12 normal).
**Takeaway**: give every child run of a tool that keeps shared state (temp roots, caches, locks) a private root, and
compare a suspicious slowdown with runs from the same hour without your change before calling it noise.

### The library you replace can check its replacement, as far as its own API differs
Once code moves off a library, the library is the obvious independent readout for the new code, but it is only as
independent as the call underneath it. psutil 7.2.2's `virtual_memory()` reads GetPerformanceInfo, so it checks a
GlobalMemoryStatusEx reader from another API (they agreed to the byte); its `net_if_addrs()` calls the same
GetAdaptersAddresses, so it checks only how the buffer is parsed.
**Why it came up**: replacing psutil under `lagdoctor/` for the installer's embeddable runtime. Its C source at the
pinned tag said which Windows call each of the three readouts makes, before any of them was trusted as a test oracle.
**Takeaway**: before using a replaced library as the oracle, read which system call it makes: a different API checks
the value, the same API checks only your parsing, and the value then needs a second witness.

### A C API that fills your buffer with linked lists lets you bounds-check every pointer
GetAdaptersAddresses writes its adapters, names and addresses into the one buffer the caller passes, linked by
pointers. So every pointer must land inside that buffer, and one that does not means the ctypes offsets are wrong:
refuse it with an error instead of reading through it, and bound each list walk so a cycle cannot loop forever.
**Why it came up**: the adapter reader in `winapi/iphlp.py`. Its tests build such a buffer from literals and break one
pointer at a time, aiming it at a valid buffer elsewhere, so a sabotaged check returns a wrong answer instead of
crashing the test process.
**Takeaway**: when an API's output is self-contained in your buffer, check each pointer against the buffer before
following it; and test such a guard with readable memory on the wrong side, never with a wild pointer.

### A driver's own counter can disagree with the reasons it reports
NVML's cumulative clock-event counters are meant to add up the time each reason its bitmask names was active. On
driver 591.86 the SW power-cap counter read 42 % over 169 readings whose reasons never named the cap, and the thermal
counter kept growing (99-100 % a reading) through idle readings that named only `gpu_idle`.
**Why it came up**: the review of the first real capture recomputed the power-cap and heat shares from the raw rows;
the rules had trusted the counters alone.
**Takeaway**: before turning a counter into a share, check it against the instantaneous flags of the same reading,
and count it only where they agree (or leave out the state, here idle, where it is known to drift).

### Running an installed command proves only the modules that command imports
A smoke test that installs a wheel and runs its commands sees only what those commands import. `lagdoctor analyze`
imported 22 of the package's 56 modules and never psutil, and the fix catalog finds its fixes by listing its own
folder, so a wheel missing most of `capture/`, `fixes/` and `winapi/`, or one that forgot to declare psutil, still
passed `--help` and `analyze`.
**Why it came up**: the Stage 3 wheel job. Of seven wheels broken on purpose (a module left out, psutil undeclared,
`laglab/` let in, and so on), five passed both commands.
**Takeaway**: check an installed package three ways: its file list against what git tracks, an import of every
module in a fresh venv, and its commands; then break one artifact per check and watch each go red.

### An editable install of a flat layout puts the whole checkout on sys.path
uv_build's editable install is a `.pth` file naming the folder above the package, which in a flat layout is the
checkout's root. Every process in the venv can then import every top-level folder there (`laglab`, `tests`,
`scripts`) from any working folder. Two effects followed. A regular package found there (one with `__init__.py`)
beats a same-named folder without `__init__.py` found earlier on sys.path: Python looks past a namespace folder for a
regular package. And Python lists a sys.path folder again whenever a file in it has been saved.
**Why it came up**: making lag-doctor a package, so that `uv run lagdoctor` works. Five of the red-first harness's 10
self-tests went red: their scratch project's `lagdoctor/` had no `__init__.py`, so `from lagdoctor import guard`
found the real package, which has no `guard` (all 10 passed with the `.pth` moved aside). Then saving CLAUDE.md during
a red-first run turned 3 of 28 verdicts into UNISOLATED: the probe saw the checkout's root being listed.
**Takeaway**: after making a flat-layout project editable, give every same-named package in fixtures and copies an
`__init__.py`, and take the real tree off sys.path in any process that must test a copy.

### A robust z-score is infinite wherever most of the baseline is one value
The median absolute deviation is 0 as soon as more than half the baseline holds one value (a game that reads nothing
most seconds), and a z-score divided by it calls every change, however ordinary, infinitely unusual: only the
absolute floor is left to decide.
**Why it came up**: the first real capture's disk reads were 0 in 54 % of its quiet seconds, so any read of 1 MiB/s
or more "moved", in 39 % of all seconds, and every mark listed "loading from disk".
**Takeaway**: give a MAD-based score a fallback spread (the mean absolute deviation × √(π/2), which equals the
standard deviation on normal data) and keep "no spread at all" for a baseline that truly never moves.

### Compare an event with chance at the event's own window length
A signal judged "active in the event" when its busiest second in the event's window clears a threshold is more likely
to be active in a long window than in a 1 s one. Measuring its baseline rate on 1 s windows inflates every lift
computed from longer events.
**Why it came up**: a mark covers 4.25 s; against plain 1 s quiet windows the capture's disk reads had a 0.36 chance,
where quiet stretches as long as a mark's window gave 0.64, so routine reads looked linked to the lag.
**Takeaway**: compute a base rate with the same statistic and the same window length as the events it is compared
with, or the comparison measures the windows, not the cause.

### A "see the other list" pointer must land for every case it can come from
Replacing a repeated detail with a pointer ("which was not recorded (see Not measured)") moves the duty of saying
why to the list it points at, and that list may be built from a different predicate than the one behind the pointer.
**Why it came up**: the rules call a source missing when it has no rows, but the Not measured list read only each
source's own state; a sampler that ran ("ok") and recorded nothing was pointed at and listed nowhere.
**Takeaway**: when a message starts pointing elsewhere, check the target is derived from the same condition (or a
wider one) and test the case where the two used to differ.

### An invisible character in source is one edit away from another
A literal no-break space looks like a space and a byte-order mark like nothing, so an editor, a formatter or an
agent's edit tool can swap one for another character, and the diff shows nothing anyone would notice.
**Why it came up**: the console's table of ASCII forms mapped the no-break space to a space; an edit retyped the key
as a plain space, the entry became a no-op, and every no-break space printed as "?". Writing the fix, the agent's
edit tool turned the escape `\u00a0` back into the literal character.
**Takeaway**: write invisible and look-alike characters as escapes (`"\u00a0"`, `"\ufeff"`), and after an
edit read the line back with `ascii()` to see which one landed.

### A total that no PR touches goes stale without a merge conflict
A figure in a doc (a test count, a size) moves with every change that adds to what it counts, but only a change to
its own line touches the text, so PRs merged in parallel apply cleanly and leave the figure wrong.
**Why it came up**: PR #2 set the README's totals to 2,632 tests and 1,467 sabotages in 28 specs; #5 and #4 then
added 13 tests and 18 sabotages without touching that line, and main went green with a stale README (and again after
#7). Recounting caught a second trap: CI's download summary said 33 PresentMon tests where the README said 32, but it
already said 33 at the commit that set the 32, so the two count different things.
**Takeaway**: after the last of a batch of parallel merges, recount each doc total on main from its source, and
before correcting a figure against a counter, read the counter at the commit that set the figure.

### An extra is published with the package; a dependency group is not
`[project.optional-dependencies]` lands in the wheel's metadata (`Provides-Extra`, `Requires-Dist: ...; extra ==`),
so anyone can install it from the published package; `[dependency-groups]` (PEP 735) stays in `pyproject.toml` and
the lockfile, for the checkout alone.
**Why it came up**: LagLab's pyglet was an extra, so the wheel offered `lag-doctor[laglab]`, which would have
installed a graphics library for a `laglab/` the wheel does not ship.
**Takeaway**: an extra is for a feature of what you ship; tools for the tests or the repo's own scripts go in a
group.

### Python writes bytecode beside an installed package whenever it can
Importing a module whose `__pycache__` has no valid `.pyc` makes Python write one next to the source, if the folder
is writable, and an administrator can write into Program Files.
**Why it came up**: a sabotaged MSI without its bytecode ran fine, but CI, as an administrator, wrote
`lagdoctor\__pycache__\*.pyc` into `C:\Program Files\lag-doctor`, and the uninstall then left the folder behind,
since Windows Installer removes only the files it installed.
**Takeaway**: ship unchecked-hash `.pyc` files compiled by the very runtime that loads them
(`compileall --invalidation-mode unchecked-hash`), and check after a run that the install folder is byte for byte
as installed.

### A PATH change reaches only processes started after it
An installer that edits the system PATH changes the registry; every process already running keeps the environment
it started with, and so does each child it starts.
**Why it came up**: the MSI check had to run `lagdoctor` "from a fresh shell", but every shell the CI job starts
inherits the job's old PATH. `CreateEnvironmentBlock` with the job's own token builds the environment a new sign-in
would get, from the registry, and the check runs cmd and PowerShell in that.
**Takeaway**: to test what a new window sees after an install, build its environment from the registry; a child of
the installing process proves nothing about PATH.

### `PurePosixPath` folds away the very path parts a safety check looks for
`PurePosixPath("a//b").parts` and `PurePosixPath("a/./b").parts` are both `('a', 'b')`: the empty and `.` parts
never reach a check written over `.parts`.
**Why it came up**: the MSI build refuses unsafe names in the wheel; its first version checked `.parts` and let
`lagdoctor//x.py` and `lagdoctor/./x.py` through, which a test caught.
**Takeaway**: validate a name from an archive on its raw text (`name.split("/")`), before any path class
normalises it.

### A signed exe ends in zero padding, so a changed last byte may be no change
An Authenticode signature sits at the end of a PE file, padded to 8 bytes with zeros, so overwriting the last byte
with `\0` usually leaves the file as it was.
**Why it came up**: two tests "tampered" with PresentMon that way and expected its check to fail; the check passed,
because the bytes had not changed.
**Takeaway**: tamper by flipping a byte inside the code (`data[500_000] ^= 0xFF`), and assert the hash changed
before trusting the test.

### Windows hands out port 0 in sequence, and excludes ports per protocol
A socket bound to port 0 gets the next free number after one hint that every process and every `connect()` shares,
one step at a time, the hint jumping somewhere else now and then. Excluded ports are kept per protocol (`netsh int
ipv4 show excludedportrange protocol=udp`): on GitHub's runners, Docker's NAT holds 200 UDP ports in a row, at a new
place on each runner, that TCP still hands out.
**Why it came up**: the self-test's loopback DNS stand-ins took TCP's next port-0 number and tried UDP on it, ten
times, and twice on CI all ten failed. On ten fresh runners, nine had that UDP block; with the hint inside it (1.2 %
of its positions), the real tests failed 36 times out of 36. With random dynamic ports instead, they passed 60 of 60
there.
**Takeaway**: one protocol's port-0 choice says nothing about another protocol's ports, and retries taken from a
sequential allocator are one try, not ten: draw them independently, and name each refusal.

### An interpreter's list of its standard library is not what a distribution of it ships
`sys.stdlib_module_names` is compiled into CPython from its whole source tree, the same in every build of a version;
python.org's embeddable ZIP of that very build leaves out tkinter, IDLE, turtle, venv, ensurepip and the tests.
**Why it came up**: `tests/test_stdlib_only.py` allowed `lagdoctor/` every name on the test interpreter's list, so
`import tkinter` would have passed CI and failed in the MSI's runtime. Asked itself, that runtime imports 264 of the
290 names on its own list (`scripts/runtime_modules.py`).
**Takeaway**: judge what a shipped runtime can import by asking that runtime (look up and import each name there),
never by the catalogue of the interpreter that happens to run the check.

### In a PowerShell double-quoted string, `?` and `:` carry on a variable's name
`"$env:LOCALAPPDATA\x"` expands as expected, but `"$env:LOCALAPPDATA?x"` and `"$env:LOCALAPPDATA:x"` expand to
an empty string (Windows PowerShell 5.1, measured): PowerShell reads the `?` or `:` as part of the variable's name.
A `$` or a backtick later in the string is code too.
**Why it came up**: the HTML report's analyze command names the capture as `"$env:LOCALAPPDATA\..."`, so the
Windows user name stays off a page people share; a test runs the command's argument through a real
`powershell.exe`.
**Takeaway**: write `${env:NAME}` unless a separator follows, put a backtick before `$`, backticks and quotes in
the rest, and prove the string by running PowerShell on it.

### Opening a store URI starts the game elsewhere; opening the game's exe starts it inside you
`ShellExecuteEx` of `steam://rungameid/<id>` starts the scheme's handler with the URI, and without
`SEE_MASK_NOCLOSEPROCESS` the caller gets no handle at all: the running store client starts the game. `ShellExecuteEx`
of an `.exe` calls CreateProcess inside the caller, which becomes the game's parent, and CreateProcess makes
full-access handles whether or not they are passed back.
**Why it came up**: the desktop-app plan (`docs/LAUNCHER-PLAN.md`) needs a Play button that keeps "never touch the
game", and `tests/test_no_handles.py` scans only psutil and OpenProcess, so it would have noticed neither launch.
**Takeaway**: start a game through its launcher's URI, never from your own process; a "no handles" guard must cover
the launch APIs too.

### A local web UI is a server: Windows lets another account share its port, and cookies ignore ports
CPython's `HTTPServer` sets `allow_reuse_address`, so it binds with SO_REUSEADDR, and on Windows another account's
socket that also sets it may bind the same port and take its connections. Cookies are scoped by host, not by port
(RFC 6265, section 8.5), so a login cookie for `127.0.0.1:PORT` reaches every local server the browser talks to.
**Why it came up**: the first draft of lag-doctor's UI was a page served on 127.0.0.1 into an Edge window. The
review found both problems, and the owner then chose a desktop window with no server (DECISIONS 2026-11-08).
**Takeaway**: a local UI server binds with SO_EXCLUSIVEADDRUSE and checks a per-launch secret sent in a header, never
a cookie; a window that needs no server at all is simpler still.

### A rerun hides its failed attempt from the run list
GitHub lists a workflow run once, with its last attempt's verdict: a run that failed and then passed on a rerun shows
green in `gh run list` and on the Actions page, and its failed attempt is reachable only through `run_attempt` and the
attempts API.
**Why it came up**: counting the loopback port flake over CI's history. Both sightings were first attempts that a
rerun had turned green, so the run list showed it in none of 64 runs; over every attempt it was 2 of 136 test jobs.
**Takeaway**: count a flake over every attempt (`/actions/runs/<id>/attempts/<n>/jobs`), never over the run list:
reruns are where flakes go to hide.
