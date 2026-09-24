#!/usr/bin/env python3
"""Commit references in helm/**/*.py prose must RESOLVE — dead shas rot the
record.

THE DEFECT, measured on this repo 2026-08-01: the docstrings and comments in
helm/*.py cited history by sha, rebases rewrote that history, and a large
fraction of the citations went dead — `git cat-file` unresolvable or resolving
to an object no ref reaches. A dead citation is worse than none: a reader
greps it, finds nothing, and either distrusts the (true) prose around it or —
the shaguard incident class — "extends" it into an invented sha. Every dead
ref in that sweep was recoverable by CONTENT (subject-line grep, `git log -S`
on a distinctive changed line), which is why the repo convention is now sha
PLUS subject line: when the next rebase kills the sha, the subject makes the
truth findable again.

WHAT THIS FILE HOLDS:

  * every 7-40 lowercase-hex token found in a DOCSTRING or COMMENT under
    helm/ must be vouched by one of FOUR REPO-LOCAL authorities: git (the
    token resolves to a commit that is an ancestor of HEAD or the target of a
    live ref — the archive/rescue tag idiom names commits deliberately kept
    out of trunk ancestry); LEDGER_CITED (the token is a dispatch,
    land-request, or gate-receipt row id, listed with its reason and AUDITED
    against the live ledgers — see below); PATCH_IDS (a full 40-hex content
    identity recomputed from a recorded diff, never resolved by cat-file); or
    SKIP (a reasoned non-object runtime token no ledger can vouch for: harness
    session ids, quoted log lines, shaguard's fabricated exhibits);
  * digits-only tokens are structurally skipped: epochs, pids, byte counts
    and inode caps share the grammar and are not citations;
  * WHY THE VERDICT IS REPO-LOCAL, learned the long way. Two earlier rounds
    gave the guard live-ledger arms (`_is_ledger_row`, `_is_gate_receipt`)
    and retired 19 exemptions on their strength. A review's #88 P1 measured
    those arms answering False under `unittest discover` — red on every
    retired id, green in isolation — and tests/__init__.py names the law that
    makes this permanent: EVERY runner plants a scratch HELM_HOME before any
    test module imports (bug class `a-test-may-not-read-live-state-it-did-
    not-plant`), so an in-suite live-ledger read is blind EVERYWHERE, not
    just under one unlucky import order. A verdict on repo-committed text
    that varies by host, home, or suite order is the vacuous-pass class
    inverted. So the suite consults only what the repo carries; the live
    arms survive as the AUDIT engine;
  * THE AUDIT is where the ledgers actually vouch:
    `python3 tests/test_docstring_refs.py --audit`, run wherever the real
    ledgers live (the author's machine, at manifest-touch time). It pops the
    suite's planted scratch homes, reads the true coordination and gate
    ledgers, and is LOUD in every degraded state — an unreachable ledger, an
    EMPTY ledger (a sandboxed home reads as zero rows, which must never look
    like verification), and an entry the ledgers disown all print
    `unverifiable ledger citation` and exit nonzero. Registry admission is
    category-specific: LEDGER_CITED fails closed against those live ledgers;
    SKIP requires a non-object token and a reason because it silences forever;
    PATCH_IDS recomputes content identity from its recorded diff. The old flat
    list asked citation_vouched about all three and made SKIP unusable;
  * MUST-HIT control: a032ce7 ("orcaadopt: the session join could type into
    ANOTHER agent's pane — it now refuses") is cited in helm/orcaadopt.py and
    is live — the scanner finding and passing it proves the scan is not
    vacuously empty and the resolver is not vacuously red;
  * MUST-MISS control: this file's own fabricated token (below) must FAIL the
    resolver — proving the resolver can say no, i.e. a green run is a run in
    which every real citation individually resolved.
  * A RELEASE EXPORT CANNOT JUDGE THE CITATIONS, AND SAYS SO. The published
    tree is one commit with none of the development history, so the commits
    the prose cites are simply not in it. When the MUST-HIT commit itself is
    absent from the repository (tests/_release.py, history_gap), a citation
    whose commit is ABSENT is excused and the test says how many; a citation
    whose commit is present but unreachable still fails, and everything else
    here still runs. Wherever the MUST-HIT commit is present — every
    development checkout — nothing is excused and every check runs as before.

Scanner scope is helm/**/*.py prose only (comments via tokenize, docstrings
via ast). String literals that merely contain hex (the zero-sha constant, the
OAuth client id, a quoted hex alphabet) are code, not citations, and stay out of
scope. The suite reads git via subprocess against this checkout and nothing
live: the ledgers are read only by the explicit --audit entrypoint and by
fixtures the tests themselves plant; no network.
"""
import ast
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, eventledger, gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM = os.path.join(ROOT, "helm")
HEX = re.compile(r"\b[0-9a-f]{7,40}\b")

# MUST-HIT: known-live citation the scanner is required to find in helm/ and
# the resolver is required to pass. Cited (5 sites) in helm/orcaadopt.py.
MUST_HIT = "a032ce7"
# MUST-MISS: this test's own example token. Fabricated; if the resolver ever
# passes it, the resolver is broken (or someone minted a commit to spite us).
MUST_MISS = "abad1deacafe"
# MUST-MISS for the gate arm: fabricated 16-hex, shaped exactly like a full
# receipt id. If the ledger ever passes it, sha fabrication goes uncatchable.
MUST_MISS_GATE = "deadbeefdeadbeef"
# MUST-HIT patch identity: recomputed from the permanent ba5e740 diff. Unlike a
# Git object id, cat-file must reject it while patch-id --stable verifies it.
MUST_HIT_PATCH = "1f7bacc86ebe5849147f11e33494cf62da89da45"
# MUST-MISS on the same patch verifier: right shape, no diff can produce it.
MUST_MISS_PATCH = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

# LEDGER ROW IDS CITED IN PROSE — dispatch and land-request rows from the
# coordination ledger, receipt ids from the gate ledger. Hex like a sha,
# never git objects, and legitimately citable: kimi's lane comment cited
# dispatch id 1ddf37fc on 2026-08-02, this guard read it as a dead sha,
# red-ed her gate, and confounded a flake investigation for an hour — the
# author had NO legal way to cite a ledger row. This manifest is that way.
#
# Every entry is keyed by the exact token as it appears in prose, with the
# reason it is cited. To cite a new row: write the prose, add the entry here,
# and run `python3 tests/test_docstring_refs.py --audit` on the machine that
# holds the live ledgers — the audit REFUSES (nonzero, `unverifiable ledger
# citation`) any entry the ledgers cannot vouch for, which is how a mistyped
# or fabricated id gets caught the day it is added instead of never. Every
# entry below was verified that way against the live ledgers on 2026-08-02:
# 19 coordination rows, 10 gate receipts, zero ambiguous prefixes.
#
# An entry in any registry that stops matching prose is stale and fails the
# suite; authority does not justify dead manifest data.
# THE REGISTRY MOVED TO helm/docref_guard.py so the PRE-COMMIT rung can
# read it too: scanners are snapshotted out of helm/*.py into
# .git/hooks/.helm-scanners/ and cannot import from tests/. ONE registry,
# two readers — a second copy would drift, and the copy that drifts is
# always the one the other reader trusts.
from helm import docref_guard                        # noqa: E402
from helm.docref_guard import LEDGER_CITED, PATCH_IDS, SKIP  # noqa: E402
from tests._release import history_gap               # noqa: E402


def _git(*args):
    return subprocess.run(("git", "-C", ROOT) + args,
                          capture_output=True, text=True)


def _prose_tokens():
    """(file, token) pairs from every comment and docstring under helm/."""
    out = []
    for dirpath, _dirs, files in os.walk(HELM):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with tokenize.open(path) as f:
                src = f.read()
            prose = [t.string for t in
                     tokenize.generate_tokens(io.StringIO(src).readline)
                     if t.type == tokenize.COMMENT]
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc:
                        prose.append(doc)
            rel = os.path.relpath(path, ROOT)
            for text in prose:
                for tok in HEX.findall(text):
                    out.append((rel, tok))
    return out


def _resolves(tok):
    """True iff tok names a commit an in-repo reader can actually reach:
    an ancestor of HEAD, or the target of a live ref (archive/rescue tags)."""
    if _git("cat-file", "-e", tok + "^{commit}").returncode != 0:
        return False
    full = _git("rev-parse", "--verify", "-q", tok + "^{commit}").stdout.strip()
    if _git("merge-base", "--is-ancestor", full, "HEAD").returncode == 0:
        return True
    return bool(_git("for-each-ref", "--points-at", full).stdout.strip())


