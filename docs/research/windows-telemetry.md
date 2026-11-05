# Windows telemetry, settings and fix mechanics (no admin, no game handles)

Research for lag-doctor: how to sample the system 4 times a second without admin rights and without
opening a handle to the game, how to read the static settings that cause lag, and exactly how a
reversible fix would change each one. Nothing on the system was changed while writing this.

**Machine and conditions (2026-11-04).** Windows 11 Home 10.0.26200; i7-12650H (6 P-cores with
Hyper-Threading + 4 E-cores = 16 logical CPUs); RTX 3070 Laptop GPU (driver 591.86 = 32.0.15.9186) plus
Intel UHD Graphics; 31.6 GB RAM. Shell unelevated (Medium integrity). Python 3.13.3 and psutil 7.2.2,
run with `uv run --no-sync python` from the project folder. While the tests ran, another program on this
PC (a `python.exe`, pid 656, not lag-doctor) kept the NVIDIA GPU busy and used up to about one core. It served as a
stand-in "game". **It is not a renderer:** it is a machine-learning training job (a Python
script), which `nvidia-smi` lists as process type `C` (CUDA compute).
Windows accounts its CUDA work to the NVIDIA `engtype_3D` engine (§2.2), which is why it showed up as
"3D" [verified: `Get-CimInstance Win32_Process -Filter "ProcessId=656"` command line; `nvidia-smi` process table;
`<verify>\v_pdh3.py`]. The NVIDIA GPU was at 74–85 °C, in P3, at 780 of 2100 MHz, with NVML's
software-thermal-slowdown flag set. The active power plan was ASUS "Silent", and only the laptop's own
panel was connected.

**Tags.** `[verified: X]` means I read it in source X at the version given, or ran it here and checked
it against a second, independent readout. `[inferred]` means it is my conclusion from verified facts.
`[unverified]` means it is not confirmed: test it before relying on it.

**Abbreviations.** `SDK\…` = `C:\Program Files (x86)\Windows Kits\10\Include\10.0.19041.0\…` (the
Windows SDK headers installed on this PC). `<scratch>` =
`%TEMP%\claude\…\scratchpad\research-windows-telemetry`,
the throwaway folder that holds the scripts which produced every pasted output. It is temporary, so the
code that matters is reproduced below. `<verify>` = the sibling folder `…\scratchpad\research-windows-telemetry-verify`,
which holds the independent fact-check scripts (`v_*.py`) and their outputs (`v_*.out.txt`). "phnt" =
`https://raw.githubusercontent.com/winsiderss/phnt/53fbbdc5b5d2b08761db1c7b26bfa8c820924356/` (a 2026-03-26 commit,
"sync latest", per the GitHub API).

**Fact-check (2026-11-04).** Every claim below was re-checked by a second pass: re-run locally with its own
scripts, or re-read in the cited source. Corrections are made in place, and the old → new value of each one is
listed in the **Verification log** at the end.

---

## 0. Cheat sheet

| Need | Use | Admin? | Handle to game? | Measured cost here |
|---|---|---|---|---|
| CPU per logical CPU, frequency, DPC/ISR, memory, disk, GPU engines, GPU memory, ACPI thermal zone | PDH (`pdh.dll`) via ctypes, one query | no | no | collect 2.1–2.6 ms + read 3.1–3.6 ms per poll (§11) |
| Game's CPU and per-thread CPU | `NtQuerySystemInformation(SystemProcessInformation=5)` | no | no | 5.5–6.6 ms call, 6.5–6.9 ms with lean parse |
| Game's exe path | `NtQuerySystemInformation(SystemProcessIdInformation=88)` | no | no | one call |
| NVIDIA util / temp / power / clocks / throttle reasons | NVML (`C:\Windows\System32\nvml.dll`) via ctypes | no | no | ≈1.3 µs per call, except `nvmlDeviceGetDisplayActive` ≈170 µs (§4.4) |
| Share of time the GPU was throttled | NVML field 269 via `nvmlDeviceGetFieldValues` (or `nvidia-smi … clocks_event_reasons_counters.*`) | no | no | 1.4 µs (§4.4) |
| Which GPU a LUID is; integrated or discrete; HAGS state | DXGI + DXCore + D3DKMT | no | no | 15–90 ms (one-off; 89 ms in a fresh process) |
| Display modes, max refresh, internal panel | `EnumDisplaySettingsW`, `QueryDisplayConfig` | no | no | 21–23 ms (on demand) |
| Power plan, power mode, Game Mode active | `powrprof.dll` | no | no | callback, no polling |
| NIC bytes | `GetIfEntry2` (iphlpapi) | no | no | 0.08–0.11 ms (0.3 ms inside the sampler loop) |
| Service → PID | `EnumServicesStatusExW` | no | no | 1.9–2.8 ms |
| SSD or HDD | `IOCTL_STORAGE_QUERY_PROPERTY` on `\\.\C:` with access 0 | no | no | one call |

Do not use at 4 Hz: PDH `\Thread(*)` (≈470–490 ms per collect), `\Process(*)` (9 ms), `\Process V2(*)` (4.3 ms),
`\System\*` (7 ms), `\Network Interface(*)` (5.2 ms), `psutil.net_io_counters()` (58–68 ms), one-shot `nvidia-smi`
(96–106 ms). Do not use `\Processor Information(*)\% Processor Time` or `% Idle Time` at 4 Hz either: at 250 ms
they fail or read garbage (§1.3).
Never construct `psutil.Process(game_pid)`, and never call `psutil.process_iter()` or `psutil.pid_exists(game_pid)`:
the constructor itself opens a handle to the process, and so do `name()`, `exe()` and `cpu_times()` (§9.4).

---

## 1. PDH via ctypes

### 1.1 The recipe, as run

From `<scratch>\pdh_recipe.py`. The signatures, struct layouts and constants come from `SDK\um\pdh.h` and
`SDK\um\pdhmsg.h` [verified: SDK headers]: `typedef LONG PDH_STATUS`, `PDH_HQUERY`/`PDH_HCOUNTER` are
`HANDLE`, `PDH_FMT_DOUBLE = 0x00000200`, `PDH_FMT_NOCAP100 = 0x00008000`, `PDH_MORE_DATA = 0x800007D2`.

```python
import ctypes, ctypes.wintypes as wt, time
pdh = ctypes.WinDLL("pdh.dll")
PDH_FMT_DOUBLE, PDH_FMT_NOCAP100, PDH_MORE_DATA = 0x200, 0x8000, 0x800007D2

class _U(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_longlong),
                ("AnsiStringValue", ctypes.c_char_p), ("WideStringValue", ctypes.c_wchar_p)]
class PDH_FMT_COUNTERVALUE(ctypes.Structure):          # 16 bytes on x64: CStatus @0, union @8
    _anonymous_ = ("u",); _fields_ = [("CStatus", wt.DWORD), ("u", _U)]
class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):   # 24 bytes on x64
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]

pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
pdh.PdhAddEnglishCounterW.argtypes = [wt.HANDLE, wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
pdh.PdhCollectQueryData.argtypes = [wt.HANDLE]
pdh.PdhGetFormattedCounterArrayW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                             ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
pdh.PdhCloseQuery.argtypes = [wt.HANDLE]
for f in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData", "PdhGetFormattedCounterArrayW", "PdhCloseQuery"):
    getattr(pdh, f).restype = ctypes.c_long

hq = wt.HANDLE(); pdh.PdhOpenQueryW(None, 0, ctypes.byref(hq))
hc = wt.HANDLE(); st = pdh.PdhAddEnglishCounterW(hq, r"\GPU Engine(*)\Utilization Percentage", 0, ctypes.byref(hc))

def read_array(hc, fmt=PDH_FMT_DOUBLE | PDH_FMT_NOCAP100):
    size, count = wt.DWORD(0), wt.DWORD(0)              # always start at size 0 (see 1.3)
    st = pdh.PdhGetFormattedCounterArrayW(hc, fmt, ctypes.byref(size), ctypes.byref(count), None)
    for _ in range(4):
        if (st & 0xFFFFFFFF) != PDH_MORE_DATA:
            return st & 0xFFFFFFFF, []                  # e.g. 0xC0000BBA before the 2nd collect
        buf = (ctypes.c_byte * size.value)()            # items AND their name strings live in this buffer
        st = pdh.PdhGetFormattedCounterArrayW(hc, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if st == 0:
            it = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
            return 0, [(it[i].szName, it[i].FmtValue.CStatus, it[i].FmtValue.doubleValue) for i in range(count.value)]
    return st & 0xFFFFFFFF, []

pdh.PdhCollectQueryData(hq); time.sleep(0.25); pdh.PdhCollectQueryData(hq)   # rate counters need two
status, items = read_array(hc)          # names already copied: reading a c_wchar_p field returns a new str;
                                        # never keep raw szName pointers, they point into `buf`
pdh.PdhCloseQuery(hq)
```

### 1.2 What it printed (`uv run --no-sync python <scratch>\pdh_recipe.py`; excerpt, with some multi-line results joined onto one line)

```
  add FAILED '\\This Set\\Does Not Exist': PDH_CSTATUS_NO_OBJECT
first collect: PDH_CSTATUS_VALID_DATA in 20.9 ms
after ONE collect, \Processor Information(*)\% Processor Time: call=PDH_CSTATUS_INVALID_DATA; item statuses=[]
after ONE collect, \Memory\Available MBytes: call=OK; item statuses=['PDH_CSTATUS_VALID_DATA']
after ONE collect, \Memory\Pages/sec: call=PDH_INVALID_DATA; item statuses=[]
\Processor Information(*)\% Processor Utility  [OK, 18 items, 0.08 ms]
    '0,0'          PDH_CSTATUS_VALID_DATA     39.373
    '_Total'       PDH_CSTATUS_VALID_DATA     16.448
\Processor Information(*)\Actual Frequency  [OK, 18 items, 0.05 ms]
    '_Total'       PDH_CSTATUS_VALID_DATA     1,268.981
\Thermal Zone Information(*)\Temperature  [OK, 1 items, 0.09 ms]
    '\\_TZ.THRM'   PDH_CSTATUS_VALID_DATA     357.000
\Memory\Committed Bytes  [OK, 1 items]  ''  PDH_CSTATUS_VALID_DATA  34,500,177,920.000
\Memory\Commit Limit     [OK, 1 items]  ''  PDH_CSTATUS_VALID_DATA  40,434,671,616.000
\PhysicalDisk(*)\% Idle Time  [OK, 3 items]  '0 C:' 97.989 | '1 E:' 100.110 | '_Total' 99.050
\GPU Adapter Memory(*)\Dedicated Usage  [OK, 3 items]
    'luid_0x00000000_0x00010F83_phys_0'  PDH_CSTATUS_VALID_DATA  4,393,775,104.000
\GPU Engine(*)\Utilization Percentage  [OK, 480 items] statuses={'PDH_CSTATUS_VALID_DATA': 480}
    'pid_656_luid_0x00000000_0x00010F83_phys_0_eng_0_engtype_3D'   PDH_CSTATUS_VALID_DATA   79.088
\Process V2(*)\% Processor Time  [OK, 298 items, duplicate names=0]   'python:13160' ...
\Process(*)\% Processor Time     [OK, 298 items, duplicate names=167] 'python' 'python' ...
```

A second run with one collect only showed which counters work after a single collect
[verified: a one-collect test script run here; excerpt]:

```
  after ONE collect \GPU Engine(*)\Utilization Percentage: PDH_CSTATUS_INVALID_DATA (0 items)
  after ONE collect \GPU Adapter Memory(*)\Dedicated Usage: OK (3 items)
  after ONE collect \Processor Information(_Total)\% Processor Utility: PDH_INVALID_DATA (0 items)
  after ONE collect \PhysicalDisk(_Total)\Avg. Disk sec/Read: PDH_INVALID_DATA (0 items)
  after ONE collect \Thermal Zone Information(*)\Temperature: OK (1 items)
```

### 1.3 Rules

- **Use `PdhAddEnglishCounterW`.** Microsoft calls it *"a language-neutral way to add performance counters"*;
  `PdhAddCounterW` needs localised paths [verified:
  https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhaddenglishcounterw, Remarks]. This
  machine is en-US, so a localised system was not tested [verified: `Get-WinSystemLocale` → en-US].
  A path to a counter set that doesn't exist fails at add time with `PDH_CSTATUS_NO_OBJECT` (0xC0000BB8);
  a missing counter in a set that exists fails with `PDH_CSTATUS_NO_COUNTER` (0xC0000BB9) [verified: run above;
  `<verify>\v_pdh.py`]. **A missing instance does not fail:** an explicit instance that doesn't exist yet (say
  a game pid that has not started) is added with `ERROR_SUCCESS`, and a collect on a single missing instance returns
  `PDH_NO_DATA` [verified: same page, and
  https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhcollectquerydata, Remarks]. Use wildcards and
  filter by name.
- **Rate and timer counters need two collects.** On the first read, the whole call fails. A wildcard
  path `(*)` fails with `PDH_CSTATUS_INVALID_DATA` (0xC0000BBA); a path with an explicit instance or no
  instance fails with `PDH_INVALID_DATA` (0xC0000BC6). Raw counters (`Available MBytes`, `Thermal Zone
  Temperature`, `GPU Adapter Memory`, `Paging File % Usage`, `Processor Frequency`, `% Performance Limit`,
  `Current Disk Queue Length`) are valid after one collect [verified: runs above, re-run in `<verify>\v_pdh.py`].
  The rate interval is the time between the last two collects: *"PDH stores the raw counter values for the
  current and previous collection"* [verified: PdhCollectQueryData page, Remarks].
- **Buffer sizing.** Call once with size 0 and a NULL buffer, get `PDH_MORE_DATA` plus the size, then
  call again. Microsoft: *"If the specified size on input is greater than zero but less than the required
  size, you should not rely on the returned size"* — so always start from 0
  [verified: https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhgetformattedcounterarrayw].
  The item names point into your own buffer, so copy them before freeing it [verified: same page, and the struct layout in `pdh.h`;
  the GPU Engine array needed 73,522 bytes for 484 items of 24 bytes (11,616), the rest being the name strings,
  `<verify>\v_pdh.py`]. Both calls format the data of the last collect, so the size cannot change between them
  unless another thread collects the same query in between [inferred].
