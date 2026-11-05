# Learnings

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
