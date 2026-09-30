"""--help honesty — an unknown verb NEVER exits 0.

Pins the fix that retired the fleet workaround (store prior
help-exit0-masks-unknown-verb): `helm <verb> --help` is a trustworthy
existence probe again — unknown verbs refuse with exit 2 on stderr, known
verbs print their own usage line, and only the bare/global forms dump the
verb table. Plus one honesty test per swept sub-dispatcher that used to
swallow unknown subverbs and run its default action with exit 0.
"""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import cli  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# THE ROOT LISTING'S BUDGET, owned here and not by the module that prints it,
# so the producer cannot loosen its own bound. `helm --help` is the first thing
# any model runs. Every full entry printed whole costs about 120 KB; one line
# per verb, carrying its subverbs and positional args ahead of its first
# sentence, costs about 11.9 KB for 113 verbs. The bound leaves room for new
# verbs and refuses any listing that prints full entries again.
LISTING_BUDGET_BYTES = 13500
LISTING_LINE_COLUMNS = 120


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliHelpHonestyTest(unittest.TestCase):
    def test_unknown_verb_with_help_exits_2(self):
        # THE regression: --help after an unknown verb must NOT fall through
        # to the global usage with exit 0 (the false existence probe).
        rc, out, err = _run(["frobnicate", "--help"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'frobnicate'", err)
        self.assertNotIn("usage: helm <verb>", out)

    def test_unknown_verb_bare_exits_2_with_suggestion(self):
        rc, _, err = _run(["projcts"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'projcts'", err)
        self.assertIn("did you mean 'projects'?", err)

    def test_known_verb_help_prints_verb_usage_not_global(self):
        rc, out, _ = _run(["store", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("store list|get", out)
        self.assertNotIn("steering station", out)

    def test_dispatch_help_renders_force_on_the_send_synopsis(self):
        rc, out, err = _run(["dispatch", "--help"])
        self.assertEqual(rc, 0, err)
        rendered = out + err
        send_help = rendered.split("|add", 1)[0]
        self.assertIn("dispatch send", send_help)
        self.assertIn("[--force]", send_help)

    def test_global_help_forms_exit_0(self):
        for argv in ([], ["--help"], ["-h"], ["help"]):
            rc, out, _ = _run(argv)
            self.assertEqual(rc, 0, argv)
            self.assertIn("usage: helm <verb>", out)


def _entry(verb):
    """What `helm <verb> --help` prints after `helm `: the verb's entry in the
    help table, else the first line of its handler's docstring."""
    from helm import cli_help
    return (cli_help._VERB_HELP.get(verb)
            or (cli.VERBS[verb].__doc__ or verb).strip().split("\n")[0])


# THE ROOT LISTING'S HEAD WIDTH, owned here beside the line and byte bounds: a
# verb's usage head takes at most this many columns, a cut one included.
LISTING_HEAD_COLUMNS = 56

# THE TEST'S OWN READING of a usage head, by regex where the producer scans
# bracket depth, so the arm that compares them does not only agree with the
# helper it checks: innermost groups first, each with the `...` repeating it.
_GROUP = re.compile(r"(\[[^][(){}]*\]|\([^][(){}]*\)|\{[^][(){}]*\})"
                    r"((?:\.\.\.)?)")

# A pipe TYPED inside one argument, which separates no forms: in a
# <placeholder>, in a "quoted" argument, or spaced between two placeholders
# (`add <type> <id> | <stmt>`).
_TYPED = re.compile(r'<[^<>]*>|"[^"]*"|(?<=>) \| (?=<)')
_SUBVERB = r"[a-z][a-z0-9-]*"

# Marks in the working text: a typed pipe, a kept group's open and close, and
# the place a dropped group stood (so no later group reads as first after it).
_PIPE, _KEEP, _SHUT, _GONE = "\0", "\1", "\2", "\3"


def _read_group(m):
    """One innermost group: KEPT when it is a `[...]` whose every form begins
    with a subverb and nothing but subverbs stand before it in its form, else
    taken out with the `...` that repeats it."""
    group, dots = m.group(1), m.group(2)
    before = re.split(r"[|\[({%s]" % _KEEP, m.string[:m.start()])[-1]
    forms = [" ".join(f.replace(_GONE, "").split())
             for f in group[1:-1].split("|")]
    if (group[0] == "[" and all(re.match(_SUBVERB + "(?: |$)", f)
                                for f in forms)
            and all(re.fullmatch(_SUBVERB, t) for t in before.split())):
        return _KEEP + _PIPE.join(forms) + _SHUT + dots
    return _GONE


def _whole(form):
    """Whether one form is a single kept group, open to close."""
    depth = 0
    for i, ch in enumerate(form):
        depth += (ch == _KEEP) - (ch == _SHUT)
        if not depth:
            return form[0] == _KEEP and i == len(form) - 1
    return False


def _compact_head(verb):
    """The verb's full entry with every bracketed group taken out but an
    optional subverb group (a `[...]` of subverbs standing where a subverb
    would), cut before its first ` — `, less the verb's own name at the front
    of any form, spaces collapsed and the emptied forms dropped, a typed pipe
    kept with its spaces; when a form empties or is only an optional group, the
    verb runs bare and the whole head is one optional group. This is the whole
    usage head a listing line may show a prefix of."""
    text = _TYPED.sub(lambda m: m.group(0).replace("|", _PIPE), _entry(verb))
    n = 1
    while n:
        text, n = _GROUP.subn(_read_group, text)
    usage = text.split(" — ", 1)[0] if " — " in text else ""
    forms = [re.sub(r"^%s(?: |$)" % re.escape(verb), "",
                    " ".join(f.replace(_GONE, "").split()))
             for f in usage.split("|")]
    bare = any(not f or _whole(f) for f in forms)
    head = "|".join(f[1:-1] if _whole(f) else f for f in forms if f)
    head = "[%s]" % head if head and bare else head
    for mark, ch in ((_PIPE, "|"), (_KEEP, "["), (_SHUT, "]")):
        head = head.replace(mark, ch)
    return head


def _uncut(text):
    """`text` less a trailing cut mark (always right after a non-space, never
    the ` ...` a usage writes) and the `]`s a cut head closes after it."""
    m = re.fullmatch(r"(.*\S)\.\.\.\]*", text)
    return m.group(1) if m else text


class OneLinePerVerbListingTest(unittest.TestCase):
    """`helm --help` lists every verb on ONE line and points at the full text;
    `helm <verb> --help` prints that verb's full entry whole."""

    def _listing(self, argv):
        rc, out, err = _run(argv)
        self.assertEqual(rc, 0, err)
        head, sep, body = out.partition("usage: helm <verb> [args]\n")
        self.assertEqual(sep, "usage: helm <verb> [args]\n", out[:200])
        lines = body.splitlines()
        verbs = [ln for ln in lines if ln.startswith("  ")]
        rest = [ln for ln in lines if ln.strip() and not ln.startswith("  ")]
        return out, verbs, rest

    def test_the_root_listing_is_one_line_per_verb_under_the_budget(self):
        out, verbs, rest = self._listing(["--help"])
        self.assertIn("usage: helm <verb> [args]", out)
        size = len(out.encode("utf-8"))
        # a floor as well as a ceiling: a hundred-odd verb names alone
        # outweigh this, so an empty or truncated listing cannot pass
        self.assertGreater(size, 2000)
        self.assertLessEqual(size, LISTING_BUDGET_BYTES,
                             "helm --help prints %d bytes, over the %d-byte "
                             "budget" % (size, LISTING_BUDGET_BYTES))
        wide = [ln for ln in out.splitlines()
                if len(ln) > LISTING_LINE_COLUMNS]
        self.assertEqual(wide, [], "listing lines wider than %d columns"
                         % LISTING_LINE_COLUMNS)
        # ONE closing line, last, and it names the door to the full text.
        self.assertEqual(len(rest), 1, rest)
        self.assertIn("helm <verb> --help", rest[0])
        self.assertTrue(out.rstrip("\n").endswith(rest[0]), out[-200:])
        # EXACTLY the dispatch table, in its order, once each: every
        # _VERB_HELP key is a verb, and the docstring-only verbs are listed
        # too, so no verb is missing and none is printed twice.
        heads = [ln.split()[0] for ln in verbs]
        self.assertIn("preread", heads)
        self.assertEqual(heads, list(cli.VERBS))
        from helm import cli_help
        self.assertTrue(set(cli_help._VERB_HELP) <= set(heads),
                        sorted(set(cli_help._VERB_HELP) - set(heads)))
        # bare `helm` and every help spelling print this same listing
        for argv in ([], ["-h"], ["help"]):
            self.assertEqual(self._listing(argv)[0], out, argv)

    def test_every_line_is_cut_from_its_own_full_entry(self):
        """ONE SOURCE OF TRUTH: a line says nothing its full entry does not.
        Each line is `  <verb> <head> — <sentence>` (no head when the verb
        takes no subverb or argument). The head, less a trailing `...` cut
        mark and the `]`s it closes, begins the entry's usage compacted (its
        bracketed groups taken out but an optional subverb group); the
        sentence, less the cut mark, is a substring of the entry. Neither can
        drift from the text `helm <verb> --help` prints."""
        _out, verbs, _rest = self._listing(["--help"])
        rows = []
        for ln in verbs:
            verb, _sp, rest = ln[2:].partition(" ")
            head, _sep, text = (("", "", rest[2:]) if rest.startswith("— ")
                                else rest.partition(" — "))
            self.assertTrue(text, ln)
            rows.append((verb, head, text))
        self.assertEqual([verb for verb, _h, _t in rows], list(cli.VERBS))
        got = {verb: (head, text) for verb, head, text in rows}
        # pinned literally: a docstring-only verb with no head and its
        # sentence's closing stop trimmed, and the heads that other suites
        # read this listing for (a seat's subverbs, stale's two doors)
        self.assertEqual(got["home"], ("", "print the resolved ~/.helm root"))
        self.assertEqual(got["stale"][0],
                         "sweep|redispatch <dispatch-id> --reviewer "
                         "<current-seat>")
        self.assertTrue(got["seat"][0].startswith(
            "add|up|down|launch|spawn|where|rehome <seat> "), got["seat"])
        self.assertTrue(got["dispatch"][0].startswith(
            "send <recipient> <lane> "), got["dispatch"])
        # THE FIRST SENTENCE, pinned on entries whose description runs on
        # past it: the substring arm below admits a listing that prints the
        # whole description cut at the column (measured: 12,393 bytes, under
        # the budget, every line a substring of its entry). `brief` pins the
        # bracket-depth stop — its only `. ` sits inside a parenthesis.
        self.assertEqual(got["dispatch"][1], "durable DISPATCH ledger")
        self.assertEqual(got["gate"][1], "MINT a suite result")
        self.assertTrue(got["brief"][1].startswith(
            "the operator's morning brief: sessions, knowledge delta (incl. "),
            got["brief"])
        drifted = [(verb, head, text) for verb, head, text in rows
                   if not _compact_head(verb).startswith(_uncut(head))
                   or _uncut(text) not in _entry(verb)]
        self.assertEqual(drifted, [])

    def _heads(self):
        """{verb: (head, sentence)} read off the root listing's lines."""
        got = {}
        for ln in self._listing(["--help"])[1]:
            verb, _sp, rest = ln[2:].partition(" ")
            head, _sep, text = (("", "", rest[2:]) if rest.startswith("— ")
                                else rest.partition(" — "))
            got[verb] = (head, text)
        self.assertEqual(list(got), list(cli.VERBS))
        return {verb: head for verb, (head, _text) in got.items()}

    def test_a_verb_that_runs_bare_shows_its_subverbs_optional(self):
        """A verb whose usage admits its bare form (`ship [--apply]`, `codex
        [list]`) shows its subverbs as ONE optional group, never as if one were
        required; a verb that takes only subverbs keeps them required, and a
        verb that takes none has no head."""
        got = self._heads()
        # the ten a reader found reading as if a subverb were required
        self.assertEqual(got["ship"], "[pull|hosts]")
        self.assertEqual(got["record"], "[status|install|swallows]")
        self.assertEqual(got["todos"], "[promote <id>|demote]")
        self.assertEqual(got["friction"], "[record <guard>|dial]")
        self.assertEqual(got["scratch"],
                         "[small|big|durable|gc|unattributable|status]")
        for verb, start in (
                ("burn",
                 "[why <family>|burst|runway|calibrate|declare"),
                ("proxywatch",
                 "[vendor-reset set <family> <ISO-8601|epoch-ms>"),
                ("codex", "[list|pool <name>|unpool <name>|pooled|"),
                ("accounts", "[show <id>|set <id> --vendor V "),
                ("projects", "[state|residency|forget <name>|")):
            self.assertTrue(got[verb].startswith(start)
                            and got[verb].endswith("]"), (verb, got[verb]))
        # only subverbs: required, no brackets; no subverbs at all: no head
        self.assertTrue(got["store"].startswith("list|get|resolve|pinned|"),
                        got["store"])
        self.assertEqual(got["cell"], "join|send|status")
        # four a sweep of the handlers' no-argument branches found running
        # bare under a head that read required, each read from source and
        # never run here (bare `reflex` lists, `mcpd` serves, `chat` reads
        # the room, `env` takes the census)
        self.assertEqual(got["reflex"],
                         "[list|add|retire|rescope <id> <project|fleet|->"
                         "|smoke]")
        self.assertEqual(got["mcpd"], "[serve|token]")
        self.assertEqual(got["env"], "[census]")
        self.assertTrue(got["chat"].startswith("[read|post|rooms|")
                        and got["chat"].endswith("]"), got["chat"])
        # `helm hooks` bare REFUSES (usage, exit 2), so its entry brackets
        # no subverb and its head is required: the one entry whose brackets
        # a review found saying bare when the verb does not run bare
        self.assertEqual(_run(["hooks"])[0], 2)
        self.assertEqual(got["hooks"],
                         "install|status|latency|sync|run <EVENT>|preflight...")
        self.assertEqual((got["home"], got["gc"], got["sessions"]),
                         ("", "", ""))

    def test_an_optional_subverb_group_stays_and_a_flag_group_goes(self):
        """A `[...]` group whose every form begins with a subverb, standing
        where a subverb would, is kept; a flag group, a metavariable group and
        a group after other groups are taken out. The uncut head of EVERY verb
        is exactly this suite's own regex reading of its entry."""
        from helm import cli_help
        full = {verb: cli_help.usage_head(verb, _entry(verb), width=10 ** 6)
                for verb in cli.VERBS}
        self.assertTrue(full["seat"].endswith(
            "|lifecycle [show|record]|list|status"), full["seat"])
        self.assertIn("|stash [list|apply|pop|drop|show <message-substring>]"
                      "|install-guard", full["work"])
        self.assertTrue(full["codex"].startswith("[list|pool <name>|"))
        got = self._heads()
        self.assertEqual(got["away"], "[off|status]")
        self.assertEqual(got["skills"], "[dupes|sync]")
        self.assertEqual(got["creds"], "[crosscheck]")
        self.assertEqual(got["lineage"], "[seed|add|external|archive-report]")
        # a flag group (`[--dry | --apply]`), a metavariable group
        # (`[<project>]`, `[@occurrence]`) and `launch`'s `[claude args…]`,
        # which stands after its flag groups, never show
        self.assertEqual((full["gc"], full["sessions"], full["launch"]),
                         ("", "", ""))
        self.assertEqual(full["prune"], "<sid>")
        self.assertIn("|receipts <broadcast-id>|node ", full["chat"])
        self.assertNotIn("--name", full["scratch"])
        self.assertEqual({verb: head for verb, head in full.items()
                          if head != _compact_head(verb)}, {})

    def test_a_typed_pipe_is_not_an_alternation(self):
        """A pipe typed inside one argument keeps its spaces (`<id> |
        <statement>`) or its quotes, so it never reads as a choice between
        forms; a spaced pipe between forms, and a pipe inside a placeholder,
        read as before."""
        from helm import cli_help
        got = self._heads()
        self.assertEqual(got["premise"], "<id> | <statement>")
        self.assertTrue(got["mentor"].startswith(
            'observe <project>|teach <project> "<id> | <steer>"'),
            got["mentor"])
        store = cli_help.usage_head("store", _entry("store"), width=10 ** 6)
        self.assertIn("|add <type> <id> | <stmt>|keywords <id>|", store)
        self.assertNotIn("<id>|<stmt>", got["store"])
        self.assertTrue(got["preread"].startswith(
            "<dispatch-id>|--ref <sha> --repo <path>"), got["preread"])
        self.assertTrue(got["dispatch"].startswith(
            "send <recipient> <lane> <message...|body on stdin>"))

    def test_a_cut_head_fits_its_columns_and_closes_what_it_opened(self):
        """Every head fits LISTING_HEAD_COLUMNS, a cut one included, and a cut
        closes the optional group it cut into after the cut mark; no cut
        splits a placeholder or a quoted argument."""
        got = self._heads()
        self.assertEqual(got["projects"],
                         "[state|residency|forget <name>|restore <name>...]")
        self.assertTrue(got["homes"].startswith(
            "[prepare|provision|verify|archive|restore|")
            and got["homes"].endswith("...]"), got["homes"])
        self.assertTrue(got["configs"].startswith("[list|show <path>|")
                        and got["configs"].endswith("...]"), got["configs"])
        bad = {verb: head for verb, head in got.items()
               if len(head) > LISTING_HEAD_COLUMNS
               or head.count("[") != head.count("]")
               or head.count("<") != head.count(">")
               or head.count('"') % 2}
        self.assertEqual(bad, {})

    def test_helm_help_verb_prints_what_verb_help_prints(self):
        """`helm help <verb>` is `helm <verb> --help` for every verb, `helm
        help` alone is the listing, and an unknown verb is refused the same
        way on both doors."""
        self.assertEqual(_run(["help"]), _run(["--help"]))
        got = {verb: _run(["help", verb]) for verb in cli.VERBS}
        self.assertEqual(got["seat"][:2], (0, "helm " + _entry("seat") + "\n"))
        self.assertEqual([verb for verb, res in got.items()
                          if res != _run([verb, "--help"])], [])
        rc, out, err = _run(["help", "frobnicate"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("unknown verb 'frobnicate'", err)
        self.assertEqual(_run(["frobnicate", "--help"]), (rc, out, err))

    def test_a_flag_after_help_names_no_verb(self):
        """`helm help --json` answers as `helm --help --json` does, with the
        listing at exit 0: a flag after `help` names no verb, so it is never
        refused as `unknown verb '--json'`. A word after `help` still names
        a verb, and the listing's closing line and docs/VERBS.md name both
        doors to a verb's full entry."""
        listing = _run(["--help"])
        self.assertEqual(listing[0], 0)
        self.assertIn("usage: helm <verb>", listing[1])
        for flag in ("--json", "--bogus", "-x", "-h", "--help"):
            self.assertEqual(_run(["help", flag]), _run(["--help", flag]),
                             flag)
            self.assertEqual(_run(["help", flag]), listing, flag)
        # CONTROL: the word form still reaches a verb and still refuses one
        # the table does not have
        self.assertEqual(_run(["help", "seat", "--json"])[:2],
                         (0, "helm " + _entry("seat") + "\n"))
        self.assertEqual(_run(["help", "frobnicate"])[0], 2)
        closing = listing[1].rstrip("\n").splitlines()[-1]
        self.assertIn("helm <verb> --help", closing)
        self.assertIn("helm help <verb>", closing)
        with open(os.path.join(ROOT, "docs", "VERBS.md"),
                  encoding="utf-8") as f:
            intro = f.read().split("\n## ", 1)[0]
        self.assertIn("`helm <verb> --help`", intro)
        self.assertIn("`helm help <verb>`", intro)

    def test_an_abbreviation_ends_no_sentence(self):
        """`incl. ` inside a first sentence does not end it."""
        from helm import cli_help
        self.assertIn("shared-checkout guards incl. the never-track staged-set",
                      cli_help.summary(_entry("work")))
        self.assertEqual(
            [verb for verb in cli.VERBS
             if re.search(r"\b(?:incl|e\.g|i\.e|cf|vs|viz)$",
                          cli_help.summary(_entry(verb)))], [])

    def test_verb_help_prints_the_full_entry_unchanged(self):
        """The full text lives in helm/cli_help.py and prints whole. Every
        reader's spelling of the table (`cli._VERB_HELP`, a from-import off
        cli, the module itself) is the one same object."""
        from helm import cli_help
        from helm.cli import _VERB_HELP
        self.assertIn("preread", cli_help._VERB_HELP)
        self.assertIn("preread", cli._VERB_HELP)
        self.assertIn("preread", _VERB_HELP)
        self.assertIs(cli._VERB_HELP, cli_help._VERB_HELP)
        self.assertIs(_VERB_HELP, cli_help._VERB_HELP)
        outs = {(verb, flag): _run([verb, flag])
                for verb in cli.VERBS for flag in ("--help", "-h")}
        self.assertEqual(len(outs), 2 * len(cli.VERBS))
        # the docstring fallback and a table entry, each pinned literally so
        # the arm does not only agree with the helper it shares a source with
        self.assertEqual(outs[("home", "--help")][:2],
                         (0, "helm home — print the resolved ~/.helm root.\n"))
        self.assertTrue(outs[("preread", "-h")][1].startswith(
            "helm preread <dispatch-id> | --ref <sha> --repo <path> "))
        wrong = [key for key, (rc, out, _err) in outs.items()
                 if (rc, out) != (0, "helm " + _entry(key[0]) + "\n")]
        self.assertEqual(wrong, [])

    def test_a_refused_tail_on_a_no_arg_verb_still_quotes_its_full_entry(self):
        got = {verb: _run([verb, "--bogus"]) for verb in cli.NOARG_VERBS}
        self.assertIn("(home — print the resolved ~/.helm root.)",
                      got["home"][2])
        self.assertEqual(
            {verb: (rc, out, "unknown arg '--bogus' (%s)" % _entry(verb) in err)
             for verb, (rc, out, err) in got.items()},
            {verb: (2, "", True) for verb in cli.NOARG_VERBS})

    def _probe(self, argv):
        """Run one real CLI call in a fresh interpreter and report whether
        the help table was imported or bound on cli. A fresh process is the
        only honest reading: this process imported cli_help already."""
        adopted = tempfile.mkdtemp(prefix="helm-test-adopted-")
        self.addCleanup(shutil.rmtree, adopted, True)
        code = ("import sys; sys.path.insert(0, %r); from helm import cli; "
                "rc = cli.main(sys.argv[1:]); "
                "print('PROBE', rc, 'helm.cli_help' in sys.modules, "
                "'_VERB_HELP' in vars(cli))" % ROOT)
        env = dict(os.environ, HELM_ADOPTED_DIR=adopted,
                   HELM_NO_TREE_WARNING="1")
        p = subprocess.run([sys.executable, "-c", code] + list(argv),
                           capture_output=True, text=True, cwd=ROOT, env=env,
                           timeout=60)
        probe = [ln for ln in p.stdout.splitlines() if ln.startswith("PROBE ")]
        self.assertEqual(len(probe), 1, (p.stdout[-400:], p.stderr[-400:]))
        return probe[0].split()[1:]

    def test_an_ordinary_verb_never_imports_the_help_table(self):
        # POSITIVE CONTROL first: the probe can see the import, because the
        # help paths do load it.
        self.assertEqual(self._probe(["--help"]), ["0", "True", "False"])
        self.assertEqual(self._probe(["home", "--help"]),
                         ["0", "True", "False"])
        # a no-arg verb (its tail guard quotes the entry only on refusal) and
        # a lazily dispatched one
        for argv in (["home"], ["store", "counts"]):
            self.assertEqual(self._probe(argv), ["0", "False", "False"], argv)


class DispatcherHonestyTest(unittest.TestCase):
    """Each swept dispatcher refuses an unknown subverb/arg with exit 2
    instead of silently running its default action."""

    def _refuses(self, argv, needle):
        rc, _, err = _run(argv)
        self.assertEqual(rc, 2, argv)
        self.assertIn(needle, err)

    def test_skills_unknown_subverb(self):
        self._refuses(["skills", "frobnicate"], "unknown verb 'frobnicate'")

    def test_creds_unknown_subverb(self):
        self._refuses(["creds", "frobnicate"], "unknown verb 'frobnicate'")

    def test_tidy_unknown_arg(self):
        self._refuses(["tidy", "frobnicate"], "unknown arg 'frobnicate'")

    def test_watchdog_unknown_arg(self):
        self._refuses(["watchdog", "frobnicate"], "unknown arg 'frobnicate'")

    def test_lr_help_names_every_terminal_annotation(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        rc, out, err = _run(["lr", "--help"])
        self.assertEqual(rc, 0, err)
        for verb in ("discharge", "withdraw", "abandon", "close-landed"):
            self.assertIn(verb, out)


if __name__ == "__main__":
    unittest.main()
