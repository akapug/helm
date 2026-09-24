#!/usr/bin/env python3
"""The quota tab's ACCOUNTS card — the endpoints and the renderer the owner sees.

Two halves, and they prove different things:

SERVER. `/api/accounts` and its POST twin are driven IN PROCESS (no helm-web is
started; the handlers are functions and the suite calls them). What is pinned is
the shape the card reads, that the writer's refusals reach the page VERBATIM
with the right status (400 refused, 409 conflict), and that an unreadable
inventory answers `unreadable` rather than an empty list — because an empty list
renders as "you have no accounts", which is the exact sentence this whole
surface exists to stop anybody saying.

CLIENT. The renderer is RUN, NOT MIRRORED: declRowHTML / declTotalsHTML /
declUndescribedHTML / renderDeclared / declChips and the add-edit-remove flow
are lifted VERBATIM out of the assembled web UI and executed under real node
against a minimal DOM. A python mirror of JS rots silently; this cannot.

TRANSPORT. The render half still supplies a stand-in `declPost`, because what
it is about is what the card DRAWS. A stand-in that cannot throw is also how
this card shipped a dead conflict branch: the real `post()` THROWS on any
non-2xx and the fake resolved with the body, so `r.code === "conflict"` was
never reachable in production and a mutant that swallowed every server refusal
kept the whole suite green. So RealTransportRuntimeTest below lifts the REAL
`post` and the REAL `declPost` out of the assembled page and drives them
against a fetch stub that answers like the server does — 409 with a code, 400
with a sentence, a network failure with nothing at all.

Node is optional on non-web hosts: absent, the client half SKIPS, like any
optional toolchain.
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-webaccounts-", var="HELM_HOME")

from helm import accounts, web, web_ui_loader  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402


@contextlib.contextmanager
def measuring(rows):
    """THE PROVIDER IS THE FIXTURE, and `get_creds` is not a seam.

    Both account doors read the ONE acquisition object every handler on this
    tab consumes, so patching `web.get_creds` is patching something no door
    consults: such an arm passes while proving nothing, which is a harness
    making two things equal that production lets drift. The rows go in where
    the provider's own do, and
    the merge, the subscription keying and the handle resolution are all the
    shipped ones.

    The rows are spelled as the PROVIDER spells them (`accounts()` output);
    the merged row the page sees is derived from them by `helm.web_quota`."""
    from unittest import mock
    from helm import providers

    class Measured:
        def accounts(self):
            return [dict(r) for r in rows]

        def cred_state(self):
            return []

        def windows(self):
            return []

    def drop():
        with web._qlock:
            web._qstate.pop("creds", None)

    provider = Measured()
    with mock.patch.object(web, "_provider", lambda: provider), \
            mock.patch.object(providers, "default_provider", lambda: provider):
        drop()
        try:
            yield provider
        finally:
            drop()


def _extract_const(src, name):
    """The SHIPPED declaration of one top-level const, verbatim.

    A python MIRROR of a JS table rots silently, which is this file's own
    stated law about the renderer — and the field list was a mirror: three
    fields were added to the form and every arm here went on driving the old
    eleven. Lifting the real declaration is the same cure the functions get.
    """
    head = "\nconst %s = " % name
    at = src.index(head) + 1
    depth, i = 0, at
    while i < len(src):
        if src[i] in "[{":
            depth += 1
        elif src[i] in "]}":
            depth -= 1
            if depth == 0:
                end = src.index(";", i) + 1
                return src[at:end]
        i += 1
    raise AssertionError("no const %s in the assembled page" % name)
from tests.test_accounts import AccountsBase, row  # noqa: E402


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

class AccountsEndpointTest(AccountsBase):
    """No server process: these ARE the handlers the server dispatches to."""

    def test_both_endpoints_are_registered_on_the_tables_the_server_reads(self):
        self.assertIs(web.API["/api/accounts"], web._api_accounts)
        self.assertIs(web.POST_API["/api/accounts"], web._api_accounts_post)
        # the control on the SAME table: it is populated, so "not in it" is a
        # statement about a real registry rather than about an empty dict
        self.assertIn("/api/creds", web.QUERY_API)
        self.assertNotIn("/api/accounts", web.QUERY_API)

    def test_the_get_carries_the_rows_totals_revision_join_and_cap(self):
        accounts.save(row(measured_as="quota-row-1"))
        got = web._api_accounts()
        self.assertEqual([a["id"] for a in got["accounts"]], ["acct-a"])
        for key in ("totals", "revision", "join", "cap", "bad", "unreadable"):
            self.assertIn(key, got)
        self.assertEqual(got["cap"], accounts.MAX_ACCOUNTS)
        self.assertEqual(got["owner_only"], accounts.OWNER_ONLY)
        json.dumps(got)          # the handler's output must be JSON-able

    def test_an_unreadable_inventory_answers_unknown_not_an_empty_list(self):
        self.plant("{this is not json")
        got = web._api_accounts()
        self.assertEqual(got["accounts"], [])
        self.assertTrue(got["unreadable"])

    def test_a_save_writes_and_answers_the_new_revision(self):
        out, status = web._api_accounts_post({"action": "save", "account": row()})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["account"]["id"], "acct-a")
        self.assertEqual(out["revision"], accounts.read()["revision"])

    def test_a_secret_is_refused_with_the_writers_own_sentence_at_400(self):
        out, status = web._api_accounts_post(
            {"action": "save", "account": row(notes="sk-abcdefabcdefabcdefabcdef")})
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "refused")
        self.assertIn("NO secrets", out["error"])
        self.assertNotIn("sk-abcdefabcdef", out["error"])
        self.assertEqual(accounts.read()["revision"], accounts.MISSING_REVISION)
        self.assertEqual(accounts.read()["accounts"], [])

    def test_a_stale_revision_is_a_409_and_writes_nothing(self):
        web._api_accounts_post({"action": "save", "account": row()})
        stale = accounts.read()["revision"]
        web._api_accounts_post({"action": "save", "account": row(id="acct-b")})
        before = self.raw()
        self.assertEqual(sorted(before["accounts"]), ["acct-a", "acct-b"])
        out, status = web._api_accounts_post(
            {"action": "save", "account": row(plan="Clobbered"), "revision": stale})
        self.assertEqual(status, 409)
        self.assertEqual(out["code"], "conflict")
        self.assertEqual(self.raw(), before)

    def test_a_remove_answers_the_new_revision_and_a_stale_one_conflicts(self):
        web._api_accounts_post({"action": "save", "account": row()})
        web._api_accounts_post({"action": "save", "account": row(id="acct-b")})
        current = accounts.read()["revision"]
        out, status = web._api_accounts_post(
            {"action": "remove", "id": "acct-a", "revision": current})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        out, status = web._api_accounts_post(
            {"action": "remove", "id": "acct-b", "revision": current})
        self.assertEqual(status, 409)

    def test_a_landed_remove_is_never_answered_as_a_failure(self):
        """The read after the remove only fetches the next revision. When it
        raises, the removal is already on disk, so the honest answer is the
        `stale` 409 the page reads as landed, never a 500."""
        web._api_accounts_post({"action": "save", "account": row()})
        current = accounts.read()["revision"]
        real = accounts.read
        calls = []

        def read_once_then_raise(*a, **kw):
            calls.append(1)
            raise OSError("the list could not be read back")

        # CONTROL: the handler's remove really does read after it writes, and
        # with a working read it answers 200 (the arm above proves that).
        removed = []
        real_remove = accounts.remove

        def remove_then_break(*a, **kw):
            got = real_remove(*a, **kw)
            removed.append(got)
            accounts.read = read_once_then_raise
            return got

        accounts.remove = remove_then_break
        try:
            out, status = web._api_accounts_post(
                {"action": "remove", "id": "acct-a", "revision": current})
        finally:
            accounts.remove = real_remove
            accounts.read = real
        self.assertTrue(removed and removed[0][0], "the remove never ran")
        self.assertTrue(calls, "the handler did not read back at all")
        self.assertEqual(status, 409, out)
        self.assertEqual(out.get("code"), "stale", out)
        self.assertIn("was removed", out["error"])
        self.assertNotIn("the list could not be read back", out["error"])
        self.assertEqual([a["id"] for a in accounts.read()["accounts"]], [],
                         "the removal is not on disk")

    def test_a_hand_broken_row_is_removable_through_the_real_web_handler(self):  # noqa: VACUOUS_ASSERTION — the empty `bad` at the end is the POINT (both broken rows came off); its unconditional control is the assertEqual(len(accounts.read()["bad"]), 2) on the same observable, before the removes
        """The card's remove button on a skipped row posted the ESCAPED id and
        the door refuses exactly that, so the button could never work. It posts
        the handle now."""
        self.plant({"version": 1, "accounts": dict(BROKEN_ROWS, **{"acct-a": row()})})
        got = web._api_accounts()
        bad = {b["id"]: b["handle"] for b in got["bad"]}
        self.assertEqual(len(bad), len(BROKEN_ROWS), got["bad"])
        # the same observable the tail asserts is empty, NON-empty first
        self.assertEqual(len(accounts.read()["bad"]), len(BROKEN_ROWS))
        # what the card PRINTS is refused — the reason the handle exists
        escaped = next(e for e in bad if e != "acct-bad")
        out, status = web._api_accounts_post(
            {"action": "remove", "id": escaped, "revision": got["revision"]})
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "refused")
        # what the button now CARRIES removes it
        for handle in bad.values():
            out, status = web._api_accounts_post(
                {"action": "remove", "id": handle,
                 "revision": accounts.read()["revision"]})
            self.assertEqual(status, 200, out)
            self.assertTrue(out["ok"])
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a"])
        self.assertEqual(accounts.read()["bad"], [])

    def test_a_page_edit_that_changes_the_id_refuses_and_leaves_one_row(self):
        """The form says the id stays the same; before this it did not have to.
        Counted in the FILE: the duplicate read as a normal two-account
        inventory through the API, which is why it went unseen."""
        web._api_accounts_post({"action": "save", "account": row()})
        out, status = web._api_accounts_post(
            {"action": "save", "account": row(id="acct-b"),
             "original_id": "acct-a"})
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "refused")
        self.assertIn("the id is the row's key", out["error"])
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a"])
        # THE CONTROL on the same door: the ADD path carries no original_id and
        # still mints the second row
        out, status = web._api_accounts_post(
            {"action": "save", "account": row(id="acct-b")})
        self.assertEqual(status, 200, out)
        self.assertEqual(sorted(self.raw()["accounts"]), ["acct-a", "acct-b"])

    def test_an_unknown_action_refuses_and_names_the_ones_that_exist(self):
        out, status = web._api_accounts_post({"action": "drop-everything"})
        self.assertEqual(status, 400)
        self.assertIn("save | remove", out["error"])

    @staticmethod
    def _exploding(exc):
        """THE PROVIDER EXPLODES, not a helm function standing in for it.

        `web.get_creds` answers out of the one acquisition object every
        handler consumes, so patching it to raise proves nothing about what
        the handlers do — a harness making two things equal that production
        lets drift. The raise goes where production's raises come from."""
        from unittest import mock
        from helm import providers

        class Boom:
            def accounts(self):
                raise exc

            def cred_state(self):
                return []

            def windows(self):
                return []

        patches = [mock.patch.object(t, a, lambda: Boom())
                   for t, a in ((web, "_provider"),
                                (providers, "default_provider"))]
        for patch in patches:
            patch.start()
        with web._qlock:
            web._qstate.pop("creds", None)
        return patches

    def _unexplode(self, patches):
        for patch in patches:
            patch.stop()
        with web._qlock:
            web._qstate.pop("creds", None)

    def test_the_get_never_raises_even_with_the_quota_provider_exploding(self):
        accounts.save(row())
        patches = self._exploding(RuntimeError("boom"))
        self.addCleanup(self._unexplode, patches)
        got = web._api_accounts()
        self.assertEqual([a["id"] for a in got["accounts"]], ["acct-a"])
        self.assertEqual(got["join"]["measured_only"], [])

    def test_a_provider_that_raised_is_reported_never_read_as_no_measurements(self):
        """SWALLOWING IT INTO [] is a lie with the same shape as the truth: the
        card would then tell him every declared row has "no quota row called
        that right now". The failure is named instead, and the class name is
        all of it that renders — an exception's text carries paths."""
        accounts.save(row(measured_as="quota-row-1"))
        patches = self._exploding(RuntimeError("/home/someone/secret-path"))
        self.addCleanup(self._unexplode, patches)
        got = web._api_accounts()
        self.assertIn("could not read the measured quota rows",
                      got["measured_unavailable"])
        self.assertIn("RuntimeError", got["measured_unavailable"])
        self.assertNotIn("secret-path", got["measured_unavailable"])
        # the control: a provider that ANSWERS, with nothing in it, reports no
        # failure at all — the two must never come back as the same value
        self._unexplode(patches)
        from unittest import mock
        from helm import providers

        class Quiet:
            def accounts(self):
                return []

            def cred_state(self):
                return []

            def windows(self):
                return []

        with mock.patch.object(web, "_provider", lambda: Quiet()), \
                mock.patch.object(providers, "default_provider", lambda: Quiet()):
            self.assertIsNone(web._api_accounts()["measured_unavailable"])

    def test_the_page_is_never_given_an_undescribed_accounts_whole_name(self):
        """THE MASKING SEAM IS THE SERVER'S. The card renders what this handler
        sends, so a renderer that has to remember to mask is one that will
        forget — it did, and printed provider addresses whole in the describe
        buttons. What crosses the wire is the mask, the opaque handle and the
        id to suggest; the whole name stays behind, in the join the CLI's
        `--json` reads."""
        address = "someone" + "@" + "example.test"
        measured = [{"name": address, "provider": "codex", "home": "/h"},
                    {"name": "quota-row-1", "provider": "codex", "home": "/h"}]
        with measuring(measured):
            got = web._api_accounts()
        # ensure_ascii=False so the mask's ellipsis is itself rather than an
        # escape — the assertion below is about what the page receives
        wire = json.dumps(got, ensure_ascii=False)
        self.assertNotIn(address, wire)             # …never whole…
        self.assertIn("s…@example.test", wire)      # …and it IS named
        undescribed = {m["name_masked"]: m for m in got["join"]["measured_only"]}
        self.assertEqual(sorted(undescribed), ["quota-row-1", "s…@example.test"])
        for m in got["join"]["measured_only"]:
            self.assertNotIn("name", m, "the whole name may not reach the page")
            self.assertTrue(m["key"] and m["suggest_id"])
        # the CONTROL on the same call: the join the CLI reads still carries the
        # whole name, because a masked join key joins nothing
        self.assertEqual(
            [m["name"] for m in accounts.join([], measured)["measured_only"]],
            [address, "quota-row-1"])

    def test_the_describe_handle_round_trips_through_the_real_save(self):  # noqa: VACUOUS_ASSERTION — the empty `measured_only` at the end is the POINT (the account is described now); its unconditional control is the assertEqual(len(...), 1) on the same observable, same handler, same measured rows, before the save
        """The button posts the handle; the row on disk carries the join key.
        A describe that wrote the mask — or nothing — would leave the account
        he just described reading "declared, not measured"."""
        address = "someone" + "@" + "example.test"
        measured = [{"name": address, "provider": "codex", "home": "/h"}]
        with measuring(measured):
            got = web._api_accounts()
            # the positive control for the emptiness assertions below, on the
            # same observable: one undescribed row before the describe
            self.assertEqual(len(got["join"]["measured_only"]), 1)
            handle = got["join"]["measured_only"][0]
            payload = dict(row(id=handle["suggest_id"]), measured_key=handle["key"])
            out, status = web._api_accounts_post(
                {"action": "save", "account": payload})
        self.assertEqual(status, 200, out)
        stored = self.raw()["accounts"]["someone"]
        self.assertEqual(stored["measured_as"], address)
        self.assertNotIn("measured_key", stored,
                         "the handle is the page's shape, never the row's")
        # …and the row now MATCHES the account it was describing
        with measuring(measured):
            after = web._api_accounts()
        self.assertEqual(after["join"]["measured_only"], [])
        self.assertEqual(after["accounts"][0]["measured_as_masked"],
                         "s…@example.test")

    def test_a_handle_nothing_measures_any_more_is_refused_and_writes_nothing(self):
        """Silently dropping it would write a row with no join key at all, and
        the card would then say "declared, not measured" about the very
        account he clicked."""
        address = "someone" + "@" + "example.test"
        measured = [{"name": address, "provider": "codex", "home": "/h"}]
        with measuring(measured):
            out, status = web._api_accounts_post(
                {"action": "save",
                 "account": dict(row(), measured_key="m-0000000000000000")})
        self.assertEqual(status, 400)
        self.assertIn("no longer measures", out["error"])
        self.assertEqual(accounts.read()["accounts"], [])
        # THE CONTROL ON THE SAME CALL: the live handle, everything else
        # identical, writes the row — so "nothing was written" above is about a
        # refusal rather than about a door that writes nothing at all
        with measuring(measured):
            out, status = web._api_accounts_post(
                {"action": "save",
                 "account": dict(row(), measured_key=accounts.measured_key(address))})
        self.assertEqual(status, 200, out)
        self.assertEqual([a["measured_as"] for a in accounts.read()["accounts"]],
                         [address])

    def test_a_page_edit_through_the_real_handler_keeps_what_the_form_omits(self):
        """THE POST DOOR, not the module door: this is the exact payload the
        card sends, against the exact handler the server dispatches to."""
        # the stored row is put there by the SEEDER (accounts.save is the
        # writer it uses); the page cannot write `seeded_from` at all, which is
        # the arm two functions down.
        full = dict(row(), renews_on="2027-01-31", confirmed=True,
                    seeded_from="helm's measured quota rows")
        accounts.save(full)
        before = accounts.read()["accounts"][0]
        form = {k: before[k] for k in ("id", "vendor", "plan", "count",
                                       "price_month", "good_for", "not_for",
                                       "reach", "notes", "renews_on")}
        form["plan"] = "Bigger Plan"
        out, status = web._api_accounts_post(
            {"action": "save", "account": form,
             "revision": accounts.read()["revision"]})
        self.assertEqual(status, 200, out)
        after = accounts.read()["accounts"][0]
        self.assertEqual(after["plan"], "Bigger Plan")        # the edit landed…
        self.assertEqual(after["renews_on"], "2027-01-31")    # …and these stayed
        self.assertTrue(after["confirmed"])
        self.assertEqual(after["seeded_from"], "helm's measured quota rows")


    def test_the_page_may_not_write_where_a_row_came_from(self):
        """PROVENANCE IS HELM'S. `seeded_from` is how a seeded row says it is a
        guess nobody has confirmed; a payload that can set it can also erase
        that, and the refusal is the same shape as every other field helm
        does not take from the form."""
        accounts.save(dict(row(), seeded_from="helm's measured quota rows"))
        out, status = web._api_accounts_post(
            {"action": "save",
             "account": dict(row(), seeded_from="I typed this myself"),
             "revision": accounts.read()["revision"]})
        self.assertEqual(status, 400)
        self.assertEqual(out["code"], "refused")
        self.assertIn("seeded_from", out["error"])
        self.assertEqual(accounts.read()["accounts"][0]["seeded_from"],
                         "helm's measured quota rows")
        # THE CONTROL: the same payload WITHOUT the field saves, and the
        # provenance the page never carried is still there afterwards.
        out, status = web._api_accounts_post(
            {"action": "save", "account": dict(row(), plan="Bigger"),
             "revision": accounts.read()["revision"]})
        self.assertEqual(status, 200, out)
        after = accounts.read()["accounts"][0]
        self.assertEqual(after["plan"], "Bigger")
        self.assertEqual(after["seeded_from"], "helm's measured quota rows")

    def test_a_save_against_a_row_that_vanished_says_the_row_is_gone(self):
        """He hits confirm on a row somebody removed under him. The answer was
        "vendor is required — say it in a few words.", which is about a box he
        never touched. 409 like the conflict it is, so the card reloads."""
        web._api_accounts_post({"action": "save", "account": row()})
        accounts.remove("acct-a")
        # the revision AFTER the removal: a stale one answers the conflict
        # first, which is its own arm two functions up.
        current = accounts.read()["revision"]
        out, status = web._api_accounts_post(
            {"action": "save", "account": {"id": "acct-a", "confirmed": True},
             "revision": current})
        self.assertEqual(status, 409, out)
        self.assertEqual(out["code"], "gone")
        self.assertIn("acct-a", out["error"])
        self.assertNotIn("vendor is required", out["error"])
        # THE CONTROL on the same door: the row put back, the same partial
        # payload is an ordinary merge that answers 200.
        web._api_accounts_post({"action": "save", "account": row()})
        out, status = web._api_accounts_post(
            {"action": "save", "account": {"id": "acct-a", "confirmed": True},
             "revision": accounts.read()["revision"]})
        self.assertEqual(status, 200, out)
        self.assertTrue(accounts.read()["accounts"][0]["confirmed"])


