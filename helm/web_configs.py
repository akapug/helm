"""Configuration projections for :mod:`helm.web`."""
import os
import sys
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import json
import shutil
import time

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



def _api_configs():
    """The config-file list model: home/user scope + the project-scope cwd tree.
    Same graceful degrade as the store."""
    try:
        from . import configs
        out = {"homes": configs.homes_configs(), "tree": configs.tree()}
        json.dumps(out)
        return out
    except Exception:
        return {"unavailable": True}



def _api_config_injection(qs):
    """One seat's config identity + versioned injection-weight observations.

    This endpoint is opt-in from the roster popup.  It may read the injection
    ledger and transcript catalog; neither read belongs on /api/chat/roster's
    two-second presence path.
    """
    seat, session = _q1(qs, "seat"), _q1(qs, "session")
    try:
        from . import injection_config
        return injection_config.view(seat, session), 200
    except Exception:
        return injection_config.unknown_view(seat, session, "observation"), 200



def _api_inject_pack(qs):
    """WHAT HELM PUT IN THIS SEAT'S TURNS, entry by entry, for the Config pane.

    The sibling of `_api_config_injection` and deliberately not folded into it:
    that endpoint answers WHICH SEAT AM I LOOKING AT (config identity joined to
    weight samples), this one answers WHAT IS IN ITS CONTEXT AND WHAT CAN I
    CHANGE. Same opt-in posture and the same reason — it reads the whole fire
    ledger, which has no business on a presence poll.
    """
    try:
        from . import injectpack
        return injectpack.view(_q1(qs, "seat"), _q1(qs, "session")), 200
    except Exception as exc:
        return {"state": "unknown", "unavailable": ["pack"], "entries": [],
                "turns": [], "window": None, "session_total": None,
                "why": "the injection pack could not be read: %s" % exc}, 200


def _api_inject_act(payload):
    """One owner click on one entry — the store's own writer, never a file poke.

    THE REFUSALS ARE THE WRITERS' AND ARE PASSED THROUGH VERBATIM. A page that
    rewrites a refusal into its own words teaches the owner a vocabulary the
    CLI does not share, and the next person who reads `helm store` output has
    to translate. 400 is the writer declining; it is not a defect.
    """
    from . import injectpack
    out, err = injectpack.act(payload.get("action") or "",
                              payload.get("id") or "",
                              reason=payload.get("reason") or "",
                              owner=payload.get("owner"),
                              project=payload.get("project") or None)
    if err:
        return {"error": err, "code": "refused"}, 400
    return dict(out, ok=True), 200


def _api_configs_cascade(qs):
    """What a seat at ?cwd= loads — physics' resolved cascade. Optional
    ?harness=claude|codex|pi (default claude) and ?home=DIR (default the
    harness's default home). Returns (obj, status)."""
    from . import configs
    harness = (qs.get("harness") or ["claude"])[0]
    if harness not in ("claude", "codex", "pi"):
        return {"error": "harness wants claude|codex|pi, got %r" % harness}, 400
    cwd = (qs.get("cwd") or [None])[0]
    default_home = os.path.join(
        os.path.expanduser("~"), ".pi/agent" if harness == "pi" else ".codex" if harness == "codex" else ".claude")
    home_p = (qs.get("home") or [None])[0] or default_home
    return configs.resolve(home_p, cwd, harness), 200



# ── configs editor surface (the predecessor's /api/configs/* contracts, ported exactly) ──
# configs.py owns all behavior (recognition gate, backup→validate→atomic write,
# entry ops, restore); these handlers only adapt query/payload shapes.

def _invalidate_tree(out):
    """Drop what this write can actually have changed — and only that.

    TWO CACHES, TWO RATES. The BUILT payload carries the per-directory file
    rows, so every landed write drops it: without that the owner saves a config
    through this very API and the tree beside his editor keeps describing the
    file as it was. The WALK carries which DIRECTORIES hold configs, and a save
    over a file the tree already lists cannot change that set by definition —
    so a plain save leaves the walk alone. Re-walking the owner's cwd root once
    per save is the hang this lane exists to cure, and a door that did it would
    be re-introducing it one keystroke at a time.

    WHAT CAN CHANGE THE SET IS A CREATE, and the writer says so: every door
    here returns through `configs.write_file`, whose result carries `created`.
    A new CLAUDE.md puts a directory on the owner's surface that was not there
    a moment ago, and no probe of the old walk would ever find it.

    A REFUSED WRITE CHANGED NOTHING AND INVALIDATES NOTHING: re-walking on
    every rejected revision would let a client with a stale revision keep the
    cache permanently cold.
    """
    if not isinstance(out, dict) or "error" in out:
        return
    from . import configs
    if out.get("created"):
        configs.clear_tree_cache()      # the walk AND everything built on it
    else:
        configs.invalidate_built()


