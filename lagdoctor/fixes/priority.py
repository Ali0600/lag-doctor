"""background_priority: lower one of the user's own background processes, for this session only.

Refused before any handle is opened: the game and its child processes (the caller passes their
PIDs as params "exclude"), lag-doctor itself, and Windows services (looked up through the service
manager). Refused after a limited query: protected and system processes (they refuse even that,
WT 9.2), session-0 processes, and other users' processes. The class only ever goes down: raising
a priority is never offered (WT 9.3; DECISIONS.md "No fix that raises the game's priority").

A priority class lasts until the process exits, so the journal marks the entry session_only.
Undo restores the journaled class only while the same (pid, create_time) still runs; otherwise
read() says GONE and base.py settles the entry without writing.
"""
from __future__ import annotations

import os
from typing import Any

from lagdoctor.fixes.base import GONE, Fix, FixPlan, is_gone
from lagdoctor.fixes.catalog import register
from lagdoctor.winapi import process as winproc
from lagdoctor.winapi.process import ProcessGone

LOWER_CLASSES = ("below_normal", "idle")             # what this fix may set
_RANK = {"idle": 0, "below_normal": 1, "normal": 2, "above_normal": 3, "high": 4, "realtime": 5}
_LABEL = {"idle": "low", "below_normal": "below normal", "normal": "normal", "above_normal": "above normal",
          "high": "high", "realtime": "realtime"}
_KEYS = {"pid", "create_time", "class", "exclude", "name"}
MAX_EXCLUDE = 4096
MAX_NAME = 260


class Refused(Exception):
    """lag-doctor will not change this process; the message says why, in plain English."""


def _pid(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 0xFFFFFFFF:
        raise ValueError(f"{what} must be a process id, got {value!r}")
    return value


def _parse(params: dict[str, Any]) -> tuple[int, int, str, frozenset[int]]:
    """(pid, create_time, class name, excluded pids), or ValueError. Unknown keys are refused, so a
    misspelt "exclude" can never switch off the game check."""
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    unknown = set(params) - _KEYS
    if unknown:
        raise ValueError(f"unknown params {sorted(unknown)}; expected {sorted(_KEYS)}")
    pid = _pid(params.get("pid"), "pid")
    ctime = params.get("create_time")
    if isinstance(ctime, bool) or not isinstance(ctime, int) or not 0 < ctime < 2**63:
        raise ValueError(f"create_time must be the process create time in FILETIME ticks, got {ctime!r}")
    cls = params.get("class")
    if cls not in LOWER_CLASSES:
        raise ValueError(f"class must be one of {list(LOWER_CLASSES)}, got {cls!r}")
    exclude = params.get("exclude", [])
    if not isinstance(exclude, (list, tuple)) or len(exclude) > MAX_EXCLUDE:
        raise ValueError(f"exclude must be a list of at most {MAX_EXCLUDE} process ids")
    name = params.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > MAX_NAME or not name.isprintable()):
        raise ValueError(f"name must be a short printable string, got {name!r}")
    return pid, ctime, cls, frozenset(_pid(p, "exclude entries") for p in exclude)


@register
class BackgroundPriorityFix(Fix):
    id = "background_priority"
    title = "Lower a background process's priority"
    description = ("Lowers the CPU priority class of one of your own background programs (to below normal) so "
                   "the game's threads win when both want the CPU. It lasts until that program exits; undo puts "
                   "the original class back while the same program is still running. lag-doctor never changes "
                   "the game, its child processes, Windows services, protected or system processes, or other "
                   "users' programs, and never raises a priority.")
    session_only = True

    def __init__(self, proc: Any = None) -> None:
        self.proc = proc if proc is not None else winproc

    # ---- refusals ------------------------------------------------------
    def _subject(self, params: dict[str, Any]) -> tuple[int, int] | None:
        """(pid, create_time) of a process this fix may touch, None if it is gone; Refused otherwise."""
        pid, ctime, _, exclude = _parse(params)
        # No handle is opened for these: the game must never be opened at all.
        if pid in exclude:
            raise Refused(f"process {pid} is the game or one of its child processes; lag-doctor never "
                          f"touches the game")
        if pid == os.getpid():
            raise Refused(f"process {pid} is lag-doctor itself")
        services = self.proc.service_pids()
        if pid in services:
            raise Refused(f"process {pid} runs the Windows service {', '.join(services[pid])}; lag-doctor "
                          f"leaves services alone")
        try:
            if self.proc.is_service_or_protected(pid, create_time=ctime):
                raise Refused(f"process {pid} is a protected or system process; lag-doctor leaves it alone")
            if not self.proc.same_user(pid, create_time=ctime):
                raise Refused(f"process {pid} belongs to another user; lag-doctor only changes your own programs")
        except ProcessGone:
            return None
        except PermissionError as exc:
            raise Refused(f"process {pid} cannot be checked ({exc}); lag-doctor leaves it alone") from exc
        return pid, ctime

    # ---- Fix interface ---------------------------------------------------
    def target(self, params: dict[str, Any]) -> str:
        pid = _parse(params)[0]
        name = params.get("name")
        return f"priority class of {name} (process {pid})" if name else f"priority class of process {pid}"

    def read(self, params: dict[str, Any]) -> Any:
        subject = self._subject(params)
        if subject is None:
            return dict(GONE)
        pid, ctime = subject
        try:
            cls = self.proc.priority_class(pid, create_time=ctime)
        except ProcessGone:
            return dict(GONE)
        if cls not in winproc.CLASS_NAMES:
            raise OSError(f"process {pid} reports an unknown priority class {cls!r}")
        return winproc.CLASS_NAMES[cls]

    def write(self, params: dict[str, Any], value: Any) -> None:
        if not isinstance(value, str) or value not in _RANK:
            raise ValueError(f"not a priority class name: {value!r}")
        subject = self._subject(params)          # the same refusals guard every write, undo included
        if subject is None:
            raise ProcessGone(f"process {params.get('pid')} is no longer running")
        pid, ctime = subject
        self.proc.set_priority_class(pid, winproc.CLASSES[value], create_time=ctime)

    def wanted(self, params: dict[str, Any], current: Any) -> Any:
        target = _parse(params)[2]
        if isinstance(current, str) and current in _RANK and _RANK[current] <= _RANK[target]:
            return current                       # already that low or lower: never raise it
        return target

    def describe(self, before: Any, after: Any) -> str:
        if is_gone(before):
            return f"{self.title}: the process has exited; nothing to change"
        if before == after:
            return f"{self.title}: it is already at {_LABEL.get(before, before)} priority or lower; nothing to change"
        return (f"{self.title}: {_LABEL.get(before, before)} -> {_LABEL.get(after, after)} until the process "
                f"exits")

    def plan(self, params: dict[str, Any]) -> FixPlan:
        plan = super().plan(params)
        plan.notes.append("Lasts until that program exits; nothing is saved to disk. Undo restores the original "
                          "class only while the same program is still running.")
        return plan
