"""PresentMon CSV parsing and the mapping to F columns, on literal bytes and on real replay fixtures.

Literal lines below are real PresentMon 2.6.0 output (tests/fixtures/presentmon, test_case_5 and
test_case_0 replayed with lag-doctor's flags); a test that needs another case edits one field of them.
"""
from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import presentmon as pm
from lagdoctor.model import F

FIX = Path(__file__).parent / "fixtures" / "presentmon"

HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,AllowsTearing,"
          "PresentMode,FrameType,HybridPresent,TimeInQPC,MsBetweenSimulationStart,MsBetweenPresents,"
          "MsBetweenDisplayChange,MsInPresentAPI,MsRenderPresentLatency,MsUntilDisplayed,MsPCLatency,CPUStartQPC,"
          "MsBetweenAppStart,MsCPUBusy,MsCPUWait,MsGPULatency,MsGPUTime,MsGPUBusy,MsGPUWait,MsAnimationError,"
          "AnimationTime,MsFlipDelay,MsAllInputToPhotonLatency,MsClickToPhotonLatency")
SHOWN = ("PresentBench.exe,24892,0x2A70D2CAC00,DXGI,0,0,0,Composed: Flip,Application,0,2466961846796,NA,"
         "10.99180000000000,16.62670000000000,0.54730000000000,0.86620000000000,23.3253,NA,2466961742501,10.9768,"
         "10.4295,0.5473,0.1897,11.1060,0.8736,10.2324,-5.5830,1970.5829,NA,NA,NA")
DROPPED = ("PresentBench.exe,24892,0x2A70D2CAC00,DXGI,0,0,0,Composed: Flip,Application,0,2466961626795,NA,"
           "11.06220000000000,NA,0.52690000000000,0.78810000000000,NA,NA,2466961521260,11.0804,10.5535,0.5269,"
           "1.1731,10.1685,1.5667,8.6018,NA,NA,NA,NA,NA")
OTHER_RUNTIME = ("Presenter.exe,3976,0x0,Other,-1,0,0,Composed: Copy with GPU GDI,Application,0,2096121153,NA,"
                 "15.08910000000000,16.67170000000000,0.00000000000000,0.14000000000000,23.2474,NA,2095970262,"
                 "15.0891,15.0891,0.0000,0.5439,14.6852,0.2073,14.4779,-1.9828,2278.6016,NA,NA,NA")
COLS = HEADER.split(",")


def edit(line: str, **values: str) -> str:
    fields = line.split(",")
    for name, value in values.items():
        fields[COLS.index(name)] = value
    return ",".join(fields)


def rows_of(data: bytes, parser: pm.CsvParser | None = None, chunk: int | None = None) -> list[dict]:
    chunks = [data] if chunk is None else [data[i:i + chunk] for i in range(0, len(data), chunk)]
    return list(pm.parse(chunks, parser))


def csv(*lines: str) -> bytes:
    return "".join(ln + "\r\n" for ln in lines).encode("latin-1")


# ---------------------------------------------------------------- decoding (rule 1) ---

def test_pipe_text_is_latin1_not_the_ansi_code_page():
    # PresentMon writes U+0000-U+00FF into a pipe as single bytes (PM 4, V9); cp1252 would turn 0x80 into a euro.
    raw = csv(HEADER, edit(SHOWN, Application="Pr\xe9\x80.exe"))
    [row] = rows_of(raw)
    assert row["Application"] == "Pré\u0080.exe"


def test_crlf_is_stripped_from_the_last_field():
    [row] = rows_of(csv(HEADER, edit(SHOWN, MsClickToPhotonLatency="1.2500")))
    assert row["MsClickToPhotonLatency"] == 1.25