def _absent(tok):
    """True iff no commit by this name is in the repository at all — the one
    state a release export excuses. Present-but-unreachable is not absent."""
    return _git("cat-file", "-e", tok + "^{commit}").returncode != 0


def _dead(tok):
    """Dead = disowned by all three REPO-LOCAL authorities: no LEDGER_CITED
    entry, no SKIP entry, no reachable commit. Nothing here reads a ledger,
    an environment variable, or any state a neighbouring test could have
    planted — the verdict on repo-committed text is a function of the repo,
    identical on every host under every suite order. The live ledgers get
    their say through the --audit entrypoint, which polices LEDGER_CITED
    itself. Dict lookups first: they are free, git is a subprocess."""
    return (tok not in LEDGER_CITED and tok not in PATCH_IDS
            and tok not in SKIP and not _resolves(tok))


def _row_vouched(tok, snap):
    """True iff tok names exactly one row in an already-read snapshot — the
    full id, or a UNIQUE prefix. Prose cites 8- and 12-hex abbreviations, and
    two rows sharing a prefix means the citation does not name one row — that
    is not a vouched id and it must not be cleared."""
    t = tok.strip().lower()
    if t in snap:
        return True
    hits = 0
    for rid in snap:
        if rid.startswith(t):
            hits += 1
            if hits > 1:
                return False        # ambiguous prefix names no single row
    return hits == 1


# THE PREDICATE MOVED TO helm/docref_guard.py, beside the registry it
# polices and for the same reason the registry moved there: the pre-commit
# rung cannot import from tests/, and the authority evaluator must be
# snapshot-owned rather than read out of the tree being judged (F2,
# 2026-08-05). One implementation, imported here.
_citation_vouched = docref_guard.citation_vouched


def _is_ledger_row(tok):
    """True iff tok names a DISPATCH / LAND-REQUEST row (full id or unique
    prefix) in the coordination ledger the AMBIENT home resolves. The audit's
    row arm and a fixture-tested engine — never part of the suite verdict,
    because under any test runner the ambient home is a planted scratch
    (tests/__init__.py) and this function is honestly blind: an unreachable
    or empty ledger answers False, refusing every token rather than passing
    one blind."""
    try:
        snap, unavailable = dispatches.snapshot()
    except Exception:
        return False
    if unavailable or not snap:
        return False
    return _row_vouched(tok, snap)


def _is_gate_receipt(tok):
    """True iff tok names a MINTED gate receipt (full id or unique prefix,
    ambiguity refused by gate.by_id). The audit's receipt arm; same blindness
    contract as `_is_ledger_row`: unavailable answers False."""
    receipt, _err = gate.by_id(tok)
    return receipt is not None


def _audit(cited=None):
    """Verify every LEDGER_CITED token against the LIVE ledgers.

    -> (unverified, notes): unverified maps each token no reachable ledger
    vouches for to a loud `unverifiable ledger citation` line; notes name
    every degraded ledger state. An EMPTY ledger gets its own note because a
    planted or sandboxed home reads as zero rows, and zero rows must never be
    mistaken for a verification pass — under the suite's scratch home every
    entry would report unverifiable, which is the audit refusing to lie, not
    the manifest rotting. Run this where the ledgers actually live."""
    cited = LEDGER_CITED if cited is None else cited
    notes = []
    try:
        snap, row_unavailable = dispatches.snapshot()
    except Exception as exc:
        snap, row_unavailable = {}, str(exc)
    if row_unavailable:
        notes.append("coordination ledger unreachable: %s" % row_unavailable)
    elif not snap:
        notes.append("coordination ledger read EMPTY — a planted or sandboxed "
                     "home is not the live ledger")
    receipts, gate_unavailable, _skipped = gate.receipts()
    receipt_snap = {str(r["id"]): r for r in receipts}
    if gate_unavailable:
        notes.append("gate receipt ledger unreachable: %s" % gate_unavailable)
    elif not receipts:
        notes.append("gate receipt ledger read EMPTY — a planted or sandboxed "
                     "home is not the live ledger")
    degraded = bool(row_unavailable or not snap
                    or gate_unavailable or not receipt_snap)
    unverified = {}
    for tok in sorted(cited):
        if not degraded and _citation_vouched(tok, snap, receipt_snap):
            continue
        unverified[tok] = ("unverifiable ledger citation %s (%s) — no "
                          "reachable ledger vouches for it" % (tok, cited[tok]))
    return unverified, notes


def _audit_main():
    """The one deliberate live read. tests/__init__.py plants scratch homes so
    no TEST can read live state; this explicit entrypoint pops exactly what
    was planted (a value the operator chose survives — the plant defers to
    chosen values, so a chosen value never lives under the plant's tmp root)
    and lets the true home answer. Nonzero on ANY unverifiable entry."""
    import tests
    root = tests._TESTROOT
    if root:
        for var in tests.PLANTED:
            if (os.environ.get(var) or "").startswith(root):
                os.environ.pop(var, None)
    unverified, notes = _audit()
    for note in notes:
        print("note: %s" % note)
    for tok in sorted(unverified):
        print("FAIL: %s" % unverified[tok])
    print("%d ledger citation(s), %d unverifiable"
          % (len(LEDGER_CITED), len(unverified)))
    return 1 if unverified else 0


class DocstringCommitRefsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pairs = _prose_tokens()
        cls.tokens = {t for _f, t in cls.pairs}
        # None in every development checkout; the reason in a release export.
        cls.gap = history_gap(MUST_HIT)

    def test_must_hit_control_is_found_and_live(self):
        # Scanner recall: an empty or blind scan cannot pass this.
        self.assertIn(MUST_HIT, self.tokens,
                      "scanner lost the known-live citation in helm/orcaadopt.py")
        if self.gap:
            self.skipTest("the control is found; its LIVE half needs the "
                          "development history — " + self.gap)
        self.assertTrue(_resolves(MUST_HIT),
                        "known-live citation %s stopped resolving — either "
                        "history moved under it (fix the citation by content: "
                        "the subject is quoted beside it) or the resolver "
                        "broke" % MUST_HIT)

    def test_must_miss_control_fails_the_resolver(self):
        # Positive control on the SAME observable first: the resolver can say
        # yes to a real commit, so its no below is a verdict, not a stub. A
        # release export does not hold MUST_HIT, so there the control is HEAD
        # itself — still a real commit the resolver must accept.
        control = (_git("rev-parse", "HEAD").stdout.strip() if self.gap
                   else MUST_HIT)
        self.assertTrue(_resolves(control),
                        "resolver rejected the known-live control commit %s"
                        % control)
        self.assertFalse(_resolves(MUST_MISS),
                         "the resolver passed a fabricated sha — every green "
                         "result this suite ever produced is now suspect")

    def test_every_cited_sha_resolves(self):  # noqa: VACUOUS_ASSERTION — emptiness IS the claim; the assertIn/assertTrue anchors keep it non-vacuous
        # Anchor: the scan contains the known-live citation, so the empty
        # dead-set below is a verdict about REAL tokens, not an empty scan.
        self.assertIn(MUST_HIT, self.tokens,
                      "scan lost the known-live citation — emptiness below "
                      "would be vacuous")
        checked = {(f, t) for f, t in self.pairs if not t.isdigit()}
        self.assertTrue(checked, "nothing left to check — scan or rules broke")
        dead = sorted({(f, t) for f, t in checked if _dead(t)})
        # A RELEASE EXPORT excuses exactly the citations whose commit it does
        # not hold; a present-but-unreachable commit is still dead. In every
        # development checkout self.gap is None and nothing is excused.
        absent = sorted({t for _f, t in dead if _absent(t)}) if self.gap else []
        dead = [(f, t) for f, t in dead if t not in absent]
        self.assertEqual(dead, [],
                         "dead commit citations — recover a real sha by "
                         "content (subject grep / git log -S) and rewrite sha "
                         "WITH subject; rows/receipts belong in LEDGER_CITED, "
                         "verified content identities in PATCH_IDS, and "
                         "runtime non-objects in SKIP; then run "
                         "`python3 tests/test_docstring_refs.py --audit` "
                         "where the live ledgers live: %s" % dead)
        if absent:
            self.skipTest("%d of %d cited tokens were judged and hold; %d cite "
                          "commits this repository does not have, which only "
                          "the development history can judge — %s"
                          % (len({t for _f, t in checked}) - len(absent),
                             len({t for _f, t in checked}), len(absent),
                             self.gap))

    def test_a_fabricated_row_id_still_reads_dead(self):
        """The must-miss for the row arm. If the engine ever vouches for a
        token nobody minted, the audit stops catching fabricated manifest
        entries — the same hole the gate arm is pinned against."""
        # POSITIVE CONTROL on the same observable, unconditional: the engine
        # DOES vouch for a row its snapshot holds. Without it, an engine that
        # always answered False would satisfy every line below.
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({"abc111": {}}, None)):
            self.assertTrue(_is_ledger_row("abc111"))
            self.assertFalse(_is_ledger_row("dead" * 8))
        self.assertTrue(_dead("dead" * 8),
                        "a fabricated 32-hex token no longer reads dead")

    def test_an_ambiguous_prefix_names_no_row_and_is_refused(self):
        """A prefix shared by two rows does not NAME a row, so it is not a
        vouched id. Clearing it would exempt a token on the strength of an
        identification that was never made."""
        rows = {"abc111": {}, "abc222": {}, "zzz999": {}}
        with mock.patch.object(dispatches, "snapshot", return_value=(rows, None)):
            self.assertFalse(_is_ledger_row("abc"))   # two rows share it
            self.assertTrue(_is_ledger_row("zzz"))    # control: unique resolves

    def test_an_unavailable_ledger_answers_false_never_blind(self):
        """Unavailable answers False — the engine refuses every token rather
        than vouching for one it could not check. It must never pass a token
        because the ledger could not be read."""
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger unreadable")):
            self.assertFalse(_is_ledger_row("abc111"))
        with mock.patch.object(dispatches, "snapshot", side_effect=OSError("x")):
            self.assertFalse(_is_ledger_row("abc111"))
        # control on the same observable: a HEALTHY ledger does vouch
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({"abc111": {}}, None)):
            self.assertTrue(_is_ledger_row("abc111"))

    def test_fabricated_16_hex_still_reads_dead(self):
        # Positive control on the SAME observable first, unconditional: the
        # gate arm says yes to a minted receipt, so its no below is a
        # verdict, not a stub.
        with mock.patch.object(gate, "by_id",
                               return_value=({"id": "0" * 16}, None)):
            self.assertTrue(_is_gate_receipt("0" * 16),
                            "gate arm rejected the known-minted control "
                            "receipt")
        self.assertFalse(_is_gate_receipt(MUST_MISS_GATE),
                         "the ledger passed a fabricated receipt id — the "
                         "sha-fabrication class just went uncatchable")
        self.assertTrue(_dead(MUST_MISS_GATE),
                        "a fabricated 16-hex token no longer reads dead")

    def test_the_audit_catches_a_fabricated_manifest_entry(self):
        """The property SKIP never had: an entry the live ledgers disown is
        REFUSED, loudly, at the moment it is added — not silenced forever."""
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({"abc111": {}}, None)), \
             mock.patch.object(gate, "receipts", return_value=(
                 [{"id": "feedfacefeedface"}], None, 0)):
            unverified, _notes = _audit(cited={"abc111": "real row",
                                               "deadfa11": "fabricated"})
        self.assertNotIn("abc111", unverified,
                         "the audit refused an entry the ledger vouches for")
        self.assertIn("deadfa11", unverified)
        self.assertIn("unverifiable ledger citation", unverified["deadfa11"])

    def test_the_audit_is_loud_when_ledgers_are_unreachable(self):  # noqa: VACUOUS_ASSERTION — the healthy-ledger assertNotIn is the unconditional positive control on the same unverified mapping
        """An unreachable ledger means UNVERIFIED, stated per token and per
        ledger — never a silent pass ('could not check' is not 'checked') and
        never a silent skip. The suite's own scratch home reads as EMPTY, so
        that state gets the same loudness."""
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({"abc111": {}}, None)), \
             mock.patch.object(gate, "receipts", return_value=(
                 [{"id": "feedfacefeedface"}], None, 0)):
            clean, _notes = _audit(cited={"abc111": "healthy row"})
        self.assertNotIn("abc111", clean,
                         "the audit cannot vouch even with both ledgers healthy")
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "ledger unreadable")), \
             mock.patch.object(gate, "receipts",
                               return_value=([], "receipts unreadable", 0)), \
             mock.patch.object(gate, "by_id",
                               return_value=({"id": "abc111"}, None)) as late:
            unverified, notes = _audit(cited={"abc111": "row behind an outage"})
        late.assert_not_called()     # one evidence snapshot, never a recovery race
        self.assertIn("abc111", unverified)
        self.assertIn("unverifiable ledger citation", unverified["abc111"])
        self.assertTrue(any("coordination ledger unreachable" in n
                            for n in notes), notes)
        self.assertTrue(any("gate receipt ledger unreachable" in n
                            for n in notes), notes)

    def test_a_prefix_shared_ACROSS_ledgers_names_no_citation(self):
        """Uniqueness is global: one dispatch plus one receipt is two matches,
        even though each ledger would call the prefix unique in isolation."""
        rows = {"abc11111bbbb2222": {}}
        receipts = [{"id": "abc11111cccc3333"}]
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)), \
             mock.patch.object(gate, "receipts",
                               return_value=(receipts, None, 0)):
            unverified, _notes = _audit(cited={"abc11111": "ambiguous"})
        self.assertIn("abc11111", unverified,
                      "two ledger namespaces each vouched for a different row")

    def test_registered_patch_ids_recompute_from_their_diff(self):  # noqa: VACUOUS_ASSERTION — the forged MUST-MISS immediately below reuses the same diff endpoints and asserts a non-empty rejection, so the empty real-registry result is bracketed on the identical verifier
        """PATCH_IDS is content authority, not a second unverified SKIP list."""
        self.assertIn(MUST_HIT_PATCH, PATCH_IDS)
        self.assertFalse(_resolves(MUST_HIT_PATCH),
                         "the patch-id unexpectedly resolves as a commit")
        unheld = sorted(k for k, (base, tip, _r) in PATCH_IDS.items()
                        if _absent(base) or _absent(tip))
        if self.gap and unheld:
            self.skipTest("%d registered patch-id(s) record a diff between "
                          "commits this repository does not have — %s"
                          % (len(unheld), self.gap))
        self.assertEqual(docref_guard.patch_id_rejections(ROOT, PATCH_IDS), {})

        # MUST-MISS on the same verifier: reuse the known diff endpoints but
        # forge the expected identity. A verifier that checks only shape fails.
        base, tip, reason = PATCH_IDS[MUST_HIT_PATCH]
        rejected = docref_guard.patch_id_rejections(
            ROOT, {MUST_MISS_PATCH: (base, tip, reason)})
        self.assertIn(MUST_MISS_PATCH, rejected)
        self.assertIn("recomputed", rejected[MUST_MISS_PATCH])

    def test_no_exemption_is_stale_or_double_booked(self):  # noqa: VACUOUS_ASSERTION — emptiness IS the claim; the assertIn anchors keep it non-vacuous
        # Anchors: one entry from EACH list that must currently match prose —
        # proves the scan sees exempted tokens before we call none of them
        # stale.
        self.assertIn("ae7a5d6f", LEDGER_CITED,
                      "the manifest anchor moved lists — re-anchor this test")
        self.assertIn("ae7a5d6f", self.tokens,
                      "the dispatch-id manifest entry no longer matches — the "
                      "staleness verdict below would be vacuous")
        self.assertIn("a009d56", self.tokens,
                      "the shaguard-exhibit SKIP entry no longer matches — "
                      "the staleness verdict below would be vacuous")
        self.assertIn(MUST_HIT_PATCH, self.tokens,
                      "the PATCH_IDS entry no longer matches prose — the "
                      "staleness verdict below would be vacuous")
        registries = (set(LEDGER_CITED), set(SKIP), set(PATCH_IDS))
        overlap = set.union(*(a & b for i, a in enumerate(registries)
                              for b in registries[i + 1:]))
        self.assertEqual(sorted(overlap), [],
                         "a token has two authority categories — keep one owner")
        stale = sorted(set.union(*registries) - self.tokens)
        self.assertEqual(stale, [],
                         "registered tokens no longer cited anywhere under "
                         "helm/ — delete their entries: %s" % stale)


