#!/usr/bin/env python3
"""mode "proxy-oauth" — the third seat auth shape, for families whose PROXY
holds the OAuth itself.

The three modes and what actually distinguishes them:
  proxy      (codex)        auth is TRANSLATED out of a sibling CLI's cred store
  proxy-key  (kimi/ds4pro)  a bearer is BAKED into the seat's 0600 config
  proxy-oauth(gemini/grok)  the proxy AUTHENTICATES ITSELF via its own login flag

The third had no implementation, which is why `helm seat add gemini` could not
work — and worse, why the two council proxies ran for a day out of
~/.helm/research/, outside the seat system entirely. Nothing supervised them,
nothing watched their credentials, and the grok cred was sitting at 0664
(group- and world-readable) with no one to notice.

The invariant under test in both directions: a seat that CANNOT AUTHENTICATE is
not a seat, and must not be reported as one.
"""
import copy
import inspect
import os
import shutil
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import autocompact, seat  # noqa: E402


def _floor_only(**over):
    """gemini's entry with its OWNER STATEMENT stripped — the observed-floor
    grade standing alone, which is the only grade OBSERVED_FLOOR_HEADROOM
    binds. The shipped entry carries both keys, and the owner grade backs the
    pin, so a mutant built on `dict(FAMILIES["gemini"], ...)` is admitted by
    the owner arm and proves nothing about the floor arms. Every floor
    assertion has to come through here."""
    fam = {k: v for k, v in seat.FAMILIES["gemini"].items()
           if k != "owner_stated_window"}
    fam.update(over)
    return fam


class ProxyOAuthBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-oauth-")
        self.prior = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        self.donor = os.path.join(self.tmp, "donor")
        os.makedirs(self.donor)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant_cred(self, name="antigravity-someone@example.com.json", mode=0o600,
                   where=None):
        d = where or self.donor
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        # A PLAUSIBLE credential, matching the field names the real files use.
        # The first version wrote {"type":..., "refresh":...} — `refresh`, not
        # `refresh_token` — which is not a credential shape at all, and nothing
        # noticed because adoption validated only the FILENAME. The content check
        # kimi's review added rejected this fixture immediately, which is the
        # check working: a test fixture that could never authenticate was
        # standing in for one that could.
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"type":"antigravity","access_token":"ya29.test",'
                    '"refresh_token":"1//test","disabled":false,'
                    '"expired":"2099-01-01T00:00:00Z"}\n')
        os.chmod(p, mode)
        return p


