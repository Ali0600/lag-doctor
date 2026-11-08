# The first real capture: what it showed lag-doctor getting wrong

One 14-minute recording of FINAL FANTASY VII REBIRTH (`ff7rebirth_.exe`) on this laptop, 2026-11-05, with
`--no-network`. PresentMon was not visible to the recording (see Fixed), so it has no frame timing: only the
system sampler, NVIDIA telemetry, the inventory and three moments marked with the hotkey. Three read-only
reviewers recomputed every claim of its report from the raw capture
(`%LOCALAPPDATA%\lag-doctor\sessions\20261105-…-ff7rebirth`). This is the curated result: what was fixed
with it, and what is still open, each with the code to start from.

## What the capture showed

- **The GPU slowed itself down from heat almost all the time.** NVML named `sw_thermal_slowdown` in 840 of
  845 readings (never the power brake), at 77-89 °C (median 87) against a 75 °C target. The core clock never
  passed 780 MHz (37 % of its 2,100 MHz top), and was 405-420 MHz at all three marks while the GPU was 98-100 %
  busy; it sat in P-state 3 throughout. The enforced power limit fell from about 85 W to 55 W at 02:49 (default
  80 W) and stayed there.
- **Video memory** was at or above 90 % from 12:26 to the end; one spill (shared memory 253 to 793 MB) came with
  a 1-2 s GPU drop at 13:44-13:46.
- **Commit charge** stood at 89-99.9 % of the commit limit all session while 7.9 GB or more of RAM stayed free:
  a small page file.
- **The CPU** never ran above its base clock (median 70 % of base) while Windows reported no limit.
- **The Windows plan "Silent"** sets the CPU exactly as Balanced does (`powercfg /qh`: 180 of 182 settings
  equal; the others are screen brightness and the battery hibernate timer).

## Fixed with this review

- PresentMon set up from a shell inside the Claude desktop app landed in that app's private copy of AppData and
  was "not installed" everywhere else: `paths.private_copy`, refused by `setup`, `watch`, `fix` and `undo`
  (exit 5); `scripts/e2e.py` stops before its UAC prompt (exit 3).
- `watch --elevate` recorded 14 minutes without frame timing and said so only in the report: it now stops
  first (exit 6), says so before recording when frame timing is off, and warns live when PresentMon fails or
  ends mid-recording (a crash, its trace session stopped from outside) or never sends a frame while the game is
  in front; the status line shows `off` for a source not running and `-` once PresentMon has ended.
- `scripts/e2e.py` asked for UAC, then failed all 8 scenarios on the same missing PresentMon: a preflight.
- `power_plan` blamed the plan by its name ("eco" matched "ASUS Recommended"), took GPU heat as its evidence,
  and offered a switch that would only have changed the screen brightness: judged by the plan's own settings
  now (the P-core cap included on hybrid CPUs), and Armoury Crate's quiet mode is read through the plan it
  switches to, with its own advice and no switch of Windows plan.
- "held down by heat or its power brake" when only heat fired: `attribute.held_by`.
- `--no-network` showed as "not recorded in this capture": now "turned off for this recording".

## Still open: analysis

