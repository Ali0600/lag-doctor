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
nothing. PresentMon's console text is UTF-16LE with a BOM.
**Why it came up**: a commit message and a "no leftover processes" check both went wrong here.
**Takeaway**: write files with `[IO.File]::WriteAllText(..., UTF8Encoding($false))`, query processes with
`Get-CimInstance Win32_Process`, and prove a "found nothing" check can find something.