@pytest.mark.parametrize("chunk", [None, 1, 7, 64])
def test_bom_files_decode_to_the_same_rows_in_any_chunking(chunk):
    utf16 = (FIX / "tc1_stdout_file_utf16.csv").read_bytes()
    utf8 = (FIX / "tc1_output_file_utf8.csv").read_bytes()
    assert utf16[:2] == b"\xff\xfe" and utf8[:3] == b"\xef\xbb\xbf"
    p16, p8 = pm.CsvParser(), pm.CsvParser()
    a, b = rows_of(utf16, p16, chunk), rows_of(utf8, p8, chunk)
    assert len(a) == 4 and a == b
    assert p16.headers == p8.headers == 1 and p16.malformed == p8.malformed == 0
    assert a[0]["Application"] == "dwm.exe" and isinstance(a[0]["TimeInQPC"], int)


def test_a_utf8_bom_is_not_left_on_the_header():
    # With the BOM left in place the first line would not look like a header and every row would be refused.
    p = pm.CsvParser()
    assert rows_of(b"\xef\xbb\xbf" + csv(HEADER, SHOWN), p)
    assert p.headers == 1 and p.malformed == 0


def test_an_overlong_line_is_counted_and_dropped_without_losing_the_next_rows():
    p = pm.CsvParser()
    junk = "x" * (pm.MAX_LINE_CHARS + 10)
    rows = rows_of(csv(HEADER, junk, SHOWN), p, chunk=4096)
    assert len(rows) == 1 and p.malformed == 1


@pytest.mark.parametrize("chunk", [None, 1000])
def test_the_line_limit_keeps_a_line_at_the_limit_and_drops_one_past_it(chunk):
    at, past = "a" * pm.MAX_LINE_CHARS, "b" * (pm.MAX_LINE_CHARS + 1)
    data = f"{at}\r\n{past}\r\nnext\r\n".encode("latin-1")
    s = pm.LineSplitter()
    pieces = [data] if chunk is None else [data[i:i + chunk] for i in range(0, len(data), chunk)]
    lines = [ln for piece in pieces for ln in s.feed(piece)] + s.close()
    assert lines == [at, "next"] and s.dropped == 1


def test_the_splitter_never_buffers_more_than_one_line_limit():
    s = pm.LineSplitter()
    assert s.feed(b"abc") == []
    for _ in range(300):                                      # 1.2 MB with no newline
        assert s.feed(b"y" * 4096) == []
        assert len(s._pending) <= pm.MAX_LINE_CHARS + 1
    assert s.feed(b"tail\r\nnext\r\n") == ["next"] and s.dropped == 1


def test_a_long_application_name_under_the_limit_is_still_read():
    p = pm.CsvParser()
    name = "n" * (pm.MAX_LINE_CHARS - len(SHOWN) - 10) + ".exe"
    [row] = rows_of(csv(HEADER, edit(SHOWN, Application=name)), p)
    assert row["Application"] == name and p.malformed == 0


def test_a_last_line_cut_off_without_its_newline_is_counted_not_parsed():
    p = pm.CsvParser()
    rows = rows_of(csv(HEADER, SHOWN) + SHOWN.encode("latin-1"), p)
    assert len(rows) == 1 and p.malformed == 1


def test_blank_lines_are_ignored():
    p = pm.CsvParser()
    assert len(rows_of(b"\r\n" + csv(HEADER) + b"\r\n" + csv(SHOWN), p)) == 1 and p.malformed == 0


# --------------------------------------------------------- headers and rows (rules 2-4) ---

def test_a_row_before_any_header_is_malformed():
    p = pm.CsvParser()
    assert rows_of(csv(SHOWN, HEADER, SHOWN), p) and p.malformed == 1 and p.rows == 1


def test_every_header_line_rebuilds_the_column_map():
    second = (FIX / "tc1_qpc_frametype.csv").read_bytes()       # 29 columns: no HybridPresent, no MsPCLatency
    p = pm.CsvParser()
    rows = rows_of(csv(HEADER, SHOWN) + second, p)
    assert p.headers == 2 and p.malformed == 0 and len(rows) == 4
    assert "HybridPresent" in rows[0] and "HybridPresent" not in rows[1]
    first_line = second.split(b"\r\n")[1].decode("latin-1").split(",")
    assert rows[1]["TimeInQPC"] == int(first_line[second.split(b"\r\n")[0].split(b",").index(b"TimeInQPC")])
    assert pm.frame_row(rows[1], 0.0, "0x1")[F.HYBRID] is None