- **`PDH_FMT_NOCAP100`.** Without it, values above 100 are cut to 100 (*"The default behavior is that counter
  values are capped at a value of 100"*) [verified: same page]. Values above 100 happen, and by a lot: for the
  CUDA job (pid 656), `GPU Engine … engtype_3D` read **3,651.8 % in one 250 ms window** and 1,513 % in one 1 s
  window, with all-zero windows in between [verified: `<verify>\v_pdh2.py`, `v_pdh3_1hz.py`]. The help text also
  says `% Processor Utility` "may exceed 100%". Keep the raw values, average them over the window you report, and
  clamp only the result (over 80 polls: mean of unclamped 83.2 % vs mean of per-sample-clamped 75.3 %).
- **Per-instance status. Correction: failures are common for the idle-based CPU counters.** Each item has its
  own `CStatus`, and the whole array call can fail too. Measured with `<verify>\v_pdh3.py` and `v_pdh4.py`:

  | Counter (polled at 4 Hz) | Whole-call failures | Items not `PDH_CSTATUS_VALID_DATA` | `_Total` range seen |
  |---|---|---|---|
  | `\Processor Information(*)\% Processor Time` | 2 of 80, `PDH_CALC_NEGATIVE_DENOMINATOR` (0x800007D6) | 90 of 1,404, `PDH_CSTATUS_INVALID_DATA` | 0.0–73.2 |
  | `\Processor Information(_Total)\% Processor Time` | the same 2 of 80 | 0 | same |
  | `\Processor(*)\% Processor Time` (legacy set) | 4 of 40, `PDH_CALC_NEGATIVE_DENOMINATOR` | 58 of 612 | 0.0–71.5 |
  | `\Processor Information(*)\% Idle Time` | 0 of 40 | 25 of 720 | 28.2–**135.5** |
  | `% Processor Utility`, `% Processor Performance`, `% Privileged Utility`, `% User Time`, `% Privileged Time` | 0 | 0 | Utility 3.8–43.5 |
  | `\GPU Engine(*)\Utilization Percentage` | 0 of 80 | 0 of 38,720 | — |

  At a 1 s cadence `% Processor Time` per-CPU items were still invalid 27 of 540 times (5 %) and `_Total` still read
  0.0 once while `% Processor Utility` read 7.6–24.2 [verified: `<verify>\v_pdh3_1hz.py`]. Per-CPU values move in
  steps of about 6.2 % at 250 ms, which fits the help text's warning that idle time is sampled *"at an internal
  sampling interval of the system clock tick"* [inferred]. The researcher's "once, the first GPU Engine read after
  warm-up came back empty" was not reproduced for GPU Engine; the empty `(*)` reads seen in the fact-check were all
  `% Processor Time` calls failing with `PDH_CALC_NEGATIVE_DENOMINATOR`. Treat any non-zero call status, and any
  item whose `CStatus` is not 0, as "no sample this tick", and never as zero.
- **Duplicate instance names.** `\Process(*)` returns 167 duplicate names (each `svchost` comes back as
  plain `svchost`, without `#n`). So the only way to map a value to a PID is to read `\Process(*)\ID
  Process` and trust that both arrays are in the same order [verified: run above; ordering is
  [inferred]]. `\Process V2(*)` uses `name:pid` names and has no duplicates [verified: run above].
- **New instances appear by themselves.** A wildcard counter picks up processes that start after it
  was added: a child process started between two collects appeared in `Process V2` as `python:<pid>`
  [verified: `pdh_recipe.py` started a child, which then appeared; re-run in `<verify>\v_misc.py`: `python:7388` absent
  before, present after the next two collects].

### 1.4 Cost of one `PdhCollectQueryData`, one query per counter set

`uv run --no-sync python <scratch>\pdh_cost.py`: 20 collects spaced 250 ms apart, then 60 collects back to back.
"Our CPU" is process CPU time divided by 60, so it counts kernel time spent on our thread too.

```
set                                                  spaced med spaced max tight wall  tight CPU  (ms per collect)
Processor Information (11 ctrs)                            2.44      17.99       2.52       0.52
Processor Information _Total only (3 ctrs)                 0.86       1.30       0.42       0.26
Thermal Zone Information (4 ctrs)                          0.46       0.57       0.24       0.26
Memory (6 ctrs)                                            0.27       0.50       0.08       0.00
Paging File (1 ctr)                                        0.22       0.38       0.06       0.00
PhysicalDisk (6 ctrs)                                      0.33       0.54       0.15       0.26
Network Interface (2 ctrs)                                 5.68       7.69       5.20       3.12
GPU Engine (1 ctr)                                         0.61       1.34       0.46       0.52
GPU Adapter/Local/Process Memory (4 ctrs)                  0.16       0.27       0.06       0.00
Process V2 (1 ctr)                                         4.71      14.39       4.30       4.17
Process (1 ctr)                                            9.15      10.55       9.00       8.33
System Processor Queue Length + Context Switches           7.71      13.05       7.04       7.03
Thread (1 ctr)  [fact-check run only]                    486.34     638.13     467.04     448.18
```

Fact-check re-run (`<verify>\v_cost.py`, `v_cost2.py`, same method): Processor Information (11 ctrs) 1.00–1.04 ms
spaced median (max 1.97), so lower than above. Every other set came within about 15 % of the table (Thermal 0.44,
Memory 0.23, PhysicalDisk 0.33, Network Interface 5.74, GPU Engine 0.60, GPU memory 0.14, Process V2 4.34,
Process 8.39, System 6.54). **`\Thread(*)` costs about half a second per collect** (≈4,700 instances), so never
use it [verified].

`time.process_time()` on Windows only moves in 15.625 ms steps, so per-call CPU must be averaged over
many calls, as above [verified: the earlier 20-sample run showed only the values 0, 15.62 and 31.25 ms].
Reading the formatted arrays costs little per counter (0.01–0.14 ms), but parsing about 480 GPU Engine
names in Python adds up (§11).

---

## 2. Counter paths that exist on this machine

Every counter set below exists here [verified: `Get-Counter -ListSet <set>` for each, and every path
in this section was added and read through PDH]. "Type" is the `dwType` from `PdhGetCounterInfoW`,
decoded with the definitions in `SDK\um\winperf.h`. "Meaning" is the provider's own Explain text,
read with `PdhGetCounterInfoW(..., bRetrieveExplainText=TRUE)` [verified: `<scratch>\pdh_explain.py`; every type and
every quoted phrase in the table re-read in `<verify>\v_pdh.py` and `v_explain.py`, with `PERF_*` values computed
from `winperf.h` itself; all matched].

### 2.1 Sets, counters, instances here

| Path | Type | Meaning (from the provider's Explain text, shortened) | Value seen |
|---|---|---|---|
| `\GPU Engine(*)\Utilization Percentage` | PERF_100NSEC_TIMER (rate) | "Percentage of time this process is using" the engine | 480–493 instances, one per (process, engine) |
| `\GPU Engine(*)\Running Time` | PERF_COUNTER_LARGE_RAWCOUNT | "Running time of this engine". The NVIDIA instances format to values near 2^64 (for example 18,446,744,068,736,817,152 for pid 656, a negative raw value read as unsigned); the Intel instances look sane (38,868,602,570) [verified: `<verify>\v_pdh.py`] | don't use |
| `\GPU Adapter Memory(*)\Dedicated Usage`, `\Shared Usage`, `\Total Committed` | LARGE_RAWCOUNT, bytes | memory used by the adapter | NVIDIA 4,393,775,104 dedicated |
| `\GPU Local Adapter Memory(*)\Local Usage` | LARGE_RAWCOUNT | "Local adapter memory used" | instances `luid_…_phys_0_part_0` (Intel, Basic Render), `luid_…_phys_0` (NVIDIA) |
| `\GPU Non Local Adapter Memory(*)\Non Local Usage` | LARGE_RAWCOUNT | "Non local adapter memory used" | one instance, `luid_0x00000000_0x00010F83_phys_0_part_0` (NVIDIA). `Get-Counter -ListSet` labels this set *SingleInstance* even so |
| `\GPU Process Memory(*)\Dedicated Usage`, `\Shared Usage`, `\Local Usage`, `\Non Local Usage`, `\Total Committed` | LARGE_RAWCOUNT | per process, per adapter: `pid_<pid>_luid_…_phys_0` | pid 656: 3,866,267,648 dedicated |
| `\Processor Information(*)\% Processor Time` | PERF_100NSEC_TIMER_INV | busy share of wall time. The help text warns that idle accounting is sampled "at an internal sampling interval of the system clock tick". **At 250 ms it is unusable here:** whole-call and per-item failures, and `_Total` swinging 0–73 under a steady load (§1.3) | `_Total` 28.9 |
| `…\% Processor Utility` | PERF_AVERAGE_BULK | "amount of work a processor is completing, as a percentage of … nominal performance and never idle … may exceed 100%" | `_Total` 16.4 |
| `…\% Processor Performance` | PERF_AVERAGE_BULK | average performance while running, as a % of nominal | `_Total` 55.1 |
| `…\Processor Frequency` | RAWCOUNT, MHz | the frequency the OS requested: "will not accurately reflect actual processor frequency" | 2,249 |
| `…\Actual Frequency` | PERF_AVERAGE_BULK, MHz | "current frequency … as measured by the OS" | 1,269 |
| `…\% of Maximum Frequency` | RAWCOUNT | % of max, with the same caveat as Processor Frequency | 55 |
| `…\% Performance Limit` | RAWCOUNT | "performance the processor guarantees … Performance can be limited by Windows power policy, or by the platform as a result of a power budget, overheating" | 100 |
| `…\Performance Limit Flags` | RAWCOUNT | "reasons why the processor performance was limited". **The bit meanings are not documented** [unverified] | 0 |
| `…\% DPC Time`, `…\% Interrupt Time` | PERF_100NSEC_TIMER | time spent servicing DPCs / hardware interrupts | `0,0`: 24.5 % DPC, 6.1 % ISR (the per-core spike is on core 0) |
| `…\DPC Rate` | RAWCOUNT | "the last observed value only" | `_Total` 267 |
| `\Thermal Zone Information(*)\Temperature` | RAWCOUNT | "in degrees Kelvin". `Get-Counter -ListSet` labels this set *SingleInstance*, but it has the instance `\_TZ.THRM` and `(*)` works | `\_TZ.THRM` 357–359 (83.9–85.9 °C); 351–353 later in the fact-check |
| `…\High Precision Temperature` | RAWCOUNT | "in tenths of degrees Kelvin" | 3,572 |
| `…\% Passive Limit` | RAWCOUNT | "100% indicates the devices are unconstrained" | 100 |
| `…\Throttle Reasons` | RAWCOUNT | "reasons why the thermal zone is limiting performance". Bits not documented [unverified] | 0 |
| `\Memory\Available MBytes` | LARGE_RAWCOUNT | standby + free + zero lists | 16,010 |
| `\Memory\Committed Bytes`, `\Commit Limit` | LARGE_RAWCOUNT | committed virtual memory and the limit before the page file must grow | 34.5 GB of 40.4 GB (85 %) |
| `\Memory\Pages Input/sec` | PERF_COUNTER_COUNTER | "pages … read from disk to resolve hard page faults" | 20.8 |
| `\Memory\Page Faults/sec` | PERF_COUNTER_COUNTER | soft + hard faults; "hard faults … can cause significant delays" | 214,520 |
| `\Paging File(*)\% Usage` | PERF_RAW_FRACTION | — | `C:\pagefile.sys` 10.1 |
| `\PhysicalDisk(*)\% Idle Time` | PERF_PRECISION_100NS_TIMER | — | `0 C:` 98.0, `1 E:` 100.1 (can go a little over 100) |
| `\PhysicalDisk(*)\Avg. Disk sec/Read` | PERF_AVERAGE_TIMER, seconds | average read latency | 0.001 |
| `\PhysicalDisk(*)\Disk Read Bytes/sec`, `Disk Write Bytes/sec` | PERF_COUNTER_BULK_COUNT | bytes/s | C: 263 kB/s read, 1.2 MB/s write |
| `\PhysicalDisk(*)\Current Disk Queue Length` | RAWCOUNT | "instantaneous snapshot" | 0 |
| `\Process(*)\…`, `\Process V2(*)\…` | — | see §2.4 | 298 instances |
| `\Network Interface(*)\Bytes Received/sec`, `Bytes Sent/sec`, `Current Bandwidth` | Bytes/sec: PERF_COUNTER_BULK_COUNT (no Explain text); Current Bandwidth: LARGE_RAWCOUNT, *"bits per second"* | instances `Intel[R] Ethernet Connection [16] I219-LM`, `Intel[R] Wi-Fi 6 AX201 160MHz` (the `[R]` replaces `(R)`) | Wi-Fi 10.5 kB/s in; bandwidth 303,300,000 |

`\Processor Information` instance names are `group,index`: here `0,0` … `0,15`, plus `0,_Total` and
`_Total` [verified: `Get-Counter -ListSet "Processor Information"`]. `0,N` is logical processor N of
group 0, which is the same numbering as `LogicalProcessorIndex` in §3.7 [inferred, and consistent with
the per-class split measured there].

### 2.2 GPU Engine instance names

Format: `pid_<pid>_luid_0x<HighPart:08X>_0x<LowPart:08X>_phys_<n>_eng_<n>_engtype_<type>`
[verified: the LUID halves match DXGI `AdapterLuid` in §5.1, with HighPart = 0 and LowPart = 0x00010F83
for the NVIDIA GPU].

- `pid` is the process. This is how you get the game's GPU use without a handle. **Busy `engtype_3D` does not mean
  "rendering":** the CUDA training job (pid 656) appears only on the NVIDIA `eng_0 … engtype_3D` instance, because
  this NVIDIA engine list has no separate compute engine [verified: `<verify>\v_pdh2.py` and `nvidia-smi` showing
  type `C`]. So ML jobs and other compute work look like a game rendering. Combine it with the process name and
  the display/presentation data from PresentMon before you call something a game.
- `luid` identifies the adapter. Map it with §5.
- `phys` is the physical adapter inside a linked adapter (always 0 here). `eng` is the engine ordinal.
  `engtype` is the engine name reported by the driver [inferred from the data below and from the
  blog's description of linked GPUs].
- **`engtype` can be empty, can contain `_` (`OFA_0`), and can contain a space (`GDI Render`).** Split
  at most 9 times: `name.split("_", 9)`. Field 9 is `"engtype_…"`; remove the `engtype_` prefix and keep
  the rest as it is [verified: the parser in §11 ran against every instance on this machine].

Engines here [verified: `Get-Counter -ListSet "GPU Engine"` PathsWithInstances, decoded; re-decoded from the PDH
array with `split("_", 9)` in `<verify>\v_misc.py`, identical]:

```
0x00000000_0x00010F83 (NVIDIA): 0 3D, 1 LegacyOverlay, 2 VideoDecode, 3 Copy, 4 Copy, 5 Security, 6 VideoEncode, 7 OFA_0, 8 VR
0x00000000_0x00010C49 (Intel):  0 3D, 1 VideoDecode, 2 Copy, 3 VideoProcessing, 4 VideoDecode, 5-9 '', 10 GDI Render, 11 VideoProcessing, 12 ''
0x00000000_0x00010EF4 (Microsoft Basic Render Driver): 0-16 3D
```

### 2.3 GPU % per process, the way Task Manager does it

Microsoft's GPU scheduler lead, describing Task Manager: per process it shows *"the utilization of the
busiest engine, across all GPUs, for that process"*, and for overall GPU usage *"we opted to pick the
percentage utilization of the busiest engine"* [verified:
https://devblogs.microsoft.com/directx/gpus-in-the-task-manager/ (`article:published_time` 2017-07-21)]. The post
never sums engines; it rejects averaging across engines, and says the column summary is *"the maximum of the
utilization across all GPUs"* [verified: same page]. So:

```python
eng_sum, proc_max = {}, {}
for inst, st, v in items:                          # \GPU Engine(*)\Utilization Percentage
    if st != 0: continue                           # per-item status (§1.3)
    pid, luid, phys, eng, typ = parse(inst)
    k = (luid, phys, eng)
    eng_sum[k] = eng_sum.get(k, 0.0) + v           # an engine is time-shared: sum over processes
    proc_max.setdefault(pid, {})[k] = max(proc_max.get(pid, {}).get(k, 0.0), v)
overall_samples.append(max(eng_sum.values()))                 # busiest engine, UNCLAMPED
game_samples.append(max(proc_max.get(game_pid, {0: 0.0}).values()))   # the game's busiest engine, UNCLAMPED
# report once per second, over the last 4 samples (collections.deque(maxlen=4)):
gpu_overall = min(100.0, sum(overall_samples) / len(overall_samples))
gpu_of_game = min(100.0, sum(game_samples) / len(game_samples))
```

(Fact-check change: the researcher's version clamped each 250 ms sample to 100, which loses time; see below.)

When there are two engines of the same type (Intel `VideoDecode` 1 and 4, NVIDIA `Copy` 3 and 4), treat
each one as a separate engine and take the maximum, not the sum. That is how the "busiest engine" rule
applies [inferred: the blog does not discuss engines of the same type]. **Do not clamp each 250 ms sample.**
The sum over processes for NVIDIA 3D reached 112.9 % in the researcher's run (§11). In the fact-check run the
CUDA job alone read 3,651.8 % in one 250 ms window after several 0 % windows, and 1,513 % in one 1 s window
[verified: `<verify>\v_pdh2.py`, `v_pdh3_1hz.py`]. The engine time arrives in bursts, apparently credited when the
work completes [inferred]. So average the **unclamped** per-sample values over at least 1 s, then clamp the
average to 100, and expect long-running compute work to look spiky even then. This is a Windows-scheduler view, and it is
not comparable with NVML's `utilization.gpu` (76 % at the same moment) [verified: §11 log].

### 2.4 `Process` and `Process V2`

| | `\Process(*)` | `\Process V2(*)` |
|---|---|---|
| Instance name | image name without `.exe`. Duplicates come back with the same name (167 here) | `name:pid`, for example `python:13160`, unique |
| PID | `\Process(*)\ID Process`, matched by array index | in the name. A `\Process V2(*)\Process ID` counter also exists |
| Cost per collect | 9.0 ms | 4.3 ms |

[verified: `pdh_recipe.py`, `pdh_cost.py`; re-run: 300 instances, 167 duplicates again, `Process V2` 0 duplicates,
`<verify>\v_pdh.py`]. Neither gives per-thread data. Per-thread data would need the `\Thread(*)` set, which costs
≈470–490 ms per collect (§1.4), or §3. For lag-doctor, §3 replaces both of them.

### 2.5 Thermal zone caveats

`\_TZ.THRM` is the firmware's ACPI thermal zone. It is not the CPU package temperature, and what it
measures is up to the laptop maker [inferred]. It is live here: 83.9–85.9 °C over 10 s under load
(§11). WMI `root\wmi MSAcpi_ThermalZoneTemperature` gives "Access denied" without admin; the same data
is readable through the PDH counter or `Win32_PerfFormattedData_Counters_ThermalZoneInformation`
[verified: `Get-CimInstance` of both, unelevated; re-run in the fact-check: "Access denied", and `\_TZ.THRM` 353 K,
3532, 100, 0 from the Perf class]. Without a kernel driver there is no CPU package or
per-core temperature [inferred].

---

## 3. Per-thread CPU of another process without a handle

### 3.1 x64 layouts

`NtQuerySystemInformation(SystemInformationClass=5, buf, len, &retlen)` (from `ntdll`) returns a chain of
`SYSTEM_PROCESS_INFORMATION` entries. Each entry is followed by `NumberOfThreads` ×
`SYSTEM_THREAD_INFORMATION` entries. `SystemProcessInformation = 5` and `STATUS_INFO_LENGTH_MISMATCH =
0xC0000004` [verified: `SDK\um\winternl.h`, `SDK\shared\ntstatus.h`]. Full field names come from phnt
`ntexapi.h` @53fbbdc. The SDK's `winternl.h` hides most of them as `Reserved*`. Offsets below were
computed by ctypes from the phnt definition and then checked against psutil for our own process (§3.2)
[verified: `<scratch>\ntqsi_threads.py`].

`SYSTEM_PROCESS_INFORMATION` (sizeof 0x100; the thread array starts at +0x100):

| Off | Field | Type | Off | Field | Type |
|---|---|---|---|---|---|
| 0x00 | NextEntryOffset | ULONG (0 = last) | 0x68 | UniqueProcessKey | ULONG_PTR |
| 0x04 | NumberOfThreads | ULONG | 0x70 | PeakVirtualSize | SIZE_T |
| 0x08 | WorkingSetPrivateSize | ULONGLONG | 0x78 | VirtualSize | SIZE_T |
| 0x10 | HardFaultCount | ULONG | 0x80 | PageFaultCount | ULONG |
| 0x14 | NumberOfThreadsHighWatermark | ULONG | 0x88 | PeakWorkingSetSize | SIZE_T |
| 0x18 | CycleTime | ULONGLONG (cycles, all threads) | 0x90 | WorkingSetSize | SIZE_T |
| 0x20 | CreateTime | LARGE_INTEGER (FILETIME) | 0xB8 | PagefileUsage | SIZE_T |
| 0x28 | UserTime | LARGE_INTEGER (100 ns) | 0xC8 | PrivatePageCount | SIZE_T |
| 0x30 | KernelTime | LARGE_INTEGER (100 ns) | 0xD0 | ReadOperationCount | LARGE_INTEGER |
| 0x38 | ImageName | UNICODE_STRING {USHORT Length; USHORT MaximumLength; pad; PWSTR Buffer@+8} | 0xD8 | WriteOperationCount | LARGE_INTEGER |
| 0x48 | BasePriority | LONG | 0xE0 | OtherOperationCount | LARGE_INTEGER |
| 0x50 | UniqueProcessId | HANDLE | 0xE8 | ReadTransferCount | LARGE_INTEGER (bytes) |
| 0x58 | InheritedFromUniqueProcessId | HANDLE | 0xF0 | WriteTransferCount | LARGE_INTEGER |
| 0x60 | HandleCount | ULONG | 0xF8 | OtherTransferCount | LARGE_INTEGER |
| 0x64 | SessionId | ULONG | 0xC0 | PeakPagefileUsage | SIZE_T |
| | | | 0x98–0xB0 | QuotaPeakPagedPoolUsage, QuotaPagedPoolUsage, QuotaPeakNonPagedPoolUsage, QuotaNonPagedPoolUsage | SIZE_T each |

`SYSTEM_THREAD_INFORMATION` (sizeof 0x50): `KernelTime@0x00`, `UserTime@0x08`, `CreateTime@0x10`
(all LARGE_INTEGER), `WaitTime@0x18` (ULONG), `StartAddress@0x20`, `ClientId@0x28`
{`UniqueProcess@0x28`, `UniqueThread@0x30`}, `Priority@0x38` (LONG), `BasePriority@0x3C` (LONG),
`ContextSwitches@0x40`, `ThreadState@0x44`, `WaitReason@0x48` (ULONG each).
`ThreadState`: 0 Initialized, 1 Ready, 2 Running, 3 Standby, 4 Terminated, 5 Waiting, 6 Transition,
7 DeferredReady [verified: phnt `ntkeapi.h` @53fbbdc, `KTHREAD_STATE`].

The `ImageName.Buffer` pointer points into your own buffer. A NULL pointer means pid 0
("System Idle Process") [verified: run].

### 3.2 Checked against psutil (our own process; psutil was read just after the snapshot; excerpt, with my notes in parentheses)

```
field                                           NtQSI         psutil / Win32
ImageName                                  python.exe             python.exe
InheritedFromUniqueProcessId                    25272                  25272
NumberOfThreads                                     4                      4
HandleCount                                       175                    176
CreateTime->unix                        1793835313.41          1793835313.41
UserTime s                                   0.203125                0.21875
KernelTime s                                    0.125                  0.125
WorkingSetSize                               23142400               24371200
QuotaPagedPoolUsage                            175368                 175368
ReadOperationCount                                 99                     99
ReadTransferCount                             1115931                1115931
SessionId                                          12                     12   (ProcessIdToSessionId)
BasePriority                                        8                     32   (psutil.nice() is the class 0x20)
thread ids equal to psutil.threads(): True (4 threads)
```

The fields that keep growing (times, working set, faults) differ by the work done between the two
reads. The fixed fields match exactly. The fact-check re-ran the comparison with its own ctypes structs built from
phnt (`<verify>\v_ntqsi.py`): `sizeof` came out 0x100 / 0x50 and every offset in the table above matched. ImageName,
parent pid, thread count, CreateTime, UserTime (0.0625 s), KernelTime (0.171875 s), read count and bytes, SessionId
and the thread IDs all matched psutil. `PagefileUsage` (13,230,080) equals psutil's `memory_info().vms`.

### 3.3 Buffer growth on `STATUS_INFO_LENGTH_MISMATCH`

```
attempt 0: buffer 65,536 -> NTSTATUS 0xC0000004, ReturnLength 574,848
attempt 1: buffer 640,384 -> NTSTATUS 0x00000000, ReturnLength 574,848
NULL/0 probe -> 0xC0000004, ReturnLength 574,848
```

When the call fails, it reports the size it needs. Grow to that size plus headroom, because the
process list can grow between the two calls. Then keep the buffer and reuse it on every poll
[verified: inline test; re-run in `<verify>\v_ntqsi.py`: NULL/0 probe and a 64 KiB buffer both → `0xC0000004` with
ReturnLength 571,240; a 636,776-byte buffer → 0].

### 3.4 Two snapshots → per-thread CPU% (100 % = one logical CPU)

```python
# lean parser used by the sampler (struct.unpack_from straight on the ctypes buffer)
u = struct.unpack_from
nxt, nthr = u("<II", b, off); create, user, kern = u("<qqq", b, off + 0x20); pid = u("<Q", b, off + 0x50)[0]
for i in range(nthr):                                    # only for the game's pid
    o = off + 0x100 + 0x50 * i
    tk, tu, tc = u("<qqq", b, o); tid = u("<Q", b, o + 0x30)[0]
    prio, _base, cs, state, wait = u("<llIII", b, o + 0x38)
# CPU% of a thread between snapshots A and B, timestamps from time.perf_counter_ns() at the middle of each call:
dt_100ns = (tB_ns - tA_ns) / 100
pct = 100.0 * ((kB + uB) - (kA + uA)) / dt_100ns         # only if CreateTime is unchanged (TIDs get reused)
```

With one of our own threads spinning [verified: `<scratch>\ntqsi_threads.py`]:

```
window 1.0s: top threads of our process (100% = one logical CPU); spinner tid = 20060
  tid  20060:   96.3%   (delta 10156250 x100ns; delta / 156250 = 65.00)
  tid  12220:    1.5%   (delta 156250 x100ns; delta / 156250 = 1.00)
window 0.25s:
  tid  20060:   93.8%   (delta 2812500 x100ns; delta / 156250 = 18.00)
  tid  12220:   15.6%   (delta 468750 x100ns; delta / 156250 = 3.00)
thread time values that are exact multiples of 156250 (100ns): 3567/3567
```

Fact-check re-run with its own spinner (`<verify>\v_ntqsi.py`): 1.0 s window 93.1 % (61 ticks); three 0.25 s
windows 101.7 %, 96.6 %, 97.9 % (18, 17, 18 ticks). So a single 250 ms value above 100 % is normal [verified].

### 3.5 Quantisation (important)

Thread and process times only move in clock ticks of 156,250 × 100 ns (15.625 ms). All 3,567 non-zero
values on the system were exact multiples [verified: run above; re-run 3,486 of 3,486 in `<verify>\v_ntqsi.py`].
156,250 is the clock increment that `GetSystemTimeAdjustment` reports (`lpTimeIncrement = 156250`). The step
stayed 156,250 even though another program had raised the timer resolution to 10 ms
(`NtQueryTimerResolution`: coarsest 156250, finest 5000, current 100000) [verified: same run; signature from phnt
`ntexapi.h` @53fbbdc]. So a game calling `timeBeginPeriod(1)` does not make thread times finer [inferred].
Over a 250 ms window, one thread's CPU%
is therefore only accurate to ±6.25 % of a core. The sampler logged 104 % for a single thread over one
window (§11). **Compute per-thread CPU over a 1-second window, which gives ±1.6 %, and clamp each thread
to 100 %** [inferred from the measurements]. `CycleTime` at +0x18 is counted in cycles and so is not
tick-quantised, but it exists only per process, and cycles cannot be turned into time while the
frequency keeps changing [inferred].

### 3.6 Cost

306 processes and 4,834 threads take 587 KB. The call alone costs 6.6 ms of wall time and 6.7 ms of CPU.
A full Python parse into dicts costs 26.8 ms, which is too slow. The lean parser in §3.4 (names cached
by (pid, CreateTime), threads only for the game) costs 6.9 ms per poll in total [verified:
`ntqsi_threads.py`, `sampler_bench.py`]. Fact-check run: 300 processes, 4,682 threads, 570 KB, 5.52 ms median
(max 8.25), 5.47 ms CPU per call; a parse of everything into dicts through ctypes `from_address` structs took
13.1 ms [verified: `<verify>\v_ntqsi.py`]. For comparison, `psutil.process_iter(['name','pid'])` costs 5.2 ms,
**but it opens a handle to every process, the game included**. It builds a `Process(pid)` for each pid, and the
constructor itself opens a handle (§9.4) [verified: psutil source at `release-7.2.2`, `psutil/__init__.py`
`process_iter` → `add(pid)` → `Process(pid)`].

### 3.7 P-cores vs E-cores

**No. Without admin you cannot tell which cores a given thread ran on.** `SYSTEM_THREAD_INFORMATION` has
no processor field [verified: layout above]. The usual alternatives each need something we don't have:
ETW CSwitch events from a kernel trace session need admin or membership of Performance Log Users (the
user is not a member) [unverified: not tested here], and `GetThreadIdealProcessorEx` needs a handle to
one of the game's threads [verified: `GetThreadIdealProcessorEx(_In_ HANDLE hThread, …)` in
`SDK\um\processthreadsapi.h`; ruled out by the scope rules].

What is possible without admin is a map of which logical processors are P and E, and how loaded each
class is. `GetSystemCpuSetInformation(buf, len, &ret, NULL, 0)` returns 32-byte
`SYSTEM_CPU_SET_INFORMATION` entries. `EfficiencyClass` is at +18 and `AllFlags` at +19 (bit 0 =
Parked); `SchedulingClass` is the low byte of the DWORD at +20 [verified: `SDK\um\winnt.h`; run below]. A higher
`EfficiencyClass` means faster but less efficient: *"CPU Sets with higher numerical values of this field have home
processors that are faster but less power-efficient"* [verified:
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-system_cpu_set_information], so class 1 = P-core
here. A size probe with a NULL buffer fails with error 122 and returns the size needed (512). `Parked` changes from
moment to moment: in the fact-check run LPs 0 and 11 were parked [verified: `<verify>\v_cpusets.py`]:

```
GetSystemCpuSetInformation ok = True bytes = 512
 lp core llc eff parked sched id
  0    0   0   1      0     1 256      ... lp 0-11: eff 1 (P-cores, 2 threads per core; lp 4-7 have SchedulingClass 2)
 12   12   0   0      0     0 268      ... lp 12-15: eff 0 (E-cores, 1 thread each)
GetLogicalProcessorInformationEx(RelationProcessorCore): core eff=1 smt=1 lps=[0, 1]; ... core eff=0 smt=0 lps=[15]
% Processor Utility: class 0: mean 10.3 over 4 LPs (max 12.1), class 1: mean 16.0 over 12 LPs (max 39.3)
Actual Frequency MHz: class 0: mean 1344.6 over 4 LPs, class 1: mean 1246.0 over 12 LPs
```

Map PDH `Processor Information(0,N)` → logical processor N → `EfficiencyClass`. A game thread at about
100 % of one core while the P-cores show spare capacity and the E-cores are pegged suggests the game was
scheduled onto E-cores, but that is a guess, not something we can measure [inferred].

---

## 4. NVIDIA: `nvidia-smi` and NVML

### 4.1 Field names this driver accepts

`nvidia-smi --help-query-gpu` (v591.86) lists both spellings: *"clocks_event_reasons.active" or
"clocks_throttle_reasons.active"* — the same holds for every reason field [verified: local help
output]. Use these fields:

- `utilization.gpu`, `utilization.memory`. The help says *"The sample period may be between 1 second and 1/6 second depending on the product."*
- `memory.used`, `memory.total`, `memory.reserved`
- `temperature.gpu`, `temperature.gpu.tlimit`
- `power.draw`, which on Ampere and newer is *"average power draw over 1 sec"*; `power.draw.instant`; `enforced.power.limit`
- `clocks.current.graphics` (alias `clocks.gr`), `clocks.max.graphics`, `clocks.current.memory`, `clocks.max.memory`
- `pstate`
- `clocks_event_reasons.active` (a bitmask), plus `.gpu_idle`, `.sw_power_cap`, `.hw_slowdown`, `.hw_thermal_slowdown`, `.sw_thermal_slowdown` and `.hw_power_brake_slowdown`
- `clocks_event_reasons_counters.sw_thermal_slowdown`, `.hw_thermal_slowdown`, `.sw_power_cap`, `.hw_power_brake_slowdown`, `.sync_boost`:
  *"counters in microseconds"* of the time each reason held the clocks down. Two readings give the share of a
  window spent throttled, which is a better signal than a bit that is either set or not. Here:
  `22039776617 us, 0 us, 183506090 us, 0 us, 0 us`, i.e. about 6.1 h of SW thermal slowdown accumulated (since
  driver load or boot, which one was not checked) [verified: `<verify>\nvsmi-help-query-gpu.txt`, one query run]
- `display_active`

Every field above is in this driver's `--help-query-gpu` output, which has 725 lines [verified: `<verify>\nvsmi-help-query-gpu.txt`,
`nvidia-smi --version` → 591.86, NVML 591.86, CUDA 13.1].

The bits come from `nvml.h` [verified: https://raw.githubusercontent.com/NVIDIA/go-nvml/v0.13.4-1/pkg/nvml/nvml.h]:

| Bit | Name |
|---|---|
| 0x1 | GpuIdle |
| 0x2 | ApplicationsClocksSetting |
| 0x4 | SwPowerCap |
| 0x8 | HwSlowdown |
| 0x10 | SyncBoost |
| 0x20 | SwThermalSlowdown |
| 0x40 | HwThermalSlowdown |
| 0x80 | HwPowerBrakeSlowdown |
| 0x100 | DisplayClockSetting |
| 0x200 | BoardLimit (*"Board limit policy is limiting clocks"*) |
| 0x400 | Reliability (*"Reliability policy is limiting clocks"*) |

Header v0.13.4-1 defines the hardware bits only under their `…ThrottleReason…` names [verified: re-fetched,
`nvml.h` lines 3704–3800; 0x200 and 0x400 were missing from this table before the fact-check].

### 4.2 Streaming mode, a real run

`-lms 250` is *"Probe until Ctrl+C at specified millisecond interval"* [verified: `nvidia-smi -h`].
There is no sample-count option. Do not reach for `-c`: in nvidia-smi it means `--compute-mode`, a
setter. Run the process and terminate it yourself.

```
C:\Windows\system32\nvidia-smi.exe --query-gpu=timestamp,index,name,pci.bus_id,utilization.gpu,utilization.memory,memory.used,memory.total,memory.reserved,temperature.gpu,power.draw,power.draw.instant,enforced.power.limit,clocks.current.graphics,clocks.max.graphics,clocks.current.memory,clocks.max.memory,pstate,clocks_event_reasons.active,clocks_event_reasons.gpu_idle,clocks_event_reasons.sw_power_cap,clocks_event_reasons.hw_slowdown,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_power_brake_slowdown,display_active --format=csv,nounits -lms 250
```
```
  +   75.1 ms | timestamp, index, name, pci.bus_id, utilization.gpu [%], utilization.memory [%], memory.used [MiB], memory.total [MiB], memory.reserved [MiB], temperature.gpu, power.draw [W], power.draw.instant [W], enforced.power.limit [W], clocks.current.graphics [MHz], clocks.max.graphics [MHz], clocks.current.memory [MHz], clocks.max.memory [MHz], pstate, clocks_event_reasons.active, ..., display_active
  +   87.5 ms | 2026/11/04 15:38:58.669, 0, NVIDIA GeForce RTX 3070 Laptop GPU, 00000000:01:00.0, 65, 15, 4153, 8192, 174, 83, 39.17, 46.56, 87.70, 780, 2100, 5470, 6001, P3, 0x0000000000000020, Not Active, Not Active, Not Active, Not Active, Active, Not Active, Disabled
  +  338.8 ms | 2026/11/04 15:38:58.933, 0, NVIDIA GeForce RTX 3070 Laptop GPU, 00000000:01:00.0, 65, 15, 4153, 8192, 174, 83, 39.17, 46.56, 87.70, 780, 2100, 5470, 6001, P3, 0x0000000000000020, Not Active, Not Active, Not Active, Not Active, Active, Not Active, Disabled
  +  594.5 ms | 2026/11/04 15:38:59.188, 0, NVIDIA GeForce RTX 3070 Laptop GPU, 00000000:01:00.0, 100, 25, 4153, 8192, 174, 85, 58.48, 58.48, 88.12, 780, 2100, 5470, 6001, P3, 0x0000000000000020, Not Active, Not Active, Not Active, Not Active, Active, Not Active, Disabled
  sample gaps: median 256.4 ms, min 251.3, max 259.7
  nvidia-smi child CPU after 2.65s: user 0.016s + kernel 0.047s = 2.4% of one core (includes start-up)
  terminated, exit code 1 | surviving nvidia-smi: []
```

[verified: `<scratch>\nvidia_probe.py`; re-run in `<verify>\v_nvsmi.py`: gaps 255.5–256.1 ms, rows identical in
pairs, exit code 1 after `terminate()`, no survivor]. The first two samples are identical: the driver refreshes about
every 500 ms (§4.4). The GPU is in thermal slowdown here: 0x20 is SwThermalSlowdown, the graphics clock
is 780 of 2100 MHz, and the temperature is 83–85 °C. That is a real lag cause, caught live. Watch the
format details: `--format=csv,nounits` still prints units in the header line, and the boolean reason
fields are the strings `Active` / `Not Active`. `Popen.terminate()` exits with code 1 (TerminateProcess),
and afterwards no `nvidia-smi.exe` was left running [verified].

### 4.3 One-shot cost

Spawning `nvidia-smi --query-gpu=… --format=csv,noheader,nounits` once per sample costs a median 96 ms
and a maximum of 102 ms of wall time per call [verified: `nvidia_probe.py`; fact-check: median 106 ms, max 123 ms
over 8 calls]. Use streaming or NVML.

### 4.4 NVML via ctypes (better)

```python
nv = ctypes.CDLL(r"C:\Windows\System32\nvml.dll")      # 8.17.15.9186, shipped with the driver
nv.nvmlInit_v2()                                        # 0 = NVML_SUCCESS
dev = ctypes.c_void_p(); nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
class Util(ctypes.Structure): _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]
class Mem(ctypes.Structure):  _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]
u, m, t, p, lim, clk, mx, ps, disp = Util(), Mem(), *(ctypes.c_uint() for _ in range(7))
r = ctypes.c_ulonglong()
nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
nv.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(m))
nv.nvmlDeviceGetTemperature(dev, 0, ctypes.byref(t))         # NVML_TEMPERATURE_GPU = 0, deg C
nv.nvmlDeviceGetPowerUsage(dev, ctypes.byref(p))             # milliwatts
nv.nvmlDeviceGetEnforcedPowerLimit(dev, ctypes.byref(lim))   # milliwatts
nv.nvmlDeviceGetClockInfo(dev, 0, ctypes.byref(clk))         # NVML_CLOCK_GRAPHICS = 0, MHz
nv.nvmlDeviceGetMaxClockInfo(dev, 0, ctypes.byref(mx))
nv.nvmlDeviceGetPerformanceState(dev, ctypes.byref(ps))      # 0 = P0 ... 32 = unknown
nv.nvmlDeviceGetCurrentClocksEventReasons(dev, ctypes.byref(r))
nv.nvmlDeviceGetDisplayActive(dev, ctypes.byref(disp))
nv.nvmlShutdown()
```

Names, enum values and units come from nvml.h at go-nvml v0.13.4-1 (`NVML_TEMPERATURE_GPU = 0`,
`NVML_CLOCK_GRAPHICS = 0`, power in milliwatts, `nvmlMemory_t.used` = *"Sum of Reserved and Allocated"*)
[verified]. Output:

```
  nvmlInit_v2 -> 0
  driver version: 591.86
  nvmlDeviceGetPciInfo_v3 -> 0 busId=00000000:01:00.0 pciDeviceId=0x249D10DE subsys=0x136D1043
  exports nvmlDeviceGetCurrentClocksEventReasons: True | nvmlDeviceGetTemperatureV: True
  sample 0: rc=[0,0,0,0,0,0,0,0,0,0] util gpu=62% mem=13% | vram used=4341 MiB of 8192 MiB | 84 C | 44.3 W of 89.1 W | gfx 780/2100 MHz | P3 | reasons=0x20 | display_active=0 | 3.12 ms
  sample 1: ... | 0.62 ms
  sample 2: ... | 0.32 ms
2000 iterations of 3 calls: rc set={0}, distinct (util,power) values seen=1, mean wall 0.0066 ms
value changes over 3 s (ms, (util%, mW)): [(0, (100, 56121)), (166, (100, 57205)), (666, (100, 58337)), (1167, (99, 58702)), (1666, (99, 58509)), (2175, (100, 61280)), (2665, (37, 38519))]
```

[verified: `nvidia_probe.py`, `nvml_loop.py`].

**Verdict: use NVML.**

- Most calls cost about 1.3 µs. **Correction:** `nvmlDeviceGetDisplayActive` costs a median 167 µs (p90 294 µs, max
  1.24 ms) and `nvmlDeviceGetMemoryInfo` 3.7 µs. The full 10-call sample above costs a median 208 µs, almost all of
  it `GetDisplayActive` [verified: `<verify>\v_nvml_cost.py`, 300 calls each]. Read `display_active` on a slow tier.
- No child process is needed.
- You get typed values, and there is no CSV to parse.

Re-run with the same calls (`<verify>\v_nvml.py`): every call returned 0; `busId=00000000:01:00.0`; values changed
at intervals of `[320, 500, 500, 500, 500, 502]` ms; `nvml.dll` FileVersion `8.17.15.9186`, NVIDIA Corporation
[verified]. "VRAM used" from NVML (4,282 MiB) equals nvidia-smi `memory.used` (4,109) plus `memory.reserved`
(174), which is what the header comment *"Sum of Reserved and Allocated"* says [verified: same moment].

**Throttle time, not just the throttle bit.** `nvmlDeviceGetFieldValues` with field
`NVML_FI_DEV_CLOCKS_EVENT_REASON_SW_THERM_SLOWDOWN = 269` returns the accumulated nanoseconds of SW thermal
slowdown. Related fields: `…HW_THERM_SLOWDOWN = 270`, `…HW_POWER_BRAKE_SLOWDOWN = 271`,
`…SW_POWER_CAP = NVML_FI_DEV_PERF_POLICY_POWER = 74`, `…SYNC_BOOST = 76`. `nvmlFieldValue_t` is 40 bytes:
`{uint fieldId; uint scopeId; int64 timestamp (µs since 1970); int64 latencyUsec; int valueType; int nvmlReturn;
union value}`. Check each entry's `nvmlReturn` [verified: go-nvml v0.13.4-1 `nvml.h` lines 2727–2729,
3097–3101, 3270–3279]. Run here:

```python
class Val(ctypes.Union): _fields_ = [("d", ctypes.c_double), ("ull", ctypes.c_ulonglong)]
class FV(ctypes.Structure):
    _fields_ = [("fieldId", ctypes.c_uint), ("scopeId", ctypes.c_uint), ("timestamp", ctypes.c_longlong),
                ("latencyUsec", ctypes.c_longlong), ("valueType", ctypes.c_int), ("nvmlReturn", ctypes.c_int), ("value", Val)]
fv = (FV * 1)(FV(fieldId=269)); nv.nvmlDeviceGetFieldValues(dev, 1, fv)      # rc 0; fv[0].nvmlReturn 0; valueType 3
# throttled share of a window = (b.value.ull - a.value.ull) / 1e3 / (b.timestamp - a.timestamp)
```
```
  field 269: ret=0 type=3 value=22,189,328,775,552 -> 22,190,328,452,800 over 1.002 s = 99.8% of the time
  field 74: ret=0 type=3 value=188,938,987,360 -> 188,938,987,360 over 1.002 s = 0.0% of the time
  field 270: ret=0 type=3 value=0 -> 0 over 1.001 s = 0.0% of the time
```
[verified: `<verify>\v_nvml.py`; one call costs 1.4 µs]

Caveats:

1. The values refresh about every 500 ms here, so at 4 Hz every other sample repeats [verified].
2. "VRAM used" has three different definitions on this machine [verified]:
   - NVML `used`: 4,341 MiB (includes reserved memory)
   - nvidia-smi `memory.used`: 4,153 MiB (allocated by active contexts)
   - PDH `GPU Adapter Memory\Dedicated Usage`: 4,190 MiB (Windows VidMm)
3. `nvmlDeviceGetTemperature` is marked `DEPRECATED(13.0)` in the header but works with this driver
   [verified]. So is `nvmlDeviceGetCurrentClocksThrottleReasons`; use `…EventReasons` [verified: header line 6649].
   `nvmlDeviceGetTemperatureV(device, nvmlTemperature_t*)` is exported (struct `{unsigned int version;
   nvmlTemperatureSensors_t sensorType; int temperature;}`), but nobody called it here [verified: export present; header].
4. On an Optimus laptop, NVML queries may wake an idle dGPU [unverified: could not test, because the
   dGPU was busy the whole time]. Query NVML only while the game's GPU Engine instances are on the NVIDIA LUID.

### 4.5 NVML device ↔ LUID

NVML has no LUID. Match it by PCI location instead: `nvmlDeviceGetPciInfo_v3` gave
`busId=00000000:01:00.0`, and D3DKMT `KMTQAITYPE_ADAPTERADDRESS` for LUID 0x10F83 gave
bus/device/function `[1, 0, 0]` (§5.3) [verified: both runs, re-run in `<verify>\v_nvml.py` and `v_gpu.py`].
`nvmlPciInfo_t` is 68 bytes: `busIdLegacy[16]`, domain, bus, device, pciDeviceId, pciSubSystemId, `busId[32]`
[verified: header lines 202–214; ctypes sizeof]. It has no function field: take bus and device from the struct
and the function from the `busId` string (`00000000:01:00.0`), then compare with D3DKMT's three numbers.

---

## 5. LUID → adapter name, VRAM, integrated or discrete

### 5.1 DXGI (vtable indices from `SDK\shared\dxgi.h`, by counting the C `…Vtbl` structs)

- `IDXGIFactory1`: 0 QueryInterface, 1 AddRef, 2 Release, 3–6 IDXGIObject, 7 EnumAdapters, 8
  MakeWindowAssociation, 9 GetWindowAssociation, 10 CreateSwapChain, 11 CreateSoftwareAdapter,
  **12 EnumAdapters1**, 13 IsCurrent.
- `IDXGIAdapter1`: **7 EnumOutputs**, 8 GetDesc, 9 CheckInterfaceSupport, **10 GetDesc1**.
- `IDXGIOutput`: **7 GetDesc**.
- `IID_IDXGIFactory1 = 770aae78-f26f-4dba-a829-253c83d1b387`.
- `DXGI_ERROR_NOT_FOUND = 0x887A0002`.
- `DXGI_ADAPTER_DESC1` = {WCHAR Description[128]; UINT VendorId, DeviceId, SubSysId, Revision;
  SIZE_T DedicatedVideoMemory, DedicatedSystemMemory, SharedSystemMemory; LUID AdapterLuid; UINT Flags}.
- Flags: 2 = `DXGI_ADAPTER_FLAG_SOFTWARE`.

[verified: `SDK\shared\dxgi.h`, `SDK\shared\winerror.h`]

```python
def vcall(obj, index, restype, *argtypes):              # call COM vtable slot `index`
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *a: fn(obj, *a)
factory = ctypes.c_void_p()
dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory))
EnumAdapters1 = vcall(factory, 12, HRESULT, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
# loop i until hr == DXGI_ERROR_NOT_FOUND:
vcall(adapter, 10, HRESULT, ctypes.POINTER(DXGI_ADAPTER_DESC1))(ctypes.byref(desc))     # GetDesc1
vcall(adapter, 7, HRESULT, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(j, ctypes.byref(out))  # EnumOutputs
vcall(out, 7, HRESULT, ctypes.POINTER(DXGI_OUTPUT_DESC))(ctypes.byref(od))              # IDXGIOutput::GetDesc
vcall(obj, 2, ctypes.c_ulong)()                                                          # Release
luid = f"luid_0x{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08X}_0x{desc.AdapterLuid.LowPart:08X}"
```

```
[DXGI 0] 'Intel(R) UHD Graphics' vendor=0x8086 device=0x46A3 subsys=0x136D1043 luid_0x00000000_0x00010C49 dedicatedVRAM=128 MiB dedicatedSys=0 MiB shared=16194 MiB flags=0
      output 0: \\.\DISPLAY1 desktop=(0,0)-(1920,1080) attached=True
[DXGI 1] 'NVIDIA GeForce RTX 3070 Laptop GPU' vendor=0x10DE device=0x249D subsys=0x136D1043 luid_0x00000000_0x00010F83 dedicatedVRAM=8018 MiB dedicatedSys=0 MiB shared=16194 MiB flags=0
      (no outputs)
[DXGI 2] 'Microsoft Basic Render Driver' vendor=0x1414 device=0x008C subsys=0x00000000 luid_0x00000000_0x00010EF4 dedicatedVRAM=0 MiB dedicatedSys=0 MiB shared=16194 MiB flags=2
      (no outputs)
```

[verified: `<scratch>\gpu_adapters.py`; re-run in `<verify>\v_gpu.py`, identical; vtable slots re-counted
mechanically from the `…Vtbl` structs in `SDK\shared\dxgi.h` by `<verify>\v_vtbl.py`]. LUIDs are only valid until the next reboot or driver restart
[inferred], so build this map at startup and rebuild it whenever you see an unknown LUID.

### 5.2 Integrated or discrete: DXCore (`dxcore.dll`), by LUID

Interfaces from `SDK\um\dxcore_interface.h` (C++ only, so slots are counted in declaration order):

- `IDXCoreAdapterFactory` `78ee5945-c36e-4b13-a669-005dd11c0f06`: 3 CreateAdapterList, **4 GetAdapterByLuid(const LUID&, REFIID, void**)**.
- `IDXCoreAdapter` `f0db4c7f-fe5a-42a2-bd62-f2a6cf6fc83e`: 3 IsValid, 4 IsAttributeSupported, **5 IsPropertySupported**, **6 GetProperty(prop, size_t, void*)**, **7 GetPropertySize**.
- `DXCoreAdapterProperty` values: DriverDescription 2, DedicatedAdapterMemory 7, IsHardware 11, **IsIntegrated 12**, IsDetachable 13.

[verified: header; run. Fact-check: the slot numbers were re-counted from the `virtual` declarations only. The
header's template overloads of `GetProperty`, `CreateAdapterList` and `GetAdapterByLuid` are non-virtual and take no
slot. Both IIDs match the `MIDL_INTERFACE` lines, and `<verify>\v_gpu.py` printed the three lines below again]

```
  luid_0x00000000_0x00010C49: 'Intel(R) UHD Graphics' IsHardware=True IsIntegrated=True IsDetachable=False dedicated=128 MiB
  luid_0x00000000_0x00010F83: 'NVIDIA GeForce RTX 3070 Laptop GPU' IsHardware=True IsIntegrated=False IsDetachable=False dedicated=8018 MiB
  luid_0x00000000_0x00010EF4: 'Microsoft Basic Render Driver' IsHardware=False IsIntegrated=False IsDetachable=False dedicated=0 MiB
```

### 5.3 D3DKMT (gdi32): hybrid role, HAGS state, PCI address

The definitions are in `SDK\shared\d3dkmthk.h` and `SDK\shared\d3dkmdt.h`:

- `D3DKMT_OPENADAPTERFROMLUID` = {LUID AdapterLuid; UINT hAdapter}.
- `D3DKMT_QUERYADAPTERINFO` = {UINT hAdapter; int Type; void* pPrivateDriverData; UINT PrivateDriverDataSize}.
- Query types: `KMTQAITYPE_ADAPTERADDRESS = 6`, `KMTQAITYPE_ADAPTERTYPE = 15`, `KMTQAITYPE_WDDM_2_7_CAPS = 70`.
- `D3DKMT_WDDM_2_7_CAPS` bits: 0 HwSchSupported, 1 HwSchEnabled (*"whether the hardware scheduling is
  currently enabled for this GPU"*), 2 HwSchEnabledByDefault, 3 IndependentVidPnVSyncControl.
- `D3DKMT_ADAPTERTYPE` bits 0–15: RenderSupported, DisplaySupported, SoftwareDevice, PostDevice,
  HybridDiscrete, HybridIntegrated, IndirectDisplayDevice, Paravirtualized, ACGSupported,
  SupportSetTimingsFromVidPn, Detachable, ComputeOnly, Prototype, RuntimePowerManagement, TestOnly,
  SingleAdapterHybridMode.

[verified: SDK headers; https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/ns-d3dkmthk-_d3dkmt_adaptertype for bits 13–15]

```
  'Intel(R) UHD Graphics': ADAPTERTYPE value=0x0000232B [Render, Display, PostDevice, HybridIntegrated, ACGSupported, SupportSetTimingsFromVidPn, +bit13 RuntimePowerManagement]
      WDDM_2_7_CAPS value=0x1 HwSchSupported=1 HwSchEnabled=0 HwSchEnabledByDefault=0 | ADAPTERADDRESS bus/dev/fn=[0, 2, 0]
  'NVIDIA GeForce RTX 3070 Laptop GPU': ADAPTERTYPE value=0x00002313 [Render, Display, HybridDiscrete, ACGSupported, SupportSetTimingsFromVidPn, +bit13]
      WDDM_2_7_CAPS value=0xB HwSchSupported=1 HwSchEnabled=1 HwSchEnabledByDefault=0 | ADAPTERADDRESS bus/dev/fn=[1, 0, 0]
  'Microsoft Basic Render Driver': ADAPTERTYPE value=0x00000105 [Render, SoftwareDevice, ACGSupported]
```

Always close the handle with `D3DKMTCloseAdapter(&hAdapter)`. **HAGS is on for the NVIDIA GPU**, which
agrees with the registry value `HwSchMode = 2` (§8). The fact-check re-run printed the same values; the Basic
Render Driver's `ADAPTERADDRESS` comes back as `[4294967295, 65535, 65535]` (no PCI location), and its WDDM 2.7
caps as 0 [verified: `<verify>\v_gpu.py`]. The 19041 SDK header stops the `D3DKMT_ADAPTERTYPE` bitfield at bit 12
(`Prototype`); bits 13–15 come only from the online page [verified: both].

---

## 6. Displays

### 6.1 Per monitor: current mode, best refresh at this resolution, adapter, internal panel

The definitions are in `SDK\um\wingdi.h` and `SDK\um\winuser.h`:

- Structs: `DISPLAY_DEVICEW` and `DEVMODEW` (220 bytes, display union flattened: dmPositionX/Y,
  dmDisplayOrientation, dmDisplayFixedOutput). `DISPLAYCONFIG_PATH_INFO` is 72 bytes,
  `DISPLAYCONFIG_MODE_INFO` 64 bytes and `DISPLAYCONFIG_TARGET_DEVICE_NAME` 420 bytes; the script asserts all three sizes.
  `DISPLAY_DEVICEW` is 840 bytes and `DISPLAYCONFIG_SOURCE_DEVICE_NAME` 84 [verified: ctypes sizeof in `<verify>\v_displays.py`,
  and the API accepted each `header.size`].
- Flags and fields: `ENUM_CURRENT_SETTINGS = (DWORD)-1`, `DM_PELSWIDTH 0x80000`, `DM_PELSHEIGHT 0x100000`,
  `DM_DISPLAYFREQUENCY 0x400000`, `CDS_UPDATEREGISTRY 1`, `CDS_TEST 2`, `QDC_ONLY_ACTIVE_PATHS 2`.
- `DisplayConfigGetDeviceInfo` types: `DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME 1`, `…GET_TARGET_NAME 2`.

[verified: headers; `<scratch>\displays.py`]

Method:

1. `EnumDisplayDevicesW(NULL, i)` lists the adapters' outputs (`\\.\DISPLAYn`, `DeviceString` = adapter
   name). Keep the ones with `StateFlags & 1` (attached to desktop).
2. `EnumDisplayDevicesW(name, 0)` gives the monitor on that output.
3. `EnumDisplaySettingsW(name, ENUM_CURRENT_SETTINGS)` gives the current mode.
4. `EnumDisplaySettingsW(name, k = 0, 1, …)` lists every mode. Filter to the current width, height and
   bpp, and take the highest `dmDisplayFrequency`.
5. `QueryDisplayConfig` gives the exact refresh rate as a fraction, the adapter LUID and the output
   technology. Join it on `viewGdiDeviceName`.

```
\\.\DISPLAY1 adapter='Intel(R) UHD Graphics' flags=0x5 (primary=True) monitor='Generic PnP Monitor' id='MONITOR\\CMN153C\\{4d36e96e-e325-1'
   current 1920x1080 32bpp @ 144 Hz; 66 modes; refresh rates at this resolution: [60, 144]; max 144 Hz
   ChangeDisplaySettingsExW(..., 144 Hz, CDS_TEST) -> 0 DISP_CHANGE_SUCCESSFUL  [current]
   ChangeDisplaySettingsExW(..., 144 Hz, CDS_TEST) -> 0 DISP_CHANGE_SUCCESSFUL  [max]
   mode after CDS_TEST calls: 1920x1080 @ 144 Hz (unchanged = True)
\\.\DISPLAY2..4 adapter='Intel(R) UHD Graphics' flags=0x0 (not attached to desktop)
\\.\DISPLAY5..8 adapter='NVIDIA GeForce RTX 3070 Laptop GPU' flags=0x0 (not attached to desktop)

== QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS) ==
rc=0 paths=1 modes=2
\\.\DISPLAY1 (rc 0/0) adapter luid_0x00000000_0x00010C49 monitor='' tech=INTERNAL internal=True refresh=342060000/2375360 = 144.003 Hz
```

The fact-check re-run (`<verify>\v_displays.py`, `ChangeDisplaySettingsExW` only ever called with `CDS_TEST`)
printed the same modes, paths and fraction, plus three facts the implementation needs [verified]:

- **`CDS_TEST` really validates.** A negative control, 1920x1080 at 997 Hz (not an enumerated mode), returned
  `-2` (`DISP_CHANGE_BADMODE`); the mode was unchanged afterwards.
- **`EnumDisplaySettingsW` rewrites `dmSize`** to 188 (the display part of `DEVMODEW`) in the struct it fills,
  though 220 was passed in. Both sizes are accepted. Don't assert `dmSize == 220` on a returned mode.
- **The panel's largest mode is 1920x1080** (60 and 144 Hz; 66 modes in all; `ENUM_REGISTRY_SETTINGS` =
  1920x1080@144). `DisplayConfigGetDeviceInfo` with `header.size = 416` instead of 420 returns 87
  (`ERROR_INVALID_PARAMETER`), which confirms the 420-byte struct.

