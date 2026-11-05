# Scratch project for the red-first runner's own tests

`tests/test_redfirst.py` copies this folder to a temp folder, puts the real `scripts/redfirst.py` into its
`scripts/`, and runs it on specs that sabotage `lagdoctor/guard.py`.

- No `__init__.py` and no `test_*.py` names, so the real suite never collects or imports anything here.
- This README is a file at the project root that the runner must not copy (only `lagdoctor/`, `laglab/`,
  `tests/`, `scripts/`, `docs/`, `pyproject.toml` and `uv.lock` are copied).