class TransportBindingTest(unittest.TestCase):
    """The route, node-free — so a host with no node still holds it.

    The BEHAVIOUR of the transport is proven by running it
    (RealTransportRuntimeTest); this pins only the two routes, which a
    node-less host would otherwise have no arm for at all."""

    def test_the_card_posts_to_the_registered_mutation_route(self):
        src = web_ui_loader.read_text()
        self.assertIn('post("/api/accounts", body)', src)
        self.assertIn('j("/api/accounts")', src)

    def test_the_transport_is_a_named_function_so_the_suite_can_run_it(self):
        """A const arrow is invisible to the function extractor, and the one
        line this card could not execute is the line that broke."""
        self.assertIn("async function declPost(body)", web_ui_loader.read_text())


# ---------------------------------------------------------------------------
# client — the real renderer under real node
# ---------------------------------------------------------------------------

EXTRACT = ["declareChip", "declChips", "declDupHTML", "declRowHTML", "declTotalsHTML",
           "declUndescribedHTML", "renderDeclared", "declAct", "declSlug",
           "declFormOpen", "declFormClose", "declPayload", "declSave",
           "declRemove", "declConfirm", "declOpenFor", "declOpenForKey", "declFreeId",
           "loadDeclared",
           # fill mode — the owner's bulk editor, 21-quotafill.js.part
           "declFillSort", "declFillHeader", "declFillCell", "declFillRowHTML",
           "renderDeclFill", "declFillBind", "declFillKey", "declFillNext",
           "declFillSave", "declFillPost", "declFillMsg", "declFillRowFor",
           # the composition fill mode edits — the SAME rows the quota table
           # shows, so an account helm measures is editable here whether a
           # declared record exists for it yet or not. The composition itself
           # is LIFTED rather than stubbed: see the note in SUPPORT.
           "fillRows", "fillComposed", "fillNeedsDescribe", "declFillLost",
           "quotaRows", "quotaAliasOrder", "quotaDeclOrder",
           "declFillRefreshHeader", "declFillToggle"]

# THE ADDRESS THE PROVIDER MINTED, house-convention and invented. The card's
# undescribed fixture is not typed out below: it is what `accounts.join` really
# answers about this name, so an arm asserting the address is absent from the
# HTML is asserting it about a row that CARRIES the address (a renderer reading
# the wrong field of it renders the address, which is the defect).
MEASURED_ADDRESS = "someone" + "@" + "example.test"
# A HAND-BROKEN FILE, the kind this module's docstring says is legal to make:
# one row whose KEY cannot be an id (so the escaped rendering of it is all a
# surface may print, and `remove` refuses that rendering) and one whose value
# is not a row at all (its key is fine, so it removes the ordinary way).
BROKEN_KEY = "acct-\u0007x"
BROKEN_ROWS = ((BROKEN_KEY, {"vendor": "vendor-y"}),
               ("acct-bad", "not even a dict"))
UNDESCRIBED_MEASURED = [{"name": MEASURED_ADDRESS, "provider": "vendor-y"},
                        {"name": "quota-row-2", "provider": "vendor-x"}]

SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = (n, l = 26) => String(n).length > l ? String(n).slice(0, l - 1) + "…" : String(n);
const TOASTS = [], POSTED = [];
const toast = (t) => TOASTS.push(t);
const renderAccts = () => { RENDER_ACCTS_CALLS++; };
let RENDER_ACCTS_CALLS = 0;
/* THE TABLE'S OWN COMPOSITION, stubbed HERE and exercised for real in
   tests/test_web_quota.py where CREDS and DECL both exist. Fill mode calls it
   rather than re-deriving the row set, which is the whole point — the two
   screens cannot disagree about which accounts exist — so the seam under test
   in this file is the MAPPING, and the rows going into it are a fixture. */
/* THE SHIPPED COMPOSITION, LIFTED — never a local re-implementation of it.
   A stub stood here and resolved a declared record by a `subscription` field
   that the real save response does not carry, so the create transition it
   rehearsed was one the server never produces and a real defect rode through a
   green suite. The rows now come from `quotaRows` itself, out of the assembled
   page, driven by CREDS and DECL exactly as the browser drives it. */
let CREDS = [];
let POST_REPLY = {ok: true};
let FETCH_REPLY = null;
let CONFIRMED = true;
const confirm = () => CONFIRMED;
const declPost = body => { POSTED.push(body); return Promise.resolve(POST_REPLY); };
const j = _url => Promise.resolve(FETCH_REPLY);

/* a minimal DOM: one stub element per selector the card touches */
const EL = {};
function el(sel) {
  if (!EL[sel]) EL[sel] = {
    sel, innerHTML: "", textContent: "", value: "", checked: false,
    hidden: true, open: false,
    querySelectorAll: () => [],
    addEventListener: () => {},
  };
  return EL[sel];
}
const $ = sel => el(sel);
"""

DRIVER = r"""
/* WHAT THE SERVER REALLY ANSWERS about two undescribed accounts, spliced in by
   setUpClass from `accounts.join` rather than typed here. */
const UNDESC = __UNDESCRIBED__;
/* …and what it answers about a really hand-broken file: one row whose KEY is
   not an id (the kind `remove` cannot be handed the rendering of) and one
   whose VALUE is not a row. */
const BADROWS = __BADROWS__;

const A = (over) => Object.assign({
  id: "acct-a", vendor: "vendor-x", plan: "Small Plan", price_month: "$7",
  count: 1, good_for: "reading the live timeline",
  not_for: "building; the quota is too small", reach: "owner-only, ask",
  measured_as: null, measured_as_masked: null, notes: null, renews_on: null,
  seeded_from: null, confirmed: false,
  needs_confirm: false, needs_describe: false, updated_at: "2026-01-02T03:04:05Z",
  headline: "vendor-x · Small Plan · x1 · $7/mo · reading the live timeline",
}, over || {});

function render(state) {
  EL["#declrows"] = null; delete EL["#declrows"];
  DECL = Object.assign({accounts: [], bad: [], unreadable: null, revision: "r1",
                        totals: {accounts: 0, units: 0, monthly_spend: 0, unpriced: 0},
                        join: {matched: {}, declared_only: [], measured_only: []},
                        cap: 64}, state || {});
  renderDeclared();
  return el("#declrows").innerHTML;
}