### 6.2 Is it the built-in panel?

Look at `DISPLAYCONFIG_PATH_TARGET_INFO.outputTechnology`. `DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL =
0x80000000` means the panel is built in, and so do `LVDS 6`, `DISPLAYPORT_EMBEDDED 11` and
`UDI_EMBEDDED 13`. External outputs include `HDMI 5`, `DISPLAYPORT_EXTERNAL 10`, `DVI 4` and
`MIRACAST 15` [verified: `SDK\um\wingdi.h`; this panel reported INTERNAL]. The panel has no EDID
friendly name (`monitorFriendlyDeviceName` is empty), so identify it by `monitorDevicePath` or the
`MONITOR\CMN153C` ID [verified: run].

### 6.3 `Win32_VideoController` is stale here

`Get-CimInstance Win32_VideoController` reports `NVIDIA … CurrentHorizontalResolution 2560, Vertical 1440,
CurrentRefreshRate 143`, but no display is attached to the NVIDIA adapter: DXGI shows no outputs,
EnumDisplayDevices shows no attached device on the NVIDIA outputs, and QueryDisplayConfig has one path
(Intel). It also gives `AdapterRAM 4293918720` for an 8 GB GPU, because the field tops out at 4 GB
[verified: all four readouts above; re-read in the fact-check, same values]. That is where the
"2560x1440@143Hz on NVIDIA" in the task brief came from. **Where the stale 2560x1440 itself comes from is
unknown** [unverified]. The researcher guessed "the last external monitor", but `Win32_DesktopMonitor` reports
2560x1440 for the internal panel's own PnP ID (`DISPLAY\CMN153C\4&25E05DB5&0&UID8388688`), and that panel's
largest enumerated mode is 1920x1080, so the guess is not established [verified: `Get-CimInstance
Win32_DesktopMonitor`; `<verify>\v_modes.py`]. **Do not use Win32_VideoController or Win32_DesktopMonitor for
modes or VRAM.** Use §5 and §6.1. Also, 143 vs 144 Hz is integer rounding:
the panel's real rate is 144.003 Hz, and an external monitor at, say, 143.98 Hz would show as 143 in
`dmDisplayFrequency` [inferred].

