"""`pythonw -B -E -s -m lagdoctor.relay ...`: the relay process (relay/server.py), started through one UAC prompt."""
import sys

from lagdoctor.relay.server import main

sys.exit(main())