class FamiliesTest(unittest.TestCase):
    def test_both_council_families_are_registered(self):
        for fam in ("gemini", "grok"):
            self.assertIn(fam, seat.FAMILIES)
            self.assertEqual(seat.FAMILIES[fam]["mode"], "proxy-oauth")
            self.assertTrue(seat.FAMILIES[fam]["login_flag"].startswith("-"))
            self.assertTrue(seat.FAMILIES[fam]["auth_glob"].endswith(".json"))

    def test_the_defaults_are_the_MEASURED_models_not_the_highest_versions(self):
        """A 70-probe sweep graded every live model on a task whose correct
        answer was REFUTE. `grok-4.5` — the pick anyone would make by name, and
        the one the draft FAMILIES entry guessed — CONFIRMED A FALSE CLAIM, which
        is the exact failure a council exists to defeat. `gemini-3-pro`, the other
        guess, does not exist on this endpoint at all.

        This pins the outcome of that measurement so a later 'surely the newer
        one is better' edit has to argue with the evidence.

        GROK UPDATED TO grok-build-0.1 on the OWNER'S instruction (2026-07-25:
        "we want 4.5+ or grok build 0.1+"). The evidence still binds on WHICH
        newer model: NOT grok-4.5, because that is the one the sweep caught
        confirming a false claim. build-0.1 is newer, satisfies the ask, and was
        not among the models that folded. So the rule this test protects is
        intact — a higher version number never justifies itself, and the owner's
        preference selected among models the evidence had already cleared.

        The sweep is a MEASUREMENT, not an approved policy; the owner has said so
        explicitly. It informs the default and does not override him."""
        self.assertEqual(seat.FAMILIES["gemini"]["model"], "gemini-3.6-flash-high")
        self.assertEqual(seat.FAMILIES["grok"]["model"], "grok-build-0.1")
        self.assertNotEqual(seat.FAMILIES["grok"]["model"], "grok-4.5",
                            "grok-4.5 confirmed a false claim in the sweep")
        for fam in ("gemini", "grok"):
            self.assertEqual(seat.FAMILIES[fam]["probe_models"],
                             (seat.FAMILIES[fam]["model"],))

    def test_a_pinned_window_names_the_evidence_grade_that_backs_it(self):
        """A GUESS AND A READING ARE NOT THE SAME THING, which is the
        correction this test carries. It used to demand max_context be absent
        for BOTH council families, absolutely, reasoning that these /v1/models
        return only {id, object, owned_by} so any number would be "a guess
        wearing a measurement's clothes". That enumerated ONE evidence channel
        — the probe that backs kimi's 1000000 — and read its silence as the
        absence of all evidence.

        FOUR GRADES EXIST NOW AND EACH HAS ITS OWN KEY: a floor a live seat was
        watched holding, a ceiling a request actually 400'd at, a
        context_length an endpoint reported, and (2026-08-03) a window the
        OWNER stated. THE GRADES NEVER MERGE — that is the whole design, and
        the owner grade lives in its own test below.

        THE FATAL-DIRECTION ARGUMENT IS UNCHANGED AND STILL GOVERNS. Under-
        stating a window costs one early compaction (recoverable). Overstating
        it 400s the seat "input exceeds the context window" with in-band
        compaction unable to escape, because /compact replays the same
        oversized transcript (codex 2026-07-30, ds4pro 2026-07-29). That is why
        the measured-disproof arms run FIRST and bind every grade, why omission
        is always still allowed, and why grok — with nothing observed, nothing
        probed and nothing said about it — stays absent."""
        # POSITIVE CONTROLS FIRST, all unconditional, all on the SAME
        # observable the shipped-table arm uses: the real predicate
        # demonstrably says NO, and demonstrably says YES to a floor-backed
        # pin. A checker that returned "" for everything would satisfy the
        # shipped-table arm while proving nothing whatsoever; one that returned
        # a refusal for everything would satisfy every refusal arm.
        # EACH ARM IS PINNED BY ITS OWN DIAGNOSTIC, not merely by "something
        # was refused". Mutation control 2026-08-03 caught exactly this: with
        # the floor-required arm deleted the entry was still refused, by the
        # NEXT arm, so a name-only assertion stayed green while the checker
        # had started telling the reader to go get a bigger reading for a
        # family that had recorded no reading at all. The arms hand out
        # different instructions and the test has to say which one is right.
        admitted = seat._unbacked_window_reason(
            {"floorbacked": _floor_only(max_context=750000)})
        self.assertFalse(admitted,
                         "750000 is 2.10x the 357000 floor and the FLOOR "
                         "grade must still admit it — a headroom arm that "
                         "refuses everything is not a headroom arm")
        vendorish = seat._unbacked_window_reason(
            {"vendorish": _floor_only(max_context=1048576)})
        self.assertIn("vendorish", vendorish,
                      "the documented vendor number is 2.94x the floor and "
                      "must be REFUSED — a headroom wide enough to admit it "
                      "does nothing at all")
        self.assertIn("extrapolation", vendorish)
        bare = seat._unbacked_window_reason(
            {"bare": {k: v for k, v in seat.FAMILIES["gemini"].items()
                      if k not in ("observed_context_floor",
                                   "owner_stated_window")}})
        self.assertIn("bare", bare,
                      "a pin with no recorded evidence at all is the bare "
                      "assertion the old rule existed to stop")
        self.assertIn("no backing", bare)
        self.assertIn("owner_stated_window", bare,
                      "the refusal must hand the reader the WHOLE menu of "
                      "grades, or the only fix it suggests is inventing a "
                      "floor")
        tiny = seat._unbacked_window_reason(
            {"tiny": _floor_only(observed_context_floor=150000,
                                 max_context=300000)})
        self.assertIn("tiny", tiny,
                      "a floor under CC's assumed 200k disproves nothing, so "
                      "it may not unlock a pin")
        self.assertIn("disproves nothing", tiny)
        under = seat._unbacked_window_reason(
            {"under": _floor_only(max_context=300000)})
        self.assertIn("under", under,
                      "a pin below the entry's own recorded floor is "
                      "contradicted by its own evidence")
        self.assertIn("below its own observed floor", under)
        # THE MEASURED-DISPROOF ARMS, which bind every grade including the
        # owner's. A ceiling is the failure itself: codex reached 369,663
        # tokens and every request 400'd, so 369663 is not a window, it is the
        # proof of one smaller than that.
        crashed = seat._unbacked_window_reason(
            {"crashed": dict(seat.FAMILIES["codex"], max_context=369663)})
        self.assertIn("crashed", crashed)
        self.assertIn("observed_context_ceiling", crashed)
        overprobed = seat._unbacked_window_reason(
            {"overprobed": dict(seat.FAMILIES["kimi"], max_context=1048577)})
        self.assertIn("overprobed", overprobed)
        self.assertIn("probed_context_length", overprobed)
        # …and each of those grades SUFFICES on its own, so the two arms above
        # are refusing the overshoot rather than the grade.
        self.assertFalse(seat._unbacked_window_reason(
            {"probed": dict(seat.FAMILIES["kimi"])}),
            "an endpoint-reported context_length must back its own pin")
        self.assertFalse(seat._unbacked_window_reason(
            {"ceilinged": {k: v for k, v in seat.FAMILIES["codex"].items()
                           if k != "owner_stated_window"}}),
            "a measured 400 point must back a pin that sits under it")

        # …and the shipped table passes that same predicate.
        self.assertFalse(seat._unbacked_window_reason(),
                         "the shipped FAMILIES table does not back its "
                         "own pinned context windows")
        # gemini's FLOOR is still a real disproof of CC's default and the pin
        # still may not sit under it, whatever grade ends up backing it.
        self.assertGreater(seat.FAMILIES["gemini"]["observed_context_floor"],
                           autocompact.CC_ASSUMED_WINDOW)
        self.assertGreaterEqual(seat.FAMILIES["gemini"]["max_context"],
                                seat.FAMILIES["gemini"]
                                ["observed_context_floor"])
        # grok has been observed at nothing, probed at nothing and named in
        # nothing the owner said, so it may pin nothing. Omission stays the
        # honest AND the safe value — CC's conservative default.
        self.assertIsNone(seat.FAMILIES["grok"].get("max_context"),
                          "grok ships an unmeasured context window")
        self.assertIsNone(seat.FAMILIES["grok"].get("observed_context_floor"))
        self.assertIsNone(seat.FAMILIES["grok"].get("owner_stated_window"))

    def test_an_owner_statement_is_its_own_grade_and_never_a_floor(self):
        """THE GRADE ADDED 2026-08-03, and the thing it must never become.

        The owner supplied the measurement the table lacked — "almsot all
        models have 1m cw at this point, only codex is i think 360k", then "ds4
        is 1m, kimi is 1m, gemini 1m" — and directed the change: "set 100% at
        those (320k is fine for codex) and see what new errors if any they
        get". That is a real evidence grade: a direct claim about the model
        from the person who owns the subscriptions and has watched these seats
        for months. It is NOT a measurement, and @kimi's review of the floor
        arm stands — an owner statement is not the same grade of floor as a
        measured one, and 1m on a 417k floor was CORRECTLY refused.

        SO THE STATEMENT GOT ITS OWN KEY INSTEAD OF WIDENING THE HEADROOM.
        Laundering 1000000 into observed_context_floor would assert a seat was
        watched holding a million tokens; nobody watched that. This test's
        centre is that swapping ONE key flips the verdict on the SAME number:
        with owner_stated_window the shipped gemini entry is admitted, and with
        only its floor the very same 1000000 is refused as extrapolation.

        NO RATIO CHECK APPLIES TO A STATEMENT, DELIBERATELY. The headroom
        bounds extrapolation FROM a floor, because a floor-backed pin is
        computed from the floor. A statement is not computed from anything, and
        ruling that the owner may only say things within 2.5x of whatever a
        seat happened to be holding would make his knowledge a function of our
        sampling luck. What still binds him is every MEASURED disproof: a pin
        under a live floor, over a probed context_length, or at/above a
        measured 400 point is refused no matter who said it."""
        fam = seat.FAMILIES["gemini"]
        owner = fam["owner_stated_window"]
        # THE TWO GRADES ARE BOTH PRESENT IN ONE ENTRY AND CANNOT BE READ AS
        # EACH OTHER: the floor is an int meaning "seen"; the statement is a
        # record carrying the DATE and the OWNER'S OWN WORDS.
        self.assertIn("said", owner)
        self.assertIn("verbatim", owner)
        self.assertIn("gemini 1m", owner["verbatim"],
                      "the entry must quote the owner, not paraphrase him")
        self.assertIn("2026-08-03", owner["said"])
        self.assertGreater(fam["observed_context_floor"],
                           autocompact.CC_ASSUMED_WINDOW)
        # SAME NUMBER, SAME FLOOR, ONE KEY REMOVED -> REFUSED. This is the
        # distinguishability proof: the pin is owner-backed, and the floor
        # demonstrably could not have backed it.
        floorless = seat._unbacked_window_reason({"gemini": _floor_only()})
        self.assertIn("extrapolation", floorless,
                      "1000000 is 2.80x the 357000 floor — the floor grade "
                      "must still refuse it, exactly as @kimi ruled")
        self.assertIn("observed floor", floorless)
        self.assertFalse(seat._unbacked_window_reason({"gemini": dict(fam)}),
                         "the shipped entry, WITH the owner statement, is "
                         "the same number and must be admitted")
        # WHAT STOPS AN AGENT WRITING "OWNER SAID SO", arm by arm, each pinned
        # by ITS OWN diagnostic. A refusal that only proved "something was
        # refused" is what let two mutants survive the previous pass.
        shapeless = seat._unbacked_window_reason(
            {"shapeless": dict(fam, owner_stated_window=1000000)})
        self.assertIn("must be a record", shapeless,
                      "a bare number in this field is the agent-invented "
                      "window the grade exists to keep out")
        wordless = seat._unbacked_window_reason(
            {"wordless": dict(fam, owner_stated_window={
                "tokens": 1000000, "said": "2026-08-03"})})
        self.assertIn("missing verbatim", wordless)
        enlarged = seat._unbacked_window_reason(
            {"enlarged": dict(fam, max_context=1100000)})
        self.assertIn("ABOVE", enlarged,
                      "an agent may not enlarge the owner's number; the quote "
                      "backs what he said and nothing over it")
        self.assertIn("owner_stated_window", enlarged)
        # …and the OTHER direction is allowed, because it is the recoverable
        # one. This is the arm that keeps the asymmetry from quietly becoming
        # a symmetry: a lower pin costs an early compaction, a higher one
        # wedges the seat.
        self.assertFalse(seat._unbacked_window_reason(
            {"gemini": dict(fam, max_context=400000)}),
            "pinning UNDER what the owner said is the safe direction and "
            "must stay legal")
        summarised = seat._unbacked_window_reason(
            {"summarised": dict(fam, owner_stated_window=dict(
                owner, verbatim="gemini's window is much larger"))})
        self.assertIn("do not state", summarised,
                      "the quote must STATE the number; a summary of what the "
                      "owner meant is an agent's number again")
        refiled = seat._unbacked_window_reason({"grokish": dict(fam)})
        self.assertIn("name none of", refiled,
                      "a sentence about gemini may not back a pin filed "
                      "under another family")
        # the notation the owner actually types, read in BOTH units, so the
        # "quote must state the number" arm is matching the owner's writing
        # rather than demanding he type digits.
        stated = seat._quoted_token_counts(
            "almsot all models have 1m cw at this point, "
            "only codex is i think 360k")
        self.assertIn(1000000, stated)
        self.assertIn(360000, stated,
                      "the hedged 360k the owner did NOT act on still parses "
                      "— codex stays at 320000 by decision, not by a reader "
                      "that could not see the number")

    def test_the_family_name_arm_takes_declared_aliases_not_open_prefixes(self):
        """@kimi's FIX on r3, and why a prefix match was failing OPEN.

        The name arm exists so a sentence about one model cannot back a pin on
        another. Its first version asked `name.startswith(word)` for any word
        of 3+ characters, because the owner types the MODEL and not the
        FAMILIES key — "ds4 is 1m" is about ds4pro, and that need is real.

        But a prefix test makes EVERY truncation an alias. @kimi probed the
        real predicate and found "gem 1m" admits a gemini pin: a string the
        owner would never type, and precisely what an agent fabricating a
        quote WOULD produce. An anti-fabrication arm that accepts words its
        subject never says is not narrowing anything.

        So an alias is DECLARED and matched exactly — the FAMILIES key, the
        first alphanumeric segment of each declared model, plus an optional
        owner_aliases. ONLY THE FIRST SEGMENT: taking every segment would make
        "flash" and "high" aliases of gemini via gemini-3.6-flash-high, which
        is the same failure open in a new coat. Derivation rather than a
        hand-written tuple is deliberate, so a family added tomorrow arrives
        with its aliases already populated instead of with the arm empty."""
        gemini = seat.FAMILIES["gemini"]
        owner_words = gemini["owner_stated_window"]["verbatim"]
        # THE FINDING ITSELF, against the real predicate.
        self.assertFalse(seat._quote_names_family("gemini", "gem 1m"),
                         "a truncated family name is not the family — this is "
                         "the exact string @kimi got a gemini pin out of")
        self.assertFalse(seat._quote_names_family("gemini", "gemin 1m"),
                         "one character short is still not the family")
        # …while the need that motivated the prefix match still works, because
        # "ds4" is now ds4-pro's DECLARED stem rather than an accident.
        self.assertTrue(seat._quote_names_family("ds4pro", owner_words),
                        "the owner types the model: 'ds4 is 1m' must still "
                        "back ds4pro")
        self.assertTrue(seat._quote_names_family("gemini", owner_words))
        self.assertTrue(seat._quote_names_family("kimi", owner_words))
        self.assertFalse(seat._quote_names_family("grok", owner_words),
                         "the same sentence names three families and grok is "
                         "not one of them")
        # ONLY THE FIRST SEGMENT NAMES A FAMILY. Both of these are real words
        # inside gemini's own declared model string.
        self.assertFalse(seat._quote_names_family("gemini", "flash 1m"))
        self.assertFalse(seat._quote_names_family("gemini", "high 1m"))
        aliases = seat._family_owner_aliases("gemini", gemini)
        self.assertIn("gemini", aliases)
        self.assertNotIn("flash", aliases)
        self.assertNotIn("gem", aliases)
        ds4 = seat._family_owner_aliases("ds4pro", seat.FAMILIES["ds4pro"])
        self.assertIn("ds4", ds4, "the owner's word, now declared")
        self.assertIn("ds4pro", ds4, "and the key it is filed under")
        # A DERIVED STEM MUST BE CONSISTENT WITH ITS KEY. This arm is here
        # because the first cut of the fix derived from the models alone and
        # BROKE re-filing: gemini's entry copied under another key brings
        # gemini-3.6-flash-high with it, so "gemini" stayed an alias and the
        # owner's gemini sentence backed a pin filed elsewhere. The
        # re-filing test above caught it; this pins it at the helper too, so
        # the next person to widen the derivation sees which arm they broke.
        self.assertFalse(seat._quote_names_family("grokish", owner_words,
                                                  gemini),
                         "gemini's own entry re-filed under another key must "
                         "not be backed by the sentence about gemini")
        self.assertNotIn("gemini",
                         seat._family_owner_aliases("grokish", gemini),
                         "a model stem only counts where it shares a prefix "
                         "with the key it is filed under")
        # …and a stem genuinely unrelated to its key earns nothing by
        # inference. codex declares gpt-5.6-sol; "gpt" backs codex only if
        # someone declares it in owner_aliases on purpose.
        self.assertNotIn("gpt",
                         seat._family_owner_aliases("codex",
                                                    seat.FAMILIES["codex"]))
        self.assertIn("gpt", seat._family_owner_aliases(
            "codex", dict(seat.FAMILIES["codex"], owner_aliases=("gpt",))),
            "the explicit escape hatch must still work, or an owner idiom "
            "that is nobody's model stem can never be honoured")
        # END TO END through the shipped predicate, not only the helper: the
        # fabricated quote must fail the WHOLE grade, not merely one function.
        fabricated = seat._unbacked_window_reason(
            {"gemini": dict(gemini, owner_stated_window=dict(
                gemini["owner_stated_window"], verbatim="gem 1m"))})
        self.assertIn("name none of", fabricated)
        self.assertIn("prefix of the family name is not the family",
                      fabricated,
                      "the refusal must say WHY, or the next author reads it "
                      "as a typo and widens the match again")
        # NO TWO FAMILIES MAY ANSWER TO ONE WORD. Asserted at import, and the
        # guard must be able to DIE — a collision is exactly what a new family
        # introduces and no other test is looking at.
        self.assertFalse(seat._family_owner_aliases_are_unique(),
                         "the shipped table must be clean")
        # TWO ROUTES CAN ACTUALLY COLLIDE, and the fixture must exercise each.
        # (A first fixture here set grok's probe_models to ds4-turbo and
        # stopped colliding once key-consistency landed — a fixture that no
        # longer trips the clause it was written for proves nothing.)
        # ROUTE 1 — the explicit escape hatch, which is exempt from
        # key-consistency and is therefore the one route that can name
        # anything at all.
        declared = copy.deepcopy(seat.FAMILIES)
        declared["grok"]["owner_aliases"] = ("ds4",)
        clash = seat._family_owner_aliases_are_unique(declared)
        self.assertIn("ds4", clash)
        self.assertIn("ds4pro", clash)
        self.assertIn("grok", clash,
                      "the refusal names the shared word AND both claimants, "
                      "so the fix is obvious without re-deriving it")
        # ROUTE 2 — derivation, which still collides when one key is a prefix
        # of another: a family keyed "ds4" beside ds4pro both derive "ds4".
        sibling = copy.deepcopy(seat.FAMILIES)
        sibling["ds4"] = copy.deepcopy(sibling["ds4pro"])
        derived_clash = seat._family_owner_aliases_are_unique(sibling)
        self.assertIn("ds4", derived_clash)
        self.assertIn("ds4pro", derived_clash,
                      "key-consistency narrows derivation but does not make "
                      "it collision-proof; prefix-sibling keys still clash")

    def test_the_guard_reaches_every_family_that_pins_not_only_proxy_oauth(
            self):
        """A GUARD A NEW PIN CAN STEP AROUND BY BEING THE WRONG MODE IS NOT A
        GUARD. `_unbacked_window_reason` opened with
        `if fam.get("mode") != "proxy-oauth": continue` until 2026-08-03, on
        the reasoning that other modes had a probe channel of their own. Then
        ds4pro — mode proxy-key — took a 1000000 pin, and under that scope the
        guard would have skipped it in silence whatever backed it. This test
        keeps the bypass shut: strip ds4pro's evidence and the refusal must
        still arrive."""
        self.assertNotEqual(seat.FAMILIES["ds4pro"]["mode"], "proxy-oauth",
                            "ds4pro must stay the wrong mode for the old "
                            "scope, or this test stops testing anything")
        stripped = seat._unbacked_window_reason(
            {"ds4pro": {k: v for k, v in seat.FAMILIES["ds4pro"].items()
                        if k != "probed_context_length"}})
        self.assertIn("ds4pro", stripped,
                      "a proxy-key family pinning a window with no evidence "
                      "must be REFUSED, not skipped for being the wrong mode")
        self.assertIn("no backing", stripped)
        self.assertFalse(seat._unbacked_window_reason(
            {"ds4pro": dict(seat.FAMILIES["ds4pro"])}),
            "…and the shipped entry, with its probed reading, is admitted")

    def test_the_two_grades_are_legible_side_by_side_in_the_table(self):
        """SAME NUMBER, SAME DAY, DIFFERENT PROVENANCE — and the table has to
        say which is which, because that is the only job this design has.

        ds4pro and gemini both pin 1000000 as of 2026-08-03 and they did NOT
        get there the same way. ds4pro's came off a PUBLISHED endpoint —
        OpenRouter's public no-auth /v1/models reports context_length=1048576
        for deepseek/deepseek-v4-pro (2026-08-02) — the same grade that has
        backed kimi since 2026-07-23. gemini's came from the OWNER SAYING SO:
        its proxy /v1/models returns only {id, object, owned_by}, so there is
        nothing to read and his sentence is the whole of the evidence.

        THE FAILURE THIS FORBIDS is the one that produced the omission it
        replaced: ds4pro was handed "the gemini/grok posture", a piece of
        reasoning that rests on GEMINI'S evidence gap, while its own model was
        published all along."""
        ds4 = seat.FAMILIES["ds4pro"]
        gem = seat.FAMILIES["gemini"]
        self.assertEqual(ds4["max_context"], gem["max_context"],
                         "the two entries must still be the same number, or "
                         "this test is not about telling grades apart")
        # the PROBED one reads as an endpoint reading and carries no words
        self.assertGreaterEqual(ds4["probed_context_length"],
                                ds4["max_context"])
        self.assertIsNone(ds4.get("owner_stated_window"),
                          "ds4pro's number is published, not spoken — filing "
                          "it under the owner would hide a real measurement "
                          "behind a weaker grade")
        # the OWNER-STATED one carries the owner's sentence and a date, and
        # has no endpoint reading to carry instead
        self.assertIn("gemini 1m", gem["owner_stated_window"]["verbatim"])
        self.assertIsNone(gem.get("probed_context_length"),
                          "gemini's endpoint reports no context_length — a "
                          "reading here would be invented")
        # kimi is the second worked example of the probed grade, so ds4pro's
        # is not a one-off shape nobody else uses.
        self.assertGreaterEqual(seat.FAMILIES["kimi"]["probed_context_length"],
                                seat.FAMILIES["kimi"]["max_context"])

    def test_the_shipped_table_is_checked_at_IMPORT_not_only_by_this_file(
            self):
        """A PREDICATE NOTHING CALLS ON THE REAL TABLE IS NOT A GUARD.

        Every other test here drives `_unbacked_window_reason` itself, so
        deleting the module-level `assert not _unbacked_window_reason()` from
        seat.py leaves this whole file GREEN while a running fleet imports a
        FAMILIES table nothing checked — the seat launches with whatever number
        was typed, and the refusal only ever appears in a test run. The
        statement is therefore pinned by its own text."""
        src = inspect.getsource(seat)
        # POSITIVE CONTROL on the SAME observable, unconditional: the source
        # really is seat.py's and really carries the predicate, so a failure
        # below is a missing ASSERT and not a mis-resolved module.
        self.assertIn("def _unbacked_window_reason(", src)
        self.assertIn("assert not _unbacked_window_reason()", src,
                      "seat.py no longer checks its own shipped table at "
                      "import — the tests would stay green while the fleet "
                      "ran on an unbacked window")
        # The alias-collision guard is the same class and needs the same pin:
        # a collision is what a NEW family introduces, and a predicate only
        # this file ever calls would never see one.
        self.assertIn("def _family_owner_aliases_are_unique(", src)
        self.assertIn("assert not _family_owner_aliases_are_unique()", src,
                      "two families answering to one owner word would ship "
                      "unnoticed — the arm that refuses cross-filing would "
                      "admit the same sentence for both")

    def test_the_assumed_window_mirror_matches_autocompact(self):
        """seat.py cannot import autocompact (autocompact imports seat), so the
        200k it compares floors against is a copy. An unpinned family resolves
        through autocompact._window to the real one — if the two ever drift,
        the "this floor disproves nothing" arm starts measuring against a
        number no seat is actually held to."""
        # POSITIVE CONTROL, unconditional: the mirror is LIVE, not decorative.
        # The real predicate refuses a floor sitting exactly at CC's assumed
        # window and NAMES that number, so a mirror silently set to 0 (or
        # never consulted) turns this arm red rather than quietly matching.
        # FLOOR-ONLY, because the owner grade short-circuits the floor arms:
        # the shipped gemini entry would be admitted and prove nothing here.
        # AND max_context IS PULLED DOWN INSIDE THE HEADROOM ON PURPOSE.
        # Mutation control 2026-08-03: with gemini's 1000000 left in place,
        # deleting the "floor disproves nothing" arm entirely still left this
        # arm GREEN — the HEADROOM arm caught the entry instead and its
        # message happens to contain the floor, which at 200000 is the same
        # digits. The mutant died elsewhere while this assertion sat there
        # claiming to prove the mirror. 250000 is inside 2.5x of 200000, so
        # the weak-floor arm is the ONLY arm that can speak here.
        self.assertIn(str(autocompact.CC_ASSUMED_WINDOW),
                      seat._unbacked_window_reason(
                          {"atdefault": _floor_only(
                              max_context=250000,
                              observed_context_floor=(
                                  autocompact.CC_ASSUMED_WINDOW))}))
        self.assertEqual(seat._CC_ASSUMED_WINDOW_MIRROR,
                         autocompact.CC_ASSUMED_WINDOW)

    def test_ports_do_not_collide_with_the_existing_families(self):
        ports = [f["port"] for f in seat.FAMILIES.values()]
        self.assertEqual(len(ports), len(set(ports)))


