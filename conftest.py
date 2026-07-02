"""Ensures the repo root is importable as `src.*` regardless of how pytest is invoked.

There is no setup.py/pyproject.toml/src package `__init__.py` in this repo (main.py
also relies on plain `from src... import ...` run from the repo root) -- this file's
mere presence makes pytest anchor its rootdir/sys.path insertion here.
"""