def test_a_header_without_the_columns_lag_doctor_needs_makes_its_rows_unusable():
    default = HEADER.replace("TimeInQPC", "TimeInMs")           # PresentMon run without --qpc_time
    p = pm.CsvParser()
    assert rows_of(csv(default, SHOWN, SHOWN), p) == []
    assert p.missing == ["TimeInQPC"] and p.malformed == 2


def test_a_comma_in_the_application_name_is_rejoined():
    p = pm.CsvParser()
    rows = rows_of(csv(HEADER, edit(SHOWN, Application="My,Odd,Game.exe")), p)
    assert [r["Application"] for r in rows] == ["My,Odd,Game.exe"]
    assert rows[0]["ProcessID"] == 24892 and rows[0]["SwapChainAddress"] == 0x2A70D2CAC00
    assert p.rejoined == 1 and p.malformed == 0


def test_extra_fields_that_do_not_line_up_are_refused_not_rejoined():
    # PresentMon 2.6.0's own bug: a 17-column header with 18-field rows (PM 1.4). Never misparse it.
    p = pm.CsvParser()
    assert rows_of((FIX / "tc0_header_row_mismatch.csv").read_bytes(), p) == []
    assert p.headers == 1 and p.malformed == 4 and p.rejoined == 0


def test_a_comma_name_whose_pid_does_not_line_up_is_refused():
    p = pm.CsvParser()
    bad = edit(SHOWN, Application="My,Game.exe", ProcessID="24x92")
    assert rows_of(csv(HEADER, bad), p) == [] and p.malformed == 1 and p.rejoined == 0


def test_a_short_row_is_malformed():
    p = pm.CsvParser()
    assert rows_of(csv(HEADER, SHOWN.rsplit(",", 1)[0]), p) == [] and p.malformed == 1


def test_values_are_typed_by_column():
    [r] = rows_of(csv(HEADER, OTHER_RUNTIME))
    assert r["ProcessID"] == 3976 and r["SwapChainAddress"] == 0 and r["SyncInterval"] == -1
    assert r["TimeInQPC"] == 2096121153 and isinstance(r["TimeInQPC"], int)
    assert r["MsBetweenPresents"] == pytest.approx(15.0891) and r["MsAnimationError"] == pytest.approx(-1.9828)
    assert r["PresentMode"] == "Composed: Copy with GPU GDI" and r["MsFlipDelay"] is None


@pytest.mark.parametrize("column,value", [
    ("MsBetweenPresents", "abc"), ("MsCPUBusy", "1.0x"), ("TimeInQPC", "12.5"), ("TimeInQPC", "2466_961"),
    ("ProcessID", "\xb2"), ("ProcessID", "-4"), ("ProcessID", "4294967296"), ("SwapChainAddress", "2A70D2CAC00"),
    ("MsUntilDisplayed", " 1.0"), ("TimeInQPC", "NA"),
])
def test_a_value_that_cannot_be_right_makes_the_row_malformed(column, value):
    p = pm.CsvParser()
    assert rows_of(csv(HEADER, edit(SHOWN, **{column: value})), p) == []
    assert p.malformed == 1


def test_printf_nan_is_a_missing_value_not_a_lost_frame():
    [r] = rows_of(csv(HEADER, edit(SHOWN, MsGPULatency="-nan(ind)")))
    assert r["MsGPULatency"] is None and r["MsBetweenPresents"] == pytest.approx(10.9918)


def test_text_in_a_column_lag_doctor_does_not_read_is_kept():
    [r] = rows_of(csv(HEADER + ",Note", SHOWN + ",hello"))
    assert r["Note"] == "hello"


# ------------------------------------------------------------------ mapping to F ---

