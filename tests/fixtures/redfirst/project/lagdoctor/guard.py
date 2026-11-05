"""A stand-in guard for the red-first runner's own tests: PID 0 (every process) is refused."""


def refuses(pid: int) -> bool:
    return pid == 0


def describe(pid: int) -> str:
    return f"pid {pid}"     # no check reads this, so a mutation here survives
