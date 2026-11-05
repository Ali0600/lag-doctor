"""Planted violations for tests/test_no_handles.py. Never imported or run: only parsed.

Every line that must be reported ends with an "expect" marker; the test compares the scanner's hits with
those markers, line for line.
"""
import ctypes
import psutil
import psutil as ps
from psutil import Process, Process as P
from psutil import pid_exists  # expect: importing pid_exists

psutil.Process(1234)  # expect: Process with an argument
psutil.Process(pid=1234)  # expect: Process with a keyword argument
ps.Process(4)  # expect: through a module alias
Process(8)  # expect: through an imported name
P(12)  # expect: through an imported alias
for proc in psutil.process_iter():  # expect: process_iter
    pass
psutil.pid_exists(16)  # expect: pid_exists
check = ps.pid_exists  # expect: a reference without a call
make = psutil.Process  # expect: Process kept for later
spawn = psutil.Popen(["x"])  # expect: Popen (a psutil.Process)
k32 = ctypes.windll.kernel32
handle = k32.OpenProcess(0x1000, False, 20)  # expect: OpenProcess outside winapi/process.py
opener = getattr(k32, "OpenProcess")  # expect: OpenProcess by name
nt_open = ctypes.windll.ntdll.NtOpenProcess  # expect: NtOpenProcess
lister = getattr(psutil, "process_iter")  # expect: getattr on psutil
mod = psutil
mod.Process(24)  # expect: through a module alias made by assignment