### 6.4 Fix: set the highest refresh rate, and restore it

Only CDS_TEST was run here. The rest is the documented sequence, not executed.

```python
saved = current DEVMODEW from EnumDisplaySettingsW(dev, ENUM_CURRENT_SETTINGS)    # persist to lag-doctor's state file first
dm = DEVMODEW(dmSize=220)
dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = saved.dmPelsWidth, saved.dmPelsHeight, best_hz
dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
r = user32.ChangeDisplaySettingsExW(dev, ctypes.byref(dm), None, CDS_TEST, None)        # must be 0
if r == 0:
    r = user32.ChangeDisplaySettingsExW(dev, ctypes.byref(dm), None, CDS_UPDATEREGISTRY, None)
# verify: EnumDisplaySettingsW(ENUM_CURRENT_SETTINGS).dmDisplayFrequency == best_hz AND the QueryDisplayConfig refresh fraction
# restore: same two calls with saved.dmDisplayFrequency
```

Microsoft's documentation of these flags and return codes
[verified: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw]:

- `CDS_TEST`: *"Specifying CDS_TEST allows an application to determine which graphics modes are
  actually valid, without causing the system to change to them."*
- `CDS_UPDATEREGISTRY` changes the mode immediately and *"The mode information is stored in the USER
  profile"*, so no admin is needed [inferred from the per-user storage; the change itself was not run]. Don't
  add `CDS_GLOBAL` (8), which saves *"in the global settings area so that they will affect all users"*.
