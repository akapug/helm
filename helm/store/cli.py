"""helm store — the CLI dispatch (cmd_store).

The `helm store <verb>` surface + its add-time guard constants. Moved verbatim
from the pre-split helm/store.py; the top of the one-way dep graph.
"""
import os
import shlex
import re
import sys

from .. import delim, freetext, pk
from ._common import (
    _slug, CERTAIN, BELIEF_CLAMP, STATUS_LIVE, STATUS_CANDIDATE,
    STATUS_PROVISIONAL, PRIOR_PREFIX,
    _coerce_conf, derive_class,
)
from .load import (typed_id, 
    candidates, load_all, counts, scope_census, _find, find_typed, _default_dir,
    entry_scope, scope_label,
    _parse_prior, _parse_lexicon, _parse_heuristic, _parse_reference,
)
from .resolve import resolve_prompt, pinned, route_cell as store_route_cell
from .write import (
    xrev_clear, confirm, reject, revise, write_prior, _lexicon_path, write_lexicon,
    write_heuristic, write_reference, apply_evidence, mark_superseded, retire,
    demote, pinned_stats, retag, regate, regloss, rescope, _kw_list, _KEYWORD_TYPES,
    guard_entry_keywords, record_mint_events, doctor, _AUTHORED_PROBES_MAX,
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
from ._common import _decode_lists

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
      >=3-word phrases get 1-2-word stems auto-added — but a stem the store's
      own statements use everywhere (corpus-common) is REFUSED: it would fire
      on ordinary chatter and displace a real rule from the cap-4 window.
      That stop-list is MEASURED, never pinned, so the verdict moves as the
      store grows: each refusal prints its statement-df + threshold, and
      `store doctor` re-checks stems already stored against today's corpus;
      keywords that already resolve to a live sibling refuse toward `keywords <id> --add`
      (--force-new or HELM_STORE_FORCE_NEW=1 overrides, recorded)
  keywords <id> [--add CSV] [--remove CSV] [--set CSV] [--type T]
                                              retrieval keys: no flags = print
                                              them (the sharpen loop's look step);
                                              mutations linted like add
  gates <id> [--add CSV] [--remove CSV] [--set CSV] [--type T]
                                              the rule's PRECONDITIONS (entry
                                              ids): they ride in the same
                                              whisper when the rule fires and
                                              share its probe vocabulary; every
                                              id must resolve; no flags = print
  gloss <id> [--set TEXT...] [--clear] [--type T]
                                              the SHORT line that FIRES in place
                                              of a long statement; --set takes
                                              the whole trailing argv; refused
                                              with the exact overage past
                                              LINE_CAP; no flags = print it
  rescope <id> <project|fleet|-> [--type T]    RECORD THE PROJECT THIS ENTRY IS
                                              ABOUT (task/2435): a registry
                                              project name, `fleet` for owner
                                              policy that applies everywhere,
                                              or `-` to clear. A seat is
                                              injected fleet entries plus its
                                              own project's. THIS IS THE ONLY
                                              WAY an adopted-root entry becomes
                                              project-scoped; clearing returns
                                              it to its root's default, which
                                              is the project root it sits under,
                                              else fleet unless a helm-global
                                              statement names a helm lane,
                                              train, task or row (a seat name
                                              scopes nothing)
  xrev-clear <id> --by <who> [--type T] [--force-new]
                                              candidate -> PROVISIONAL: a
                                              cross-family /x review cleared it
                                              (the reviewer attests; the verb
                                              never runs the review). Provisional
                                              FIRES with a [provisional] tag,
                                              awaiting owner ratify
  revise <id> <corrected statement...>        correct a LIVE entry IN PLACE:
                                              stages the correction ALONGSIDE
                                              the statement still being served,
                                              and `confirm` swaps it in under
                                              the SAME id. For when the claim is
                                              right and only a clause is wrong —
                                              `evidence` would move CONFIDENCE
                                              (a lie about a claim that holds)
                                              and `supersede` TOMBSTONES the id.
                                              It keeps SERVING while staged, so
                                              canon never goes dark while it is
                                              being corrected
  confirm <id> [--type T] [--edit <stmt...>] [--force-new]
                                              owner ratify -> live (candidate OR
                                              provisional), or INSTALL a staged
                                              `revise` on a live entry
  reject <id> [--type T] [why...]             reject a candidate/provisional —
                                              retired in place (file kept)
      --type on any: disambiguate when reviewable entries share an id across
      types (ambiguous bare id is refused — never ratify/retire the wrong entry)
  evidence <ts> <id> <delta> <reason...>      move a belief (logged + clamped);
                                              heuristic/reference/lexicon are
                                              certain-by-construction: receipt
                                              LOGGED in-entry, confidence stays 1
  supersede <ts> <old-id> <new-id> [reason]   TOMBSTONE old (file kept)
  retire <ts> <id> [why...]                   retire (file kept as the record)
  demote <id> [--undo] <reason...>            flip always->jit with a receipt (--undo = provenanced restore)
  events [--limit N]                          the mutation-receipt trail (_global/.state/events.jsonl)
  counts                                      per-root type inventory
  doctor [--fix]                              retrieval-field audit: ids with
                                              whitespace (--fix re-keys each to its
                                              kebab id and leaves a tombstone at the
                                              old one, or says why it holds),
                                              statement-shaped lexicon terms,
                                              space-separated keyword CSVs,
                                              field-shift (cascade) rows, zero-keyword
                                              live entries, more than 6 authored
                                              probes, stems the corpus has
                                              drifted out from under, and near-duplicate
                                              statements (counted once by the stem
                                              gate). --fix repairs the mechanical
                                              classes (receipted): every repair is
                                              validated and staged before any lands,
                                              so one refused row withholds all of
                                              them (rc 1, nothing written); the
                                              rename leg is the filesystem's
                                              boundary — a rename failure after the
                                              first lands a prefix, and the message
                                              names what landed, what did not, and
                                              any stage residue; an attested row is
                                              rewritten only when its seal verifies
                                              (an unreadable or malformed proof is
                                              unverified, never absent), otherwise
                                              left byte-identical and named; the
                                              judgment classes stay a report"""


# The READ verbs that answer "what applies HERE" — these infer the project from
# cwd when --project is absent. Writes are deliberately excluded: see cmd_store.
# doctor rides here: its default is a read, and its --fix lands on each entry's
# own path (nothing is homed by cwd), the same law as the lifecycle verbs.
_CWD_SCOPED_READS = ("resolve", "list", "get", "pinned", "counts", "doctor")
# Rows printed per doctor class before the "... and N more" tail — one class
# (stem_drift) is populous by design and would otherwise bury the four the
# reader can act on in one pass.
_DOCTOR_HEAD = 15
# Lifecycle verbs RESOLVE an existing id, so they take the same cwd project
# lens as the reads (#951): before this, `get` (cwd-scoped) resolved a project
# entry that `evidence`/`retire` (global lens) then answered "not found" about
# — same cwd, same id, two doors, two answers. Nothing is HOMED by cwd here:
# the write lands on the entry's own path, on whichever root already holds it.
# `add` stays out — MINTING is the verb where cwd would silently decide where
# knowledge lives, and that stays an explicit --project decision.
_CWD_SCOPED_LIFECYCLE = ("evidence", "supersede", "retire", "demote",
                         "keywords", "gates", "gloss", "confirm", "reject",
                         "revise", "rescope",
                         "xrev-clear")


_ADD_FLAGS = {
    "--source": ("source", "one"),
    "--rationale": ("rationale", "many"),
    "--candidate": ("candidate", "switch"),
    "--force-new": ("force_new", "switch"),
}


# EVERY VERB'S OWN GRAMMAR — FLAGS, POSITIONAL COUNT, AND WHETHER A FREE-TEXT
# TAIL FOLLOWS. Derived from each branch's own parsing, never from _USAGE
# prose, because prose drifts and a guard keyed to it refuses whatever the
# prose forgot.
#
# THIS REPLACED A NAME-ONLY TABLE AFTER TWO MEASURED FALSE REFUSALS, and the
# ruling (meld 1788967609) is that the parser must OWN validation and
# normalization while the branches consume only its structured output. A guard
# standing beside a parse it does not perform cannot know whether a token is a
# FLAG or a VALUE or PROSE, and each round of patching it only moved which of
# the three it got wrong:
#   `keywords <id> --set --force`  -> --force is a VALUE   (works on main)
#   `evidence <ts> <id> -0.10 --because` -> --because is PROSE (works on main)
# Both were refused by successive name-only cuts. A guard whose failure
# direction is refusing working commands is worse than the leak it closes.
#
#   flags: name -> "switch" (no value) | "one" (next token, dash-leading or
#          not) | "rest" (the whole remaining argv)
#   pos:   how many positional arguments precede any free text
#   tail:  False        no free text; any unknown --name is a flag error
#          "literal"    everything after the positionals is prose, dash-leading
#                       tokens included — the established contract of a verb
#                       that consumes an arbitrary REASON (evidence, retire)
#          "guarded"    prose, but an unknown --name REFUSES until a `--` is
#                       given; after `--` the remainder is literal
#
# TWO VERBS ARE DELIBERATELY ABSENT, both because they OWN an idiosyncratic
# grammar no uniform parser reproduces without a bespoke rule:
#
#   `add`   — `_add_args` already refuses unknown options and owns add's
#             unquoted pipe grammar.
#   `gloss` — its `--type` is read from ANYWHERE for its value, yet its tokens
#             are stripped from the `--set` payload ONLY when they are the
#             immediate pair after it. `gloss <id> --set x --type prior` gives
#             ctype "prior" AND text "x --type prior" — the same token both
#             consumed and kept. A uniform loop cannot express that: mine
#             consumed `--type` anywhere and SILENTLY DELETED payload text
#             (review, FIX/MEASURED, which also caught that my handler comment
#             claimed parity with main when it did not hold). Exempting it
#             preserves main byte for byte, which is worth more than
#             uniformity here: the defect this lane exists to close lives in
#             `revise`, not in `gloss`.
_GRAMMAR = {
    "list": {"flags": {"--type": "one", "--all": "switch",
                       "--candidates": "switch"}, "pos": 0, "tail": False},
    "get": {"flags": {}, "pos": 1, "tail": False},
    "xrev-clear": {"flags": {"--by": "one", "--type": "one",
                             "--force-new": "switch"}, "pos": 1,
                   "tail": False},
    # REVISE IS GUARDED AND THE OTHERS ARE NOT, and the line between them is
    # what the tail WRITES. A review superseded one clause of its own meld
    # ruling here: a revise tail becomes a CANON STATEMENT, so accepting
    # `revise <id> "S" --source SRC` as ordinary text leaves task/1933's
    # original corruption path reachable — visible garbage now rather than
    # silently stripped garbage, but still accepted into a rule that fires at
    # the fleet. A reason or a why is metadata about a decision already made;
    # a statement IS the decision.
    "revise": {"flags": {"--type": "one"}, "pos": 1, "tail": "guarded"},
    "confirm": {"flags": {"--type": "one", "--force-new": "switch",
                          "--edit": "rest"}, "pos": 1, "tail": False},
    "reject": {"flags": {"--type": "one"}, "pos": 1, "tail": "literal"},
    "resolve": {"flags": {}, "pos": 0, "tail": "literal"},
    "pinned": {"flags": {"--stats": "switch"}, "pos": 0, "tail": False},
    "evidence": {"flags": {}, "pos": 3, "tail": "literal"},
    "supersede": {"flags": {}, "pos": 3, "tail": "literal"},
    "keywords": {"flags": {"--add": "one", "--remove": "one", "--set": "one",
                           "--type": "one"}, "pos": 1, "tail": False},
    "gates": {"flags": {"--add": "one", "--remove": "one", "--set": "one",
                        "--type": "one"}, "pos": 1, "tail": False},
    "retire": {"flags": {}, "pos": 2, "tail": "literal"},
    "demote": {"flags": {"--undo": "switch"}, "pos": 1, "tail": "literal"},
    "events": {"flags": {"--limit": "one"}, "pos": 0, "tail": False},
    "counts": {"flags": {}, "pos": 0, "tail": False},
    "rescope": {"flags": {"--type": "one"}, "pos": 2, "tail": False},
    # doctor is a read with ONE switch; modelled so its argv goes through the
    # same parser as every other verb and a stray `--fix=x` or a doubled flag
    # refuses instead of being silently read off `rest`.
    "doctor": {"flags": {"--fix": "switch"}, "pos": 0, "tail": False},
}


class Args(object):
    """One verb's argv, already decided. Branches read THIS, never `rest`."""

    __slots__ = ("flags", "pos", "tail")

    def __init__(self, flags, pos, tail):
        self.flags = flags          # {name: value}; switches map to True
        self.pos = pos              # [positional, ...]
        self.tail = tail            # the free text, already joined

    def get(self, name, default=None):
        return self.flags.get(name, default)

    def has(self, name):
        return name in self.flags

    def at(self, i, default=""):
        return self.pos[i] if i < len(self.pos) else default


def _acting_actor():
    """The seat this process may NAME AS HAVING ACTED, or None.

    IT USES `resolve_identity`, NOT `acting_seat`, AND THE DIFFERENCE IS THE
    WHOLE POINT. `acting_seat` never returns None: when neither a declared
    name nor a roster binding answers, it MINTS one from session and cwd.
    helm/seats_identity.py names that state DERIVED and calls it "THE DANGEROUS
    ONE ... a STRANGER IDENTITY THAT PASSES EVERY CHECK", and `resolve_identity`
    exists precisely so a caller can tell it apart — "Any caller whose ACTION
    differs by provenance must use this instead, because the distinction it
    needs is invisible in the string". Its rule of thumb names this case:
    if being wrong about WHO would mark work as done for the wrong party,
    DERIVED must not be treated as an identity. A ratification record is
    exactly that.

    A resolver that mints on absence turns an anonymous revise — one that
    records by=null — into a record stamped with an invented agent name. That
    is a FALSE IDENTITY in a provenance field, and it is worse than a false
    REFUSAL, because nothing about it looks wrong.

    A DISAGREEMENT IS ALSO NOT AN IDENTITY. When this process's declared name
    and the roster's binding for its session both answer and CONTRADICT each
    other, there is no fact here to record, so this returns None rather than
    picking whichever comes first in the resolution order."""
    from ..home import session_id
    try:
        from .. import seats_identity as ident
        sid = session_id()
        if ident.identity_disagreement(sid):
            return None
        state, name = ident.resolve_identity(sid)
        if state in (ident.DECLARED, ident.ROSTERED):
            return name or None
        return None                    # DERIVED: a minted stranger, not an actor
    except Exception:      # noqa: BLE001 — an unreadable roster names nobody;
        return None        # it never invents an actor to fill the field


def parse_argv(cmd, rest):
    """(Args, err) for one store verb — the single door that decides what a
    token IS.

    THE RULES, in the order they resolve a token, each one paid for by a
    measured failure:

    1. A RECOGNIZED FLAG IS A FLAG WHEREVER IT APPEARS, and a valued one takes
       the NEXT TOKEN even if that token is dash-leading. This is store's own
       convention and `keywords <id> --set --force` depends on it.
    2. `--flag=value` is the attached spelling of the same thing, parsed here
       rather than passed through as an unrecognized token that the branch
       would then ignore in silence.
    3. `--` ends flag parsing where the verb HAS somewhere for literal text to
       go. The parser consumes it; branches never see it.
    4. Once a verb's positionals are satisfied and it has a free-text tail,
       an UNRECOGNIZED dash-leading token is PROSE, not an error. That is what
       makes `evidence <ts> <id> -0.10 --because` mean what it means on main.
    5. Otherwise an unrecognized dash-leading token is a FLAG THIS VERB DOES
       NOT TAKE, and it refuses. This is the original defect: `revise <id>
       "S" --source X` used to drop the flag and splice its orphaned VALUE
       into the stored statement, silently, and the corrupted rule then fired
       at the fleet.

    A duplicate flag and a valued flag with nothing after it both REFUSE
    rather than taking a guess about which one the operator meant."""
    spec = _GRAMMAR.get(cmd)
    if spec is None:                   # add, and any verb not yet modelled
        return None, None
    known, npos = spec["flags"], spec["pos"]
    tail_policy = spec["tail"]                    # False | "literal" | "guarded"
    has_tail = bool(tail_policy)
    flags, pos, words, seen = {}, [], [], set()
    escaped = False
    rest_target, rest_words = None, []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not escaped and tok == "--" and (npos or has_tail or rest_target):
            escaped = True
            i += 1
            continue
        name, eq, attached = tok.partition("=")
        shape = None if escaped else known.get(name)
        if shape is not None:
            # ONE CONSUMPTION PATH, and this is the whole point of the shape.
            # A prose-consuming flag used to hand off to a NESTED scanner that
            # re-implemented flag handling more weakly, so the two disagreed:
            # `confirm <id> --edit corrected --force-new=false` set force TRUE
            # there while the main path refuses `=` on a switch entirely — a
            # false-looking token silently becoming AUTHORIZATION
            # (FIX/MEASURED). A rest flag now just sets a TARGET and the same
            # loop keeps running, so duplicates, attached values and switch
            # rules are enforced identically wherever a flag appears.
            if name in seen:
                return None, ("%s may appear only once" % name)
            seen.add(name)
            if eq and not attached:
                return None, ("%s= needs a value after the equals" % name)
            if shape == "switch":
                if eq:
                    return None, ("%s takes no value" % name)
                flags[name] = True
                i += 1
                continue
            if shape == "rest":
                if eq:
                    return None, ("%s takes its value as the remaining "
                                  "arguments, not with =" % name)
                rest_target = name
                i += 1
                continue
            if eq:
                flags[name] = attached
                i += 1
                continue
            if i + 1 >= len(rest):
                return None, ("%s needs a value" % name)
            flags[name] = rest[i + 1]
            i += 2
            continue
        # NOT A FLAG THIS VERB TAKES.
        if rest_target is not None:
            # Inside a prose-consuming flag: an unrecognised dash token is the
            # operator's text, which is what `--edit` and `--set` are FOR.
            rest_words.append(tok)
            i += 1
            continue
        filled = has_tail and len(pos) >= npos
        takes_prose = filled and (tail_policy == "literal" or escaped)
        if not escaped and tok.startswith("--") and not takes_prose:
            return None, ("unknown flag %s" % tok)
        # A TAILLESS VERB HAS NO BUCKET TO DROP A TOKEN INTO — `filled` is
        # gated on `has_tail` so extras stay POSITIONAL and visible to the
        # branch, rather than vanishing into an `Args.tail` nobody reads
        # (`get foo bar` used to resolve "foo" and discard "bar").
        (words if filled else pos).append(tok)
        i += 1
    if rest_target is not None:
        flags[rest_target] = " ".join(rest_words)
    return Args(flags, pos, " ".join(words).strip()), None


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
    """store <list|get|add|resolve|pinned|keywords|gates|gloss|rescope|xrev-clear|confirm|reject|evidence|supersede|retire|demote|events|counts> — the ONE typed personal-knowledge store."""
    args = list(args)
    project = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 >= len(args):
            print("helm store: --project needs a name", file=sys.stderr)
            return 2
        project = args[i + 1]
        del args[i:i + 2]
    elif args and args[0] in _CWD_SCOPED_READS + _CWD_SCOPED_LIFECYCLE:
        # INFER THE PROJECT FROM cwd ON READS AND LIFECYCLE WRITES, and SAY SO.
        #
        # The hook path already does this: `helm inject --hook-json` reads the
        # cwd out of the hook JSON and derives the project, so a seat working in
        # a project DOES get that project's entries. The interactive CLI never
        # looked at os.getcwd(), so the same query typed by a human — or by an
        # agent checking its own work — returned NOTHING and made a working
        # feature look unimplemented.
        #
        # LIVE, during a production incident in a client project: a seat wrote
        # an incident runbook with `--project <name>`, could not resolve it
        # from that project's own cwd, concluded "project-scoped resolve is
        # unfinished", and moved the entry back to _global where it "provably
        # works". The entry was fine and project resolve was fine — the READ
        # PATH IT TESTED WITH could not see it. An inconsistency between the
        # path that runs and the path you debug with is worse than either being
        # broken, because it manufactures false architectural conclusions under
        # time pressure.
        #
        # READS AND ID-RESOLVING LIFECYCLE VERBS — the lens must be the same on
        # both doors or the write door answers "not found" about entries the
        # read door just displayed (#951, the same false-architectural-
        # conclusion machine as the incident above, now on the write side).
        # A MINT (`add`) still homes to _global without an explicit --project,
        # because where knowledge LIVES is a decision, and silently homing an
        # entry by whatever directory you happened to be in is the surprise
        # this fix exists to remove, not add.
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

    # ONE DOOR, BEFORE ANY VERB RUNS. Placed here rather than in each branch
    # because the branches are exactly what disagreed: `add` refused unknown
    # options, `revise` swallowed them into the entry's own text. A per-branch
    # guard is a promise every future verb has to remember to keep.
    # ONE DOOR, BEFORE ANY VERB RUNS, AND IT OWNS THE PARSE. Placed here
    # rather than in each branch because the branches are exactly what
    # disagreed: `add` refused unknown options while `revise` swallowed them
    # into the entry's own text. A per-branch guard is a promise every future
    # verb has to remember to keep; a parser the branches READ FROM cannot
    # drift from them, because there is nothing left to drift against.
    ns, perr = parse_argv(cmd, rest)
    if perr:
        spec = _GRAMMAR.get(cmd) or {}
        takes = sorted(spec.get("flags") or ())
        print("helm store %s: %s.%s" % (
            cmd, perr,
            (" It takes: " + ", ".join(takes)) if takes
            else " It takes no flags."), file=sys.stderr)
        return 2

    if cmd == "list":
        t = None
        if ns.has("--type"):
            t = ns.get("--type")
        if ns.has("--candidates"):
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
                      + (e.get("source") or "?") + " " + scope_label(e) + "]: "
                      + (e.get("statement") or "")[:100])
                print("      confirm: helm store confirm " + str(e["id"]) + dq
                      + "   reject: helm store reject " + str(e["id"]) + dq)
            return 0
        es = load_all(project=project, include_retired=ns.has("--all"),
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
                  + scope_label(e) + "]: " + (e.get("statement") or "")[:100])
        return 0

    if cmd == "get":
        eid = " ".join(ns.pos).strip()
        e = find_typed(eid, project=project)   # `lexicon:seam` names one type
        if not e:
            print("helm store: '" + eid + "' not found")
            # AMBIGUOUS STATEMENT-ID PREFIX: _find refuses to guess between
            # two dark rows sharing the typed token (its callers include write
            # verbs) — but a silent not-found here would hide the near-hits
            # the reader is one longer prefix away from. Teach the cure.
            from .load import _statement_id_matches, load_all as _la
            grouped = _statement_id_matches(
                eid, _la(project=project, include_retired=True))
            if len(grouped) > 1:
                print("  ambiguous — %d statement-shaped ids start with that "
                      "token; extend the prefix:" % len(grouped))
                for g in sorted(grouped):
                    any_e = next(iter(grouped[g].values()))
                    print("    " + str(any_e["id"])[:100])
            return 1
        # root is WHERE THE FILE SITS; scope is WHAT GOVERNS ITS INJECTION.
        # Two different questions, so both ride under their own names: they
        # differ for exactly the entries someone has rescoped, and a reader
        # given only the root cannot tell a classified entry from an
        # unclassified one.
        print(str(e["id"]) + " [" + e["type"] + " " + e["class"] + " "
              + ("%.2f" % e["confidence"]) + "/" + e["load_class"] + " "
              + e["status"] + " root=" + e["root"] + " scope=" + scope_label(e)
              + "]: " + (e.get("statement") or ""))
        for k in ("gloss", "keywords", "gates", "gate_probes", "domain", "url",
                  "supersedes", "replaced_by", "xrev_by", "xrev_ts",
                  "retired_why"):
            if e.get(k):
                print("  " + k + ": " + str(e[k]))
        # A STAGED REVISION IS INVISIBLE EVERYWHERE ELSE (T1: the grep
        # census found pending_revision in the parser and the writer and NO
        # read surface). So `revise` could stage a correction that nobody —
        # including the owner about to run `confirm` — could read before it
        # installed. A verb whose whole point is that the entry KEEPS SERVING
        # while a correction waits owes a way to see what is waiting; the
        # backend without the visible surface is worth nothing.
        #
        # Rendered explicitly rather than through the loop above: it is a
        # DICT, so `str(e[k])` would print a repr, and the STATEMENT is the
        # part a reader needs at full width.
        pending = e.get("pending_revision")
        if isinstance(pending, dict) and pending.get("statement"):
            print("  STAGED REVISION — `helm store confirm " + str(e["id"])
                  + "` installs this under the SAME id:")
            print("    " + str(pending["statement"]))
            print("    staged " + str(pending.get("ts") or "at an unrecorded time")
                  + (" by " + str(pending["by"]) if pending.get("by") else ""))
        print("  path: " + e["path"])
        return 0

    if cmd == "xrev-clear":
        force_new = ns.has("--force-new")
        by = ns.get("--by")
        ctype = ns.get("--type")
        eid = " ".join(ns.pos).strip()
        if not eid or not by:
            print("usage: helm store xrev-clear <id> --by <reviewer> "
                  "[--type T] [--force-new]\n"
                  "  Graduates a CANDIDATE to provisional, or ATTESTS the "
                  "staged revision on a LIVE entry — the riskier write, "
                  "because that entry is already firing fleet-wide. The "
                  "reviewer named must not be the actor who staged it.",
                  file=sys.stderr)
            return 2
        guard_notes = []
        e, err = xrev_clear(eid, pk.now_ts(), by, project=project, ctype=ctype,
                            force=force_new, guard_notes=guard_notes)
        if err:
            print("helm store xrev-clear: " + err, file=sys.stderr)
            return 1
        # THE VERB NOW HAS TWO SUBJECTS AND MUST NOT DESCRIBE THE WRONG ONE.
        # Attesting a live entry's staged revision changes no status and
        # starts no firing, so the graduation sentence would be false about
        # it in every clause — and a false success line is how an actor
        # believes it did the thing it did not do.
        if ((e or {}).get("pending_revision") or {}).get("xrev_by"):
            print("helm store: ATTESTED the staged revision on '" + eid
                  + "' (read by " + by + ") — the LIVE entry is unchanged and "
                  "still serving its old statement; land the revision with: "
                  "helm store confirm " + eid)
        else:
            print("helm store: XREV-CLEARED '" + eid + "' candidate -> "
                  "provisional (cleared by " + by + ") — now FIRES with a "
                  "[provisional] tag; owner ratifies via: helm store confirm "
                  + eid)
        for note in guard_notes:
            print(note)
        return 0

    if cmd == "revise":
        ctype = ns.get("--type")
        # THE ID IS THE FIRST POSITIONAL AND EVERYTHING AFTER IS THE STATEMENT.
        # Not `--edit`-style, because a revision without a statement is the
        # only shape this verb must never accept, so the statement is
        # positional and its absence is a usage error rather than a no-op.
        # The parser decided which tokens are which; this branch no longer
        # filters argv itself, which is what used to eat an unknown flag and
        # splice its orphaned value into the stored statement.
        eid = ns.at(0)
        stmt = ns.tail
        if not eid or not stmt:
            print("usage: helm store revise <id> <corrected statement...> "
                  "[--type T]\n"
                  "  Stages a correction to a LIVE entry. The entry STAYS LIVE "
                  "and keeps serving its current statement until you run "
                  "`helm store confirm <id>` — so canon is never dark while it "
                  "is being fixed. Same id, so ratification and retrieval "
                  "weight are preserved; use `supersede` only when the belief "
                  "itself changed, not its wording.", file=sys.stderr)
            return 2
        e, err = revise(eid, pk.now_ts(), stmt, project=project, ctype=ctype,
                        by=_acting_actor() or "")
        if err:
            print("helm store revise: " + err, file=sys.stderr)
            return 1
        print("helm store: REVISION STAGED on '" + eid + "' — the live entry "
              "is UNCHANGED and still firing. Land it with:\n"
              "  helm store confirm " + eid)
        return 0

    if cmd == "confirm":
        force_new = ns.has("--force-new")
        ctype = ns.get("--type")
        # `--edit` takes the whole remaining argv as the new statement, so the
        # parser hands it back joined; the id is what stood before it.
        new_stmt = str(ns.get("--edit") or "").strip() or None
        eid = " ".join(ns.pos).strip()
        if not eid:
            print("usage: helm store confirm <id> [--type T] "
                  "[--edit <new definition...>] [--force-new]", file=sys.stderr)
            return 2
        guard_notes = []
        who = _acting_actor()
        e, err = confirm(eid, pk.now_ts(), new_statement=new_stmt, project=project,
                         ctype=ctype, force=force_new, guard_notes=guard_notes,
                         by=who or "")
        if err:
            print("helm store confirm: " + err, file=sys.stderr)
            return 1
        # IT NAMES WHO ACTUALLY RATIFIED, because this literal read
        # "(owner-ratified)" for EVERY caller — there is no actor check in
        # confirm() and none here. An agent running this verb is told, and
        # tells the ledger's readers, that the owner ratified an edit he has
        # never seen. An unattributable confirm says so rather than borrowing
        # the owner's authority.
        print("helm store: CONFIRMED '" + eid + "' -> live ("
              + ("ratified by " + who if who else
                 "ratified by an UNIDENTIFIED caller — this process could not "
                 "resolve a seat, so the ledger records no actor")
              + ")"
              + (" (definition edited)" if new_stmt else "")
              + " - now fires in the JIT lane untagged")
        for note in guard_notes:
            print(note)
        return 0

    if cmd == "reject":
        ctype = ns.get("--type")
        if not ns.pos:
            print("usage: helm store reject <id> [--type T] [why...]", file=sys.stderr)
            return 2
        e, err = reject(ns.at(0), pk.now_ts(), why=ns.tail,
                        project=project, ctype=ctype)
        if err:
            print("helm store reject: " + err, file=sys.stderr)
            return 1
        print("helm store: REJECTED '" + ns.at(0) + "' -> retired "
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
                pflag = (" --project " + shlex.quote(project)) if project else ""
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
                pflag = (" --project " + shlex.quote(project)) if project else ""
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
            # DECODED, because a DIRECT parser call is a different door
            # from _parse_entry and only the latter decodes. Measured: the
            # merge below carried pending_revision through as its raw JSON
            # STRING, and _json1 then encoded it a SECOND time — the field
            # survived the redefine and came back unreadable, which reads to
            # `confirm` as no revision staged. Same corruption as dropping it,
            # arriving by the opposite route. _decode_lists is idempotent, so
            # calling it at this door costs nothing when the value is already
            # typed.
            prev = _decode_lists(_parse_lexicon(path) or {})
            # A RE-MINT OVER A RETIRED OR REJECTED TERM IS A FRESH LIFECYCLE,
            # the prior and heuristic doors' _STALE_ON_REMINT law. The merge
            # below starts from `prev` so no live field is ever forgotten —
            # and that same inheritance carried a rejected term's retired_ts
            # / retired_why AND its stale gloss into the new LIVE entry
            # (dispatch fff5cef99aec): a live row reading as retired, firing a
            # line written for the definition the owner rejected. The merge
            # law is for a LIVE redefine; a dead term is re-minted, not merged.
            if prev and prev.get("status") not in (STATUS_LIVE, STATUS_PROVISIONAL,
                                                    STATUS_CANDIDATE):
                for stale in _STALE_ON_REMINT:
                    prev.pop(stale, None)
            # MERGE BY CONSTRUCTION, NOT BY AN ALLOWLIST. This reconstruction
            # named every field it meant to carry, and has now silently
            # dropped one THREE TIMES: `gloss` (recorded two
            # comments down) and `pending_revision` (T1 — a legal
            # redefine CANCELLED a staged correction with no word to anyone,
            # which is the store-revise defect arriving through an unrelated
            # verb). A third instance of one defect is a shape problem, not a
            # missing line. Starting from `prev` inverts the default: a field
            # SURVIVES unless this block deliberately changes it, so a fifth
            # field added to lexicon entries inherits the merge instead of
            # waiting to be forgotten. Everything overwritten below is
            # overwritten ON PURPOSE and says why.
            e = dict(prev)
            e.update({"term": parts[0], "definition": parts[1],
                 "kind": kind or prev.get("kind") or "phrase",
                 "keywords": parts[3] if len(parts) > 3 and parts[3] else prev.get("keywords", ""),
                 "domain": parts[4] if len(parts) > 4 and parts[4] else prev.get("domain", ""),
                 "examples": prev.get("examples") or [],
                 # MERGE, per this block's own contract two comments up. The
                 # reconstruction listed every optional field EXCEPT the gloss,
                 # so a keyword-only redefine silently dropped the short line
                 # the term fires with (r3). A gloss is DERIVED FROM
                 # THE DEFINITION, so it survives only while the definition is
                 # unchanged — a redefine that changes the meaning invalidates
                 # it exactly as `confirm --edit` does, and it is dropped rather
                 # than guessed at.
                 # COMPARE IN THE STORED REPRESENTATION. write_lexicon
                 # whitespace-normalizes the definition before it ever reaches
                 # disk, so prev holds the normalized form — comparing it to
                 # RAW parts[1] read a retyped-but-identical definition as a
                 # semantic change and silently dropped the gloss (the
                 # gloss-round blocker). Normalize the candidate the same way.
                 "gloss": (prev.get("gloss") or "")
                          if str(prev.get("definition") or "")
                          == re.sub(r"\s+", " ", parts[1]).strip() else "",
                 "term_scope": scope, "status": status,
                 "source": source or prev.get("source")
                 or ("inferred" if candidate else "define"),
                 "updated_ts": ts, "hits": prev.get("hits") or "0"})
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
        text = ns.tail
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
        if not ns.has("--stats"):
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
        if len(ns.pos) < 3 or not ns.tail:
            print("usage: helm store evidence <ts> <id> <delta> <reason...>", file=sys.stderr)
            return 2
        e, err = apply_evidence(ns.at(1), ns.at(0), ns.at(2), ns.tail,
                                by="agent", project=project)
        if err and not e:
            print("helm store evidence: " + err, file=sys.stderr)
            return 1
        if err:
            # informational: a certain-prior's contradiction was LOGGED + surfaced
            # as drift (confidence intentionally not moved). Not an error.
            print("helm store: " + err)
        print("helm store: '" + ns.at(1) + "' confidence -> " + ("%.2f" % e["confidence"]))
        return 0

    if cmd == "supersede":
        if len(ns.pos) < 3:
            print("usage: helm store supersede <ts> <old-id> <new-id> [reason...]",
                  file=sys.stderr)
            return 2
        e, err = mark_superseded(ns.at(1), ns.at(2), ns.at(0), ns.tail,
                                 project=project)
        if err:
            print("helm store supersede: " + err, file=sys.stderr)
            return 1
        print("helm store: TOMBSTONED '" + ns.at(1) + "' (delete_eligible, replaced_by '"
              + ns.at(2) + "') - file KEPT until the sweep verifies no dangling ref")
        return 0

    if cmd == "keywords":
        # NO ARGS IS A READ, and that is deliberate: the sharpen loop is
        # resolve -> look -> sharpen -> RETEST, and making the "look" step cost a
        # separate verb is how people skip it and guess at the delta instead.
        if not ns.pos:
            print("usage: helm store keywords <id> [--add CSV] [--remove CSV] "
                  "[--set CSV] [--type T]   (no flags = print them)",
                  file=sys.stderr)
            return 2
        eid = ns.at(0)
        opt = dict(ns.flags)
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
        # READ THE ENTRY BEFORE THE WRITE, because the echo below has to be
        # true about the ENTRY and not merely about the input. retag's --add
        # branch drops a cell that is already present CASE-INSENSITIVELY
        # (`if k.lower() not in have`) silently and with no mark, and --remove
        # of an absent cell is equally a no-op. An echo listing what the caller
        # SUPPLIED guarantees its own contents by construction, so it can never
        # falsify "it landed" — which is this lane's own defect arriving from
        # the other side. `before` is what lets each listed cell say which of
        # the two things happened to it. _find is the same door the read branch
        # above uses; None simply means retag is about to report not-found.
        prior = _find(eid, project=project,
                      types=(ctype,) if ctype else _KEYWORD_TYPES)
        before = {k.lower() for k in _kw_list((prior or {}).get("keywords"))}
        e, err = retag(eid, pk.now_ts(), add=opt.get("--add"),
                       remove=opt.get("--remove"), replace=opt.get("--set"),
                       project=project, ctype=ctype)
        if err:
            print("helm store keywords: " + err, file=sys.stderr)
            return 1
        kws = _kw_list(e.get("keywords"))
        print("helm store: '%s' [%s] now carries %d keyword%s"
              % (e["id"], e["type"], len(kws), "" if len(kws) == 1 else "s"))
        # SHOW HOW THE INPUT SPLIT, NEVER THE MERGED FIELD RE-JOINED. The old
        # echo printed `", ".join(kws)`, and the join separator IS the input
        # separator — so six phrases the caller joined with SEMICOLONS, stored
        # as ONE unmatchable 40-word cell, rendered exactly like six good ones.
        # That is how task/1692 happened: the count rose, the count was TRUE,
        # every signal said the capture landed, and only the resolve-retest
        # dissented — read as a ranking problem for ten minutes.
        #
        # A LINT CANNOT COVER THIS AND WAS MEASURED AND REFUSED. _keyword_lint
        # (write.py:681) returns None unless the WHOLE field is one cell, so an
        # incremental write to a healthy entry is unexaminable by construction;
        # and a per-cell salad refusal would reject 1345 of 10045 live cells
        # (13.4%), most of them the short symptom phrasings capture asks for.
        # So this SHOWS the split and judges nothing. The `|` delimiters are
        # the payload: they mark where helm put the cell boundaries, which is
        # the one fact the caller cannot otherwise see.
        for flag in ("--set", "--add", "--remove"):
            cells = _kw_list(opt.get(flag))
            if not cells:
                continue
            print("  %s parsed as %d cell%s:"
                  % (flag, len(cells), "" if len(cells) == 1 else "s"))
            for k in cells:
                # WHAT THE CELL DID, not what it asked for: `=` and `?` mark
                # the inert cases, which are otherwise indistinguishable from
                # work because the caller's own list always contains them.
                if flag == "--add":
                    mark = "=" if k.lower() in before else "+"
                elif flag == "--remove":
                    mark = "-" if k.lower() in before else "?"
                else:
                    mark = "|"
                print("    %s %s" % (mark, k))
        # AND THE RESULT, one cell per line — the SAME render the read branch
        # twenty lines above prints, so the write echo and the read echo agree.
        # THIS IS THE ONLY LIST THAT CAN CONTRADICT THE CALLER: the supplied
        # list's contents are guaranteed by construction and so can never
        # falsify "it landed". The two are read together — a `=` or `?` says
        # the write was inert, and this says what the entry carries now.
        print("  entry now holds %d cell%s:"
              % (len(kws), "" if len(kws) == 1 else "s"))
        for k in kws:
            print("    | " + k)
        # THE LOOP IS NOT DONE AT THE WRITE. /learn's own law: a capture is
        # done at FIRES, never at stored — so the verb that widens is the right
        # place to say what remains, rather than leaving the author to
        # remember it. (_RETEST — the same constant `add` prints.)
        print(_RETEST)
        return 0

    if cmd == "gates":
        # THE SIBLING OF `keywords`, same shape on purpose: no flags reads,
        # flags mutate, every id named must resolve (task/1346).
        if not ns.pos:
            print("usage: helm store gates <id> [--add CSV] [--remove CSV] "
                  "[--set CSV] [--type T]   (no flags = print them)",
                  file=sys.stderr)
            return 2
        eid = ns.at(0)
        opt = dict(ns.flags)
        ctype = opt.get("--type")
        if not any(k in opt for k in ("--add", "--remove", "--set")):
            e = find_typed(eid, project=project,
                           types=(ctype,) if ctype else _KEYWORD_TYPES)
            if not e:
                print("helm store gates: '%s' not found" % eid, file=sys.stderr)
                return 1
            gs = _kw_list(e.get("gates"))
            print("%s [%s] — %d gate%s"
                  % (e["id"], e["type"], len(gs), "" if len(gs) == 1 else "s"))
            for g in gs:
                print("  " + g)
            return 0
        e, err = regate(eid, pk.now_ts(), add=opt.get("--add"),
                        remove=opt.get("--remove"), replace=opt.get("--set"),
                        project=project, ctype=ctype)
        if err:
            print("helm store gates: " + err, file=sys.stderr)
            return 1
        gs = _kw_list(e.get("gates"))
        print("helm store: '%s' [%s] now carries %d gate%s"
              % (e["id"], e["type"], len(gs), "" if len(gs) == 1 else "s"))
        if gs:
            print("  " + ", ".join(gs))
        print("  the gates ride in the same whisper as the rule and share its "
              "vocabulary — `helm inject --explain` with the rule's words shows "
              "both")
        return 0

    if cmd == "gloss":
        # THE SIBLING OF `gates` AND `keywords`: no flags reads, --set writes
        # the whole trailing argv as the text, --clear empties. The write goes
        # through the writers' own _commit, so the LINE_CAP refusal is the
        # oracle's, with the exact overage.
        # GLOSS PARSES ITS OWN ARGV, verbatim as main does, because it is
        # EXEMPT from `_GRAMMAR` (see the note there). `--type` is read from
        # anywhere for its value but only stripped from the payload when it is
        # the immediate pair after `--set`; routing that through the shared
        # loop consumed it anywhere and deleted operator text.
        if len(rest) < 1:
            print("usage: helm store gloss <id> [--set TEXT...] [--clear] "
                  "[--type T]   (no flags = print it)", file=sys.stderr)
            return 2
        eid = rest[0]
        # GUARD THE STRUCTURAL REGION ONLY, which is everything before `--set`
        # (or all of it when there is none). After `--set` the argv is the
        # operator's payload and main's placement contract owns it; before
        # `--set` a dash token can only be a flag, so an unknown one there is
        # the same silent discard this lane exists to close. MEASURED probe:
        # `gloss <id> --clear --bogus value` succeeded, cleared,
        # and dropped the unknown pair without a word. Exempting the verb from
        # the shared parser was right for its PAYLOAD and left this hole in
        # its STRUCTURE; both halves are the verb's own, so the guard is too.
        _cut = rest.index("--set") if "--set" in rest else len(rest)
        _j = 1
        while _j < _cut:
            _t = rest[_j]
            if not _t.startswith("--"):
                _j += 1
                continue
            if _t == "--clear":
                _j += 1
                continue
            if _t == "--type":
                if _j + 1 >= _cut:
                    print("helm store gloss: --type needs a type",
                          file=sys.stderr)
                    return 2
                _j += 2
                continue
            print("helm store gloss: unknown flag %s — REFUSING, because this "
                  "verb would have discarded it in silence. It takes: --set, "
                  "--clear, --type." % _t, file=sys.stderr)
            return 2
        ctype = rest[rest.index("--type") + 1] \
            if "--type" in rest and rest.index("--type") + 1 < len(rest) else None
        clear = "--clear" in rest
        text = None
        if "--set" in rest:
            tail = rest[rest.index("--set") + 1:]
            if ctype and tail[:2] == ["--type", ctype]:
                tail = tail[2:]
            text, _trc = freetext.tail("helm store", "keyword --set", tail,
                                       "the replacement text")
            if _trc is not None:
                return _trc
            text = text or ""
        if text is None and not clear:
            e = find_typed(eid, project=project,
                           types=(ctype,) if ctype else _KEYWORD_TYPES)
            if not e:
                print("helm store gloss: '%s' not found" % eid, file=sys.stderr)
                return 1
            gl = str(e.get("gloss") or "").strip()
            print("%s [%s] — %s" % (e["id"], e["type"],
                                    ("gloss: " + gl) if gl else "no gloss"))
            return 0
        e, err = regloss(eid, pk.now_ts(), text=text, clear=clear,
                         project=project, ctype=ctype)
        if err:
            print("helm store gloss: " + err, file=sys.stderr)
            return 1
        gl = str(e.get("gloss") or "").strip()
        if gl:
            from .. import inject
            print("helm store: '%s' [%s] gloss set — the line that fires is %d "
                  "of %d bytes" % (e["id"], e["type"],
                                   len(inject._entry_line_full(e).encode("utf-8")),
                                   inject.LINE_CAP))
            print("  " + gl)
        else:
            print("helm store: '%s' [%s] gloss cleared — the statement fires "
                  "again, truncated past LINE_CAP" % (e["id"], e["type"]))
        return 0

    if cmd == "retire":
        if len(ns.pos) < 2:
            print("usage: helm store retire <ts> <id> [why...]", file=sys.stderr)
            return 2
        e, err = retire(ns.at(1), ns.at(0), ns.tail, project=project)
        if err:
            print("helm store retire: " + err, file=sys.stderr)
            return 1
        print("helm store: RETIRED '" + ns.at(1) + "' (" + (e.get("retired_why") or "")
              + ") - file kept as the record")
        return 0

    if cmd == "demote":
        undo = ns.has("--undo")
        words = [ns.at(0)] + (ns.tail.split() if ns.tail else [])
        if len(words) < 2:
            print("usage: helm store demote <id> [--undo] <reason...>", file=sys.stderr)
            return 2
        reason, _drc = freetext.tail("helm store", "demote", words[1:],
                                     "a demote reason")
        if _drc is not None:
            return _drc
        e, err = demote(words[0], pk.now_ts(), reason or "",
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
        if ns.has("--limit"):
            n = ns.get("--limit")
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

    if cmd == "rescope":
        a, err = parse_argv(cmd, rest)
        if err:
            print("helm store rescope: " + err, file=sys.stderr)
            return 2
        if len(a.pos) < 2:
            print("usage: helm store rescope <id> <project|fleet|-> [--type T]",
                  file=sys.stderr)
            return 2
        owner = "" if a.at(1) == "-" else a.at(1)
        e, err = rescope(a.at(0), pk.now_ts(), owner, project=project,
                         ctype=a.get("--type"))
        if err:
            print("helm store rescope: " + err, file=sys.stderr)
            return 1
        if owner:
            # AS RECORDED, never as typed: a name is validated and stored
            # verbatim, so reporting the operand would hide any rewrite.
            print("helm store: %s is about %s"
                  % (e["id"], str(e.get("project") or "")))
        else:
            # CLEARING RETURNS THE ROW TO A DERIVATION, and the three roots
            # derive three different answers — a row under a project root
            # derives THAT PROJECT. So the line reads the derivation back
            # instead of promising fleet, which was wrong for exactly the rows
            # an operator is most likely to clear by mistake.
            owner_now, how = entry_scope(e)
            print("helm store: %s records no project — its root decides, and "
                  "that is %s (derived: %s)" % (e["id"], owner_now, how))
        return 0

    if cmd == "counts":
        c = counts(project=project)
        print("helm store counts:")
        for root in c:
            per = c[root]
            row = " ".join(k + "=" + str(per[k]) for k in sorted(per)) or "-"
            print("  %-11s %4d  %s" % (root, sum(per.values()), row))
        # THE CENSUS THE FENCE OWES (task/2435): what this project's lane
        # admits, and how many rows record no project at all — the backlog is a
        # number someone can work rather than a silence.
        cen = scope_census(project=project)
        print("  %-11s fleet=%d own=%d unscoped-derived-fleet=%d "
              "unscoped-derived-helm=%d foreign=%d"
              % ("scope", cen["fleet"], cen["own"], cen["unscoped_fleet"],
                 cen["unscoped_helm"], cen["foreign"]))
        derived = cen["unscoped_fleet"] + cen["unscoped_helm"]
        if derived:
            # THE DERIVED SPLIT IS THE WORKABLE NUMBER, and it is printed as two
            # numbers because the two halves are two different jobs: the fleet
            # half reaches every seat on a default, the helm half is held to one
            # project because a statement named an identity.
            print("  %d entr%s record no project — %d reach every seat by "
                  "default and %d are held to the helm project because "
                  "their statement names a helm artifact; "
                  "`helm store rescope <id> <project|fleet>` records "
                  "one" % (derived, "y" if derived == 1 else "ies",
                           cen["unscoped_fleet"], cen["unscoped_helm"]))
        return 0

    if cmd == "doctor":
        fix = ns.has("--fix")
        try:
            report, actions = doctor(project=project, fix=fix)
        except ValueError as exc:
            # a writer refused one staged repair: NOTHING landed (doctor's
            # whole-batch law) — say so where the operator is looking
            print("helm store doctor: " + str(exc), file=sys.stderr)
            return 1
        total = sum(len(v) for k, v in report.items()
                    if k not in ("floor_unmeasured", "census_unmeasured"))
        floor_line = None
        if report["floor_unmeasured"]:
            # the stem floor counts DISTINCT VERIFIED statements; with none,
            # an empty stem_drift class means "unmeasured", not "clean"
            fl = report["floor_unmeasured"][0]
            floor_line = ("stem floor unmeasured: %d attested carriers — no "
                          "stem is demoted to common until a sealed row "
                          "verifies (%d unsealed statement%s cast no vote)"
                          % (fl["attested"], fl["unsealed"],
                             "" if fl["unsealed"] == 1 else "s"))
        census_line = None
        if report["census_unmeasured"]:
            # the prompt leg's twin: stem_unfit ran on the shape leg only
            cu = report["census_unmeasured"][0]
            census_line = ("prompt census unmeasured: %d of %d turns "
                           "recorded — generated stems are checked for "
                           "shape only until it has seen %d"
                           % (cu["turns"], cu["min_turns"], cu["min_turns"]))
        if not total:
            print("helm store doctor: no retrieval-field defects found")
            for line in (floor_line, census_line):
                if line:
                    print(line)
            return 0
        for line in (floor_line, census_line):
            if line:
                print(line)
        # EVERY PRINTED CURE NAMES THE TYPED ROW THE DOCTOR MEASURED. The
        # classifier judges each typed row on its own (a prior and a reference
        # sharing one id are two rows), but a bare `keywords ID` resolves the
        # untyped WINNER (prior > heuristic > reference > lexicon), so an
        # untyped cure printed for a drifted REFERENCE would select its
        # same-id PRIOR and strip that prior's deliberately authored cell
        # while the reference stayed stale (task/1077 revival, F1).
        # `--type` is the house disambiguator (retag's typed find), and the
        # doctor's own project lens rides along, exactly as the refused-add
        # cures above carry it — the command must resolve what was measured.
        # The project is QUOTED at every printed cure: the registry admits a
        # project name with an internal space, and an unquoted `--project px
        # blue` re-parses as `--project px` plus an ignored positional, so
        # the printed line would edit a same-id sibling in the wrong project
        # (task/1077 revival, r4).
        pflag = (" --project " + shlex.quote(project)) if project else ""
        if report["space_ids"]:
            # task/2980: an id is ONE kebab token. Every row is listed with
            # what --fix does to it or why it will not, so the dry run IS the
            # migration plan a reader signs off on.
            rows = report["space_ids"]
            moving = [e for e in rows if e.get("rekey")]
            print("IDS WITH WHITESPACE (%d) — an id is one kebab token; %d "
                  "re-key%s to the id shown, leaving a tombstone at the old "
                  "id that points to it:%s"
                  % (len(rows), len(moving), "" if len(moving) == 1 else "s",
                     "" if fix else "  (--fix re-keys)"))
            for e in moving:
                print("  %-11s %s -> %s" % ("[" + e["type"] + "]",
                                            str(e["id"])[:70], e["rekey"]))
            for e in rows:
                if not e.get("rekey"):
                    print("  %-11s %s  HELD: %s"
                          % ("[" + e["type"] + "]", str(e["id"])[:60],
                             str(e.get("rekey_held") or "")[:160]))
        if report["statement_ids"]:
            print("STATEMENT-SHAPED LEXICON TERMS (%d) — a term is the phrase "
                  "a prompt contains, so it keeps its spaces; addressable by "
                  "its leading token (unique-prefix resolve), report only:"
                  % len(report["statement_ids"]))
            for e in report["statement_ids"]:
                print("  %-9s %s" % ("[" + e["type"] + "]", str(e["id"])[:90]))
        if report["cascade"]:
            print("FIELD-SHIFT (delimiter cascade) ROWS (%d) — keywords CSV "
                  "stranded in domain:%s" % (len(report["cascade"]),
                  "" if fix else "  (--fix repairs)"))
            for e in report["cascade"]:
                print("  %-9s %s" % ("[" + e["type"] + "]", str(e["id"])[:90]))
        if report["space_csv"]:
            print("SPACE-SEPARATED KEYWORD CSVs (%d) — one fused probe, can "
                  "never match:%s" % (len(report["space_csv"]),
                  "" if fix else "  (--fix comma-izes)"))
            for e in report["space_csv"]:
                print("  %-9s %s" % ("[" + e["type"] + "]", str(e["id"])[:70]))
        if report["flagged"]:
            print("PROSE-SHAPED KEYWORD FIELDS (%d) — human call, never "
                  "auto-split:" % len(report["flagged"]))
            for e in report["flagged"]:
                print("  %-9s %-50s kw=%r" % ("[" + e["type"] + "]",
                      str(e["id"])[:50], str(e.get("keywords"))[:50]))
        if report["zero_keys"]:
            print("ZERO-KEYWORD LIVE ENTRIES (%d) — resolve indexes only "
                  "id + keywords, so these never fire; key them:"
                  % len(report["zero_keys"]))
            for e in report["zero_keys"]:
                print("  helm store keywords \"%s\" --type %s "
                      "--add <symptom,csv>%s"
                      % (str(e["id"])[:70], e["type"], pflag))
        if report["stem_drift"]:
            # THE DRIFT LEG the mint-time refusal cannot cover: the stop-list
            # is measured from the store's own statements and MOVES, so a stem
            # that was discriminating at its mint can become chatter later.
            # Nothing is pinned (see corpus_common) — this is where an author
            # sees the verdict move, and it is a report because dropping a key
            # is retrieval judgment. Head-capped: this class is populous by
            # design (217 live rows on 2026-08-11) and must not bury the rest.
            rows = report["stem_drift"]
            print("CORPUS-DRIFTED STEMS (%d) — auto-stems the CURRENT corpus "
                  "would now refuse (the stop-list moved under them); "
                  "report only, drop what no longer discriminates:" % len(rows))
            for e in rows[:_DOCTOR_HEAD]:
                print("  helm store keywords \"%s\" --type %s --remove \"%s\"%s"
                      % (str(e["id"])[:70], e["type"],
                         ",".join(e["stem_drift"]), pflag))
            if len(rows) > _DOCTOR_HEAD:
                print("  ... and %d more" % (len(rows) - _DOCTOR_HEAD))
        if report["stem_unfit"]:
            # task/2978: generated stems the mint gate would no longer mint
            # (numeric, <= 2 characters, prompt-common). FIXABLE: the cells
            # are the guard's own appended tail (generated_stems), never an
            # author's, so --fix removes them with a receipt.
            rows = report["stem_unfit"]
            print("GENERATED STEMS THE MINT GATE WOULD NOT MINT (%d) — "
                  "numeric, <= 2 characters, or prompt-common; authored "
                  "cells are never touched:%s"
                  % (len(rows), "" if fix else "  (--fix removes)"))
            for e in rows[:_DOCTOR_HEAD]:
                print("  %-9s %-50s %s" % ("[" + e["type"] + "]",
                      str(e["id"])[:50], ",".join(e["stem_unfit"])[:70]))
            if len(rows) > _DOCTOR_HEAD:
                print("  ... and %d more" % (len(rows) - _DOCTOR_HEAD))
        if report["wide_keys"]:
            # trigger design R3 (heuristic store-keywords-few-short-rare):
            # keys are candidate generators, 3-6 short symptom probes. A
            # warning, never a repair: which keys stay is the author's call.
            rows = sorted(report["wide_keys"], key=lambda e: -e["wide_keys"])
            print("WIDE KEYS (%d) — more than %d authored probes: the entry "
                  "is two entries or a route; keep 3-6 short, rare symptom "
                  "probes (report only):" % (len(rows), _AUTHORED_PROBES_MAX))
            for e in rows[:_DOCTOR_HEAD]:
                print("  %-11s %-60s %d probes" % ("[" + e["type"] + "]",
                      str(e["id"])[:60], e["wide_keys"]))
            if len(rows) > _DOCTOR_HEAD:
                print("  ... and %d more" % (len(rows) - _DOCTOR_HEAD))
        if report.get("legacy_routes"):
            # trigger design lane 1: `notice:<kind>` is read as
            # `route:arrival.<kind>`; the re-key moves the cell to the one
            # spelling the ROUTES table uses. Report only: one command each.
            rows = report["legacy_routes"]
            print("LEGACY ROUTE CELLS (%d) — `notice:<kind>` reads as "
                  "`route:arrival.<kind>`; re-key each to the route spelling:"
                  % len(rows))
            for e in rows[:_DOCTOR_HEAD]:
                for c in e["legacy_routes"]:
                    print("  helm store keywords \"%s\" --type %s --remove "
                          "\"%s\" --add \"route:%s\"%s"
                          % (str(e["id"])[:70], e["type"], c,
                             store_route_cell(c), pflag))
            if len(rows) > _DOCTOR_HEAD:
                print("  ... and %d more" % (len(rows) - _DOCTOR_HEAD))
        if report["dup_statements"]:
            # the copies the corpus measure counts ONCE (_dup_folds): the
            # add door warned at this overlap that the row should have been a
            # supersede; this is the same cure, printed for the rows that
            # accumulated anyway. Report only — which copy is the keeper is
            # the author's call.
            now = pk.now_ts()
            print("NEAR-DUPLICATE STATEMENTS (%d) — counted as ONE contributor "
                  "by the stem gate's corpus measure (the add door's "
                  "supersede-not-duplicate overlap); report only, supersede "
                  "the copy:" % len(report["dup_statements"]))
            # THE OPERANDS ARE TYPED (typed_id — `reference:dup-b`), because
            # the supersede door resolves a bare slug to the untyped WINNER
            # and a prior outranks a reference: a bare suggestion about a
            # measured reference pair tombstoned the unrelated prior sharing
            # the slug. Lexicons have no supersede door (write_lexicon
            # serializes no tombstone), so the report says so instead of
            # printing a command that refuses.
            for e in report["dup_statements"][:_DOCTOR_HEAD]:
                kt, kid = e["dup_of"]
                kept = typed_id({"type": kt, "id": kid})
                copy = typed_id(e)
                if "lexicon" in (kt, e["type"]):
                    print("  %-9s %s duplicates %s — no supersede door for "
                          "a lexicon: redefine or retire the copy by hand"
                          % ("[" + e["type"] + "]", copy[:60], kept[:60]))
                    continue
                print("  helm store supersede %s %s %s <reason...>%s"
                      % (now, copy[:70], kept[:70], pflag))
            if len(report["dup_statements"]) > _DOCTOR_HEAD:
                print("  ... and %d more"
                      % (len(report["dup_statements"]) - _DOCTOR_HEAD))
        if report["attest_unverified"]:
            # A SEAL THE DOCTOR COULD NOT VERIFY IS NOT REWRITTEN: these rows
            # are listed in their defect class above and left byte-identical
            # by --fix — rewriting them would put a fresh last_updated and a
            # doctor receipt over a broken attestation. Verify first.
            print("ATTESTED ROWS LEFT UNTOUCHED (%d) — their seal did not "
                  "verify, so --fix leaves every byte as it found it; "
                  "check the seal, then re-run:"
                  % len(report["attest_unverified"]))
            for e in report["attest_unverified"]:
                print("  helm premise-check \"%s\"%s   # %s"
                      % (str(e["id"])[:60], pflag, e["attest_why"][:90]))
        if actions:
            print("FIXED %d row%s (receipts in each entry's evidence_log + "
                  "the events journal):" % (len(actions),
                  "" if len(actions) == 1 else "s"))
            for eid, klass, reason in actions:
                print("  %-10s %s — %s" % (klass, eid[:60], reason[:90]))
            print(_RETEST)
        return 0

    print("helm store: unknown verb '" + cmd + "'", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
