"""The Python runtime lag-doctor's installer ships: python.org's embeddable 3.13.17 for x64.

Stage 2 measured this exact ZIP running as a service from Program Files (E3c, docs/research/install-and-rights.md
section 8: sys.path holds its own ZIP and folder only, and all 30 of its binaries are signed), and the MSI ships the
same bytes, so scripts/stage2/drive.py and scripts/msi_build.py both read the pin from here.
"""
from __future__ import annotations

# python.org's last 3.13 release with Windows binaries (3.13.18 is source-only). Digest and size as the release
# page lists them, and as re-hashed from the download on 2026-11-07.
EMBED_VERSION = "3.13.17"
EMBED_URL = f"https://www.python.org/ftp/python/{EMBED_VERSION}/python-{EMBED_VERSION}-embed-amd64.zip"
EMBED_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
EMBED_SIZE = 11_441_267
EMBED_PTH = f"python{EMBED_VERSION.replace('.', '')[:3]}._pth"      # python313._pth: what sets sys.path
