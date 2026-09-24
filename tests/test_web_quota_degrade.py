#!/usr/bin/env python3
"""EVERY WAY THE MEASURED WORLD CAN FAIL, AND THE ONE SET OF ANSWERS.

A DEGRADE BRANCH PER FAILURE IS A DEFECT PER FAILURE NOBODY WROTE ONE FOR. A
catch that names `ProviderError` does not cover the `OSError` one raise-site
over, and that OSError is real: the native provider appends every probe cycle
to a history file, so an unwritable path raises it out of the enrichment. Left
uncovered it answered a write that had ALREADY LANDED with a 500 and emptied
the owner's declared inventory on the read.

So this module is not about a list of failures. It is about the CLOSURE: the
acquisition answers with ONE object in one of THREE states — measured,
nothing-measures-here, and could-not-read-with-its-cause — and every door on
this tab consumes that object. The last arm here is the acceptance test for
that claim: it injects raises of five different exception types at three
different seams of the acquisition, and asserts that what the doors do is a
member of the closed set. A new `raise` cannot invent a behaviour none of the
doors already handles, because there is nowhere for it to land except those
three states.

THE INSTRUMENTS ARE THE REAL PROVIDERS, never a stub that cannot tell the
three apart:

  A. `NativeQuotaProvider` over a temp HOME whose anthropic credential walks
     due-refresh -> expired-token -> removed, with a partial confirm at each
     step. An expired accessToken returns before the usage GET, so this touches
     no network.
  D. the same provider with its history file unwritable — a REAL `OSError`
     out of `_append_history`, which is production's own non-ProviderError
     raise site and the one both regressions rode in on.
  B. `CliQuotaProvider` over a real fake binary and a real subprocess, whose
     acquisition and enrichment can each answer non-JSON or a non-zero rc.
  C. two homes, one login: what the declared card offers after one of them is
     described.

THE OWNER'S OWN FILE IS NEVER TOUCHED. `AccountsBase` redirects HELM_ACCOUNTS
into a temp dir for every arm here, and every home these arms read is a
tempdir HOME of their own.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-quotadegrade-", var="HELM_HOME")

from helm import accounts, providers, web  # noqa: E402
from tests.test_accounts import AccountsBase  # noqa: E402


def clear_caches():
    """What the TTL lapsing does: the next read builds the world again."""
    for key in ("creds", "measured-accounts", "provider-accounts"):
        with web._qlock:
            web._qstate.pop(key, None)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def creds_row(name):
    return next((r for r in web.get_creds() if r["name"] == name), None)


class NativeHomeBase(AccountsBase):
    """A temp HOME holding two anthropic credential homes for ONE login, and
    the REAL native provider pointed at it. Lifted from the review probe."""

    EMAIL = "alpha" + "@" + "v.test"

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="helm-degrade-home-")
        self._prev_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(self._restore_home)
        self.alpha = os.path.join(self.home, ".claude-homes", "alpha")
        self.alpha2 = os.path.join(self.home, ".claude-homes", "alpha2")
        for h in (self.alpha, self.alpha2):
            write_json(os.path.join(h, ".claude.json"),
                       {"oauthAccount": {"emailAddress": self.EMAIL,
                                         "organizationType": "claude_max",
                                         "accountUuid": "u-alpha"}})
            self.cred(h, access_dead=True, refresh_alive=True)
        self.provider = providers.NativeQuotaProvider(
            history_path=os.path.join(self.home, "hist.jsonl"))
        for target, attr in ((web, "_provider"), (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: self.provider)
            patch.start()
            self.addCleanup(patch.stop)
        clear_caches()
        self.addCleanup(clear_caches)

    def _restore_home(self):
        if self._prev_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._prev_home
        shutil.rmtree(self.home, ignore_errors=True)

    @staticmethod
    def cred(h, access_dead, refresh_alive):
        now_ms = int(time.time() * 1000)
        block = {"accessToken": "sk-ant-oat01-probe-not-a-real-token-" + "x" * 40,
                 "refreshToken": "sk-ant-ort01-probe-not-a-real-token-" + "y" * 40,
                 "expiresAt": now_ms - (2 * 86400 * 1000 if access_dead else -3600 * 1000),
                 "refreshTokenExpiresAt": now_ms + (30 * 86400 * 1000 if refresh_alive
                                                    else -86400 * 1000)}
        write_json(os.path.join(h, ".credentials.json"), {"claudeAiOauth": block})

    def lapse(self):
        """TTL lapse on BOTH caches — web's entry and the provider's own."""
        clear_caches()
        self.provider._cred_cache = None

    def declare(self, rid="claude-alpha"):
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": rid, "vendor": "anthropic", "plan": "Max 20x",
            "reach": "anthropic", "count": 1, "price_month": "$200",
            "measured_key": accounts.measured_key(self.EMAIL)}})
        self.assertEqual(status, 200, out)
        return out["account"]

    def confirm(self, rid="claude-alpha"):
        return web._api_accounts_post(
            {"action": "save", "account": {"id": rid, "confirmed": True}})


class NativeCredentialWalkTest(NativeHomeBase):
    """PROBE A, LIFTED. The credential walks due-refresh -> expired-token ->
    removed under a declared row, and the record must stay keyed to the
    subscription the table rendered at every step. This one was GREEN on the
    reviewed tip and is here as the regression guard for the cure below."""

    def test_a_partial_confirm_keys_to_the_rendered_row_through_every_state(self):  # noqa: VACUOUS_ASSERTION — the control is the first assertion in the arm, unconditional: get_creds names both homes, so every assertIsNotNone below is about the credential walk rather than a table that was never populated
        # the unconditional positive control on the observable every assertion
        # below reads: the provider really measures these two homes
        self.assertEqual(sorted(r["name"] for r in web.get_creds()),
                         [self.EMAIL, self.EMAIL + "#alpha2"])
        row = creds_row(self.EMAIL)
        alias = creds_row(self.EMAIL + "#alpha2")
        self.assertIsNotNone(row, web.get_creds())
        self.assertIsNotNone(alias, web.get_creds())
        self.assertEqual(alias["subscription"], row["subscription"],
                         "two homes, one login, two subscriptions")
        rendered = row["subscription"]
        first = self.declare()
        self.assertEqual(first["subscription"], rendered)

        self.cred(self.alpha, access_dead=True, refresh_alive=False)
        self.lapse()
        row = creds_row(self.EMAIL)
        self.assertIsNotNone(row, "the expired row left the table")
        out, status = self.confirm()
        self.assertEqual(status, 200, out)
        self.assertTrue(out["account"]["confirmed"])
        self.assertEqual(out["account"]["subscription"], rendered)
        self.assertEqual(row["subscription"], rendered)

        os.remove(os.path.join(self.alpha, ".credentials.json"))
        self.lapse()
        out, status = self.confirm()
        self.assertEqual(status, 200, out)
        self.assertEqual(out["account"]["subscription"], rendered,
                         "removing one home's credential re-keyed his record")


class NonProviderErrorFromTheEnrichmentTest(NativeHomeBase):
    """PROBE D, LIFTED AND ARMED — the two live WORSE-THAN-MAIN regressions.

    `NativeQuotaProvider.cred_state` appends every probe cycle to its history
    file. Put a FILE where that file's parent directory should be and the
    append raises `FileExistsError` out of its `os.makedirs` — an `OSError`,
    which is not a `ProviderError`, so a catch naming `ProviderError` lets it
    out of the cache and into the two doors:

      the CONFIRM answered a write that had already landed with a 500 carrying
      the exception's text, and the card painted an X and skipped its reload;

      the GET landed in the outer `except`, which returns an EMPTY declared
      inventory — so the rows the owner had typed vanished off his screen and
      the card drew "helm does not know how many accounts you have"."""

    def blocked_history(self):
        blocker = os.path.join(self.home, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        self.provider.history_path = os.path.join(blocker, "h.jsonl")
        self.lapse()
        return blocker

    def test_the_fixture_raises_a_real_oserror_that_is_not_a_provider_error(self):
        """THE CONTROL, and it is not optional: every arm below is about what
        happens when the enrichment raises something the ProviderError catch
        cannot see. If the fixture stopped raising — or started raising a
        ProviderError — the arms would pass on an absence."""
        self.blocked_history()
        with self.assertRaises(OSError) as caught:
            self.provider.cred_state()
        self.assertNotIsInstance(caught.exception, providers.ProviderError)
        self.assertTrue(self.provider.accounts(),
                        "the ACQUISITION failed too, so nothing below is "
                        "about the enrichment")

    def test_a_landed_confirm_is_never_answered_as_a_failure(self):
        """HIGH-1. He ticked the box, the write landed on disk, and helm told
        him it had not. The only honest answers are 200, or the `stale` 409 the
        card already reads as "saved, and the screen has moved on"."""
        first = self.declare()
        self.blocked_history()
        out, status = self.confirm()
        self.assertIn(status, (200, 409), out)
        if status == 409:
            self.assertEqual(out.get("code"), "stale", out)
        stored = accounts.read()["accounts"][0]
        self.assertTrue(stored["confirmed"],
                        "the tick did not reach disk at all")
        self.assertEqual(stored["id"], first["id"])

    def test_the_confirm_still_keys_to_the_row_he_ticked_it_on(self):
        first = self.declare()
        self.blocked_history()
        out, status = self.confirm()
        self.assertEqual(status, 200, out)
        self.assertEqual(out["account"]["subscription"], first["subscription"],
                         "the enrichment raised an OSError between the render "
                         "and the tick, and his record was re-keyed")

    def test_the_get_keeps_every_declared_row_on_his_screen(self):
        """HIGH-2. `unreadable` is a sentence about the DECLARED INVENTORY —
        the card renders it INSTEAD of the rows. A measured-side failure that
        sets it takes rows he typed off the page and tells him helm does not
        know how many accounts he has."""
        first = self.declare()
        self.blocked_history()
        got = web._api_accounts()
        self.assertIsNone(got.get("unreadable"),
                          "a measured-side raise reported the owner's own "
                          "inventory as unreadable")
        self.assertEqual([a["id"] for a in got["accounts"]], [first["id"]],
                         "the declared rows vanished from his screen")
        self.assertTrue(got["measured_unavailable"],
                        "the measurement failed and the card was not told")

    def test_the_page_is_never_given_the_exceptions_own_text(self):  # noqa: VACUOUS_ASSERTION — the control is the assertIn(blocker, str(exc)) at the end of this same arm, on the same path: the text really does name the blocker, so its absence from every string above is the rule holding
        """The class-name-only rule. The `OSError`'s text is the PATH it could
        not open — a credential home on this fleet — and every string here
        renders on his page."""
        self.declare()
        blocker = self.blocked_history()
        said = []
        got = web._api_accounts()
        said += [got.get("unreadable") or "", got.get("measured_unavailable") or ""]
        out, _status = self.confirm()
        said.append(json.dumps(out))
        creds, _ = web._api_creds({})
        said.append(json.dumps(creds))
        for text in said:
            self.assertNotIn(blocker, text,
                             "an exception's own text reached the page")
        # THE CONTROL, because this arm is an absence: the blocker path really
        # is what the exception says, so its absence above is the rule holding
        # rather than a string that was never going to appear.
        with self.assertRaises(OSError) as caught:
            self.provider.cred_state()
        self.assertIn(blocker, str(caught.exception))


class TheUndescribedStripNeverOffersAnAliasTest(NativeHomeBase):
    """PROBE C, LIFTED AND ARMED. accounts.join's `measured_only` is keyed by
    the measured NAME, and two homes under one login are two names. Describe
    one and the card still offers "describe alpha#alpha2" — and the click mints
    a SECOND record on a subscription that already has one, which is the alias
    row the owner's ruling forbids ("one row per PAID SUBSCRIPTION; homes and
    seats attach as aliases and never mint their own")."""

    def test_describing_one_home_describes_the_subscription_both_are_on(self):
        # THE CONTROL, unconditional and on the same observable: before
        # anything is described the strip offers BOTH homes, so the emptiness
        # below is the describe landing rather than a strip that never fills
        self.assertEqual(
            len(web._api_accounts()["join"]["measured_only"]), 2,
            web._api_accounts()["join"])
        first = self.declare()
        self.assertTrue(first["subscription"])
        view = web._api_accounts()
        offered = [m["name_masked"] for m in view["join"]["measured_only"]]
        self.assertEqual(
            offered, [],
            "the card still offers the OTHER home of a subscription the owner "
            "has already described: %r" % (offered,))

    def test_the_alias_still_reaches_the_cli_half_of_the_join(self):
        """AND IT IS NOT DROPPED ON THE FLOOR. The alias is not an undescribed
        account, but it is still a home helm measures, and the join is what
        `helm accounts --json` reads. Silently losing it would be the same
        class pointed the other way."""
        self.declare()
        linked = accounts.join(accounts.read()["accounts"], web.get_creds())
        names = [a["name"] for a in linked.get("aliases", [])]
        self.assertIn(self.EMAIL + "#alpha2", names, linked)


FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
mode = open(os.environ["DEGRADE_MODE_FILE"]).read().strip()
verb = sys.argv[1] if len(sys.argv) > 1 else ""
ACC = [{"name": "home-one", "provider": "codex", "home": "/h", "email": "one@v.test",
        "tier": "Big Plan", "active": False, "usable": True}]
CS = [{"account": "home-one", "cred_state": "ok", "headroom_pct": 50.0, "tier": "Big Plan"}]
WN = [{"account": "home-one", "windows_left": 1.0, "windows_per_week": 2.0, "verdict": "ok"}]
if verb == "list":
    if mode == "list-nonjson": print("not json"); sys.exit(0)
    if mode == "list-fail": print("boom", file=sys.stderr); sys.exit(1)
    print(json.dumps(ACC)); sys.exit(0)
if verb == "cred-state":
    if mode == "enrich-nonjson": print("not json"); sys.exit(0)
    if mode == "enrich-fail": print("boom", file=sys.stderr); sys.exit(1)
    print(json.dumps(CS)); sys.exit(0)
if verb == "windows":
    if mode == "enrich-nonjson": print("not json"); sys.exit(0)
    if mode == "enrich-fail": sys.exit(1)
    print(json.dumps(WN)); sys.exit(0)
print("[]")
'''


class CliDoorFalsifierTest(AccountsBase):
    """PROBE B, LIFTED. The REAL `CliQuotaProvider` over a real subprocess: the
    acquisition and the enrichment each answer non-JSON (a ProviderError) or a
    non-zero rc. A stub that could not tell those two apart is the shape that
    let four defects ride through a green suite on this lane."""

    NAME = "home-one"

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="helm-degrade-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.binary = os.path.join(self.tmp, "fakequota")
        with open(self.binary, "w", encoding="utf-8") as f:
            f.write(FAKE_CLI)
        os.chmod(self.binary, os.stat(self.binary).st_mode | stat.S_IXUSR)
        self.mode_file = os.path.join(self.tmp, "mode")
        os.environ["DEGRADE_MODE_FILE"] = self.mode_file
        self.addCleanup(os.environ.pop, "DEGRADE_MODE_FILE", None)
        self.mode("ok")
        self.provider = providers.CliQuotaProvider(self.binary)
        for target, attr in ((web, "_provider"), (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: self.provider)
            patch.start()
            self.addCleanup(patch.stop)
        clear_caches()
        self.addCleanup(clear_caches)

    def mode(self, m):
        with open(self.mode_file, "w", encoding="utf-8") as f:
            f.write(m)
        clear_caches()

    def declare(self):
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 200, out)
        return out["account"]

    def test_the_fixture_really_produces_all_four_answers(self):
        """THE CONTROL over a real subprocess: without it every arm below could
        pass against a binary that always answered the same way."""
        from helm.providers import ProviderError
        self.mode("ok")
        self.assertTrue(self.provider.accounts())
        self.assertTrue(self.provider.cred_state())
        self.mode("enrich-nonjson")
        with self.assertRaises(ProviderError):
            self.provider.cred_state()
        self.assertTrue(self.provider.accounts(), "the acquisition failed too")
        self.mode("list-nonjson")
        with self.assertRaises(ProviderError):
            self.provider.accounts()

    def test_an_unreadable_enrichment_keeps_the_row_and_its_key(self):
        first = self.declare()
        self.mode("enrich-nonjson")
        row = creds_row(self.NAME)
        self.assertIsNotNone(row, "the account list was discarded with the numbers")
        self.assertTrue(row.get("quota_unreadable"))
        self.assertEqual(row["subscription"], first["subscription"])
        out, status = web._api_accounts_post(
            {"action": "save", "account": {"id": "codex-home-one", "confirmed": True}})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["account"]["subscription"], first["subscription"])

    def test_an_unreadable_acquisition_does_not_empty_his_inventory(self):
        first = self.declare()
        self.mode("list-nonjson")
        got = web._api_accounts()
        self.assertIsNone(got.get("unreadable"))
        self.assertEqual([a["id"] for a in got["accounts"]], [first["id"]])
        self.assertTrue(got["measured_unavailable"],
                        "the acquisition could not be read and the card was "
                        "told nothing")

    def test_a_cli_that_is_not_there_is_not_a_cli_that_failed(self):
        """THE OTHER HALF OF THE ARM ABOVE, and the reason it can be asserted
        at all. Both arrive as a `ProviderError` from the acquisition, and the
        page owes them opposite sentences: a host with no quota binary measures
        nothing and nothing is wrong; a binary that answered garbage is a world
        helm could not read."""
        from helm.providers import NoQuotaProvider
        self.declare()
        absent = providers.CliQuotaProvider("")
        with mock.patch.object(web, "_provider", lambda: absent), \
                mock.patch.object(providers, "default_provider", lambda: absent):
            clear_caches()
            with self.assertRaises(NoQuotaProvider):
                absent.accounts()
            self.assertIsNone(web._api_accounts()["measured_unavailable"],
                              "a machine with no quota CLI was told helm could "
                              "not read its accounts")
        clear_caches()
        self.mode("list-nonjson")
        self.assertTrue(web._api_accounts()["measured_unavailable"],
                        "a CLI that answered garbage drew as a machine with no "
                        "CLI at all")

    def test_the_key_comes_back_when_the_world_reads_again(self):
        first = self.declare()
        self.mode("list-nonjson")
        # the control on the same observable: it IS set while the world cannot
        # be read, so the None below is the recovery and not a dead field
        self.assertTrue(web._api_accounts()["measured_unavailable"])
        self.mode("ok")
        got = web._api_accounts()
        self.assertEqual(got["accounts"][0]["subscription"], first["subscription"])
        self.assertIsNone(got["measured_unavailable"])


class ThreeStatesAndNoFourthTest(AccountsBase):
    """THE ACCEPTANCE TEST for the rearchitecture.

    Every door on this tab consumes ONE acquisition result. This arm injects a
    raise of four different exception types at four different seams inside that
    acquisition and asserts that what the doors do is a member of a CLOSED SET:

      nothing raises out of any door;
      the owner's declared rows are all still on his screen;
      `unreadable` — the sentence that REPLACES those rows — stays None,
        because it is about his inventory and nothing here is;
      a write that landed is never reported as a failure;
      what reaches his page names the exception's CLASS and never its text.

    A new `raise` added to the acquisition tomorrow lands in the same one
    catch, so it produces a state every door already handles. That is the
    property; this is how it is measured."""

    NAME = "home-one"
    SECRET = "/home/someone/secret-path"

    class Provider:
        """One provider with a seam-by-seam injector. Its account list, its
        credential states and its windows ANSWER unless told otherwise, so an
        arm that injects at one seam is about that seam."""

        def __init__(self, name):
            self.name, self.boom = name, {}

        def _maybe(self, seam):
            exc = self.boom.get(seam)
            if exc is not None:
                raise exc

        def accounts(self):
            self._maybe("accounts")
            return [{"name": self.name, "provider": "codex", "home": "/h",
                     "email": "one" + "@" + "v.test", "tier": "Big Plan",
                     "active": False, "usable": True}]

        def cred_state(self):
            self._maybe("cred_state")
            return [{"account": self.name, "cred_state": "ok",
                     "headroom_pct": 50.0, "tier": "Big Plan"}]

        def windows(self):
            self._maybe("windows")
            return [{"account": self.name, "windows_left": 1.0,
                     "windows_per_week": 2.0, "verdict": "ok"}]

    def setUp(self):
        super().setUp()
        self.provider = self.Provider(self.NAME)
        for target, attr in ((web, "_provider"), (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: self.provider)
            patch.start()
            self.addCleanup(patch.stop)
        clear_caches()
        self.addCleanup(clear_caches)

    def declare(self):
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 200, out)
        return out["account"]

    def inject(self, seam, exc):
        self.provider.boom = {seam: exc} if seam else {}
        clear_caches()

    def test_the_control_answers_healthily_with_nothing_injected(self):
        first = self.declare()
        self.inject(None, None)
        got = web._api_accounts()
        self.assertIsNone(got["measured_unavailable"])
        self.assertIsNone(got.get("unreadable"))
        self.assertEqual([a["id"] for a in got["accounts"]], [first["id"]])
        self.assertTrue(web.get_creds())
        self.assertTrue(first["subscription"])

    def test_every_injected_raise_lands_in_the_closed_set(self):  # noqa: VACUOUS_ASSERTION — the loop is the point (the claim is over a SET of injections) and the three unconditional assertions before it are the control on the same observables: with nothing injected the doors answer a keyed row, no failure sentence and a non-empty table
        first = self.declare()
        # THE UNCONDITIONAL CONTROL, before the loop and on the same
        # observables: with nothing injected the doors answer a keyed row and
        # no failure, so every assertion inside the loop is about the raise
        self.assertTrue(first["subscription"])
        self.assertIsNone(web._api_accounts()["measured_unavailable"])
        self.assertTrue(web.get_creds())
        seams = ("accounts", "cred_state", "windows")
        excs = (OSError(self.SECRET), RuntimeError(self.SECRET),
                ValueError(self.SECRET), ZeroDivisionError(self.SECRET),
                providers.ProviderError(self.SECRET))
        for seam in seams:
            for exc in excs:
                with self.subTest(seam=seam, exc=type(exc).__name__):
                    self.inject(seam, exc)
                    rows = web.get_creds()
                    self.assertIsInstance(rows, list)
                    self.assertIsInstance(web._measured_accounts(), list)
                    body, status = web._api_creds({})
                    self.assertEqual(status, 200)
                    got = web._api_accounts()
                    self.assertIsNone(
                        got.get("unreadable"),
                        "a measured-side raise reported his own inventory "
                        "unreadable")
                    self.assertEqual([a["id"] for a in got["accounts"]],
                                     [first["id"]],
                                     "his declared rows left the screen")
                    out, st = web._api_accounts_post({
                        "action": "save",
                        "account": {"id": first["id"], "confirmed": True}})
                    self.assertIn(st, (200, 409), out)
                    if st == 409:
                        self.assertEqual(out.get("code"), "stale", out)
                    self.assertTrue(accounts.read()["accounts"][0]["confirmed"],
                                    "the write did not land")
                    said = " ".join([got.get("measured_unavailable") or "",
                                     json.dumps(body), json.dumps(out)])
                    self.assertNotIn(self.SECRET, said,
                                     "the exception's own text reached his page")

    def test_nothing_measures_here_stays_its_own_state(self):
        """FAIL-OPEN IS NOT A FAILURE. A machine with no quota provider at all
        measures NOTHING, and that is an honest empty world — sessions and
        resume do not need quota. It must not start reading as "helm could not
        read your accounts", which is the opposite sentence."""
        self.declare()
        self.inject("accounts", providers.NoQuotaProvider("no quota CLI configured"))
        got = web._api_accounts()
        self.assertEqual(web.get_creds(), [])
        self.assertIsNone(got["measured_unavailable"],
                          "a machine with no provider was told helm could not "
                          "read its accounts")
        self.inject("accounts", OSError(self.SECRET))
        self.assertTrue(web._api_accounts()["measured_unavailable"],
                        "a world helm COULD NOT READ came back as an empty one")

    def test_the_fill_door_refusal_names_the_read_it_could_not_do(self):
        """THE DESCRIBE BUTTON. Resolving the handle it posts needs the measured
        world. "helm no longer measures that account" is a statement about the
        world; with the world unread, the only true sentence is that helm could
        not look."""
        self.inject("accounts", OSError(self.SECRET))
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 400, out)
        self.assertIn("cannot read", out["error"])
        self.assertNotIn("no longer measures", out["error"])
        self.assertNotIn(self.SECRET, out["error"])
        self.assertEqual(accounts.read()["accounts"], [],
                         "a refusal that says nothing was changed changed "
                         "something")
        # THE CONTROL ON THE SAME CALL: the same payload with the world
        # readable WRITES the row, so "nothing was written" above is about the
        # refusal and not about a door that writes nothing at all
        self.inject(None, None)
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 200, out)
        self.assertEqual([a["id"] for a in accounts.read()["accounts"]],
                         ["codex-home-one"])


class StubWorldBase(AccountsBase):
    """A stub provider, a temp HOME, and clean caches. The provider answers
    `ACCOUNTS` and an empty enrichment unless an arm injects a raise."""

    ACCOUNTS = ()

    class Provider:
        def __init__(self, rows):
            self.rows, self.boom = [dict(r) for r in rows], {}

        def _maybe(self, seam):
            exc = self.boom.get(seam)
            if exc is not None:
                raise exc

        def accounts(self):
            self._maybe("accounts")
            return [dict(r) for r in self.rows]

        def cred_state(self):
            self._maybe("cred_state")
            return []

        def windows(self):
            self._maybe("windows")
            return []

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="helm-degrade-stubhome-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home})
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = self.Provider(self.ACCOUNTS)
        for target, attr in ((web, "_provider"), (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: self.provider)
            patch.start()
            self.addCleanup(patch.stop)
        clear_caches()
        self.addCleanup(clear_caches)


class ASecondCodexHomeJoinsTheOneRecordTest(StubWorldBase):
    """task/2914 (a), from the LAND 183 browser pass. A codex home is NAMED for
    its directory, and its login is read out of the token inside it — so two
    homes on one login are two names that share only the `email`. The quota
    table keys them from the provider's own rows and draws one row; the
    declared card joined against the MERGED rows, which drop that `email`, so
    it keyed the second home by its directory name, offered "describe
    admin-home-second", and the click minted a second record for one bill.

    The anthropic arm above cannot see this: an anthropic home is named for
    its login, so its name alone keys it correctly."""

    EMAIL = "admin" + "@" + "v.test"
    ACCOUNTS = (
        {"name": "admin-home", "provider": "codex", "home": "/h/admin-home",
         "email": "admin" + "@" + "v.test", "active": False, "usable": True},
        {"name": "admin-home-second", "provider": "codex",
         "home": "/h/admin-home-second", "email": "admin" + "@" + "v.test",
         "active": False, "usable": True},
    )

    def describe(self, name, rid):
        return web._api_accounts_post({"action": "save", "account": {
            "id": rid, "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(name)}})

    def test_the_second_home_is_an_alias_of_the_described_one(self):
        # CONTROL: the table itself says both homes are one subscription, and
        # before anything is described the card offers both
        keys = {r["subscription"] for r in web.get_creds()}
        self.assertEqual(len(keys), 1, web.get_creds())
        self.assertEqual(
            len(web._api_accounts()["join"]["measured_only"]), 2)
        out, status = self.describe("admin-home", "codex-admin")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["account"]["subscription"], keys.pop())
        linked = web._api_accounts()["join"]
        self.assertEqual(
            [m["name_masked"] for m in linked["measured_only"]], [],
            "the card still offers the second home of a login the owner has "
            "already described, and its button mints a second record")
        aliases = linked["aliases"]
        self.assertEqual([a["name"] for a in aliases], ["admin-home-second"])
        self.assertEqual(aliases[0]["subscription"],
                         out["account"]["subscription"])


class AnUnprovenMemberRidesItsOwnFlagTest(StubWorldBase):
    """task/2981. The page wore one word, "unknown", for a row with no
    reading and for a codex Team member whose workspace is pooled while no
    pool file is proven to be it. The second is MEASURED, and its cure is to
    pool that member's credential. So the row carries `member_unproven`, and
    a row with no reading does not."""

    ACCOUNTS = (
        {"name": "hey-home", "provider": "codex", "home": "/h/cx-hey",
         "email": "hey" + "@" + "team.example", "active": False,
         "usable": True},
        {"name": "quiet-home", "provider": "codex", "home": "/h/cx-quiet",
         "email": "quiet" + "@" + "team.example", "active": False,
         "usable": True},
    )

    def setUp(self):
        super().setUp()
        self.provider.cred_state = lambda: [
            {"account": "hey-home", "provider": "codex",
             "cred_state": "unknown", "status": providers.POOL_MEMBER_UNKNOWN,
             "headroom_pct": None}]

    def test_the_unproven_member_is_flagged_and_the_silent_row_is_not(self):
        rows = {r["name"]: r for r in web.get_creds()}
        self.assertEqual(rows["hey-home"]["state"], "unknown")
        self.assertIs(rows["hey-home"]["member_unproven"], True)
        # THE CONTROL: the row with no reading reads the same word, and is
        # not flagged
        self.assertEqual(rows["quiet-home"]["state"], "unknown")
        self.assertIs(rows["quiet-home"]["member_unproven"], False)

class TheCardOffersOnlyWhatTheTableDrawsTest(StubWorldBase):
    """The other edge of the same join. A quota CLI may report a vendor the
    table does not draw; the describe button resolves its handle against the
    table's rows, so offering such an account is offering a button that is
    refused on click."""

    ACCOUNTS = (
        {"name": "home-one", "provider": "codex", "home": "/h/one",
         "email": "one" + "@" + "v.test", "active": False, "usable": True},
        {"name": "elsewhere", "provider": "gemini", "home": "/h/g",
         "email": "g" + "@" + "v.test", "active": False, "usable": True},
    )

    def test_an_account_the_table_does_not_draw_is_not_offered(self):
        self.assertEqual([r["name"] for r in web.get_creds()], ["home-one"])
        offered = web._api_accounts()["join"]["measured_only"]
        self.assertEqual([m["key"] for m in offered],
                         [accounts.measured_key("home-one")])


class WorldReadFaultsReachTheWebLogTest(StubWorldBase):
    """task/2914 (b); the LAND 183 browser pass calls it (c). The world read has one
    net, and it turns every raise into `unreadable` — which is right for the
    PAGE, and was silent everywhere else: a NameError in the read showed only
    as a chip whose tooltip said "NameError", with nothing in the server's
    stderr. A programming error could not be told apart from an outage.

    The page still gets the class name only. The traceback goes where every
    other unexpected failure of `helm web` goes: its stderr, the log the 500
    path already sends him to."""

    ACCOUNTS = ({"name": "home-one", "provider": "codex", "home": "/h",
                 "email": "one" + "@" + "v.test", "active": False,
                 "usable": True},)

    def read_with_log(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            body, status = web._api_creds({})
        return body, status, buf.getvalue()

    def test_the_control_logs_nothing_when_the_world_reads(self):
        body, status, log = self.read_with_log()
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual(log, "")

    def test_an_acquisition_fault_is_logged_with_its_traceback(self):
        from helm import web_quota

        def broken():
            return undefined_name  # noqa: F821 — the programming error itself

        with mock.patch.object(web_quota, "_read_the_world", broken):
            body, status, log = self.read_with_log()
        self.assertEqual(status, 200)
        self.assertTrue(body.get("unavailable"), body)
        self.assertNotIn("undefined_name", json.dumps(body))
        self.assertIn("[helm web]", log)
        self.assertIn("Traceback", log)
        self.assertIn("NameError", log)
        self.assertIn("undefined_name", log)

    def test_an_enrichment_fault_is_logged_with_its_traceback(self):
        self.provider.boom = {"windows": OSError("history path unwritable")}
        body, status, log = self.read_with_log()
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertTrue(body[0].get("quota_unreadable"), body)
        self.assertIn("[helm web]", log)
        self.assertIn("Traceback", log)
        self.assertIn("OSError: history path unwritable", log)


if __name__ == "__main__":
    unittest.main()