def test_a_displayed_frame_maps_to_every_f_column():
    [r] = rows_of(csv(HEADER, SHOWN))
    origin = 100.0
    fr = pm.frame_row(r, origin, "0x2A70D2CAC00")
    assert set(fr) == set(F.ALL)
    assert fr[F.T] == pytest.approx(2466961846796 / clock.qpc_frequency() - origin)
    assert fr[F.FRAME_MS] == pytest.approx(10.9918)
    assert fr[F.CPU_BUSY_MS] == pytest.approx(10.4295) and fr[F.CPU_WAIT_MS] == pytest.approx(0.5473)
    assert fr[F.GPU_LATENCY_MS] == pytest.approx(0.1897) and fr[F.GPU_BUSY_MS] == pytest.approx(0.8736)
    assert fr[F.GPU_WAIT_MS] == pytest.approx(10.2324)
    assert fr[F.DISPLAYED] == 1 and fr[F.DISPLAY_MS] == pytest.approx(16.6267)
    assert fr[F.DISPLAY_LATENCY_MS] == pytest.approx(10.4295 + 23.3253)
    assert fr[F.INPUT_LATENCY_MS] is None and fr[F.ANIM_ERROR_MS] == pytest.approx(-5.583)
    assert (fr[F.PRESENT_MODE], fr[F.RUNTIME], fr[F.SYNC_INTERVAL], fr[F.ALLOWS_TEARING]) == ("Composed: Flip", "DXGI", 0, 0)
    assert (fr[F.SWAP_CHAIN], fr[F.FRAME_TYPE], fr[F.HYBRID]) == ("0x2A70D2CAC00", "Application", 0)


def test_a_dropped_frame_is_displayed_0_with_no_display_times():
    [r] = rows_of(csv(HEADER, DROPPED))
    fr = pm.frame_row(r, 0.0, "0x1")
    assert fr[F.DISPLAYED] == 0 and fr[F.DISPLAY_MS] is None and fr[F.DISPLAY_LATENCY_MS] is None
    assert fr[F.FRAME_MS] == pytest.approx(11.0622)


def test_displayed_is_unknown_when_display_tracking_is_off():
    no_display = ",".join(c for c in COLS if c != "MsUntilDisplayed")
    line = ",".join(v for c, v in zip(COLS, SHOWN.split(",")) if c != "MsUntilDisplayed")
    [r] = rows_of(csv(no_display, line))
    assert pm.frame_row(r, 0.0, "0x1")[F.DISPLAYED] is None


@pytest.mark.parametrize("column,f_name", [("MsCPUBusy", F.CPU_BUSY_MS), ("MsCPUWait", F.CPU_WAIT_MS)])
def test_a_zero_cpu_time_means_missing_but_the_smallest_real_value_is_kept(column, f_name):
    # PresentMon prints a missing CPU busy/wait as 0.0000 (WriteMetricOrZero, PM 2.3).
    [zero, tiny] = rows_of(csv(HEADER, edit(SHOWN, **{column: "0.0000"}), edit(SHOWN, **{column: "0.0001"})))
    assert pm.frame_row(zero, 0.0, "0x1")[f_name] is None
    assert pm.frame_row(tiny, 0.0, "0x1")[f_name] == pytest.approx(0.0001)


def test_the_kernel_only_runtime_has_no_cpu_wait():
    [r] = rows_of(csv(HEADER, OTHER_RUNTIME))
    fr = pm.frame_row(r, 0.0, "0x0")
    assert fr[F.CPU_WAIT_MS] is None and fr[F.CPU_BUSY_MS] == pytest.approx(15.0891)
    assert fr[F.RUNTIME] == "Other" and fr[F.SWAP_CHAIN] == "0x0"


