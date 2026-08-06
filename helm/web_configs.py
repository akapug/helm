"""Configuration projections for :mod:`helm.web`."""
import os
import sys

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



# ── configs editor surface (the predecessor cockpit's /api/configs/* contracts, ported exactly) ──
# configs.py owns all behavior (recognition gate, backup→validate→atomic write,
# entry ops, restore); these handlers only adapt query/payload shapes.

def _api_configs_tree(qs):
    """The cwd tree of dirs holding project configs. ?root= narrows the scan;
    live session cwds are folded in (catalog rows' cwd)."""
    from . import configs
    cwds = []
    try:
        cwds = [r["cwd"] for r in _transcripts().get_catalog()["rows"] if r.get("cwd")]
    except Exception:
        pass  # no catalog on this machine — the scanned roots still answer
    out = configs.tree(_q1(qs, "root") or None, extra_cwds=cwds)
    return out, (400 if out.get("error") else 200)



def _api_configs_homes():
    from . import configs
    return configs.homes_configs()



def _api_configs_resolve(qs):
    """What a seat (home, cwd, harness) loads — the cascade with MCP
    winner/shadowed annotation. home= is a name or a path (the predecessor
    contract)."""
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
    with an error field + empty content — the predecessor contract the UI
    renders."""
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
    return out, (409 if out.get("code") == "conflict" else 400 if "error" in out else 200)



def _api_configs_entry_post(payload):
    """Structured entry op (add/remove an MCP server) — never hand-edits JSON."""
    from . import configs
    out = configs.entry_op(payload.get("action") or "", payload.get("path") or "",
                           payload.get("kind") or "", payload.get("name") or "",
                           payload.get("value"))
    return out, (400 if "error" in out else 200)



def _api_configs_restore_post(payload):
    """Restore a backup over its origin (validated + re-backed-up first)."""
    from . import configs
    out = configs.restore(payload.get("backup") or "")
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
    return {"ok": True, "path": ap, "trash": dest}, 200



# ── credential homes: the quota view's homes card (homes.py backend) ──

def _api_homes():
    """Every credential home (live, broken-alias, archived) — the predecessor
    cockpit's /api/homes shape."""
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
    decided). Found by a cross-family review after three rounds had already been
    fixed elsewhere, with exact repros: <tmp>/codex-parent/.claude answered codex,
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
