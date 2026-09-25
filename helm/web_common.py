"""Shared low-level bindings for :mod:`helm.web`."""
import os
import secrets
import sys
# EXPLICIT, not inherited. `time` reached this module only through the
# globals() copy from `helm.web` below, so it was bound when web.py had
# already imported it and ABSENT on a direct `from helm import web_common`.
# code_drift()'s fail-open then swallowed the NameError and answered "no
# drift" — the honest-staleness report silenced by an import-order accident,
# which is the exact half-live failure the function exists to announce. The
# binding is identical either way (web.py imports the same module object), so
# naming it here costs nothing and removes the ordering dependency.
import time

from . import localnames, web_ui_loader

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})


BIND = "127.0.0.1"

DEFAULT_PORT = 7433

PACKAGE_DIR = web_ui_loader.PACKAGE_DIR


# Per-process anti-CSRF bearer (the predecessor's MUTATION_TOKEN, ported): a hostile page
# can fire cross-origin POSTs at 127.0.0.1 but can never READ our UI to learn
# the token, so EVERY POST demands it (403 without). It reaches the browser by
# template substitution — _ui() replaces __HELM_TOKEN__ when serving the page.
# HELM_API_TOKEN (or a declared predecessor's spelling) pins it; else fresh
# each process.
MUTATION_TOKEN = (os.environ.get("HELM_API_TOKEN")
                  or localnames.legacy_env("API_TOKEN") or secrets.token_hex(16))



def _source_stamp():
    """Newest mtime across the SOURCE of the package this process loaded.

    Self-referential on purpose — it resolves from __file__, so it answers
    about the tree this server is actually running, never about whichever
    checkout the caller happens to be standing in.
    """
    newest = 0.0
    try:
        pkg = os.path.dirname(os.path.abspath(__file__))
        for root, _dirs, names in os.walk(pkg):
            if "__pycache__" in root:
                continue
            for n in names:
                if n.endswith(".py"):
                    newest = max(newest, os.path.getmtime(os.path.join(root, n)))
    except OSError:
        return 0.0
    return newest



# stamped ONCE, at import, which is exactly when this process froze its code.
_LOADED_STAMP = _source_stamp()



def code_drift():
    """None, or what the operator needs to know to trust this page.

    `helm web` has TWO deployment lifetimes in one process: the UI manifest
    and sections are assembled per REQUEST and are always current, while web.py
    is imported ONCE and is frozen for the process's life. Nothing reloads it.

    So a change touching both halves goes HALF-LIVE the moment it lands: the
    new template renders against the old server. That is worse than plain
    staleness — the surface looks current while the logic behind it is not,
    and there is no way to tell from the page. Measured 2026-07-25: the
    owner's console had been running 20h-old web.py behind current HTML,
    including an honest-presence fix whose whole point was a roster dot not
    overclaiming what it knows. Half of an honesty fix is not honest.

    Fails OPEN: any error reports no drift. A broken self-check must never
    take down the console it is trying to describe.
    """
    try:
        now = _source_stamp()
        if not now or not _LOADED_STAMP or now <= _LOADED_STAMP:
            return None
        return {"loaded": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(_LOADED_STAMP)),
                "on_disk": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now)),
                "stale_seconds": int(now - _LOADED_STAMP),
                "note": "this server is running code it loaded at start; "
                        "helm source has changed since. The page you are "
                        "reading is current, the server behind it is not — "
                        "restart helm web to load it."}
    except Exception:
        return None



def _q1(qs, key, default=None):
    """First value of a parse_qs list, else default."""
    v = qs.get(key)
    return v[0] if v else default



_DEFAULT_ROOM = []      # one-slot lazy cache; the derivation shells out to git