class SyntheticLedgerTest(unittest.TestCase):
    """The row arm proven through the REAL reader — file, checked_events,
    fold — against rows THIS test planted, the only ledger a test may read
    (tests/__init__.py). Synthetic ids only: tests/ is public-bound."""

    SYN = "eeee9999ffff0000"
    TWIN_A = "aaaa1111bbbb2222"
    TWIN_B = "aaaa1111cccc3333"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-docref-ledger-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior

    def mint(self, rid):
        """One v1-schema open row — the minimal shape `_new_state` folds into
        a snapshot state, same fixture idiom as test_dispatch_chain."""
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        row = {"v": 1, "id": rid, "seq": 0, "status": "open",
               "ts": "2026-08-02T00:00:00Z", "recipient": "codex-3",
               "lane": "synthetic-docref-fixture", "tip": "f" * 40,
               "deadline_s": 600}
        self.assertTrue(eventledger.append(path, row),
                        "fixture ledger refused the synthetic row")

    def test_a_minted_row_vouches_through_the_real_reader(self):
        self.mint(self.SYN)
        self.mint(self.TWIN_A)
        self.mint(self.TWIN_B)
        self.assertTrue(_is_ledger_row(self.SYN),
                        "the real reader disowned a row this test minted")
        self.assertTrue(_is_ledger_row("eeee9999"),
                        "a unique 8-hex prefix did not resolve — prose cites "
                        "abbreviations")
        self.assertTrue(_is_ledger_row(self.TWIN_A),
                        "a full id must resolve even where its prefix is shared")
        self.assertFalse(_is_ledger_row("aaaa1111"),
                         "a prefix two rows share names no row and must be "
                         "refused")

    def test_a_never_minted_id_is_refused_and_audits_unverifiable(self):
        self.mint(self.SYN)
        # positive control on the same observable first: the reader DOES
        # vouch for the row this test minted, so the refusals below are
        # verdicts, not a reader that answers False to everything.
        self.assertTrue(_is_ledger_row(self.SYN),
                        "the real reader disowned the minted control row")
        self.assertFalse(_is_ledger_row("1234abcd"),
                         "the reader vouched for an id nobody minted")
        healthy_gate = ([{"id": "feedfacefeedface"}], None, 0)
        with mock.patch.object(gate, "receipts", return_value=healthy_gate):
            unverified, _notes = _audit(
                cited={"1234abcd": "synthetic fabrication"})
            # control on the same observable: with BOTH ledgers readable, the
            # minted coordination row audits clean.
            clean, _notes = _audit(cited={self.SYN: "synthetic real row"})
        self.assertIn("1234abcd", unverified)
        self.assertIn("unverifiable ledger citation", unverified["1234abcd"])
        self.assertEqual(clean, {},  # noqa: VACUOUS_ASSERTION — emptiness IS the claim; the assertIn on unverified above is the positive control on the same observable
                         "the audit refused a row the planted ledger holds")

    def test_an_unreachable_ledger_is_loud_through_the_real_reader(self):
        self.mint(self.SYN)
        # positive control on the same observable BEFORE breaking the ledger:
        # the row vouches while the ledger is healthy, so the False below is
        # the unreachability speaking, not a reader that never says yes.
        self.assertTrue(_is_ledger_row(self.SYN),
                        "the real reader disowned the minted control row")
        path = dispatches.ledger_path()
        real = path + ".real"
        os.rename(path, real)
        os.symlink(real, path)      # the reader refuses symlinked ledgers
        _snap, unavailable = dispatches.snapshot()
        self.assertTrue(unavailable,
                        "a symlinked ledger still read as healthy — this "
                        "fixture no longer exercises the unreachable path")
        self.assertFalse(_is_ledger_row(self.SYN),
                         "the engine vouched for a row behind an unreachable "
                         "ledger — that is passing blind")
        unverified, notes = _audit(
            cited={self.SYN: "synthetic row behind an unreachable ledger"})
        self.assertIn(self.SYN, unverified)
        self.assertIn("unverifiable ledger citation", unverified[self.SYN])
        self.assertTrue(any("coordination ledger unreachable" in n
                            for n in notes), notes)


