"""The Stage 2 experiments (docs/INSTALLER-PLAN.md, Stage 2): a throwaway capture service on GitHub's runners.

They install Windows services and write under Program Files, so they run only on GitHub's own throwaway
runners (guard.require_throwaway_runner), from .github/workflows/experiments.yml. drive.py is the entry point;
service_host.py is the service itself, run by python.org's embeddable runtime; standin.py stands in for a game.
"""
