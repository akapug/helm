#!/usr/bin/env python3
"""pytest's entry to the suite-wide env. The logic lives in tests/__init__.py.

THIS FILE DELIBERATELY HOLDS NO PLANTING OF ITS OWN. It used to, and that was
the bug: a conftest is read by pytest and by nothing else, while this repo's
canonical runner is `python -m unittest discover` (CONTRIBUTING.md, AGENTS.md,
and the fab gate). Everything planted here was a no-op on the gate and in
the normal local run, which is worse than an empty file — it reads as
protection, so nobody looks again. The incident history that used to live in
this docstring moved with the code it explains; see tests/__init__.py.

Keeping a pytest copy in step with a unittest copy is not something anyone can
promise over time: one of the two drifts and only the runner nobody uses
notices. So the plants moved to `tests/__init__.py`, the one module BOTH
runners load before any test in the package runs, and this file exists only to
put the pytest path through that same module.

The import below is the whole point, not a formality — `tests/__init__.py`
plants at IMPORT time, so importing the package IS the effect. pytest already
imports this conftest as `tests.conftest` because this directory is a package,
which imports `tests` first; stating it here makes the dependency explicit and
keeps the plant under an import mode that would load this file standalone.

Anyone adding a suite-wide env var: put it in tests/__init__.py. If you are
typing `os.environ` into this file, that is the drift this docstring is about.
"""
from . import PLANTED  # noqa: F401  — imported for its import-time side effect
