"""Safe imports for tests/test_stdlib_only.py. Never imported or run: only parsed.

Prose may say "import psutil" or name psutil.pids(): docstrings, comments and strings are not imports.
"""
from __future__ import annotations

import ctypes.wintypes
import importlib
import os, sys
import winreg
from ctypes import wintypes
from typing import TYPE_CHECKING

import lagdoctor
import lagdoctor.winapi.ntquery
from lagdoctor.capture import presentmon
from . import sibling
from .winapi import iphlp
from .. import model

# import psutil  (a comment is not an import)
message = "never import psutil under lagdoctor/"
psutil = None  # a name that happens to be psutil is not an import
importlib.import_module("lagdoctor.fixes.power")
importlib.import_module(f"lagdoctor.fixes.{name}")  # the fix catalog's own loader does this
importlib.import_module("json")
__import__("lagdoctor.report")
