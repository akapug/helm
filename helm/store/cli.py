"""helm store — the CLI dispatch (cmd_store).

The `helm store <verb>` surface + its add-time guard constants. Moved verbatim
from the pre-split helm/store.py; the top of the one-way dep graph.
"""
import os
import re
import sys

from .. import delim, pk
from ._common import (
    _slug, CERTAIN, BELIEF_CLAMP, STATUS_LIVE, STATUS_CANDIDATE, PRIOR_PREFIX,
    _coerce_conf, derive_class,
)
from .load import (
    candidates, load_all, counts, _find, _default_dir,
    _parse_prior, _parse_lexicon, _parse_heuristic, _parse_reference,
)
from .resolve import resolve_prompt, pinned
from .write import (
    xrev_clear, confirm, reject, write_prior, _lexicon_path, write_lexicon,
    write_heuristic, write_reference, apply_evidence, mark_superseded, retire,
    demote, pinned_stats, retag, _kw_list, _KEYWORD_TYPES,
    guard_entry_keywords, record_mint_events,
)
from .index import _near_dup, near_dup_warning, _fmt

# THE one retest line (the resolve-test nudge) — a capture is done at FIRES,
# never at stored:. The constant lives in _common (premise/_capture.py emits
# it too); this module's emitters are `add` and `keywords --add`.
from ._common import RETEST as _RETEST


# add's <type> arg -> the store type the guard checks (premise IS a prior).
# Lexicon is exempt by design: redefinition is its only update lane (no
# supersede/evidence leg), so a re-define stays a legal in-place update.
_GUARD_TYPE = {"prior": "prior", "premise": "prior",
               "heuristic": "heuristic", "reference": "reference"}

# re-minting a retired/superseded id starts a FRESH lifecycle: the old
# record's tombstone metadata must not ride into a live entry ("live but
# replaced_by X" / "live but retired_ts Y" corrupts provenance — codex-seat
# review). Every parse-then-update add branch scrubs these; reject makes
# rejected -> re-add a routine agent lane, so the scrub is load-bearing.
from ._common import STALE_ON_REMINT as _STALE_ON_REMINT

