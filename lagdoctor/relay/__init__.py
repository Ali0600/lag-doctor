"""The relay: frame timing and the game-server tracker with administrator rights, for one recording.

`lagdoctor watch` records in the player's own window. When frame timing or the game-server tracker needs rights
this window lacks, it starts the relay through one UAC prompt (`client.launch`). The relay (`server.py`, run as
`pythonw -m lagdoctor.relay`) runs PresentMon on the approved game and the Kernel-Network ETW session, and passes
back over one locked pipe only what the recording needs: the game's frame rows, the game's and the recorder's own
network events, and bytes received and sent per program for the programs in the recorder's Windows session. It
writes no file and takes no PresentMon or ETW argument from the pipe. The messages are in `protocol.py`; the
contract is in docs/DESIGN.md ("The relay"). Stage 4's capture service (docs/INSTALLER-PLAN.md) is the same
protocol with another policy.
"""
