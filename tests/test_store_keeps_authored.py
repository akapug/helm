#!/usr/bin/env python3
"""Every store write verb keeps what a person WROTE in an entry file.

THE DEFECT (reported by a /learn agent on a scratch copy, reproduced on a temp
copy of the live adopted file prior-no-dup-prefer-visible-crew-over-shadow-sa):
each writer rebuilt the whole file from the parsed entry, and the parser keeps
only the keys its type reads. So `keywords --add`, `evidence`, `gloss --set`
and `revise` on an ADOPTED file (a premise drained from agent memory) replaced
its hand-written narrative, owner quotes included, with the two-line generated
template, and dropped `drained_from` and `origin_session`.

THE RULE UNDER TEST (helm/store/write.py, "authored content"): the writer owns
the keys its parser reads; every other frontmatter key is carried verbatim. A
body is regenerated only where it is exactly the writer's own template (a head
paragraph saying the frontmatter statement, then the fixed template lines);
everything after that span, or the whole body when it is not the template, is
kept byte-identical.

Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CACHE_DIR / HELM_CHAT_DIR are
tmp dirs; nothing reads or writes the live store.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import store  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC", "HELM_CHAT_NAME",
            "MELD_CHAT_NAME")

TS = "2026-09-25T12:00:00Z"
PID = "crew-keeps-its-subagents-visible"
STATEMENT = ("an organized crew keeps every subagent visible on the board so "
             "no seat duplicates another seat's work")

# THE NARRATIVE: several paragraphs, an owner quote with double quotes, a
# markdown list, a line that LOOKS like a template line, trailing spaces and
# no final newline — every byte a regenerating writer would normalize away.
NARRATIVE = (
    'David 2026-06-24: "I really don\'t see why, if you have such an organized '
    'dev team, there should be anyone duplicating work."\n'
    "\n"
    "**Root cause:** my off-band subagents were INVISIBLE to the crew on the "
    "board, so the navigator routed a pilot to build the same test.  \n"
    "\n"
    "**How to apply:**\n"
    "1. Prefer the visible crew.\n"
    "2. Announce and claim any subagent on the board.\n"
    "\n"
    "**Why a prior:** this line is prose, not the template.\n"
    "\n"
    "Sharpens [[feedback-idle-team-unblock-priority]].")

ADOPTED = (
    "---\n"
    "name: prior-" + PID + "\n"
    'description: "prior: ' + PID + " - " + STATEMENT[:120] + '"\n'
    "metadata:\n"
    "  node_type: memory\n"
    "  type: prior\n"
    "  id: " + PID + "\n"
    "  statement: " + STATEMENT + "\n"
    "  confidence: 0.90\n"
    "  class: prior\n"
    "  load_class: jit\n"
    '  evidence_log: [{"ts":"2026-07-18T10:07:47Z","type":"stated","delta":0.9,'
    '"reason":"drained from feedback memory","by":"human"}]\n'
    '  confidence_history: [{"ts":"2026-07-18T10:07:47Z","value":0.90,'
    '"reason":"drained from feedback memory"}]\n'
    "  status: live\n"
    "  keywords: subagent,visible,duplicates,organized\n"
    "  source: drain\n"
    "  stated_ts: 2026-07-18T10:07:47Z\n"
    "  last_updated: 2026-07-18T10:07:47Z\n"
    "  drained_from: feedback-crew-keeps-its-subagents-visible.md\n"
    "  origin_session: 0b5c1d2e-3f40-4a51-8b62-7c83d94ea5f6\n"
    "---\n" + NARRATIVE)

KEPT_KEYS = ("  drained_from: feedback-crew-keeps-its-subagents-visible.md",
             "  origin_session: 0b5c1d2e-3f40-4a51-8b62-7c83d94ea5f6")


def run_store(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


def split(text):
    """(frontmatter lines, body text) — the body is every byte after the
    second fence line."""
    lines = text.split("\n")
    fences = [i for i, l in enumerate(lines) if l.strip() == "---"]
    return lines[fences[0] + 1:fences[1]], "\n".join(lines[fences[1] + 1:])


class AuthoredBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-authored-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, name, text):
        p = os.path.join(os.environ["HELM_ADOPTED_DIR"], name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def read(self, p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def ok(self, *args):
        rc, out, err = run_store(*args)
        self.assertEqual(rc, 0, "helm store %s refused:\n%s%s" % (args, out, err))
        return out


class AdoptedPriorKeepsItsNarrative(AuthoredBase):
    """Each write verb, on a fresh adopted file: the narrative body is
    byte-identical and drained_from / origin_session survive. Mutation killed:
    any writer that rebuilds the body or drops a key its parser does not read
    (the pre-fix write_prior failed every arm)."""

    def assertKept(self, p):
        fm, body = split(self.read(p))
        self.assertEqual(body, NARRATIVE)
        for line in KEPT_KEYS:
            self.assertIn(line, fm)
        return fm

    def verb(self, *args):
        p = self.plant("prior-%s.md" % PID, ADOPTED)
        self.ok(*args)
        return p

    def test_keywords_add(self):
        fm = self.assertKept(self.verb("keywords", PID, "--add", "navigator"))
        self.assertIn("navigator", [c for l in fm if l.startswith("  keywords: ")
                                    for c in l.split(": ", 1)[1].split(",")])

    def test_keywords_remove(self):
        self.assertKept(self.verb("keywords", PID, "--remove", "organized"))

    def test_evidence(self):
        fm = self.assertKept(self.verb("evidence", TS, PID, "0.02", "seen again"))
        self.assertTrue(any("seen again" in l for l in fm))

    def test_gloss_set_then_clear(self):
        p = self.verb("gloss", PID, "--set", "keep every subagent visible on the board")
        fm = self.assertKept(p)
        self.assertIn("  gloss: keep every subagent visible on the board", fm)
        self.ok("gloss", PID, "--clear")
        fm = self.assertKept(p)
        self.assertFalse([l for l in fm if l.startswith("  gloss:")],
                         "a cleared gloss is a key the writer OWNS: it must go")

    def test_revise_stages_and_confirm_installs_in_frontmatter(self):
        new = ("an organized crew announces every subagent on the board before "
               "it starts, so no seat duplicates another seat's work")
        p = self.verb("revise", PID, new)
        fm = self.assertKept(p)
        self.assertTrue(any(l.startswith("  pending_revision: ") and new in l
                            for l in fm), "revise stages the new statement")
        self.assertIn("  statement: " + STATEMENT, fm, "revise keeps serving the old")
        self.ok("confirm", PID)
        fm = self.assertKept(p)
        # WHERE THE NEW STATEMENT GOES: the frontmatter statement line, which
        # is what every loader reads; the narrative never held a generated line.
        self.assertIn("  statement: " + new, fm)
        self.assertFalse([l for l in fm if l.startswith("  pending_revision:")])
        self.assertEqual(store._find(PID)["statement"], new)

    def test_rescope(self):
        self.assertKept(self.verb("rescope", PID, "fleet"))

    def test_retire(self):
        fm = self.assertKept(self.verb("retire", TS, PID))
        self.assertIn("  status: retired", fm)

    def test_demote(self):
        p = self.plant("prior-%s.md" % PID,
                       ADOPTED.replace("  load_class: jit", "  load_class: always"))
        self.ok("demote", PID, "the owner moved it to the jit lane")
        fm = self.assertKept(p)
        self.assertIn("  load_class: jit", fm)

    def test_doctor_fix_carries_from_the_entry_not_the_stage(self):
        """doctor --fix writes each repair to a STAGE file first; the carry
        must read the entry it replaces, not the (absent) stage."""
        salad = ADOPTED.replace("  keywords: subagent,visible,duplicates,organized",
                                "  keywords: subagent visible duplicates organized navigator")
        p = self.plant("prior-%s.md" % PID, salad)
        self.ok("doctor", "--fix")
        fm = self.assertKept(p)
        self.assertTrue(any(l.startswith("  keywords: ") and "," in l for l in fm),
                        "the space-CSV row really was repaired")

    def test_doctor_rekey_moves_the_narrative_with_the_entry(self):
        """A re-key writes the entry to a NEW file and a tombstone at the old
        one; the new file carries the old file's body and keys (it is the
        same entry, moved), and the tombstone keeps its own. The id typed
        twice is the shortest re-keyable shape whose file name follows it."""
        short = "crew-plan"
        old = self.plant("prior-%s-%s.md" % (short, short),
                         ADOPTED.replace("name: prior-" + PID,
                                         "name: prior-%s-%s" % (short, short))
                                .replace("  id: " + PID + "\n",
                                         "  id: %s %s\n" % (short, short)))
        self.ok("doctor", "--fix")
        new = os.path.join(os.environ["HELM_ADOPTED_DIR"], "prior-%s.md" % short)
        fm = self.assertKept(new)
        self.assertIn("  id: " + short, fm)
        fm = self.assertKept(old)
        self.assertIn("  replaced_by: " + short, fm)


class OtherWritersKeepTheirs(AuthoredBase):
    """The same carry holds for every writer, not only write_prior: an
    adopted heuristic, reference and lexicon keep their narrative and their
    upgraded_from key through `keywords --add`."""

    def check(self, name, text, *args):
        p = self.plant(name, text)
        self.ok(*args)
        fm, body = split(self.read(p))
        self.assertEqual(body, NARRATIVE)
        self.assertIn("  upgraded_from: feedback-x.md", fm)

    def test_heuristic(self):
        self.check("heuristic-board-first.md",
                   "---\nname: heuristic-board-first\ndescription: \"heuristic\"\n"
                   "metadata:\n  node_type: memory\n  type: heuristic\n"
                   "  id: board-first\n  move: announce a subagent on the board first\n"
                   "  trigger: subagent,announce\n  status: live\n  source: drain\n"
                   "  upgraded_from: feedback-x.md\n---\n" + NARRATIVE,
                   "keywords", "board-first", "--type", "heuristic", "--add", "navigator")

    def test_reference(self):
        self.check("ref-board-guide.md",
                   "---\nname: ref-board-guide\ndescription: \"reference\"\n"
                   "metadata:\n  node_type: memory\n  type: reference\n"
                   "  id: board-guide\n  summary: how the crew board routes work\n"
                   "  keywords: board,routes\n  status: live\n  source: drain\n"
                   "  upgraded_from: feedback-x.md\n---\n" + NARRATIVE,
                   "keywords", "board-guide", "--type", "reference", "--add", "navigator")

    def test_lexicon(self):
        self.check("lex-shadow-work.md",
                   "---\nname: lex-shadow-work\ndescription: \"lexicon\"\n"
                   "metadata:\n  node_type: memory\n  type: lexicon\n"
                   "  term: shadow-work\n  scope: global\n  kind: phrase\n"
                   "  source: drain-upgrade\n  updated_ts: 2026-07-19T00:00:00Z\n"
                   "  hits: 0\n  definition: work the coordinator cannot see\n"
                   "  keywords: shadow,coordinator\n"
                   "  upgraded_from: feedback-x.md\n---\n" + NARRATIVE,
                   "keywords", "shadow-work", "--type", "lexicon", "--add", "navigator")


class GeneratedBodiesStillRegenerate(AuthoredBase):
    """The generated-body entries keep working exactly as before: the body is
    the template and follows the statement. Mutation killed: a carry that
    treats every existing body as hand-written (a stale PRIOR: line would
    survive a confirmed revision)."""

    def seed(self, pid, statement):
        return store.write_prior({"id": pid, "statement": statement,
                                  "confidence": 0.6, "keywords": "ferry,harbor",
                                  "stated_ts": TS, "source": "human"},
                                 root_dir=os.path.join(os.environ["HELM_HOME"],
                                                       "_global", "premises"))

    def test_write_is_byte_identical_to_a_fresh_write(self):
        p = self.seed("ferry-runs-hourly", "the harbor ferry runs every hour")
        self.ok("keywords", "ferry-runs-hourly", "--add", "timetable")
        now = self.read(p)
        e = store._find("ferry-runs-hourly")
        fresh = os.path.join(self.tmp, "fresh", os.path.basename(p))
        store.write_prior(dict(e), path=fresh)
        self.assertEqual(now, self.read(fresh))

    def test_confirmed_revision_rewrites_the_head_line(self):
        p = self.seed("ferry-runs-hourly", "the harbor ferry runs every hour")
        self.ok("revise", "ferry-runs-hourly", "the harbor ferry runs every half hour")
        self.ok("confirm", "ferry-runs-hourly")
        _fm, body = split(self.read(p))
        self.assertIn("PRIOR: the harbor ferry runs every half hour\n", body)
        self.assertNotIn("every hour", body)

    def test_a_statement_with_double_quotes_still_reads_as_generated(self):
        """The writer folds " to ' in the frontmatter but not in the body, so
        the head check compares folded text."""
        p = self.seed("ferry-quote", 'the ferry sign says "hourly" today')
        self.ok("revise", "ferry-quote", "the ferry sign says half-hourly today")
        self.ok("confirm", "ferry-quote")
        _fm, body = split(self.read(p))
        self.assertIn("PRIOR: the ferry sign says half-hourly today\n", body)
        self.assertNotIn("hourly\" today", body)

    def test_paragraphs_appended_below_the_template_survive_a_revision(self):
        p = self.seed("ferry-runs-hourly", "the harbor ferry runs every hour")
        note = "\n**Measured 2026-09-01:** the 07:00 boat was cancelled twice.\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(note)
        self.ok("revise", "ferry-runs-hourly", "the harbor ferry runs every half hour")
        self.ok("confirm", "ferry-runs-hourly")
        _fm, body = split(self.read(p))
        self.assertTrue(body.startswith("\nPRIOR: the harbor ferry runs every half hour\n"))
        self.assertTrue(body.endswith(note), body)

    def test_a_note_written_directly_under_the_head_line_is_not_template(self):
        """The head paragraph is template only when it says the statement and
        nothing more; a note typed straight under it makes the whole body
        hand-written, kept byte-identical."""
        p = self.seed("ferry-runs-hourly", "the harbor ferry runs every hour")
        text = self.read(p).replace(
            "PRIOR: the harbor ferry runs every hour\n",
            "PRIOR: the harbor ferry runs every hour\n(except on the 25th)\n")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        _fm, before = split(text)
        self.ok("keywords", "ferry-runs-hourly", "--add", "timetable")
        _fm, after = split(self.read(p))
        self.assertEqual(after, before)



class NearTemplateLinesAreKept(AuthoredBase):
    """A fixed template line that carries a VALUE (the heuristic's trigger,
    the reference's source) is template only when it says what the file's own
    frontmatter says — the head paragraph's rule, one line down. Mutation
    killed: the pre-cure `.*` slot read a hand-edited trigger line and a
    hand-cited source line as generated and replaced both."""

    HEUR = ("---\nname: heuristic-board-first\ndescription: \"heuristic\"\n"
            "metadata:\n  node_type: memory\n  type: heuristic\n"
            "  id: board-first\n  move: announce a subagent on the board first\n"
            "  trigger: subagent,announce\n  status: live\n  source: human\n---\n")
    HEUR_BODY = (
        "\nHEURISTIC (a cross-domain MOVE you APPLY, not a belief you hold): "
        "announce a subagent on the board first\n\n"
        "**Trigger pattern** (when it fires): %s\n\n"
        "**Why a heuristic, not a premise:** this is a STRATEGY you reach for "
        "across domains (confidence=1 by construction), distinct from a "
        "premise/prior (a belief that gates). A reflex is this move compiled to "
        "fire every turn. It surfaces JIT when its trigger pattern appears in a "
        "turn - never added to the always-on digest.\n")

    def test_a_hand_edited_trigger_line_keeps_the_whole_body(self):
        body = self.HEUR_BODY % ("subagent,announce -- and when a pilot idles "
                                 "(David 2026-09-01)")
        p = self.plant("heuristic-board-first.md", self.HEUR + body)
        self.ok("keywords", "board-first", "--type", "heuristic", "--add", "navigator")
        _fm, after = split(self.read(p))
        self.assertEqual(after, body)

    def test_a_generated_trigger_line_still_follows_the_keywords(self):
        p = self.plant("heuristic-board-first.md",
                       self.HEUR + self.HEUR_BODY % "subagent,announce")
        self.ok("keywords", "board-first", "--type", "heuristic", "--add", "navigator")
        _fm, after = split(self.read(p))
        self.assertIn("(when it fires): subagent,announce,navigator\n", after)
        self.assertEqual(after.count("**Trigger pattern**"), 1, after)

    def test_a_hand_cited_source_line_survives(self):
        head = ("---\nname: ref-board-guide\ndescription: \"reference\"\n"
                "metadata:\n  node_type: memory\n  type: reference\n"
                "  id: board-guide\n  summary: how the crew board routes work\n"
                "  keywords: board,routes\n  status: live\n  source: human\n---\n")
        cite = "Source: David's notebook, 2026-08-02, page 5"
        p = self.plant("ref-board-guide.md",
                       head + "\nREFERENCE: how the crew board routes work\n\n"
                       + cite + "\n")
        self.ok("keywords", "board-guide", "--type", "reference", "--add", "navigator")
        _fm, after = split(self.read(p))
        self.assertIn("\n" + cite + "\n", after)


class EveryFrontmatterLineIsCarried(AuthoredBase):
    """The writer drops only the key lines it owns. A comment, a YAML list
    under an unknown key, a key the flat grammar cannot read (a hyphen, a
    digit) and a line above the opening fence are someone's writing. Mutation
    killed: the pre-cure carry kept only `[A-Za-z_]+:` lines and started at
    the opening fence."""

    def test_non_key_lines_and_a_preamble_survive(self):
        extra = ("  # owner: keep this entry even when it goes quiet\n"
                 "  tags:\n    - crew\n    - board\n"
                 "  drained-from: feedback-crew.md\n  sha256: 9f2c\n")
        text = "Kept above the fence.\n" + ADOPTED.replace(
            "  origin_session:", extra + "  origin_session:")
        p = self.plant("prior-%s.md" % PID, text)
        self.ok("keywords", PID, "--add", "navigator")
        now = self.read(p)
        self.assertTrue(now.startswith("Kept above the fence.\n---\n"), now[:80])
        fm, body = split(now)
        self.assertEqual(body, NARRATIVE)
        for line in extra.rstrip("\n").split("\n") + list(KEPT_KEYS):
            self.assertIn(line, fm)
        self.assertEqual(store._find(PID)["statement"], STATEMENT)


class UnreadableOriginRefuses(AuthoredBase):
    def test_non_utf8_origin_refuses_rather_than_dropping_the_body(self):
        """FAIL-CLOSED: a writer that cannot read the body it replaces cannot
        keep it, so it refuses and the file keeps every byte."""
        p = os.path.join(os.environ["HELM_ADOPTED_DIR"], "prior-%s.md" % PID)
        raw = ADOPTED.encode("utf-8") + b"\n\xff\xfe a byte no decoder keeps\n"
        with open(p, "wb") as f:
            f.write(raw)
        e = store._find(PID)
        e["keywords"] += ",navigator"
        with self.assertRaisesRegex(ValueError, "cannot be read as UTF-8"):
            store.write_prior(e, path=p)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), raw)


if __name__ == "__main__":
    unittest.main()