1. **Without frame timing, GPU heat and video memory go to "Could not check"** whenever the whole-capture median
   game GPU use is under 95 % (here 71 %), though the GPU was 98-99 % busy in minutes 11-13 and at every mark;
   their own results were high and medium findings. `analysis/frames.py` no-frames bound verdict; the gate in
   `rules.py` (`_gated`).
   *Fixed (PR #7):* the verdict stays `unknown`, and `frames.probable_gpu` reads LC 3.4's fallback per second;
   a GPU-side finding passes the gate when the engine was at 95 % or more in 10 % of the 1 s windows and 10 s, or
   at half the marks, and says so in its evidence. The capture: 173 s (20 %) and 3 of 3 marks, so GPU heat is
   likely and video memory possible.
2. **"The CPU ran at full speed"** is cleared from the limit flags and a drop against the first two minutes; the
   level itself (median 70 % of base) is never checked. `rules._cpu_throttle`.
   *Fixed (PR #7):* a median `% Processor Performance` at or below 80 % of base counts on its own ("The CPU ran
   below its base clock"), and the cleared text says what was measured. The capture: worth checking, since the
   game's busiest thread stayed at 58 % of a CPU.
3. **Focus "checked and fine"** on the whole-session share (94 %) while 2 of the 3 marks fell in the one stretch
   with another window in front; with fewer than 5 marks the lift guard never applies. `rules._not_focused`.
   *Fixed (PR #PR2):* the session share counts against only when the lag moments did not come while the game was
   behind more often than that share; with too few for the lift test the cause is unchecked, with the counts. The
   capture: "the game was the active window 94% of the time, but 2 of the 3 marked moments came while another
   window was in front: too few to tell".
4. **"A system signal moved" for standing conditions.** `asset_streaming` fires on any read of 1 MiB/s or more
   when the quiet baseline is 0 (z = inf): in 64 % of all windows here, with the SSD at most 19 % busy.
   `vram_overflow` compares a level with the whole-capture median and says "rose" for a flat plateau.
   `attribute.py` (`_judge`, the asset_streaming and vram_overflow parts).
   *Fixed (PR #PR2):* a baseline whose MAD is 0 takes its spread from the mean absolute deviation (a read must
   reach 53 MB/s here, past 7.9 % of the quiet seconds), and the shared-memory spill is judged by how far it rose
   within 5 s. The capture's first mark now shows nothing moved, the plateau no spill at any mark.
5. **The lift test is inflated**: a busiest-1-s peak over an event's longer window against a plain 1-s quiet
   share (asset_streaming: base 0.36, real chance for a mark-sized window 0.64). `attribute.py` lift.
   *Fixed (PR #PR2):* `b` is the chance that a quiet stretch as long as each event's window holds an active
   second (`attribute._expected`). The capture's reads now have b 0.20 at the marks, lift 1.7: no link.
6. **Marks keep two causes, picked by `CAUSE_ORDER` when z ties at `Z_CAP`**, dropping background CPU at 602 % of
   a CPU and the CPU at 56 % of base. `diagnose.py` (the `[:2]`), `attribute.py` ranking.
   *Fixed (PR #PR2):* a mark keeps every signal that moved, best first, in the stats and both reports. The
   ranking stays (ties at the cap still go to the more specific cause); with item 4 fixed, ties are rarer. The
   capture: 13:27 lists the other window, other programs at 602 % of a CPU and a 73 MB/s read; 13:35 the CPU at
   56 % of base.
7. **Commit-limit exhaustion and "RAM stayed free" cancel** in one evidence family, so a commit charge at
   99.9 % is never reported. `rules._ram_pressure`.
   *Fixed (PR #7):* over 90 % committed with RAM free, free RAM is no evidence against (nor hard faults for, being
   file reads); the finding is titled for the page file. The capture: possible, "Windows nearly ran out of memory to
   hand out: the page file is small".
8. **`_heat_share` counts idle readings** (NVML's counters keep growing at `gpu_idle`). `rules._heat_share`.
   *Fixed (PR #7):* `attribute.throttle_readings`, which the rule and the per-event signal both read, leaves out
   readings that name `gpu_idle`; a GPU idle in every reading is unchecked, not "checked and fine".
9. **`_power_cap_item` trusts the field-74 counter**, which on driver 591.86 read 42 % while the `sw_power_cap`
   reason was named in 0 of 169 readings. `rules._power_cap_item`.
   *Fixed (PR #7):* `attribute.power_cap_readings` counts a reading's share only where its reasons named the cap,
   and the evidence says so when the counter moved without it. The capture: 78 % at a 55 W cap, from 670 of 839
   readings.
10. **Background CPU counts lag-doctor's own processes** (`target["tool_pids"]`). `attribute._others`.
    *Fixed (PR #PR2):* its processes leave the listed totals and the machine-wide reading as well as the blame;
    `rules._others_steady` agrees. On the capture the background baseline fell from 91 to 85 % of a CPU.
11. **A capture with only marks** never reaches the "too few events" branch, so the causes the marks named vanish
    from both lists. `rules.py` stutter-cause rules (`_first_cause_ids`).
    *Fixed (PR #PR2):* with marks standing in for the frames, the marks are the events the rules judge
    (`_Env.judged`): too few of them is unchecked ("only 3 marked moments"), and a supported cause is titled
    "Lag you marked ...". The capture's marks name no stutter cause first once item 4 is fixed.

## Still open: wording (40 found; the ones a player meets first)

- The verdict wraps the whole PresentMon message in brackets, path and command included (`diagnose.headline`):
  a short reason there, the path and the command once under Not measured.
- "frame timing was skipped:" doubles "frame timing" in every list, and `_Env.need` chains colons ("needs X, which
  was not measured: ..."): "these need frame timing, which was not recorded (see Not measured)".
- The Game Bar cause's title is its only evidence line (`rules._game_dvr`).
- The console breaks the report path at "lag-" (`console.py` wraps with `break_on_hyphens`): the path on a line
  of its own.
- The status line is 95 characters in a 71-column console, so `\r` rewrites only its last row and the screen
  scrolls a row a second: cut it to the console width, the key hint only in "Recording...".
- Offsets (13:22) and durations (14:05) print like times of day beside the start time (`charts.fmt_clock`).
- "Checked and fine" is sorted case-sensitively; "video memory full" at 92 % reads "over 90 % full"; "CPU load"
  and "GPU load" sit under Could not check though both were measured (`labels.py` TOPICS).
- HTML: "No lag events were found" when nothing could find them; "0 frames" for "frame timing not recorded";
  "undo puts it back" twice in a fix box; backticks shown as characters; the game named by its exe, not its window
  title.