class AdoptTest(ProxyOAuthBase):
    def test_a_directory_a_file_or_an_auth_subdir_all_resolve(self):
        """The owner should not have to know which shape the proxy chose."""
        p = self.plant_cred()
        pat = "antigravity-*.json"
        self.assertEqual(seat._adoptable_cred(self.donor, pat)[0], p)
        self.assertEqual(seat._adoptable_cred(p, pat)[0], p)
        nested = os.path.join(self.tmp, "nest")
        q = self.plant_cred(where=os.path.join(nested, "auth"))
        self.assertEqual(seat._adoptable_cred(nested, pat)[0], q)

    def test_a_missing_source_is_a_named_reason_not_a_crash(self):
        got, err = seat._adoptable_cred(os.path.join(self.tmp, "nope"), "x-*.json")
        self.assertIsNone(got)
        self.assertIn("no such file", err)

    def test_no_match_says_what_it_looked_for_and_where(self):
        got, err = seat._adoptable_cred(self.donor, "xai-*.json")
        self.assertIsNone(got)
        self.assertIn("xai-*.json", err)
        self.assertIn(self.donor, err)

    def test_the_newest_credential_wins(self):
        import time
        old = self.plant_cred("antigravity-old@example.com.json")
        past = time.time() - 9000
        os.utime(old, (past, past))
        new = self.plant_cred("antigravity-new@example.com.json")
        self.assertEqual(seat._adoptable_cred(self.donor, "antigravity-*.json")[0],
                         new)


