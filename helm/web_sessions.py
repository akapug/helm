"""Session projections for :mod:`helm.web`."""
import sys

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



def _catalog_opensession(rows):
    """Catalog rows with OpenSession-aligned metadata names (cwd is metadata,
    never identity) — the predecessor's /api/catalog?format=opensession, ported."""
    return [{
        "harness": r["h"], "id": r["i"], "cwd": r.get("cwd") or r["c"], "title": r["t"],
        "git": {"branch": r["b"]} if r["b"] else {},
        "createdAt": r["cr"], "updatedAt": r["u"],
        "messageCount": r["m"], "sizeBytes": r["z"], "path": r["p"],
    } for r in rows]



def _api_catalog(qs):
    try:
        cat = _transcripts().get_catalog(refresh=_q1(qs, "refresh") == "1")
        if _q1(qs, "format") == "opensession":
            return {"sessions": _catalog_opensession(cat["rows"]),
                    "stats": cat["stats"], "scanned_at": cat["scanned_at"]}, 200
        return cat, 200
    except Exception:
        return {"unavailable": True}, 200



def _api_search(qs):
    q = _q1(qs, "q")
    if not q:
        return {"error": "need q="}, 400
    try:
        limit = int(_q1(qs, "limit", "40"))
    except ValueError:
        return {"error": "limit wants an integer"}, 400
    try:
        return _transcripts().deep_search(
            q, limit=limit, scope=_q1(qs, "scope") or None,
            include_synthetic=_q1(qs, "synthetic") == "1"), 200
    except Exception as e:
        kind = "exec" if isinstance(e, OSError) else "exception"
        issue = {"stage": "deep-search", "kind": kind,
                 "message": "transcript search unavailable (%s)" % type(e).__name__}
        return {"hits": [], "error": issue["message"], "unavailable": [issue]}, 200



def _api_session(qs):
    """One session's transcript page — DELIBERATELY UNCACHED.

    MEASURED: 1.76s warm, and every sibling read on this console holds its
    answer behind one memo or another, so a census that counts caches reads
    this route as the one that is missing one. It is not. Three reasons, and
    they are recorded here so the next sweep does not file it again:

    IT IS NOT POLLED. The card fetches this when the owner OPENS a session or
    pages back through one. There is no cadence to amortise a memo over — the
    burn-down and the room summary are cached because a timer pays their cost
    forever whether anybody is looking or not, and nothing here does.

    ITS KEY BARELY REPEATS. The answer is a function of (sid, before, limit,
    find, harness), and the motion this route actually sees is paging BACKWARD
    through a transcript — a different `before` every time. A memo keyed on
    that tuple would miss on exactly the requests that cost the most and hit
    only when the owner re-opened an identical page.

    AND THE SESSION IT READS IS USUALLY STILL BEING WRITTEN. The read that
    matters most is a seat the owner is watching work right now, and a cached
    page of a live transcript is a page that stops growing while its subject
    does not. Paying 1.76s for a deliberate click to see the true tail is the
    right trade; paying nothing to see a tail that is quietly minutes old is
    not — that is the freshness this console spends everywhere else.

    WHAT WOULD CHANGE THIS is a poller appearing on this route. If one ever
    does, the memo it needs is keyed on the transcript file's identity, the
    way `configs.list_backups` is keyed on its directory — never on a clock
    guessed against a fill nobody measured.
    """
    sid = _q1(qs, "sid")
    if not sid:
        return {"error": "need sid="}, 400
    try:
        before = int(_q1(qs, "before")) if _q1(qs, "before") else None
        limit = int(_q1(qs, "limit", "60"))
    except ValueError:
        return {"error": "before/limit want integers"}, 400
    try:
        return _transcripts().get_session(
            sid, before=before, limit=limit,
            find=_q1(qs, "find") or None,
            harness=_q1(qs, "harness") or None), 200
    except Exception:
        return {"unavailable": True}, 200



def _api_cmd(qs):
    sid = _q1(qs, "sid")
    if not sid:
        return {"error": "need sid="}, 400
    acct = _q1(qs, "account")
    if not acct:
        # the predecessor's degraded-account handling: with no provider (or none reporting)
        # an omitted account is well-defined — the machine's default account.
        try:
            degraded = not get_creds()
        except Exception:
            degraded = True
        if degraded:
            acct = "(default)"
        else:
            return {"error": "need account= (accounts exist — pick one, see /api/creds)"}, 400
    try:
        return _transcripts().make_cmd(acct, sid, _q1(qs, "model") or None), 200
    except Exception:
        return {"unavailable": True}, 200



def _api_cwd_post(payload):
    """Re-home a session's cwd (set) or --reset it (cwd: null). Metadata, never
    identity; claude additionally gets a project-slug symlink so --resume resolves."""
    out = _transcripts().cwd_override(payload)
    return out, (400 if "error" in out else 200)



def _api_prune_post(payload):
    """Resume studio: derive a NEW smaller still-resumable COPY via cv prune;
    the original is never mutated. dry: true previews without running."""
    out = _transcripts().prune_session(
        payload.get("sid") or "", preset=payload.get("preset", "lean"),
        dry=bool(payload.get("dry")), tokens=payload.get("tokens"))
    return out, (400 if "error" in out else 200)
del _web
