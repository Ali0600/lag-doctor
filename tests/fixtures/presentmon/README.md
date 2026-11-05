# PresentMon test fixtures

Real PresentMon 2.6.0 output, used by `tests/test_presentmon_*.py`.

## Source

- **Captures**: the gold ETW captures of Intel PresentMon v2.6.0,
  <https://github.com/GameTechDev/PresentMon/tree/v2.6.0/Tests/Gold>, tag `v2.6.0` =
  commit `e13fce6acdb55a808fd8318175a56863e532d95f`. The tests find them through the `gold_etl_dir`
  fixture (`%LOCALAPPDATA%\lag-doctor\testdata\presentmon-gold`).
- **Tool**: the PresentMon 2.6.0 release exe (`PresentMon-2.6.0-x64.exe`, 980,320 bytes,
  SHA-256 `b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af`, signed by Intel Corporation).
- **Replay flags**: lag-doctor's live flags minus the live-only ones (`docs/research/presentmon.md` section 10):
  `--etl_file <capture> --output_stdout --no_console_stats --qpc_time --track_frame_type --track_pc_latency --track_hybrid_present`.
  An ETL replay needs no admin rights.

Rebuild every file with `uv run python tests/fixtures/presentmon/regenerate.py`; two runs give identical bytes.

## Files

| File | What it is |
|---|---|
| `tc5_pid24892.csv` | test_case_5 read from a pipe (Latin-1, CRLF): every row of PresentBench.exe (PID 24892, 258 + 7 rows) plus the first 3 rows of each other swap chain, in PresentMon's own order |
| `tc0_head.csv` | test_case_0 from a pipe: the first 3 rows of every swap chain plus all 8 dropped frames |
| `tc0_header_row_mismatch.csv` | test_case_0 with `--no_track_display --no_track_gpu --track_pc_latency`: PresentMon 2.6.0's own bug, a 17-column header over 18-field rows (header + 4 rows) |
| `tc1_qpc_frametype.csv` | test_case_1 with `--qpc_time --track_frame_type` only: a different 29-column header (header + 3 rows) |
| `tc1_output_file_utf8.csv` | test_case_1 through `--output_file`: UTF-8 with a BOM (header + 4 rows) |
| `tc1_stdout_file_utf16.csv` | test_case_1 with stdout redirected to an empty disk file: UTF-16LE with a BOM (header + 4 rows) |
| `access_denied.stderr` | stderr of a live, unelevated start (exit code 6, 527 bytes); written only when the token cannot trace, so no capture ever starts |
| `fake_presentmon.py` | a stand-in child for the stop-protocol tests: it plays `tc5_pid24892.csv` into its stdout and handles console control events natively, as PresentMon does |
| `regenerate.py` | rebuilds every file above |

Trimming keeps whole lines exactly as PresentMon wrote them (cut at an end of line, never re-encoded).
`.gitattributes` here turns off line-ending conversion so the bytes stay as written.

The "already running" stderr that `fake_presentmon.py` writes is not observed output: it is built from
PresentMon's format strings in `PresentMon/MainThread.cpp` lines 338-341 and 363-370 at v2.6.0, because
seeing it for real needs an elevated capture.

## Licence

The gold captures and PresentMon are MIT-licensed; this is the licence at the tag above (`LICENSE.txt`):

```
Copyright (C) 2017-2024 Intel Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom
the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.
```