class CredContentTest(ProxyOAuthBase):
    """kimi's r1 FIX: adoption validated the FILENAME and never the CONTENT.

    The asymmetry was the tell. Mode "proxy" (codex) refuses an expired token via
    _cred_exp and parses structure via translate_codex_auth; proxy-oauth checked
    that a name matched a glob and copied the bytes. So the NO-credential path
    returned 1 while the BAD-credential path returned 0 and printed
    "next: helm seat launch gemini" for a seat that could not authenticate.
    """
    REAL_ANTIGRAVITY = {"access_token": "ya29." + "x" * 240, "refresh_token": "1//" + "y" * 100,
                        "type": "antigravity", "email": "someone@example.com",
                        "expires_in": 3599, "disabled": False,
                        "expired": "2099-01-01T00:00:00Z"}

    def _write(self, obj, name="antigravity-someone@example.com.json"):
        p = os.path.join(self.donor, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else __import__("json").dumps(obj))
        return p

    def test_kimis_exact_repro_garbage_named_like_a_credential(self):
        p = self._write("this is not json and not a credential")
        self.assertIn("not JSON", seat._oauth_cred_reason(p))
        # seat._add takes args as a LIST. Calling it
        # seat._add("gemini", "--auth-from", self.donor) makes args the STRING
        # "--auth-from" and silently binds self.donor to `room`; the string then
        # substring-matches, args[1] is the character "-", that path is missing,
        # and rc is 1 — the assertion passed for a reason that had nothing to do
        # with validation. Only mutation testing exposed it: deleting the content
        # check entirely left all 22 tests green while a live repro returned 0.
        self.assertEqual(seat._add("gemini", ["--auth-from", self.donor]), 1,
                         "minted a seat from a file that cannot authenticate")

    def test_json_that_is_not_an_object_is_refused(self):
        self.assertIn("not an object",
                      seat._oauth_cred_reason(self._write("[1, 2, 3]")))

    def test_a_credential_with_no_tokens_at_all_is_refused(self):
        p = self._write({"email": "someone@example.com", "type": "antigravity"})
        self.assertIn("neither access_token nor refresh_token",
                      seat._oauth_cred_reason(p))

    def test_a_disabled_credential_is_refused_as_quarantined(self):
        bad = dict(self.REAL_ANTIGRAVITY, disabled=True)
        self.assertIn("disabled", seat._oauth_cred_reason(self._write(bad)))

    def test_an_EXPIRED_access_token_WITH_a_refresh_token_is_still_adoptable(self):
        """THE OVER-STRICTNESS GUARD, and the reason this check asks 'can it
        possibly authenticate' rather than 'is it fresh'. These proxies refresh,
        so a stale access token beside a live refresh_token is the ORDINARY case
        — the live gemini credential is exactly this shape most of the time.
        Refusing it would reject the common path in the name of safety."""
        stale = dict(self.REAL_ANTIGRAVITY, expired="2020-01-01T00:00:00Z")
        self.assertIsNone(seat._oauth_cred_reason(self._write(stale)))

    def test_expired_with_NO_refresh_token_is_refused(self):
        dead = {"access_token": "ya29.dead", "type": "antigravity",
                "expired": "2020-01-01T00:00:00Z"}
        self.assertIn("no refresh_token", seat._oauth_cred_reason(self._write(dead)))

    def test_an_unparseable_expiry_stamp_is_not_treated_as_expired(self):
        """A stamp we cannot read is not evidence of anything. Guessing 'expired'
        from a parse failure would refuse working credentials on a format change."""
        odd = {"access_token": "ya29.live", "type": "antigravity",
               "expired": "sometime last tuesday"}
        self.assertIsNone(seat._oauth_cred_reason(self._write(odd)))

    def test_the_real_live_credential_shapes_are_accepted(self):
        """Guard against a validator so strict it rejects the two credentials the
        fleet actually runs on — checked against their real key sets."""
        self.assertIsNone(seat._oauth_cred_reason(self._write(self.REAL_ANTIGRAVITY)))
        xai = {"access_token": "x" * 780, "refresh_token": "y" * 86, "type": "xai",
               "auth_kind": "oidc", "base_url": "https://x", "disabled": False,
               "expired": "2099-01-01T00:00:00Z", "expires_in": 21600,
               "id_token": "z" * 490, "token_type": "Bearer"}
        self.assertIsNone(seat._oauth_cred_reason(
            self._write(xai, "xai-someone@example.com.json")))


