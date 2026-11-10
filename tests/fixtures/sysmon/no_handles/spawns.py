"""Planted program starts for tests/test_no_handles.py's spawn scan. Never imported or run: only parsed.

Every line that must be reported ends with an "expect" marker; the test compares the scanner's hits with those
markers, line for line.
"""
import asyncio
import concurrent.futures
import ctypes
import multiprocessing
import os
import subprocess
import subprocess as sp
import webbrowser
from os import startfile  # expect: importing os.startfile
from subprocess import run as go  # expect: importing subprocess.run under another name

subprocess.Popen(["x"])  # expect: Popen
sp.check_output(["x"])  # expect: through a module alias
os.startfile("steam://rungameid/1")  # expect: startfile, as Play must never
os.system("x")  # expect: system
webbrowser.open("https://example.com/")  # expect: webbrowser, which is startfile on Windows
asyncio.create_subprocess_exec("x")  # expect: asyncio's subprocess
multiprocessing.Process(target=print)  # expect: another Python process
concurrent.futures.ProcessPoolExecutor()  # expect: a process pool
runner = subprocess.run  # expect: a reference kept for later
mod = subprocess
mod.call(["x"])  # expect: through an alias made by assignment
shell32 = ctypes.windll.shell32
shell32.ShellExecuteW(None, "open", "x", None, None, 1)  # expect: ShellExecuteW
k32 = ctypes.windll.kernel32
k32.CreateProcessW(None, "x", None, None, False, 0, None, None, None, None)  # expect: CreateProcessW
maker = getattr(k32, "CreateProcessAsUserW")  # expect: a Windows name by getattr
spawner = getattr(subprocess, "Popen")  # expect: getattr on subprocess
SLOTS = {"ShellExecute": 31}  # expect: a COM method's name in a slot table


def later():
    os.execv("x", ["x"])  # expect: inside a function