- `DISP_CHANGE_RESTART` (1) means a reboot is needed: report it and do not count it as success.
- `DISP_CHANGE_BADMODE` is -2. The full list is in winuser.h.
- *"use the DEVMODE returned by the EnumDisplaySettings function"*: build the target mode from one that
  enumeration returned.

A GPU control panel or Windows' Dynamic Refresh Rate may later override the change [unverified]:
check it again after a while.

---

## 7. Power

### 7.1 Active scheme, its name, and its processor settings (documented `powrprof.dll`)

```python
pg = ctypes.POINTER(GUID)(); pp.PowerGetActiveScheme(None, ctypes.byref(pg))   # LocalFree(pg) afterwards
size = wt.DWORD(0); pp.PowerReadFriendlyName(None, ctypes.byref(g), None, None, None, ctypes.byref(size))
buf = ctypes.create_string_buffer(size.value); pp.PowerReadFriendlyName(None, ctypes.byref(g), None, None, buf, ctypes.byref(size))
name = buf.raw.decode("utf-16-le").rstrip("\0")
pp.PowerReadACValueIndex(None, ctypes.byref(g), ctypes.byref(SUB_PROCESSOR), ctypes.byref(SETTING), ctypes.byref(ac))   # DC: PowerReadDCValueIndex
```