const out = {};
out.declared_only = render({
  accounts: [A()],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
/* THE THREE NEW DECLARED FIELDS, in the reading fold. A field the owner can
   type on the fill screen and cannot read back on the card is a field he has
   no way to check, which is the gap this scenario pins. */
out.three_fields = render({
  accounts: [A({billing_word: "subscription", allowance: "20 requests per week",
                key_where: "the opencode credential home"})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.three_fields_absent = render({
  accounts: [A({billing_word: null, allowance: null, key_where: null})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
/* WHICH ROWS ARE THE SAME BILL. He filled this card by hand and reported "some
   of them were dupes or unknown to me"; every fact needed to tell him which was
   already on the wire and only the card was silent. */
out.dup_proven = render({
  accounts: [A({id: "home-a", duplicate_of: ["home-b"]})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.dup_pointer = render({
  accounts: [A({id: "the-default", points_at_default: true})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.dup_question = render({
  accounts: [A({id: "unbound", vendor: "vendor-x", vendor_siblings: ["proven"]})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
/* THE CONTROL on the same renderer: a row with nothing to say says nothing.
   `out.declared_only` above is that row, so the arm can compare against it. */
out.dup_silent = render({
  accounts: [A({id: "clean"})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.measured_match = render({
  accounts: [A({measured_as: "quota-row-1", measured_as_masked: "quota-row-1"})],
  join: {matched: {"quota-row-1": A({measured_as: "quota-row-1"})},
         declared_only: [], measured_only: []},
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.masked_address = render({
  accounts: [A({measured_as: "person@example.test",
                measured_as_masked: "p…@example.test"})],
  join: {matched: {"person@example.test": A({measured_as: "person@example.test"})},
         declared_only: [], measured_only: []},
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.stale_measurement = render({
  accounts: [A({measured_as: "quota-row-gone"})],
  join: {matched: {}, declared_only: [A({measured_as: "quota-row-gone"})],
         measured_only: ["quota-row-1"]},
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.seeded = render({
  accounts: [A({seeded_from: "helm's measured quota rows", needs_confirm: true})],
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});
out.unpriced = render({
  accounts: [A(), A({id: "acct-b"})],
  totals: {accounts: 2, units: 5, monthly_spend: 7, unpriced: 1}});
out.unreadable = render({unreadable: "the accounts file could not be read as an inventory"});
out.empty = render({});
out.bad_entry = render({bad: BADROWS});
out.undescribed = render({
  join: {matched: {}, declared_only: [], measured_only: UNDESC}});
/* the measured side is unreadable: a row may not claim its measurement is ABSENT */
out.measured_down = render({
  accounts: [A({measured_as: "quota-row-1", measured_as_masked: "quota-row-1"})],
  measured_unavailable: "helm could not read the measured quota rows just now (ProviderError)",
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});

/* …and the SAME failure against a row that names no measurement at all: the
   provider being down says nothing about a row nothing ever measures. */
out.measured_down_unmeasured = render({
  accounts: [A({measured_as: null, measured_as_masked: null})],
  measured_unavailable: "helm could not read the measured quota rows just now (ProviderError)",
  totals: {accounts: 1, units: 1, monthly_spend: 7, unpriced: 0}});

/* the chips a declared row lends a measured one */
DECL = {accounts: [], bad: [], unreadable: null, revision: "r1", totals: null,
        join: {matched: {"quota-row-1": A({measured_as: "quota-row-1"})},
               declared_only: [], measured_only: []}, cap: 64};
out.chips_matched = declChips("quota-row-1");
out.chips_unmatched = declChips("quota-row-2");

/* A SEEDED ROW NOBODY DESCRIBED: the seed pre-declared every measured account
   on the owner's real board, so this is the state fourteen of his fifteen rows
   were in — the one where the describe affordance had to stay reachable. */
DECL = {accounts: [], bad: [], unreadable: null, revision: "r1", totals: null,
        join: {matched: {"quota-row-1": A({measured_as: "quota-row-1",
                                           good_for: "not described yet — say what this one is for",
                                           needs_describe: true})},
               declared_only: [], measured_only: []}, cap: 64};
out.chips_undescribed = declChips("quota-row-1");

out.slug = [declSlug("person@example.test"), declSlug("Some Name 2"), declSlug("")];
/* the free-id prefill: a suggested id already declared moves to -2, then -3; a free one stays */
DECL.accounts = [{id: "dana"}, {id: "dana-2"}];
out.free_id = [declFreeId("dana"), declFreeId("mira")];
DECL.accounts = [];

/* --- the add flow --- */
render({accounts: [], revision: "rev-add"});
declFormOpen(null);
out.add_form_visible = !el("#declform").hidden;
out.add_count_default = el("#dCount").value;
out.add_id_readonly = !!el("#dId").readOnly;
el("#dId").value = "acct-new";
el("#dVendor").value = "vendor-x";
el("#dPlan").value = "Small Plan";
el("#dCount").value = "2";
el("#dGood").value = "reading";
el("#dNot").value = "building";
el("#dReach").value = "owner-only, ask";
out.add_payload = declPayload();
el("#dRenews").value = "2027-01-31";
el("#dConfirm").checked = true;
out.add_payload_full = declPayload();
POST_REPLY = {ok: true};
FETCH_REPLY = {accounts: [], bad: [], revision: "rev-add-2", totals: null,
               join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};

/* --- the edit flow: an existing row prefills every field --- */
render({accounts: [A({notes: "renewed by card"})], revision: "rev-edit"});
declFormOpen(A({notes: "renewed by card", renews_on: "2027-01-31", confirmed: true}));
out.edit_prefill = {id: el("#dId").value, vendor: el("#dVendor").value,
                    plan: el("#dPlan").value, count: el("#dCount").value,
                    price: el("#dPrice").value, good: el("#dGood").value,
                    not_for: el("#dNot").value, reach: el("#dReach").value,
                    notes: el("#dNotes").value, renews: el("#dRenews").value,
                    confirmed: el("#dConfirm").checked};
out.edit_msg = el("#dMsg").innerHTML;
/* …and clearing a box sends the EMPTY, which is what makes the door clear it */
el("#dNotes").value = "";
out.edit_cleared_payload = declPayload();
/* an unconfirmed row leaves the box unticked rather than inheriting the last */
declFormOpen(A({confirmed: false}));
out.edit_unconfirmed_box = el("#dConfirm").checked;

/* --- the describe flow: prefilled from what is MEASURED --- */
render({accounts: [], revision: "rev-desc"});
declAct({dataset: {dact: "describe", dname: "person@example.test"}});
out.describe_prefill = {id: el("#dId").value, measured: el("#dMeasured").value,
                        readonly: !!el("#dMeasured").readOnly};

/* …and the SAME affordance on the card's own undescribed list, where the card
   holds a HANDLE and a MASK and was never given the provider's spelling */
render({accounts: [], revision: "rev-desc-key",
        join: {matched: {}, declared_only: [], measured_only: UNDESC}});
declAct({dataset: {dact: "describe", dkey: UNDESC[0].key,
                   dname: UNDESC[0].name_masked, dsuggest: UNDESC[0].suggest_id}});
out.describe_by_key = {id: el("#dId").value, measured: el("#dMeasured").value,
                       readonly: !!el("#dMeasured").readOnly,
                       payload: declPayload()};
/* the form CLOSING forgets the handle, so the next add is not bound to it */
declFormClose();
declFormOpen(null);
out.describe_key_cleared = declPayload();

/* the SAME affordance on a measured account that IS declared (a seeded row
   nobody has described carries it): it opens the row that exists */
render({accounts: [A({id: "acct-seeded", measured_as: "quota-row-1",
                      needs_describe: true})],
        join: {matched: {"quota-row-1": A({id: "acct-seeded", measured_as: "quota-row-1",
                                           needs_describe: true})},
               declared_only: [], measured_only: []},
        revision: "rev-desc2"});
declAct({dataset: {dact: "describe", dname: "quota-row-1"}});
out.describe_existing = {id: el("#dId").value, editing: DECL_EDIT,
                         msg: el("#dMsg").innerHTML};

(async () => {
  /* --- save carries the revision the form was opened against --- */
  render({accounts: [], revision: "rev-save"});
  declFormOpen(null);
  el("#dId").value = "acct-new";
  el("#dVendor").value = "vendor-x";
  POSTED.length = 0;
  POST_REPLY = {ok: true};
  await declSave();
  out.save_posted = POSTED[POSTED.length - 1];
  out.save_form_closed = el("#declform").hidden;

  /* --- an EDIT may not retype the id: the box is read-only and the payload
         carries the id the form opened, whatever the box now holds --- */
  render({accounts: [A()], revision: "rev-edit-id"});
  declFormOpen(A());
  out.edit_id_readonly = !!el("#dId").readOnly;
  el("#dId").value = "acct-tampered";
  out.edit_tampered_payload = declPayload();
  POSTED.length = 0;
  POST_REPLY = {ok: true};
  await declSave();
  out.edit_posted = POSTED[POSTED.length - 1];

  /* --- a refusal stays on the form and is shown VERBATIM --- */
  render({accounts: [], revision: "rev-refuse"});
  declFormOpen(null);
  el("#dId").value = "acct-new";
  POST_REPLY = {error: "notes looks like it contains an API key or token.",
                code: "refused"};
  await declSave();
  out.refuse_msg = el("#dMsg").innerHTML;
  out.refuse_form_open = !el("#declform").hidden;

  /* --- a conflict reloads before anything else --- */
  render({accounts: [], revision: "rev-conflict"});
  declFormOpen(null);
  POST_REPLY = {error: "someone else changed the accounts list. Reload the page.",
                code: "conflict"};
  RENDER_ACCTS_CALLS = 0;
  await declSave();
  out.conflict_msg = el("#dMsg").innerHTML;
  out.conflict_reloaded = RENDER_ACCTS_CALLS > 0;

  /* --- remove asks first, and a refusal at the dialog posts nothing --- */
  render({accounts: [A()], revision: "rev-rm"});
  POSTED.length = 0;
  CONFIRMED = false;
  await declRemove("acct-a");
  out.remove_without_confirm = POSTED.length;
  CONFIRMED = true;
  POST_REPLY = {ok: true};
  await declRemove("acct-a");
  out.remove_posted = POSTED[POSTED.length - 1];


/* ══ fill mode: every row on one screen ══ */
const FILL_DEFAULTS = {accounts: [], bad: [], unreadable: null, revision: "rf1",
                       totals: {accounts: 0, units: 0, monthly_spend: 0, unpriced: 0},
                       join: {matched: {}, declared_only: [], measured_only: []},
                       cap: 64};

/* ONE MEASURED CREDENTIAL, in the shape /api/creds really answers — including
   the two opaque handles the browser is given instead of a login. */
const M = (over) => Object.assign({
  name: "quota-row-9", provider: "vendor-x", tier: "Big Plan", state: "ok",
  headroom: 50, active: false, home: "/h", home_name: "h",
  subscription: "s-nine", measured_key: "m-deadbeefdeadbeef",
}, over || {});

/* DRIVE THE SHIPPED COMPOSITION the way the browser does — by saying what is
   declared and what is measured — and hand back what fill mode iterates. */
const FR = (decls, creds) => {
  DECL = Object.assign({}, FILL_DEFAULTS, {accounts: decls || []});
  CREDS = creds || [];
  return fillRows();
};

function fillRender(state) {
  EL["#declfill"] = null; delete EL["#declfill"];
  DECL = Object.assign({}, FILL_DEFAULTS, state || {});
  CREDS = (state && state.creds) || [];
  DECL_FILL = true;
  renderDeclFill();
  return el("#declfill").innerHTML;
}

/* THE SORT. A row nobody described, then one not yet confirmed, then the done
   ones — and the input is deliberately in the WORST order, so "sorted" is a
   claim about the function rather than about the fixture. */
out.fill_sort = declFillSort(FR([
  A({id: "done-1", confirmed: true}),
  A({id: "mine-2", confirmed: true}),
  A({id: "unconfirmed", confirmed: false, needs_confirm: true}),
  A({id: "undescribed", needs_describe: true, needs_confirm: true}),
])).map(r => r.id);
/* the CONTROL on the same function: two rows in the SAME tier keep the order
   they were declared in, so the sort above moved a row for its state rather
   than for its name. */
out.fill_sort_stable = declFillSort(FR([
  A({id: "zed", confirmed: true}), A({id: "alpha", confirmed: true})
])).map(r => r.id);
/* A MEASURED ACCOUNT WITH NO RECORD is the emptiest row there is and sorts to
   the very top — it is the one the owner has not been asked about yet. */
out.fill_sort_undescribed_measured = declFillSort(
  FR([A({id: "done", confirmed: true})], [M()])).map(r => r.id || r.label);

/* the SHIPPED column list, handed back whole: an arm that retyped these keys
   would keep agreeing with itself after the page changed. */
out.fill_col_keys = JSON.stringify(FILL_COLS.map(c => ({key: c.key})));
out.fill_header = declFillHeader(FR([
  A({id: "a", confirmed: true, price_value: 7}),
  A({id: "b", confirmed: true, price_value: 20}),
  A({id: "c", confirmed: false, price_value: null, needs_describe: true}),
]));
out.fill_header_done = declFillHeader(FR([A({id: "a", confirmed: true, price_value: 7})]));

out.fill_table = fillRender({
  accounts: [A({id: "acct-a", confirmed: true, price_value: 7}),
             A({id: "acct-b", billing: "sub", allowance: "20 requests per week",
                key_where: "the codex credhome", needs_describe: true})]});
out.fill_unreadable = fillRender({unreadable: "the accounts file could not be read as an inventory"});
out.fill_empty = fillRender({accounts: []});

/* the toggle hands the screen to ONE of the two cards */
fillRender({accounts: [A()]});
DECL_FILL = false;
declFillToggle();
/* BOTH READINGS IN ONE SENSE — visible, never a mix of visible and hidden.
   The first spelling asked one of them the opposite question and the arm read
   as though the cards were on screen together. */
out.fill_on = {fill: !el("#declfill").hidden, read: !el("#declrows").hidden,
               label: el("#dFill").textContent};
declFillToggle();
out.fill_off = {fill: !el("#declfill").hidden, read: !el("#declrows").hidden,
                label: el("#dFill").textContent};

  /* ── and the writing half of fill mode, in the same IIFE ── */
  /* --- ONE CELL POSTS ONE FIELD, with the revision it was drawn against --- */
  fillRender({accounts: [A({id: "acct-a", price_month: "$7"})], revision: "rev-fill"});
  POSTED.length = 0;
  POST_REPLY = {ok: true, revision: "rev-fill-2",
                account: A({id: "acct-a", price_month: "$20", price_value: 20})};
  await declFillSave({dataset: {fkeyrow: "id:acct-a", fkey: "price_month", frow: "0"},
                      value: "$20", type: "text"});
  out.fill_cell_posted = POSTED[POSTED.length - 1];
  out.fill_revision_after = DECL.revision;
  out.fill_row_after = DECL.accounts[0].price_month;

  /* …and a box he tabbed through WITHOUT changing posts nothing at all */
  POSTED.length = 0;
  await declFillSave({dataset: {fkeyrow: "id:acct-a", fkey: "price_month", frow: "0"},
                      value: "$20", type: "text"});
  out.fill_unchanged_posts = POSTED.length;

  /* --- A CELL ON A ROW WITH NO RECORD CREATES ONE, COMPOSED --- */
  /* the owner's ruling: "to compose instead of make new". He types a price
     against an account helm measures and nobody has described; the save
     carries the fields helm has already READ — vendor, plan tier, reach — and
     binds the new record to that account through its handle. `good_for` and
     `not_for` are exactly what no reading can supply and are NOT invented. */
  fillRender({accounts: [], revision: "rev-new", creds: [M()]});
  POSTED.length = 0;
  POST_REPLY = {ok: true, revision: "rev-new-2",
                /* the real door answers through `client_rows`, so the record
                   carries the key the page groups on — which is the whole of
                   HIGH-1 and is proven end to end in
                   CreateTransitionEndToEndTest against the shipped writer. */
                account: A({id: "vendor-x-quota-row-9", price_month: "$9",
                            subscription: "s-nine"})};
  await declFillSave({dataset: {fkeyrow: "s-nine", fkey: "price_month",
                                frow: "0"}, value: "$9", type: "text"});
  out.fill_create_posted = POSTED[POSTED.length - 1];
  /* THE SECOND CELL ON THAT LINE EDITS THE RECORD THE FIRST ONE CREATED, and
     this is the whole of "compose instead of make new": without it every cell
     he fills on an undescribed account would mint another row for it. */
  POSTED.length = 0;
  POST_REPLY = {ok: true, revision: "rev-new-3",
                account: A({id: "vendor-x-quota-row-9", price_month: "$9",
                            notes: "renews on the 3rd", subscription: "s-nine"})};
  await declFillSave({dataset: {fkeyrow: "s-nine", fkey: "notes",
                                frow: "0"}, value: "renews on the 3rd", type: "text"});
  out.fill_second_cell_posted = POSTED[POSTED.length - 1];
  out.fill_after_create_ids = (DECL.accounts || []).map(a => a.id);

  /* the tick is a field like any other and carries a boolean */
  fillRender({accounts: [A({id: "acct-a", confirmed: false})], revision: "rev-tick"});
  POSTED.length = 0;
  POST_REPLY = {ok: true, revision: "rev-fill-3",
                account: A({id: "acct-a", confirmed: true, price_value: 20})};
  await declFillSave({dataset: {fkeyrow: "id:acct-a", fkey: "confirmed", frow: "0"},
                      checked: true, type: "checkbox"});
  out.fill_tick_posted = POSTED[POSTED.length - 1];

  /* --- A CELL WHOSE ROW IS GONE SAYS SO, AND NEVER SAVES INTO NOTHING --- */
  /* The cells carry their row's key from the moment the table was drawn, and
     the composition under them can move — a re-measure that re-keys an
     account, a row removed in another tab. His only feedback on this screen IS
     the row, so a silent no-op reads exactly like a save. */
  const LOSTMSG = {dataset: {fmsg: "s-vanished"}, innerHTML: ""};
  fillRender({accounts: [A({id: "acct-a"})], revision: "rev-lost"});
  el("#declfill").querySelectorAll = () => [LOSTMSG];
  POSTED.length = 0;
  TOASTS.length = 0;
  RENDER_ACCTS_CALLS = 0;
  FETCH_REPLY = Object.assign({}, FILL_DEFAULTS, {revision: "rev-lost-2"});
  out.fill_lost_returned = await declFillSave(
    {dataset: {fkeyrow: "s-vanished", fkey: "price_month", frow: "0"},
     value: "$40", type: "text"});
  out.fill_lost_posted = POSTED.length;
  out.fill_lost_msg = LOSTMSG.innerHTML;
  out.fill_lost_toast = TOASTS[TOASTS.length - 1];
  out.fill_lost_reloaded = RENDER_ACCTS_CALLS;

  /* --- A REFUSAL STAYS BESIDE THE ROW IT IS ABOUT --- */
  const MSG = {dataset: {fmsg: "id:acct-a"}, innerHTML: ""};
  fillRender({accounts: [A({id: "acct-a"})], revision: "rev-refuse"});
  el("#declfill").querySelectorAll = () => [MSG];
  POST_REPLY = {error: "key_where looks like it contains an API key or token.",
                code: "refused"};
  RENDER_ACCTS_CALLS = 0;
  await declFillSave({dataset: {fkeyrow: "id:acct-a", fkey: "key_where", frow: "0"},
                      value: "sk-not-a-place", type: "text"});
  out.fill_refusal_msg = MSG.innerHTML;
  out.fill_refusal_reloaded = RENDER_ACCTS_CALLS;

  /* --- A STALE REVISION NAMES THE ROW AND RELOADS THE CARD --- */
  const MSG2 = {dataset: {fmsg: "id:acct-a"}, innerHTML: ""};
  fillRender({accounts: [A({id: "acct-a"})], revision: "rev-stale"});
  el("#declfill").querySelectorAll = () => [MSG2];
  POST_REPLY = {error: "someone else changed the accounts list. Reload the page.",
                code: "conflict"};
  TOASTS.length = 0;
  RENDER_ACCTS_CALLS = 0;
  FETCH_REPLY = Object.assign({}, FILL_DEFAULTS, {revision: "rev-stale-2"});
  await declFillSave({dataset: {fkeyrow: "id:acct-a", fkey: "vendor", frow: "0"},
                      value: "vendor-z", type: "text"});
  out.fill_conflict_msg = MSG2.innerHTML;
  out.fill_conflict_toast = TOASTS[TOASTS.length - 1];
  out.fill_conflict_reloaded = RENDER_ACCTS_CALLS;

  /* --- ENTER AND TAB GO DOWN THE COLUMN --- */
  const BOXES = [];
  const FOCUSED = [];
  for (let r = 0; r < 3; r++) {
    for (const k of ["vendor", "plan"]) {
      BOXES.push({dataset: {fid: "acct-" + r, fkey: k, frow: String(r)},
                  value: "", type: "text",
                  focus() { FOCUSED.push(this.dataset.fkey + "/" + this.dataset.frow); }});
    }
  }
  fillRender({accounts: [A({id: "acct-0"}), A({id: "acct-1"}), A({id: "acct-2"})],
              revision: "rev-keys"});
  el("#declfill").querySelectorAll = () => BOXES;
  const ev = key => ({key, shiftKey: false, preventDefault() { this.prevented = true; }});
  const enter = ev("Enter");
  declFillKey(enter, BOXES[0]);              // vendor row 0 -> vendor row 1
  const tab = ev("Tab");
  declFillKey(tab, BOXES[2]);                // vendor row 1 -> vendor row 2
  const back = {key: "Tab", shiftKey: true, preventDefault() { this.prevented = true; }};
  declFillKey(back, BOXES[4]);               // vendor row 2 -> vendor row 1
  /* A SNAPSHOT, not the array. The control below clears FOCUSED to count its
     own focus calls, and a reference here would be emptied with it — the
     reading would be taken after the thing it measured was reset. */
  out.fill_focus_order = FOCUSED.slice();
  out.fill_key_prevented = [!!enter.prevented, !!tab.prevented, !!back.prevented];
  /* an ordinary character is NOT navigation — the control that says the two
     keys above were handled for being those keys */
  const plain = ev("a");
  FOCUSED.length = 0;
  declFillKey(plain, BOXES[0]);
  out.fill_plain_key = {focused: FOCUSED.length, prevented: !!plain.prevented};

  console.log(JSON.stringify(out));
})();
"""


class DeclaredCardRenderTest(unittest.TestCase):
    """The card the owner reads, executed rather than described."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, name) for name in EXTRACT)
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-accounts-")
        cls.path = os.path.join(cls.tmp, "run.js")
        broken = os.path.join(tempfile.mkdtemp(prefix="helm-web-accounts-bad-"),
                              "accounts.json")
        with open(broken, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "accounts": dict(BROKEN_ROWS)}, f)
        bad_rows = accounts.read(broken)["bad"]
        assert len(bad_rows) == len(BROKEN_ROWS), bad_rows
        undescribed = accounts.join([], UNDESCRIBED_MEASURED)["measured_only"]
        assert any(m["name"] == MEASURED_ADDRESS for m in undescribed), \
            "the fixture must carry the whole address, or 'it is absent from " \
            "the HTML' is a statement about an empty input"
        driver = DRIVER.replace("__UNDESCRIBED__", json.dumps(undescribed)) \
                       .replace("__BADROWS__", json.dumps(bad_rows))
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(SUPPORT + "\nlet DECL, DECL_EDIT = null, DECL_MKEY = null;\n"
                    + "let DECL_FILL = false;\n"
                    + _extract_const(src, "DECL_FIELDS") + "\n"
                    + _extract_const(src, "FILL_COLS") + "\n"
                    + _extract_const(src, "FILL_BILLING") + "\n"
                    + fns + "\n" + driver)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        proc = subprocess.run([cls.node, cls.path], capture_output=True,
                              text=True, timeout=60)
        cls.proc = proc
        try:
            cls.out = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def html(self, key):
        self.assertIn(key, self.out, "harness produced no %s\nstdout=%r\nstderr=%r"
                      % (key, self.proc.stdout, self.proc.stderr))
        return self.out[key]

    # ---- headlines and detail ----

    def test_the_headline_is_the_servers_and_appears_on_the_row(self):
        html = self.html("declared_only")
        self.assertIn("vendor-x · Small Plan · x1 · $7/mo · reading the live timeline",
                      html)

    def test_for_and_not_for_live_behind_a_native_details_never_inline(self):
        html = self.html("declared_only")
        self.assertIn("<details><summary>details</summary>", html)
        head, _sep, detail = html.partition("<details>")
        self.assertNotIn("building; the quota is too small", head,
                         "what it must NOT be used for belongs behind the click")
        self.assertIn("building; the quota is too small", detail)
        self.assertIn("reading the live timeline", detail)

    def test_the_fold_reads_back_billing_allowance_and_where_the_key_is_kept(self):
        html = self.html("three_fields")
        _head, _sep, detail = html.partition("<details>")
        for label, value in (("billing", "subscription"),
                             ("allowance", "20 requests per week"),
                             ("key kept where", "the opencode credential home")):
            self.assertIn(label, detail, "the fold must label %r" % label)
            self.assertIn(value, detail, "the fold must print %r" % label)
        # THE CONTROL on the same observable: a row declaring none of the three
        # does not grow empty rubric, so the arm above is about the values
        # reaching the fold rather than about three labels always being there.
        bare = self.html("three_fields_absent")
        self.assertIn("declrow", bare,
                      "the control rendered a row at all, so the absence "
                      "below is about the three fields and not an empty page")
        self.assertNotIn("key kept where", bare)

    def test_a_declared_row_with_no_measurement_says_so_and_shows_no_quota_cell(self):
        html = self.html("declared_only")
        self.assertIn("declared, not measured", html)
        self.assertNotIn("qcell", html,
                         "a fake quota cell would make an unmeasured account "
                         "look like a broken measured one")

    def test_a_declared_row_naming_a_measurement_that_is_gone_names_it(self):
        html = self.html("stale_measurement")
        self.assertIn("declared, not measured", html)
        self.assertIn("quota-row-gone", html)

    def test_a_matched_row_says_which_measurement_it_is(self):
        html = self.html("measured_match")
        self.assertIn("measured as quota-row-1", html)
        self.assertNotIn("declared, not measured", html)

    def test_an_address_shaped_join_key_renders_masked_never_whole(self):
        """The join needs the provider's own spelling; the page does not. A new
        card that printed account addresses would publish something no existing
        verb publishes."""
        html = self.html("masked_address")
        self.assertIn("measured as p…@example.test", html)
        self.assertNotIn("person@example.test", html)

    def test_a_seeded_row_asks_to_be_confirmed_in_the_headline_area(self):
        html = self.html("seeded")
        self.assertIn("helm&#39;s measured quota rows — please confirm", html)
        self.assertEqual(html.count("please confirm"), 1,
                         "the renderer appends the ask; a source that also "
                         "carries it renders the phrase twice")

    def test_a_seeded_row_carries_a_confirm_button_beside_the_ask(self):
        """Without it the only answer to "please confirm" is `edit`, and an
        edit is a fourteen-field rewrite driven from a form that holds eleven."""
        html = self.html("seeded")
        self.assertIn('data-dact="confirm"', html)
        # the same observable, positively: the unseeded card IS a rendered row
        # with its own buttons, so "no confirm here" is about a drawn row
        plain = self.html("declared_only")
        self.assertIn('data-dact="edit"', plain)
        self.assertNotIn('data-dact="confirm"', plain)

    def test_an_unreadable_measured_side_is_unknown_never_no_quota_row(self):
        """A provider that RAISED is not a fleet with no measured accounts."""
        html = self.html("measured_down")
        self.assertIn("could not read the measured quota rows", html)
        self.assertIn("UNKNOWN, not absent", html)
        self.assertNotIn("no quota row called", html)
        # …and the control: with the provider healthy the same row says so
        self.assertIn("measured as quota-row-1", self.html("measured_match"))

    def test_an_unmeasured_row_keeps_its_own_sentence_when_the_provider_is_down(self):
        """THE SAME MISTAKE POINTED THE OTHER WAY. "this row's measurement is
        UNKNOWN, not absent" is right about a row that NAMES a measurement and
        wrong about a row that names none — nothing ever measures that one, so
        its quota line is not unknown, it is the sentence it always had."""
        html = self.html("measured_down_unmeasured")
        self.assertIn("no quota provider watches this one", html)
        self.assertNotIn("UNKNOWN", html)
        # THE CONTROL, same provider failure, a row that DOES name one: the
        # unknown sentence is still drawn, so this is about the row and not
        # about a card that has stopped reporting the failure.
        self.assertIn("UNKNOWN", self.html("measured_down"))

    def test_every_row_carries_its_own_edit_and_remove_buttons(self):
        html = self.html("declared_only")
        self.assertIn('data-dact="edit"', html)
        self.assertIn('data-dact="rm"', html)
        self.assertIn('data-did="acct-a"', html)

    # ---- totals ----

    def test_two_rows_naming_one_login_say_so_on_the_card(self):
        """He could tell they were duplicates only from memory of which
        credential home belonged to which account. Now the row says it."""
        html = self.html("dup_proven")
        self.assertIn("same subscription as home-b", html)
        self.assertIn("ddup", html)

    def test_a_row_bound_to_a_default_is_called_a_pointer_not_a_plan(self):
        self.assertIn("points at a default", self.html("dup_pointer"))

    def test_an_unbound_row_is_ASKED_about_rather_than_accused(self):
        """helm cannot prove whether it is its own account or a second name for
        one — so the card must not claim to know. Different word, different
        class, and it never says "same subscription"."""
        html = self.html("dup_question")
        self.assertIn("names no login", html)
        self.assertIn("dask", html)
        self.assertNotIn("same subscription", html)

    def test_a_row_with_nothing_to_say_says_nothing(self):
        """THE UNCONDITIONAL CONTROL on the same renderer: without it every arm
        above would pass on a card that simply prints all three on every row."""
        html = self.html("dup_silent")
        for said in ("same subscription", "points at a default", "names no login"):
            self.assertNotIn(said, html)
        # …and it is a REAL rendered row, not an empty string
        self.assertIn("clean", html)

    def test_the_totals_line_says_accounts_units_and_monthly_spend(self):
        html = self.html("declared_only")
        self.assertIn("<b>1</b> account declared", html)
        self.assertIn("<b>1</b> subscription in total", html)
        self.assertIn("<b>$7</b>/month", html)

    def test_an_unpriced_row_makes_the_total_say_it_is_a_floor(self):
        html = self.html("unpriced")
        self.assertIn("1 with no price recorded", html)
        self.assertIn("the total is a floor", html)

    def test_a_fully_priced_inventory_claims_no_floor(self):
        """The control for the arm above: the disclaimer must be conditional,
        not printed on every render."""
        html = self.html("declared_only")
        self.assertIn("<b>$7</b>/month", html)
        self.assertNotIn("the total is a floor", html)

    # ---- the states that must never read as zero ----

    def test_an_unreadable_inventory_renders_the_unknown_strip_not_an_empty_card(self):
        html = self.html("unreadable")
        self.assertIn("dunknown", html)
        self.assertIn("could not be read", html)
        self.assertIn('this is not "none"', html)

    def test_an_empty_inventory_invites_the_first_entry(self):
        html = self.html("empty")
        self.assertIn("nothing declared yet", html)
        self.assertNotIn("dunknown", html)

    def test_a_skipped_row_is_shown_and_says_nothing_was_dropped(self):
        html = self.html("bad_entry")
        self.assertIn("acct-bad", html)
        self.assertIn("nothing was dropped", html)
        self.assertIn('data-dact="rm"', html)

    def test_a_broken_rows_remove_button_carries_a_handle_that_works(self):
        """The name is the ESCAPED rendering, which is what makes it safe to
        print — and exactly what `remove` refuses. So the button carried a
        string that could never remove anything. The handle does, and the
        escaped spelling stays for the eye."""
        html = self.html("bad_entry")
        self.assertIn("acct-\\x07x", html)          # the eye still gets it…
        self.assertNotIn("acct-\x07x", html)        # …and never the raw control
        self.assertIn('data-did="%s"' % accounts.bad_handle(BROKEN_KEY), html)
        self.assertNotIn('data-did="acct-\\x07x"', html)
        # the CONTROL on the same list: a row whose KEY is fine is still
        # removed by its own id, not by a handle
        self.assertIn('data-did="acct-bad"', html)

    def test_a_measured_account_nobody_described_gets_a_one_click_affordance(self):
        html = self.html("undescribed")
        self.assertIn('data-dact="describe"', html)
        self.assertIn("not described yet", html)
        # the account that was never an address is named as the provider named
        # it — the CONTROL that proves the masker below is the EMAIL masker and
        # not a blanket redaction of every measured name
        self.assertIn("describe quota-row-2", html)

    def test_the_describe_buttons_name_an_address_masked_never_whole(self):
        """THE ONE THE OWNER SEES. This card's own module says every human
        rendering masks the provider's account name; these buttons printed it
        whole, so the page published addresses no verb publishes. The fixture
        is what `accounts.join` really answers and it CARRIES the address, so
        "absent" here is about a row that has one."""
        html = self.html("undescribed")
        self.assertIn("s…@example.test", html)        # it IS named…
        self.assertNotIn(MEASURED_ADDRESS, html)      # …and never whole
        self.assertNotIn("someone@", html)
        # and the button posts the HANDLE, because a mask is not a key
        self.assertIn('data-dkey="%s"' % accounts.measured_key(MEASURED_ADDRESS),
                      html)

    def test_the_describe_button_opens_the_form_on_the_handle_it_carries(self):
        """A round trip in two halves: this is the page's half — the id it
        suggests, the mask it shows, and the handle it will post. The server
        half is test_the_describe_handle_round_trips_through_the_real_save."""
        got = self.html("describe_by_key")
        self.assertEqual(got["id"], "someone")
        self.assertEqual(got["measured"], "s…@example.test")
        self.assertTrue(got["readonly"],
                        "a mask he can type over is a join key he cannot see")
        self.assertEqual(got["payload"]["measured_key"],
                         accounts.measured_key(MEASURED_ADDRESS))
        self.assertNotIn("measured_as", got["payload"],
                         "posting the mask as the join key would join nothing")
        # the control: the ORDINARY path still posts a typed name, and closing
        # the form forgets the handle
        self.assertNotIn("measured_key", self.html("describe_key_cleared"))
        self.assertIn("measured_as", self.html("describe_key_cleared"))
        self.assertFalse(self.html("describe_prefill")["readonly"])

    # ---- the chips lent to the measured table ----

    def test_a_matched_account_lends_chips_and_an_unmatched_one_lends_none(self):
        self.assertTrue(self.html("chips_matched"))
        self.assertEqual(self.html("chips_unmatched"), [])
        joined = "".join(self.html("chips_matched"))
        self.assertIn("Small Plan", joined)
        self.assertIn("$7/mo", joined)
        self.assertIn("for: reading the live timeline", joined)

    def test_a_seeded_row_nobody_described_lends_the_ASK_not_the_placeholder(self):
        """The seed had to fill a required field to write the row at all, so
        every measured row on the owner's board gained a chip reading "for: not
        described yet — say what this one i…" and the one-click describe
        affordance became unreachable on all of them. The prompt is drawn as a
        prompt."""
        joined = "".join(self.html("chips_undescribed"))
        self.assertIn("+ describe", joined)
        self.assertIn('data-declare="quota-row-1"', joined)
        self.assertNotIn("not described yet", joined)
        self.assertIn("Small Plan", joined)     # the plan chip still rides along

    def test_the_chips_carry_no_quota_cell_markup_at_all(self):
        joined = "".join(self.html("chips_matched"))
        self.assertIn("flagchip", joined)     # there ARE chips to inspect
        for forbidden in ("<td", "qcell", "qbar", "</tr>"):
            self.assertNotIn(forbidden, joined,
                             "the declared layer may only APPEND to the meta "
                             "line; it may never emit a measured cell")

    # ---- add / edit / describe / save / remove ----

    def test_an_edit_cannot_retype_the_id_and_posts_the_one_it_opened(self):
        """BOTH HALVES OF THE SAME RULE on the page: the box he cannot change,
        and the id the payload carries if something changes it anyway. The
        server half — the refusal — is
        test_a_page_edit_that_changes_the_id_refuses_and_leaves_one_row."""
        self.assertTrue(self.html("edit_id_readonly"))
        self.assertFalse(self.html("add_id_readonly"),
                         "an ADD is where he types the id; only an edit locks it")
        self.assertEqual(self.html("edit_tampered_payload")["id"], "acct-a")
        posted = self.html("edit_posted")
        self.assertEqual(posted["account"]["id"], "acct-a")
        self.assertEqual(posted["original_id"], "acct-a")
        # the control on the same field: an ADD names no original row
        self.assertIsNone(self.html("save_posted").get("original_id"))

    def test_the_add_form_opens_with_a_sane_count_and_no_id(self):
        self.assertTrue(self.html("add_form_visible"))
        self.assertEqual(self.html("add_count_default"), "1")

    def test_the_form_posts_every_field_it_carries_empties_included(self):
        """THE DOOR MERGES, so an omitted field keeps its stored value — which
        makes an omitted EMPTY a box the owner can never clear. The form is the
        writer that means "clear it", so it sends what it holds, blank or not.
        `seeded_from` survives because the form never holds it at all."""
        payload = self.html("add_payload")
        self.assertEqual(payload["id"], "acct-new")
        self.assertEqual(payload["count"], "2")
        self.assertEqual(payload["notes"], "")
        self.assertEqual(payload["measured_as"], "")
        self.assertNotIn("seeded_from", payload)
        self.assertIs(payload["confirmed"], False)

    def test_the_form_carries_the_renewal_date_and_the_confirm_tick(self):
        """The details fold RENDERS "renews <date>"; before this the form had
        no box for it, so the edit button deleted what the fold displayed."""
        payload = self.html("add_payload_full")
        self.assertEqual(payload["renews_on"], "2027-01-31")
        self.assertIs(payload["confirmed"], True)

    def test_clearing_a_box_sends_the_empty_that_clears_the_field(self):
        payload = self.html("edit_cleared_payload")
        self.assertEqual(payload["notes"], "")
        self.assertEqual(payload["plan"], "Small Plan")   # …and only that one

    def test_the_confirm_tick_follows_the_row_it_opened(self):
        """A sticky checkbox would confirm the NEXT row he opens, silently.
        The driver opens a CONFIRMED row first and an unconfirmed one second,
        against the same stub element: the True is what makes the False mean
        the box moved rather than never having been ticked."""
        boxes = [self.html("edit_prefill")["confirmed"],
                 self.html("edit_unconfirmed_box")]
        self.assertEqual(boxes, [True, False])

    def test_editing_an_existing_row_prefills_every_field_it_has(self):
        pre = self.html("edit_prefill")
        self.assertEqual(pre["id"], "acct-a")
        self.assertEqual(pre["vendor"], "vendor-x")
        self.assertEqual(pre["plan"], "Small Plan")
        self.assertEqual(pre["count"], "1")
        self.assertEqual(pre["price"], "$7")
        self.assertEqual(pre["good"], "reading the live timeline")
        self.assertEqual(pre["not_for"], "building; the quota is too small")
        self.assertEqual(pre["reach"], "owner-only, ask")
        self.assertEqual(pre["notes"], "renewed by card")
        self.assertEqual(pre["renews"], "2027-01-31")
        self.assertIn("the id stays the same", self.html("edit_msg"))

    def test_the_prefill_lands_on_a_free_id_never_on_a_row_already_declared(self):
        """Two measured accounts can slug to one id; the prefill for the second
        must not open the first's row. Control: a free slug stays itself."""
        self.assertEqual(self.html("free_id"), ["dana-3", "mira"])

    def test_describing_a_measured_account_prefills_a_slug_never_the_address(self):
        pre = self.html("describe_prefill")
        self.assertEqual(pre["measured"], "person@example.test")
        self.assertEqual(pre["id"], "person")
        self.assertNotIn("@", pre["id"])

    def test_describing_an_already_declared_account_edits_that_row(self):
        """A seeded row carries the describe affordance too, so the affordance
        has to know the row exists — prefilling a fresh slug mints a second row
        for one account and leaves the first still asking."""
        got = self.html("describe_existing")
        self.assertEqual(got["id"], "acct-seeded")
        self.assertEqual(got["editing"], "acct-seeded")
        self.assertIn("the id stays the same", got["msg"])
        # the control on the same call: an UNDECLARED name still gets a slug
        self.assertEqual(self.html("describe_prefill")["id"], "person")

    def test_the_slug_helper_drops_everything_an_id_may_not_contain(self):
        self.assertEqual(self.html("slug"), ["person", "some-name-2", ""])

    def test_a_save_carries_the_revision_the_form_was_opened_against(self):
        posted = self.html("save_posted")
        self.assertEqual(posted["action"], "save")
        self.assertEqual(posted["revision"], "rev-save")
        self.assertEqual(posted["account"]["id"], "acct-new")

    def test_a_successful_save_closes_the_form(self):
        self.assertTrue(self.html("save_form_closed"))

    def test_a_refusal_is_shown_verbatim_and_leaves_the_form_open_to_fix(self):
        msg = self.html("refuse_msg")
        self.assertIn("notes looks like it contains an API key or token.", msg)
        self.assertTrue(self.html("refuse_form_open"))

    def test_a_conflict_reloads_the_card_so_he_is_not_editing_a_ghost(self):
        self.assertIn("Reload the page", self.html("conflict_msg"))
        self.assertTrue(self.html("conflict_reloaded"))

    def test_remove_asks_before_it_posts_anything(self):  # noqa: VACUOUS_ASSERTION — ZERO posts is the product law; the control on the same POSTED channel is test_remove_carries_the_id_and_the_revision, which records a post from the identical call once the dialog is answered
        self.assertEqual(self.html("remove_without_confirm"), 0)

    def test_remove_carries_the_id_and_the_revision(self):
        posted = self.html("remove_posted")
        self.assertEqual(posted["action"], "remove")
        self.assertEqual(posted["id"], "acct-a")
        self.assertEqual(posted["revision"], "rev-rm")


# ---------------------------------------------------------------------------
# the REAL transport — the card's error path, executed
    # ---- fill mode: the screen the owner actually fills ----

    def test_the_rows_that_still_need_him_sort_to_the_top(self):
        self.assertEqual(self.html("fill_sort"),
                         ["undescribed", "unconfirmed", "done-1", "mine-2"])

    def test_a_measured_account_with_no_record_is_a_row_he_can_fill(self):
        """THE OWNER'S NAMED ARM. An account helm measures and nobody has
        described is a row here, and it sorts to the top because it is the one
        he has not been asked about. A screen that edited declared RECORDS
        would not show it at all until the seed minted one, which is the "make
        new" he objected to."""
        self.assertEqual(self.html("fill_sort_undescribed_measured")[0],
                         "quota-row-9")

    def test_filling_a_cell_on_such_a_row_composes_a_record_never_invents_one(self):
        """"adding fields intelligently": the save carries what helm has
        already READ — the vendor, the plan tier, the reach — beside the one
        field he typed. What it does NOT carry is a sentence about what the
        account is for, because no reading can supply that and the seeder's
        guess in his mouth is what he complained about."""
        posted = self.html("fill_create_posted")["account"]
        self.assertEqual(posted["price_month"], "$9")
        self.assertEqual(posted["vendor"], "vendor-x")
        self.assertEqual(posted["plan"], "Big Plan")
        self.assertEqual(posted["reach"], "vendor-x")
        # bound to the ACCOUNT, through the handle the door resolves — a record
        # that joined nothing would read back as "declared, not measured"
        self.assertEqual(posted["measured_key"], "m-deadbeefdeadbeef")
        for invented in ("good_for", "not_for"):
            self.assertNotIn(invented, posted)

    def test_the_next_cell_edits_that_record_instead_of_making_another(self):
        """This is the whole of "compose instead of make new". The cells carry
        the SUBSCRIPTION in the DOM, not the record's id, so creating a record
        cannot change the key the rest of the line was drawn with — keyed by
        the record, every cell after the first would have looked up a key that
        no longer existed and silently saved nothing."""
        second = self.html("fill_second_cell_posted")
        self.assertEqual(second["account"], {"notes": "renews on the 3rd",
                                             "id": "vendor-x-quota-row-9"})
        self.assertEqual(second["original_id"], "vendor-x-quota-row-9")
        # ONE record for one account, which is the claim
        self.assertEqual(self.html("fill_after_create_ids"),
                         ["vendor-x-quota-row-9"])

    def test_a_cell_whose_row_is_gone_says_so_and_never_saves_into_nothing(self):
        """A cell that answers "nothing happened" to a value he typed is the
        worst shape this screen has: his only feedback IS the row re-rendering,
        so a silent no-op is indistinguishable from a save. Rare does not make
        it acceptable — it makes it a thing he will not be looking for."""
        self.assertEqual(self.html("fill_lost_posted"), 0,
                         "a row that is gone cannot be written to")
        said = self.html("fill_lost_msg")
        self.assertIn("not on the screen any more", said)
        self.assertIn("nothing was saved", said)
        self.assertIn("not on the screen any more", self.html("fill_lost_toast"))
        # …and the screen is put back in step, so the next thing he sees is
        # true rather than the row he just typed into
        self.assertEqual(self.html("fill_lost_reloaded"), 1)
        # THE CONTROL on the same call shape: it answers a REFUSAL object, not
        # the `null` a silent no-op returns, so a caller can tell them apart.
        self.assertEqual(self.html("fill_lost_returned")["code"], "gone")

    def test_the_fill_screen_carries_a_notes_box(self):
        """His words on the screen he had just filled: "i didnt see a field for
        notes". It was on the add/edit form all along — only this table lacked
        it, and this table is the screen he was on. Lifted from the shipped
        page, so a column removed there fails here."""
        keys = [c["key"] for c in json.loads(self.html("fill_col_keys"))]
        self.assertIn("notes", keys)
        # THE CONTROL on the same list: the columns that were always there
        # still are, so this is an addition rather than a replacement.
        for was in ("vendor", "plan", "price_month", "billing", "confirmed"):
            self.assertIn(was, keys)

    def test_two_rows_in_one_tier_keep_the_order_they_were_declared_in(self):
        """The CONTROL on the sort: it moves a row for its STATE. Without
        this, a sort that happened to order by id would pass the arm above."""
        self.assertEqual(self.html("fill_sort_stable"), ["zed", "alpha"])

    def test_the_header_says_how_many_are_confirmed_and_how_many_unpriced(self):
        head = self.html("fill_header")
        for fragment in ("<b>2</b> of <b>3</b> row", "confirmed",
                         "<b>1</b> unpriced", "nobody has described"):
            self.assertIn(fragment, head)
        self.assertIn("still need you are at the top", head)

    def test_a_finished_inventory_says_that_is_all_of_them(self):
        done = self.html("fill_header_done")
        self.assertIn("that is all of them", done)
        self.assertIn("dfilldone", done)
        # the control on the same producer: the unfinished header does not
        self.assertNotIn("that is all of them", self.html("fill_header"))

    def test_every_editable_field_is_a_cell_in_the_order_the_columns_declare(self):
        html = self.html("fill_table")
        at = [html.index('data-fkey="%s"' % key) for key in
              ("vendor", "plan", "count", "price_month", "billing", "allowance",
               "key_where", "good_for", "not_for", "renews_on", "confirmed")]
        self.assertEqual(at, sorted(at), "the cells are not in column order")

    def test_the_id_is_not_an_editable_cell_and_neither_is_the_join_key(self):
        html = self.html("fill_table")
        self.assertNotIn('data-fkey="id"', html,
                         "retyping the id writes a SECOND row; the door "
                         "refuses it and the screen must not offer it")
        self.assertNotIn('data-fkey="measured_as"', html)

    def test_billing_is_a_closed_set_on_the_screen_never_a_free_box(self):
        html = self.html("fill_table")
        self.assertIn("<select", html)
        for token in ("sub", "payg", "prepaid", "free"):
            self.assertIn('value="%s"' % token, html)
        self.assertIn("not declared", html)

    def test_a_row_carrying_its_values_renders_them_in_its_boxes(self):
        html = self.html("fill_table")
        self.assertIn('value="20 requests per week"', html)
        self.assertIn('value="the codex credhome"', html)

    def test_the_no_secrets_refusal_is_on_the_fill_screen_too(self):
        html = self.html("fill_table")
        self.assertIn("never a password, key, token or email", html)
        self.assertIn("never the key", html)

    def test_an_unreadable_inventory_offers_no_boxes_at_all(self):
        html = self.html("fill_unreadable")
        self.assertIn("dunknown", html)
        self.assertNotIn("dfillbox", html)
        # the control on the same renderer: a readable one DOES draw boxes
        self.assertIn("dfillbox", self.html("fill_table"))

    def test_fill_mode_and_the_reading_card_take_turns(self):
        on, off = self.html("fill_on"), self.html("fill_off")
        # both fields say VISIBLE, so the two lines below read the same way
        self.assertTrue(on["fill"])
        self.assertFalse(on["read"], "both cards were on screen at once")
        self.assertIn("done", on["label"])
        self.assertFalse(off["fill"])
        self.assertTrue(off["read"], "toggling off left him with no card")
        self.assertIn("fill in", off["label"])

    def test_one_cell_posts_one_field_with_the_revision_it_was_drawn_against(self):
        posted = self.html("fill_cell_posted")
        self.assertEqual(posted["action"], "save")
        self.assertEqual(posted["revision"], "rev-fill")
        self.assertEqual(posted["original_id"], "acct-a")
        self.assertEqual(posted["account"],
                         {"id": "acct-a", "price_month": "$20"},
                         "a cell that carried more than its own field can "
                         "overwrite a box he never touched")

    def test_the_new_revision_is_kept_so_the_next_cell_is_not_a_self_conflict(self):
        self.assertEqual(self.html("fill_revision_after"), "rev-fill-2")
        self.assertEqual(self.html("fill_row_after"), "$20",
                         "the server's row replaces the local one, so the "
                         "header recounts off what was really written")

    def test_a_box_he_tabbed_through_without_changing_posts_nothing(self):
        self.assertEqual(self.html("fill_unchanged_posts"), 0)

    def test_the_confirmed_tick_posts_a_boolean_through_the_same_door(self):
        posted = self.html("fill_tick_posted")
        self.assertEqual(posted["account"], {"id": "acct-a", "confirmed": True})

    def test_a_refusal_stays_beside_the_row_and_does_not_reload_the_card(self):
        msg = self.html("fill_refusal_msg")
        self.assertIn("looks like it contains an API key", msg)
        self.assertNotIn("sk-not-a-place", msg,
                         "a refusal may never echo what it refused")
        self.assertEqual(self.html("fill_refusal_reloaded"), 0)

    def test_a_stale_revision_is_refused_names_the_row_and_reloads(self):
        self.assertIn("someone else changed", self.html("fill_conflict_msg"))
        self.assertIn("acct-a", self.html("fill_conflict_toast"),
                      "a conflict that does not say WHICH row leaves him "
                      "hunting for the one that did not save")
        self.assertTrue(self.html("fill_conflict_reloaded"))

    def test_enter_and_tab_move_down_the_column_and_shift_tab_moves_back_up(self):
        self.assertEqual(self.html("fill_focus_order"),
                         ["vendor/1", "vendor/2", "vendor/1"])
        self.assertEqual(self.html("fill_key_prevented"), [True, True, True],
                         "the browser's own left-to-right Tab has to be "
                         "suppressed or he walks across the row instead")

    def test_an_ordinary_keystroke_is_not_navigation(self):
        plain = self.html("fill_plain_key")
        self.assertEqual(plain["focused"], 0)
        self.assertFalse(plain["prevented"])

# ---------------------------------------------------------------------------

TRANSPORT_EXTRACT = ["refreshToken", "post", "declPost", "loadDeclared",
                     "declFormOpen", "declFormClose", "declPayload",
                     "declSave", "declRemove", "declConfirm"]

TRANSPORT_SUPPORT = r"""
/* THE SERVER, answering the way helm/web.py answers. Each arm sets REPLY; a
   REPLY with no body at all is the network/garbage case. */
let TOKEN = "t-1";
let REPLY = {status: 200, body: {ok: true}};
const SENT = [];
let PAGE_FETCHES = 0;
const fetch = (url, opts) => {
  if (!opts || opts.method !== "POST") {           // refreshToken's page read
    PAGE_FETCHES++;
    return Promise.resolve({ok: true, status: 200, text: () => Promise.resolve("")});
  }
  SENT.push({url, body: JSON.parse(opts.body)});
  const r = REPLY;
  return Promise.resolve({
    ok: r.status >= 200 && r.status < 300, status: r.status,
    json: () => r.body === undefined ? Promise.reject(new Error("not json"))
                                     : Promise.resolve(r.body)});
};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const TOASTS = [];
const toast = t => TOASTS.push(t);
let RELOADS = 0;
const renderDeclared = () => { RELOADS++; };
/* fill mode's renderer is a STUB here for the same reason renderDeclared is:
   this harness is about what the WIRE does. loadDeclared calls it, so its
   absence would take every arm in this class down with a ReferenceError —
   which is exactly what it did the first time fill mode landed. */
const renderDeclFill = () => {};
const renderAccts = () => {};
let FETCH_REPLY = null;
const j = _url => Promise.resolve(FETCH_REPLY);
let CONFIRMED = true;
const confirm = () => CONFIRMED;
const EL = {};
const el = sel => (EL[sel] = EL[sel] || {sel, innerHTML: "", textContent: "",
                                         value: "", checked: false, hidden: true,
                                         querySelectorAll: () => [],
                                         addEventListener: () => {}});
const $ = sel => el(sel);
"""

TRANSPORT_DRIVER = r"""
const REV2 = {accounts: [], bad: [], revision: "rev-NEW", totals: null,
              join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};

function openWith(revision) {
  DECL = {accounts: [], bad: [], unreadable: null, revision, totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  declFormOpen(null);
  el("#dId").value = "acct-new";
  el("#dVendor").value = "vendor-x";
  SENT.length = 0;
  RELOADS = 0;
}

const out = {};
(async () => {
  /* --- 409 conflict: the card reloads, and the NEXT save carries the NEW
         revision rather than re-sending the stale one forever --- */
  openWith("rev-stale");
  FETCH_REPLY = REV2;
  REPLY = {status: 409, body: {error: "someone else changed the accounts list while this one was open.", code: "conflict"}};
  const conflict = await declSave();
  out.conflict_code = conflict && conflict.code;
  out.conflict_reloaded = RELOADS > 0;
  out.conflict_msg = el("#dMsg").innerHTML;
  out.revision_after_conflict = DECL.revision;
  REPLY = {status: 200, body: {ok: true}};
  await declSave();
  out.second_save_revision_sent = SENT[SENT.length - 1].body.revision;

  /* --- 400 refusal: the server's sentence VERBATIM, no path, no status --- */
  openWith("rev-r");
  REPLY = {status: 400, body: {error: "notes looks like it contains an API key or token: 'sk-abc…'.", code: "refused"}};
  const refused = await declSave();
  out.refuse_returned = refused && refused.error;
  out.refuse_msg = el("#dMsg").innerHTML;
  out.refuse_form_open = !el("#declform").hidden;
  out.refuse_reloaded = RELOADS;

  /* --- a failure with NO body still reaches him as something --- */
  openWith("rev-n");
  REPLY = {status: 500};
  const broken = await declSave();
  out.no_body_error = broken && broken.error;
  out.no_body_msg = el("#dMsg").innerHTML;

  /* --- the row he is editing was removed under him --- */
  openWith("rev-gone");
  FETCH_REPLY = REV2;
  REPLY = {status: 409, body: {error: "there is no declared account 'acct-a' to change — nothing in the inventory has that id any more.", code: "gone"}};
  const gone = await declSave();
  out.gone_code = gone && gone.code;
  out.gone_reloaded = RELOADS > 0;
  out.gone_msg = el("#dMsg").innerHTML;

  /* --- a 2xx is still the parsed body --- */
  openWith("rev-ok");
  REPLY = {status: 200, body: {ok: true, revision: "rev-NEW"}};
  FETCH_REPLY = REV2;
  const good = await declSave();
  out.ok_body = good;
  out.ok_form_closed = el("#declform").hidden;

  /* --- remove and confirm ride the same transport --- */
  DECL = {accounts: [], bad: [], unreadable: null, revision: "rev-rm", totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  SENT.length = 0; RELOADS = 0; TOASTS.length = 0;
  REPLY = {status: 409, body: {error: "someone else changed the accounts list.", code: "conflict"}};
  await declRemove("acct-a");
  out.rm_conflict_reloaded = RELOADS > 0;
  out.rm_toast = TOASTS[TOASTS.length - 1];

  /* --- remove, and the removal LANDED: only the read-back failed --- */
  DECL = {accounts: [], bad: [], unreadable: null, revision: "rev-rs", totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  SENT.length = 0; RELOADS = 0; TOASTS.length = 0;
  REPLY = {status: 409, body: {error: "the account was removed, but helm could not read the accounts list back just now.", code: "stale"}};
  const rstale = await declRemove("acct-a");
  out.rm_stale_code = rstale && rstale.code;
  out.rm_stale_reloaded = RELOADS > 0;
  out.rm_stale_toast = TOASTS[TOASTS.length - 1];

  DECL = {accounts: [], bad: [], unreadable: null, revision: "rev-c", totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  SENT.length = 0;
  REPLY = {status: 200, body: {ok: true}};
  FETCH_REPLY = REV2;
  await declConfirm("acct-a");
  out.confirm_sent = SENT[SENT.length - 1].body;

  /* --- confirm, and the tick LANDED. `stale` is the server's word for "the
         write went through and the read that should have confirmed it could
         not see the row"; declSave and the fill path both read it that way.
         Painted as a failure here it tells him his tick did not take when it
         did, and skips the reload that would have shown him. --- */
  DECL = {accounts: [], bad: [], unreadable: null, revision: "rev-cs", totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  SENT.length = 0; RELOADS = 0; TOASTS.length = 0;
  FETCH_REPLY = REV2;
  REPLY = {status: 409, body: {error: "your change was saved, but the accounts list changed underneath it.", code: "stale"}};
  const cstale = await declConfirm("acct-a");
  out.confirm_stale_code = cstale && cstale.code;
  out.confirm_stale_reloaded = RELOADS > 0;
  out.confirm_stale_toast = TOASTS[TOASTS.length - 1];

  /* --- …and the OTHER branch, which is what keeps the arm above from being
         satisfied by a confirm that calls every error a success. --- */
  DECL = {accounts: [], bad: [], unreadable: null, revision: "rev-cc", totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  SENT.length = 0; RELOADS = 0; TOASTS.length = 0;
  FETCH_REPLY = REV2;
  REPLY = {status: 409, body: {error: "someone else changed the accounts list.", code: "conflict"}};
  await declConfirm("acct-a");
  out.confirm_conflict_reloaded = RELOADS > 0;
  out.confirm_conflict_toast = TOASTS[TOASTS.length - 1];

  DECL = {accounts: [], bad: [], unreadable: null, revision: "rev-cr", totals: null,
          join: {matched: {}, declared_only: [], measured_only: []}, cap: 64};
  TOASTS.length = 0;
  REPLY = {status: 400, body: {error: "that row cannot be confirmed.", code: "refused"}};
  await declConfirm("acct-a");
  out.confirm_refused_toast = TOASTS[TOASTS.length - 1];

  out.page_fetches = PAGE_FETCHES;
  console.log(JSON.stringify(out));
})();
"""


class RealTransportRuntimeTest(unittest.TestCase):
    """THE CARD'S ERROR PATH, BOUND TO THE TRANSPORT IT ACTUALLY USES.

    Everything here is lifted verbatim from the assembled page: `post` (which
    throws on any non-2xx), `declPost` (which has to get the server's own
    object back out of that throw), and the three writers that branch on it.
    The only stand-in is `fetch`, which answers exactly as helm/web.py does.

    THE DEFECT THIS EXISTS FOR: with a hand-written `declPost` in the harness,
    a mutant that replaced the catch with `() => ({})` — swallowing EVERY
    server refusal so the owner is shown nothing at all — passed the whole
    suite green."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in TRANSPORT_EXTRACT)
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-accounts-transport-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(TRANSPORT_SUPPORT + "\nlet DECL, DECL_EDIT = null;\n"
                    + "const DECL_FIELDS = [[\"#dId\", \"id\"], [\"#dVendor\", \"vendor\"], "
                      "[\"#dPlan\", \"plan\"], [\"#dCount\", \"count\"], "
                      "[\"#dPrice\", \"price_month\"], [\"#dGood\", \"good_for\"], "
                      "[\"#dNot\", \"not_for\"], [\"#dReach\", \"reach\"], "
                      "[\"#dMeasured\", \"measured_as\"], [\"#dRenews\", \"renews_on\"], "
                      "[\"#dNotes\", \"notes\"]];\n"
                    + fns + "\n" + TRANSPORT_DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def got(self, key):
        self.assertIn(key, self.out, "harness produced no %s\nstdout=%r\nstderr=%r"
                      % (key, self.proc.stdout, self.proc.stderr))
        return self.out[key]

    def test_the_transport_carries_the_servers_code_through_a_real_throw(self):
        """post() throws; the code the writers branch on survives it."""
        self.assertEqual(self.got("conflict_code"), "conflict")

    def test_a_conflict_reloads_the_card_and_the_next_save_carries_the_new_revision(self):
        """THE OWNER'S LOOP. Without the reload his next save re-sends the same
        stale revision and is refused again, forever."""
        self.assertTrue(self.got("conflict_reloaded"))
        self.assertEqual(self.got("revision_after_conflict"), "rev-NEW")
        self.assertEqual(self.got("second_save_revision_sent"), "rev-NEW")

    def test_a_refusal_reaches_him_as_the_servers_sentence_and_nothing_else(self):
        """VERBATIM means verbatim: no route, no status code, no Error: in
        front of it. He is being told what he typed wrong."""
        sentence = "notes looks like it contains an API key or token"
        self.assertIn(sentence, self.got("refuse_returned"))
        msg = self.got("refuse_msg")
        self.assertIn(sentence, msg)
        for noise in ("/api/accounts", "400", "Error"):
            self.assertNotIn(noise, msg)
        self.assertTrue(self.got("refuse_form_open"))
        self.assertEqual(self.got("refuse_reloaded"), 0)   # a refusal is not a conflict

    def test_a_failure_with_no_body_reaches_him_as_a_sentence_not_a_route(self):
        """The network, a proxy, a non-JSON answer: there is no sentence to
        show, so the rendering IS the fallback — and the fallback was
        "/api/accounts -> 500", the one developer-shaped string left on this
        card. It says what happened, that nothing changed, and keeps the
        status, which is the only part of the old string he could relay."""
        said = self.got("no_body_error")
        self.assertIn("could not reach the accounts store", said)
        self.assertIn("Nothing was changed", said)
        self.assertIn("500", said)                    # the status is a fact…
        self.assertNotIn("/api/accounts", said)       # …the route is not his
        self.assertNotIn("->", said)
        self.assertIn("could not reach", self.got("no_body_msg"))

    def test_a_row_removed_under_him_reloads_the_card_like_a_conflict(self):
        """A `gone` is a lost update with a different sentence: the row he is
        editing is not there. Without the reload the card keeps drawing a row
        that does not exist and every retry answers the same way."""
        self.assertEqual(self.got("gone_code"), "gone")
        self.assertTrue(self.got("gone_reloaded"))
        self.assertIn("no declared account", self.got("gone_msg"))

    def test_a_success_is_the_parsed_body_and_closes_the_form(self):
        """THE POSITIVE CONTROL for every refusal arm above: the same lifted
        transport, against a 200, resolves with the server's object."""
        self.assertEqual(self.got("ok_body"), {"ok": True, "revision": "rev-NEW"})
        self.assertTrue(self.got("ok_form_closed"))

    def test_remove_reloads_on_a_conflict_and_shows_the_sentence(self):
        self.assertTrue(self.got("rm_conflict_reloaded"))
        self.assertIn("someone else changed", self.got("rm_toast"))

    def test_a_remove_that_landed_is_not_painted_as_a_failure(self):
        """`stale` means the write landed, for a remove as for a save and a
        confirm. The conflict arm above is the control: the same branch still
        wears a cross for an answer that did NOT land."""
        self.assertEqual(self.got("rm_stale_code"), "stale")
        self.assertTrue(self.got("rm_stale_reloaded"),
                        "the card kept drawing a row that is gone")
        toast = self.got("rm_stale_toast")
        self.assertTrue(toast.startswith("\u2713 removed"), toast)
        self.assertTrue(self.got("rm_toast").startswith("\u2717"),
                        self.got("rm_toast"))

    def test_confirm_posts_the_id_and_the_flag_and_nothing_else(self):
        """A confirm that carried the form's fields would be an edit, and an
        edit driven from a form narrower than the schema is what destroys
        renews_on."""
        sent = self.got("confirm_sent")
        self.assertEqual(sent["action"], "save")
        self.assertEqual(sent["account"], {"id": "acct-a", "confirmed": True})
        self.assertEqual(sent["revision"], "rev-c")

    def test_a_confirm_that_landed_is_not_painted_as_a_failure(self):
        """`stale` MEANS THE WRITE LANDED. `declSave` and the fill path both
        read it that way and close like a success; `declConfirm` alone wore a ✗
        and skipped the reload — one branch out of step with its two siblings.
        He ticked the box, helm told him it had failed, and the card kept
        drawing the pre-tick row, so his next move is to tick it again."""
        self.assertEqual(self.got("confirm_stale_code"), "stale")
        toast = self.got("confirm_stale_toast")
        self.assertTrue(toast.startswith("\u2713"),
                        "a confirm that LANDED reached him as %r" % toast)
        self.assertIn("changed underneath it", toast,
                      "the server's own sentence is what says what happened")
        self.assertTrue(self.got("confirm_stale_reloaded"),
                        "the write landed and the card was never re-read, so "
                        "the screen still shows the row untick ed")

    def test_a_confirm_that_did_not_land_is_still_a_failure(self):
        """THE SIBLING BRANCH, and without it the arm above is satisfied by a
        `declConfirm` that calls every error a success. A `conflict` wrote
        NOTHING: it wears the ✗ and reloads; a `refused` wrote nothing either
        and does not even reload."""
        conflict = self.got("confirm_conflict_toast")
        self.assertTrue(conflict.startswith("\u2717"), conflict)
        self.assertTrue(self.got("confirm_conflict_reloaded"))
        self.assertTrue(self.got("confirm_refused_toast").startswith("\u2717"))

    def test_no_arm_here_touched_the_bearer_refresh_path(self):  # noqa: VACUOUS_ASSERTION — ZERO page fetches is the product law for this file: none of these arms is a 403, so a refresh would mean an arm ran something other than what it says. The positive control on the same stub is test_a_success_is_the_parsed_body_and_closes_the_form, whose 200 proves the fetch stub answered at all
        """A page fetch would mean a 403 arm ran by accident and every result
        above is about something other than what its name says."""
        self.assertEqual(self.got("page_fetches"), 0)


class CreateTransitionEndToEndTest(AccountsBase):
    """THE ROUND-TRIP, WITH NOTHING STUBBED BETWEEN THE TWO ENDS.

    He types a price on an account helm measures and nobody has described. The
    write goes through the real POST door; the answer it gives back is fed,
    VERBATIM, to the shipped `quotaRows` under real node. That is the whole
    transition his ruling is about, and until this arm existed nothing ran it:
    the client fixture carried a `subscription` field that the real response
    does not have, so the join it rehearsed was one the server never produces.
    A green arm over a forgiving fixture is how the defect shipped."""

    NAME = "quota-row-9"

    class Provider:
        """THE MACHINE'S CREDENTIALS, and nothing else. Everything between this
        and the browser is shipped code: the merged row, the subscription key,
        the handle resolution and the two doors."""

        def __init__(self, name, email):
            self.name, self.email = name, email

        def accounts(self):
            return [{"name": self.name, "provider": "codex", "home": "/h",
                     "email": self.email, "tier": "Big Plan",
                     "active": False, "usable": True}]

        def cred_state(self):
            return [{"account": self.name, "cred_state": "ok",
                     "headroom_pct": 50.0, "tier": "Big Plan"}]

        def windows(self):
            return []

    def setUp(self):
        super().setUp()
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        from unittest import mock
        from helm import providers
        provider = self.provider = self.Provider(
            self.NAME, "nine" + "@" + "v.test")
        for target, attr in ((web, "_provider"),
                             (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: provider)
            patch.start()
            self.addCleanup(patch.stop)
        self.drop_cache()
        self.addCleanup(self.drop_cache)
        # THE ROW THE PAGE IS REALLY GIVEN, lifted out of the shipped door
        # rather than built here. A hand-made cred is exactly how this file
        # came to carry a `subscription` field the server never sent, and the
        # arm rehearsed a join production could not produce.
        self.measured = web._measured_accounts()
        self.cred = next(r for r in web.get_creds() if r["name"] == self.NAME)

    @staticmethod
    def drop_cache():
        with web._qlock:
            web._qstate.pop("creds", None)

    def compose(self, **cells):
        """What the fill screen posts for a cell on a row with no record: the
        fields helm has MEASURED, plus the one he typed, plus the handle. Built
        from the page's own `fillComposed` so this cannot drift from it."""
        composed = self.run_js(
            "console.log(JSON.stringify(fillComposed({cred: %s})));"
            % json.dumps(self.cred), ["fillComposed"])
        payload = dict(composed, id="vendor-x-quota-row-9",
                       measured_key=self.cred["measured_key"])
        payload.update(cells)
        return {"action": "save", "account": payload}

    def run_js(self, driver, names, decl=None):
        src = web_ui_loader.read_text()
        fns = "\n".join(_extract_fn(src, n) for n in names)
        head = ("const esc = s => String(s ?? '');\n"
                "const CREDS = %s;\nlet DECL = {accounts: %s};\n"
                % (json.dumps([self.cred]), json.dumps(decl or [])))
        tmp = tempfile.mkdtemp(prefix="helm-create-e2e-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + fns + "\n" + driver)
        proc = subprocess.run([shutil.which("node"), path],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "null")

    ROWS_JS = """
const rows = quotaRows();
console.log(JSON.stringify({
  rows: rows.length,
  decl_on_measured_row: rows[0].decl ? rows[0].decl.id : null,
  spare: (rows[0].spare || []).map(a => a.id),
  fill_keys: fillRows().map(r => r.key),
  fill_ids: fillRows().map(r => r.id),
}));"""
    ROWS_FNS = ["quotaRows", "quotaAliasOrder", "quotaDeclOrder", "fillRows",
                "fillComposed", "fillNeedsDescribe"]

    def test_the_created_record_lands_on_the_row_he_typed_in(self):
        """THE WHOLE CLAIM. Without the save answering through the same
        producer as the GET, the record comes back unkeyed, the page groups it
        by name while the measured row groups by subscription, and it splits
        off as a SECOND row — with the row he typed in still blank."""
        out, status = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertEqual(status, 200, out)
        seen = self.run_js(self.ROWS_JS, self.ROWS_FNS, decl=[out["account"]])
        self.assertEqual(seen["rows"], 1, seen)
        self.assertEqual(seen["decl_on_measured_row"], "vendor-x-quota-row-9")
        self.assertEqual(seen["spare"], [])
        self.assertEqual(seen["fill_ids"], ["vendor-x-quota-row-9"])

    def unreadable_world(self):
        """The provider stops answering WHO IS THERE and the cache lapses. The
        page is not told: it polls the measured rows on its own clock, so the
        rows it holds (`self.cred`) are still the keyed ones from the last
        read that worked."""
        def refuses():
            raise OSError("the measured world cannot be read")
        self.provider.accounts = refuses
        self.drop_cache()

    def test_a_record_stays_on_its_row_while_the_world_cannot_be_read(self):  # noqa: VACUOUS_ASSERTION — the loop is the point (the claim is over BOTH ways the page receives the record) and its control is unconditional and before it, on the same observables: with the world readable the same composition answers one row carrying this record
        """THE KEY IS WITHHELD, AND THE ROW MUST NOT NOTICE. With the measured
        world unreadable the server does not know which subscription a record
        is on and says so by answering no key. The page still holds the keyed
        measured rows it polled earlier, so a record that fell back to its
        NAME while its measured row kept its KEY would leave the row he ticked
        and draw as a second row — and the next cell typed on the emptied row
        would create a second record for one bill. Both sides fall back
        together: the record joins through the measured row its `measured_as`
        names, and takes that row's key."""
        first, status = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertEqual(status, 200, first)
        rid = first["account"]["id"]
        # THE CONTROL, unconditional and on the same observables: with the
        # world readable the record is keyed and sits on the measured row
        self.assertEqual(first["account"]["subscription"],
                         self.cred["subscription"])
        healthy = self.run_js(self.ROWS_JS, self.ROWS_FNS,
                              decl=[first["account"]])
        self.assertEqual((healthy["rows"], healthy["decl_on_measured_row"]),
                         (1, rid), healthy)

        self.unreadable_world()
        out, status = web._api_accounts_post(
            {"action": "save", "account": {"id": rid, "confirmed": True}})
        self.assertEqual(status, 200, out)
        # THE STATE UNDER TEST WAS REACHED: the tick landed and the key really
        # was withheld, so what follows is about a keyless record and not
        # about a world that quietly read after all
        self.assertTrue(out["account"]["confirmed"])
        self.assertEqual((out["account"]["subscription"],
                          out["account"].get("subscription_unreadable")),
                         (None, True), out["account"])
        got = web._api_accounts()
        self.assertTrue(got["measured_unavailable"], got)
        # BOTH WAYS THE PAGE RECEIVES THE RECORD: the save's own answer, which
        # the fill screen splices into its list, and the list reloaded whole
        for door, decl in (("save", [out["account"]]),
                           ("reload", got["accounts"])):
            with self.subTest(door=door):
                seen = self.run_js(self.ROWS_JS, self.ROWS_FNS, decl=decl)
                self.assertEqual(seen["rows"], 1,
                                 "the record left the row he ticked and drew "
                                 "as a second row: %r" % (seen,))
                self.assertEqual(seen["decl_on_measured_row"], rid, seen)
                self.assertEqual(seen["fill_ids"], [rid],
                                 "the row he was editing reads as having no "
                                 "record, so the next cell would CREATE one")
                self.assertEqual(seen["fill_keys"], healthy["fill_keys"],
                                 "the key his cells carry moved under him")

    def test_the_answer_carries_the_key_the_page_groups_on(self):
        """Stated on the response itself, because that is the field whose
        absence caused it — and against the measured row's own key, so this is
        an agreement between the two sides rather than a shape check."""
        out, _status = web._api_accounts_post(self.compose(price_month="$9"))
        # the key is a REAL one, not two Nones agreeing — the unconditional
        # positive control, and the exact failure being closed was a None here
        self.assertTrue(self.cred["subscription"].startswith("s-"))
        self.assertTrue(out["account"]["subscription"])
        self.assertEqual(out["account"]["subscription"],
                         self.cred["subscription"])
        # THE CONTROL: the GET produces the identical record, field for field.
        # Two doors, one shape, which is the law this closes.
        got = accounts.client_rows(measured=self.measured)["accounts"]
        self.assertEqual(next(r for r in got if r["id"] == out["account"]["id"]),
                         out["account"])

    def test_the_key_the_cells_carry_does_not_move_when_the_record_appears(self):
        """The cells hold their row's key from the moment the table is drawn.
        If creating a record changed it, every cell after the first would look
        up a key that no longer existed."""
        before = self.run_js(self.ROWS_JS, self.ROWS_FNS, decl=[])
        out, _status = web._api_accounts_post(self.compose(price_month="$9"))
        after = self.run_js(self.ROWS_JS, self.ROWS_FNS, decl=[out["account"]])
        self.assertEqual(before["fill_keys"], after["fill_keys"])
        # …and the row REALLY changed state across that pair, so the equality
        # above is about the key rather than about nothing happening.
        self.assertEqual(before["fill_ids"], [None])
        self.assertEqual(after["fill_ids"], ["vendor-x-quota-row-9"])

    def test_the_second_cell_edits_that_record_instead_of_making_another(self):
        first, _s = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertEqual(_s, 200, first)
        # the page now posts ONE field with the id it was given back
        out, status = web._api_accounts_post(
            {"action": "save", "revision": accounts.read()["revision"],
             "original_id": first["account"]["id"],
             "account": {"id": first["account"]["id"], "notes": "renews 3rd"}})
        self.assertEqual(status, 200, out)
        self.assertEqual([a["id"] for a in accounts.read()["accounts"]],
                         ["vendor-x-quota-row-9"])
        self.assertEqual(out["account"]["notes"], "renews 3rd")
        self.assertEqual(out["account"]["price_month"], "$9")

    def test_the_compose_sends_a_count_because_the_writer_requires_one(self):
        """Measured against the writer rather than read off it: without a count
        every create is refused "count is required — how many of this exact
        plan do you have?", which is a question about a box that is not on this
        screen, asked in answer to a price he just typed."""
        payload = self.compose(price_month="$9")
        self.assertEqual(payload["account"]["count"], 1)
        # THE CONTROL that this is the writer's rule and not a preference:
        # take the count back out and the same door refuses.
        payload["account"].pop("count")
        out, status = web._api_accounts_post(payload)
        self.assertEqual(status, 400, out)
        self.assertIn("count is required", out["error"])

    def test_a_write_the_re_read_cannot_see_is_reported_never_papered_over(self):  # noqa: VACUOUS_ASSERTION — `assertNotIn("account", out)` is the absence, and its unconditional positive control is the unpatched call at the end of the same arm, which asserts a 200 carrying a keyed record
        """THE MISS PATH, which nothing exercised until this arm.

        The write returned, so the record is on disk; a re-read that cannot see
        it means the file moved underneath — another writer removed the row, or
        it stopped parsing and `read` fail-opened to an empty inventory, which
        is right for a page that must still render and wrong as the answer to a
        write. That is a contradiction, not a condition to default around.

        DEFAULTING TO THE WRITTEN ROW HERE would hand back the unkeyed record
        — the exact shape the whole one-producer fix exists to eliminate — on
        the one path no test reaches. He is told instead, and the page
        reloads."""
        from unittest import mock
        real = accounts.client_rows

        def blind(*a, **k):
            """the inventory read answers without the row — a removal landing
            between the write and the read, or a file that stopped parsing."""
            view = real(*a, **k)
            return dict(view, accounts=[r for r in view["accounts"]
                                        if r["id"] != "vendor-x-quota-row-9"])

        with mock.patch.object(accounts, "client_rows", blind):
            out, status = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertEqual(status, 409, out)
        self.assertEqual(out["code"], "stale")
        self.assertIn("was saved", out["error"])
        # …and NOTHING that looks like a record comes back, which is the half
        # that matters: an unkeyed one would re-split the row he typed into.
        self.assertNotIn("account", out)
        # THE CONTROL on the same call, unpatched: the record IS found and IS
        # keyed, so the refusal above is about the blinded read and not about a
        # door that refuses every save.
        out, status = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertEqual(status, 200, out)
        self.assertTrue(out["account"]["subscription"])

    def test_the_write_still_landed_when_the_re_read_could_not_see_it(self):
        """The record is on disk either way — the refusal is about the SCREEN
        being out of step, not about the write. Telling him it failed would be
        as wrong as handing back the unkeyed shape."""
        from unittest import mock
        real = accounts.client_rows
        with mock.patch.object(accounts, "client_rows",
                               lambda *a, **k: dict(real(*a, **k), accounts=[])):
            out, _status = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertEqual(out["code"], "stale")
        stored = {a["id"]: a for a in accounts.read()["accounts"]}
        self.assertIn("vendor-x-quota-row-9", stored)
        self.assertEqual(stored["vendor-x-quota-row-9"]["price_month"], "$9")

    def test_the_compose_invents_no_sentence_about_what_it_is_for(self):  # noqa: VACUOUS_ASSERTION — the absence IS the contract (no reading can supply good_for/not_for, so composing one would put words in his mouth); its unconditional control is the three assertEquals on the same payload above, which prove the dict is populated
        payload = self.compose(price_month="$9")["account"]
        # the PROVIDER's own words, read off the row the page was given — and
        # the unconditional positive control on the same dict: it is populated,
        # so the two absences below are about those two fields
        self.assertEqual(payload["vendor"], self.cred["provider"])
        self.assertEqual(payload["plan"], self.cred["tier"])
        self.assertEqual(payload["reach"], self.cred["provider"])
        for invented in ("good_for", "not_for"):
            self.assertNotIn(invented, payload)
        # …and the row it creates SAYS nobody has described it
        out, _s = web._api_accounts_post(self.compose(price_month="$9"))
        self.assertTrue(out["account"]["needs_describe"])


class OneSnapshotOfTheMeasuredWorldTest(AccountsBase):
    """TWO CACHES WITH THE SAME TTL ARE NOT ONE SNAPSHOT.

    The row the owner looks at is rendered from one acquisition of the
    providers; the subscription his declaration is keyed by was derived from
    another. Equal TTLs do not make equal epochs — each entry ages from its own
    first build — so the two can straddle a change.

    AND THE CHANGE IS AN ORDINARY OPERATION ON THIS FLEET. A codex row is named
    for its credential HOME and its login is read out of the token inside it, so
    re-logging that home into another account keeps the name and changes the
    identity. Rotating a spent credential does exactly that. Across the two
    epochs the page then renders subscription A while the save keys the record
    to subscription B, the record splits off the row it was typed on, and
    HIGH-1 is back by a second route.

    THE PROVIDER IS THE ONLY THING STUBBED HERE, and that is the point. Patching
    the two accessors to one object makes the two sources identical by
    construction, so such an arm cannot observe the defect it exists to guard.
    One provider, asked twice, answering what the disk says each time — and the
    two production surfaces are compared TO EACH OTHER rather than to a value
    written down here, because an arm that checks one side against an expected
    constant passes while the two sides disagree."""

    NAME = "home-one"
    IDENTITIES = ["first" + "@" + "v.test", "second" + "@" + "v.test"]

    class Provider:
        """One provider, whose account list changes under it between reads —
        which is all a re-login is."""

        def __init__(self, name, identities):
            self.name, self.identities, self.reads = name, list(identities), 0

        def accounts(self):
            identity = self.identities[min(self.reads, len(self.identities) - 1)]
            self.reads += 1
            return [{"name": self.name, "provider": "codex", "home": "/h",
                     "email": identity, "tier": "Big Plan", "active": False,
                     "usable": True}]

        def cred_state(self):
            return [{"account": self.name, "cred_state": "ok",
                     "headroom_pct": 50.0, "tier": "Big Plan"}]

        def windows(self):
            return [{"account": self.name, "windows_left": 1.0,
                     "windows_per_week": 2.0, "verdict": "ok"}]

    def setUp(self):
        super().setUp()
        from unittest import mock
        from helm import providers
        self.provider = self.Provider(self.NAME, self.IDENTITIES)
        for target, attr in ((web, "_provider"), (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: self.provider)
            patch.start()
            self.addCleanup(patch.stop)
        self.clear_caches()
        self.addCleanup(self.clear_caches)

    def clear_caches(self):
        for key in ("creds", "measured-accounts", "provider-accounts"):
            with web._qlock:
                web._qstate.pop(key, None)

    def test_both_doors_read_one_acquisition_of_the_measured_world(self):
        """THE FALSIFIER. The row is rendered first, the save keyed second, and
        the identity moves in between — so the two keys agree only if one
        acquisition served both."""
        rendered = web.get_creds()
        self.assertEqual(len(rendered), 1, rendered)
        self.assertTrue(rendered[0]["subscription"], "the row has no key at all")

        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1, "price_month": "$9",
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 200, out)

        self.assertEqual(
            out["account"]["subscription"], rendered[0]["subscription"],
            "the page rendered one subscription for this account and the save "
            "keyed the record to another, so the record he just created will "
            "not be on the row he typed it into")

    def test_the_provider_really_moved_under_the_two_reads(self):
        """THE CONTROL, and without it the arm above passes on a fixture that
        never changed. The same NAME carries a different login on the second
        acquisition, which is what a re-login looks like from here."""
        first = self.provider.accounts()[0]
        second = self.provider.accounts()[0]
        # both reads returned a real row, so the comparisons below are between
        # two measurements rather than between two absences
        self.assertTrue(first["email"] and second["email"])
        self.assertEqual(first["name"], second["name"])
        self.assertNotEqual(first["email"], second["email"])
        self.assertNotEqual(
            accounts.subscription_key(accounts.measured_identity(first)),
            accounts.subscription_key(accounts.measured_identity(second)),
            "the fixture's two identities key the same, so nothing could drift")

    def test_dropping_the_rows_drops_the_world_they_were_read_from(self):
        """A SEPARATE CACHE ENTRY IS A DEPENDENCY NOBODY CAN SEE. Keyed apart,
        busting the rows rebuilds them against a snapshot that was not busted —
        so a caller who asked for fresh rows gets new numbers over an old world,
        which is the same drift one epoch removes, only quieter. Anything that
        drops the rows must drop the world they came from."""
        first = web.get_creds()[0]["subscription"]
        with web._qlock:                      # exactly what a caller busting
            web._qstate.pop("creds", None)    # the rows does, and no more
        second = web.get_creds()[0]["subscription"]
        self.assertNotEqual(
            first, second,
            "the rows were rebuilt but the identities came from the entry "
            "that was not dropped, so a re-read cannot see a re-login")
        # …and the doors still agree AFTER that re-read, which is the property
        # the drop must not break.
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["account"]["subscription"], second)

    def test_one_acquisition_serves_both_doors_rather_than_two_that_agree(self):
        """Counted at the PROVIDER. Equal answers are not evidence of one read
        — two reads a moment apart usually agree — so this asks how many times
        the world was actually consulted."""
        self.provider.reads = 0
        web.get_creds()
        web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1,
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(self.provider.reads, 1,
                         "the account list was acquired %d times for one page "
                         "and one save; two acquisitions can straddle a change"
                         % self.provider.reads)


class UnreadableEnrichmentIsNotAnEmptyWorldTest(AccountsBase):
    """AN ENRICHMENT THAT CANNOT BE READ MAY NOT DISCARD AN ACQUISITION THAT
    SUCCEEDED, and it may not answer with the value that means "nothing there".

    THE DEFECT, measured on a live probe: the quota entry acquired the account
    list, then read the credential states and the windows inside the SAME
    try. A `ProviderError` from either of those two — which is what an expired
    credential looks like from here, an ordinary event on this fleet — returned
    `{"accounts": [], "rows": []}` and threw away an account list that had
    already been read successfully.

    AND THE COST LANDED ON THE ONE THING THIS LANE EXISTS TO FIX. The declared
    record claims its subscription through `measured_as`, and
    `accounts.declared_identity` resolves that name against the measured rows to
    recover the login behind it. With the measured world emptied it cannot, so
    it falls back to keying on the vendor plus the raw name — a DIFFERENT
    subscription key for the same row. The owner ticks "confirmed" on a row he
    is looking at, the save comes back keyed to a subscription the table has
    never heard of, and the record splits off the row he was editing. That is
    the "make new" his 13:15 ruling forbids, re-entered through the READ.

    THREE STATES, AND THEY STAY THREE. No provider at all is an EMPTY world and
    still fail-open, because sessions and resume do not need quota. Windows that
    answer NOTHING is a measured world whose rows carry no numbers. Windows that
    could not be READ is a third thing, and the card has to say so — "unknown,
    not absent" — because a page that draws the second when it means the third
    is telling him a confident wrong sentence about his own accounts."""

    NAME = "home-one"

    class Provider:
        """ONE provider, whose account list always reads and whose enrichment
        can fail, answer nothing, or answer. Production lets those three drift
        apart; a stub that could not tell them apart is the shape that let four
        defects ride through a green suite on this lane."""

        def __init__(self, name):
            self.name, self.mode, self.account_reads = name, "ok", 0

        def accounts(self):
            self.account_reads += 1
            return [{"name": self.name, "provider": "codex", "home": "/h",
                     "email": "one" + "@" + "v.test", "tier": "Big Plan",
                     "active": False, "usable": True}]

        def _enriched(self, rows):
            from helm.providers import ProviderError
            if self.mode == "unreadable":
                raise ProviderError("the credential for that home expired")
            return [] if self.mode == "empty" else rows

        def cred_state(self):
            return self._enriched([{"account": self.name, "cred_state": "ok",
                                    "headroom_pct": 50.0, "tier": "Big Plan"}])

        def windows(self):
            return self._enriched([{"account": self.name, "windows_left": 1.0,
                                    "windows_per_week": 2.0, "verdict": "ok"}])

    def setUp(self):
        super().setUp()
        from unittest import mock
        from helm import providers
        self.provider = self.Provider(self.NAME)
        for target, attr in ((web, "_provider"), (providers, "default_provider")):
            patch = mock.patch.object(target, attr, lambda: self.provider)
            patch.start()
            self.addCleanup(patch.stop)
        self.clear_caches()
        self.addCleanup(self.clear_caches)

    def clear_caches(self):
        for key in ("creds", "measured-accounts", "provider-accounts"):
            with web._qlock:
                web._qstate.pop(key, None)

    def become(self, mode):
        """What the TTL lapsing on an expired credential does: the world the
        next build reads has changed, and the entry is gone."""
        self.provider.mode = mode
        self.clear_caches()

    def declare(self):
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": "codex-home-one", "vendor": "codex", "plan": "Big Plan",
            "reach": "codex", "count": 1, "price_month": "$9",
            "measured_key": accounts.measured_key(self.NAME)}})
        self.assertEqual(status, 200, out)
        return out["account"]

    def test_the_fixture_fails_answers_empty_and_answers_on_demand(self):
        """THE CONTROL. Every arm below is about which of three worlds the door
        reports, so the fixture has to actually produce all three — and its
        account list has to READ in all three, or nothing below is about
        enrichment at all."""
        from helm.providers import ProviderError
        self.provider.mode = "unreadable"
        with self.assertRaises(ProviderError):
            self.provider.cred_state()
        with self.assertRaises(ProviderError):
            self.provider.windows()
        self.provider.mode = "empty"
        self.assertEqual(self.provider.cred_state(), [])
        self.provider.mode = "ok"
        self.assertTrue(self.provider.cred_state())
        for mode in ("ok", "empty", "unreadable"):
            self.provider.mode = mode
            self.assertTrue(self.provider.accounts(),
                            "the acquisition failed in %r, so an empty measured "
                            "world there would prove nothing about enrichment"
                            % mode)

    def test_an_unreadable_enrichment_keeps_the_accounts_already_acquired(self):
        """THE ACQUISITION SUCCEEDED. Whatever happens to the numbers, the
        identities are known — and every key on this page is derived from them.

        Asked through `_measured_accounts`, which is the door the two account
        handlers use and the one the facade registers; the entry behind it is
        web_quota's own business and the pinned web surface stays pinned."""
        self.assertTrue(web._measured_accounts(), "the healthy read is empty")
        self.become("unreadable")
        self.assertTrue(
            web._measured_accounts(),
            "a ProviderError reading the credential states or the windows "
            "discarded an account list that had already been read")

    def test_unreadable_and_empty_do_not_come_back_as_the_same_value(self):
        """THE RULE, at the two doors he can see it through: "could not read"
        and "nothing there" must never share a value — and the identity world
        must survive BOTH, which is what keeps his record on the row he typed
        it into either way."""
        self.become("unreadable")
        unreadable = (web._measured_accounts(), web.get_creds(),
                      web._api_accounts()["measured_unavailable"])
        self.become("empty")
        empty = (web._measured_accounts(), web.get_creds(),
                 web._api_accounts()["measured_unavailable"])
        self.assertTrue(unreadable[0], "identity lost when unreadable")
        self.assertTrue(empty[0], "identity lost when empty")
        self.assertNotEqual(
            unreadable[1:], empty[1:],
            "an enrichment helm could not read and an enrichment that "
            "answered nothing came back as the same two answers")

    def test_a_failed_enrichment_does_not_split_the_row_he_is_confirming(self):
        """THE OWNER-VISIBLE FALSIFIER, and the probe that found this. The row
        is declared while the credential reads; the credential then expires; he
        ticks confirmed. The record must come back on the SAME subscription, or
        it has silently become a second row for an account that already has
        one — the exact complaint this lane exists to end."""
        first = self.declare()
        key = first["subscription"]
        self.assertTrue(key, "the declared row was keyed to no subscription")
        self.become("unreadable")
        out, status = web._api_accounts_post({"action": "save", "account": {
            "id": first["id"], "confirmed": True}})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["account"]["confirmed"], "the tick did not land")
        self.assertEqual(
            out["account"]["subscription"], key,
            "the credential expired between the render and the tick, so the "
            "save keyed his record to a different subscription than the row he "
            "ticked it on: the declaration splits again")

    def test_the_card_calls_that_measurement_unknown_rather_than_absent(self):
        """WHAT HE READS. `measured_unavailable` is the sentence the card turns
        into "this row's measurement is UNKNOWN, not absent"; an enrichment
        that answered nothing is not that, and must not wear it."""
        self.declare()
        self.become("unreadable")
        said = web._api_accounts()["measured_unavailable"]
        self.assertTrue(said, "an unreadable measurement drew as an absent one")
        self.assertIn("ProviderError", said)
        self.assertNotIn(
            "expired", said,
            "an exception's own text can name a credential home or a path, and "
            "this string renders on his page")
        self.become("empty")
        self.assertIsNone(
            web._api_accounts()["measured_unavailable"],
            "an enrichment that answered, with nothing in it, told him helm "
            "could not read his accounts")

    def test_a_row_whose_numbers_are_unreadable_says_so_on_the_wire(self):
        """The TABLE's half. A row filtered out for having no data is the page
        drawing "nothing there" for "could not read" — the same conflation, one
        surface further out — so the row has to carry the distinction."""
        self.become("unreadable")
        rows = web.get_creds()
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0].get("quota_unreadable"))
        self.become("empty")
        rows = web.get_creds()
        self.assertEqual(len(rows), 1, rows)
        self.assertFalse(rows[0].get("quota_unreadable"),
                         "a window that reported nothing was marked unreadable")


class OwnerSurfaceLawTest(unittest.TestCase):
    """NOTHING THE OWNER COULD READ MAY GO.

    This began as ADDITIVE ONLY — a lane may add a card, never remove or merge
    one. The owner then ruled the merge himself: "i dont understand why the
    accounts we pay for [aren't handled] by making it editable and adding
    fields intelligently, to compose instead of make new". So the law is no
    longer about the boxes, which he asked us to collapse; it is about the
    FACTS, which he did not. Every number and every affordance the two account
    panels carried is still on the page, now as cells of one row."""

    def setUp(self):
        self.src = web_ui_loader.read_text()

    def test_every_pre_existing_quota_surface_is_still_on_the_page(self):
        self.assertIn('id="acctdeclared"', self.src)   # the page IS assembled
        for marker in ('id="qonecard"', 'id="homescard"', 'id="burncard"',
                       'id="chartcard"', 'id="qonebody"', 'id="qonehead"',
                       'id="homerows"', 'id="homeform"', 'id="attn"'):
            self.assertIn(marker, self.src, marker)

    def test_the_declared_surface_sits_above_the_folds_he_could_not_get_past(self):  # noqa: VACUOUS_ASSERTION — every claim here is a POSITION, and the two assertIn calls above them are the unconditional control that both markers are on the assembled page at all
        """His complaint was that he could not FIND it: a collapsed one-line row
        at the bottom of a 2400px page. It now sits directly under the table it
        annotates and above the diagnostic folds, and it opens by default."""
        # the page IS assembled and DOES carry both — the unconditional control
        # under every ordering claim below, since index() on a missing marker
        # would raise rather than report a position.
        self.assertIn('id="qonecard"', self.src)
        self.assertIn('id="acctdeclared"', self.src)
        i_table = self.src.index('id="qonecard"')
        i_new = self.src.index('id="acctdeclared"')
        for below in ('id="burncard"', 'id="homescard"'):
            self.assertLess(i_new, self.src.index(below), below)
        self.assertLess(i_table, i_new)
        self.assertIn('id="acctdeclared" open', self.src)
        # the burn-flag fold is no longer one of them: it moved onto the burn
        # board (task/2975), ahead of this tab, and the quota tab kept its
        # account tables
        self.assertLess(self.src.index('id="flagcard"'),
                        self.src.index('id="view-quota"'))

    def test_the_order_question_is_answered_by_the_tiers_not_by_a_column_sort(self):
        """THE ONE AFFORDANCE THIS PASS REMOVED, pinned here with its reason so
        it is a decision on the record rather than a silent precedent.

        The old table's headers sorted by account name, by windows-left and by
        each window's remaining %. His item 2 was a QUESTION — "how are these
        ordered? most relevant should be top, kicked/awaiting reset should be
        bottom, no?" — and every one of those sorts was him answering it by
        hand, once per visit. The default order answers it on the page's face:
        usable first, within that a live seat then most headroom then nearest
        reset, walled last by soonest reset, each tier named in a band. There
        was never a sort by spend, so nothing he could rank by is gone.

        A PER-COLUMN SORT IS NOT HOW THIS TABLE ANSWERS THAT QUESTION. The rows
        are grouped by family and banded by tier; a global sort would have to
        destroy that grouping or hide inside it. Restoring one means replacing
        the bands, not adding a handler — and the next person reading this
        should know that before they try."""
        self.assertNotIn('data-s="win"', self.src)
        self.assertNotIn('<th data-s="account"', self.src)
        # THE CONTROL, and the substance of the claim: what the sorts ranked by
        # is what the default order now ranks by, in the shipped sort.
        order = self.src.split("function acctDefaultSort")[1][:900]
        for ranked in ("acctTier", "headroom", "acctResetAt", "live"):
            self.assertIn(ranked, order, ranked)
        # …and the tiers are NAMED on the page, which is the half a sort never
        # gave him: a table that is ordered but silent leaves him asking again.
        self.assertIn("const ATIER_SAID", self.src)
        self.assertIn("function acctTierRow", self.src)
        # the filter the chips gave him is a fold on the family header
        self.assertIn("QFOLD", self.src)

    def test_every_number_the_old_account_table_carried_still_renders(self):
        """The columns were merged, not dropped: `windows left` and its verdict
        moved INTO the reset cell, because when the room comes back and how much
        of it will be wasted are one question."""
        table = self.src.split("function quotaMeasuredCells")[1][:2000] \
            + self.src.split("function quotaResetCell")[1][:1200] \
            + self.src.split("function quotaIdCell")[1][:1600]
        for fact in ("windows_left", "windows_per_week", "windows_verdict",
                     "name_lies", "duplicate_homes", "stfix", "resets_at_ms"):
            self.assertIn(fact, table, fact)
        self.assertIn("function quotaHeadHTML", self.src)

    def test_the_new_card_is_in_the_assembled_page_and_in_the_manifest(self):
        self.assertIn('id="acctdeclared"', self.src)
        self.assertIn("function declRowHTML", self.src)
        manifest = os.path.join(os.path.dirname(web_ui_loader.__file__),
                                "web_ui", "manifest.txt")
        with open(manifest, encoding="utf-8") as f:
            listed = f.read()
        self.assertIn("views/10-quota.html.part", listed)
        self.assertIn("scripts/20-quota.js.part", listed)

    def test_the_fill_screen_is_assembled_after_the_card_it_edits(self):
        """A part the manifest does not list is a file nothing serves, and
        this one holds every box the owner types into."""
        manifest = os.path.join(os.path.dirname(web_ui_loader.__file__),
                                "web_ui", "manifest.txt")
        with open(manifest, encoding="utf-8") as f:
            listed = f.read()
        self.assertIn("scripts/21-quotafill.js.part", listed)
        self.assertLess(listed.index("scripts/20-quota.js.part"),
                        listed.index("scripts/21-quotafill.js.part"),
                        "fill mode calls the card's own declPost and DECL, so "
                        "it has to be assembled after them")
        for marker in ('id="declfill"', 'id="dFill"', "function renderDeclFill",
                       "function declFillSave", "const FILL_COLS"):
            self.assertIn(marker, self.src, marker)

    def test_the_three_new_fields_have_boxes_on_the_add_form_too(self):
        """Fill mode edits rows that EXIST; the form is still the only way to
        add one, so a field it cannot carry is a field a new row never gets."""
        for marker in ('id="dBilling"', 'id="dAllowance"', 'id="dKeyWhere"'):
            self.assertIn(marker, self.src, marker)
        for key in ('"billing"', '"allowance"', '"key_where"'):
            self.assertIn(key, self.src.split("const DECL_FIELDS")[1][:600], key)


if __name__ == "__main__":
    unittest.main()