# how many `|` fields each type's grammar accepts. A surplus field does not
# append — it CASCADES (statement tail -> keywords -> domain -> off the end),
# so it is refused rather than absorbed. See helm/delim.py for the parse and
# for the case this still cannot catch.
_ARITY = {
    "prior":     (5, "prior: <id> | <statement> [| conf [| keywords [| domain]]]"),
    "premise":   (4, "premise: <id> | <statement> [| keywords [| domain]]"),
    "lexicon":   (5, "lexicon: <term> | <definition> [| kind [| keywords [| domain]]]"),
    "heuristic": (4, "heuristic: <id> | <move> [| trigger-csv [| domain]]"),
    "reference": (5, "reference: <id> | <summary> [| url [| keywords [| domain]]]"),
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: helm store <verb> [args] [--project P]
  list [--type T] [--all] [--candidates]      entries (live; --all incl. retired; --candidates only)
  get <id>                                    one entry, full record
  resolve <text>                              JIT lookup — what fires for this prompt (or pipe on stdin)
  pinned [--stats]                            the always-on lane (--stats: budget walk + ledger made-it/starved)
  add <type> <id> | <statement> [| ...]       type: prior|premise|lexicon|heuristic|reference
      prior:     <id> | <statement> [| conf [| keywords [| domain]]]  (belief, default 0.6)
      premise:   <id> | <statement> [| keywords [| domain]]           (certain, conf 1.0)
      lexicon:   <term> | <definition> [| kind [| keywords [| domain]]]  (kind: ONE slug, e.g. phrase|coinage|bug-class)
      heuristic: <id> | <move> [| trigger-csv [| domain]]
      reference: <id> | <summary> [| url [| keywords [| domain]]]
      flags: [--source S] [--rationale <text...>] [--candidate] [--force-new]
             --candidate (prior|lexicon|heuristic|reference): safe inferred
             capture — writes a non-live candidate EXCLUDED from inject until
             confirmed (premise refused: certainty is the human-only lane)
      a LIVE same-id add is REFUSED (supersede/evidence instead, printed);
      a near-identical statement warns and proceeds (lexicon redefines freely
      EXCEPT --candidate over a live term — capture never de-canonizes)
      keywords are LINTED at add: empty / lone-word / comma-less >=4-word
      salad refuse (symptom phrases of 1-3 words are what resolve matches);
      >=3-word phrases get 1-2-word stems auto-added; keywords that already
      resolve to a live sibling refuse toward `keywords <id> --add`
      (--force-new or HELM_STORE_FORCE_NEW=1 overrides, recorded)
  keywords <id> [--add CSV] [--remove CSV] [--set CSV] [--type T]
                                              retrieval keys: no flags = print
                                              them (the widen loop's look step);
                                              mutations linted like add
  xrev-clear <id> --by <who> [--type T] [--force-new]
                                              candidate -> PROVISIONAL: a
                                              cross-family /x review cleared it
                                              (the reviewer attests; the verb
                                              never runs the review). Provisional
                                              FIRES with a [provisional] tag,
                                              awaiting owner ratify
  confirm <id> [--type T] [--edit <stmt...>] [--force-new]
                                              owner ratify -> live (candidate OR
                                              provisional)
  reject <id> [--type T] [why...]             reject a candidate/provisional —
                                              retired in place (file kept)
      --type on any: disambiguate when reviewable entries share an id across
      types (ambiguous bare id is refused — never ratify/retire the wrong entry)
  evidence <ts> <id> <delta> <reason...>      move a belief (logged + clamped)
  supersede <ts> <old-id> <new-id> [reason]   TOMBSTONE old (file kept)
  retire <ts> <id> [why...]                   retire (file kept as the record)
  demote <id> [--undo] <reason...>            flip always->jit with a receipt (--undo = provenanced restore)
  events [--limit N]                          the mutation-receipt trail (_global/.state/events.jsonl)
  counts                                      per-root type inventory"""


# The READ verbs that answer "what applies HERE" — these infer the project from
# cwd when --project is absent. Writes are deliberately excluded: see cmd_store.
_CWD_SCOPED_READS = ("resolve", "list", "get", "pinned", "counts")


_ADD_FLAGS = {
    "--source": ("source", "one"),
    "--rationale": ("rationale", "many"),
    "--candidate": ("candidate", "switch"),
    "--force-new": ("force_new", "switch"),
}


def _add_args(tail):
    """Parse add's flags while leaving its unquoted pipe grammar untouched.

    Like fleetnotes._set_args, known flags change the active field, duplicate
    and unknown flags refuse, and prose joins only after the complete argv has
    been validated. Rationale is the one multi-token option: it consumes until
    the next known add flag, then the scanner resumes there.
    """
    out = {"kept": [], "source": None, "rationale": "",
           "candidate": False, "force_new": False}
    seen = set()
    i = 0
    while i < len(tail):
        token = tail[i]
        spec = _ADD_FLAGS.get(token)
        if not spec:
            if token.startswith("--"):
                raise ValueError("unknown store add option %s" % token)
            out["kept"].append(token)
            i += 1
            continue
        if token in seen:
            raise ValueError("%s may appear only once" % token)
        seen.add(token)
        field, shape = spec
        if shape == "switch":
            out[field] = True
            i += 1
            continue
        if shape == "one":
            if i + 1 >= len(tail):
                raise ValueError("%s needs a value" % token)
            value = tail[i + 1]
            if value.startswith("--"):
                raise ValueError("%s needs a value before %s" % (token, value))
            if not value.strip():
                raise ValueError("%s needs a value" % token)
            out[field] = value
            i += 2
            continue
        words = []
        i += 1
        while i < len(tail) and tail[i] not in _ADD_FLAGS:
            if tail[i].startswith("--"):
                raise ValueError("unknown store add option %s" % tail[i])
            words.append(tail[i])
            i += 1
        value = " ".join(words).strip()
        if not value:
            before = " before %s" % tail[i] if i < len(tail) else ""
            raise ValueError("%s needs text%s" % (token, before))
        out[field] = value
    return out


def cmd_store(args):
    """store <list|get|add|resolve|pinned|keywords|xrev-clear|confirm|reject|evidence|supersede|retire|demote|events|counts> — the ONE typed personal-knowledge store."""
    args = list(args)
    project = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 >= len(args):
            print("helm store: --project needs a name", file=sys.stderr)
            return 2
        project = args[i + 1]
        del args[i:i + 2]
    elif args and args[0] in _CWD_SCOPED_READS:
        # INFER THE PROJECT FROM cwd ON READS, and SAY SO.
        #
        # The hook path already does this: `helm inject --hook-json` reads the
        # cwd out of the hook JSON and derives the project, so a seat working in
        # a project DOES get that project's entries. The interactive CLI never
        # looked at os.getcwd(), so the same query typed by a human — or by an
        # agent checking its own work — returned NOTHING and made a working
        # feature look unimplemented.
        #
        # LIVE 2026-07-28, during a production incident on a sibling project: a
        # seat wrote an incident runbook with `--project sibling-inc`, could not
        # resolve it from that project's cwd, concluded "project-scoped resolve
        # is unfinished",
        # and moved the entry back to _global where it "provably works". The
        # entry was fine and project resolve was fine — the READ PATH IT TESTED
        # WITH could not see it. An inconsistency between the path that runs and
        # the path you debug with is worse than either being broken, because it
        # manufactures false architectural conclusions under time pressure.
        #
        # READS ONLY. A write still homes to _global without an explicit
        # --project, because where knowledge LIVES is a decision, and silently
        # homing an entry by whatever directory you happened to be in is the
        # surprise this fix exists to remove, not add.
        try:
            from ..inject._ledger import project_for_cwd
            inferred = project_for_cwd(os.getcwd())
        except Exception:
            inferred = None            # fail open: global-only, never a crash
        if inferred:
            project = inferred
            print("helm store: scoping to project '%s' (from cwd; pass "
                  "--project to override)" % inferred, file=sys.stderr)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd, rest = args[0], args[1:]

    if cmd == "list":
        t = None
        if "--type" in rest:
            i = rest.index("--type")
            t = rest[i + 1] if i + 1 < len(rest) else None
        if "--candidates" in rest:
            cs = candidates(project=project, types=(t,) if t else None)
            if not cs:
                print("helm store: no candidates (safe-inferred capture is empty)")
                return 0
            cs.sort(key=lambda e: (e["type"], str(e["id"])))
            byslug = {}
            for e in cs:
                k = _slug(str(e["id"]))
                byslug[k] = byslug.get(k, 0) + 1
            print("helm store list --candidates (%d — excluded from inject until confirmed):"
                  % len(cs))
            for e in cs:
                # a slug shared across types needs the qualifier — the bare
                # printed command would resolve ambiguously and be refused
                dq = (" --type " + e["type"]) \
                    if byslug[_slug(str(e["id"]))] > 1 else ""
                print("  ? " + str(e["id"]) + " [" + e["type"] + " src="
                      + (e.get("source") or "?") + " " + e["scope"] + "]: "
                      + (e.get("statement") or "")[:100])
                print("      confirm: helm store confirm " + str(e["id"]) + dq
                      + "   reject: helm store reject " + str(e["id"]) + dq)
            return 0
        es = load_all(project=project, include_retired=("--all" in rest),
                      types=(t,) if t else None)
        if not es:
            print("helm store: empty. Add one: helm store add prior <id> | <statement>")
            return 0
        es.sort(key=lambda e: (e["type"], -e["confidence"], str(e["id"])))
        print("helm store (" + str(len(es)) + " entries):")
        for e in es:
            mark = "" if e["status"] == STATUS_LIVE else " [" + e["status"] + "]"
            print("  - " + str(e["id"]) + mark + " [" + e["type"] + " "
                  + ("%.2f" % e["confidence"]) + "/" + e["load_class"] + " "
                  + e["scope"] + "]: " + (e.get("statement") or "")[:100])
        return 0

    if cmd == "get":
        eid = " ".join(a for a in rest if not a.startswith("--")).strip()
        e = _find(eid, project=project)
        if not e:
            print("helm store: '" + eid + "' not found")
            return 1
        print(str(e["id"]) + " [" + e["type"] + " " + e["class"] + " "
              + ("%.2f" % e["confidence"]) + "/" + e["load_class"] + " "
              + e["status"] + " " + e["root"] + "]: " + (e.get("statement") or ""))
        for k in ("keywords", "domain", "url", "supersedes", "replaced_by",
                  "xrev_by", "xrev_ts", "retired_why"):
            if e.get(k):
                print("  " + k + ": " + str(e[k]))
        print("  path: " + e["path"])
        return 0

    if cmd == "xrev-clear":
        by = None
        ctype = None
        force_new = "--force-new" in rest
        rest = [a for a in rest if a != "--force-new"]
        if "--by" in rest:
            i = rest.index("--by")
            if i + 1 >= len(rest):
                print("helm store xrev-clear: --by needs a reviewer", file=sys.stderr)
                return 2
            by = rest[i + 1]
            del rest[i:i + 2]
        if "--type" in rest:
            i = rest.index("--type")
            if i + 1 >= len(rest):
                print("helm store xrev-clear: --type needs a type", file=sys.stderr)
                return 2
            ctype = rest[i + 1]
            del rest[i:i + 2]
        eid = " ".join(a for a in rest if not a.startswith("--")).strip()
        if not eid or not by:
            print("usage: helm store xrev-clear <id> --by <reviewer> [--type T] "
                  "[--force-new]", file=sys.stderr)
            return 2
        guard_notes = []
        e, err = xrev_clear(eid, pk.now_ts(), by, project=project, ctype=ctype,
                            force=force_new, guard_notes=guard_notes)
        if err:
            print("helm store xrev-clear: " + err, file=sys.stderr)
            return 1
        print("helm store: XREV-CLEARED '" + eid + "' candidate -> provisional "
              "(cleared by " + by + ") — now FIRES with a [provisional] tag; "
              "owner ratifies via: helm store confirm " + eid)
        for note in guard_notes:
            print(note)
        return 0

    if cmd == "confirm":
        ctype = None
        force_new = "--force-new" in rest
        rest = [a for a in rest if a != "--force-new"]
        if "--type" in rest:
            i = rest.index("--type")
            if i + 1 >= len(rest):
                print("helm store confirm: --type needs a type", file=sys.stderr)
                return 2
            ctype = rest[i + 1]
            del rest[i:i + 2]
        new_stmt = None
        if "--edit" in rest:
            i = rest.index("--edit")
            eid = " ".join(a for a in rest[:i] if not a.startswith("--")).strip()
            new_stmt = " ".join(rest[i + 1:]).strip() or None
        else:
            eid = " ".join(a for a in rest if not a.startswith("--")).strip()
        if not eid:
            print("usage: helm store confirm <id> [--type T] "
                  "[--edit <new definition...>] [--force-new]", file=sys.stderr)
            return 2
        guard_notes = []
        e, err = confirm(eid, pk.now_ts(), new_statement=new_stmt, project=project,
                         ctype=ctype, force=force_new, guard_notes=guard_notes)
        if err:
            print("helm store confirm: " + err, file=sys.stderr)
            return 1
        print("helm store: CONFIRMED '" + eid + "' -> live (owner-ratified)"
              + (" (definition edited)" if new_stmt else "")
              + " - now fires in the JIT lane untagged")
        for note in guard_notes:
            print(note)
        return 0

    if cmd == "reject":
        ctype = None
        if "--type" in rest:
            i = rest.index("--type")
            if i + 1 >= len(rest):
                print("helm store reject: --type needs a type", file=sys.stderr)
                return 2
            ctype = rest[i + 1]
            del rest[i:i + 2]
        if not rest:
            print("usage: helm store reject <id> [--type T] [why...]", file=sys.stderr)
            return 2
        e, err = reject(rest[0], pk.now_ts(), why=" ".join(rest[1:]).strip(),
                        project=project, ctype=ctype)
        if err:
            print("helm store reject: " + err, file=sys.stderr)
            return 1
        print("helm store: REJECTED '" + rest[0] + "' -> retired "
              "(file kept as the record)")
        return 0

    if cmd == "add":
        if len(rest) < 2:
            print(_USAGE, file=sys.stderr)
            return 2
        etype = rest[0]
        if etype not in ("prior", "premise", "lexicon", "heuristic", "reference"):
            print("helm store: unknown type '" + etype
                  + "' (prior|premise|lexicon|heuristic|reference)", file=sys.stderr)
            return 2
        try:
            opts = _add_args(rest[1:])
        except ValueError as e:
            print("helm store add: " + str(e), file=sys.stderr)
            return 2
        kept = opts["kept"]
        source = opts["source"]
        rationale = opts["rationale"]
        candidate = opts["candidate"]
        force_new = opts["force_new"]
        if candidate and etype == "premise":
            # an inference may not claim certainty even in escrow — confirm
            # ratifies the CAPTURE, it must not be the door to an auto-1.0
            print("helm store add: a premise (certainty 1.0) cannot be born a "
                  "candidate — the certainty rail is human-only. Capture the "
                  "inference as a belief:", file=sys.stderr)
            print("  helm store add prior <id> | <statement> [| conf] --candidate",
                  file=sys.stderr)
            return 2
        arity, grammar = _ARITY.get(etype, (5, _USAGE))
        parts, refused = delim.split(" ".join(kept), arity, grammar)
        if refused:
            print("helm store add: " + refused, file=sys.stderr)
            return 2
        if len(parts) < 2 or not parts[0] or not parts[1]:
            print(_USAGE, file=sys.stderr)
            return 2
        ts = pk.now_ts()

        # supersede-not-duplicate guard: a live same-id NEVER silently
        # overwrites (hard refuse, exact follow-up commands); a near-identical
        # statement under another id warns loudly and proceeds.
        gt = _GUARD_TYPE.get(etype)
        if gt:
            cur = _find(parts[0], project=project, types=(gt,))
            if cur and cur.get("status") == STATUS_LIVE:
                print("helm store add: '%s' is already LIVE [%s %s] — refusing to "
                      "overwrite (supersede-not-duplicate law)"
                      % (parts[0], gt, cur["root"]), file=sys.stderr)
                # the printed commands must carry the SCOPE of the refused add —
                # without --project they resolve against global and can mutate
                # the wrong entry (codex-seat review)
                pflag = (" --project " + project) if project else ""
                if gt == "prior":
                    print("  update its confidence:  helm store evidence %s %s "
                          "<delta> <reason...>%s" % (ts, parts[0], pflag), file=sys.stderr)
                print("  or replace it:          helm store add %s <new-id> | <statement...>%s"
                      % (etype, pflag), file=sys.stderr)
                print("                          helm store supersede %s %s <new-id> "
                      "[reason...]%s" % (ts, parts[0], pflag), file=sys.stderr)
                return 1
            dup, ov = _near_dup(gt, parts[0], parts[1], project=project)
            if dup:
                # ONE wording — near_dup_warning is shared with premise capture
                warn = near_dup_warning(dup, ov, ts, parts[0], project=project)
                print("helm store add: " + warn[0])
                for line in warn[1:]:
                    print(line)

        # lexicon is _GUARD_TYPE-exempt (redefinition is its one update lane) —
        # but a CANDIDATE add must never ride that exemption over a LIVE term:
        # writing status:candidate in place DE-canonizes the human-confirmed
        # definition (it drops out of inject until re-confirmed, and a later
        # reject retires it with the original knowledge already gone). Capture
        # may coin, never demote.
        if etype == "lexicon" and candidate:
            cur = _find(parts[0], project=project, types=("lexicon",))
            if cur and cur.get("status") == STATUS_LIVE:
                pflag = (" --project " + project) if project else ""
                print("helm store add: term '%s' is already LIVE [lexicon %s] — "
                      "refusing the candidate add (capture never de-canonizes a "
                      "confirmed term)" % (parts[0], cur["root"]), file=sys.stderr)
                print("  redefine it live:  helm store add lexicon %s | <definition...>%s"
                      % (parts[0], pflag), file=sys.stderr)
                print("  or capture the new sense under a distinct term id",
                      file=sys.stderr)
                return 1

        if etype in ("prior", "premise"):
            path = os.path.join(_default_dir("prior", project),
                                PRIOR_PREFIX + _slug(parts[0]) + ".md")
            e = _parse_prior(path) or {}
            if etype == "prior":
                # a non-number in the confidence slot is the delimiter cascade
                # arriving WITHIN arity — the old `except ValueError: 0.6`
                # swallowed the text whole and stored a belief nobody stated.
                # This is the one slot with a type, so it is the one place the
                # mis-split can be caught exactly rather than guessed at.
                conf = 0.6
                if len(parts) > 2 and parts[2]:
                    try:
                        conf = float(parts[2])
                    except ValueError:
                        print("helm store add: confidence is '%s', which is not "
                              "a number — refusing rather than storing 0.6 and "
                              "dropping the text.\n  grammar: %s\n  if that "
                              "belongs in the statement, escape the pipe before "
                              "it as `\\|`." % (delim.echo(parts[2]), grammar),
                              file=sys.stderr)
                        return 2
                kw = parts[3] if len(parts) > 3 else e.get("keywords", "")
                dom = parts[4] if len(parts) > 4 else e.get("domain", "")
                src = source or ("inferred" if candidate else "agent-inferred")
            else:
                conf = CERTAIN
                kw = parts[2] if len(parts) > 2 else e.get("keywords", "")
                dom = parts[3] if len(parts) > 3 else e.get("domain", "")
                src = source or "human"
            conf = _coerce_conf(conf)
            if etype == "prior":
                # the prior verb mints BELIEFS; certainty (1.0) is the premise
                # verb's human-only lane — an add-prior 0.999/1.0 clamps to 0.99
                conf = min(conf, BELIEF_CLAMP[1])
            # add-time findability gate (lint / duplicate probe / stem
            # decomposition) on the EFFECTIVE keywords — the re-mint fallback
            # included, because what LOADS is what resolves.
            kw, gerr, gnotes, gevents = guard_entry_keywords(
                "prior", parts[0], kw, project=project, force=force_new)
            if gerr:
                print("helm store add: " + gerr, file=sys.stderr)
                return 1
            # fresh lifecycle on re-mint (see _STALE_ON_REMINT); a prior also
            # sheds the old belief's audit trail — the new statement's
            # confidence is not evidence-continuous with the retired one's
            for stale in _STALE_ON_REMINT + ("evidence_log", "confidence_history"):
                e.pop(stale, None)
            e.update({"id": parts[0], "statement": parts[1],
                      "status": STATUS_CANDIDATE if candidate else STATUS_LIVE,
                      "stated_ts": ts, "last_updated": ts, "confidence": conf,
                      "keywords": kw, "domain": dom, "source": src})
            # SEED the audit trail at creation when a rationale is given, so a
            # belief is never confidence-without-a-receipt — even at birth.
            if rationale and not (e.get("evidence_log") or e.get("confidence_history")):
                seed_kind = "stated" if etype == "premise" else "assigned"
                e["evidence_log"] = [{"ts": ts, "type": seed_kind, "delta": round(conf, 4),
                                      "reason": rationale, "by": src}]
                e["confidence_history"] = [{"ts": ts, "value": round(conf, 4),
                                            "reason": rationale}]
            p = write_prior(e, path=path)
            record_mint_events(gevents)
            pk.event("store.add", parts[0],
                     etype + (" candidate — " if candidate else " — ") + parts[1])
            if candidate:
                print("helm store: CANDIDATE '" + parts[0] + "' [prior "
                      + ("%.2f" % conf) + " src=" + src + "] - " + parts[1])
                print("  excluded from inject until confirmed: helm store confirm " + parts[0])
            else:
                print("helm store: LIVE '" + parts[0] + "' [" + derive_class(conf) + " "
                      + ("%.2f" % conf) + "] - " + parts[1])
            print("  stored: " + p)
            for note in gnotes:
                print(note)
            if not candidate:  # a candidate cannot fire yet — retest at confirm
                print(_RETEST)
            return 0

        if etype == "lexicon":
            # the pipe contract is closed: a field the store will not keep is
            # REFUSED, never silently filed under the wrong key (the live
            # incident: a keywords CSV in field 3 died as kind:). The
            # over-arity half of this guard (`len(parts) > 5`) MOVED to the
            # shared delim check above, which now refuses a sixth field for
            # every type rather than only this one — leaving the disjunct here
            # would be a branch that can no longer be reached. What stays is
            # the part only lexicon can know: kind is a single taxonomy slug,
            # so a CSV in it is a WITHIN-arity cascade, caught by TYPE the way
            # a non-numeric confidence is caught for prior. Those two typed
            # fields are the only exact within-arity detections helm has.
            kind = parts[2] if len(parts) > 2 and parts[2] else ""
            if "," in kind:
                print("helm store add: lexicon is <term> | <definition> "
                      "[| kind [| keywords,csv [| domain]]] — kind is ONE "
                      "taxonomy slug (phrase|coinage|bug-class), field 4 "
                      "carries the keywords CSV", file=sys.stderr)
                return 2
            scope = ("project:" + project) if project else "global"
            status = STATUS_CANDIDATE if candidate else STATUS_LIVE
            # redefine MERGES, never strips: an absent optional field keeps the
            # existing entry's value — the coach landing verb's bare 2-field
            # sharpen must not destroy the symptom vocabulary this closed
            # contract exists to protect (the incident class, via a legal verb)
            path = _lexicon_path(parts[0], scope, _default_dir("lexicon", project))
            prev = _parse_lexicon(path) or {}
            e = {"term": parts[0], "definition": parts[1],
                 "kind": kind or prev.get("kind") or "phrase",
                 "keywords": parts[3] if len(parts) > 3 and parts[3] else prev.get("keywords", ""),
                 "domain": parts[4] if len(parts) > 4 and parts[4] else prev.get("domain", ""),
                 "examples": prev.get("examples") or [],
                 # MERGE, per this block's own contract two comments up. The
                 # reconstruction listed every optional field EXCEPT the gloss,
                 # so a keyword-only redefine silently dropped the short line
                 # the term fires with (codex round 3). A gloss is DERIVED FROM
                 # THE DEFINITION, so it survives only while the definition is
                 # unchanged — a redefine that changes the meaning invalidates
                 # it exactly as `confirm --edit` does, and it is dropped rather
                 # than guessed at.
                 # COMPARE IN THE STORED REPRESENTATION. write_lexicon
                 # whitespace-normalizes the definition before it ever reaches
                 # disk, so prev holds the normalized form — comparing it to
                 # RAW parts[1] read a retyped-but-identical definition as a
                 # semantic change and silently dropped the gloss (codex,
                 # gloss-round blocker). Normalize the candidate the same way.
                 "gloss": (prev.get("gloss") or "")
                          if str(prev.get("definition") or "")
                          == re.sub(r"\s+", " ", parts[1]).strip() else "",
                 "term_scope": scope, "status": status,
                 "source": source or prev.get("source")
                 or ("inferred" if candidate else "define"),
                 "updated_ts": ts, "hits": prev.get("hits") or "0"}
            # findability gate on the MERGED keywords (lexicon: exempt from
            # the empty case — the term is a probe by construction — and from
            # the duplicate probe, per its redefine/alias-fold law; provided
            # keywords are linted + stemmed like everyone else's)
            e["keywords"], gerr, gnotes, gevents = guard_entry_keywords(
                "lexicon", parts[0], e["keywords"], project=project,
                force=force_new)
            if gerr:
                print("helm store add: " + gerr, file=sys.stderr)
                return 1
            p = write_lexicon(e, path=path)
            record_mint_events(gevents)
            pk.event("store.add", parts[0],
                     ("lexicon candidate — " if candidate else "lexicon — ") + parts[1])
            if candidate:
                print("helm store: CANDIDATE '" + parts[0] + "' [lexicon " + scope
                      + " src=" + e["source"] + "] - " + parts[1])
                print("  excluded from inject until confirmed: helm store confirm " + parts[0])
            else:
                print("helm store: LIVE '" + parts[0] + "' [lexicon " + scope + "] - " + parts[1])
            print("  stored: " + p)
            for note in gnotes:
                print(note)
            if not candidate:
                print(_RETEST)
            return 0

        if etype == "heuristic":
            path = os.path.join(_default_dir("heuristic", project),
                                "heuristic-" + _slug(parts[0]) + ".md")
            e = _parse_heuristic(path) or {}
            for stale in _STALE_ON_REMINT:  # fresh lifecycle on re-mint
                e.pop(stale, None)
            trig = parts[2] if len(parts) > 2 and parts[2] else (e.get("trigger") or "")
            dom = parts[3] if len(parts) > 3 and parts[3] else e.get("domain", "")
            # findability gate on the trigger CSV — a heuristic's probes ARE
            # its trigger (write_heuristic serializes trigger-first)
            trig, gerr, gnotes, gevents = guard_entry_keywords(
                "heuristic", parts[0], trig, project=project, force=force_new)
            if gerr:
                print("helm store add: " + gerr, file=sys.stderr)
                return 1
            e.update({"id": parts[0], "move": parts[1], "statement": parts[1],
                      "trigger": trig, "domain": dom,
                      "status": STATUS_CANDIDATE if candidate else STATUS_LIVE,
                      "stated_ts": ts, "last_updated": ts,
                      "source": source or ("inferred" if candidate
                                           else (e.get("source") or "human"))})
            p = write_heuristic(e, path=path)
            record_mint_events(gevents)
            pk.event("store.add", parts[0],
                     ("heuristic candidate — " if candidate else "heuristic — ") + parts[1])
            if candidate:
                print("helm store: CANDIDATE '" + parts[0] + "' [heuristic src="
                      + e["source"] + "] - " + parts[1])
                print("  excluded from inject until confirmed: helm store confirm " + parts[0])
            else:
                print("helm store: LIVE '" + parts[0] + "' [heuristic conf=1 jit] - " + parts[1])
            if trig:
                print("  trigger: " + trig)
            print("  stored: " + p)
            for note in gnotes:
                print(note)
            if not candidate:
                print(_RETEST)
            return 0

        # reference: <id> | <summary> [| url [| keywords [| domain]]]
        path = os.path.join(_default_dir("reference", project),
                            "ref-" + _slug(parts[0]) + ".md")
        e = _parse_reference(path, os.path.basename(path)) or {}
        for stale in _STALE_ON_REMINT:  # fresh lifecycle on re-mint
            e.pop(stale, None)
        # findability gate on the effective keywords (re-mint fallback included)
        kw = parts[3] if len(parts) > 3 and parts[3] else e.get("keywords", "")
        kw, gerr, gnotes, gevents = guard_entry_keywords(
            "reference", parts[0], kw, project=project, force=force_new)
        if gerr:
            print("helm store add: " + gerr, file=sys.stderr)
            return 1
        e.update({"id": parts[0], "statement": parts[1], "summary": parts[1],
                  "url": parts[2] if len(parts) > 2 and parts[2] else e.get("url", ""),
                  "keywords": kw,
                  "domain": parts[4] if len(parts) > 4 and parts[4] else e.get("domain", ""),
                  "status": STATUS_CANDIDATE if candidate else STATUS_LIVE,
                  "stated_ts": e.get("stated_ts") or ts, "last_updated": ts,
                  "source": source or ("inferred" if candidate
                                       else (e.get("source") or "harvest"))})
        p = write_reference(e, path=path)
        record_mint_events(gevents)
        pk.event("store.add", parts[0],
                 ("reference candidate — " if candidate else "reference — ") + parts[1])
        if candidate:
            print("helm store: CANDIDATE '" + parts[0] + "' [reference src="
                  + e["source"] + "] - " + parts[1])
            print("  excluded from inject until confirmed: helm store confirm " + parts[0])
        else:
            print("helm store: LIVE '" + parts[0] + "' [reference jit] - " + parts[1])
        print("  stored: " + p)
        for note in gnotes:
            print(note)
        if not candidate:
            print(_RETEST)
        return 0

    if cmd == "resolve":
        # explicit args win; stdin is the hook path (may be an empty pipe —
        # ignoring args silently made an advertised verb a no-op). The --project
        # flag PAIR was already stripped above — filtering every word equal to
        # the project name here ate real query tokens (exactly the keyword most
        # likely to match that project's entries).
        text = " ".join(a for a in rest if not a.startswith("--"))
        from_args = bool(text)
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read()
        if not text.strip():
            print("usage: helm store resolve <text>   (or pipe prompt text on stdin)",
                  file=sys.stderr)
            return 2
        hits = resolve_prompt(text, project=project)
        for e in hits:
            print(_fmt(e))
        if not hits and from_args:
            # a human asked directly — explain the silence; the stdin/hook path
            # stays empty-on-no-match (salience law)
            print("helm store resolve: no JIT match (salience law — generic-only "
                  "matches never fire); try `helm store get <id>` for direct lookup")
        return 0

    if cmd == "pinned":
        if "--stats" not in rest:
            for e in pinned(project=project):
                print(_fmt(e))
            return 0
        s = pinned_stats(project=project)
        if not s["always"]:
            print("helm store pinned: no always-entries")
            return 0
        print("pinned lane (%d always, budget %dB, walk: confidence > recency > id):"
              % (len(s["always"]), s["budget"]))
        w = max(len(str(e["id"])) for e in s["always"])
        for e in s["always"]:
            eid = str(e["id"])
            win = ("made %d/%d" % (s["made"][eid], s["rows"])) if s["rows"] \
                else "no ledger rows"
            print("  %s %-*s  %s" % ("+" if eid in s["fits"] else "-", w, eid, win))
        # PRESENT TENSE FIRST. A lifetime-zero count answers "has this ever
        # landed", which is not the question an owner pinning a rule is asking;
        # they are asking "will this reach me". `fits` is inject's own budget
        # walk, so an always-entry absent from it CANNOT fire on the next turn
        # regardless of its history — and a 400-byte entry in a 1200-byte
        # budget with two ahead of it will never fit, no matter how long you
        # wait. Reporting only lifetime-zeros hid exactly that: 2 entries that
        # could not fire at all, while `starved` printed nothing.
        cannot = [str(e["id"]) for e in s["always"] if str(e["id"]) not in s["fits"]]
        if cannot:
            print("%d of %d always-entries CANNOT FIRE — the budget is spent "
                  "before the walk reaches them (%d/%d bytes used):"
                  % (len(cannot), len(s["always"]), s["used"], s["budget"]))
            print("  " + ", ".join(cannot))
            print("  shorten an entry ahead of them, or: helm store demote <id> <reason...>")
        if not s["rows"]:
            print("no fire-ledger rows with a pinned lane yet — historical "
                  "counts arrive as turns run")
            return 0
        print("ledger window: %d pinned-lane rows (%s .. %s)"
              % (s["rows"], s["first"], s["last"]))
        never = [str(e["id"]) for e in s["always"]
                 if s["made"][str(e["id"])] == 0 and str(e["id"]) in s["fits"]]
        if never:
            print("%d of %d fit TODAY but never made the budget over this window "
                  "(recently pinned, or recently unblocked):"
                  % (len(never), len(s["always"])))
            print("  " + ", ".join(never))
        return 0

    if cmd == "evidence":
        if len(rest) < 4:
            print("usage: helm store evidence <ts> <id> <delta> <reason...>", file=sys.stderr)
            return 2
        e, err = apply_evidence(rest[1], rest[0], rest[2], " ".join(rest[3:]),
                                by="agent", project=project)
        if err and not e:
            print("helm store evidence: " + err, file=sys.stderr)
            return 1
        if err:
            # informational: a certain-prior's contradiction was LOGGED + surfaced
            # as drift (confidence intentionally not moved). Not an error.
            print("helm store: " + err)
        print("helm store: '" + rest[1] + "' confidence -> " + ("%.2f" % e["confidence"]))
        return 0

    if cmd == "supersede":
        if len(rest) < 3:
            print("usage: helm store supersede <ts> <old-id> <new-id> [reason...]",
                  file=sys.stderr)
            return 2
        e, err = mark_superseded(rest[1], rest[2], rest[0], " ".join(rest[3:]),
                                 project=project)
        if err:
            print("helm store supersede: " + err, file=sys.stderr)
            return 1
        print("helm store: TOMBSTONED '" + rest[1] + "' (delete_eligible, replaced_by '"
              + rest[2] + "') - file KEPT until the sweep verifies no dangling ref")
        return 0

    if cmd == "keywords":
        # NO ARGS IS A READ, and that is deliberate: the widen loop is
        # resolve -> look -> widen -> RETEST, and making the "look" step cost a
        # separate verb is how people skip it and guess at the delta instead.
        if len(rest) < 1:
            print("usage: helm store keywords <id> [--add CSV] [--remove CSV] "
                  "[--set CSV] [--type T]   (no flags = print them)",
                  file=sys.stderr)
            return 2
        eid = rest[0]
        opt = {k: rest[rest.index(k) + 1] if rest.index(k) + 1 < len(rest)
               else "" for k in ("--add", "--remove", "--set", "--type")
               if k in rest}
        ctype = opt.get("--type")
        if not any(k in opt for k in ("--add", "--remove", "--set")):
            e = _find(eid, project=project,
                      types=(ctype,) if ctype else _KEYWORD_TYPES)
            if not e:
                print("helm store keywords: '%s' not found" % eid,
                      file=sys.stderr)
                return 1
            kws = _kw_list(e.get("keywords"))
            print("%s [%s] — %d keyword%s"
                  % (e["id"], e["type"], len(kws), "" if len(kws) == 1 else "s"))
            for k in kws:
                print("  " + k)
            return 0
        e, err = retag(eid, pk.now_ts(), add=opt.get("--add"),
                       remove=opt.get("--remove"), replace=opt.get("--set"),
                       project=project, ctype=ctype)
        if err:
            print("helm store keywords: " + err, file=sys.stderr)
            return 1
        kws = _kw_list(e.get("keywords"))
        print("helm store: '%s' [%s] now carries %d keyword%s"
              % (e["id"], e["type"], len(kws), "" if len(kws) == 1 else "s"))
        print("  " + ", ".join(kws))
        # THE LOOP IS NOT DONE AT THE WRITE. /learn's own law: a capture is
        # done at FIRES, never at stored — so the verb that widens is the right
        # place to say what remains, rather than leaving the author to
        # remember it. (_RETEST — the same constant `add` prints.)
        print(_RETEST)
        return 0

    if cmd == "retire":
        if len(rest) < 2:
            print("usage: helm store retire <ts> <id> [why...]", file=sys.stderr)
            return 2
        e, err = retire(rest[1], rest[0], " ".join(rest[2:]), project=project)
        if err:
            print("helm store retire: " + err, file=sys.stderr)
            return 1
        print("helm store: RETIRED '" + rest[1] + "' (" + (e.get("retired_why") or "")
              + ") - file kept as the record")
        return 0

    if cmd == "demote":
        undo = "--undo" in rest
        words = [a for a in rest if a != "--undo"]
        if len(words) < 2:
            print("usage: helm store demote <id> [--undo] <reason...>", file=sys.stderr)
            return 2
        e, err = demote(words[0], pk.now_ts(), " ".join(words[1:]),
                        project=project, undo=undo)
        if err:
            print("helm store demote: " + err, file=sys.stderr)
            return 1
        if undo:
            print("helm store: RESTORED '%s' -> %s (undemoted receipt appended "
                  "— one provenanced flip)" % (e["id"], e["load_class"]))
        else:
            print("helm store: DEMOTED '%s' always -> jit (receipt carries the "
                  "prior state; restore: helm store demote %s --undo <reason...>)"
                  % (e["id"], e["id"]))
        return 0

    if cmd == "events":
        n = "20"
        if "--limit" in rest:
            i = rest.index("--limit")
            n = rest[i + 1] if i + 1 < len(rest) else ""
        try:
            n = int(n)
        except ValueError:
            print("usage: helm store events [--limit N]", file=sys.stderr)
            return 2
        rows = pk.read_events(n)
        if not rows:
            print("helm store events: no mutation receipts yet (writers journal "
                  "to " + pk.events_path() + ")")
            return 0
        print("helm store events (last %d):" % len(rows))
        for r in rows:
            print("  %s  %-10s %-16s %s — %s"
                  % (r.get("ts") or "-", str(r.get("actor") or "-")[:10],
                     r.get("verb") or "-", r.get("target") or "-",
                     r.get("summary") or ""))
        return 0

    if cmd == "counts":
        c = counts(project=project)
        print("helm store counts:")
        for root in c:
            per = c[root]
            row = " ".join(k + "=" + str(per[k]) for k in sorted(per)) or "-"
            print("  %-11s %4d  %s" % (root, sum(per.values()), row))
        return 0

    print("helm store: unknown verb '" + cmd + "'", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
