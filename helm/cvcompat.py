"""The one seam between helm and the cv on PATH: which grammar it speaks.

cv 0.11 changed its interface on purpose and kept no aliases (upstream
docs/INTERFACE-V2.md). The parts helm touches:

  - `cv prune --thinking` is `--drop-thinking`; a bare `--thinking` now names
    what an EMIT does with reasoning, and `cv prune` refuses it.
  - a message window is `A..B` (it was `A-B`); both are 0-based, end-exclusive.
  - every `--json` key is snake_case (`size_bytes`, `message_count`,
    `updated_at`, `created_at`, and the prune report's `new_id`/`source_id`).
  - a block in `cv show --json` is tagged `type` (it was `kind`), and a message
    now carries its own `kind` (prompt, reply, injected_context, ...).
  - the MCP tools take the command names: `recall` and `read_session` are gone,
    `search`, `show` and `pack` replace them.

Helm runs against both grammars until every host has the new cv. Every call
site asks this module rather than spelling a version itself.

WRITERS (argv) need the version, so it is probed once per resolved binary
identity and only when a writer asks. READERS (JSON) never need it: they
accept both spellings, even when cv is upgraded under a running process.

This file is also imported by `catalog.py` when that module runs as a script,
so it imports nothing from the package.
"""
import os
import re
import shutil
import subprocess

CV = "cv"
V2 = (0, 11, 0)            # the first release that speaks the new grammar
DROP_THINKING = "--drop-thinking"
_PROBE_TIMEOUT = 15
_TIMED_OUT = object()
_seen = []                 # [resolved-binary identity, probed version]


def _binary_identity():
    """Resolve cv anew: the same PATH spelling can name replaced bytes."""
    path = shutil.which(CV)
    if path is None:
        return None
    try:
        path = os.path.realpath(path)
        st = os.stat(path)
    except OSError:
        # Unreadable identity cannot authorize reuse of a previously probed
        # grammar; a subsequent writer resolves and probes afresh.
        return None
    return (path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
            st.st_ctime_ns)


def version():
    """The installed cv's (major, minor, patch), once per binary identity.

    An unreadable identity or a timed-out probe cannot reuse a cached success;
    the next writer tries again. Popen (not `subprocess.run`) keeps this probe
    separate from tests stubbing one ordinary cv command."""
    identity = _binary_identity()
    if identity is None:
        return None
    if _seen and _seen[0] == identity:
        return _seen[1]
    found = _probe()
    if found is _TIMED_OUT or found is None or _binary_identity() != identity:
        return None
    _seen[:] = [identity, found]
    return found


def _probe():
    try:
        p = subprocess.Popen([CV, "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    try:
        out, _ = p.communicate(timeout=_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return _TIMED_OUT
    return parse_version(out)


def parse_version(text):
    """`cv 0.13.0 (<build>)` -> (0, 13, 0); None if there is no x.y.z."""
    m = re.search(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def v2():
    """True when the installed cv speaks the 0.11+ grammar.

    An unknown version reads as the CURRENT grammar: with no cv every call fails
    the same way in either grammar, and a version helm cannot parse is newer
    than any it was written against."""
    v = version()
    return v is None or v >= V2


def drop_thinking():
    """The `cv prune` flag that flattens the oldest reasoning."""
    return DROP_THINKING if v2() else "--thinking"


def window(start, end):
    """The `cv show --range` value for messages [start, end)."""
    return "%d%s%d" % (start, ".." if v2() else "-", end)


def field(obj, snake):
    """`obj[snake]`, else the camelCase key a cv before 0.11 wrote for it."""
    if snake in obj:
        return obj[snake]
    head, *rest = snake.split("_")
    return obj.get(head + "".join(w.capitalize() for w in rest))


def block_type(block):
    """A `cv show --json` content block's tag: `type` since 0.11, `kind` before."""
    return block.get("type") or block.get("kind")