def _api_configs_tree(qs):
    """The cwd tree of dirs holding project configs. ?root= narrows the scan;
    live session cwds are folded in (the predecessor: catalog rows' cwd).

    ?rescan=1 is the tab's ↻ button: it walks the tree instead of answering
    from configs' short-TTL cache. Every other GET — tab open, reload, a second
    pane, the reload after a save — takes the cache, because a walk of the whole
    cwd root per request is what leaves the owner's tab sitting on "scanning
    config tree…".

    The live session cwds this handler folds in are NOT part of the cache key.
    They are a moving set (one row per running seat), so keying on them would
    have made this — the only production caller — miss on nearly every request
    and leave every miss behind in the cache. configs.tree applies them as
    per-directory probes on top of the cached walk instead.
    """
    from . import configs
    cwds = []
    try:
        cwds = [r["cwd"] for r in _transcripts().get_catalog()["rows"] if r.get("cwd")]
    except Exception:
        pass  # no catalog on this machine — the scanned roots still answer
    out = configs.tree(_q1(qs, "root") or None, extra_cwds=cwds,
                       rescan=_q1(qs, "rescan") in ("1", "true", "yes"))
    return out, (400 if out.get("error") else 200)



def _api_configs_homes():
    from . import configs
    return configs.homes_configs()



def _api_configs_resolve(qs):
    """What a seat (home, cwd, harness) loads — the cascade with MCP
    winner/shadowed annotation. home= is a name or a path (the predecessor's contract)."""
    from . import configs
    hp = _resolve_home_path(_q1(qs, "home") or "")
    if not hp:
        return {"error": "need home= (name or path, see /api/configs/homes)"}, 400
    harness = _q1(qs, "harness") or configs.harness_for(hp)
    # TWO WAYS THIS LINE REACHED physics_report's `raise ValueError`, on an
    # UNAUTHENTICATED GET: harness_for now answers None for a home it cannot
    # place (it used to guess "claude" for anything), and harness= is a raw
    # query param that was never validated at all — ?harness=bogus was already
    # an HTTP 500 before this lane touched anything. One screen closes both,
    # and it refuses in the shape configs.resolve already refuses with.
    if harness not in configs.HARNESSES:
        return {"error": "unknown harness %r for this home — pass harness= (%s)"
                         % (harness, ", ".join(configs.HARNESSES)),
                "code": "refused"}, 400
    return configs.resolve(hp, _q1(qs, "cwd") or None, harness), 200



def _api_configs_file(qs):
    """One recognized config file's content (+editability). Refusals answer 200
    with an error field + empty content — the predecessor's contract the UI renders."""
    from . import configs
    p = _q1(qs, "path")
    if not p:
        return {"error": "need path="}, 400
    return configs.read_file(p), 200



def _api_configs_backups():
    from . import configs
    return configs.list_backups()



def _api_configs_file_post(payload):
    """Save one config file against the revision the editor actually opened."""
    from . import configs
    if not isinstance(payload.get("revision"), str):
        return {"error": "revision is required; reload before saving", "code": "revision"}, 400
    out = configs.write_file(payload.get("path") or "", payload.get("content") or "",
                             expected_revision=payload["revision"])
    _invalidate_tree(out)
    return out, (409 if out.get("code") == "conflict" else 400 if "error" in out else 200)



def _api_configs_entry_post(payload):
    """Structured entry op (add/remove an MCP server) — never hand-edits JSON."""
    from . import configs
    out = configs.entry_op(payload.get("action") or "", payload.get("path") or "",
                           payload.get("kind") or "", payload.get("name") or "",
                           payload.get("value"))
    _invalidate_tree(out)
    return out, (400 if "error" in out else 200)