def test_a_generated_frame_has_no_cpu_timing_and_no_gpu_busy_of_its_own():
    [gen, app] = rows_of(csv(HEADER, edit(SHOWN, FrameType="Intel XeSS-FG"), SHOWN))
    g, a = pm.frame_row(gen, 0.0, "0x1"), pm.frame_row(app, 0.0, "0x1")
    for col in (F.CPU_BUSY_MS, F.CPU_WAIT_MS, F.GPU_BUSY_MS, F.DISPLAY_LATENCY_MS):
        assert g[col] is None and a[col] is not None, col
    assert g[F.FRAME_MS] == a[F.FRAME_MS] and g[F.GPU_LATENCY_MS] == a[F.GPU_LATENCY_MS]
    assert g[F.FRAME_TYPE] == "Intel XeSS-FG"


def test_without_a_frame_type_column_every_frame_counts_as_the_apps_own():
    cols = ",".join(c for c in COLS if c != "FrameType")
    line = ",".join(v for c, v in zip(COLS, SHOWN.split(",")) if c != "FrameType")
    [r] = rows_of(csv(cols, line))
    fr = pm.frame_row(r, 0.0, "0x1")
    assert fr[F.FRAME_TYPE] is None and fr[F.CPU_BUSY_MS] == pytest.approx(10.4295)


def test_a_zero_gpu_busy_on_an_app_frame_is_kept():
    [r] = rows_of(csv(HEADER, edit(SHOWN, MsGPUBusy="0.0000")))
    assert pm.frame_row(r, 0.0, "0x1")[F.GPU_BUSY_MS] == 0.0


def test_the_main_chain_is_the_one_with_the_most_displayed_frames():
    chains = {"0xA": {"frames": 10, "displayed": 2}, "0xB": {"frames": 8, "displayed": 6},
              "0xC": {"frames": 9, "displayed": 6}}
    assert pm.main_chain_of(chains) == "0xC"                  # ties on displayed go to more frames
    assert pm.main_chain_of({}) is None


# ---------------------------------------------------------- whole replay fixtures ---

def _chains(rows: list[dict]) -> dict[tuple[int, int], list[dict]]:
    out: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        out.setdefault((r["ProcessID"], r["SwapChainAddress"]), []).append(r)
    return out


def test_the_test_case_5_fixture_gives_the_reference_numbers():
    # PM 10: PresentBench.exe pid 24892 -> 258 rows, 84 dropped, median MsBetweenPresents 11.051.
    p = pm.CsvParser()
    chains = _chains(rows_of((FIX / "tc5_pid24892.csv").read_bytes(), p))
    main = chains[(24892, 0x2A70D2CAC00)]
    assert len(main) == 258 and sum(r["MsUntilDisplayed"] is None for r in main) == 84
    assert round(statistics.median(r["MsBetweenPresents"] for r in main), 3) == 11.051
    assert len(chains[(24892, 0x0)]) == 7
    assert p.headers == 1 and p.malformed == 0 and p.rows == 280


def test_rows_are_ordered_within_a_chain_but_not_across_chains():
    rows = rows_of((FIX / "tc5_pid24892.csv").read_bytes())
    times = [r["TimeInQPC"] for r in rows]
    assert any(b < a for a, b in zip(times, times[1:]))          # the stream really is interleaved
    for chain in _chains(rows).values():
        t = [r["TimeInQPC"] for r in chain]
        assert t == sorted(t)


def test_the_test_case_0_fixture_maps_without_loss():
    p = pm.CsvParser()
    rows = rows_of((FIX / "tc0_head.csv").read_bytes(), p)
    frames = [pm.frame_row(r, 0.0, f"0x{r['SwapChainAddress']:X}") for r in rows]
    assert p.malformed == 0 and len(frames) == 36
    other = [f for f in frames if f[F.RUNTIME] == "Other"]
    assert other and all(f[F.CPU_WAIT_MS] is None for f in other)
    assert all(f[F.CPU_WAIT_MS] is not None for f in frames if f[F.RUNTIME] != "Other")
    assert sum(f[F.DISPLAYED] == 0 for f in frames) == 8           # every dropped frame of test_case_0 (PM 2.3)