class MintTest(ProxyOAuthBase):
    def _add(self, family, *args):
        return seat._add(family, list(args))

    def test_adopting_writes_the_credential_0600_whatever_the_source_mode(self):
        """The live grok credential was found at 0664 — group- and world-readable
        — because the proxy wrote it under its own umask with nothing watching.
        Nothing was watching precisely because it lived outside the seat system.
        Adoption re-permissions on the way in; it does not inherit."""
        self.plant_cred("xai-someone@example.com.json", mode=0o664,
                        where=os.path.join(self.donor, "grok"))
        rc = self._add("grok", "--auth-from", os.path.join(self.donor, "grok"))
        self.assertEqual(rc, 0)
        d = seat.seat_dir("grok")
        got = os.listdir(os.path.join(d, "auth"))
        cred = [n for n in got if n.startswith("xai-")]
        self.assertEqual(len(cred), 1)
        p = os.path.join(d, "auth", cred[0])
        self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600",
                         "adopted a credential at the source's looser mode")

    def test_no_credential_means_rc1_and_the_login_command_never_success(self):
        """THE LOAD-BEARING ONE. A seat that cannot authenticate is not a seat.
        Minting the home and printing a success line would be the same laundering
        this codebase keeps finding: a green line over an absent capability."""
        rc = self._add("gemini")
        self.assertEqual(rc, 1, "reported success for a seat with no credential")

    def test_the_home_is_still_written_so_the_login_has_a_target(self):
        """rc 1 is not 'do nothing' — the interactive login needs a config to
        point at, and that config is the whole reason the auth lands in the right
        place. Refusing to claim success is different from refusing to prepare."""
        self._add("gemini")
        cfg = os.path.join(seat.seat_dir("gemini"), "config.yaml")
        self.assertTrue(os.path.exists(cfg))
        with open(cfg, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("port: %d" % seat.FAMILIES["gemini"]["port"], body)
        self.assertIn(os.path.join(seat.seat_dir("gemini"), "auth"), body)

    def test_the_config_keeps_the_keepalive_and_cooldown_fixes(self):
        """_config_yaml is shared with the codex path, so proxy-oauth inherits
        the two settings a hand-rolled config has repeatedly dropped: the 15s
        non-stream keepalive (empty-200 on long compaction passes) and the
        explicit 5s transient cooldown (0 means the fork's 60s DEFAULT, not
        disabled)."""
        self._add("gemini")
        path = os.path.join(seat.seat_dir("gemini"), "config.yaml")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("nonstream-keepalive-interval: 15", body)
        self.assertIn("transient-error-cooldown-seconds: 5", body)

    def test_an_unreadable_auth_from_is_rc1_with_the_reason(self):
        rc = self._add("gemini", "--auth-from", os.path.join(self.tmp, "ghost"))
        self.assertEqual(rc, 1)

    def test_auth_from_without_a_value_is_usage_not_an_index_error(self):
        self.assertEqual(self._add("gemini", "--auth-from"), 2)


if __name__ == "__main__":
    unittest.main()
