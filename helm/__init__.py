"""helm — the personal knowledge home for people who build with coding agents.

One home (~/.helm) that knows your projects across every harness, holds the
authored knowledge chain per project (premises/heuristics/lexicon/prd/journal/
evals/archive), unifies the typed personal-knowledge store (priors/lexicon/
memory) behind one resolver, and keeps it all clean (drain + drift + lineage).

helm is an INDEX/OVERLAY over stores that already exist — harness session
homes, per-project memory dirs, recall indexes, the repos themselves. It
references; it never duplicates. Every projection is read-only as truth: an
edit lands in the source, and the projection re-derives.
"""

import os as _os

# A RECORDING GATE'S CHILD (task/3039, helm/gateloads.py). While a whole-suite
# gate records which files each test module reaches, a helm process a test
# starts writes, at exit, the repository files it loaded and read, so the test
# that started it is charged with them. Loaded BY PATH, never as
# `helm.gateloads`, so a recorded run adds no helm module to any child's
# sys.modules, and only when the gate set the variable or planted its marker
# in this checkout's git dir: every other process pays one environment read
# and one or two stats.
_GATE_LOADS = None
# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module data a
# test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_GATE_LOADS": "a recording gate's child state, armed once per process",
}


def _gate_loads_marked(root):
    """Does this checkout carry a recording gate's marker (helm/gateloads.py
    MARKER, in its git dir)? The cheap question every helm process whose
    environment lacks the keys asks: one stat, or one small read and a stat
    for a worktree."""
    dotgit = _os.path.join(root, ".git")
    if _os.path.isdir(dotgit):
        return _os.path.exists(_os.path.join(dotgit, "helm-gate-loads"))
    try:
        with open(dotgit, encoding="utf-8") as fh:
            line = fh.read(4096)
    except (OSError, UnicodeDecodeError):
        return False
    if not line.startswith("gitdir:"):
        return False
    found = line[len("gitdir:"):].strip()
    found = found if _os.path.isabs(found) else _os.path.join(root, found)
    return _os.path.exists(_os.path.join(found, "helm-gate-loads"))


_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _os.environ.get("HELM_GATE_LOADS_DIR") or _gate_loads_marked(_ROOT):
    try:
        import importlib.util as _util
        _spec = _util.spec_from_file_location(
            "_helm_gate_loads_child",
            _os.path.join(_ROOT, "helm", "gateloads.py"))
        _loads = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_loads)
        _GATE_LOADS = _loads.arm_child(root=_ROOT)
        del _util, _spec, _loads
    except Exception:                                   # noqa: BLE001
        _GATE_LOADS = None
del _ROOT

from . import seat_reachability  # noqa: E402

__version__ = "0.3.3"