def _api_configs_restore_post(payload):
    """Restore a backup over its origin (validated + re-backed-up first)."""
    from . import configs
    out = configs.restore(payload.get("backup") or "")
    _invalidate_tree(out)
    return out, (400 if "error" in out else 200)



# ── skills: census read + the owner-requested enable/disable/delete surface ──
# disable = rename <skill> -> <skill>.disabled (reversible); delete = MOVE to
# the trash dir (archive-not-delete law: nothing is ever destroyed).

TRASH_DIR = os.path.join(os.path.expanduser("~"), ".cache", "helm", "skills-trash")

DISABLED_SUFFIX = ".disabled"



def _api_skills():
    """The census, dupes-flagged, with the real dir path each mutation needs."""
    try:
        from . import skills
        found, bad = skills.census()
        name_dupes, _content_dupes = skills.dupes(found)
        rows = []
        for s in found:
            disabled = s["name"].endswith(DISABLED_SUFFIX)
            group = name_dupes.get(s["name"]) or []
            rows.append({
                "name": s["name"][:-len(DISABLED_SUFFIX)] if disabled else s["name"],
                "disabled": disabled, "home": s["home"], "real": s["real"],
                "path": os.path.join(s["home"], s["name"]),
                "has_manifest": s["has_manifest"],
                "shadowed": len(group) > 1,
                "diverged": len({x["hash"] for x in group}) > 1,
            })
        rows.sort(key=lambda r: (r["name"], r["home"]))
        return {"skills": rows, "bad": [{"path": p, "why": w} for p, w in bad]}
    except Exception:
        return {"unavailable": True}



def _valid_skill_dir(payload):
    """(abspath, None) iff payload['path'] is a REAL skill dir the census knows
    (inside a known skill home) — the gate for every mutation. Else (None, err)."""
    from . import skills
    p = payload.get("path")
    if not isinstance(p, str) or not p.strip():
        return None, ({"error": 'payload wants {"path": "<real skill dir>"}'}, 400)
    ap = os.path.abspath(os.path.expanduser(p))
    found, _bad = skills.census()
    known = {os.path.abspath(os.path.join(s["home"], s["name"])) for s in found}
    if ap not in known or not os.path.isdir(ap):
        return None, ({"error": "refused: not a known skill dir "
                                "(census-validated): %s" % ap}, 400)
    return ap, None



def _api_skills_toggle(payload):
    ap, err = _valid_skill_dir(payload)
    if err:
        return err
    if ap.endswith(DISABLED_SUFFIX):
        new, state = ap[:-len(DISABLED_SUFFIX)], "enabled"
    else:
        new, state = ap + DISABLED_SUFFIX, "disabled"
    if os.path.exists(new):
        return {"error": "refused: %s already exists" % new}, 409
    os.rename(ap, new)
    # A skill directory is a row in the configs tree (its entry count is on
    # the owner's surface), and losing the last one takes the directory off it.
    from . import configs
    configs.clear_tree_cache()
    return {"ok": True, "path": ap, "new_path": new, "state": state}, 200



def _api_skills_delete(payload):
    ap, err = _valid_skill_dir(payload)
    if err:
        return err
    import shutil
    import time
    stamp = (time.strftime("%Y%m%dT%H%M%S", time.gmtime())
             + "-%09d" % (time.time_ns() % 1_000_000_000))
    dest = os.path.join(TRASH_DIR, "%s-%s" % (stamp, os.path.basename(ap)))
    os.makedirs(TRASH_DIR, exist_ok=True)
    shutil.move(ap, dest)
    from . import configs
    configs.clear_tree_cache()
    return {"ok": True, "path": ap, "trash": dest}, 200



# ── credential homes: the quota view's homes card (homes.py backend) ──

def _api_homes():
    """Every credential home (live, broken-alias, archived) — the predecessor's /api/homes shape."""
    try:
        from . import homes
        return homes.homes_list()
    except Exception:
        return {"unavailable": True}



