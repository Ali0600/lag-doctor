# Security policy

## Reporting a problem

Please report security problems privately: open this repository's **Security** tab and choose **Report a
vulnerability**. Do not open a public issue for them. There is no fixed response time.

## What counts

lag-doctor runs one part of itself with administrator rights: `watch --elevate` starts the recording in an elevated
window after a UAC prompt. A vulnerability is anything that lets another program or user:

- ride on that prompt, or make the elevated part run code or open files it should not;
- read what a recording captures from another account;
- make lag-doctor run a PresentMon other than the pinned, hash-checked 2.6.0 release;
- make a fix change something other than what it showed you and journaled, or keep `undo` from restoring it.

Already documented, so not a new finding: a program that already runs as you can edit a git checkout's code before
you approve `--elevate`, `scripts/e2e.py` or `scripts/etw_access.py` (README, Limits).

## Supported versions

Only the latest commit on `main`; there are no releases yet.
