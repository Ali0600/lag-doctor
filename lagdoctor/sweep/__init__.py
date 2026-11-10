"""The settings sweep: try a game's settings one after another and rank them (`lagdoctor sweep`).

The owner chose the assisted sweep (docs/DECISIONS.md, 2026-11-09; research in docs/research/settings-sweeps.md).
lag-doctor writes each setting into the game's own settings file while the game is closed, through the fix journal
(lagdoctor/fixes/game_setting.py), and puts the original back at the end. The player starts the game, loads a save,
presses the start key at a chosen spot and quits; each run is an ordinary recording (`watch`), measured over a fixed
window from the key, and the settings are ranked against their own run-to-run spread.

- inifile.py: one value of an ini file read or changed, every other byte kept (pure);
- plan.py: the settings tried, the order of the runs and the sweep's state file (pure);
- results.py: each run's numbers over its window, the checks that keep a run out, and the ranking (pure);
- runner.py: the loop that applies a setting, asks the player, records and checks (its seams injectable);
- command.py: `lagdoctor sweep`, which builds the sweep from the command line and runs it.
"""