def _api_homes_post(payload):
    """Home lifecycle: helm prepares/verifies/moves DIRECTORIES only — the human
    runs every login; token contents are never touched (CRED_AUTH_CANON)."""
    from . import homes
    act = payload.get("action")
    if act == "create":
        out = homes.home_create(payload.get("provider"), payload.get("email"))
    elif act == "verify":
        out = homes.home_verify(payload.get("name"), payload.get("provider") or None)
    elif act == "archive":
        out = homes.home_archive(payload.get("name"), payload.get("provider") or None)
    elif act == "unarchive":
        out = homes.home_unarchive(payload.get("name"))
    elif act == "migrate":
        out = homes.home_migrate(payload.get("name"), payload.get("provider") or None)
    else:
        out = {"error": "unknown action %r "
                        "(create | verify | archive | unarchive | migrate)" % (act,)}
    return out, (400 if "error" in out else 200)



def _resolve_home_path(name):
    """Home NAME or path → absolute home path (claude-homes, codex-homes, defaults)."""
    if not name:
        return None
    if name.startswith("/") or name.startswith("~"):
        p = os.path.expanduser(name)
        return p if os.path.isdir(p) else None
    HOME = os.path.expanduser("~")
    if name == "(default-claude)":
        return os.path.join(HOME, ".claude")
    if name == "(default-codex)":
        return os.path.join(HOME, ".codex")
    for root in (os.path.join(HOME, ".claude-homes"), os.path.join(HOME, ".codex-homes")):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            return p
    return None



def _api_physics(qs):
    """What a seat on this home would load — the homes card's physics button.

    THE FOURTH COPY OF THE SUBSTRING GUESS, and the one that mattered most:
    this line read `"codex" if "/codex" in hp or "codex-homes" in hp else
    "claude"` — the ORIGINAL bug, untouched, on a LIVE OWNER SURFACE
    (the assembled UI calls /api/physics with NO harness=, so the guess always
    decided). Found by a second read after three rounds had already been fixed
    elsewhere, with exact repros: <tmp>/codex-parent/.claude answered codex,
    <tmp>/.claude-homes/codex-account answered codex, <tmp>/.pi/agent
    answered claude — and pi could never be answered at all, since the
    expression had only two outcomes.

    Two copies of one predicate is how the second survives the first fix; FOUR
    copies is how a fix gets celebrated while the owner's own button keeps the
    bug. The cure is the same lookup every other caller now uses, and the same
    HARNESSES screen — ?harness=bogus reached physics_report's raise and
    became an HTTP 500 on an unauthenticated GET.
    """
    from . import configs, physics
    hp = _resolve_home_path(_q1(qs, "home") or "")
    if not hp:
        return {"error": "need home= (name or path)"}, 400
    harness = _q1(qs, "harness") or configs.harness_for(hp)
    if harness not in configs.HARNESSES:
        return {"error": "unknown harness %r for this home — pass harness= (%s)"
                         % (harness, ", ".join(configs.HARNESSES)),
                "code": "refused"}, 400
    return physics.physics_report(hp, _q1(qs, "cwd") or os.path.expanduser("~"),
                                  harness), 200



def _api_physics_diff(qs):
    """What differs between two homes' physics (cred axis when cwd is omitted).

    A DIFF NEEDS ONE HARNESS FOR TWO HOMES, so `or "claude"` was not a
    default, it was a silent claim that both homes are claude — and it
    answered for a pi/codex pair as confidently as for a matched one. The
    honest rule is that the harness is DERIVABLE only when both homes agree;
    a mismatched pair is a question the caller has to answer, not one this
    layer may invent. Explicit harness= is still honoured and now validated
    (it reached physics_diff unchecked before, the same 500 as above).
    """
    from . import configs, physics
    a = _resolve_home_path(_q1(qs, "a") or "")
    b = _resolve_home_path(_q1(qs, "b") or "")
    if not a or not b:
        return {"error": "need a= and b= (home names or paths)"}, 400
    harness = _q1(qs, "harness")
    if not harness:
        ha, hb = configs.harness_for(a), configs.harness_for(b)
        if ha and ha == hb:
            harness = ha
        else:
            return {"error": "cannot derive one harness for these two homes "
                             "(%s vs %s) — pass harness= (%s)"
                             % (ha or "unknown", hb or "unknown",
                                ", ".join(configs.HARNESSES)),
                    "code": "refused"}, 400
    if harness not in configs.HARNESSES:
        return {"error": "unknown harness %r — pass one of %s"
                         % (harness, ", ".join(configs.HARNESSES)),
                "code": "refused"}, 400
    return physics.physics_diff(a, b, harness,
                                cwd=_q1(qs, "cwd") or None), 200
del _web