The GUIDs come from `SDK\um\winnt.h`: `GUID_PROCESSOR_SETTINGS_SUBGROUP 54533251-82be-4824-96c1-47b60b740d00`,
`GUID_PROCESSOR_THROTTLE_MAXIMUM bc5038f7-23e0-4960-96da-33abaf5935ec`, `…_MINIMUM
893dee8e-2bef-41e0-89c6-b55d0929964c`, `GUID_PROCESSOR_PERF_BOOST_MODE be337238-0d82-4146-a960-4f3749d470c7`,
and Balanced `GUID_TYPICAL_POWER_SAVINGS 381b4222-f694-41f0-9685-ff5bb260df2e` [verified; each `DEFINE_GUID`
re-read in the fact-check]. `PowerGetActiveScheme`'s page says to free the returned GUID with `LocalFree`
[verified: https://learn.microsoft.com/en-us/windows/win32/api/powersetting/nf-powersetting-powergetactivescheme].
Output [verified: `<scratch>\power.py`; `<verify>\v_power.py` printed the same four lines; `powercfg /list` re-run, same
three plans]:

```
PowerGetActiveScheme rc=0: 64a64f24-65b9-4b56-befd-5ec1eaced9b3  name='Silent'
  max processor state %    AC=100 DC=100
  min processor state %    AC=5 DC=5
  perf boost mode          AC=2 DC=2
```

`powercfg /list` shows three plans: Balanced `381b4222-…`, Silent `64a64f24-…` (active) and
"ASUS Recommended" (its GUID left out: probably unique to this PC). Silent is an ASUS plan. Switching the
Windows plan may not change the ASUS fan and power mode that Armoury Crate sets through the firmware;
both may need to change together [unverified]. Silent is a likely cause of the GPU thermal slowdown
seen in §4 [inferred].

### 7.2 The power-mode slider (Best power efficiency / Balanced / Best performance)

- **Getters, undocumented but exported.** `powrprof.dll` exports `PowerGetActualOverlayScheme`,
  `PowerGetEffectiveOverlayScheme`, `PowerGetOverlaySchemes`, `PowerSetActiveOverlayScheme`,
  `PowerGetUserConfiguredACPowerMode`, `PowerSetUserConfiguredACPowerMode` and the DC versions
  [verified: parsed the export table of `C:\Windows\System32\powrprof.dll`]. They are not in the
  10.0.19041 SDK headers. The community signatures are `DWORD PowerGet{Actual,Effective}OverlayScheme(GUID*
  out)` and `DWORD PowerSetActiveOverlayScheme(GUID in, by value)` [unverified: community P/Invoke at
  https://learn.microsoft.com/en-us/answers/questions/1194268/edit-a-posh-script-to-adjust-the-power-mode-slider].
  The getters ran here: `PowerGetActualOverlayScheme rc=0: 00000000-0000-0000-0000-000000000000`, and
  the Effective one returned the same [verified; re-run in `<verify>\v_power.py`, exports found with
  `GetProcAddress`]. **The slider is stored per power source:** with the same guessed signature
  (`DWORD f(GUID* out)`), `PowerGetUserConfiguredACPowerMode` returned GUID_NULL and
  `PowerGetUserConfiguredDCPowerMode` returned `961cc777-2547-4f9d-8174-7d86181b8a7a` (Better battery)
  [verified: `<verify>\v_power_user.py`, rc 0 for both; the signature is a community guess]. So on battery this
  laptop would presumably run at "Best power efficiency" [inferred]. A fix must save and restore the AC and DC values
  separately. The registry values
  `HKLM\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\ActiveOverlayAcPowerScheme` / `…Dc…`
  do not exist here [verified: winreg], so the registry gives no second readout.
- **Overlay GUIDs.** Better battery (Windows 11 calls it "Best power efficiency")
  `961cc777-2547-4f9d-8174-7d86181b8a7a`. Best performance `ded574b5-45a0-4f42-8737-46345c09c238`.
  Better performance, which is the default: `3af9B8d9-7c97-431d-ad78-34a8bfea439f` (provisioning) or
  `{381B4222-F694-41F0-9685-FF5BB260DF2E}` (INF table) [verified:
  https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/customize-power-slider]. The
  getters return GUID_NULL in Balanced; this matches the effective mode `Balanced` in §7.3 [verified]. So
  "set Balanced" probably means GUID_NULL [unverified]. The community script on the Q&A page cited above maps
  `"better_performance" = [guid] "00000000000000000000000000000000"`, which agrees, but it is still community
  code [verified: page text]. The provisioning table adds: *"If no default is configured, Better Performance will be
  the default slider mode for both AC and DC"* [verified: customize-power-slider page]. The same page says *"The slider will appear on a
  device only when the Balanced power plan, or any plan that is derived from Balanced, is selected"*
  [verified]. Whether ASUS "Silent" is derived from Balanced is unknown [unverified].
- **Fix.** Save the current overlay GUID, call `PowerSetActiveOverlayScheme(ded574b5-…)`, check the
  result with `PowerGetEffectiveOverlayScheme` and the effective-mode callback, and on restore set the
  saved GUID [unverified: never called].

### 7.3 Effective power mode, including "Game Mode is engaged right now" (documented)

```python
CB = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
@CB
def on_mode(mode, ctx): ...                # 0 BatterySaver 1 BetterBattery 2 Balanced 3 HighPerformance 4 MaxPerformance 5 GameMode 6 MixedReality
h = ctypes.c_void_p()
pp.PowerRegisterForEffectivePowerModeNotifications(2, on_mode, None, ctypes.byref(h))   # EFFECTIVE_POWER_MODE_V2 = 2
...
pp.PowerUnregisterFromEffectivePowerModeNotifications(h)
```
```
PowerRegisterForEffectivePowerModeNotifications(V2) rc=0x0; callbacks: Balanced (+1 ms)
```

The enum values come from `SDK\um\powersetting.h` (10.0.19041): BatterySaver 0 … GameMode 5,
MixedReality 6 [verified]. The callback *"will also be called once upon registration to supply the
current mode"*, and V2 *"tracks the performance power slider, battery saver, game mode and windows mixed
reality power states"* [verified:
https://learn.microsoft.com/en-us/windows/win32/api/powersetting/nf-powersetting-powerregisterforeffectivepowermodenotifications].
Keep the callback object alive for as long as you are registered. The current docs add aliases
`EnergySaverHighSavings` and `EnergySaverStandard`, described as *"an alias of"* BatterySaver and
BetterBattery and marked as prerelease, and the page shows no numeric values. **Careful:** the page lists the enum with the
aliases interleaved (`BatterySaver, EnergySaverHighSavings, BetterBattery, EnergySaverStandard, Balanced, …`). Read
literally as C, that would make Balanced 4 and GameMode 7 [verified: page text,
https://learn.microsoft.com/en-us/windows/win32/api/powersetting/ne-powersetting-effective_power_mode]. Hard-code
the header values instead. Microsoft's generated bindings in windows-rs 0.62.0 (tag sha `50e85cbb`) still have
BatterySaver 0, BetterBattery 1, Balanced 2, HighPerformance 3, MaxPerformance 4, GameMode 5, MixedReality 6, and
no EnergySaver constants [verified:
https://raw.githubusercontent.com/microsoft/windows-rs/0.62.0/crates/libs/windows/src/Windows/Win32/System/Power/mod.rs,
lines 1065–1071]. The observed value 2 = Balanced also fits: the PC was on AC with the AC slider at GUID_NULL, not
Better battery (§7.2). The fact-check re-run again got one callback, `Balanced`, 0.5 ms after registering
[verified: `<verify>\v_power.py`].
**This is the documented way to check whether Game Mode is active for the running game:** with V2, mode
5 while the game runs. That could not be observed here, because no game had focus [unverified for
lag-doctor's real games].

### 7.4 AC power, battery, battery saver

`GetSystemPowerStatus` → `SYSTEM_POWER_STATUS {BYTE ACLineStatus, BatteryFlag, BatteryLifePercent,
SystemStatusFlag; DWORD BatteryLifeTime, BatteryFullLifeTime}`. The meanings
[verified: https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-system_power_status]:

- `ACLineStatus`: 0 = offline, 1 = online, 255 = unknown.
- `BatteryFlag` (flag values, can combine): 1 = high (more than 66 %), 2 = low (less than 33 %), 4 = critical
  (less than 5 %), 8 = charging, 128 = no system battery, **255 = unknown** (the page's own wording). 0 means not charging and
  between low and high.
- `SystemStatusFlag`: 1 = *"Battery saver on"*.
- `BatteryLifeTime` and `BatteryFullLifeTime`: −1 (0xFFFFFFFF) when unknown or on AC.

Here: `ACLineStatus=1 BatteryFlag=0x01 BatteryLifePercent=100 SystemStatusFlag=0 BatteryLifeTime=4294967295`
[verified: run; re-run in `<verify>\v_power.py`, same values, `sizeof` 12]. 0.002–0.02 ms per call.

### 7.5 Which of these need admin to change?

| Setting | Change with | Admin? | Evidence |
|---|---|---|---|
| Active power plan | `PowerSetActiveScheme(NULL, &guid)` | **No** | `powercfg /getsecuritydescriptor ActionSetActive` → `O:BAG:SYD:P(A;CI;KRKW;;;BU)(A;CI;KA;;;BA)(A;CI;KA;;;SY)(A;CI;KA;;;CO)(A;CI;KR;;;AC)(A;CI;KR;;;S-1-15-3-1024-…)`: BU (Builtin Users) has read+write [verified: run]. The storage key `HKLM\…\Power\User\PowerSchemes` is read-only even for Administrators (only SYSTEM can write), so changes go through the power service, not a registry write [verified: `Get-Acl`] |
| Power-mode overlay | `PowerSetActiveOverlayScheme` | Probably no, since Settings lets standard users change it | [unverified] |
| Processor min/max state inside a plan | `PowerWriteACValueIndex` + `PowerSetActiveScheme` | Same security descriptor as above for that plan (it was also `…BU KRKW…` for Silent) [verified: `powercfg /getsecuritydescriptor 64a64f24-…`] | writing a value was not tested |
| Battery saver | Settings UI | — | no API looked at |

---

## 8. Gaming-related settings

Read with `winreg` / `Get-ItemProperty`, unelevated: 3 registry reads cost 0.12 ms [verified:
`<scratch>\static_cost.py`]. "Here" is this machine's value.

| Setting | Where | Values | Here | To change |
|---|---|---|---|---|
| Game Mode | `HKCU\Software\Microsoft\GameBar\AutoGameModeEnabled` (DWORD) | 1 or absent = on, 0 = off [unverified: community sources only; no Microsoft doc found] | **absent** (so on by default [unverified]). The key holds only `GamepadDoublePressIntervalMs = 1` | HKCU, no admin. When it takes effect is [unverified]. Check that it engaged with the effective-mode callback (§7.3, mode 5) |
| Game DVR master switch | `HKCU\System\GameConfigStore\GameDVR_Enabled` (DWORD) | 1 = on [unverified meaning] | **1** (also `GameDVR_FSEBehaviorMode=0`, `GameDVR_HonorUserFSEBehaviorMode=0`, `GameDVR_DXGIHonorFSEWindowsCompatible=0`, `GameDVR_EFSEFeatureFlags=0`) | HKCU, no admin [inferred] |
| Capture / "Record what happened" | `HKCU\Software\Microsoft\Windows\CurrentVersion\GameDVR\AppCaptureEnabled`, `HistoricalCaptureEnabled` (DWORD) | 1 = on [unverified] | **both absent** (the key has only `KGLRevision`, `KGLToGCSUpdatedRevision`, `LastGameActivity`) | HKCU [unverified]. Better: detect the cost directly, as `VideoEncode` engine use in the GPU Engine counters |
| Game DVR policy | `HKLM\SOFTWARE\Policies\Microsoft\Windows\GameDVR\AllowGameDVR` | 0 = not allowed, 1 (default) = allowed; *"If you disable this setting, Windows Game Recording won't be allowed"* [verified: https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-applicationmanagement] | policy key absent; `HKLM\SOFTWARE\Microsoft\PolicyManager\default\ApplicationManagement\AllowGameDVR\value = 1` | needs admin (HKLM policy). lag-doctor should not set policies |
| Hardware-accelerated GPU scheduling | `HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\HwSchMode` (DWORD) | 2 = on, 1 = off [unverified: no Microsoft doc for the value; it matches the readout] | **2**, and D3DKMT says `HwSchEnabled=1` on the NVIDIA GPU (§5.3) | HKLM write = admin; takes effect after a reboot [inferred]. Read it with D3DKMT, which is the effective state; advise, don't change |
| Optimizations for windowed games, VRR | `HKCU\Software\Microsoft\DirectX\UserGpuPreferences\DirectXUserGlobalSettings` (REG_SZ, e.g. `SwapEffectUpgradeEnable=1;VRROptimizeEnable=0;`) | [unverified: no Microsoft doc for the string format]. The feature, per Microsoft: DirectX 10 and 11 games in windowed and borderless windowed modes are moved from blt-model to flip-model presentation, which cuts frame latency and enables Auto HDR and VRR. It can be turned off per app, but Auto HDR must be turned off first [verified: fetched https://support.microsoft.com/en-us/topic/3f006843-2c7e-4ed0-9a5e-f9389e535952 directly during the fact-check] | **absent** (defaults apply; the default itself is [unverified]) | HKCU, no admin. After turning it on, Microsoft says *"Restart your game"* [verified: same page] |
| Per-app GPU preference | `HKCU\Software\Microsoft\DirectX\UserGpuPreferences\<full exe path>` = `GpuPreference=N;` | N follows `DXGI_GPU_PREFERENCE`: 0 unspecified, 1 minimum power (iGPU), 2 high performance (dGPU) [verified: https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_6/ne-dxgi1_6-dxgi_gpu_preference for the enum; the registry↔enum link is [unverified]: community sources] | 7 values: three ASUS utilities at `GpuPreference=1;`, three games with `AppStatus=N;` (N = 4, 4, 0; a different value format, meaning [unverified]), and `GraphicsFeaturesNotificationConfig = 1`. One value is `GpuPreference=1; ` with a trailing space, so parse by splitting on `;` and stripping each `key=value` [verified: `Get-ItemProperty`, fact-check re-read] | HKCU, no admin; takes effect when the app next starts [inferred] |
| Memory Integrity (HVCI) / VBS | `Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard` | `VirtualizationBasedSecurityStatus` 2 = *"enabled and running"*; `SecurityServicesRunning` contains 2 = *"memory integrity is running"* [verified: https://learn.microsoft.com/en-us/windows/security/hardware-security/enable-virtualization-based-protection-of-code-integrity] | **VBS 2, services running {2}, `HKLM\…\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity\Enabled = 1`** | Readable without admin: Microsoft's page says it *"can be queried from an elevated Windows PowerShell session"*, but it worked unelevated here [verified; re-run: `VBS=2 running=2 configured=2 available=1,2,3,5,6,7,8`]. 690–980 ms through a fresh PowerShell process (812 ms in the first run), so read it once. Changing it needs admin and a restart, and has a security cost; point the user to Windows Security → Core isolation. The researcher said Microsoft recommends turning it off for gaming in a support article. Fetching that URL during the fact-check returned only a generic "Microsoft Support" page with no article text, so the claim stays [unverified: https://support.microsoft.com/en-us/topic/a255f612-2949-4373-a566-ff6f3f474613] |

Every "Here" value above was re-read unelevated in the fact-check and matched [verified: `Get-ItemProperty` on each
key]. `HKCU\System\GameConfigStore` also holds two binary values the table doesn't decode,
`Win32_AutoGameModeDefaultProfile` and `Win32_GameModeRelatedProcesses`, plus subkeys `Children` and `Parents`
[verified: same read].

**Does Windows honour the GPU preference for OpenGL and Vulkan on an NVIDIA Optimus laptop?**

- **DirectX: yes.** NVIDIA: *"Beginning with Windows 10 20H1, the Windows OS assigns the graphics
  processor to use for various applications"* and *"overrides any settings made in the NVIDIA Control
  Panel > Manage 3D Settings > Preferred graphics processor setting"* [verified:
  https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-us/mergedProjects/nv3d/Setting_the_preferred_graphics_processor.htm].
  That page does not say which graphics APIs this covers.
- **Vulkan: partly.** The Khronos loader *"will attempt to sort these objects so that the system
  preference will be listed first. This mechanism does not force an application to use any particular
  GPU"*. Only drivers with loader interface v6 or later are sorted [verified:
  https://raw.githubusercontent.com/KhronosGroup/Vulkan-Loader/vulkan-sdk-1.4.363.0/docs/LoaderDriverInterface.md
  §Physical Device Sorting]. A game that picks the discrete GPU itself ignores the preference.
- **OpenGL: unknown.** Community reports say the preference is ignored [unverified].
- **lag-doctor must check, not assume.** After the game starts, see which LUID its `pid_…` 3D engine is
  busy on (§2.2). That shows which GPU it really renders on, without a handle.

---

## 9. Process priority, exe path, drive type

### 9.1 What `SetPriorityClass` needs

- The handle must have **`PROCESS_SET_INFORMATION` (0x0200)** [verified:
  https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setpriorityclass,
  https://learn.microsoft.com/en-us/windows/win32/procthread/process-security-and-access-rights].
  That means opening a handle to the game, which is out of scope for lag-doctor.
- Constants: NORMAL 0x20, IDLE 0x40, HIGH 0x80, REALTIME 0x100, BELOW_NORMAL 0x4000, ABOVE_NORMAL 0x8000 [verified: same page,
  and `SDK\um\WinBase.h` lines 625–637].
- **HIGH does not need admin; REALTIME is quietly downgraded to HIGH.** Tested on our own process
  only [verified: `<scratch>\proc_disk.py`]:

```
SetPriorityClass(self, ABOVE_NORMAL) -> ok=True lasterr=0 ; GetPriorityClass -> ABOVE_NORMAL
SetPriorityClass(self, HIGH) -> ok=True lasterr=0 ; GetPriorityClass -> HIGH
SetPriorityClass(self, REALTIME) -> ok=True lasterr=0 ; GetPriorityClass -> HIGH
restored -> NORMAL
```

  The REALTIME call reports success but the class becomes HIGH. A fix must read the class back with
  `GetPriorityClass`; a success return proves nothing. The fact-check re-run (`<verify>\v_prio.py`, own process
  only) gave the same four lines. The SetPriorityClass page does not mention this downgrade, so it is observed
  behaviour, not documented [verified: page text searched].

### 9.2 When the game is protected by anti-cheat

- **Protected processes (PPL).** `PROCESS_SET_INFORMATION` is in Microsoft's list of rights *"not
  allowed from a process to a protected process"*, so the open fails with access denied (5) [verified:
  process-security-and-access-rights, "Protected Processes"]. Tested against `MsMpEng.exe` from this unelevated
  shell: `OpenProcess` failed with 5 for `PROCESS_SET_INFORMATION`, and also for
  `PROCESS_QUERY_LIMITED_INFORMATION`, `PROCESS_QUERY_INFORMATION` and `PROCESS_SET_LIMITED_INFORMATION`
  [verified: `<verify>\v_prio.py`; no handle was returned, nothing was set]. `PROCESS_QUERY_LIMITED_INFORMATION`
  is not on Microsoft's list, so here something else refuses first, either the DACL of a SYSTEM service or
  Defender's self-protection [inferred]. For lag-doctor it means: unelevated, it cannot open MsMpEng at all, and
  should expect the same from other SYSTEM services (only MsMpEng was tried) [inferred].
- **Kernel anti-cheat using `ObRegisterCallbacks`.** The callback *"can modify this value to restrict
  the access that is granted"*, and Microsoft's list of rights a callback can remove includes
  `PROCESS_SET_INFORMATION — Modify process settings, such as by calling the user-mode SetPriorityClass
  routine` [verified:
  https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ns-wdm-_ob_pre_create_handle_information].
  The likely result: `OpenProcess` succeeds, then `SetPriorityClass` fails with `ERROR_ACCESS_DENIED`
  [inferred]. I know of no documented case where the change is silently ignored. Some anti-cheats may
  also log or flag the handle opening itself [unverified]. Either way, opening the handle breaks
  lag-doctor's rule. **Recommendation:** don't raise the game's priority. Offer to lower the priority
  of same-user background hogs instead (needs handles to those processes, not the game), and refuse for
  services and PPL processes such as Defender [inferred].

### 9.3 Risks

- *"Use HIGH_PRIORITY_CLASS with care. If a thread runs at the highest priority level for extended
  periods, other threads in the system will not get processor time."*
- *"You should almost never use REALTIME_PRIORITY_CLASS, because this interrupts system threads that
  manage mouse input, keyboard input, and background disk flushing."*

[verified: https://learn.microsoft.com/en-us/windows/win32/procthread/scheduling-priorities]. Normal
class with thread priority normal gives base priority 8; High class gives 13 (15 at `THREAD_PRIORITY_HIGHEST`
and `TIME_CRITICAL`), and REALTIME class gives 16–31 [verified: same page, table]. Threads registered with MMCSS
run above all of that: category High *"23-26"* (designed for Pro Audio), Medium *"16-22"* (the foreground
application), Low 8–15 [verified:
https://learn.microsoft.com/en-us/windows/win32/procthread/multimedia-class-scheduler-service, "Thread
Priorities"]. So a HIGH-class thread (13–15) does not preempt an MMCSS audio thread while that thread is in its 16–26
range. REALTIME (24 at normal thread priority) can. The same page says threads that *"have used their quota of CPU
resource"* drop to 1–7, and there HIGH class can starve them [verified: same page; the consequence is
inferred]. Whether a given game's or the OS's audio path registers with MMCSS was not checked [unverified].

### 9.4 The game's exe path and drive, without a handle

`NtQuerySystemInformation(SystemProcessIdInformation = 88, &SYSTEM_PROCESS_ID_INFORMATION{HANDLE ProcessId;
UNICODE_STRING ImageName}, 24, NULL)`. Set `ImageName.MaximumLength` and `Buffer` to your own buffer
first. On `STATUS_INFO_LENGTH_MISMATCH`, `MaximumLength` holds the size it needs [verified: class number
by counting phnt's `SYSTEM_INFORMATION_CLASS`, which lands on 88, consistent with its `// 90` marker two
entries later; struct from phnt `ntexapi.h`; the size-retry behaviour from psutil's `proc.c`; run below].
It returns an NT path. Turn it into a drive-letter path with `QueryDosDeviceW("C:")` for each letter
from `GetLogicalDriveStringsW`.

```
QueryDosDeviceW: {'\\Device\\HarddiskVolume3': 'C:', '\\Device\\HarddiskVolume7': 'E:'}
pid 37072: '\\Device\\HarddiskVolume3\\Python313\\python.exe' -> 'C:\\Python313\\python.exe'
pid 4: '' -> ''
pid 23740: '\\Device\\HarddiskVolume3\\ProgramData\\Microsoft\\Windows Defender\\Platform\\4.18.26100.4-0\\MsMpEng.exe' -> 'C:\\…\\MsMpEng.exe'
pid 1320: '\\Device\\HarddiskVolume3\\Windows\\System32\\csrss.exe' -> 'C:\\Windows\\System32\\csrss.exe'
```

It works even for protected processes such as Defender's `MsMpEng.exe` and `csrss.exe` [verified]. The fact-check
re-run (`<verify>\v_ntqsi.py`) also read `lsass.exe` and the training job's path. The first call with an 8-WCHAR
buffer returned `0xC0000004` with `MaximumLength` set to the bytes needed, including the terminator (e.g. 90 for
a 44-character path). For pid 4 the first call succeeds with an empty name. `sizeof(SYSTEM_PROCESS_ID_INFORMATION)`
= 24 [verified]. Remember that `OpenProcess` on MsMpEng fails even for limited query (§9.2), so class 88 is
the only way to get that path unelevated.
Note that the venv's `python.exe` started a separate process for the real interpreter (pid 37072 is
`C:\Python313\python.exe`).

**psutil trap.** psutil 7.2.2 `Process.exe()` first calls `psutil_pid_is_running`, which does
`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid)`. `Process.name()` is `basename(exe())`, and
`cpu_times()` uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetProcessTimes` [verified:
https://raw.githubusercontent.com/giampaolo/psutil/release-7.2.2/psutil/arch/windows/proc.c,
`…/proc_utils.c`, `…/psutil/_pswindows.py`]. **So every one of these opens a handle to the game.**
**Correction: so does merely constructing `psutil.Process(game_pid)`.** `Process._init` calls `_get_ident()`,
which on Windows calls `self._proc.create_time(fast_only=True)`. That is `cext.proc_times(pid)`, which does
`psutil_handle_from_pid(pid, PROCESS_QUERY_LIMITED_INFORMATION)` → `OpenProcess` [verified: same tag,
`psutil/__init__.py` `_init` and `_get_ident`, `_pswindows.py` `create_time`, `arch/windows/proc.c`
`psutil_proc_times`]. `psutil.process_iter()` builds a `Process(pid)` for every pid, so it opens a handle to every
process. `psutil.pid_exists()` is `cext.pid_exists` → `psutil_pid_is_running` → `OpenProcess` as well [verified:
same files]. Only `psutil.pids()` (`EnumProcesses`, `arch/windows/pids.c`) avoids the game.

### 9.5 SSD or HDD, without admin

Open `\\.\C:` with `CreateFileW(r"\\.\C:", 0 /*no access*/, FILE_SHARE_READ|FILE_SHARE_WRITE, NULL,
OPEN_EXISTING, 0, NULL)`. Then use these calls:

- `IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400` with `STORAGE_PROPERTY_QUERY {StorageDeviceSeekPenaltyProperty = 7, PropertyStandardQuery = 0}`.
  This returns `DEVICE_SEEK_PENALTY_DESCRIPTOR {DWORD Version, Size; BOOLEAN IncursSeekPenalty}`.
- The same IOCTL with `StorageDeviceProperty = 0` returns `STORAGE_DEVICE_DESCRIPTOR`, with `BusType` at
  +28 and `ProductIdOffset` at +16. Bus type values: 17 = NVMe, 11 = SATA, 7 = USB.
- `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000` returns the physical disk number(s): `DISK_EXTENT`
  is 24 bytes, with `DiskNumber` at +0 after an 8-byte header.

[verified: `SDK\um\winioctl.h` (`CTL_CODE` arithmetic), run below]

```
C: seekPenaltyOK=True (err 0) IncursSeekPenalty=0 -> SSD | bus=Nvme model='CT1000T500SSD8' | physical disk(s)=[0]
E: seekPenaltyOK=True (err 0) IncursSeekPenalty=0 -> SSD | bus=Nvme model='INTEL SSDPEKNU512GZ' | physical disk(s)=[1]
```

The fact-check re-run (`<verify>\v_disk.py`) printed the same two lines: `DEVICE_SEEK_PENALTY_DESCRIPTOR` came back with
Version = Size = 12, and each drive took 0.1–0.3 ms. `ProductIdOffset` at +16 and `BusType` at +28 were re-read
from the struct in `SDK\um\winioctl.h`, and `StorageDeviceSeekPenaltyProperty = 7` was counted in its enum [verified].
A second readout agrees: `Get-PhysicalDisk` (MSFT_PhysicalDisk) also works unelevated, and
`Get-Partition -DriveLetter C,E` maps the drives to those disks [verified]:

```
DeviceId FriendlyName             MediaType BusType SpindleSpeed          Size
0        NVMe CT1000T500SSD8      SSD       NVMe               0 1000204886016
1        NVMe INTEL SSDPEKNU512GZ SSD       NVMe               0  512110190592
DriveLetter DiskNumber:  C 0 | E 1
```

The disk number also links the drive to its PDH `\PhysicalDisk("0 C:")` instance [inferred: the PDH
instance name starts with the disk number].

---

## 10. Known background hogs, and how to recognise them

The detection method uses no handles:

1. Take two `SystemProcessInformation` snapshots to get CPU% and I/O bytes/s per process.
2. Map services to PIDs with `EnumServicesStatusExW(SC_ENUM_PROCESS_INFO=0, SERVICE_WIN32=0x30,
   SERVICE_ACTIVE=0x1)`. The entries are 56-byte `ENUM_SERVICE_STATUS_PROCESSW`, with `dwProcessId` at
   +44. It costs 2.8 ms for 157 services. psutil's `win_service_iter()` plus `as_dict()` takes 1,370 ms.
   Fact-check: 154 services in 1.86 ms. The +44 offset was cross-checked against `psutil.win_service_get(n).pid()`
   for WinDefend (23740) and WSearch (13992). Open the SCM with `OpenSCManagerW(NULL, NULL,
   SC_MANAGER_ENUMERATE_SERVICE = 0x0004)` [verified: `<verify>\v_net_svc.py`; `SDK\um\winsvc.h`].
3. Use the GPU Engine counters to see which processes use `VideoDecode` / `VideoEncode` / `3D`.

[verified: `<scratch>\hogs.py`, `<scratch>\net_svc_probe.py`; constants from `SDK\um\winsvc.h`, `SDK\um\winnt.h`]

What was running here (2 s window; 100 % = one logical CPU; the machine's total is 1600 %) [verified: run]:

```
  python.exe                         pid    656 cpu   97.7%   (the stand-in: the CUDA training job)
  dwm.exe                            pid  28732 cpu   60.0%
  MsMpEng.exe                        pid  23740 cpu   26.2%  io 0.10 MB/s  Defender engine  [WinDefend]
  NVDisplay.Container.exe            pid   3780 cpu   11.5%  io 1.77 MB/s  [NVDisplay.ContainerLocalSystem]
  esrv_svc.exe                       pid  11112 cpu    6.2%  io 0.18 MB/s  [ESRV_SVC_QUEENCREEK]
Known background apps present now: MsMpEng, ArmouryCrate.UserSessionHelper (3.1%), ArmouryCrate.Service, nvcontainer x3,
  SearchIndexer, asus_framework x4, MpDefenderCoreService, NisSrv, OneDrive, SearchProtocolHost, SearchFilterHost x3
Watched services running: {'BITS': 9964, 'DiagTrack': 5760, 'DoSvc': 32020, 'SysMain': 4004, 'UsoSvc': 21388, 'WinDefend': 23740, 'WSearch': 13992, 'wuauserv': 15852}
```

| Hog | Recognise by | Notes |
|---|---|---|
| Defender scans | `MsMpEng.exe` (service `WinDefend`), `MpDefenderCoreService.exe`, `NisSrv.exe`, `MpCmdRun.exe` | It was at 26 % of a core here, probably scanning the files these tests wrote [inferred]. Microsoft's own diagnosis tool (`New-MpPerformanceRecording`) is run *"in administrator mode"* [verified: https://learn.microsoft.com/en-us/defender-endpoint/tune-performance-defender-antivirus]. It is a protected process, so its priority cannot be changed; unelevated, `OpenProcess` on it fails with 5 even for limited query (§9.2). Advise only |
| Windows Update | services `wuauserv`, `UsoSvc`, `BITS` (all in `svchost.exe`); `MoUsoCoreWorker.exe`, `TiWorker.exe` / `TrustedInstaller.exe` (installing updates) | Disk and CPU while installing [inferred] |
| Delivery Optimization | service `DoSvc` (svchost) | Downloads Windows Update, Store apps, Defender definitions, Edge, Teams and *"Xbox Game Pass (PC)"* content, so it competes for bandwidth [verified: https://learn.microsoft.com/en-us/windows/deployment/do/waas-delivery-optimization] |
| Search indexer | `SearchIndexer.exe`, `SearchProtocolHost.exe`, `SearchFilterHost.exe` (service `WSearch`) | idle here |
| OneDrive | `OneDrive.exe` | network + disk while syncing [inferred] |
| Store launchers | `steam.exe`, `steamwebhelper.exe`, `steamservice.exe`; `EpicGamesLauncher.exe`, `EpicWebHelper.exe`; `Battle.net.exe`, `Agent.exe`; `EADesktop.exe`; `upc.exe` | Downloads show up as I/O and NIC bytes. Steam's shader pre-caching process (reportedly `fossilize_replay.exe`) can use a lot of CPU [unverified] |
| Browsers decoding video | `chrome.exe` / `msedge.exe` / `firefox.exe` with non-zero `engtype_VideoDecode` or `3D` in GPU Engine | detect it by engine, not by name [inferred] |
| Capture / streaming | `obs64.exe`, `Discord.exe`, `NVIDIA Overlay.exe` / `nvcontainer.exe`, `GameBar.exe`, `broadcastdvrserver.exe`, `Medal.exe` | look for `VideoEncode` engine use [inferred] |
| OEM / RGB utilities | `ArmouryCrate.Service.exe`, `ArmouryCrate.UserSessionHelper.exe`, `LightingService.exe` (ASUS Aura), `asus_framework.exe`, `iCUE.exe`, `RazerCentralService.exe`, `lghub*.exe`, `SignalRgb.exe` | Reports that RGB utilities cause DPC spikes are anecdotal [unverified]. Measure instead: per-core `% DPC Time` / `% Interrupt Time` (core 0 showed 24.5 % DPC in one sample here). Sustained high values there point to a driver, but PDH cannot say which driver; that needs ETW and admin [inferred] |
| Intel telemetry | `esrv_svc.exe` (service `ESRV_SVC_QUEENCREEK`) | 6 % of a core here [verified]. An hour later the service was *Stopped* (start type Automatic) and an `esrv.exe` process was running instead [verified: `Get-Service`, `Get-Process`], so match on both names and expect these to come and go |

---

## 11. Recommended sampler design

A prototype of this design ran here for 40 polls at 4 Hz with the stand-in (the CUDA training job) loading the
NVIDIA GPU [verified: `<scratch>\sampler_bench.py`]; an independent re-implementation ran again in the fact-check
(§11.4).

### 11.1 Every 250 ms (one sampler thread, one PDH query)

1. **One `PdhCollectQueryData`** on a query holding:
   - Processor Information: `\Processor Information(*)\% Processor Utility` and `\% Processor Performance`
     (per logical CPU, for the P/E split and to spot a saturated core), `(_Total)\% Performance Limit`,
     `(_Total)\Performance Limit Flags`, `(*)\% DPC Time` and `(*)\% Interrupt Time`. **Correction:** the
     researcher's design used per-CPU `% Processor Time` here. At 250 ms it failed whole calls and individual
     items, and read 0–73 % for a steady load (§1.3), so it is replaced. For "is this core busy" use
     `(*)\% User Time` + `(*)\% Privileged Time`, which were valid in every poll. They are tick-quantised (about 6 %
     steps per CPU at 250 ms), so judge saturation over the 1 s window [inferred from the measured statuses].
   - Memory: `\Memory\Available MBytes`, `Committed Bytes`, `Commit Limit`, `Pages Input/sec`.
   - PhysicalDisk(*): `% Idle Time`, `Avg. Disk sec/Read`, `Avg. Disk sec/Write`, `Disk Read Bytes/sec`, `Disk Write Bytes/sec`.
   - GPU: `\GPU Engine(*)\Utilization Percentage`, `\GPU Adapter Memory(*)\Dedicated Usage` and `Shared
     Usage`, `\GPU Process Memory(*)\Dedicated Usage` (filter to the game's pid).
   - Thermal: `\Thermal Zone Information(*)\Temperature` and `% Passive Limit`.

   Then read the arrays and work out game GPU% and overall GPU% as in §2.3: keep the per-sample values
   unclamped, average them over the 1 s window, and clamp only the average.
2. **One `NtQuerySystemInformation(5)`** into a reused buffer, with the lean parser: CPU for every
   process plus the game's threads. Key processes by (pid, CreateTime) and threads by (tid, CreateTime).
3. **NVML**: util, temperature, power, graphics clock, pstate, clock-event reasons, and field 269 (SW thermal
   slowdown time, §4.4) so a throttle can be reported as a share of the window. Do this only while the game's GPU
   Engine instances are on the NVIDIA LUID. Leave `nvmlDeviceGetDisplayActive` (≈170 µs) out of the 4 Hz loop.
4. **`GetSystemPowerStatus`.** `GetIfEntry2(active NIC index)` costs 0.08–0.11 ms (0.30 ms median inside the
   fact-check sampler), so it can run at 4 Hz too. `MIB_IF_ROW2` is 1,352 bytes; set `InterfaceIndex` (+8) and
   zero `InterfaceLuid` (+0) before the call; `InOctets` is at +1208 and `OutOctets` at +1280. The values matched
   psutil's per-NIC bytes [verified: `<verify>\v_net_svc.py`, offsets computed by ctypes from the
   `SDK\shared\netioapi.h` struct].

### 11.2 Slower tiers

- **Each second:** per-thread and per-process CPU% over the last 4 snapshots (a 1 s window, because of
  the ±6.25 % quantisation at 250 ms); game and overall GPU% from the 4 unclamped GPU Engine samples; per-CPU
  busy from `% User Time` + `% Privileged Time`; the NVML throttle share (field 269 delta / time delta); the ACPI
  `Throttle Reasons`.
- **Every 10–30 s, in the background:**
  - `EnumServicesStatusExW` (1.9–2.8 ms) to refresh service→PID.
  - `GetIfTable2` (3.9–6.1 ms for 69 rows) to re-pick the active NIC: up (`OperStatus == 1`), `HardwareInterface`
    flag (bit 0 of the flags byte), most bytes. Free the table with `FreeMibTable`. In the fact-check this picked index 8,
    `Intel(R) Wi-Fi 6 AX201 160MHz` [verified: `<verify>\v_net_svc.py`].
  - Rebuild the DXGI/DXCore LUID map if an unknown LUID appears.
  - `nvmlDeviceGetDisplayActive` and the per-power-source slider values (§7.2), if wanted.
- **Startup and on change:**
  - Displays (§6, 21–23 ms; re-read on `WM_DISPLAYCHANGE`).
  - Power plan, overlay and effective mode (callback, §7.3).
  - The registry settings in §8 (0.12 ms).
  - `Win32_DeviceGuard` once (0.7–1.0 s through PowerShell).
  - Drive type of the game's exe (§9.5).
  - CPU-set map (§3.7).

### 11.3 Never at 4 Hz

| Read | Cost |
|---|---|
| PDH `\Thread(*)` | ≈470–490 ms per collect (fact-check) |
| PDH `\Processor Information(*)\% Processor Time`, `% Idle Time`; `\Processor(*)\% Processor Time` | cheap, but they fail or read garbage at 250 ms (§1.3) |
| PDH `\Process(*)` | 9.0 ms per collect |
| PDH `\Process V2(*)` | 4.3 ms |
| PDH `\System\Processor Queue Length` / `Context Switches/sec` | 7.0 ms |
| PDH `\Network Interface(*)` | 5.2 ms (use `GetIfEntry2`) |
| `psutil.net_io_counters(pernic=True)` | 58–68 ms median: it calls `GetAdaptersAddresses` each time, then `GetIfEntry2` per adapter [verified: psutil `arch/windows/net.c` @release-7.2.2, `psutil_net_io_counters` → `psutil_get_nic_addresses`] |
| one-shot `nvidia-smi` | 96–106 ms |
| `psutil.win_service_iter()` + `as_dict()` | 1.4 s |
| `psutil.Process(game_pid)` (the constructor itself), any of its methods, `psutil.process_iter()`, `psutil.pid_exists()` | opens a handle to the game (§9.4) |

### 11.4 Measured cost per poll on this machine (40 polls, 4 Hz)

```
per-poll cost over 40 polls at 4 Hz (wall ms: median / max):
  pdh_collect        2.55 /   6.92
  pdh_read+parse     3.58 /   6.34
  ntqsi              6.90 /  10.16
  nvml               0.05 /   2.52
  power              0.02 /   0.04
  net_psutil        67.61 /  76.12      <- measured for comparison, NOT part of the design
  TOTAL per poll    13.89 /  19.08
sampler process CPU: 0.531 s over 10.02 s = 5.3% of one logical CPU (0.33% of the machine)
```

Independent re-implementation in the fact-check (`<verify>\v_sampler.py 656 40`). Its PDH query had the §11.1 list,
with `(*)\% Processor Time`, `(*)\% Processor Performance` and `(*)\% Processor Utility` all included. Its NVML stage
used six getters plus field 269, without `GetDisplayActive`. It also called `GetIfEntry2`:

```
per-poll cost over 40 polls at 4 Hz (wall ms: median / max):
  pdh_collect           2.14 /    5.07
  pdh_read+parse        3.08 /    5.05
  ntqsi                 6.49 /   10.78
  nvml                  0.06 /    4.58
  power                 0.02 /    0.06
  nic GetIfEntry2       0.30 /    0.99
  TOTAL                12.52 /   19.93
sampler process CPU: 0.578 s over 10.01 s = 5.8% of one logical CPU (0.36% of the machine)
```

So the researcher's cost figures stand. Swapping `% Processor Time` for `% User Time` + `% Privileged Time` adds one
counter to an object the query already collects, so the cost should not change much [inferred; not measured].

What the sampler saw, which shows the signals line up:

```
  t= 0.0s NVIDIA busiest engine 3D 112.9% vs NVML util  76% | target GPU  96.0% (3D) | target CPU 104.2% top threads ['104%', '0%'] | CPU total  2.9% | NVML 81C 48.2W 780MHz P3 reasons=0x20 | ACPI TZ 85.9C
  t= 4.0s NVIDIA busiest engine 3D  92.4% vs NVML util  78% | target GPU  82.6% (3D) | target CPU  93.6% top threads ['94%', '0%'] | CPU total 14.2% | NVML 81C 42.6W 780MHz P3 reasons=0x20 | ACPI TZ 83.9C
  t= 8.0s NVIDIA busiest engine 3D  93.7% vs NVML util  99% | target GPU  85.2% (3D) | target CPU  55.7% top threads ['56%', '0%'] | CPU total 22.4% | NVML 82C 56.9W 780MHz P3 reasons=0x20 | ACPI TZ 83.9C
```

In this run the stand-in (the CUDA training job, not a game) was limited by one CPU thread (~90–100 % of one core
while total CPU is 3–23 %) and by a GPU that the thermal limit holds at 780 MHz. Both are visible without touching
the process. An hour later the same job's CPU was split across two threads (`['49%', '47%']`, `['67%', '28%']` over
1 s windows), and NVML field 269 showed SW thermal slowdown for 100 % of every window [verified:
`<verify>\v_sampler.py`]. So "one saturated thread" was a phase of that job, not a fixed property.

### 11.5 Gotchas

1. **Warm-up and failed ticks.** Rate counters need two collects. A whole-array call can fail with
   `PDH_CSTATUS_INVALID_DATA` / `PDH_INVALID_DATA` before that. After warm-up, `% Processor Time` still fails whole
   calls with `PDH_CALC_NEGATIVE_DENOMINATOR` (2.5–10 % of polls) and individual items with
   `PDH_CSTATUS_INVALID_DATA` (5–10 %), so it is out of the design (§1.3). Treat a failed call or item as a missing
   sample. Never crash and never invent a zero.
2. **English paths, `NOCAP100`, clamping.** Use `PdhAddEnglishCounterW` and `PDH_FMT_NOCAP100`, and
   clamp only what you display, after averaging. GPU per-engine values reached 3,652 % in one 250 ms window
   (113 % in the first run), `PhysicalDisk % Idle Time` reached 100.3 %, and `Processor Information % Idle Time`
   `_Total` reached 135.5 %.
3. **Parse GPU Engine names with `split("_", 9)` and cache the results.** About 490 names per poll;
   `engtype` can be empty, `OFA_0`, or `GDI Render`.
4. **Tick quantisation.** Thread times move in 15.625 ms steps. Use 1 s windows for per-thread CPU, clamp
   threads to 100 %, and check CreateTime because TIDs/PIDs are reused.
5. **No psutil on the game.** Constructing `psutil.Process(pid)` opens a handle, and so do `name()`, `exe()`,
   `cpu_times()`, `psutil.process_iter()` and `psutil.pid_exists()` (§9.4). Use NtQSI classes 5 and 88, and
   `psutil.pids()` at most.
6. **NVML refreshes about every 500 ms here.** Half the 4 Hz samples repeat. NVML utilisation and PDH
   engine % measure different things, and "VRAM used" has three definitions (§4.4).
7. **Don't wake the dGPU just to read it.** Query NVML only while the game is on the NVIDIA LUID
   [unverified wake behaviour].
8. **Don't trust `Win32_VideoController`.** It showed a stale 2560x1440@143 on an adapter with no display
   attached (the source of that mode is unknown, §6.3) and caps AdapterRAM at 4 GB. Use DXGI, DXCore and
   QueryDisplayConfig.
9. **The ACPI thermal zone is not CPU temperature.** There is no CPU package temperature without admin
   or a driver. Report "platform thermal zone".
10. **Verify the outcome, not the call.** REALTIME priority "succeeds" as HIGH. CDS_TEST success does
    not mean the mode is applied. A GPU preference set in the registry doesn't prove where the game
    renders. Read back the effective state (GetPriorityClass, EnumDisplaySettings plus QueryDisplayConfig,
    GPU Engine LUID, the effective-power-mode callback).
11. **Bound the work.** Reuse the NtQSI buffer, grow it on `0xC0000004` to ReturnLength plus headroom,
    and decode image names only for new (pid, CreateTime) pairs.
12. **`time.process_time()` on Windows moves in 15.6 ms steps.** Measure the sampler's own cost over
    many polls, not per poll.
13. **The sampler's own load is about 5–6 % of one core** (5.3 % and 5.8 % in two independent runs). Keep it on its
    own thread and outside the GIL-heavy UI path [inferred]. If it must be cheaper, sample NtQSI threads at 2 Hz and
    keep PDH at 4 Hz [inferred].
14. **A busy 3D engine is not proof of a game.** CUDA compute (the training job) appears on `engtype_3D` here
    (§2.2). Combine the GPU Engine signal with process identity and PresentMon frames.
15. **Explicit-instance paths fail silently.** `PdhAddEnglishCounterW` accepts an instance that does not exist yet,
    and returns `ERROR_SUCCESS` (§1.3). Use `(*)` paths and pick the game's instances by name or pid.
16. **Throttling is a share, not a bit.** NVML's reason bits say "throttled now". Field 269 and the
    `clocks_event_reasons_counters.*` fields say for how much of the window, which is what a report needs (§4.4).

---

## Verification log

Independent fact-check, 2026-11-04, same machine and session, unelevated, read-only. Nothing was changed: own-process
`SetPriorityClass` was restored to NORMAL, and `ChangeDisplaySettingsExW` was only called with `CDS_TEST`. Scripts
and outputs are in `<verify>`. Headers are `SDK\…` 10.0.19041; web pages were fetched raw with curl and searched for
the exact wording. "Confirmed" means re-run or re-read and matched. "Corrected" gives old → new. "Unverifiable" means
it could not be checked here.

| # | Claim (section) | Result | Evidence |
|---|---|---|---|
| 1 | Stand-in pid 656 is a program "rendering on the NVIDIA GPU at 80–96 % 3D" (header) | **corrected** → it is a machine-learning *training* job, CUDA compute, type `C` in nvidia-smi; its work lands on `engtype_3D` | `Get-CimInstance Win32_Process` command line; `nvidia-smi` process table; `v_pdh3.py` |
| 2 | Windows 11 10.0.26200, Python 3.13.3, psutil 7.2.2, driver 591.86 = 32.0.15.9186 | confirmed | `platform.platform()`, `sys.version`, `psutil.__version__`, `nvidia-smi --version`, `Win32_VideoController.DriverVersion` |
| 3 | phnt commit `53fbbdc…` exists | confirmed (2026-03-26, "sync latest") | raw fetch 200; GitHub API |
| 4 | PDH typedefs and constants (`PDH_STATUS`=LONG, handles, `FMT_DOUBLE` 0x200, `NOCAP100` 0x8000, `MORE_DATA` 0x800007D2) | confirmed | `Pdh.h` 39, 73–74, 441, 446; `PdhMsg.h` 26 |
| 5 | Status codes `NO_OBJECT` 0xC0000BB8, `CSTATUS_INVALID_DATA` 0xC0000BBA, `INVALID_DATA` 0xC0000BC6, `CALC_NEGATIVE_DENOMINATOR` 0x800007D6 | confirmed | `PdhMsg.h` 30, 39, 41, 53 |
| 6 | `PDH_FMT_COUNTERVALUE` 16 B, `…ITEM_W` 24 B; function signatures as in the recipe | confirmed | `Pdh.h` 109–128, 323–501; ctypes sizeof |
| 7 | First collect: wildcard rate → 0xC0000BBA, explicit/no-instance → 0xC0000BC6, raw counters valid | confirmed (list of raw counters extended) | `v_pdh.py` |
| 8 | Missing set fails at add with `NO_OBJECT` | confirmed; added: missing counter → `NO_COUNTER` 0xC0000BB9; missing *instance* → `ERROR_SUCCESS` | `v_pdh.py`; PdhAddEnglishCounterW and PdhCollectQueryData pages |
| 9 | `PdhAddEnglishCounterW` is language-neutral | confirmed | PdhAddEnglishCounterW page, Remarks |
| 10 | Rate interval = time between the last two collects | confirmed (was untagged) | PdhCollectQueryData page: "raw counter values for the current and previous collection" |
| 11 | Buffer-sizing quote; names live in the caller's buffer | confirmed | PdhGetFormattedCounterArrayW page; 73,522 B for 484 items in `v_pdh.py` |
| 12 | Without `NOCAP100` values are capped at 100 | confirmed | same page |
| 13 | Values above 100 reach 112.9 %; clamp each value to 100 | **corrected** → GPU Engine reached 3,651.8 % in one 250 ms window and 1,513 % in 1 s, with 0 % windows between; average unclamped, then clamp | `v_pdh2.py`, `v_pdh3.py`, `v_pdh3_1hz.py` |
| 14 | All item statuses were VALID; `PDH_CALC_NEGATIVE_DENOMINATOR` "never appeared" | **corrected** → `% Processor Time`: 2/80 whole-call failures with 0x800007D6 and 90/1,404 items `CSTATUS_INVALID_DATA`; legacy `\Processor`: 4/40 and 58/612; `% Idle Time`: 25/720 invalid, `_Total` up to 135.5 %; at 1 Hz still 27/540 invalid | `v_pdh3.py`, `v_pdh4.py`, `v_pdh3_1hz.py` |
| 15 | Design polls per-CPU `% Processor Time` at 4 Hz (§11.1) | **corrected** → use `% Processor Utility` + `% Processor Performance`, and `% User Time` + `% Privileged Time` for busy share (0 failures in every run) | as #14 |
| 16 | `\Process(*)` has 167 duplicate names; `Process V2` uses unique `name:pid` | confirmed (300 instances, 167 duplicates, V2 0) | `v_pdh.py` |
| 17 | Wildcards pick up new processes | confirmed (`python:7388`) | `v_misc.py` |
| 18 | Collect cost per set (§1.4) | confirmed within ±15 %, except Processor Information 2.44 → 1.00–1.04 ms; added `\Thread(*)` ≈486 ms | `v_cost.py`, `v_cost2.py` |
| 19 | Every counter set and counter name used exists | confirmed (15 sets) | `Get-Counter -ListSet` |
| 20 | Counter types in §2.1 (≈35 counters) | confirmed, all | `PdhGetCounterInfoW`; `PERF_*` evaluated from `winperf.h` (`v_pdh.py`) |
| 21 | Explain-text quotes in §2.1 | confirmed verbatim | `v_explain.py`, `v_pdh.py` |
| 22 | Instance names (Processor Information `0,0…0,15`, `0,_Total`, `_Total`; `Intel[R]…` NICs; `0 C:`/`1 E:`; GPU memory `_part_0` variants) | confirmed | `Get-Counter -ListSet … PathsWithInstances` |
| 23 | GPU Non Local Adapter Memory has one instance | confirmed; name ends `_part_0`, and Get-Counter labels the set SingleInstance (as it does Thermal Zone) | same |
| 24 | GPU Engine `Running Time` values near 2^64 | refined → NVIDIA instances only; Intel values sane | `v_pdh.py` |
| 25 | GPU Engine name format, `split("_", 9)`, engine lists per LUID | confirmed | `v_misc.py` |
| 26 | Task Manager blog quotes, date 2017-07-21, "never sums engines" | confirmed | devblogs page text; `article:published_time` |
| 27 | `MSAcpi_ThermalZoneTemperature` access denied; Perf class readable | confirmed | `Get-CimInstance`, unelevated |
| 28 | `SystemProcessInformation` = 5, `STATUS_INFO_LENGTH_MISMATCH` = 0xC0000004 | confirmed | `winternl.h` 376; `ntstatus.h` 2021 |
| 29 | `SYSTEM_PROCESS_INFORMATION` offsets 0x00–0xF8, sizeof 0x100; thread struct offsets, sizeof 0x50 | confirmed, every field | phnt `ntexapi.h` 2705–2761 → ctypes (`v_ntqsi.py`) |
| 30 | `KTHREAD_STATE` 0–7 | confirmed | phnt `ntkeapi.h` 17–30 |
| 31 | Match with psutil for own process | confirmed; added `PagefileUsage` = psutil `vms` | `v_ntqsi.py` |
| 32 | Size retry (`0xC0000004` + ReturnLength, NULL/0 probe) | confirmed | `v_ntqsi.py` |
| 33 | NULL `ImageName` means pid 0 | confirmed | `v_ntqsi.py` |
| 34 | Thread times quantised at 156,250 | confirmed (3,486/3,486); added `GetSystemTimeAdjustment` = 156250, and the step holds with the timer resolution at 10 ms | `v_ntqsi.py`; phnt `NtQueryTimerResolution` |
| 35 | Per-thread CPU math; values above 100 % possible at 250 ms | confirmed (93.1 % at 1 s; 96.6–101.7 % at 0.25 s) | `v_ntqsi.py` |
| 36 | NtQSI(5) costs 6.6 ms | confirmed (5.52 ms median, 300 processes, 4,682 threads) | `v_ntqsi.py` |
| 37 | `process_iter` name() path [inferred, caching not traced] | **corrected** → `process_iter()` opens a handle to every process, through the `Process()` constructor | psutil `release-7.2.2` `__init__.py` |
| 38 | No processor field per thread; `GetThreadIdealProcessorEx` needs a thread handle | confirmed | layout; `processthreadsapi.h` 981 |
| 39 | ETW CSwitch needs admin or Performance Log Users | unverifiable (not tested) | — |
| 40 | `SYSTEM_CPU_SET_INFORMATION` +18/+19, 32 B; higher EfficiencyClass = faster | confirmed | `winnt.h` 12625–12662; learn.microsoft.com struct page |
| 41 | LPs 0–11 class 1, 12–15 class 0, LPs 4–7 SchedulingClass 2, 10 cores | confirmed (parking differs run to run) | `v_cpusets.py` |
| 42 | nvidia-smi field names, both spellings | confirmed (all 25 used, plus `clocks_event_reasons_counters.*`) | `nvsmi-help-query-gpu.txt` |
| 43 | Help quotes (sample period, `power.draw` average, `clocks.gr` alias, `memory.used` definition) | confirmed | same |
| 44 | `-lms` text; `-c` is `--compute-mode` | confirmed | `nvidia-smi -h` lines 71, 114 |
| 45 | Reason bits 0x1–0x100 at go-nvml v0.13.4-1 | confirmed; **added** 0x200 BoardLimit and 0x400 Reliability | `nvml.h` 3704–3800 |
| 46 | Streaming: ~256 ms gaps, pairs identical, units in header, `Active`/`Not Active`, exit 1, no survivors | confirmed | `v_nvsmi.py` |
| 47 | One-shot nvidia-smi costs 96 ms | confirmed (106 ms median) | `v_nvsmi.py` |
| 48 | NVML enums, units, `nvmlMemory_t.used` comment, `DEPRECATED(13.0)` on `GetTemperature` | confirmed | `nvml.h` 261–266, 1186, 1335, 1381–1397, 6481, 7055–7081, 7160 |
| 49 | NVML calls all return 0; busId `00000000:01:00.0`; `nvml.dll` 8.17.15.9186 | confirmed | `v_nvml.py`; file VersionInfo |
| 50 | NVML "≈2 µs per call" | **corrected** → ≈1.3 µs typical; `nvmlDeviceGetDisplayActive` 167 µs median (max 1.24 ms); `GetMemoryInfo` 3.7 µs | `v_nvml_cost.py` |
| 51 | NVML refreshes about every 500 ms | confirmed (intervals 500 ms) | `v_nvml.py` |
| 52 | Three VRAM definitions differ | confirmed; added NVML used = smi used + reserved | `v_nvml.py`, `v_nvsmi.py` |
| 53 | Throttle-time counters | **added**: NVML field 269 (ns), nvidia-smi `clocks_event_reasons_counters.*` (µs) | `nvml.h` 3097–3101, 3270–3279; `v_nvml.py` |
| 54 | Polling NVML wakes an idle Optimus dGPU | unverifiable (dGPU busy the whole time) | — |
| 55 | DXGI vtable slots, IID, `DXGI_ERROR_NOT_FOUND`, `DXGI_ADAPTER_DESC1`, `FLAG_SOFTWARE` = 2, `DXGI_GPU_PREFERENCE` 0/1/2 | confirmed | `v_vtbl.py` (counted from `dxgi.h` Vtbl structs); `winerror.h`; `dxgi1_6.h` |
| 56 | DXGI adapters, LUIDs, VRAM, outputs | confirmed | `v_gpu.py` |
| 57 | DXCore IIDs, slots 3–7, property IDs 2/7/11/12/13; output | confirmed | `dxcore_interface.h` 19–34, 101–295; `v_gpu.py` |
| 58 | D3DKMT structs, query types 6/15/70, WDDM 2.7 caps bits, adapter-type bits | confirmed (SDK has bits 0–12; 13–15 only online) | `d3dkmthk.h`, `d3dkmdt.h` 2108–2122; learn.microsoft.com page |
| 59 | D3DKMT values 0x232B / 0x2313 / 0x105, caps 0x1 / 0xB, addresses | confirmed | `v_gpu.py` |
| 60 | Display constants, output-technology values, `DISP_CHANGE_*` | confirmed | `wingdi.h` 2459–2463, 2804–2823, 3029–3030, 3211–3212; `WinUser.h` 12942–12967, 13019 |
| 61 | Struct sizes 220 / 72 / 64 / 420 | confirmed (a wrong `header.size` → 87) | `v_displays.py` |
| 62 | Only DISPLAY1 attached, 1920x1080 at 144 Hz, rates [60, 144], `CDS_TEST` → 0, 144.003 Hz, INTERNAL, empty friendly name | confirmed; added negative control 997 Hz → −2, and returned `dmSize` = 188 | `v_displays.py`, `v_modes.py` |
| 63 | `Win32_VideoController` stale 2560x1440@143, AdapterRAM capped at 4 GB | confirmed | `Get-CimInstance` |
| 64 | Stale mode is "probably the last external monitor" [inferred] | **retagged unverified**: `Win32_DesktopMonitor` gives 2560x1440 for the internal panel's own ID, whose largest mode is 1920x1080 | `Get-CimInstance Win32_DesktopMonitor`; `v_modes.py` |
| 65 | ChangeDisplaySettingsEx quotes | confirmed; "no admin needed" retagged inferred (never executed) | changedisplaysettingsexw page |
| 66 | Active plan Silent `64a64f24…`, processor settings 100/5/2, `powercfg /list` | confirmed | `v_power.py`; `powercfg /list` |
| 67 | Power GUIDs; `LocalFree` | confirmed | `winnt.h` 14078–14896; PowerGetActiveScheme page |
| 68 | Overlay functions exported, not in SDK headers; getters → GUID_NULL; registry overlay values absent | confirmed | `GetProcAddress`; grep of `um/` and `shared/`; `v_power.py` |
| 69 | Community signatures (GUID by value for Set) | confirmed as community code only | Q&A page text |
| 70 | Slider GUIDs and quotes | confirmed | customize-power-slider page |
| 71 | "Balanced = GUID_NULL" | still unverified; supporting evidence added (community table; AC getter = GUID_NULL while effective mode is Balanced) | Q&A page; `v_power_user.py` |
| 72 | Slider stored per power source | **added**: DC = Better battery `961cc777…`, AC = GUID_NULL | `v_power_user.py` |
| 73 | `PowerSetActiveOverlayScheme` fix; whether it needs admin | unverifiable (calling it would change a setting) | — |
| 74 | `EFFECTIVE_POWER_MODE` 0–6, V2 = 2, callback typedef, doc quotes | confirmed | `powersetting.h` 141–186; registration page; windows-rs 0.62.0 lines 1065–1071 |
| 75 | Alias numbering "normally doesn't renumber" | refined: the page lists aliases interleaved without values (a literal reading renumbers); Microsoft's windows-rs 0.62.0 keeps 0–6 | enum page text; windows-rs |
| 76 | Callback reports Balanced on registration | confirmed (+0.5 ms) | `v_power.py` |
| 77 | `SYSTEM_POWER_STATUS` meanings and values here | confirmed; added `BatteryFlag` 255 = unknown, and "bit" → flag value | struct page; `v_power.py` |
| 78 | `ActionSetActive` and Silent SDDL; `PowerSchemes` key ACL | confirmed | `powercfg /getsecuritydescriptor`; `Get-Acl` |
| 79 | Registry values in §8 (GameBar, GameConfigStore, GameDVR, policy, PolicyManager, HwSchMode, 7 UserGpuPreferences values, no DirectXUserGlobalSettings, HVCI Enabled = 1) | confirmed; noted a value with a trailing space | `Get-ItemProperty` |
| 80 | AllowGameDVR semantics and quote | confirmed | policy-csp-applicationmanagement page |
| 81 | `Win32_DeviceGuard` meanings, readable unelevated, doc says elevated | confirmed; cost 812 → 690–980 ms | HVCI page; `v_devguard.py` |
| 82 | Windowed-games support article "not fetched directly" | **corrected** → fetched; feature text verified, plus "Restart your game" and the Auto HDR dependency | support.microsoft.com 3f006843 page |
| 83 | Memory-integrity-for-gaming support article | unverifiable (URL returns a generic shell page) | support.microsoft.com a255f612 |
| 84 | Registry string ↔ `DXGI_GPU_PREFERENCE`; `AutoGameModeEnabled`, `AppCaptureEnabled` meanings; OpenGL behaviour | unverifiable (community sources only) | — |
| 85 | NVIDIA control-panel quote | confirmed | nvidia.com page text |
| 86 | Vulkan loader sorting quote and v6 requirement at `vulkan-sdk-1.4.363.0` | confirmed (tag exists) | raw fetch 200; page text |
| 87 | SetPriorityClass access right; constants | confirmed | SetPriorityClass page; `WinBase.h` 625–637 |
| 88 | HIGH works unelevated; REALTIME → HIGH silently | confirmed (own process); not documented on the page | `v_prio.py`; page text |
| 89 | PPL rights list includes `PROCESS_SET_INFORMATION` | confirmed; **added**: MsMpEng refuses even `QUERY_LIMITED` (error 5) | rights page; `v_prio.py` |
| 90 | `ObRegisterCallbacks` quotes | confirmed | OB_PRE_CREATE_HANDLE_INFORMATION page |
| 91 | Scheduling-priority quotes; base priorities 8 and 13 | confirmed | scheduling-priorities page table |
| 92 | "Audio threads usually run higher" [unverified] | partly verified: MMCSS 23–26 / 16–22 / 8–15, quota-exhausted threads 1–7 | MMCSS page |
| 93 | Class 88 = `SystemProcessIdInformation`, 24-byte struct, size retry, works on protected processes | confirmed | phnt enum (88, `// 90` marker), `ntexapi.h` 3892; psutil `proc.c`; `v_ntqsi.py` |
| 94 | psutil `exe()`, `name()`, `cpu_times()` open handles; `pids()` does not | confirmed | psutil `release-7.2.2` `proc.c` 45, 171, 232; `proc_utils.c` 124–145; `_pswindows.py` 730–741; `pids.c` 36 |
| 95 | Only the listed psutil calls open handles | **corrected** → constructing `Process(pid)` opens one too, as do `process_iter()` and `pid_exists()` | `__init__.py` 315–395; `_pswindows.py` 910–922; `proc.c` 38–48 |
| 96 | `net_io_counters` calls `GetAdaptersAddresses` each time | confirmed | psutil `net.c` 19–77 |
| 97 | IOCTL codes, `STORAGE_DEVICE_DESCRIPTOR` +16/+28, bus types 7/11/17, `DISK_EXTENT` 24 B, seek-penalty property 7 | confirmed | `winioctl.h` 419, 909–932, 1100–1109, 1214–1240, 1629–1639, 16026–16060 |
| 98 | Both drives NVMe SSD; `Get-PhysicalDisk` works unelevated | confirmed | `v_disk.py`; `Get-PhysicalDisk` |
| 99 | `EnumServicesStatusExW` constants, 56-byte entries, `dwProcessId` +44 | confirmed (154 services, 1.86 ms, PIDs match psutil) | `winsvc.h` 93, 159, 715, 734–777; `v_net_svc.py` |
| 100 | Defender "administrator mode"; Delivery Optimization content incl. Xbox Game Pass (PC) and Teams | confirmed | the two learn.microsoft.com pages |
| 101 | `esrv_svc.exe` at 6 % | point-in-time: the service was stopped an hour later and `esrv.exe` running | `Get-Service`, `Get-Process` |
| 102 | `fossilize_replay.exe`; RGB utilities and DPC spikes | unverifiable | — |
| 103 | `GetIfEntry2` 0.08 ms; `GetIfTable2` 6.1 ms / 69 rows; `net_io_counters` 67.6 ms | confirmed (0.11 ms; 3.88 ms / 69 rows; 57.6 ms); `MIB_IF_ROW2` 1,352 B | `v_net_svc.py` |
| 104 | Sampler cost 13.89 / 19.08 ms per poll, 5.3 % of one CPU | confirmed (12.52 / 19.93 ms, 5.8 %) | `v_sampler.py` |
| 105 | "One GPU Engine read came back empty after warm-up" | not reproduced for GPU Engine (0 failures in 110+ reads); the empty reads seen were `% Processor Time` failures | `v_pdh3.py` |
