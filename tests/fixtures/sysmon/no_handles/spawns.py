"""Planted program starts for tests/test_no_handles.py's spawn scan. Never imported or run: only parsed.

Every line that must be reported ends with an "expect" marker; the test compares the scanner's hits with those
markers, line for line.
"""
import asyncio
import concurrent.futures
import ctypes
import importlib
import multiprocessing
import os
import subprocess
import subprocess as sp
import sys
import webbrowser
from asyncio import subprocess as asp
from concurrent import futures
from os import startfile  # expect: importing os.startfile
from subprocess import run as go  # expect: importing subprocess.run under another name
from subprocess import *  # expect: importing everything subprocess has
from lagdoctor.capture import presentmon
from lagdoctor.winapi.launch import open_through_explorer  # expect: importing lag-doctor's own launcher

import _winapi
import nt

subprocess.Popen(["x"])  # expect: Popen
sp.check_output(["x"])  # expect: through a module alias
os.startfile("steam://rungameid/1")  # expect: startfile, as Play must never
os.system("x")  # expect: system
nt.startfile("x")  # expect: nt, the module behind os
webbrowser.open("https://example.com/")  # expect: webbrowser, which is startfile on Windows
webbrowser.get().open("https://example.com/")  # expect: webbrowser's browser object
asyncio.create_subprocess_exec("x")  # expect: asyncio's subprocess
asp.create_subprocess_shell("x")  # expect: asyncio.subprocess under another name
multiprocessing.Process(target=print)  # expect: another Python process
multiprocessing.get_context("spawn").Process(target=print)  # expect: through a context
concurrent.futures.ProcessPoolExecutor()  # expect: a process pool
futures.ProcessPoolExecutor()  # expect: a pool through a from-import of the package
_winapi.CreateProcess(None, "x", None, None, False, 0, None, None, None)  # expect: _winapi
runner = subprocess.run  # expect: a reference kept for later
runner(["x"])  # expect: and each use of it
mod = subprocess  # expect: the module passed on as a value
mod.call(["x"])  # expect: through an alias made by assignment
holder = [subprocess]  # expect: the module put away for later
shell32 = ctypes.windll.shell32
shell32.ShellExecuteW(None, "open", "x", None, None, 1)  # expect: ShellExecuteW
k32 = ctypes.windll.kernel32
k32.CreateProcessW(None, "x", None, None, False, 0, None, None, None, None)  # expect: CreateProcessW
k32.CreateProcessInternalW(None, None, "x")  # expect: a native name
maker = getattr(k32, "CreateProcessAsUserW")  # expect: a Windows name by getattr
spawner = getattr(subprocess, "Popen")  # expect: getattr on subprocess
SLOTS = {"ShellExecute": 31}  # expect: a COM method's name in a slot table
__import__("os").startfile("x")  # expect: importing os by name
importlib.import_module("subprocess")  # expect: importing subprocess by name
sys.modules["os"].system("x")  # expect: os from sys.modules
loop.subprocess_exec(print, "x")  # expect: an event loop's subprocess
open_through_explorer("steam://rungameid/1")  # expect: lag-doctor's own launcher, called outside Play
presentmon.spawn(["x"], False)  # expect: presentmon's launcher
which = "launch_elevated"  # expect: a launcher by name
pinned: object = subprocess.Popen  # expect: kept in an annotated assignment


@subprocess.check_call  # expect: as a decorator
def decorated():
    pass


class Spawner(multiprocessing.Process):  # expect: as a base class
    pass


def with_default(run=subprocess.run):  # expect: as a default, where the def is
    return run


def later():
    os.execv("x", ["x"])  # expect: inside a function
    later_go(["x"])  # expect: through an alias made further down


later_go = subprocess.check_call  # expect: that alias
second(["x"])  # expect: an alias of an alias
second = first_alias  # expect: that alias, made from another one further down
first_alias = subprocess.run  # expect: the first alias