def default_room():
    """The room this console opens on when the caller names none.

    A LITERAL "main" was the old default, and it put the owner's console in
    the one room his fleet does not talk in — he had to be TOLD where a
    council was happening. The cure is the same derivation every seat uses
    (seats.derive_home_room), asked TWICE:

      1. the process cwd — an operator running `helm web` inside a project
         means that project;
      2. THIS FILE'S OWN DIRECTORY — because a systemd unit with no
         WorkingDirectory starts in $HOME, derives no project, and lands back
         on #main. Deriving from the code's location cannot drift with a unit
         file nobody remembers to edit, and the same hole was found in six
         timer units the same day (356a651) — a fix that depends on a
         hand-installed unit property WILL be found missing on the next
         surface out.

    Reserved #main remains the honest last resort: it is what a project-less
    helm genuinely has.

    SCOPE, decided deliberately: this drives the OWNER-FACING opening room
    only. The /api/* handlers keep their "main" default, because changing an
    API's default room is a CONTRACT change for every caller that omits
    ?room — it turned 9 endpoint tests red here, and those tests are not
    encoding a bug, they are encoding the contract. Whether the API default
    should follow this derivation is a real question and it deserves its own
    gate rather than a ride-along in a UI lane.
    """
    if not _DEFAULT_ROOM:
        room = None
        try:
            from . import seats
            status, room = seats.derive_home_room_typed(seats.safe_cwd())
            if status == seats.DERIVE_NONE:
                status, room = seats.derive_home_room_typed(PACKAGE_DIR)
            if status != seats.DERIVE_OK:
                room = None
        except Exception:
            room = None            # never let homing break the server
        _DEFAULT_ROOM.append(room or "main")
    return _DEFAULT_ROOM[0]



def _chat_profile():
    """Server-side signing identity for the owner's web posts: the server's
    HELM_CELL_PROFILE (the PRD's contract), else the owner's derived cell
    handle (seats.owner_name — no name ships in code) — never the agent
    default (the web panel IS the owner surface)."""
    from . import seats
    return os.environ.get("HELM_CELL_PROFILE") \
        or os.environ.get("MELD_AGENT_PROFILE") or seats.owner_name()

_COCKPIT_BEAT = [0.0]

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_COCKPIT_BEAT": (
        "the cockpit's last-beat stamp; the arms that assert a beat "
        "(test_fleetnotes) set it first"),
    "_DEFAULT_ROOM": (
        "a one-slot lazy cache; the arms that depend on the derivation "
        "(test_web) clear it first"),
}



def _cockpit_beat():
    """Stamp: a browser cockpit just polled. Cheap enough for the 2s path."""
    _COCKPIT_BEAT[0] = time.time()



# ── sessions surface: catalog / search / session / cmd / cwd / prune ──
# ABSORBED contracts from the predecessor's route handlers: same query
# params + response shapes, thin wrappers over transcripts.py (which owns the
# behavior: single-flight caches, cv seams, overrides). Same degrade law: an
# unexpected failure answers {"unavailable": true}, never a 500.

def _transcripts():
    from . import transcripts
    return transcripts



# ── multiplayer HTTP seam: the owner surface over the blind LocalRelay + TTL
# LocalPresence (multiplayer.py, unchanged). The relay stays blind — the state
# GET is a thin passthrough of opaque envelopes and NEVER decodes one; a client
# folds the demo LWW CRDT itself (helm.multiplayer_demo, which the CLI's
# `helm multiplayer set|status` runs).
#
# NO TAB READS THESE ANY MORE. The cave tab that did was retired on
# 2026-07-30: its board carried the fleet's notes to the owner (they live in
# fleetnotes.py now — durable, on the home tab), its presence pane carried the
# owner's own row (the roster carries it now), and its relay-log pane carried
# opaque envelope ids and byte counts, which is a protocol proof and not an
# owner surface. The ENDPOINTS stay: they are the adapter seam's HTTP face for
# a bridge or a script, tests/test_web_multiplayer.py covers them, and a
# remote relay still slots in with zero changes to this file
# (register_adapter + HELM_MULTIPLAYER_BACKEND). ──
MP_OWNER_CONNECTION = "cockpit"
del _web
