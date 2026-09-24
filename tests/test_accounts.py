#!/usr/bin/env python3
"""The OWNER-DECLARED account inventory — helm/accounts.py.

WHAT THESE ARMS ARE FOR, in priority order:

1. A HAND-EDITED ROW IS NEVER SILENTLY DROPPED. The file is plain JSON under
   the helm home and editing it by hand is legal, so the failure that matters
   is not a crash — it is a save that quietly rewrites the file WITHOUT the
   line somebody typed wrong. So the corrupt-entry arms do not merely assert
   that reading survives; they save a DIFFERENT account afterwards and then
   read the raw bytes back to prove the unparseable row is still there.

2. NO SECRET REACHES DISK, AND NO REFUSAL ECHOES ONE. Every free-text field is
   scanned, and the arm proves the scanner saw real input by checking the
   matching CLEAN value is accepted — a refusal test with no accepted control
   passes just as well against a validator that refuses everything.

3. TWO TABS CANNOT CLOBBER EACH OTHER. The revision is compared inside the
   lock, so the conflict arm reads a revision, writes through a second caller,
   and then proves the first caller's save changed nothing at all.

4. UNREADABLE IS NOT EMPTY. `read` degrades for the page; `read_strict` REFUSES
   so an agent is never told we have zero accounts because a byte was garbled.

Hermetic: every arm drives a tmp HELM_ACCOUNTS path, and every fixture id is
synthetic (acct-a, vendor-x). No real account name, no email and no credential
appears anywhere in this file — the never-track commit guard would refuse it,
and the schema has no field that would accept one anyway.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-accounts-", var="HELM_HOME")

from helm import accountfields, accounts, accountseed  # noqa: E402

# THE CONTROL PAIR for the secret scanner. Each entry is (clean, dirty): the
# clean half MUST be accepted and the dirty half MUST be refused, so a scanner
# that refuses everything fails the arm exactly as loudly as one that refuses
# nothing. A guard measured only by what it rejects is a guard nobody measured.
ADDRESS_DIRTY = "someone" + "@" + "example.test"
SECRET_PAIRS = (
    ("reading the live timeline", "sk-abcdefabcdefabcdefabcdef"),
    ("the build family's proxy", "xai-0123456789abcdef0123"),
    ("model downloads", "hf_AbCdEfGhIjKlMnOpQrStUvWx"),
    ("issue triage", "ghp_AaBbCcDdEeFfGgHhIiJjKkLl"),
    ("nightly runs", "Authorization: Bearer opensesame"),
    ("the owner signs in himself", ADDRESS_DIRTY),
    ("short notes are fine", "eyJhbGciOi.eyJzdWIiOjEyMw.c2lnbmF0dXJl"),
    ("plain prose with numbers 7 and 20",
     "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVoxMjM0NTY3ODkw"),
)


# ORDINARY ENGLISH THAT CARRIES A KEY PREFIX AS A SUBSTRING. Measured through
# the real web door before the cure: every one of these was refused as an API
# key, because "sk-" lives inside task-, ask-, risk- and disk-, and the first
# two are this repo's own vocabulary. The right half of the same guard is the
# credential WORD followed by a value, which the colon-anchored rule missed.
ORDINARY_ENGLISH = (
    "see task-2721 for the rollout",
    "ask-first policy for this one",
    "risk-free evaluation runs",
    "the disk-usage baseline",
    "my basket-of-tasks is large",
    "the token bucket refills hourly",
    "password hygiene matters here",
    "secret sauce for the eval runs",
    "api key rotation is handled by the owner",
    "authorization happens in the browser",
)
CREDENTIAL_VALUES = (
    "Bearer abc123def456",
    "bearer 7f3a9b2c1d4e",
    "token: hunter2",
    "password = correcthorse9",
    "password hunter2xyz99",
    "recovery code 4821-9930-1147-2208 keep safe",
)
# THE CLASS THE CREDENTIAL-WORD-PLUS-VALUE RULE CREATED WHEN IT WAS FIRST
# WRITTEN: it took any run that was not purely alphabetic, and an English
# compound after a credential word is exactly that shape. Every one of these is
# a sentence somebody writes about a subscription, and every one was refused as
# "a credential line" — the surface accusing him of pasting a secret into his
# own prose. The hyphen now ends the run, so what faces the shape test is the
# single word in front of it.
HYPHENATED_ENGLISH = (
    "api key rotation-policy articles",
    "authorization 3-step approvals",
    "cookie consent-banner design",
    "password 8-character minimum debates",
    "token bucket-style rate limiting reading",
    "bearer 10-year bonds reading",
    "secret 25th-anniversary planning",
    "password reset-flow design work",
    "api key OAuth2Client docs",
    "secret Santa2024 gift lists",
)
# …and the counterweight, which is why the hyphen cannot simply be ignored: a
# recovery code is hyphenated ON PURPOSE. Three groups or more, three or more
# characters each, with digits — no compound above comes close.
GROUPED_CODE = "recovery code 4821-9930-1147-2208 keep safe"


def row(**over):
    """A complete, legal declared account. Every arm starts from this and
    changes ONE thing, so a failure names the field it is about."""
    base = {"id": "acct-a", "vendor": "vendor-x", "plan": "Small Plan",
            "price_month": "$7", "count": 1,
            "good_for": "reading the live timeline",
            "not_for": "building; the quota is too small",
            "reach": "owner-only, ask"}
    base.update(over)
    return base


class AccountsBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-accounts-")
        self.path = os.path.join(self.dir, "accounts.json")
        self._prev = os.environ.get("HELM_ACCOUNTS")
        os.environ["HELM_ACCOUNTS"] = self.path
        self.addCleanup(self._restore)

    def _restore(self):
        import shutil
        if self._prev is None:
            os.environ.pop("HELM_ACCOUNTS", None)
        else:
            os.environ["HELM_ACCOUNTS"] = self._prev
        shutil.rmtree(self.dir, ignore_errors=True)

    def raw(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def plant(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))


class RoundTripTest(AccountsBase):
    def test_a_saved_account_reads_back_with_every_field_it_was_given(self):
        saved, err, code = accounts.save(row(measured_as="quota-row-1",
                                             notes="renewed by card"))
        self.assertIsNone(err)
        self.assertIsNone(code)
        view = accounts.read()
        self.assertEqual([a["id"] for a in view["accounts"]], ["acct-a"])
        got = view["accounts"][0]
        for field in ("vendor", "plan", "price_month", "count", "good_for",
                      "not_for", "reach", "measured_as", "notes"):
            self.assertEqual(got[field], saved[field], field)
        self.assertTrue(got["updated_at"], "the writer stamps updated_at")

    def test_the_headline_is_one_line_and_names_vendor_plan_count_and_price(self):
        accounts.save(row())
        head = accounts.read()["accounts"][0]["headline"]
        # the unconditional positive control on the SAME string: four
        # separators means five rendered cells, so an empty headline or a
        # headline missing a cell fails here before the loop runs.
        self.assertEqual(head.count(" · "), 4, head)
        self.assertNotIn("\n", head)
        self.assertLessEqual(len(head), accounts.MAX_HEADLINE)
        for token in ("vendor-x", "Small Plan", "x1", "$7/mo"):
            self.assertIn(token, head)

    def test_the_headline_carries_the_masked_identity_so_rows_differ(self):
        """Six accounts of one vendor on one plan render as six identical
        headlines without it, and a list the owner cannot tell apart at a
        glance is a list he stops reading."""
        address = "someone" + "@" + "example.test"
        accounts.save(row(measured_as=address))
        head = accounts.read()["accounts"][0]["headline"]
        self.assertIn("s…@example.test", head)
        self.assertNotIn(address, head)
        self.assertLessEqual(len(head), accounts.MAX_HEADLINE)

    def test_the_id_is_the_key_and_is_not_stored_twice(self):
        accounts.save(row())
        stored = self.raw()["accounts"]["acct-a"]
        self.assertEqual(stored["vendor"], "vendor-x")   # the row IS there
        self.assertNotIn("id", stored,
                         "the id is the KEY; a second copy in the value is a "
                         "field that can disagree with its own key")

    def test_saving_the_same_id_updates_one_row_rather_than_duplicating_it(self):
        """The id is the key, so a second save of it is an edit. What the
        second save does to the fields it did NOT carry is MergeDoorTest's
        subject; this arm is about the count."""
        accounts.save(row())
        accounts.save(row(plan="Bigger Plan"))
        view = accounts.read()
        self.assertEqual(len(view["accounts"]), 1)
        self.assertEqual(view["accounts"][0]["plan"], "Bigger Plan")

    def test_an_edit_that_retypes_the_id_refuses_rather_than_minting_a_row(self):
        """THE ID IS THE KEY. A save under a key nothing is stored under is not
        an edit: it writes a second row and leaves the first one there, under a
        form whose own line says the id stays the same. The rows are counted in
        the FILE, because the API answer is what the duplicate hid behind."""
        accounts.save(row())
        out, err, code = accounts.save(row(id="acct-b"), original_id="acct-a")
        self.assertIsNone(out)
        self.assertEqual(code, "refused")
        self.assertIn("the id is the row's key", err)
        self.assertIn("Remove that row and add it again", err)
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a"])
        # THE CONTROL on the same call: the same edit KEEPING the id lands, so
        # "one row" above is a refusal rather than a door that writes nothing
        _out, err, _code = accounts.save(row(plan="Bigger Plan"),
                                         original_id="acct-a")
        self.assertIsNone(err, err)
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a"])
        self.assertEqual(self.raw()["accounts"]["acct-a"]["plan"], "Bigger Plan")

    def test_an_add_carries_no_original_id_and_still_mints_one(self):
        accounts.save(row())
        out, err, _code = accounts.save(row(id="acct-b"))
        self.assertIsNone(err, err)
        self.assertEqual(out["id"], "acct-b")
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a", "acct-b"])

    def test_remove_deletes_only_the_named_row(self):
        accounts.save(row())
        accounts.save(row(id="acct-b"))
        ok, err, _ = accounts.remove("acct-a")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual([a["id"] for a in accounts.read()["accounts"]], ["acct-b"])

    def test_removing_an_absent_id_refuses_and_writes_nothing(self):
        accounts.save(row())
        before = accounts.read()["revision"]
        ok, err, code = accounts.remove("acct-nope")
        self.assertFalse(ok)
        self.assertIn("no declared account", err)
        self.assertEqual(code, "refused")
        self.assertEqual(accounts.read()["revision"], before)

    def test_the_cap_refuses_a_new_id_loudly_and_still_allows_replacement(self):
        for n in range(accounts.MAX_ACCOUNTS):
            saved, err, _ = accounts.save(row(id="acct-%03d" % n))
            self.assertIsNone(err, err)
        saved, err, code = accounts.save(row(id="one-too-many"))
        self.assertIsNone(saved)
        self.assertIn("which is the limit", err)
        self.assertEqual(code, "refused")
        # replacing an existing row at the cap must still work, or the
        # inventory freezes in whatever state it hit the cap in
        saved, err, _ = accounts.save(row(id="acct-000", plan="Renamed"))
        self.assertIsNone(err, err)


class MergeDoorTest(AccountsBase):
    """A SAVE MERGES OVER THE STORED ROW — the cure for a page edit that
    silently destroyed three fields.

    The quota tab's form round-trips fewer fields than the schema holds, and
    `save` replaces the row under its key, so every edit deleted `renews_on`,
    `confirmed` and `seeded_from` with no message of any kind — the details
    fold rendering the renewal date the edit button was about to throw away.
    The rule lives at the DOOR because the CLI's `set <id> --confirm` is the
    same partial write, and a second copy of it in a second writer is a second
    chance to get it wrong."""

    FULL_ROW = dict(row(), measured_as="person" + "@" + "example.test",
                    renews_on="2027-01-31", notes="renewed by card",
                    seeded_from="the owner's own words", confirmed=True)

    def stored(self, account_id="acct-a"):
        return next(a for a in accounts.read()["accounts"] if a["id"] == account_id)

    def test_a_partial_save_preserves_every_field_it_did_not_carry(self):
        """THE FOURTEEN-FIELD ROUND TRIP: exactly the payload the page's
        declFormOpen + declPayload produce for a fully populated row."""
        saved, err, _ = accounts.save(self.FULL_ROW)
        self.assertEqual(saved["renews_on"], "2027-01-31", err)
        before = self.stored()
        page_payload = {k: before[k] for k in
                        ("id", "vendor", "plan", "count", "price_month",
                         "good_for", "not_for", "reach", "measured_as",
                         "renews_on", "notes")}
        page_payload["plan"] = "Bigger Plan"
        _after_row, err, _ = accounts.save(page_payload)
        after = self.stored()
        # THE MUST-HIT, stated as a value and carrying any refusal as its
        # message: without it every assertion below passes against a save that
        # did nothing at all.
        self.assertEqual(after["plan"], "Bigger Plan", err)
        for field in [f for f in accounts._FIELDS if f != "plan"]:
            if field == "updated_at":
                continue
            with self.subTest(field=field):
                self.assertEqual(after.get(field), before.get(field),
                                 "%s did not survive an edit that did not "
                                 "carry it" % field)

    def test_confirming_a_seeded_row_flips_confirmed_and_nothing_else(self):
        """The page's confirm button posts the id and the flag ONLY."""
        accounts.save(dict(self.FULL_ROW, confirmed=False))
        before = self.stored()
        self.assertTrue(before["needs_confirm"])
        row_out, err, _ = accounts.save({"id": "acct-a", "confirmed": True})
        self.assertEqual(row_out["id"], "acct-a", err)
        after = self.stored()
        changed = [f for f in accounts._FIELDS
                   if f != "updated_at" and before.get(f) != after.get(f)]
        self.assertEqual(changed, ["confirmed"])
        self.assertFalse(after["needs_confirm"])

    def test_an_explicit_empty_clears_the_field_it_names(self):
        """ABSENCE PRESERVES, AN EMPTY CLEARS — or the owner could never take
        a note back off a row."""
        accounts.save(self.FULL_ROW)
        # the same observable, asserted as a VALUE first: there is something
        # here to clear, so the None below is a clearing rather than a row
        # that never had a note
        self.assertEqual(self.stored()["notes"], "renewed by card")
        self.assertEqual(self.stored()["renews_on"], "2027-01-31")
        row_out, err, _ = accounts.save({"id": "acct-a", "notes": "",
                                         "renews_on": ""})
        self.assertEqual(row_out["id"], "acct-a", err)
        after = self.stored()
        self.assertEqual(after["plan"], "Small Plan")   # the row is still there
        self.assertIsNone(after["notes"])
        self.assertIsNone(after["renews_on"])
        self.assertEqual(after["seeded_from"], "the owner's own words")

    def test_a_merge_still_refuses_what_the_schema_refuses(self):
        """The merge is not a way around the door: the MERGED row is what gets
        validated, so clearing a required field is still a refusal."""
        accounts.save(self.FULL_ROW)
        saved, err, code = accounts.save({"id": "acct-a", "vendor": ""})
        self.assertIsNone(saved)
        self.assertEqual(code, "refused")
        self.assertIn("vendor is required", err)
        self.assertEqual(self.stored()["vendor"], "vendor-x")

    def test_a_merge_refuses_a_secret_before_it_reaches_the_lock(self):
        accounts.save(self.FULL_ROW)
        saved, err, code = accounts.save({"id": "acct-a",
                                          "notes": SECRET_PAIRS[0][1]})
        self.assertIsNone(saved)
        self.assertEqual(code, "refused")
        self.assertEqual(self.stored()["notes"], "renewed by card")

    def test_a_first_save_of_a_new_id_merges_over_nothing(self):
        """The add flow: there is no stored row, so the payload IS the row."""
        saved, err, _ = accounts.save(row(id="acct-new"))
        self.assertEqual(saved["id"], "acct-new", err)
        self.assertEqual(saved["plan"], "Small Plan")
        self.assertIsNone(saved["renews_on"])

    def test_a_hand_edited_row_helm_cannot_read_refuses_the_merge(self):
        """SILENTLY DROPPING IT WOULD BE THIS SAME FINDING one layer down: an
        unknown key on disk is somebody's hand-edit, and a merge that ate it
        destroys exactly what the merge exists to protect."""
        self.plant({"version": 1, "accounts": {
            "acct-a": dict(row(), invented_field="keep me")}})
        saved, err, code = accounts.save({"id": "acct-a", "count": 3})
        self.assertIsNone(saved)
        self.assertEqual(code, "refused")
        self.assertIn("invented_field", err)
        # AND IT NAMES WHICH INPUT FAILED. Validation refuses the field either
        # way, but with the caller's own sentence ("helm does not store…") the
        # owner reads it as a complaint about the three keys he just sent and
        # has no reason to look at the file.
        self.assertIn("on disk", err)
        self.assertNotIn("The inventory holds what an account IS", err)
        self.assertEqual(self.raw()["accounts"]["acct-a"]["invented_field"],
                         "keep me")
        # the control on the same door: an unknown key in the PAYLOAD gets the
        # other sentence, so the two refusals are told apart by their words
        _saved, err, _ = accounts.save(dict(row(id="acct-b"), invented_field="x"))
        self.assertIn("The inventory holds what an account IS", err)
        self.assertNotIn("on disk", err)

    def test_the_cli_confirm_flag_is_a_one_field_edit(self):
        """A flag-driven set carries one field, so without a merge at the door
        it is a whole-row rewrite that refuses for want of every required
        field the owner did not retype."""
        accounts.save(dict(self.FULL_ROW, confirmed=False))
        self.assertFalse(self.stored()["confirmed"])     # the flag starts down
        payload = accounts._set_args(["acct-a", "--confirm"])
        self.assertEqual(payload, {"id": "acct-a", "confirmed": True})
        row_out, err, _ = accounts.save(payload)
        self.assertEqual(row_out["id"], "acct-a", err)
        self.assertTrue(self.stored()["confirmed"])
        self.assertEqual(self.stored()["renews_on"], "2027-01-31")