class DocrefStagedRungTest(unittest.TestCase):
    """The PRE-COMMIT rung — the same law as the suite check, moved to the
    moment that makes it cheap. It is deliberately STRICTER than `_resolves`:
    that admits any object a LOCAL ref points at, which is why a citation can
    pass on the author's box and die on the fab."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-docref-rung-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.write("seed.py", "# seed\n")
        self.git("add", docref_guard.POLICED + "seed.py")
        self.git("commit", "-qm", "seed")

    def git(self, *a):
        return subprocess.run(("git", "-C", self.tmp) + a,
                              capture_output=True, text=True)

    def write(self, name, text):
        # UNQUALIFIED NAMES LAND UNDER helm/, because that is the tree the rung
        # polices — it is scoped to the tree the registry can vouch for, and a
        # fixture staging at the repo ROOT would prove nothing while looking
        # exactly like it did. Paths that name their own directory are kept
        # verbatim, which is how the out-of-scope case below is written.
        if "/" not in name:
            name = docref_guard.POLICED + name
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def stage(self, name, text):
        self.write(name, text)
        self.git("add", name if "/" in name else docref_guard.POLICED + name)

    def unaccounted(self):
        return docref_guard.unaccounted(
            self.tmp, docref_guard.staged_tokens(self.tmp))

    def run_guard(self):
        with mock.patch.dict(os.environ, {"HELM_DOCREF_REPO": self.tmp}):
            return docref_guard.main(["--staged"])

    def test_a_pure_decimal_literal_is_not_a_citation(self):
        """Suite/rung parity on numbers: the suite filters `t.isdigit()`
        before judging tokens, so the rung must too — a size cap like
        1000000 in an added comment is a numeric literal, not a claim that
        an object exists, and refusing it teaches HELM_DOCREF_SKIP=1."""
        self.stage("num.py", "# retry cap 1000000, window 1000000000\n")
        self.assertEqual(self.unaccounted(), [])
        # control on the same observable: a hex-lettered fabrication in the
        # same staged file still refuses — the filter is digits-only.
        self.stage("num.py", "# retry cap 1000000 near %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

    def test_a_fabricated_citation_is_unaccounted(self):
        # POSITIVE CONTROL on the same observable: a clean stage yields an
        # EMPTY result, so the non-empty result below is the rung firing and
        # not the scanner erroring into a truthy value.
        self.stage("clean.py", "# nothing cited here\n")
        self.assertEqual(self.unaccounted(), [])
        self.stage("bad.py", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

    def test_an_added_line_whose_text_starts_PLUS_PLUS_is_scanned(self):  # noqa: VACUOUS_ASSERTION — the ordinary-added-line assertion is an unconditional positive control on the same scanner and token before the header-shaped payload assertion
        """The staged guard tracks hunk state, not a `+++` prefix exclusion.

        LOAD-BEARING MUTATION: restore the old condition
        `line.startswith("+") and not line.startswith("+++")`; this arm goes
        red because the added payload renders as `+++ <token>` and disappears.
        """
        # MUST-HIT first on the same scanner and token: an ordinary added line
        # is seen, so the second assertion cannot pass because token handling
        # or the scratch repository silently stopped working.
        self.stage("plus.py", "ordinary %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

        self.stage("plus.py", "++ %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

        # SAME raw prefix outside a hunk is the file header, never payload.
        self.git("rm", "-q", "--cached", "helm/plus.py")
        self.stage("%s.py" % MUST_MISS, "# no citation\n")
        self.assertEqual(self.unaccounted(), [])

    def test_a_C_QUOTED_policed_path_does_not_inherit_the_previous_file(self):  # noqa: VACUOUS_ASSERTION — the raw-diff assertion proves Git emitted the C-quoted path shape before the same staged scanner must find its token
        """Path state resets per file and decodes Git's C-quoted b-side path.

        LOAD-BEARING MUTATIONS: restore `if line.startswith("+++ b/")`, or
        remove the pinned `diff.noprefix=false`; the quoted helm path becomes
        unreadable/inherits the preceding out-of-scope path and this arm goes
        red because its citation is silently exempted.
        """
        # Adversarial no-prefix config: the scanner pins the prefix grammar;
        # quotepath stays on so this arm also exercises the C-quote decoder.
        self.git("config", "core.quotepath", "true")
        self.git("config", "diff.noprefix", "true")
        self.stage("aaa/out.py", "# outside the policed tree\n")
        self.stage("helm/é.py", "# cites %s\n" % MUST_MISS)
        raw = self.git("-c", "diff.noprefix=false", "diff", "--cached",
                       "--unified=0", "--no-color", "--no-ext-diff").stdout
        self.assertIn('+++ "b/helm/', raw,
                      "the pinned fixture must emit a C-quoted b-side path")
        self.assertEqual(self.unaccounted(), [("helm/é.py", MUST_MISS)])

    def test_readable_but_unsupported_diff_syntax_refuses_the_commit(self):
        """Parser UNKNOWN is not Git read failure and may never skip the guard.

        LOAD-BEARING MUTATIONS: replace EITHER `_UnsupportedDiff` raise in
        `staged_tokens` with `return None`; its corresponding arm returns zero
        and prints SKIPPED instead of refusing.
        """
        base = ("diff --git a/helm/x.py b/helm/x.py\n"
                "index 1111111..2222222 100644\n"
                "--- a/helm/x.py\n")
        bodies = (
            base + "+++ b/helm/x.py\n@@@ unsupported @@@\n+abad1deacafe\n",
            base + '+++ "b/helm/unterminated\n@@ -1 +1 @@\n+abad1deacafe\n',
        )
        for body in bodies:
            err = io.StringIO()
            result = subprocess.CompletedProcess((), 0, body, "")
            with mock.patch.object(docref_guard, "_git", return_value=result):
                with contextlib.redirect_stderr(err):
                    rc = docref_guard.main(["--staged"])
            self.assertEqual(rc, 1, body)
            self.assertIn("REFUSED", err.getvalue(), body)
            self.assertIn("UNKNOWN", err.getvalue(), body)
            self.assertNotIn("SKIPPED", err.getvalue(), body)

        # CONTROL on the split's other side: an actual Git read failure keeps
        # the pre-existing warn-and-backstop behavior rather than being
        # mislabeled as parser syntax.
        err = io.StringIO()
        failed = subprocess.CompletedProcess((), 1, "", "read failed")
        with mock.patch.object(docref_guard, "_git", return_value=failed):
            with contextlib.redirect_stderr(err):
                rc = docref_guard.main(["--staged"])
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED", err.getvalue())
        self.assertNotIn("REFUSED", err.getvalue())

    def test_a_lanes_own_registry_entry_reaches_the_running_rung(self):  # noqa: VACUOUS_ASSERTION — the same self.unaccounted() call is asserted NON-EMPTY immediately before the registration and again after, so the empty reading is bracketed by two unconditional positive controls on the identical observable
        """THE RUNNING MODULE DOES NOT HOLD THIS TOKEN — that is the test.
        abcabcabcab is absent from the real LEDGER_CITED and SKIP, so it can
        only become accountable by being read out of the COMMITTING TREE.

        The lane that measured this staged a dispatch id and a registry entry
        for it in one commit, and the rung refused that commit: the entry sat
        in the index while the decision was made by the snapshot under
        .git/hooks/.helm-scanners/, and install-guard re-takes that snapshot
        from the SHARED CHECKOUT, so no lane could reach it. The only move
        left was HELM_DOCREF_SKIP=1."""
        synth = "abcabcabcab"
        self.assertNotIn(synth, LEDGER_CITED)      # the premise, asserted
        self.assertNotIn(synth, SKIP)

        self.stage("cites.py", "# cites %s\n" % synth)
        self.assertEqual([t for _p, t in self.unaccounted()], [synth])

        self.stage(os.path.basename(docref_guard.SELF),
                   'LEDGER_CITED = {"%s": "a synthetic row"}\nSKIP = {}\n'
                   % synth)
        self.assertEqual(self.unaccounted(), [])

        # MUST-MISS on the same observable: the widening is exactly one token,
        # not a registry that now admits anything.
        self.stage("other.py", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

    def test_a_lanes_own_SKIP_entry_reaches_the_running_rung(self):  # noqa: VACUOUS_ASSERTION — the same token is unaccounted immediately before its staged SKIP entry and accounted immediately after
        synth = "deadc0de9876"
        self.assertNotIn(synth, SKIP)
        self.stage("cites.py", "# runtime id %s\n" % synth)
        self.assertEqual([t for _p, t in self.unaccounted()], [synth])

        self.stage(os.path.basename(docref_guard.SELF),
                   'LEDGER_CITED = {}\nSKIP = {"%s": "runtime request id"}\n'
                   'PATCH_IDS = {}\n' % synth)
        self.assertEqual(self.unaccounted(), [])

    def test_a_lanes_own_PATCH_ID_entry_reaches_the_running_rung(self):  # noqa: VACUOUS_ASSERTION — the same content id is unaccounted before its staged PATCH_IDS entry and accounted immediately after
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("payload.py", "# content whose diff has an identity\n")
        self.git("add", docref_guard.POLICED + "payload.py")
        self.git("commit", "-qm", "patch source")
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        diff = self.git("diff", "--no-ext-diff", "--binary", base, tip, "--")
        patch = subprocess.run(
            ("git", "-C", self.tmp, "patch-id", "--stable"),
            input=diff.stdout, capture_output=True, text=True, check=True
        ).stdout.split()[0]
        self.assertNotIn(patch, PATCH_IDS)
        self.stage("cites.py", "# patch-id %s\n" % patch)
        self.assertEqual([t for _p, t in self.unaccounted()], [patch])

        self.stage(os.path.basename(docref_guard.SELF),
                   'LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = '
                   '{"%s": ("%s", "%s", "rebase carry proof")}\n'
                   % (patch, base, tip))
        self.assertEqual(self.unaccounted(), [])

    def test_snapshot_PATCH_IDS_are_part_of_the_accounted_set(self):  # noqa: VACUOUS_ASSERTION — the known patch id is a non-object and the identical staged token must still be accounted by the snapshot registry
        self.assertIn(MUST_HIT_PATCH, PATCH_IDS)
        self.assertNotEqual(
            self.git("cat-file", "-e", MUST_HIT_PATCH).returncode, 0)
        self.stage("cites.py", "# patch-id %s\n" % MUST_HIT_PATCH)
        self.assertEqual(self.unaccounted(), [])

    def test_the_category_split_bootstraps_under_the_installed_flat_snapshot(self):  # noqa: VACUOUS_ASSERTION — the same historical snapshot immediately refuses a fabricated citation after admitting the self-only category migration
        """The previous installed rung must admit the commit replacing it.

        Snapshot ownership means pre-commit runs the old evaluator while this
        file is staged. SELF is exempt and the old parser ignores PATCH_IDS, so
        re-categorising the registry must remain bootstrap-safe.

        The old rung's new-key arm is out of scope here and is injected as
        vouched — see the comment at the call, and the current rung's own
        category authority is covered by the sibling tests below.
        """
        old_ref = "19997d431198c3ec68e22bfc38d6270fb452a20f"
        gap = history_gap(MUST_HIT)
        if gap and _absent(old_ref):
            self.skipTest("the installed snapshot's source %s is not in this "
                          "repository — %s" % (old_ref[:12], gap))
        old_source = subprocess.run(
            ("git", "-C", ROOT, "show", old_ref + ":helm/docref_guard.py"),
            capture_output=True, text=True, check=True).stdout
        with open(docref_guard.__file__, encoding="utf-8") as f:
            current_source = f.read()
        self._plant_registry(old_source, current_source)

        snapshot = types.ModuleType("installed_docref_snapshot")
        snapshot.__file__ = os.path.join(
            self.tmp, ".git", "hooks", ".helm-scanners", "docref_guard.py")
        exec(compile(old_source, snapshot.__file__, "exec"), snapshot.__dict__)
        with mock.patch.dict(os.environ, {"HELM_DOCREF_REPO": self.tmp}):
            # THE ADDED KEYS ARE INJECTED AS VOUCHED, because the old rung's
            # new-key arm cannot be asked this question honestly and asking it
            # anyway turns this fixture into a veto on the registry itself.
            # Two reasons, both structural. (a) The home under test is a
            # planted scratch, so `_live_ledgers` reads EMPTY and that arm
            # fails CLOSED by design — a refusal about the FIXTURE, not about
            # the commit. (b) The old rung is CATEGORY-BLIND, which is the
            # whole defect the split fixed: it flattens LEDGER_CITED and SKIP
            # and demands a ledger row for both, so a SKIP token — a PID, a
            # session id, a meld epoch — reads unvouched forever, no matter
            # which ledger is live. Left unpatched this asserted "helm/
            # docref_guard.py never gains a key again", and it went red on the
            # first honest SKIP entry after the split (three relocated
            # non-object ids, admitted by today's `skip_rejections`) — one
            # rung in this repo admitting what another calls a defect.
            # What stays under test is the bootstrap property the docstring
            # names: SELF is exempt from its own scan, the old parser ignores
            # PATCH_IDS, and the replacing commit's prose stays accounted. The
            # MUST-MISS below is the control that keeps that non-vacuous — the
            # same evaluator refuses a fabricated citation one line later.
            vouched = ({k: {} for k in
                        snapshot.added_registry_keys(self.tmp)}, {}, None)
            with mock.patch.object(snapshot, "_live_ledgers",
                                   return_value=vouched):
                self.assertEqual(snapshot.main(["--staged"]), 0)

                self.stage("bad.py", "# cites %s\n" % MUST_MISS)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(snapshot.main(["--staged"]), 1)

    def test_an_unparseable_staged_registry_refuses_main_while_library_falls_back(self):  # noqa: VACUOUS_ASSERTION — main's rc/message refusal is the unconditional positive control before the direct-library empty fallback
        """Malformed authority fails closed at the authorizing CLI boundary.

        A direct library read still returns the snapshot-only answer rather than
        crashing, but main() must refuse before that fallback can authorize a
        commit. Keeping the two contracts distinct prevents stale prose or tests
        from turning a diagnostic fallback into the guard's decision."""
        synth = "dddeeefff12"
        self.stage("cites.py", "# cites %s\n" % synth)
        self.stage(os.path.basename(docref_guard.SELF), "def (((\n")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_guard(), 1)
        self.assertIn("staged registry data does not parse", err.getvalue())
        self.assertIn("authority is UNKNOWN", err.getvalue())

        fallback_err = io.StringIO()
        with contextlib.redirect_stderr(fallback_err):
            self.assertEqual([t for _p, t in self.unaccounted()], [synth])
            self.assertEqual(docref_guard.committing_registry(self.tmp), ())
        self.assertIn("does not parse", fallback_err.getvalue())

        # POSITIVE CONTROL ON BOTH CALLS: restore a well-formed registry and the
        # direct reader widens while main reaches category admission. The ledger
        # key is injected as vouched so this fixture measures parsing, not live
        # coordination state.
        self.stage(os.path.basename(docref_guard.SELF),
                   'LEDGER_CITED = {"%s": "now well-formed"}\nSKIP = {}\n'
                   'PATCH_IDS = {}\n' % synth)
        self.assertIn(synth, docref_guard.committing_registry(self.tmp))
        with mock.patch.object(docref_guard, "_live_ledgers",
                               return_value=({synth: {}}, {}, None)):
            self.assertEqual(self.run_guard(), 0)
        self.assertEqual([t for _p, t in self.unaccounted()], [])

    def _plant_registry(self, committed, staged=None):
        """Commit one registry, then stage another — the ADDED-key fixture."""
        name = os.path.basename(docref_guard.SELF)
        self.stage(name, committed)
        self.git("commit", "-qm", "base registry")
        if staged is not None:
            self.stage(name, staged)

    def test_a_new_key_no_ledger_vouches_for_is_REFUSED(self):
        """The repro, closed. A fabricated LEDGER_CITED key plus its
        citation minted a GREEN whole-suite receipt — measured on this lane's
        tip AND with this lane's change removed, so the gap predates the lane.
        The gate cannot close it: the suite is barred from live ledgers and the
        fab has none. This rung runs at pre-commit, where the ledgers are.

        LEDGERS ARE INJECTED, not read: a test may not read live state it did
        not plant, and the evaluator takes them as a parameter precisely so
        this arm can exist without one."""
        rows = {"aaaaaaaaaaaa": {}}
        receipts = {"bbbbbbbbbbbb": {}}
        unvouched, why = docref_guard.vouch_added_keys(
            ("deadc0de9876",), ledgers=(rows, receipts, None))
        self.assertEqual(unvouched, ("deadc0de9876",))
        self.assertIsNone(why)

        # MUST-MISS on the same call: a key a ledger DOES name passes, so the
        # refusal above is about the ledger and not about the call shape.
        self.assertEqual(docref_guard.vouch_added_keys(
            ("aaaaaaaaaaaa",), ledgers=(rows, receipts, None)), ((), None))

    def test_an_unreadable_ledger_fails_CLOSED(self):
        """I had this backwards and a review corrected it. I argued fail-OPEN
        because refusing would "block every commit on a machine without the
        live home" — but this arm fires ONLY on a commit that ADDS a registry
        key, so it blocks those and nothing else. The module already promises
        LEDGER_CITED fails CLOSED at authoring time; fail-open would have made
        the new rung contradict the sentence the registry was built around."""
        unvouched, why = docref_guard.vouch_added_keys(
            ("ccccccccccc",), ledgers=({}, {}, "coordination ledger read EMPTY"))
        self.assertEqual(unvouched, ("ccccccccccc",))
        self.assertIn("read EMPTY", why)

        # POSITIVE CONTROL on the same call: with readable ledgers naming it,
        # the SAME key passes — so the refusal is the degradation, not the key.
        self.assertEqual(docref_guard.vouch_added_keys(
            ("ccccccccccc",), ledgers=({"ccccccccccc": {}}, {}, None)),
            ((), None))

    def test_a_new_SKIP_key_uses_object_admission_not_the_ledgers(self):  # noqa: VACUOUS_ASSERTION — the accepted non-object is followed by real-object and unreadable-probe MUST-MISS controls on the same SKIP admission function, both asserting non-empty refusals
        """A runtime id cannot satisfy citation_vouched by definition.

        MUST-HIT: a non-object with a reason is admitted even when ledger reads
        would fail. MUST-MISS: the same registry refuses a real Git object.
        """
        token = "deadc0de9876"
        self._plant_registry(
            'LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = {}\n',
            'LEDGER_CITED = {}\nSKIP = {"%s": "runtime request id"}\n'
            'PATCH_IDS = {}\n' % token)
        with mock.patch.object(docref_guard, "_live_ledgers",
                               side_effect=AssertionError("SKIP read ledgers")):
            self.assertEqual(self.run_guard(), 0)

        obj = self.git("rev-parse", "HEAD").stdout.strip()
        self.stage(os.path.basename(docref_guard.SELF),
                   'LEDGER_CITED = {}\nSKIP = {"%s": "not runtime"}\n'
                   'PATCH_IDS = {}\n' % obj)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_guard(), 1)
        self.assertIn("resolves as a Git object", err.getvalue())

        # UNKNOWN is not absence: a broken cat-file probe fails closed.
        failed = subprocess.CompletedProcess(("git",), 1, "", "object db down")
        with mock.patch.object(docref_guard, "_git", return_value=failed):
            rejected = docref_guard.skip_rejections(
                self.tmp, {token: "runtime request id"})
        self.assertIn("absence could not be proven", rejected[token])

    def test_a_new_SKIP_key_must_carry_a_reason(self):
        token = "deadc0de9876"
        self._plant_registry(
            'LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = {}\n',
            'LEDGER_CITED = {}\nSKIP = {"%s": ""}\nPATCH_IDS = {}\n'
            % token)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_guard(), 1)
        self.assertIn("non-empty reason", err.getvalue())

    def test_a_new_PATCH_ID_is_recomputed_from_its_diff(self):  # noqa: VACUOUS_ASSERTION — the valid diff's zero-return is immediately paired with a forged identity over the same endpoints that must return one and name the recomputed mismatch
        """The category admits content identity, never cat-file identity."""
        self._plant_registry(
            'LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = {}\n')
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("payload.py", "# content whose diff has an identity\n")
        self.git("add", docref_guard.POLICED + "payload.py")
        self.git("commit", "-qm", "patch source")
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        diff = self.git("diff", "--no-ext-diff", "--binary", base, tip, "--")
        patch = subprocess.run(
            ("git", "-C", self.tmp, "patch-id", "--stable"),
            input=diff.stdout, capture_output=True, text=True, check=True
        ).stdout.split()[0]
        self.assertEqual(len(patch), 40)               # MUST-HIT shape
        self.assertNotEqual(self.git("cat-file", "-e", patch).returncode, 0)

        name = os.path.basename(docref_guard.SELF)
        valid = ('LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = '
                 '{"%s": ("%s", "%s", "rebase carry proof")}\n'
                 % (patch, base, tip))
        self.stage(name, valid)
        self.assertEqual(self.run_guard(), 0)

        forged = valid.replace(patch, "d" * 40)
        self.stage(name, forged)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_guard(), 1)
        self.assertIn("recomputed", err.getvalue())   # MUST-MISS same diff

    def test_a_malformed_PATCH_ID_witness_refuses_by_name(self):
        patch = "a" * 40
        rejected = docref_guard.patch_id_rejections(
            self.tmp, {patch: ("base", "tip")})
        self.assertEqual(
            rejected,
            {patch: "entry must be (base, tip, non-empty reason)"})

        self._plant_registry(
            'LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = {}\n',
            'LEDGER_CITED = {}\nSKIP = {}\nPATCH_IDS = '
            '{"%s": ("base", "tip")}\n' % patch)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_guard(), 1)
        said = err.getvalue()
        self.assertIn(patch, said)
        self.assertIn("entry must be", said)
        self.assertNotIn("Traceback", said)

    def test_same_key_authority_changes_are_re_admitted(self):  # noqa: VACUOUS_ASSERTION — the clean witness is the unconditional control, then the same three keys are mutated and the guard must return one with non-empty category-specific refusals
        """Changing a witness/reason is a new authority claim, not old data."""
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("witness.py", "# one stable content diff\n")
        self.git("add", docref_guard.POLICED + "witness.py")
        self.git("commit", "-qm", "patch witness")
        tip = self.git("rev-parse", "HEAD").stdout.strip()
        diff = self.git("diff", "--no-ext-diff", "--binary", base, tip, "--")
        patch = subprocess.run(
            ("git", "-C", self.tmp, "patch-id", "--stable"),
            input=diff.stdout, capture_output=True, text=True, check=True
        ).stdout.split()[0]
        ledger, skip = "aaaa1111bbbb", "deadc0de9876"
        committed = (
            'LEDGER_CITED = {"%s": "original row reason"}\n'
            'SKIP = {"%s": "runtime request id"}\n'
            'PATCH_IDS = {"%s": ("%s", "%s", "original diff")}\n'
            % (ledger, skip, patch, base, tip))
        self._plant_registry(committed)
        self.assertEqual(docref_guard.patch_id_rejections(
            self.tmp, {patch: (base, tip, "original diff")}), {})

        changed = (
            'LEDGER_CITED = {"%s": "changed row reason"}\n'
            'SKIP = {"%s": ""}\n'
            'PATCH_IDS = {"%s": ("bad", "bad", "changed diff")}\n'
            % (ledger, skip, patch))
        self.stage(os.path.basename(docref_guard.SELF), changed)
        err = io.StringIO()
        with mock.patch.object(docref_guard, "_live_ledgers",
                               return_value=({}, {}, "ledgers unavailable")), \
             contextlib.redirect_stderr(err):
            self.assertEqual(self.run_guard(), 1)
        said = err.getvalue()
        self.assertIn("LEDGER_CITED", said)
        self.assertIn("SKIP", said)
        self.assertIn("PATCH_IDS", said)
        self.assertIn("non-empty reason", said)
        self.assertIn("diff endpoints", said)

    def test_the_authority_evaluator_never_executes_the_judged_tree(self):
        """F2, pinned. A cure that shells out to the judged tree's
        own tests/test_docstring_refs.py — and not even its staged copy — fails. The
        repro replaced ONLY the unstaged working-tree file with a script that
        wrote a marker: guard rc=0, marker written, script staged=0. Any lane
        got a shell inside the snapshot rung and could vouch its own key.

        That is the no-exec boundary committing_registry was written to hold,
        breached one function later by the author of the boundary. The marker
        is the whole assertion: if it ever appears, the judged tree ran."""
        marker = os.path.join(self.tmp, "audit-executed")
        path = os.path.join(self.tmp, "tests", "test_docstring_refs.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("open(%r, 'w').write('ran')\n" % marker)

        # UNCONDITIONAL POSITIVE CONTROL, and the vacuity rung was right to
        # demand it: the absence of this marker proves nothing unless the
        # planted script can actually produce it. Run it once directly, see
        # the marker, then remove it — so the assertion below is about the
        # guard's behaviour and not about a fixture that never had teeth.
        subprocess.run([sys.executable, path], capture_output=True)
        self.assertTrue(os.path.exists(marker))
        os.remove(marker)

        self._plant_registry(
            'LEDGER_CITED = {"aaaaaaaaaaa": "already here"}\nSKIP = {}\n',
            'LEDGER_CITED = {"aaaaaaaaaaa": "already here",\n'
            '                "deadc0de9876": "fabricated"}\nSKIP = {}\n')
        self.stage("cites.py", "# cites deadc0de9876\n")

        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HELM_DOCREF_REPO": self.tmp}):
            with contextlib.redirect_stderr(err):
                rc = docref_guard.main(["--staged"])

        self.assertFalse(os.path.exists(marker),
                         "the judged worktree's script was EXECUTED")
        self.assertEqual(rc, 1)                 # and it still refused
        self.assertIn("deadc0de9876", err.getvalue())

    def test_a_prefix_naming_one_row_AND_one_receipt_names_two_things(self):
        """Per-ledger uniqueness is not uniqueness. A token that is an exact
        key in BOTH namespaces names two artifacts, and vouching for it would
        let a citation resolve to whichever ledger the reader happened to
        check. Survived mutation until this arm existed — the rule travelled
        with the predicate when it moved into helm/, and moving code without
        pinning its guarantees is how a property quietly becomes optional."""
        both = "aaaaaaaaaaaa"
        self.assertFalse(docref_guard.citation_vouched(
            both, {both: {}}, {both: {}}))          # exact in BOTH -> refused
        # must-hit control on the same call: exactly one namespace passes.
        self.assertTrue(docref_guard.citation_vouched(both, {both: {}}, {}))
        # and the prefix arm, same shape
        self.assertFalse(docref_guard.citation_vouched(
            "aaaa", {"aaaabbbb": {}}, {"aaaacccc": {}}))
        self.assertTrue(docref_guard.citation_vouched(
            "aaaa", {"aaaabbbb": {}}, {}))

        # EXACT-PLUS-PREFIX MUST-MISS: exact matching cannot return early while
        # a longer id in either namespace shares the same token.
        self.assertFalse(docref_guard.citation_vouched(
            "aaaaaaaa", {"aaaaaaaa": {}}, {"aaaaaaaabbbb": {}}))
        self.assertFalse(docref_guard.citation_vouched(
            "aaaaaaaa", {"aaaaaaaa": {}, "aaaaaaaacccc": {}}, {}))
        # MUST-HIT: remove the longer id and the exact identity names one thing.
        self.assertTrue(docref_guard.citation_vouched(
            "aaaaaaaa", {"aaaaaaaa": {}}, {}))

    def test_the_trusted_root_is_the_snapshots_own_checkout(self):  # noqa: VACUOUS_ASSERTION — both arms assert an EQUALITY against a constructed path, never an absence; the run-from-source arm is the unconditional positive control on the same _trusted_root() call, and the paths are deliberately synthetic (they need not exist for a pure path computation)
        """The evaluator must be SNAPSHOT-OWNED (F2). Under the suite
        both roots are this checkout, so nothing else in this file can tell a
        correct _trusted_root from one that returns the judged repo — which is
        exactly the mutation that survived until this arm existed.

        The installed rung lives at
        <shared>/.git/hooks/.helm-scanners/docref_guard.py, so the answer must
        be <shared> — the tree install-guard took the logic from — and NOT the
        lane whose commit is being judged."""
        shared = os.path.join(self.tmp, "shared-checkout")
        snap = os.path.join(shared, ".git", "hooks", ".helm-scanners",
                            "docref_guard.py")
        with mock.patch.object(docref_guard, "__file__", snap):
            self.assertEqual(docref_guard._trusted_root(), shared)

        # must-hit control on the same call: run from source, the answer is
        # that repo — so the branch above is the snapshot case and not a
        # function that returns its argument's grandparent unconditionally.
        src = os.path.join(self.tmp, "some-repo", "helm", "docref_guard.py")
        with mock.patch.object(docref_guard, "__file__", src):
            self.assertEqual(docref_guard._trusted_root(),
                             os.path.join(self.tmp, "some-repo"))

    def test_a_local_only_commit_is_unaccounted(self):
        """THE -r RULE, and the reason this rung exists at all. An object a
        LOCAL branch points at is reachable HERE and invisible to every other
        clone. `git branch -a --contains` vouches for it; `-r` does not. That
        distinction cost two whole-suite gate rounds on 2026-08-04."""
        self.write("side.py", "# side work\n")
        self.git("add", docref_guard.POLICED + "side.py")
        self.git("commit", "-qm", "local only")
        local = self.git("rev-parse", "HEAD").stdout.strip()[:12]
        # A PURE-DECIMAL PREFIX IS A NUMBER TO THE GUARD, BY DESIGN (its
        # HEX loop skips tok.isdigit()), so a fixture commit whose 12-hex
        # prefix happens to draw only digits — 1 in ~280 — is cited as a
        # numeric literal and this arm reads [] for [sha]. Measured on fab
        # 2026-08-22 (prefix 382597796175), green on the rerun. Re-mint until
        # the token is a citation, so the arm tests reachability, not luck.
        n = 0
        while local.isdigit():
            n += 1
            self.git("commit", "-q", "--amend", "-m", "local only %d" % n)
            local = self.git("rev-parse", "HEAD").stdout.strip()[:12]
        # MUST-HIT for the fixture: the object really IS reachable locally,
        # so the refusal below is about REMOTE reachability specifically.
        self.assertEqual(
            self.git("cat-file", "-e", local + "^{commit}").returncode, 0)
        self.assertTrue(self.git("branch", "-a", "--contains",
                                 local).stdout.strip())
        self.stage("cites.py", "# cites %s\n" % local)
        self.assertEqual([t for _p, t in self.unaccounted()], [local])

    def test_a_registered_token_is_accounted_for(self):
        known = next(iter(docref_guard.LEDGER_CITED))
        self.stage("ok.py", "# cites ledger row %s\n" % known)
        self.assertEqual(self.unaccounted(), [])

    def test_web_ui_parts_are_source_not_prose_citations(self):  # noqa: VACUOUS_ASSERTION — the first and last assertions prove the same token is caught in policed Python and manifest files; only browser .part source is exempt
        """The former web_ui.html was outside the prose registry's scanner.
        Splitting it must not turn inherited commit examples and encoded colors
        into newly authored citations, while manifest configuration stays in
        the ordinary staged-file scope."""
        self.stage("must_hit.py", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])
        self.git("rm", "-q", "--cached", docref_guard.POLICED + "must_hit.py")
        self.stage("helm/web_ui/scripts/example.js.part",
                   "// rounds 02bc4c0, 3f7bfe1; colors %237eb8a2 %23e8594f\n")
        self.assertEqual(self.unaccounted(), [])
        self.stage("helm/web_ui/manifest.txt", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

    def test_the_registry_file_is_exempt_from_itself(self):
        """THE GUARD MUST NOT BLOCK ITS OWN CURE. Registering a token ADDS a
        line whose text IS that token; without this exemption the rung reads
        the act of REGISTERING a citation as MAKING an unaccounted one and
        refuses the exact commit that fixes the refusal — leaving the first
        person who hits it no move but disabling the rung wholesale. Found by
        running the scanner against its own staged diff, not by reading it."""
        os.makedirs(os.path.join(self.tmp, "helm"), exist_ok=True)
        self.stage(docref_guard.SELF, '    "%s": "a new registration",\n'
                   % MUST_MISS)
        self.assertEqual(self.unaccounted(), [])
        # ...and the exemption is SCOPED to that path, never global
        self.stage("elsewhere.py", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

    def test_fixture_data_is_not_a_citation(self):  # noqa: VACUOUS_ASSERTION — the sibling POLICED test's must-HIT refuses the SAME token through the same scanner, so the empty fixture result is the exemption firing
        """tests/fixtures/ holds VERBATIM captured data; hex inside it is
        runtime identity (a terminal-handle UUID, a session id), never a claim
        about history. Measured 2026-08-04: the codex-3 pane-tail fixture's
        orca handle ends in 12 hex chars and this rung refused it as a dead
        commit. Exemption now arrives via the POLICED scope (fixtures sit
        outside helm/); this test pins the incident's own shape so the
        fixture case never regresses whatever the scope mechanism becomes."""
        os.makedirs(os.path.join(self.tmp, "tests", "fixtures"),
                    exist_ok=True)
        self.stage("tests/fixtures/pane.txt", "handle: term_%s\n" % MUST_MISS)
        self.assertEqual(self.unaccounted(), [])

    def test_a_token_OUTSIDE_the_policed_tree_is_not_this_rung_s_business(self):  # noqa: VACUOUS_ASSERTION — the must-HIT is this test's FIRST assertion: the same token inside the tree IS refused, so the empty result is the scope
        """THE SCOPE MUST EQUAL THE REGISTRY'S, and this is the trap it closes.
        The rung read every staged file while the registry only vouches for
        tokens cited under helm/, so a hex token added in tests/ had NO LEGAL
        CURE: registering it made `test_no_exemption_is_stale_or_double_booked`
        call the entry stale, and not registering it blocked the commit. The
        only move left was HELM_DOCREF_SKIP=1. Measured 2026-08-04 — a fixture
        gate token in tests/ refused a commit, was registered, and took the
        whole-suite gate RED one run later."""
        # MUST-HIT first: the identical token IS refused inside the tree, so
        # the miss below is the scope and not a scanner that stopped working.
        self.stage("inside.py", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])
        self.git("rm", "-q", "--cached", docref_guard.POLICED + "inside.py")
        self.stage("tests/fixture.py", "# fixture token %s\n" % MUST_MISS)
        self.assertEqual(self.unaccounted(), [])

    def test_only_added_lines_count(self):
        """A citation you did not write is not yours to fix. A whole-tree scan
        refuses your commit for someone else's pre-existing token, which is how
        a rung earns a blanket SKIP=1 and stops guarding anything."""
        self.write("legacy.py", "# pre-existing citation %s\n" % MUST_MISS)
        self.git("add", docref_guard.POLICED + "legacy.py")
        self.git("commit", "-qm", "legacy citation already in history")
        # MUST-HIT on the same observable, so the empty result below is the
        # added-lines rule and not a scanner that returns [] no matter what:
        # the identical token IS caught when I am the one adding it.
        self.stage("mine.py", "# I cite it too: %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])
        self.stage("mine.py", "# no citation of my own\n")
        self.assertEqual(self.unaccounted(), [])


    def merge_a_trunk_citing(self, token):
        """A real two-parent index: the trunk cites `token` under helm/, a lane
        branched before it commits elsewhere, and the merge is left staged."""
        trunk = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertTrue(trunk, "the fixture has no branch to merge from")
        self.assertEqual(self.git("checkout", "-qb", "lane").returncode, 0)
        self.stage("lane_own.py", "# the lane's own work\n")
        self.git("commit", "-qm", "lane work")
        self.assertEqual(self.git("checkout", "-q", trunk).returncode, 0)
        self.stage("trunkcite.py", "# cites %s\n" % token)
        self.git("commit", "-qm", "trunk cites it")
        self.assertEqual(self.git("checkout", "-q", "lane").returncode, 0)
        merged = self.git("merge", "--no-commit", "--no-ff", trunk)
        gitdir = self.git("rev-parse", "--absolute-git-dir").stdout.strip()
        with open(os.path.join(gitdir, "MERGE_HEAD")) as f:
            self.assertEqual(len(f.read().split()), 1,
                             "no merge is in progress, so this arm is about an "
                             "ordinary commit: %s"
                             % (merged.stdout + merged.stderr))

    def test_a_merge_is_not_asked_to_re_prove_the_other_sides_citation(self):
        """A CITATION THE OTHER SIDE WROTE IS NOT THIS COMMIT'S.

        `only added lines count` was measured against HEAD, which during a
        merge is the FIRST parent — so merging a trunk re-litigated every
        citation the trunk had added, and the ones this repo cannot re-prove (a
        lane sha that never reached a remote) refused the merge. MEASURED on
        the shipped rung before the cure, on this fixture: exit 1 naming the
        trunk's token. Same law and the same `commit_parents` as
        helm/nevertrack.py and helm/conflict_marker.py.
        """
        self.merge_a_trunk_citing(MUST_MISS)
        self.assertEqual(self.unaccounted(), [],
                         "the merge was refused for a citation its other "
                         "parent had already committed")
        # MUST-HIT on the SAME observable, in the same two-parent index: the
        # identical token IS unaccounted when this commit is the one adding it,
        # so the empty reading above is the parent rule and not a scanner that
        # returns [] whenever a merge is in progress.
        self.stage("mine.py", "# I cite it too: %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])

    def test_a_citation_the_RESOLUTION_writes_is_still_unaccounted(self):
        """THE MUST-HIT on the same two-parent index: without it the arm above
        is indistinguishable from a scanner that returns [] during any merge."""
        self.merge_a_trunk_citing(MUST_MISS)
        self.stage("mine.py", "# my own new citation %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()],
                         [MUST_MISS],
                         "a citation THIS merge added was admitted")

    def test_the_single_parent_base_did_not_move(self):
        """THE CONTROL: one parent, an added citation, still unaccounted."""
        self.stage("mine.py", "# cites %s\n" % MUST_MISS)
        self.assertEqual([t for _p, t in self.unaccounted()], [MUST_MISS])


if __name__ == "__main__":
    if "--audit" in sys.argv[1:]:
        sys.exit(_audit_main())
    unittest.main()
