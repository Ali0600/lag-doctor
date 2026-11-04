"""LagLab: a small fake game that lags on purpose, in ways we choose.

lag-doctor is tested against it: each mode produces one known cause of lag, and
LagLab logs what it did and when, on the same clock lag-doctor uses
(time.perf_counter, which is QueryPerformanceCounter on Windows). A test can then
check the doctor's verdict against ground truth the doctor never saw.
"""