class CorruptToleranceTest(AccountsBase):
    def test_a_bad_entry_is_reported_and_skipped_never_crashes_the_read(self):
        self.plant({"version": 1, "accounts": {
            "acct-a": row(), "acct-bad": {"vendor": "vendor-y"},
            "acct-c": "not even a dict"}})
        view = accounts.read()
        self.assertEqual([a["id"] for a in view["accounts"]], ["acct-a"])
        self.assertEqual(sorted(b["id"] for b in view["bad"]),
                         ["acct-bad", "acct-c"])
        self.assertTrue(all(b["why"] for b in view["bad"]),
                        "a skipped row must say WHY it was skipped")

    def test_a_bad_entry_survives_the_next_save_it_is_not_silently_dropped(self):
        """THE ARM THIS MODULE EXISTS FOR. Reading around a broken row is easy;
        the failure that costs the owner his typing is the SAVE that rewrites
        the file without it."""
        self.plant({"version": 1, "accounts": {
            "acct-bad": {"vendor": "vendor-y"}, "acct-a": row()}})
        saved, err, _ = accounts.save(row(id="acct-new"))
        self.assertIsNone(err, err)
        self.assertEqual(saved["id"], "acct-new")        # the save really ran
        still = self.raw()["accounts"]
        self.assertIn("acct-bad", still)
        self.assertEqual(still["acct-bad"], {"vendor": "vendor-y"},
                         "the unparseable row must survive byte-for-byte")
        self.assertIn("acct-new", still)

    def test_an_unreadable_file_is_backed_up_before_anything_can_overwrite_it(self):
        self.plant("{this is not json")
        view = accounts.read()
        self.assertTrue(view["unreadable"])
        backups = [n for n in os.listdir(self.dir) if ".corrupt-" in n]
        self.assertEqual(len(backups), 1, os.listdir(self.dir))
        with open(os.path.join(self.dir, backups[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{this is not json")

    def test_the_corrupt_backup_is_named_by_content_so_reads_do_not_pile_up(self):
        """READ IS NOT A RARE PATH: the quota tab fetches /api/accounts when
        the tab opens and again on every toggle of this card, and a backup name
        carrying a second-resolution stamp minted another copy of the same
        bytes each time. The digest is the identity."""
        import itertools
        from unittest import mock
        self.plant("{this is not json")
        counter = itertools.count()
        with mock.patch.object(accounts.pk, "now_ts",
                               lambda: "stamp-%d" % next(counter)):
            # THE INSTRUMENT IS ARMED. Four reads in a fast loop share one
            # wall-clock second, so a clock-named backup would collide with
            # itself and this arm would pass against the defect it is about.
            # Under this patch every call to the clock answers differently.
            stamps = [accounts.pk.now_ts(), accounts.pk.now_ts()]
            self.assertEqual(stamps, ["stamp-0", "stamp-1"])
            for _ in range(4):
                accounts.read()
        backups = [n for n in os.listdir(self.dir) if ".corrupt-" in n]
        self.assertEqual(len(backups), 1, backups)
        # …and the control: a DIFFERENT corruption still gets its own copy,
        # which is the property the backup exists for
        self.plant("{a different corruption")
        accounts.read()
        self.assertEqual(
            len([n for n in os.listdir(self.dir) if ".corrupt-" in n]), 2)

    def test_an_unreadable_file_reads_as_unknown_never_as_no_accounts(self):
        self.plant("{this is not json")
        view = accounts.read()
        self.assertEqual(view["accounts"], [])
        self.assertIn("could not be read", view["unreadable"])
        with self.assertRaises(accounts.AccountsUnreadable):
            accounts.read_strict()

    def test_a_save_over_an_unreadable_file_refuses_rather_than_overwriting(self):
        self.plant("{this is not json")
        saved, err, code = accounts.save(row())
        self.assertIsNone(saved)
        self.assertIn("before saving", err)
        self.assertEqual(code, "refused")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{this is not json")

    def test_a_missing_file_is_an_empty_inventory_and_not_an_error(self):
        view = accounts.read()
        self.assertEqual(view["accounts"], [])
        self.assertIsNone(view["unreadable"])
        self.assertEqual(view["revision"], accounts.MISSING_REVISION)


class BadRowRemovalTest(AccountsBase):
    """A HAND-BROKEN ROW MUST BE REMOVABLE, and from both surfaces.

    Reporting it and keeping it is only half a contract: the module invites a
    hand-edit, reports the row it cannot parse as printable ASCII so a hostile
    key never reaches a terminal, and then handed that ESCAPED spelling to
    `remove` — which validates it as an id and refuses, because not being an id
    is why the row is bad. The row could be removed from neither the card nor
    the CLI, and the only cure left was editing the JSON by hand again."""

    BROKEN_KEY = "acct-\u0007x"                      # a control character
    ADDRESS_KEY = "someone" + "@" + "example.test"   # an id an address cannot be

    def plant_broken(self):
        """Two unusable keys and one good row -> {escaped id: handle}."""
        self.plant({"version": 1, "accounts": {
            self.BROKEN_KEY: {"vendor": "vendor-y"},
            self.ADDRESS_KEY: {"vendor": "vendor-z"},
            "acct-a": row()}})
        view = accounts.read()
        self.assertEqual([a["id"] for a in view["accounts"]], ["acct-a"])
        self.assertEqual(len(view["bad"]), 2, view["bad"])
        return {b["id"]: b["handle"] for b in view["bad"]}

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = accounts.cmd_accounts(args)
        return rc, out.getvalue(), err.getvalue()

    def test_a_describe_never_rebinds_a_row_that_names_another_account(self):
        """Two measured accounts can slug to one suggested id. The second
        describe would merge over the first row and point it at another
        subscription; it is refused and names a free id. Controls: the same
        describe under a fresh id lands, and re-describing the SAME account
        under its own id is not a rebind."""
        first = "dana" + "@" + "example.test"
        second = "dana" + "@" + "other.test"
        saved, err, _c = accounts.save(dict(row(), id="dana", measured_as=first))
        self.assertIsNotNone(saved, err)
        saved, err, code = accounts.save({"id": "dana", "measured_as": second})
        self.assertIsNone(saved)
        self.assertEqual(code, "refused")
        self.assertIn("already describes another measured account", err)
        self.assertIn("dana-2", err)
        self.assertNotIn(second, err, "the refusal never prints the address")
        self.assertEqual(self.raw()["accounts"]["dana"]["measured_as"], first)
        saved, err, _c = accounts.save(dict(row(), id="dana-2", measured_as=second))
        self.assertIsNotNone(saved, err)
        saved, err, _c = accounts.save({"id": "dana", "measured_as": first, "plan": "Bigger Plan"})
        self.assertIsNotNone(saved, err)
        self.assertEqual(self.raw()["accounts"]["dana"]["plan"], "Bigger Plan")

    def test_a_handle_never_pops_a_healthy_row_and_no_healthy_row_can_be_named_like_one(self):
        """Two namespaces that must never cross. A file hand-edited to hold a
        healthy row spelled exactly like a broken row's handle: removing the
        broken row by that handle removes the BROKEN row, the look-alike is
        reported as a bad row (its spelling is reserved) and is removable by
        its own handle, and the writer refuses to mint such an id."""
        handle = accounts.bad_handle(self.BROKEN_KEY)
        self.assertRegex(handle, r"\Abad-[0-9a-f]{12}\Z")
        self.plant({"version": 1, "accounts": {
            self.BROKEN_KEY: {"vendor": "vendor-y"},
            handle: row(),
            "acct-a": row()}})
        view = accounts.read()
        self.assertEqual([a["id"] for a in view["accounts"]], ["acct-a"])
        self.assertEqual(len(view["bad"]), 2, view["bad"])
        ok, err, _c = accounts.remove(handle)
        self.assertTrue(ok, err)
        left = self.raw()["accounts"]
        self.assertNotIn(self.BROKEN_KEY, left, "the broken row is what came off")
        self.assertIn(handle, left, "the look-alike survived the broken row's removal")
        self.assertIn("acct-a", left)
        # the look-alike is itself a bad row now, with its own handle
        others = {b["id"]: b["handle"] for b in accounts.read()["bad"]}
        self.assertEqual(list(others), [handle])
        ok, err, _c = accounts.remove(others[handle])
        self.assertTrue(ok, err)
        self.assertEqual(list(self.raw()["accounts"]), ["acct-a"])
        # THE WRITER'S HALF: a declared id can never take the handle's shape
        saved, err, code = accounts.save(dict(row(), id="bad-0123456789ab"))
        self.assertIsNone(saved)
        self.assertEqual(code, "refused")
        self.assertIn("reserved", err)
        # control: an ordinary id beside it still lands
        saved, err, _code = accounts.save(dict(row(), id="bad-day-account"))
        self.assertIsNotNone(saved, err)

    def test_the_escaped_rendering_cannot_remove_which_is_why_a_handle_exists(self):
        """The defect, stated as the measurement: what the surfaces printed and
        posted is refused by the door they post it to."""
        handles = self.plant_broken()
        for escaped in handles:
            ok, err, code = accounts.remove(escaped)
            self.assertFalse(ok)
            self.assertEqual(code, "refused")
            self.assertIn("not a legitimate id", err)
        self.assertEqual(len(self.raw()["accounts"]), 3)
        # THE UNCONDITIONAL CONTROL on the same door: one of those same rows,
        # named by its handle instead, really does come off
        ok, err, _code = accounts.remove(sorted(handles.values())[0])
        self.assertTrue(ok, err)
        self.assertEqual(len(self.raw()["accounts"]), 2)

    def test_both_kinds_of_unusable_key_are_removable_by_their_handle(self):
        handles = self.plant_broken()
        # the same two observables the tail asserts about, NON-empty first
        self.assertEqual(len(self.raw()["accounts"]), 3)
        self.assertEqual(len(accounts.read()["bad"]), 2)
        for escaped, handle in sorted(handles.items()):
            ok, err, _code = accounts.remove(handle)
            self.assertTrue(ok, "%s: %s" % (escaped, err))
        # THE FILE afterwards holds neither broken key, and still holds the
        # good row — the read view would say the same thing about a row that
        # was merely hidden
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a"])
        self.assertEqual(accounts.read()["bad"], [])

    def test_a_good_row_is_not_removable_through_the_bad_row_handle(self):
        """THE CONTROL that keeps this from being a second delete door: the
        handle is resolved only against keys that are themselves unusable."""
        self.plant_broken()
        ok, err, code = accounts.remove(accounts.bad_handle("acct-a"))
        self.assertFalse(ok)
        self.assertEqual(code, "refused")
        self.assertIn("no declared account", err)
        self.assertIn("acct-a", self.raw()["accounts"])
        # …and it IS removable the ordinary way, so the refusal above is about
        # the handle rather than about a door that removes nothing
        ok, err, _code = accounts.remove("acct-a")
        self.assertTrue(ok, err)
        self.assertNotIn("acct-a", self.raw()["accounts"])

    def test_the_cli_hint_is_runnable_and_the_verb_takes_it(self):
        handles = self.plant_broken()
        rc, out, _err = self.run_cli([])
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIPPED", out)
        for escaped, handle in handles.items():
            self.assertIn("'%s'" % escaped, out)           # named for the eye…
            self.assertIn("helm accounts rm %s" % handle, out)   # …removable
        for handle in handles.values():
            rc, _out, err = self.run_cli(["rm", handle])
            self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a"])

    def test_a_key_that_is_not_a_string_is_projected_too(self):
        """JSON keys are strings, so no file can carry one to `read` — this is
        about the projection being TOTAL rather than about a file. A handle
        computed over a non-str key must still be id-shaped, because nothing
        downstream knows where the key came from."""
        entry = accounts._bad_entry(7, "the id is not a legitimate id")
        self.assertEqual(entry["id"], "7")
        self.assertEqual(accounts._clean_id(entry["handle"]), entry["handle"])
        self.assertTrue(accounts._unusable_key(7))
        self.assertFalse(accounts._unusable_key("acct-a"))


class SecretRefusalTest(AccountsBase):
    def test_every_free_text_field_refuses_a_secret_shaped_value(self):  # noqa: VACUOUS_ASSERTION — a pure refusal sweep; its positive control is test_the_scanner_sees_real_input_the_clean_halves_are_all_accepted, which accepts the CLEAN half of every pair in this same table
        fields = ("vendor", "plan", "price_month", "good_for", "not_for",
                  "reach", "measured_as", "notes")
        for field in fields:
            for clean, dirty in SECRET_PAIRS:
                if field == "measured_as" and dirty == ADDRESS_DIRTY:
                    # THE ONE DOCUMENTED EXEMPTION, skipped here and pinned
                    # whole one arm down (an address IS what a quota provider
                    # names its accounts, so the join key must hold one). Every
                    # OTHER shape still refuses in this field, which is what
                    # keeps the exemption narrow rather than a hole.
                    continue
                with self.subTest(field=field, shape=dirty[:6]):
                    saved, err, code = accounts.save(row(**{field: dirty}))
                    self.assertIsNone(saved, "%s accepted a secret shape" % field)
                    self.assertEqual(code, "refused")
                    self.assertIn(field, err, "the refusal must NAME the field")

    def test_the_refusal_never_echoes_the_value_it_refused(self):
        # ONE CASE UNCONDITIONALLY FIRST, so the loop below cannot be the only
        # thing that runs: this pins that `err` is a real refusal naming the
        # field, which is what makes "and it does not contain the value" mean
        # anything at all.
        _saved, err, _ = accounts.save(row(notes=SECRET_PAIRS[0][1]))
        self.assertIn("notes", err)
        self.assertNotIn(SECRET_PAIRS[0][1], err)
        for _clean, dirty in SECRET_PAIRS:
            with self.subTest(shape=dirty[:6]):
                _saved, err, _ = accounts.save(row(notes=dirty))
                self.assertNotIn(dirty, err)
                # nor any substantial fragment of it
                self.assertNotIn(dirty[4:20], err)

    def test_the_scanner_sees_real_input_the_clean_halves_are_all_accepted(self):  # noqa: VACUOUS_ASSERTION — this test IS the positive control for the refusal sweep above; its own operands are runtime values from SECRET_PAIRS rather than literals, so the rung cannot decide them, and the unconditional control save before the loop already fails against a writer that accepts nothing
        """THE POSITIVE CONTROL. Without it, a validator that refused every
        value would pass every arm above."""
        # DISTINCT NAMES ON PURPOSE: rebinding inside the loop below would
        # replace this row's provenance, and then the only thing proving the
        # writer ever accepted anything would itself be optional.
        first = SECRET_PAIRS[0][0]
        control_row, control_err, _ = accounts.save(row(id="acct-first",
                                                        notes=first))
        self.assertEqual(control_row["notes"], first, control_err)
        for n, (clean, _dirty) in enumerate(SECRET_PAIRS):
            with self.subTest(clean=clean[:20]):
                saved, err, _ = accounts.save(row(id="acct-%d" % n, notes=clean))
                # ONE POSITIVE ASSERTION, carrying the refusal as its message:
                # "err is None" and "the value landed" are the same fact here,
                # and stating it as the value makes a refused save name itself.
                self.assertEqual(saved["notes"], clean, err)

    def test_no_secret_shaped_value_ever_reached_disk(self):
        # A LEGITIMATE ROW GOES DOWN FIRST. Without it this arm would pass
        # against a writer that never wrote anything at all, which is the
        # scan-with-no-must-hit failure.
        accounts.save(row(id="acct-clean"))
        for _clean, dirty in SECRET_PAIRS:
            accounts.save(row(notes=dirty))
        blob = "" if not os.path.exists(self.path) else open(
            self.path, encoding="utf-8").read()
        self.assertIn("acct-clean", blob)
        for _clean, dirty in SECRET_PAIRS:
            self.assertNotIn(dirty, blob)

    def test_the_schema_has_no_credential_field_at_all(self):
        """Structural, not a validator somebody can route around."""
        self.assertIn("good_for", accounts._FIELDS)      # the tuple is real
        _saved, err, code = accounts.save(dict(row(), token="x"))
        self.assertEqual(code, "refused")
        self.assertIn("token", err)
        for name in ("token", "api_key", "apikey", "password", "secret",
                     "cookie", "email", "recovery_code"):
            self.assertNotIn(name, accounts._FIELDS)
            saved, err, code = accounts.save(dict(row(), **{name: "x"}))
            self.assertIsNone(saved)
            self.assertEqual(code, "refused")
            self.assertIn("never a key, token, password or login", err)

    def test_a_long_hyphenated_account_name_is_not_mistaken_for_a_key(self):
        """A guard that refuses the owner's own accounts is not a strict guard,
        it is a broken surface. Measured on the live provider rows: a
        33-character home name tripped a blob rule whose alphabet included the
        dash, so the account this page exists to describe could not be
        declared. A credential is 32+ characters with NO word structure."""
        # SYNTHETIC, and it has to be: the real name that exposed this is a
        # private needle and the never-track guard refuses it into history —
        # which is the right refusal and the reason this literal is invented.
        long_name = "vendor-y-a-long-ordinary-account-name"
        self.assertGreater(len(long_name), 32)
        saved, err, _ = accounts.save(row(id=long_name, notes=long_name))
        self.assertEqual(saved["id"], long_name, err)
        # and the control: an unbroken run of the same length still refuses
        blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVoxMjM0NTY3ODkw"
        _saved, err, _ = accounts.save(row(notes=blob))
        self.assertIn("run of key-shaped characters", err)

    def test_ordinary_english_carrying_a_key_prefix_is_not_an_api_key(self):
        """A GUARD THAT REFUSES THE SENTENCE IS NOT A STRICT GUARD. The prefix
        was matched as a bare substring, so "see task-2721 for the rollout"
        came back through the real web door as "good_for looks like it contains
        an API key (it carries a 'sk-' prefix)". A key shape is a prefix at a
        TOKEN BOUNDARY followed by real key material."""
        # the must-hit first: this table is worth nothing against a scanner
        # that has stopped refusing anything
        _saved, err, _ = accounts.save(row(notes=SECRET_PAIRS[0][1]))
        self.assertIn("API key", err)
        for text in ORDINARY_ENGLISH:
            for field in ("good_for", "not_for", "notes", "plan"):
                with self.subTest(text=text, field=field):
                    saved, err, _ = accounts.save(row(id="acct-ok",
                                                      **{field: text}))
                    self.assertEqual(saved[field], text, err)

    def test_a_credential_word_followed_by_a_value_is_refused_without_a_colon(self):
        """THE BLIND SIDE OF THE SAME GUARD: the colon-anchored rule read
        "Bearer abc123" as prose. The value has to look like key material —
        which is what keeps "the token bucket" in the arm above accepted."""
        # THE POSITIVE CONTROL, unconditional and on the same call: the same
        # credential WORD with an English word after it is ordinary prose and
        # is accepted, so this table is about the VALUE and not about the word
        saved, err, _ = accounts.save(row(notes="the token bucket refills hourly"))
        self.assertEqual(saved["notes"], "the token bucket refills hourly", err)
        for text in CREDENTIAL_VALUES:
            with self.subTest(text=text):
                saved, err, code = accounts.save(row(notes=text))
                self.assertIsNone(saved)
                self.assertEqual(code, "refused")
                self.assertIn("credential line", err)
                self.assertNotIn(text, err)

    def test_a_credential_word_before_a_hyphenated_compound_is_english(self):
        """BOTH HALVES IN ONE ARM, because either alone is worthless: a rule
        that refuses nothing passes the first half and a rule that refuses
        everything passes the second."""
        for text in HYPHENATED_ENGLISH:
            with self.subTest(text=text):
                saved, err, _ = accounts.save(row(id="acct-ok", notes=text))
                self.assertTrue(saved, "%r was refused: %s" % (text, err))
                self.assertEqual(saved["notes"], text)
        saved, err, code = accounts.save(row(notes=GROUPED_CODE))
        self.assertIsNone(saved, "a grouped recovery code is a credential")
        self.assertEqual(code, "refused")
        self.assertIn("credential line", err)
        self.assertNotIn("4821", err)          # and it never echoes the code

    def test_a_key_prefix_inside_a_hyphenated_word_is_not_a_key(self):
        """THE TOKEN BOUNDARY, which is the OTHER half of the key-shape rule
        from MIN_KEY_MATERIAL and had no arm of its own: the minimum covers
        short cases like task-2721, the lookbehind covers the long ones. A
        \\b would match inside "task-" exactly as the old substring test did."""
        for text in ("task-oriented-workflows and planning",
                     "risk-management_and_compliance reading",
                     "ask-me-anything_transcripts to read",
                     "desk-research_for_the_newsletter"):
            with self.subTest(text=text):
                saved, err, _ = accounts.save(row(id="acct-ok", notes=text))
                self.assertTrue(saved, "%r was refused: %s" % (text, err))
        # the control on the same shape: the prefix AT a boundary, with the
        # same length of material after it, still refuses
        _saved, err, _ = accounts.save(row(notes="sk-oriented-workflows and planning"))
        self.assertIn("API key", err)

    def test_the_refusal_names_the_offending_token_masked(self):
        """A long notes field refused for "a 'sk-' prefix" leaves him hunting.
        Three characters of material name the token; more would publish it."""
        _saved, err, _ = accounts.save(row(notes="fine prose sk-abcdefabcdefabcdefabcdef and more"))
        self.assertIn("sk-abc…", err)
        self.assertNotIn("abcdefabcdef", err)

    def test_a_prefix_with_no_key_material_after_it_is_just_a_word(self):
        """MIN_KEY_MATERIAL is the second half of the shape. A bare "sk-" is
        not a credential, and refusing it is how the guard ate the module's own
        documentation of itself."""
        self.assertGreaterEqual(accounts.MIN_KEY_MATERIAL, 8)
        saved, err, _ = accounts.save(row(notes="the sk- prefix is what we scan for"))
        self.assertTrue(saved, err)
        # the control: the same prefix WITH material still refuses
        _saved, err, _ = accounts.save(row(notes="sk-abcdefabcdefabcdefabcdef"))
        self.assertIn("API key", err)

    def test_every_string_the_seed_writes_survives_the_scanner(self):
        """STRICTNESS MEASURED AGAINST THE DATA THIS LANE ACTUALLY PRODUCES: a
        guard the seeder trips is a guard that empties the inventory on day
        one."""
        measured = [{"name": "person" + "@" + "example.test",
                     "provider": "vendor-x", "tier": "Max 20x"},
                    {"name": "plain-name", "provider": "vendor-y", "tier": "Team"}]
        # THE SEED NO LONGER MINTS FOR A MEASURED ACCOUNT — it describes the row
        # the quota table already shows — so the strings it writes come from the
        # owner's own row and from the logins only Orca can see.
        seeded = accounts.seed_rows(measured, orca=[
            {"email": "unseen" + "@" + "example.test"}])
        self.assertGreaterEqual(len(seeded), 2)       # there IS something to scan
        for candidate in seeded:
            with self.subTest(account=candidate["id"]):
                saved, err, _ = accounts.save(candidate)
                self.assertTrue(saved, err)

    def test_an_email_shaped_id_is_refused_with_the_slug_advice(self):
        saved, err, _ = accounts.save(row(id="person" + "@" + "example.test"))
        self.assertIsNone(saved)
        self.assertIn("never an email address", err)


class RevisionConflictTest(AccountsBase):
    def test_a_second_tab_saving_against_a_stale_revision_is_refused(self):
        accounts.save(row())
        stale = accounts.read()["revision"]
        # the other tab saves first
        accounts.save(row(id="acct-b"))
        fresh = accounts.read()["revision"]
        self.assertNotEqual(stale, fresh)
        saved, err, code = accounts.save(row(plan="Clobbered"),
                                         expected_revision=stale)
        self.assertIsNone(saved)
        self.assertEqual(code, "conflict")
        self.assertIn("Reload", err)

    def test_a_refused_save_changed_absolutely_nothing(self):
        accounts.save(row())
        stale = accounts.read()["revision"]
        accounts.save(row(id="acct-b"))
        before = self.raw()
        self.assertEqual(sorted(before["accounts"]), ["acct-a", "acct-b"])
        accounts.save(row(plan="Clobbered"), expected_revision=stale)
        self.assertEqual(self.raw(), before)

    def test_a_remove_against_a_stale_revision_is_refused(self):
        accounts.save(row())
        stale = accounts.read()["revision"]
        accounts.save(row(id="acct-b"))
        ok, err, code = accounts.remove("acct-a", expected_revision=stale)
        self.assertFalse(ok)
        self.assertEqual(code, "conflict")
        self.assertIn("acct-a", [a["id"] for a in accounts.read()["accounts"]])

    def test_the_current_revision_is_accepted(self):
        """The control: the conflict arms above would also pass against a
        writer that refused EVERY revision."""
        accounts.save(row())
        current = accounts.read()["revision"]
        saved, err, code = accounts.save(row(plan="Renamed"),
                                         expected_revision=current)
        self.assertIsNone(err, err)
        self.assertIsNone(code)
        self.assertEqual(saved["plan"], "Renamed")

    def test_a_secret_with_a_stale_revision_answers_the_secret_not_the_conflict(self):
        """THE ORDER OF THE TWO REFUSALS, and it is a property nothing tested:
        the scanner runs BEFORE the lock precisely so the most important
        refusal on this surface is never masked by a conflict. Deleting that
        pre-lock call leaves every other arm green — the key still never
        reaches disk — while the stale tab is told to reload and never told
        what it actually typed."""
        accounts.save(row())
        stale = accounts.read()["revision"]
        accounts.save(row(id="acct-b"))           # somebody else saves
        saved, err, code = accounts.save(
            row(notes="sk-abcdefghijklmnopqrstuvwxyz0123"),
            expected_revision=stale)
        self.assertIsNone(saved)
        self.assertEqual(code, "refused", err)
        self.assertIn("API key", err)
        # THE CONTROL, same stale revision, same call, clean value: it answers
        # the conflict — so this arm is about the ORDER and not about a writer
        # that refuses everything.
        saved, err, code = accounts.save(row(notes="a plain note"),
                                         expected_revision=stale)
        self.assertIsNone(saved)
        self.assertEqual(code, "conflict", err)
        # the positive control for the absence below, on the same observable:
        # the file HAS content and this read really saw it.
        on_disk = open(self.path, encoding="utf-8").read()
        self.assertIn("acct-b", on_disk)
        self.assertNotIn("sk-", on_disk)

    def test_a_partial_write_about_a_row_that_is_gone_says_the_row_is_gone(self):
        """THE CONFIRM SHAPE, which is the only one this covers. {id,
        confirmed} carries no required field, so it cannot be a create and the
        row's absence is the news — validate answered "vendor is required —
        say it in a few words", a sentence about a box he never touched. An
        edit that DOES carry required fields is deliberately not here: it is
        indistinguishable from a create, and the second control below is what
        draws that line."""
        accounts.save(row())
        accounts.remove("acct-a")
        saved, err, code = accounts.save({"id": "acct-a", "confirmed": True})
        self.assertIsNone(saved)
        self.assertEqual(code, "gone", err)
        self.assertIn("acct-a", err)
        self.assertNotIn("vendor is required", err)
        # THE CONTROLS, and the second one is the line this rule is drawn on.
        # An add carrying every required field still adds…
        saved, err, code = accounts.save(row())
        self.assertTrue(saved, err)
        # …and a CREATE missing one required field is still a create, told
        # which field: a payload holding any required field at all is somebody
        # adding an account, never somebody editing a ghost. `good_for` is no
        # longer one of them — a row may exist without a sentence nobody has
        # written — so the field this draws the line with is one that still is.
        partial = row(id="acct-new")
        partial.pop("vendor")
        saved, err, code = accounts.save(partial)
        self.assertIsNone(saved)
        self.assertEqual(code, "refused", err)
        self.assertIn("vendor", err)
        # …and the same partial payload against the row that now EXISTS merges
        saved, err, code = accounts.save({"id": "acct-a", "confirmed": True})
        self.assertIsNone(err, err)
        self.assertTrue(saved["confirmed"])
        self.assertEqual(saved["vendor"], "vendor-x")

    def test_the_missing_revision_admits_the_first_ever_save(self):
        saved, err, _ = accounts.save(row(),
                                      expected_revision=accounts.MISSING_REVISION)
        self.assertIsNone(err, err)
        self.assertEqual(saved["id"], "acct-a")


class JoinTest(AccountsBase):
    MEASURED = [{"name": "quota-row-1", "provider": "vendor-x"},
                {"name": "quota-row-2", "provider": "vendor-y"}]

    def undescribed(self, linked):
        """The whole names behind the undescribed rows. `measured_only` is a
        ROW — mask to print, handle to post back, slug to suggest — so every
        arm about WHICH accounts are undescribed reads the name out of it."""
        return [m["name"] for m in linked["measured_only"]]

    def test_a_declared_row_joins_the_measured_row_it_names(self):
        accounts.save(row(measured_as="quota-row-1"))
        linked = accounts.join(accounts.read()["accounts"], self.MEASURED)
        self.assertIn("quota-row-1", linked["matched"])
        self.assertEqual(linked["matched"]["quota-row-1"]["id"], "acct-a")
        self.assertEqual(linked["declared_only"], [])
        self.assertEqual(self.undescribed(linked), ["quota-row-2"])

    def test_a_declared_row_with_no_measurement_is_declared_only(self):
        accounts.save(row())
        linked = accounts.join(accounts.read()["accounts"], self.MEASURED)
        self.assertEqual([a["id"] for a in linked["declared_only"]], ["acct-a"])
        self.assertEqual(linked["matched"], {})
        self.assertEqual(self.undescribed(linked), ["quota-row-1", "quota-row-2"])

    def test_a_declared_row_naming_a_measurement_that_is_gone_is_declared_only(self):
        accounts.save(row(measured_as="quota-row-vanished"))
        linked = accounts.join(accounts.read()["accounts"], self.MEASURED)
        self.assertEqual([a["id"] for a in linked["declared_only"]], ["acct-a"])
        self.assertEqual(self.undescribed(linked), ["quota-row-1", "quota-row-2"])

    def test_a_measured_row_nobody_described_is_measured_only(self):
        linked = accounts.join([], self.MEASURED)
        self.assertEqual(self.undescribed(linked), ["quota-row-1", "quota-row-2"])

    def test_no_measurement_at_all_leaves_every_declared_row_reachable(self):
        accounts.save(row(measured_as="quota-row-1"))
        linked = accounts.join(accounts.read()["accounts"], [])
        self.assertEqual([a["id"] for a in linked["declared_only"]], ["acct-a"])
        self.assertEqual(linked["measured_only"], [])

    def test_an_undescribed_row_carries_the_mask_a_handle_and_a_slug(self):
        """WHAT A SURFACE NEEDS TO DRAW A DESCRIBE BUTTON, minted here so no
        renderer has to remember to mask. The mask is what a human reads, the
        handle is what the button posts back, the slug is the id to suggest."""
        address = "someone" + "@" + "example.test"
        measured = [{"name": address, "provider": "vendor-y"},
                    {"name": "quota-row-1", "provider": "vendor-x"}]
        got = {m["name"]: m for m in accounts.join([], measured)["measured_only"]}
        self.assertEqual(got[address]["name_masked"], "s…@example.test")
        self.assertEqual(got[address]["suggest_id"], "someone")
        self.assertNotIn("someone@", got[address]["key"])
        self.assertNotEqual(got[address]["key"], got[address]["name_masked"])
        # the handle names the account it came from, which is the whole reason
        # it exists: the mask cannot be posted back
        self.assertEqual(
            accounts.resolve_measured_key(got[address]["key"], measured), address)
        # THE CONTROL that proves this is the EMAIL masker and not a blanket
        # redaction: a provider name that was never an address is untouched.
        self.assertEqual(got["quota-row-1"]["name_masked"], "quota-row-1")
        self.assertEqual(got["quota-row-1"]["suggest_id"], "quota-row-1")

    def test_two_accounts_that_mask_alike_get_two_handles(self):
        """THE REASON THE MASK IS NOT THE KEY. One letter and a domain is not
        an identity: these two mask to the same string, and a door that
        resolved a masked string would bind the wrong subscription or refuse
        both — leaving the owner unable to describe either one."""
        first = "dana" + "@" + "example.test"
        second = "dave" + "@" + "example.test"
        measured = [{"name": first, "provider": "vendor-y"},
                    {"name": second, "provider": "vendor-y"}]
        rows = accounts.join([], measured)["measured_only"]
        self.assertEqual([m["name_masked"] for m in rows],
                         ["d…@example.test", "d…@example.test"])
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])
        self.assertEqual(accounts.resolve_measured_key(rows[0]["key"], measured),
                         first)
        self.assertEqual(accounts.resolve_measured_key(rows[1]["key"], measured),
                         second)

    def test_a_handle_nothing_measures_resolves_to_nothing(self):
        self.assertIsNone(accounts.resolve_measured_key(
            accounts.measured_key("quota-row-gone"), self.MEASURED))
        self.assertIsNone(accounts.resolve_measured_key("", self.MEASURED))
        # the control on the same call: a live one resolves
        self.assertEqual(accounts.resolve_measured_key(
            accounts.measured_key("quota-row-1"), self.MEASURED), "quota-row-1")

    def test_a_handle_is_id_shaped_so_every_door_that_validates_one_admits_it(self):
        """It rides back through the same payloads an id rides through, and a
        handle a validator refused would be a button that cannot be clicked."""
        address = "someone" + "@" + "example.test"
        handle = accounts.measured_key(address)
        self.assertTrue(handle.startswith("m-") and len(handle) > 8, handle)
        self.assertEqual(accounts._clean_id(handle), handle)

    def test_the_join_never_guesses_from_the_vendor_name(self):
        """A declared row joins ONLY through an explicit measured_as. Guessing
        would bind the wrong subscription to the wrong quota silently, which is
        the failure the declared layer exists to prevent."""
        accounts.save(row(vendor="quota-row-1"))
        linked = accounts.join(accounts.read()["accounts"], self.MEASURED)
        self.assertEqual([a["id"] for a in linked["declared_only"]], ["acct-a"])
        self.assertEqual(linked["matched"], {})


class TotalsTest(AccountsBase):
    def test_spend_is_price_times_count_summed_over_every_row(self):
        accounts.save(row(id="acct-a", price_month="$7", count=1))
        accounts.save(row(id="acct-b", price_month="$20", count=4))
        accounts.save(row(id="acct-c", price_month="$0", count=2))
        t = accounts.read()["totals"]
        self.assertEqual(t["accounts"], 3)
        self.assertEqual(t["units"], 7)
        self.assertEqual(t["monthly_spend"], 87.0)
        self.assertEqual(t["unpriced"], 0)

    def test_an_unpriced_row_is_counted_as_unknown_never_as_free(self):
        accounts.save(row(id="acct-a", price_month="$7", count=1))
        accounts.save(row(id="acct-b", price_month="bundled", count=3))
        accounts.save(row(id="acct-c", count=1, price_month=None))
        t = accounts.read()["totals"]
        self.assertEqual(t["monthly_spend"], 7.0)
        self.assertEqual(t["unpriced"], 2)
        self.assertEqual(t["units"], 5)

    def test_a_price_with_prose_around_it_still_contributes_its_number(self):
        accounts.save(row(price_month="$7 + tax", count=2))
        t = accounts.read()["totals"]
        self.assertEqual(t["monthly_spend"], 14.0)
        self.assertEqual(t["unpriced"], 0)

    def test_an_empty_inventory_totals_zero_without_dividing_by_anything(self):
        t = accounts.read()["totals"]
        self.assertEqual(sorted(t),
                         ["accounts", "monthly_spend", "units", "unpriced"])
        self.assertEqual((t["accounts"], t["units"], t["monthly_spend"],
                          t["unpriced"]), (0, 0, 0.0, 0))


class ValidationMessageTest(AccountsBase):
    def test_the_fields_that_identify_an_account_are_required(self):
        """REQUIRED MEANS REQUIRED TO EXIST, so each arm is a CREATE under its
        own id. On a row already on disk an omitted field is not a missing one
        — the door merges it from what is stored, which is MergeDoorTest's
        subject and the reason each id here is distinct."""
        complete, err, _ = accounts.save(row())
        self.assertIsNone(err, err)                      # the control: a full
        self.assertEqual(complete["id"], "acct-a")       # row IS accepted
        for field in ("vendor", "plan", "reach"):
            with self.subTest(field=field):
                payload = row(id="acct-%s" % field.replace("_", "-"))
                payload.pop(field)
                saved, err, code = accounts.save(payload)
                self.assertIsNone(saved)
                self.assertEqual(code, "refused")
                self.assertIn(field, err)
                self.assertIn("required", err)

    def test_what_an_account_is_FOR_is_asked_for_and_never_invented(self):
        """THE OWNER'S CORRECTION. These two were required, so anything that
        created a row had to put something in them and the only something a
        machine has is the prompt — which then rendered on his card as a
        sentence he had not written, and he read those rows back to us as
        accounts "unknown to me". A row may now exist without them, and it says
        nobody has described it rather than quoting the question as an answer."""
        for field in ("good_for", "not_for"):
            with self.subTest(field=field):
                payload = row(id="acct-%s" % field.replace("_", "-"))
                payload.pop(field)
                saved, err, _code = accounts.save(payload)
                self.assertIsNone(err, err)
                self.assertIsNone(saved[field])
        # AND THE ROW SAYS SO, which is what makes the blank an ask rather than
        # a silence — the control on the same projection is a row that DOES
        # describe itself and is not marked.
        blank = {a["id"]: a for a in accounts.read()["accounts"]}["acct-good-for"]
        self.assertTrue(blank["needs_describe"])
        described, err, _ = accounts.save(row(id="acct-said"))
        self.assertIsNone(err, err)
        self.assertFalse(described["needs_describe"])

    def test_count_refuses_nonsense_in_plain_language(self):
        good, err, _ = accounts.save(row(count=3))
        self.assertIsNone(err, err)
        self.assertEqual(good["count"], 3)
        for value, expect in ((0, "at least 1"), ("many", "whole number"),
                              (5000, "past the limit")):
            with self.subTest(value=value):
                saved, err, _ = accounts.save(row(count=value))
                self.assertIsNone(saved)
                self.assertIn(expect, err)

    def test_a_control_character_in_a_field_is_refused_at_the_seam(self):
        saved, err, _ = accounts.save(row(good_for="live timeline‮ reversed"))
        self.assertIsNone(saved)
        self.assertIn("control or bidi", err)

    def test_an_over_long_field_refuses_loudly_rather_than_trimming(self):
        saved, err, _ = accounts.save(row(good_for="x" * (accounts.MAX_SENTENCE + 1)))
        self.assertIsNone(saved)
        self.assertIn("the limit is %d" % accounts.MAX_SENTENCE, err)

    def test_renews_on_wants_a_date_or_nothing(self):
        saved, err, _ = accounts.save(row(renews_on="next tuesday"))
        self.assertIsNone(saved)
        self.assertIn("a date like", err)
        saved, err, _ = accounts.save(row(renews_on="2027-01-31"))
        self.assertIsNone(err, err)
        self.assertEqual(saved["renews_on"], "2027-01-31")


class SeedFamiliesTest(AccountsBase):
    """NO FAMILY IS ABSENT. The measured half of the seed cannot see a family
    helm does not measure, which is every key-mode family on this host — so the
    owner read a card that looked complete and was not."""

    #: A synthetic catalog in the shape the real one has: a plain family, one
    #: whose vendor is its provider, one whose vendor is its login, and one
    #: that fronts a pool of separately-billed upstream accounts.
    FAMILIES = {
        "fam-plain": {"port": 1, "mode": "proxy"},
        "fam-provider": {"provider": "vendor-p", "mode": "proxy-key"},
        "fam-login": {"auth_type": "login-l", "mode": "proxy-oauth"},
        "fam-pool": {"mode": "proxy-key",
                     "pool_providers": {"pool-a": {}, "pool-b": {}}},
    }

    def setUp(self):
        super().setUp()
        # ORCA'S STORE IS A REAL DIRECTORY ON THE REAL HOST. Unpinned, every
        # exact id list below would be an assertion about which accounts happen
        # to be logged into Orca on the machine running the suite — the same
        # reason SeedTest pins the catalog half.
        from unittest import mock
        orca = mock.patch.object(accountseed, "orca_identities",
                                 lambda *_a, **_k: [])
        orca.start()
        self.addCleanup(orca.stop)

    def ids(self):
        return sorted(a["id"] for a in accounts.read()["accounts"])

    def test_every_declared_family_and_pool_provider_becomes_a_row(self):
        """…and a family that POOLS them is the route to those bills, not one
        of them. The owner read `ds4pro` sitting beside `deepseek`, which it
        pools, and said the two were one DeepSeek account; a row for the family
        as well is a subscription he does not hold. Every pool provider is
        still its own row, which is the half this must not break."""
        written, _skipped, err = accounts.seed([], families=self.FAMILIES)
        self.assertIsNone(err, err)
        self.assertEqual(
            self.ids(),
            ["fam-login", "fam-plain", "fam-provider", "pool-a",
             "pool-b", "x-premium"])
        # THE CONTROL, on the same list: a family with no pool is still a row.
        self.assertIn("fam-plain", written)
        self.assertIn("pool-a", written)
        self.assertNotIn("fam-pool", written)

    def test_a_family_whose_vendor_already_has_a_row_mints_nothing(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same list is `assertTrue(written)` before the absence claims: the seed really ran and really wrote, so "fam-provider is not among them" is about that family
        """The bare `codex` row sat on his card beside the six codex accounts
        it merely reaches, and he could not place it. A family is a ROUTE to a
        vendor; where that vendor already has a row it adds no account, only a
        second name for one."""
        # a row helm cannot measure but the owner declared, under the vendor a
        # family also claims: the family adds no account he does not have.
        accounts.save(row(id="the-p-account", vendor="vendor-p"))
        written, _skipped, err = accounts.seed([], families=self.FAMILIES)
        self.assertIsNone(err, err)
        # THE CONTROL FIRST, unconditionally: the seeder ran and wrote rows, so
        # "fam-provider is not among them" is about that family rather than
        # about a seed that did nothing.
        self.assertTrue(written, "the seed wrote nothing at all")
        self.assertNotIn("fam-provider", written)
        self.assertNotIn("fam-provider", self.ids())
        # THE CONTROL: a family whose vendor nothing claims still gets a row
        self.assertIn("fam-plain", written)
        self.assertIn("fam-login", written)

    def test_the_vendor_is_the_catalogs_own_word_for_who_is_billed(self):
        accounts.seed([], families=self.FAMILIES)
        rows = {a["id"]: a for a in accounts.read()["accounts"]}
        self.assertEqual(rows["fam-provider"]["vendor"], "vendor-p")
        self.assertEqual(rows["fam-login"]["vendor"], "login-l")
        # a family that carries neither is named after itself, never guessed
        self.assertEqual(rows["fam-plain"]["vendor"], "fam-plain")

    def test_a_family_row_is_unconfirmed_undescribed_and_unpriced(self):
        accounts.seed([], families=self.FAMILIES)
        row_ = {a["id"]: a for a in accounts.read()["accounts"]}["fam-plain"]
        self.assertTrue(row_["needs_confirm"])
        self.assertTrue(row_["needs_describe"])
        self.assertIsNone(row_["price_month"])
        self.assertIsNone(row_["billing"])
        self.assertEqual(row_["seeded_from"], accountseed.SEEDED_FROM)
        # the control on the same projection: the owner's own row IS described
        owner = {a["id"]: a for a in accounts.read()["accounts"]}["x-premium"]
        self.assertFalse(owner["needs_describe"])

    def test_a_second_seed_adds_nothing_and_leaves_an_existing_row_identical(self):
        accounts.seed([], families=self.FAMILIES)
        accounts.save({"id": "fam-plain", "plan": "the plan I actually pay for",
                       "good_for": "cheap bulk work", "billing": "sub",
                       "confirmed": True})
        before = self.raw()["accounts"]["fam-plain"]
        written, skipped, err = accounts.seed([], families=self.FAMILIES)
        self.assertIsNone(err, err)
        self.assertEqual(written, [], "a second seed wrote a row it should "
                                      "have found already declared")
        self.assertTrue(skipped, "…and it said so rather than going silent")
        after = self.raw()["accounts"]["fam-plain"]
        self.assertEqual(json.dumps(after, sort_keys=True),
                         json.dumps(before, sort_keys=True),
                         "a declared row must be byte-identical after a second "
                         "seed — not re-stamped, not re-marked unconfirmed")

    def test_a_missing_family_is_added_beside_the_rows_already_there(self):
        accounts.seed([], families={"fam-plain": {"mode": "proxy"}})
        first = self.ids()
        self.assertIn("fam-plain", first)
        self.assertNotIn("fam-login", first)
        written, _skipped, err = accounts.seed([], families=self.FAMILIES)
        self.assertIsNone(err, err)
        self.assertEqual(sorted(written),
                         ["fam-login", "fam-provider", "pool-a", "pool-b"])
        self.assertIn("fam-plain", self.ids())

    def test_an_orca_row_wins_a_collision_with_a_family_of_the_same_name(self):
        """A row that names a login can be bound to an account and a catalog
        row cannot, so the one worth keeping under a contested id is the one
        that knows which subscription it is."""
        rows = {r["id"]: r for r in accounts.seed_candidates(
            [], families=self.FAMILIES,
            orca=[{"email": "plain" + "@" + "fam.test"}])}
        self.assertEqual(rows["anthropic-plain"]["measured_as"],
                         "plain" + "@" + "fam.test")
        # the control on the same dict: a family with no collision keeps its
        # own row, and that row REALLY IS THERE — an absent key would satisfy
        # the assertion below on its own.
        self.assertIn("fam-login", rows)
        self.assertNotIn("measured_as", rows["fam-login"])

    def test_the_real_catalog_leaves_no_family_unaccounted_for(self):
        """DERIVED FROM THE CATALOG ON BOTH SIDES, so this cannot pass by
        transcribing today's family list into an expectation.

        ACCOUNTED FOR IS NOT THE SAME AS "HAS A ROW", and the difference is the
        owner's correction. A family that pools providers is the ROUTE to their
        bills, not a bill: he read one sitting beside the provider it pools and
        said the two were one account. Such a family is accounted for by its
        pool providers, every one of which is a row — which is the property
        that actually keeps the fleet from guessing, and the one this sweeps."""
        from helm import seat
        declared = set(seat.FAMILIES)
        self.assertTrue(declared, "the control: the catalog declares families "
                                  "at all, so the sweep below sees input")
        got = {c["id"] for c in accountseed.candidates("for", "not for")}
        self.assertTrue(got, "the control: the seeder returns candidates at "
                             "all, so the set differences below are real")
        pooling = {f for f in declared
                   if (seat.FAMILIES[f] or {}).get("pool_providers")}
        self.assertEqual((declared - pooling) - got, set(),
                         "a family the catalog declares has no row on the "
                         "owner's card, so the fleet still guesses about it")
        for family in pooling:
            pool = set(seat.FAMILIES[family]["pool_providers"])
            self.assertTrue(pool, family)
            self.assertEqual(pool - got, set(),
                             "%s pools accounts that have no row, so its bills "
                             "are invisible" % family)
            self.assertNotIn(family, got,  # noqa: VACUOUS_ASSERTION — the loop runs only for families that POOL, which is a property of the live catalog and not of this arm; `pool - got` two lines up is this family's unconditional positive control, and `assertTrue(got)` above is the sweep's
                             "%s is a route to those accounts, not a seventh "
                             "subscription beside them" % family)

    def test_a_host_with_no_catalog_still_seeds_what_it_can_see(self):
        from unittest import mock
        with mock.patch.object(accountseed, "_catalog_families",
                               lambda: {}):
            written, _skipped, err = accounts.seed(
                [{"name": "quota-row-1", "provider": "vendor-x"}],
                orca=[{"email": "seatless" + "@" + "example.test"}])
        self.assertIsNone(err, err)
        self.assertIn("x-premium", written)
        self.assertIn("anthropic-seatless", written)
        # …and NOT the measured one, which needs no row: it is already a row on
        # his screen, and this is the control that the seed ran at all.
        self.assertNotIn("vendor-x-quota-row-1", written)


class SeedTest(AccountsBase):
    MEASURED = [{"name": "quota-row-1", "provider": "vendor-x", "tier": "Big"}]
    #: A subscription NOTHING on this host measures — the only kind the seed
    #: still mints a row for, now that a measured account is described in place
    #: on the row the quota table already shows.
    ORCA = [{"email": "seatless" + "@" + "example.test"}]

    def setUp(self):
        super().setUp()
        # THE CATALOG HALF IS A SEPARATE CLAIM (SeedFamiliesTest). These arms
        # pin the MEASURED half's exact row set, and a count that moved every
        # time somebody added a family to the catalog would be an assertion
        # about the ambient tree rather than about the seeder.
        from unittest import mock
        patch = mock.patch.object(accountseed, "candidates",
                                  lambda *_a, **_k: [])
        patch.start()
        self.addCleanup(patch.stop)
        # …and the OTHER ambient source, for the same reason. Orca's account
        # store is a real directory on the real host: unpinned, every count
        # below would depend on which accounts happen to be logged into Orca on
        # the machine running the suite.
        orca = mock.patch.object(accountseed, "orca_identities",
                                 lambda *_a, **_k: [])
        orca.start()
        self.addCleanup(orca.stop)

    def test_the_seed_carries_the_owners_own_x_row_verbatim(self):
        written, skipped, err = accounts.seed(self.MEASURED)
        self.assertIsNone(err, err)
        self.assertIn("x-premium", written)
        got = next(a for a in accounts.read()["accounts"] if a["id"] == "x-premium")
        # THE CATALOG'S SPELLING OF THE SAME VENDOR, which is also the one the
        # owner corrected this row to by hand. Spelled `x` it read as a
        # different vendor from the `grok` family that reaches it, so the seed
        # offered him two rows for one $7 plan and he named them as duplicates.
        self.assertEqual(got["vendor"], "xai")
        self.assertIn("about $7/month", got["plan"])
        self.assertIn("blocked from", got["good_for"])
        self.assertIn("too little Grok quota", got["not_for"])
        self.assertTrue(got["reach"].startswith(accounts.OWNER_ONLY))

    def test_every_seeded_row_says_where_it_came_from_and_asks_to_be_confirmed(self):
        accounts.seed(self.MEASURED, orca=self.ORCA)
        rows = accounts.read()["accounts"]
        self.assertEqual(len(rows), 2, rows)
        for a in rows:
            self.assertTrue(a["seeded_from"], a["id"])
            self.assertTrue(a["needs_confirm"], a["id"])

    def test_a_measured_account_gets_NO_row_and_is_described_in_place(self):
        """THE OWNER'S RULING: "i dont understand why the accounts we pay for
        [aren't handled] by making it editable and adding fields intelligently,
        to compose instead of make new".

        An account the quota table already shows IS a row on his screen. A
        declared record minted beside it made two rows out of one subscription,
        and filled the second with the seeder's own placeholder sentences —
        which is most of why half his card read as accounts he did not
        recognise. Nothing is minted; the measured row carries empty declared
        cells until he fills one."""
        written, _skipped, err = accounts.seed(self.MEASURED)
        self.assertIsNone(err, err)
        ids = sorted(a["id"] for a in accounts.read()["accounts"])
        self.assertEqual(ids, ["x-premium"], ids)
        self.assertNotIn("quota-row-1", [a.get("measured_as")
                                         for a in accounts.read()["accounts"]])
        # THE CONTROL on the same call: the seeder RAN and really wrote — the
        # owner's own row is there — so "the measured one is absent" is about
        # that account and not about a seed that did nothing.
        self.assertEqual(written, ["x-premium"])

    def test_a_subscription_NOTHING_measures_is_still_seeded_and_binds(self):
        """The other half of the same ruling: a prepaid balance, a seatless
        subscription or a login only Orca can see has no measured row to attach
        to, so a declared row is the only way it reaches the surface at all."""
        accounts.seed(self.MEASURED, orca=self.ORCA)
        seeded = [a for a in accounts.read()["accounts"]
                  if a["measured_as"] == self.ORCA[0]["email"]]
        self.assertEqual(len(seeded), 1, accounts.read()["accounts"])
        self.assertTrue(seeded[0]["seeded_from"])

    def test_two_seeded_accounts_sharing_a_local_part_get_two_rows(self):
        """Two logins can share a local part. One id for both means the second
        save REPLACES the first, and a real subscription disappears from the
        inventory at seed time with nothing said."""
        written, _skipped, err = accounts.seed([], orca=[
            {"email": "same" + "@" + "one.test"},
            {"email": "same" + "@" + "two.test"}])
        self.assertIsNone(err, err)
        rows = accounts.read()["accounts"]
        self.assertEqual(sorted(a["id"] for a in rows),
                         ["anthropic-same", "anthropic-same-two", "x-premium"])
        bound = sorted(a["measured_as"] for a in rows if a["measured_as"])
        self.assertEqual(bound, ["same" + "@" + "one.test",
                                 "same" + "@" + "two.test"])

    def test_a_seeded_row_is_marked_as_not_yet_described(self):
        """The seeder must fill `good_for` to write the row at all, so what it
        fills it with is the ASK. A surface that renders the ask as a
        description tells the owner the account is already described — measured
        on his real board, where the seed pre-declared all fourteen measured
        accounts and the one-click describe affordance became unreachable."""
        rows = {r["id"]: r for r in accounts.seed_rows([], orca=self.ORCA)}
        for candidate in rows.values():
            accounts.save(candidate)
        view = {a["id"]: a for a in accounts.read()["accounts"]}
        seeded = view["anthropic-seatless"]
        self.assertTrue(seeded["needs_describe"])
        # the OWNER'S OWN ROW is described — the control that says this flag
        # tracks the sentence and not merely the fact of being seeded
        self.assertFalse(view["x-premium"]["needs_describe"])
        self.assertTrue(view["x-premium"]["needs_confirm"])

    def test_describing_a_seeded_row_clears_the_flag(self):
        accounts.save(dict(row(), good_for=accounts.NOT_DESCRIBED_FOR))
        self.assertTrue(accounts.read()["accounts"][0]["needs_describe"])
        accounts.save({"id": "acct-a", "good_for": "reading the live timeline"})
        self.assertFalse(accounts.read()["accounts"][0]["needs_describe"])

    OWNER_SENTENCE = "the owner's own sentence about this one"

    def racing_measured(self):
        """One measured account whose seeded id is predictable, so the arms
        below can name the row both writers aim at."""
        return [{"email": "someone" + "@" + "example.test"}]

    def test_a_save_landing_inside_the_seed_is_not_overwritten(self):
        """THE WINDOW, MEASURED. The emptiness check and the writes are ONE
        mutation, so a first save that lands after the seeder has decided what
        to write still beats it: the seeder takes the lock, sees a file with
        something in it and refuses. `seed_rows` is the last thing seed
        computes before the lock, so a save driven from there lands exactly in
        the window the old order left open."""
        from unittest import mock
        real = accounts.seed_rows

        def racing(m=None, orca=None):
            rows = real(m, orca)
            saved, err, _code = accounts.save(
                row(id="anthropic-someone", good_for=self.OWNER_SENTENCE))
            self.assertIsNone(err, err)       # the racing writer really wrote
            self.assertEqual(saved["id"], "anthropic-someone")
            return rows

        with mock.patch.object(accounts, "seed_rows", racing):
            written, skipped, err = accounts.seed([], orca=self.racing_measured())
        self.assertIsNone(err, err)
        self.assertNotIn("anthropic-someone", written,
                         "the seeder wrote over a row that appeared after it "
                         "chose what to write")
        self.assertTrue(any("anthropic-someone" in s for s in skipped), skipped)
        stored = self.raw()["accounts"]
        self.assertEqual(sorted(stored), ["anthropic-someone", "x-premium"],
                         stored)
        self.assertEqual(stored["anthropic-someone"]["good_for"],
                         self.OWNER_SENTENCE,
                         "the seeder overwrote the owner's own first sentence "
                         "with its placeholder")

    def test_a_seed_racing_a_save_on_the_real_file_leaves_his_sentence(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is on the racing SAVE's error, and its unconditional positive control on the same call is the assertEqual of that saved row's good_for on disk, which only a save that really landed can satisfy
        """The same claim with real threads on a real file, both writers going
        for the SAME id. Either order is legal and only one outcome is: the
        seeder refuses a file that already has a row, and a save over a seeded
        row is the owner describing it. What may never be there afterwards is
        the placeholder."""
        import threading
        gate = threading.Barrier(2)
        out = {}

        def saver():
            gate.wait()
            out["save"] = accounts.save(
                row(id="anthropic-someone", good_for=self.OWNER_SENTENCE))

        def seeder():
            gate.wait()
            out["seed"] = accounts.seed([], orca=self.racing_measured())

        threads = [threading.Thread(target=saver), threading.Thread(target=seeder)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), "a writer hung")
        self.assertIsNone(out["save"][1], out["save"][1])
        stored = self.raw()["accounts"]["anthropic-someone"]
        self.assertEqual(stored["good_for"], self.OWNER_SENTENCE)
        self.assertNotEqual(stored["good_for"], accounts.NOT_DESCRIBED_FOR)

    def test_the_written_and_skipped_shape_is_ids_and_sentences(self):
        """The return shape is what two CLI branches print, so it is pinned
        beside the race: ids in `written`, and a skipped row named with its
        reason in parentheses."""
        long_name = "abc" + "@" + ("x" * 90) + ".test"
        written, skipped, err = accounts.seed(
            self.MEASURED, orca=self.ORCA + [{"email": long_name}])
        self.assertIsNone(err)
        self.assertEqual(sorted(written),
                         sorted(a["id"] for a in accounts.read()["accounts"]))
        self.assertTrue(all(isinstance(w, str) for w in written))
        self.assertEqual(len(skipped), 1, skipped)
        self.assertTrue(skipped[0].startswith("anthropic-abc ("), skipped)
        self.assertIn("the limit is", skipped[0])

    def test_a_seed_over_a_declared_row_adds_beside_it_and_never_over_it(self):  # noqa: VACUOUS_ASSERTION — `written` is pinned WHOLE by an assertEqual against a two-element list, which no empty or short answer can satisfy, and his own row is then read back off disk by value
        """The refusal this replaced was safe and useless: the inventory stops
        being empty after the first seed, so nothing helm learned afterwards
        could ever reach his card. What must still be true is the half that
        mattered — his own row is left exactly as it is."""
        accounts.save(row(good_for="the owner's own sentence"))
        before = self.raw()["accounts"]["acct-a"]
        written, skipped, err = accounts.seed(self.MEASURED, orca=self.ORCA)
        self.assertIsNone(err, err)
        self.assertEqual(sorted(written),
                         ["anthropic-seatless", "x-premium"])
        self.assertNotIn("acct-a", written)  # noqa: VACUOUS_ASSERTION — the assertEqual one line up is the unconditional positive control on the same list: it pins the whole of `written`, so an empty or short answer fails before this line is reached
        # the unconditional positive control on the SAME return: the seeder
        # really ran and really wrote, so "acct-a is not in it" is a statement
        # about this row rather than about an empty answer
        self.assertTrue(written)
        self.assertEqual(skipped, [])
        after = self.raw()["accounts"]["acct-a"]
        self.assertEqual(json.dumps(after, sort_keys=True),
                         json.dumps(before, sort_keys=True),
                         "the seeder rewrote a row the owner had declared")

    def test_a_measured_name_that_is_email_shaped_seeds_a_slug_not_the_address(self):
        accounts.seed([], orca=[{"email": "person" + "@" + "example.test"}])
        rows = accounts.read()["accounts"]
        ids = sorted(a["id"] for a in rows)
        # the whole id set, stated positively: the seeded id is the slugged
        # LOCAL PART under its provider, and the address never becomes a key
        self.assertEqual(ids, ["anthropic-person", "x-premium"])
        # THE JOIN KEY KEEPS THE PROVIDER'S OWN SPELLING, because that is the
        # only string the measured row can be found by — and every surface
        # renders the MASKED form instead.
        bound = next(a for a in rows if a["id"] == "anthropic-person")
        self.assertEqual(bound["measured_as"], "person" + "@" + "example.test")
        self.assertNotIn("erson", bound["measured_as_masked"])
        self.assertTrue(bound["measured_as_masked"].endswith("example.test"))

    def test_a_measured_as_that_is_email_shaped_is_accepted_and_masked_in_the_value(self):
        """The one exemption, and it is narrow: `measured_as` holds the name
        the PROVIDER minted, not free text the owner typed, and without it a
        declared row cannot find its quota row at all. Every other field still
        refuses an address. THIS ARM IS ABOUT THE VALUE; the arm that says what
        the CLI PRINTS is in CliTest, because a masked field beside an
        unmasked renderer is how the claim came to be false."""
        address = "person" + "@" + "example.test"
        saved, err, _ = accounts.save(row(measured_as=address))
        self.assertEqual(saved["measured_as"], address, err)
        # the mask stated as a VALUE: one letter and the domain, which is all a
        # reader needs to tell two accounts apart
        self.assertEqual(saved["measured_as_masked"], "p…@example.test")
        self.assertNotEqual(saved["measured_as_masked"], address)
        for field in ("vendor", "plan", "good_for", "not_for", "reach", "notes"):
            with self.subTest(field=field):
                _saved, err, _ = accounts.save(row(**{field: address}))
                self.assertIn("email address", err)


class OneRowPerSubscriptionTest(AccountsBase):
    """The owner, having filled this card by hand: "some of them were dupes or
    unknown to me (the ones i didnt edit)". Every duplicate he named came from
    the seed keying on the provider's handle for a credential DIRECTORY rather
    than on the account being billed."""

    #: Two homes holding ONE login, the default pointer that resolves to a
    #: third, and a home whose NAME lies about who is logged into it — the four
    #: shapes the live fleet has, none of them typed as an address.
    TWO_HOMES = [
        {"name": "one" + "@" + "v.test", "provider": "anthropic",
         "email": "one" + "@" + "v.test", "tier": "Big"},
        {"name": "one" + "@" + "v.test#second-home", "provider": "anthropic",
         "email": "one" + "@" + "v.test", "tier": "Big"},
        {"name": "(default-claude)", "provider": "anthropic",
         "email": "other" + "@" + "v.test", "tier": "Big"},
        {"name": "other" + "@" + "v.test", "provider": "anthropic",
         "email": "other" + "@" + "v.test", "tier": "Big"},
    ]

    def setUp(self):
        super().setUp()
        from unittest import mock
        for name in ("candidates", "orca_identities"):
            patch = mock.patch.object(accountseed, name, lambda *_a, **_k: [])
            patch.start()
            self.addCleanup(patch.stop)

    def seeded(self, measured=None, **kw):
        written, skipped, err = accounts.seed(
            self.TWO_HOMES if measured is None else measured, **kw)
        self.assertIsNone(err, err)
        return written, skipped

    def test_two_homes_holding_one_login_are_one_subscription(self):
        """The grouping the whole surface is keyed on. He was shown one Max
        plan as two accounts because the key was the credential DIRECTORY."""
        groups = accounts.measured_groups(self.TWO_HOMES)
        by = {ident: [m["name"] for m in g] for ident, g in groups}
        one = ("anthropic", "one" + "@" + "v.test")
        self.assertEqual(sorted(by[one]),
                         ["one" + "@" + "v.test", "one" + "@" + "v.test#second-home"])
        # …and the PRIMARY is the plain name, never the `#home` tiebreak: a
        # retired home would otherwise unbind a row he had described.
        self.assertEqual(by[one][0], "one" + "@" + "v.test")
        # THE CONTROL on the same call: two DIFFERENT logins stay two groups.
        self.assertEqual(len(groups), 2, by)

    def test_a_default_handle_joins_the_login_it_points_at(self):
        """It is whichever account is selected — a pointer at a subscription,
        never one of its own."""
        by = {ident: [m["name"] for m in g]
              for ident, g in accounts.measured_groups(self.TWO_HOMES)}
        other = ("anthropic", "other" + "@" + "v.test")
        self.assertIn("(default-claude)", by[other])
        self.assertEqual(by[other][0], "other" + "@" + "v.test")

    def test_one_subscription_is_one_key_and_the_key_names_no_login(self):
        """Both halves of the wire group on this, and it rides to the browser:
        it must group the same accounts and read as nothing."""
        one = accounts.subscription_identity("anthropic", None, "one" + "@" + "v.test")
        same = accounts.subscription_identity(
            "anthropic", "one" + "@" + "v.test#second-home", None)
        self.assertEqual(accounts.subscription_key(one),
                         accounts.subscription_key(same))
        # …and a DIFFERENT login is a different key — the control that says the
        # equality above is about the login and not about every input mapping
        # to one value.
        other = accounts.subscription_identity("anthropic", None, "other" + "@" + "v.test")
        self.assertNotEqual(accounts.subscription_key(one),
                            accounts.subscription_key(other))
        # THE KEY IS REAL AND SHAPED LIKE AN ID — the unconditional positive
        # control, without which "it does not contain the login" would pass on
        # a function that answered None for everything.
        self.assertTrue(accounts.subscription_key(one).startswith("s-"))
        self.assertGreater(len(accounts.subscription_key(one)), 8)
        self.assertNotIn("v.test", accounts.subscription_key(one))
        self.assertIsNone(accounts.subscription_key(None))

    def test_a_default_with_no_readable_login_is_its_own_group(self):
        """Rather than being folded in with every other unresolvable handle. It
        stays visible either way: the join still reports it as a measured row
        nobody described, which is the control this arm carries."""
        measured = [{"name": "(default-codex)", "provider": "codex"}]
        self.assertIsNone(accounts.measured_identity(measured[0]))
        self.assertEqual(accounts.measured_groups(measured), [])
        seen = accounts.join([], measured)["measured_only"]
        self.assertEqual([m["name"] for m in seen], ["(default-codex)"])

    def test_a_home_whose_name_lies_keys_to_the_login_it_really_holds(self):
        """The live fleet has one: the home NAMED for one account is logged
        into another, so keyed by name it counted a plan he does not have and
        hid one he does."""
        measured = [{"name": "named-for-a", "provider": "codex",
                     "email": "really-b" + "@" + "v.test"}]
        self.assertEqual(accounts.measured_identity(measured[0]),
                         ("codex", "really-b" + "@" + "v.test"))
        # THE CONTROL: with no email to read, the name is all there is
        self.assertEqual(
            accounts.measured_identity({"name": "named-for-a", "provider": "codex"}),
            ("codex", "named-for-a"))

    def test_orca_supplies_a_login_no_credential_home_measures(self):
        """His words: one of his Max plans "was not listed". No home measures
        as it, because the home named for it holds another account."""
        only = "unseen" + "@" + "v.test"
        written, _ = self.seeded([], orca=[{"email": only}])
        rows = {a["measured_as"] for a in accounts.read()["accounts"]}
        self.assertIn(only, rows)
        before = sorted(a["id"] for a in accounts.read()["accounts"])
        # THE CONTROL on the same inventory: an Orca account a home DOES
        # measure adds nothing — the fix for a missing row must not mint
        # duplicates — and the rows already there are all still there.
        self.seeded(self.TWO_HOMES, orca=[{"email": "one" + "@" + "v.test"}])
        self.assertEqual(sorted(a["id"] for a in accounts.read()["accounts"]),
                         before)

    def test_a_row_added_by_hand_is_not_duplicated_by_the_next_seed(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same observable is `assertEqual(bound, ["my-own-name"])`: a NON-EMPTY list, so a seed that wrote nothing at all fails it, and the second seed asserts a non-empty `written2`
        """A row the owner added by hand this morning, against a login
        this host cannot measure. A seed that went looking for that very
        account must not put a second row beside it."""
        mine = "unseen" + "@" + "v.test"
        accounts.save(row(id="my-own-name", vendor="anthropic",
                          measured_as=mine))
        _written, skipped = self.seeded([], orca=[{"email": mine}])
        self.assertTrue(any("another name" in s for s in skipped), skipped)
        bound = [a["id"] for a in accounts.read()["accounts"]
                 if a["measured_as"] == mine]
        # ONE row, and it is HIS: the count is the no-duplicate half and the
        # name is the not-clobbered half, both on the same observable.
        self.assertEqual(bound, ["my-own-name"],
                         "the seed put a second row beside the one he added")
        # THE CONTROL on the same call shape: a login NOTHING has claimed IS
        # seeded, so the arm above is about the claim and not about `orca`
        # being ignored.
        written2, _ = self.seeded([], orca=[{"email": "nobody" + "@" + "v.test"}])
        self.assertTrue([i for i in written2 if i != "x-premium"],
                        "an unclaimed login must still be seeded")


class OrcaStoreReadTest(unittest.TestCase):
    """THE READER NOTHING WAS EXERCISING.

    Every other arm about the Orca source hands `seed` an `orca=[...]` list or
    patches `orca_identities` to `[]` — so the function that actually walks
    Orca's store has been injected around in every test that mentions it, and
    the live path was untested by construction. It never raises on a missing or
    unreadable store, which is right and is also the hiding place: a reader that
    answers "nothing to add" to every shape looks identical to a reader that
    works, and the whole suite would have taken that branch.

    It is not a small path. This is the one that finds a subscription no
    credential home on the machine can see — the owner's "there was no
    <login> max plan listed" — so a silent zero here is a plan he pays for
    that never reaches his card again.

    The store's SHAPE is the thing under test: the account dirs, the `auth`
    subdirectory, and the `emailAddress` field inside `oauth-account.json`.
    Every one of those is a fact about somebody else's format, which is exactly
    the kind that drifts without anything here failing."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-orca-store-")
        self.addCleanup(shutil.rmtree, self.root, True)
        from unittest import mock
        from helm.cred import orca
        self.orca = orca
        patch = mock.patch.object(orca, "orca_accounts_root", lambda: self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def plant(self, account_id, doc):
        auth = os.path.join(self.root, account_id, self.orca.ORCA_AUTH_DIR)
        os.makedirs(auth, exist_ok=True)
        if doc is not None:
            with open(os.path.join(auth, self.orca.ORCA_ACCOUNT_JSON), "w",
                      encoding="utf-8") as f:
                json.dump(doc, f)
        return auth

    def test_it_reads_the_login_out_of_a_real_store(self):
        """Planted in the layout Orca really uses, measured off the live store:
        `<account id>/auth/oauth-account.json`, with the login under
        `emailAddress`."""
        self.plant("2beb1566-5944-4869-9328-1689c51e75b1",
                   {"accountUuid": "b23a", "emailAddress": "only" + "@" + "v.test",
                    "organizationUuid": "dfad", "billingType": "stripe_subscription"})
        self.assertEqual(accountseed.orca_identities(),
                         [{"email": "only" + "@" + "v.test"}])

    def test_it_reads_every_account_once_and_folds_a_repeat(self):
        for n, email in ((1, "a" + "@" + "v.test"), (2, "b" + "@" + "v.test"),
                         (3, "A" + "@" + "V.test")):
            self.plant("id-%d" % n, {"emailAddress": email})
        got = [a["email"] for a in accountseed.orca_identities()]
        self.assertEqual(sorted(got), ["a" + "@" + "v.test", "b" + "@" + "v.test"])

    def test_a_dir_with_no_identity_file_is_skipped_not_fatal(self):
        """THE CONTRAST THAT MAKES THE ARMS ABOVE MEAN SOMETHING: the reader
        does degrade, and the degrade does not swallow its neighbours."""
        self.plant("broken", None)
        self.plant("good", {"emailAddress": "kept" + "@" + "v.test"})
        self.assertEqual(accountseed.orca_identities(),
                         [{"email": "kept" + "@" + "v.test"}])

    def test_a_document_that_is_not_an_identity_is_skipped(self):
        self.plant("wrong-shape", {"accountUuid": "b23a"})   # no emailAddress
        self.plant("also-wrong", ["not", "a", "mapping"])
        self.plant("good", {"emailAddress": "kept" + "@" + "v.test"})
        self.assertEqual(accountseed.orca_identities(),
                         [{"email": "kept" + "@" + "v.test"}])

    def test_no_store_at_all_is_no_accounts_and_never_a_raise(self):
        """A host with no Orca is the ordinary case, and the declared inventory
        has to be readable there. THE MUST-HIT is the arm above: the same call
        DOES return a login when one is planted, so this empty answer is about
        the absent store rather than a reader that always answers nothing."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertEqual(accountseed.orca_identities(), [])

    def test_the_seed_reaches_this_reader_when_nothing_is_injected(self):
        """The seam itself: with no `orca=` argument the seed calls the real
        reader. Every other arm on this source passes one, so without this the
        injected path is the only path anything runs."""
        self.plant("id-1", {"emailAddress": "seatless" + "@" + "v.test"})
        rows = accounts.seed_rows([])
        bound = [r["measured_as"] for r in rows if r.get("measured_as")]
        self.assertEqual(bound, ["seatless" + "@" + "v.test"])
        self.assertEqual(
            [r["seeded_from"] for r in rows if r.get("measured_as")],
            [accountseed.SEEDED_FROM_ORCA])


class TombstoneTest(AccountsBase):
    """A removed row stays removed. Nobody deleted the duplicates by hand today
    because the next seed would re-mint them — the surface teaching him that
    tidying his own inventory does not work."""

    #: nothing measures this one, which is what the seed still mints for.
    ORCA = [{"email": "seatless" + "@" + "example.test"}]

    def setUp(self):
        super().setUp()
        from unittest import mock
        for name in ("candidates", "orca_identities"):
            patch = mock.patch.object(accountseed, name, lambda *_a, **_k: [])
            patch.start()
            self.addCleanup(patch.stop)

    def seed(self):
        return accounts.seed([], orca=self.ORCA)

    def test_a_removed_row_is_not_re_minted_by_the_next_seed(self):  # noqa: VACUOUS_ASSERTION — two unconditional positive controls precede the absence: the second seed still mints the owner's own row, and `skipped` is non-empty, so the seeder demonstrably considered this id and declined it
        written, _skipped, err = self.seed()
        self.assertIsNone(err, err)
        seeded_id = next(i for i in written if i != "x-premium")
        ok, err, _ = accounts.remove(seeded_id)
        self.assertTrue(ok, err)
        again, skipped, err = self.seed()
        self.assertIsNone(err, err)
        # THE CONTROL: the seed ran over the same sources and is STILL minting
        # the owner's own row, so the absence below is about the tombstone.
        self.assertIn("x-premium", again + [a["id"] for a in accounts.read()["accounts"]])
        self.assertTrue(skipped, "the seed considered nothing at all")
        self.assertNotIn(seeded_id, again)
        self.assertTrue(any("stays removed" in s for s in skipped), skipped)
        self.assertNotIn(seeded_id, [a["id"] for a in accounts.read()["accounts"]])

    def test_the_control_a_row_never_removed_is_re_minted_after_a_hand_delete(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the tombstone above, and its own claim is a PRESENCE (`assertIn(seeded_id, again)`): the row comes back when nothing tombstoned it
        """THE UNCONDITIONAL POSITIVE CONTROL on the same observable: with the
        row deleted OUTSIDE this door — by hand, in the file — the seed puts it
        straight back. That is what makes the arm above a statement about the
        tombstone rather than about the seed's skip-by-id."""
        written, _s, err = self.seed()
        self.assertIsNone(err, err)
        seeded_id = next(i for i in written if i != "x-premium")
        raw = self.raw()
        raw["accounts"].pop(seeded_id)
        self.plant(raw)
        again, _skipped, err = self.seed()
        self.assertIsNone(err, err)
        self.assertIn(seeded_id, again)

    def test_writing_the_row_again_lifts_its_tombstone(self):
        """A tombstone says "do not RE-MINT this", never "this name is banned"."""
        self.seed()
        seeded_id = next(a["id"] for a in accounts.read()["accounts"]
                         if a["id"] != "x-premium")
        accounts.remove(seeded_id)
        self.assertIn(seeded_id, accounts.removed_ids())
        saved, err, _ = accounts.save(row(id=seeded_id))
        self.assertIsNone(err, err)
        self.assertEqual(saved["id"], seeded_id)
        self.assertNotIn(seeded_id, accounts.removed_ids())

    def test_a_remove_that_finds_nothing_tombstones_nothing(self):
        """A fence around a name that was never there would silently block a
        row he may yet want."""
        self.seed()
        ok, _err, _code = accounts.remove("never-existed")
        self.assertFalse(ok)
        self.assertEqual(accounts.removed_ids(), [])
        # THE CONTROL on the same reader: a real removal DOES leave one
        accounts.remove("x-premium")
        self.assertEqual(accounts.removed_ids(), ["x-premium"])

    def test_an_inventory_nobody_deleted_from_keeps_its_old_shape(self):
        """An empty tombstone list is not written, so every older reader sees
        the file it expects."""
        accounts.save(row())
        self.assertNotIn("removed", self.raw())
        accounts.remove(row()["id"])
        self.assertIn("removed", self.raw())


class DuplicateTellTest(AccountsBase):
    """What the CARD says about which rows are the same bill. He could not tell
    them apart, and every fact needed to tell him was already on the wire."""

    MEASURED = [
        {"name": "one" + "@" + "v.test", "provider": "anthropic",
         "email": "one" + "@" + "v.test"},
        {"name": "one" + "@" + "v.test#second", "provider": "anthropic",
         "email": "one" + "@" + "v.test"},
        {"name": "(default-claude)", "provider": "anthropic",
         "email": "one" + "@" + "v.test"},
    ]

    def decorated(self):
        return {a["id"]: a for a in
                accounts.decorate_duplicates(accounts.read()["accounts"],
                                             self.MEASURED)}

    def test_two_rows_naming_one_login_each_name_the_other(self):
        accounts.save(row(id="home-a", vendor="anthropic",
                          measured_as="one" + "@" + "v.test"))
        accounts.save(row(id="home-b", vendor="anthropic",
                          measured_as="one" + "@" + "v.test#second"))
        # THE CONTROL, saved in the same breath: a different login says nothing
        accounts.save(row(id="elsewhere", vendor="anthropic",
                          measured_as="two" + "@" + "v.test"))
        got = self.decorated()
        self.assertEqual(got["home-a"]["duplicate_of"], ["home-b"])
        self.assertEqual(got["home-b"]["duplicate_of"], ["home-a"])
        self.assertEqual(got["elsewhere"]["duplicate_of"], [])

    def test_a_row_bound_to_a_default_is_called_a_pointer(self):
        accounts.save(row(id="the-default", vendor="anthropic",
                          measured_as="(default-claude)"))
        accounts.save(row(id="a-real-one", vendor="anthropic",
                          measured_as="one" + "@" + "v.test"))
        got = self.decorated()
        self.assertTrue(got["the-default"]["points_at_default"])
        self.assertFalse(got["a-real-one"]["points_at_default"])

    def test_only_the_unbound_row_asks_the_vendor_question(self):
        """Hung on every row of a vendor it put a seven-name list on six
        accounts whose logins helm had already proven — noise of exactly the
        kind he opened this card to be rid of."""
        accounts.save(row(id="proven", vendor="anthropic",
                          measured_as="one" + "@" + "v.test"))
        accounts.save(row(id="unbound", vendor="anthropic"))
        got = self.decorated()
        self.assertEqual(got["unbound"]["vendor_siblings"], ["proven"])
        self.assertEqual(got["proven"]["vendor_siblings"], [])

    def test_a_lone_row_of_its_vendor_asks_nothing(self):
        """One row for a vendor is not a question about anything."""
        accounts.save(row(id="alone", vendor="only-vendor"))
        self.assertEqual(self.decorated()["alone"]["vendor_siblings"], [])
        # THE CONTROL on the same row: give that vendor a second row and the
        # very same row starts asking. Without this, an implementation that
        # never asked at all would pass.
        accounts.save(row(id="beside-it", vendor="only-vendor"))
        self.assertEqual(self.decorated()["alone"]["vendor_siblings"],
                         ["beside-it"])


class AgentPointerTest(AccountsBase):
    def test_the_pointer_line_fits_the_declared_character_budget(self):
        accounts.save(row())
        line = accounts.agent_line()
        self.assertTrue(line)
        self.assertLessEqual(len(line), 200)
        self.assertLessEqual(len(line), accounts.POINTER_CAP)
        self.assertNotIn("\n", line)

    def test_the_pointer_names_the_verb_that_answers_and_no_account_contents(self):
        accounts.save(row())
        line = accounts.agent_line()
        self.assertIn("helm accounts", line)
        self.assertNotIn("vendor-x", line,
                         "the verb is the live answer; contents baked into a "
                         "line are stale the moment he edits one")

    def test_there_is_no_pointer_when_there_is_nothing_to_point_at(self):  # noqa: VACUOUS_ASSERTION — the EMPTY string is the product law (a pointer to nothing is the per-turn paragraph this must not become); the positive control on the same call is test_the_pointer_line_fits_the_declared_character_budget
        self.assertEqual(accounts.agent_line(), "")

    def test_an_unreadable_inventory_points_at_the_reason_not_at_zero(self):
        self.plant("{this is not json")
        line = accounts.agent_line()
        self.assertIn("unreadable", line)
        self.assertLessEqual(len(line), accounts.POINTER_CAP)

    def test_the_teach_commands_are_real_verbs_with_real_flags(self):
        accounts.save(row())
        commands = accounts.store_command()
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0].startswith("helm store add reference "))
        self.assertIn(accounts.STORE_ENTRY_ID, commands[0])
        self.assertIn("accounts,subscription", commands[0])
        self.assertTrue(commands[1].startswith("helm store gloss "))
        self.assertIn("--set ", commands[1])
        from helm import inject
        gloss = commands[1].split("--set ", 1)[1]
        self.assertEqual(gloss, accounts.STORE_GLOSS)
        self.assertLessEqual(len(gloss), inject.LINE_CAP)

    def test_the_injected_line_carries_no_count_only_the_pointer(self):
        """A gloss is FIXED TEXT that fires unchanged for as long as it lives,
        so a number inside it is a measurement that keeps steering after it
        expires: the owner edits one account and every seat's context is wrong.
        `agent_line` is computed at read time and may carry the count; this
        may not."""
        accounts.save(row())
        gloss = accounts.store_command()[1].split("--set ", 1)[1]
        self.assertIn("helm accounts", gloss)
        self.assertEqual([c for c in gloss if c.isdigit()], [])
        # the control: the LIVE line, same subject, does carry its count
        self.assertTrue(any(c.isdigit() for c in accounts.agent_line()))

    def test_the_keyword_set_stays_narrow_enough_not_to_burn_a_jit_slot(self):
        """`plan` and bare `account` were cut on purpose: they collide with
        plan-mode and ordinary chatter, and a false fire costs one of only
        four JIT slots."""
        self.assertNotIn("plan", accounts.STORE_KEYWORDS)
        self.assertNotIn("account", accounts.STORE_KEYWORDS)
        self.assertIn("accounts", accounts.STORE_KEYWORDS)


class CredsPointerTest(AccountsBase):
    def test_helm_creds_ends_with_the_pointer_only_when_something_is_declared(self):
        from helm import creds
        self.assertEqual(creds._declared_pointer(), "")
        accounts.save(row())
        self.assertIn("helm accounts", creds._declared_pointer())

    def test_the_pointer_helper_never_raises_out_of_the_scorecard(self):
        from helm import creds
        self.plant("{this is not json")
        line = creds._declared_pointer()
        self.assertIsInstance(line, str)
        self.assertIn("unreadable", line,
                      "it must not answer the empty string here: a scorecard "
                      "silently omitting the pointer reads as 'nothing "
                      "declared', which is the lie this lane exists to end")


class CliTest(AccountsBase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = accounts.cmd_accounts(args)
        return rc, out.getvalue(), err.getvalue()

    def test_the_bare_verb_says_what_to_do_when_nothing_is_declared(self):
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("nothing declared yet", out)
        self.assertIn("quota tab", out)

    def test_the_bare_verb_lists_headline_for_not_for_and_totals(self):
        accounts.save(row())
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("acct-a", out)
        self.assertIn("reading the live timeline", out)
        self.assertIn("NOT:", out)
        self.assertIn("totals:", out)

    def test_json_carries_the_rows_the_totals_and_the_join(self):
        accounts.save(row())
        rc, out, _ = self.run_cli(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual([a["id"] for a in payload["accounts"]], ["acct-a"])
        self.assertIn("totals", payload)
        self.assertIn("join", payload)
        self.assertIn("revision", payload)

    def test_set_accepts_unquoted_sentences_and_refuses_an_unknown_flag(self):
        rc, out, _ = self.run_cli(
            ["set", "acct-z", "--vendor", "vendor-x", "--plan", "Small", "Plan",
             "--count", "2", "--good-for", "reading", "the", "timeline",
             "--not-for", "building", "anything", "--reach", "owner-only, ask"])
        self.assertEqual(rc, 0, out)
        got = next(a for a in accounts.read()["accounts"] if a["id"] == "acct-z")
        self.assertEqual(got["good_for"], "reading the timeline")
        self.assertEqual(got["count"], 2)
        rc, _out, err = self.run_cli(["set", "acct-z", "--token", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown accounts set option", err)

    def test_set_refuses_a_secret_and_the_row_is_not_written(self):
        rc, _out, err = self.run_cli(
            ["set", "acct-z", "--vendor", "v", "--plan", "p", "--count", "1",
             "--good-for", "x", "--not-for", "y", "--reach", "r",
             "--notes", "sk-abcdefabcdefabcdefabcdef"])
        self.assertEqual(rc, 1)
        self.assertIn("NO secrets", err)
        self.assertEqual(accounts.read()["revision"], accounts.MISSING_REVISION)
        self.assertEqual(accounts.read()["accounts"], [])

    def test_every_human_rendering_masks_the_join_key(self):
        """THE CLAIM, ASSERTED ON THE RENDERING. The field-value arm one class
        up proves `measured_as_masked` is computed; it says nothing about what
        the verb prints, and the verb printed the address whole — in `show`, in
        `--json`, and in the measured-but-not-declared list."""
        address = "person" + "@" + "example.test"
        accounts.save(row(measured_as=address))
        # BOTH RENDERERS, UNCONDITIONALLY AND ON THE SAME OBSERVABLE: each
        # "the address is not here" sits beside "the masked spelling IS here",
        # so neither is a statement about empty output.
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0, out)
        self.assertIn("p…@example.test", out)       # it IS rendered…
        self.assertNotIn(address, out)              # …and never whole
        rc, out, _ = self.run_cli(["show", "acct-a"])
        self.assertEqual(rc, 0, out)
        self.assertIn("p…@example.test", out)
        self.assertNotIn(address, out)

    def test_json_carries_the_join_key_whole_because_the_join_needs_it(self):
        """THE OTHER HALF, SAID OUT LOUD: `--json` is the machine surface the
        join reads, and a masked join key joins nothing."""
        address = "person" + "@" + "example.test"
        accounts.save(row(measured_as=address))
        rc, out, _ = self.run_cli(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)["accounts"][0]
        self.assertEqual(payload["measured_as"], address)
        # …beside the masked spelling, which is what every renderer reads
        self.assertEqual(payload["measured_as_masked"], "p…@example.test")

    def test_the_undeclared_list_masks_the_names_the_provider_minted(self):
        address = "someone" + "@" + "example.test"
        accounts.save(row())
        measured = [{"name": address, "provider": "vendor-x", "tier": "Team"}]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            accounts._print_rows(accounts.read(), measured)
        out = stdout.getvalue()
        self.assertIn("measured but NOT declared", out)   # the list IS drawn…
        self.assertIn("s…@example.test", out)             # …naming the account…
        self.assertNotIn(address, out)                    # …and never whole

    def test_show_prints_one_row_and_suggests_on_a_miss(self):
        accounts.save(row())
        rc, out, _ = self.run_cli(["show", "acct-a"])
        self.assertEqual(rc, 0)
        self.assertIn("good_for", out)
        rc, _out, err = self.run_cli(["show", "acct-b"])
        self.assertEqual(rc, 1)
        self.assertIn("no declared account", err)

    def test_rm_says_the_account_itself_is_untouched(self):
        accounts.save(row())
        rc, out, _ = self.run_cli(["rm", "acct-a"])
        self.assertEqual(rc, 0)
        self.assertIn("untouched", out)
        self.assertEqual(accounts.read()["revision"], accounts.MISSING_REVISION)
        self.assertEqual(accounts.read()["accounts"], [])

    def test_seed_is_a_dry_run_until_apply(self):
        rc, out, _ = self.run_cli(["seed"])
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        self.assertEqual(accounts.read()["accounts"], [])
        rc, out, _ = self.run_cli(["seed", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("x-premium", out)
        self.assertTrue(accounts.read()["accounts"])

    def test_line_and_teach_print_the_agent_surfaces(self):
        accounts.save(row())
        rc, out, _ = self.run_cli(["line"])
        self.assertEqual(rc, 0)
        self.assertIn("helm accounts", out)
        rc, out, _ = self.run_cli(["teach"])
        self.assertEqual(rc, 0)
        self.assertIn("helm store add reference", out)
        self.assertIn("helm store gloss", out)

    def test_a_junk_tail_refuses_before_the_seeder_writes_anything(self):
        """`helm accounts seed --bogus --apply` exited 0 and WROTE the owner's
        inventory: the tail was never guarded, so a token helm does not have
        rode straight into the one branch of this verb that writes. The tree's
        census (tests/test_dispatch_honest.ApplyReadersAreGuarded) is what
        found it and what keeps it found; this arm is the behaviour."""
        for argv in (["seed", "--bogus", "--apply"],
                     ["seed", "--apply", "--bogus"],
                     ["seed", "--bogus"]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_cli(argv)
                self.assertEqual(rc, 2, (out, err))
                self.assertIn("--bogus", err)          # it NAMES the token
                self.assertEqual(accounts.read()["accounts"], [])
        # THE CONTROL, same empty store, same verb, junk removed: it still
        # seeds — so the arm above is about the tail and not about a seeder
        # that has stopped writing.
        rc, out, _err = self.run_cli(["seed", "--apply"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(accounts.read()["accounts"])

    def test_a_junk_tail_on_a_read_verb_is_refused_rather_than_ignored(self):
        """`helm accounts --json --bogus` answered as though the flag existed,
        which teaches him a flag helm does not have."""
        accounts.save(row())
        # THE UNCONDITIONAL CONTROL, before the table: the plain verb answers
        # 0, so every rc 2 below is about the token and not about a verb that
        # has stopped working.
        rc, out, err = self.run_cli(["--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["accounts"][0]["id"], "acct-a")
        for argv in (["--json", "--bogus"], ["show", "acct-a", "--bogus"],
                     ["rm", "--bogus"], ["line", "--bogus"],
                     ["teach", "--bogus"]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_cli(argv)
                self.assertEqual(rc, 2, (out, err))
                self.assertIn("--bogus", err)
        # THE CONTROLS: every one of those verbs still answers without the junk
        for argv in (["--json"], ["show", "acct-a"], ["line"], ["teach"],
                     ["rm", "acct-a"]):
            with self.subTest(control=argv):
                rc, out, err = self.run_cli(argv)
                self.assertEqual(rc, 0, (out, err))

    def test_the_json_flag_may_stand_on_either_side_of_the_id(self):
        """The guard splits flags from operands rather than reading position,
        so guarding the tail did not make `show --json <id>` a syntax error."""
        accounts.save(row())
        rc, out, err = self.run_cli(["show", "acct-a"])       # the control
        self.assertEqual(rc, 0, err)
        self.assertIn("good_for", out)
        for argv in (["show", "acct-a", "--json"], ["show", "--json", "acct-a"]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_cli(argv)
                self.assertEqual(rc, 0, err)
                self.assertEqual(json.loads(out)["id"], "acct-a")

    def test_an_unknown_verb_refuses_with_a_suggestion_and_never_runs_it(self):
        rc, _out, err = self.run_cli(["shwo"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'shwo'", err)

    def test_the_bare_verb_reports_a_skipped_row_rather_than_hiding_it(self):
        self.plant({"version": 1, "accounts": {"acct-bad": {"vendor": "v"}}})
        rc, out, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED 'acct-bad'", out)
        self.assertIn("still on disk", out)

    def test_the_bare_verb_refuses_an_unreadable_inventory(self):
        self.plant("{this is not json")
        rc, _out, err = self.run_cli([])
        self.assertEqual(rc, 1)
        self.assertIn("could not be read", err)


class BillingAllowanceKeyWhereTest(AccountsBase):
    """The three fields the owner asked for: HOW it is billed, HOW MUCH it
    allows, WHERE the key is kept.

    Every refusal arm here carries the CLEAN half of its own pair, because a
    validator that refuses everything passes a refusal-only arm exactly as
    loudly as one that refuses nothing (this file's own law, stated at the
    top for the secret scanner and owed by every rung added after it)."""

    def test_the_three_fields_round_trip_through_the_one_door(self):
        saved, err, _code = accounts.save(row(
            billing="sub", allowance="20 requests per week",
            key_where="the opencode credential home"))
        self.assertIsNone(err)
        got = accounts.read()["accounts"][0]
        self.assertEqual(got["billing"], "sub")
        self.assertEqual(got["allowance"], "20 requests per week")
        self.assertEqual(got["key_where"], "the opencode credential home")
        for field in ("billing", "allowance", "key_where"):
            self.assertEqual(got[field], saved[field], field)

    def test_a_row_that_declares_no_billing_says_so_and_never_defaults(self):
        accounts.save(row())
        got = accounts.read()["accounts"][0]
        self.assertIsNone(got["billing"])
        self.assertEqual(got["billing_word"], "not declared")
        # the control on the same projection: a row that DID declare it reads
        # the word, so "not declared" is a statement about this row rather
        # than about a renderer that always says it
        accounts.save(row(id="acct-b", billing="free"))
        other = [a for a in accounts.read()["accounts"] if a["id"] == "acct-b"][0]
        self.assertEqual(other["billing_word"], "free")

    def test_billing_is_a_closed_set_and_the_refusal_names_every_legal_value(self):
        _ok, err, _code = accounts.save(row(billing="monthly"))
        self.assertIsNotNone(err)
        self.assertIn("sub", err)          # unconditional, before the sweep
        for legal in accountfields.BILLING:
            self.assertIn(legal, err,
                          "a refusal of a closed set must say what would work")
        self.assertEqual(accounts.read()["accounts"], [],
                         "a refused save writes nothing")
        # the control: the same door, the same field, a legal value
        _ok, err, _code = accounts.save(row(billing="payg"))
        self.assertIsNone(err)

    def test_every_billing_token_renders_as_a_word_and_none_renders_as_itself(self):
        for token in accountfields.BILLING:
            word = accountfields.billing_word(token)
            self.assertTrue(word, token)
            self.assertNotEqual(word, "not declared", token)
        self.assertEqual(accountfields.billing_word(None), "not declared")

    def test_an_allowance_helm_cannot_parse_is_still_stored_whole(self):
        # THE PARSE IS A READING, NEVER A GATE. This is the half that decides
        # whether the field is usable: he types what is true, and helm reads
        # what it can.
        _ok, err, _code = accounts.save(row(allowance="honestly no idea"))
        self.assertIsNone(err)
        got = accounts.read()["accounts"][0]
        self.assertEqual(got["allowance"], "honestly no idea")
        self.assertIsNone(got["allowance_parsed"])

    def test_the_shapes_the_owner_writes_parse_to_cap_unit_and_cadence(self):
        cases = {
            "20 requests per week": {"cap": 20, "unit": "requests",
                                     "cadence": "week"},
            "a 5h window": {"cap": 5, "unit": "hour", "cadence": "window"},
            "weekly": {"cap": None, "unit": None, "cadence": "week"},
            "resets daily": {"cap": None, "unit": None, "cadence": "day"},
        }
        self.assertEqual(accountfields.parse("20 requests per week"),
                         {"cap": 20, "unit": "requests", "cadence": "week"})
        for text, want in cases.items():
            self.assertEqual(accountfields.parse(text), want, text)
        # the control on the same function: prose that names no allowance at
        # all reads as None, so "it parsed" is a statement about the text
        self.assertIsNone(accountfields.parse("bundled with the hardware"))

    def test_the_parse_is_derived_and_no_parsed_copy_reaches_disk(self):
        accounts.save(row(allowance="20 requests per week"))
        stored = self.raw()["accounts"]["acct-a"]
        # the unconditional positive control on the SAME observable: this read
        # of the stored row does return fields, so the absence below is a
        # statement about the row rather than about an empty dict
        self.assertIn("allowance", stored)
        self.assertEqual(stored["allowance"], "20 requests per week")
        self.assertNotIn("allowance_parsed", stored,
                         "a parsed copy on disk is a second answer that goes "
                         "stale the moment the parser improves")
        self.assertIsNotNone(
            accounts.read()["accounts"][0]["allowance_parsed"],
            "…and the reader still gets the structure")

    def test_a_pasted_key_in_key_where_is_refused_and_never_echoed(self):
        secret = "sk-" + "abcdefabcdefabcdefabcdef"
        _ok, err, _code = accounts.save(row(key_where=secret))
        self.assertIsNotNone(err)
        self.assertIn("key_where", err)
        self.assertNotIn(secret, err, "a refusal may never carry the value")
        self.assertEqual(accounts.read()["accounts"], [])
        # the clean half of the pair, through the same door
        _ok, err, _code = accounts.save(row(key_where="the codex credhome"))
        self.assertIsNone(err)

    def test_a_short_opaque_token_is_refused_where_the_general_scanner_passes(self):
        # THE RUNG THIS FIELD NEEDED. The tree's general grammar wants 32
        # characters for a blob or a known prefix; this is neither, and it is
        # exactly the shape of a paste from a provider dashboard.
        opaque = "Ab3Cd4Ef5Gh6Ij7Kl8Mn9"
        self.assertIsNone(accounts.secret_reason("key_where", opaque),
                          "the control: the general scanner does NOT see this, "
                          "which is why the field owns a second rung")
        _ok, err, _code = accounts.save(row(key_where=opaque))
        self.assertIsNotNone(err)
        self.assertIn("key_where", err)
        self.assertNotIn(opaque, err)

    def test_the_rung_refuses_a_key_shaped_run_in_key_where_and_in_notes(self):  # noqa: VACUOUS_ASSERTION — the positive control is the accounts.save/assertIsNone pair ABOVE the loop, unconditional and on the same observable (err from the same door); len(pairs)==4 pins that the loop's subject is non-empty
        # A probe measured two shapes that stored VERBATIM through both doors: a
        # 16-character hex token and a 27-character dotted one. `notes` is the
        # other box on this screen wide enough to paste a key into, so the rung
        # runs on it too and one grammar decides for both.
        # THE UNCONDITIONAL CONTROL, before any loop: this door accepts a real
        # label, so a refusal below is about the token's shape and not about a
        # door that refuses everything — and it proves the loop's subject ran.
        _ok, err, _code = accounts.save(row(key_where="the codex credhome"))
        self.assertIsNone(err)
        pairs = [(f, t) for f in ("key_where", "notes")
                 for t in ("a1b2c3d4e5f6a7b8", "Ab3dEfGh9iJkLmN0pQr.StuVwXy")]
        self.assertEqual(len(pairs), 4, "the reading below covers four cells")
        for field, token in pairs:
            _ok, err, _code = accounts.save(row(**{field: token}))
            self.assertIsNotNone(err, "%s: %r stored" % (field, token))
            self.assertIn(field, err, "the refusal names the field")
            self.assertNotIn(token, err, "the refusal echoed the value")

    def test_a_location_carrying_no_digit_passes_however_long_its_run(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone on a real token ABOVE the loop is the unconditional positive on the same observable: it proves the rung fires, so the passes below read as the digit clause and not a dead rung
        # THE CONTROL, AND THE REASON THE RUNG ASKS FOR A DIGIT. This path is
        # one 26-character run over the rung's own alphabet; without the digit
        # clause it would read as a token and his real answer would be refused.
        # THE UNCONDITIONAL POSITIVE on the same observable: the rung DOES
        # refuse a digit-carrying run, so "these passed" is a reading about the
        # digit clause rather than about a rung that never fires.
        _ok, err, _code = accounts.save(row(key_where="a1b2c3d4e5f6a7b8"))  # gitleaks:allow — a synthetic token the rung must refuse
        self.assertIsNotNone(err, "the rung must still refuse a real token")
        labels = ("~/.helm/creds/openrouter.env",
                  "the 1Password vault 'helm', item deepseek",
                  "https://dashboard.deepseek.com/api_keys")
        self.assertEqual(len(labels), 3, "three labels are read below")
        for label in labels:
            _ok, err, _code = accounts.save(row(id="acct-a", key_where=label))
            self.assertIsNone(err, label)

    def test_a_path_anchored_run_is_a_location_even_carrying_a_digit(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone on the unanchored twin is the unconditional positive on the same observable, so the passes below read as the anchor clause rather than a dead rung
        # THE FALSE POSITIVE THIS CLAUSE EXISTS FOR. "where is the key kept"
        # is answered with a path, and a version or index in the filename is
        # ordinary; refusing his own correct answer is what would make the
        # screen read as broken.
        _ok, err, _code = accounts.save(row(key_where="a1b2c3d4e5f6a7b8"))  # gitleaks:allow — a synthetic token the rung must refuse
        self.assertIsNotNone(err, "the unanchored twin must still be refused")
        for label in ("~/.helm/creds/openrouter2.env", "./codex2.env",
                      "../creds/gpt4.env", ".env.local2"):
            _ok, err, _code = accounts.save(row(id="acct-a", key_where=label))
            self.assertIsNone(err, label)

    def test_english_prose_carrying_a_number_is_not_a_key(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone on a real token above the loop is the unconditional positive on the same observable
        # THE ARM THAT CAUGHT THE FIRST WIDENING. "25th-anniversary" is exactly
        # sixteen characters and carries a digit, so a rung keyed on length
        # plus any-digit refuses ordinary notes. Language puts its digits in
        # ONE leading segment; a key interleaves them, which is the clause
        # that separates the two.
        _ok, err, _code = accounts.save(row(notes="a1b2c3d4e5f6a7b8"))
        self.assertIsNotNone(err, "the token twin must still be refused")
        for text in ("secret 25th-anniversary planning",
                     "password-protected 3-ring binder",
                     "token-based 2-factor setup",
                     "renewed for the 2026-2027 term"):
            _ok, err, _code = accounts.save(row(id="acct-a", notes=text))
            self.assertIsNone(err, text)

    def test_an_unpunctuated_run_needs_only_one_digit_to_be_a_key(self):
        # THE OTHER CLAUSE, so the prose rule above cannot be read as the whole
        # grammar: with NO punctuation in the run, a single digit is enough.
        _ok, err, _code = accounts.save(row(key_where="abcdefgh1ijklmnop"))  # gitleaks:allow — a synthetic token the rung must refuse
        self.assertIsNotNone(err)
        self.assertNotIn("abcdefgh1ijklmnop", err)
        # the control on the same observable: the same letters with no digit
        # at all are a plain word and write
        _ok, err, _code = accounts.save(row(id="acct-a", key_where="abcdefghijklmnop"))
        self.assertIsNone(err)

    def test_an_anchored_path_does_not_shelter_a_token_beside_it(self):
        # THE ANCHOR EXEMPTS A RUN, NEVER THE VALUE. Runs are found
        # independently, so a real token pasted after a legitimate path is
        # still its own unanchored run and still refused.
        _ok, err, _code = accounts.save(
            row(key_where="stored at ~/.env and also a1b2c3d4e5f6a7b8"))
        self.assertIsNotNone(err, "an anchored run sheltered the whole value")
        self.assertNotIn("a1b2c3d4e5f6a7b8", err)

    def test_the_billing_refusal_names_the_legal_words_and_not_the_offered_one(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone(err) and the legal-value save are both unconditional on the same observable; the loop only reads the four words inside a string already proven non-empty
        # THE MODULE'S OWN NO-ECHO LAW, applied to its own refusal: the value
        # is what a mistyped paste puts here, so it may not be quoted back.
        offered = "monthly-ish-a1b2c3d4"
        _ok, err, _code = accounts.save(row(billing=offered))
        self.assertIsNotNone(err)
        self.assertNotIn(offered, err, "the refusal echoed the offered value")
        legal_words = ("sub", "payg", "prepaid", "free")
        self.assertEqual(len(legal_words), 4, "four words are read below")
        for legal in legal_words:
            self.assertIn(legal, err, "the refusal must name every legal value")
        # the control: a legal value writes, so this is about the word and not
        # about a door that refuses everything
        _ok, err, _code = accounts.save(row(id="acct-a", billing="sub"))
        self.assertIsNone(err)

    def test_a_real_location_label_with_punctuation_is_accepted(self):
        _ok, err, _code = accounts.save(row(key_where="the codex credhome"))
        self.assertIsNone(err)
        self.assertEqual(accounts.read()["accounts"][0]["key_where"],
                         "the codex credhome")
        for label in ("~/.config/codex/auth.json", "the provider dashboard",
                      "1password", "the env file beside the proxy"):
            _ok, err, _code = accounts.save(row(id="acct-a", key_where=label))
            self.assertIsNone(err, label)

    def test_key_present_is_derived_from_the_label_never_declared(self):
        accounts.save(row(key_where="the codex credhome"))
        self.assertTrue(accounts.read()["accounts"][0]["key_present"])
        accounts.save({"id": "acct-a", "key_where": ""})
        self.assertFalse(accounts.read()["accounts"][0]["key_present"])

    def test_helm_does_not_store_a_key_field_under_any_of_these_names(self):
        # the control: the door DOES accept a field helm knows, so "unknown
        # field refused" is about the name rather than about a closed door
        _ok, err, _code = accounts.save(row(notes="renewed by card"))
        self.assertIsNone(err)
        for name in ("key", "api_key", "token", "key_value"):
            _ok, err, _code = accounts.save(row(**{name: "whatever"}))
            self.assertIsNotNone(err, name)
            self.assertIn(name, err)

    def test_the_headline_carries_the_billing_word_and_the_allowance(self):
        accounts.save(row(billing="sub", allowance="a 5h window"))
        head = accounts.read()["accounts"][0]["headline"]
        self.assertIn("subscription", head)
        self.assertIn("a 5h window", head)
        # the control: a row declaring neither carries neither word
        accounts.save(row(id="acct-b"))
        other = [a for a in accounts.read()["accounts"]
                 if a["id"] == "acct-b"][0]
        self.assertNotIn("subscription", other["headline"])

    def test_the_table_says_billing_on_a_row_that_declares_none(self):
        accounts.save(row())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            accounts._print_rows(accounts.read())
        self.assertIn("billing: not declared", buf.getvalue())
        # the control on the same renderer
        accounts.save(row(billing="prepaid", allowance="500 credits"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            accounts._print_rows(accounts.read())
        self.assertIn("billing: prepaid", buf.getvalue())
        self.assertIn("allows 500 credits", buf.getvalue())

    def test_the_set_flags_reach_the_payload_the_door_reads(self):
        payload = accounts._set_args(
            ["acct-a", "--billing", "sub", "--allowance", "20 requests per week",
             "--key-where", "the codex credhome"])
        self.assertEqual(payload["billing"], "sub")
        self.assertEqual(payload["allowance"], "20 requests per week")
        self.assertEqual(payload["key_where"], "the codex credhome")

    def test_the_usage_names_the_three_flags_and_the_closed_set(self):
        self.assertIn("usage: helm accounts", accounts._USAGE)
        for flag in ("--billing", "--allowance", "--key-where"):
            self.assertIn(flag, accounts._USAGE)
        for legal in accountfields.BILLING:
            self.assertIn(legal, accounts._USAGE)

    def test_the_json_surface_carries_all_three(self):
        accounts.save(row(billing="sub", allowance="a 5h window",
                          key_where="the codex credhome"))
        got = json.loads(json.dumps(accounts.read()))["accounts"][0]
        self.assertIn("vendor", got)       # the projection is populated at all
        for field in ("billing", "billing_word", "allowance",
                      "allowance_parsed", "key_where", "key_present"):
            self.assertIn(field, got, field)


class AgentRowLinesTest(AccountsBase):
    """`helm accounts line` is what the JIT pointer points AT, so the glance it
    gives has to carry the two fields a seat most often guesses about."""

    def test_one_line_per_row_names_the_billing_word_and_the_allowance(self):
        accounts.save(row(id="opencode-go", vendor="opencode", plan="Go",
                          price_month="$10", billing="sub",
                          allowance="20 requests per week"))
        lines = accounts.agent_rows()
        self.assertEqual(len(lines), 1)
        for fragment in ("opencode-go:", "opencode Go", "$10/mo",
                         "subscription", "20 requests per week"):
            self.assertIn(fragment, lines[0], fragment)

    def test_a_row_that_declares_neither_says_neither(self):
        """The control: the words above are about the ROW, not about a line
        that always prints them."""
        accounts.save(row(id="acct-a"))
        line = accounts.agent_rows()[0]
        self.assertIn("acct-a:", line)
        self.assertNotIn("subscription", line)
        self.assertNotIn("pay-as-you-go", line)

    def test_a_seeded_row_says_it_is_still_a_guess(self):
        accounts.save(row(id="acct-a", seeded_from="helm's seat catalog"))
        self.assertIn("unconfirmed", accounts.agent_rows()[0])
        accounts.save({"id": "acct-a", "confirmed": True})
        self.assertNotIn("unconfirmed", accounts.agent_rows()[0])

    def test_no_line_is_longer_than_the_cap(self):
        accounts.save(row(id="acct-a", plan="P" * 80, allowance="A" * 60,
                          price_month="$1234"))
        lines = accounts.agent_rows()
        self.assertEqual(len(lines), 1)      # there IS a line to measure
        self.assertLessEqual(len(lines[0]), accounts.ROW_LINE_CAP)

    def test_the_verb_prints_the_pointer_and_then_the_rows(self):
        accounts.save(row(id="acct-a", billing="free"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            accounts.cmd_accounts(["line"])
        out = buf.getvalue().splitlines()
        self.assertIn("run `helm accounts`", out[0])
        self.assertIn("acct-a:", out[1])
        self.assertIn("free", out[1])

    def test_an_empty_inventory_prints_nothing_at_all(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            accounts.cmd_accounts(["line"])
        self.assertEqual(buf.getvalue(), "",
                         "a pointer to nothing is the per-turn paragraph this "
                         "must not become")
        # the control on the same call: with one row it DOES print
        accounts.save(row())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            accounts.cmd_accounts(["line"])
        self.assertTrue(buf.getvalue())

    def test_the_injected_pointer_itself_still_carries_no_contents(self):
        """agent_line is FIXED TEXT plus a count and is injected into turns; a
        row's words inside it would be a measurement that keeps steering after
        the owner edits it."""
        accounts.save(row(id="acct-a", billing="sub", plan="Small Plan"))
        line = accounts.agent_line()
        self.assertIn("1 declared account", line)   # it says something
        self.assertNotIn("Small Plan", line)
        self.assertNotIn("subscription", line)


class VerbRegistrationTest(unittest.TestCase):
    def test_the_verb_is_in_the_dispatch_table_and_the_root_synopsis(self):
        from helm import cli
        self.assertIn("accounts", cli.VERBS)
        self.assertIn("accounts", cli._VERB_HELP)
        synopsis = cli._VERB_HELP["accounts"]
        for token in ("show <id>", "set <id>", "rm <id>", "seed", "line", "teach"):
            self.assertIn(token, synopsis,
                          "a subverb the code accepts must appear in the root "
                          "synopsis (the surface-wiring rung)")

    def test_the_module_usage_names_every_subverb_the_dispatcher_accepts(self):
        usage = accounts._USAGE
        self.assertIn("usage: helm accounts", usage)
        for token in ("show", "set", "rm", "seed", "line", "teach"):
            self.assertIn("helm accounts %s" % token, usage)


if __name__ == "__main__":
    unittest.main()
