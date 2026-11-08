"""Planted imports for tests/test_stdlib_only.py. Never imported or run: only parsed.

Every line that must be reported ends with an "expect" marker; the test compares the scanner's hits with
those markers, line for line.
"""
import importlib
import importlib as il
from importlib import import_module
from importlib import import_module as load

import psutil  # expect: a plain import
import psutil as ps  # expect: under an alias
import os, psutil  # expect: one name of several
import psutil._pswindows  # expect: a submodule
from psutil import pids  # expect: a from-import
from psutil._common import usage_percent  # expect: from a submodule
import pyglet  # expect: another package outside the standard library
import tkinter  # expect: a standard-library module the embeddable runtime leaves out
from tkinter import ttk  # expect: from a left-out package
import idlelib.pyshell  # expect: a left-out package's submodule
from turtle import Turtle  # expect: another left-out module
import curses  # expect: a module the runtime ships but cannot import
import cgi  # expect: a module Python 3.13 removed, still on Python 3.11's standard-library list

__import__("psutil")  # expect: __import__
importlib.import_module("psutil")  # expect: importlib.import_module
il.import_module("psutil._psutil_windows")  # expect: through an alias of importlib
import_module("psutil")  # expect: import_module imported by name
load("psutil")  # expect: import_module under another name
importlib.__import__("psutil")  # expect: importlib.__import__
importlib.import_module("venv")  # expect: a left-out module, loaded by name
importlib.import_module(name)  # expect: a name the scan cannot read
importlib.import_module(f"{package}.winapi")  # expect: an f-string that does not start with a module name
importlib.import_module(".winapi", "lagdoctor")  # expect: a relative name
importlib.import_module("lagdoctor" + suffix)  # expect: a name built at run time
importlib.import_module(f"lagdoctor{suffix}")  # expect: an f-string whose top-level name runs on


def later():
    import psutil  # expect: inside a function
    return psutil


try:
    import psutil  # expect: inside a try
except ImportError:
    psutil = None

if TYPE_CHECKING:
    from psutil import Process  # expect: for type checking only
