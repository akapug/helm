#!/usr/bin/env python3
"""`or-free`: OpenRouter's free-models router as ONE model class.

THE OWNER RULING, verbatim, answering helm's refusal of `openrouter/free`: "oh
i think we can trust none of those models will be the same as opus 5.5 or any
of our standard models, just create a meta-model class model called OR-free or
something". `seat_catalog.OR_FREE_RULING` carries its date, and the refusal it
retired in the words that refusal used.

The refusal it overturns was agent-authored: the router picks a free model per
call, so the reviewing family was "unknowable" and a review by it "silently
voids the cross-family guarantee". The ruling answers that by naming the whole
router ONE class. These arms pin what that has to mean everywhere helm asks a
model's family:

  1  the openrouter family maps `openrouter/free` as its default, the class
     route `or-free`, and no refusal anywhere still carries the retired reason
  2  ONE owner function answers the class for both spellings, and the class is
     distinct from every standard family
  3  a model run's read by `or-free` is a CROSS-FAMILY read of a claude, codex
     or qwen author, and `or-free` reading `or-free` is the same family
  4  the proxy canary admits the model the router SERVED on the class route,
     and still refuses a served id outside the class
  5  free models may log or train on prompts, so a recipient on a
     public-code-only route is refused a row whose tip is on no PUBLIC
     branch: privacy is per commit, and a repository with a public remote
     still holds private commits (an unexported lane, a pre-public branch);
     which remote is public is read from the checkout's own config, never
     from the process environment, and gh is asked of github.com itself
  6  helm's own generator mints the class route, in a temporary helm home, and
     helm's own config checks read it clean
"""
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (dispatches, hostpath_guard, proxywatch,  # noqa: E402
                  repofacts, seat,
                  seat_catalog, seat_health, seat_launch_assets,
                  seat_lifecycle, tasks)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq  # noqa: E402
from tests._tmphome import pin_live_seats, pin_suite_guard  # noqa: E402


def setUpModule():
    # every class that writes a dispatch row runs under the measured-empty
    # live-seat stand-in (tests/test_env_hygiene.py says why)
    pin_live_seats()


FAMILY = "openrouter"  # noqa: SEAT_NAME — the catalog FAMILY that hosts the class IS the subject of every arm here
CLASS = "or-free"
ROUTER = "openrouter/free"
#: What the router served in the owner's own probes and in this lane's, every
#: call at cost 0: all of them carry the `:free` suffix.
SERVED = ("cohere/north-mini-code:free", "inclusionai/ling-3.0-flash-fin:free",
          "nvidia/nemotron-3-ultra-550b-a55b:free",
          "google/gemma-4-31b-it:free")


def _compat_block(text):
    _prefix, blocks = seat_launch_assets._top_blocks(text)
    return "".join(body for key, body in blocks
                   if key == "openai-compatibility")


def _hand_config(model):
    """A hand-written provider block mapping `model`, the shape the refused
    list is read against."""
    return ('openai-compatibility:\n'
            '  - name: "openrouter-hand"\n'
            '    base-url: "https://openrouter.ai/api/v1"\n'
            '    api-key-entries:\n'
            '      - api-key: "sk-or-TEST"\n'
            '    models:\n'
            '      - name: "%s"\n'
            '        alias: "or-anything"\n' % model)


def _class_table(**row_over):
    """A synthetic per-model family with ONE class row, valid as built; each
    arm breaks exactly one thing."""
    row = {"alias": "x-class", "default": True,
           "upstream_model": "vendor/router",
           "model_class": "x-class", "served_suffix": ":free",
           "pricing": {"prompt": "0", "completion": "0",
                       "read": "2026-09-25"},
           "terms": {"verdict": "public-code-only", "read": "2026-09-25",
                     "by": "a-test"}}
    row.update(row_over)
    return {"port": 9999, "model": "x-class", "mode": "proxy-key",
            "base_url": "https://example.invalid/api/v1",
            "key_env": "X_API_KEY", "provider": "x",
            "model_fallback": "x-code",
            "model_providers": {
                "x-router": row,
                "x-two": {"alias": "x-code",
                          "upstream_model": "vendor/two:free",
                          "pricing": {"prompt": "0", "completion": "0",
                                      "read": "2026-09-25"},
                          "terms": {"verdict": "private-code-safe",
                                    "read": "2026-09-25", "by": "a-test"}}}}


class TheRouterIsAdmittedAsOneClassTest(unittest.TestCase):
    """The deny-list entry is replaced by the admission, and the family's
    default seat runs the class."""

    def test_the_router_is_no_longer_refused_on_its_family(self):  # noqa: VACUOUS_ASSERTION — the SAME reader is asserted to refuse a still-refused id by its words, so its None for the router is a verdict
        fam = seat.FAMILIES[FAMILY]
        block = _compat_block(_hand_config(ROUTER))
        self.assertNotIn(ROUTER, fam["disqualified_models"])
        self.assertIsNone(
            seat_launch_assets.disqualified_route_reason(block, fam))
        # CONTROL on the SAME reader: a model the family still refuses for
        # training on submitted data is still refused, so the None above is
        # the admission and not a reader that stopped reading.
        refused = seat_launch_assets.disqualified_route_reason(
            _compat_block(_hand_config("liquid/lfm-2.5-2.6b:free")), fam)
        self.assertIn("TRAINS ON SUBMITTED DATA", refused)

    def test_the_family_default_is_the_class_route(self):
        fam = seat.FAMILIES[FAMILY]
        self.assertEqual(fam["model"], CLASS)
        route = seat.proxy_routes(FAMILY)[0]
        self.assertEqual(route, {"alias": CLASS, "provider": "openrouter-free",
                                 "upstream_model": ROUTER,
                                 "base_url": "https://openrouter.ai/api/v1"})
        row = fam["model_providers"]["openrouter-free"]
        self.assertEqual((row["model_class"], row["served_suffix"],
                          row["terms"]["verdict"]),
                         (CLASS, ":free", seat_catalog.DATA_TERMS_PUBLIC_ONLY))
        # the launch window is the router's own listed window less the output
        # cap, the input-ceiling law this table states for every pin
        self.assertEqual(fam["model_context"][CLASS],
                         row["probed_context_length"]
                         - fam["max_output_tokens"])

    def test_the_ruling_is_recorded_with_the_refusal_it_retired(self):
        """The history stays honest: the owner's words and the date, and the
        retired refusal's own reason, are DATA beside the class."""
        ruling = seat_catalog.OR_FREE_RULING
        self.assertEqual(ruling["class"], CLASS)
        self.assertEqual(ruling["router"], ROUTER)
        self.assertTrue(ruling["said"].startswith("2026-09-25"))
        self.assertIn("meta-model class model called OR-free",
                      ruling["verbatim"])
        self.assertIn("RANDOM FREE MODEL PER CALL", ruling["retired_refusal"])

    def test_no_refusal_anywhere_still_carries_the_retired_reason(self):  # noqa: VACUOUS_ASSERTION — the count of reasons read is asserted unconditionally before the loop, so an empty table fails there
        """ANY STALE PATH THAT STILL REFUSES THE ROUTER ON THE OLD GROUNDS
        FAILS HERE: every refusal the catalog carries is read, and the
        generator's own doc of what the list refuses is read with them."""
        reasons = [(family, model, why)
                   for family, fam in seat.FAMILIES.items()
                   for model, why in (fam.get("disqualified_models")
                                      or {}).items()]
        self.assertGreaterEqual(len(reasons), 10)
        for family, model, why in reasons:
            self.assertNotIn("RANDOM FREE MODEL", why, (family, model))
            self.assertNotIn("voids the cross-family", why, (family, model))
        doc = seat_launch_assets.disqualified_route_reason.__doc__
        self.assertIn("THE DISQUALIFICATION LIST", doc)
        self.assertNotIn("RANDOM free model", doc)


class OneOwnerAnswersTheClassTest(unittest.TestCase):
    """Every place helm derives a reviewer's family reads the class through
    ONE function, never a special case of its own."""

    def test_both_spellings_resolve_through_the_one_owner(self):  # noqa: VACUOUS_ASSERTION — each spelling is asserted EQUAL to the class, unconditionally, on both readers
        for spelling in (CLASS, ROUTER, "OR-FREE", "or-free[1m]",
                         " openrouter/free "):
            with self.subTest(spelling=spelling):
                self.assertEqual(seat_catalog.model_class(spelling), CLASS)
                self.assertEqual(dispatches._model_family(spelling), CLASS)

    def test_the_class_is_distinct_from_every_standard_family(self):  # noqa: VACUOUS_ASSERTION — the standard ids are asserted to resolve to their OWN families first, so the inequality is between two real answers
        self.assertNotIn(CLASS, seat.FAMILIES)
        self.assertEqual(dispatches._model_family("claude-opus-5-5"),
                         "claude")
        self.assertEqual(dispatches._model_family("gpt-6-astra"), "codex")  # noqa: SEAT_NAME — the MODEL family codex's model resolves to, not a seat
        self.assertEqual(dispatches._model_family("qwen27"), "qwen27")  # noqa: SEAT_NAME — the MODEL family of the local qwen, not a seat
        for family, fam in seat.FAMILIES.items():
            self.assertNotEqual(dispatches._family_lineage(family), CLASS,
                                family)
            if family != FAMILY:
                # no standard family's own launch model is a class member
                self.assertIsNone(seat_catalog.model_class(fam["model"]),
                                  family)

    def test_a_served_model_is_never_the_class(self):  # noqa: VACUOUS_ASSERTION — the same owner function is asserted EQUAL to the class for the router id first, so its None for a served id is a verdict
        """The class is named by the ROUTER. What it served is a model of its
        own, and the tail of the router id is not a spelling of it."""
        self.assertEqual(seat_catalog.model_class(ROUTER), CLASS)
        for spelling in SERVED + ("free", "openrouter", "or-fast"):
            self.assertIsNone(seat_catalog.model_class(spelling), spelling)


class TheClassDeclarationIsCheckedAtImportTest(unittest.TestCase):
    """A class row is a promise every reader keeps, so a malformed one is
    refused where it is typed."""

    def test_a_well_formed_class_row_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the arm beside it asserts the same predicate REFUSES each broken variant of this table by name
        self.assertIsNone(seat_catalog.model_provider_error("x",
                                                            _class_table()))

    def test_each_broken_class_row_is_refused_naming_its_key(self):  # noqa: VACUOUS_ASSERTION — the intact table is asserted admitted in the arm above, and every case asserts a reason that names its key
        cases = (
            ({"model_class": "y-class"}, "model_class"),
            ({"model_class": FAMILY}, "model_class"),
            ({"served_suffix": ""}, "served_suffix"),
            ({"terms": {"verdict": "trains-on-submitted-data",
                        "read": "2026-09-25", "by": "a-test"}},
             "private-code-safe"),
        )
        for over, word in cases:
            with self.subTest(over=over):
                why = seat_catalog.model_provider_error(
                    "x", _class_table(**over))
                self.assertIsNotNone(why)
                self.assertIn(word, why)
        plain = _class_table()
        del plain["model_providers"]["x-router"]["model_class"]
        why = seat_catalog.model_provider_error("x", plain)
        self.assertIn("served_suffix", why)


class AnOrFreeReadIsCrossFamilyTest(_landreq.LandReqBase):
    """A model run's read by the class, recorded on the ledger the way a
    Workflow run's is (`verdict --reviewer-model M --reviewer-run RUN`)."""

    RUN = "wf-or-free"

    def record(self, rid, author, reader=CLASS, run=None):
        return dispatches.mark_verdict(
            rid, self.side, "read clean", polarity="concur", basis="measured",
            bind_author=True, reviewer_model=reader,
            reviewer_run=run or self.RUN, author_model=author)

    def reads(self, rid):
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        return list(current[rid].get("advisory_reads") or ())

    def test_an_or_free_read_of_a_claude_codex_or_qwen_author_is_cross_family(self):  # noqa: VACUOUS_ASSERTION — every subtest asserts err is None and the recorded family and independence EQUAL exact values off the replayed ledger
        for n, author in enumerate(("claude-opus-5-5", "gpt-6-astra",
                                    "qwen27")):
            with self.subTest(author=author):
                row = self.dispatch(ref=self.side, lane="lane/or-free-%d" % n,
                                    kind="review")
                _out, err = self.record(row["id"], author)
                self.assertIsNone(err, err)
                read = self.reads(row["id"])[-1]
                self.assertEqual((read["reviewer_model"],
                                  read["reviewer_family"],
                                  read["independence"]),
                                 (CLASS, CLASS, "cross-family"))

    def test_the_router_id_reading_its_own_class_is_the_same_family(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted positively by its words, and the cross-family arm above proves a read DOES land on this ledger
        """Two spellings of one class are one family: the router id cannot
        review work its own alias wrote."""
        row = self.dispatch(ref=self.side, lane="lane/or-free-self",
                            kind="review")
        out, err = self.record(row["id"], CLASS, reader=ROUTER)
        self.assertIsNone(out)
        self.assertIn("same family (%s)" % CLASS, err)
        self.assertEqual(self.reads(row["id"]), [])


class TheCanaryAdmitsWhatTheRouterServedTest(unittest.TestCase):
    """The router names the model that served each call, so the class route's
    response model is never the router id. The canary that binds a seat's
    runtime proof must admit that, or the seat can never bind a verdict."""

    def _canary(self, served):
        routes = [r for r in seat.proxy_routes(FAMILY) if r["alias"] == CLASS]
        self.assertEqual(len(routes), 1)
        route = routes[0]
        index = "a" * 16
        shape = {"url": "http://127.0.0.1:8400", "token": "secret",
                 "model": CLASS, "auth_routes": {index: (route,)},
                 "proof": {"v": 1, "route": route}}
        trace = "20260925050000-%s-deadbeef" % index
        with mock.patch.object(proxywatch, "_proxy_runtime_shape",
                               return_value=(shape, None)), \
                mock.patch.object(proxywatch, "_canary_once",
                                  return_value=("HEALTHY", "HTTP 200", 4, 200,
                                                served, trace)), \
                mock.patch.object(proxywatch, "_sanitized_proxy_proof",
                                  side_effect=lambda proof: proof):
            proof, err = proxywatch.proxy_runtime_canary("seat-under-test",
                                                         observed_at=1000)
        return route, proof, err

    def test_a_served_free_model_binds_the_class_route(self):  # noqa: VACUOUS_ASSERTION — every subtest asserts the proof's route EQUAL to the class route and its canary EQUAL to HEALTHY
        for served in SERVED:
            with self.subTest(served=served):
                route, proof, err = self._canary(served)
                self.assertIsNone(err, err)
                self.assertEqual(proof["route"], route)
                self.assertEqual(proof["canary"],
                                 {"state": "HEALTHY", "status": 200})

    def test_a_served_id_outside_the_class_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — the admitted arm above runs the same canary to a real proof; here each refusal is asserted by its words
        for served in ("openai/gpt-6", "", None, "cohere/north-mini-code:free "):
            with self.subTest(served=served):
                _route, proof, err = self._canary(served)
                self.assertIsNone(proof)
                self.assertIn("canary response model does not match", err)
                self.assertIn(CLASS, err)


PRIVATE_SLUG = "acme/widget"
PUBLIC_SLUG = "acme/widget-public"
MIRROR_SLUG = "acme/widget-mirror"


def _answer(visibility, stale=False, why=None):
    """One cached gh answer, in the shape `repofacts.visibility` returns."""
    return {"visibility": visibility, "why": why, "checked_at": 1.0,
            "stale": stale}


class _HelmsOwnRemotes(object):
    """helm's own remotes, planted on the LandReqBase fixture repository as
    remote-tracking refs, with no network and no fetch: a PRIVATE working
    origin and a PUBLIC export. Trunk is a-b-c, and `side` is one commit off a:

      aspublic/main -> b       the export carries a and b
      origin/main   -> c       the private origin carries all of trunk
      origin/side   -> side    and a lane nobody exported

    So a and b are on a public branch, and c and side are on the private
    origin only. The remote NAMES are helm's; the export's SHAPE here is a
    shared-history one (a public fork or mirror whose branch carries trunk
    commits). helm's real export is a rewriting release that shares no
    commit with trunk; the rewriting-release arm below plants that shape
    instead. gh is never asked: `answers` stands in for its cache, and
    `refreshed` is what the cache holds once a re-ask was waited for (None:
    the wait ran out and nothing changed).

    Both remotes are scp-form GitHub URLs, and GIT_SSH_COMMAND runs every
    connection against a bare repository under the sandbox (`hosted`), so
    nothing reaches a network: the export's advertises main -> b, and the
    origin's main -> c and side -> side (task/3410: only what a PUBLIC
    remote ADVERTISES NOW vouches for a commit). HOME is an empty temp
    directory, because the door's reads drop GIT_CONFIG_GLOBAL and git then
    reads HOME's config."""

    def setUp(self):
        super().setUp()
        self.hosted = os.path.join(self.tmp, "hosted")
        bindir = os.path.join(self.tmp, "fake-bin")
        os.makedirs(bindir)
        self.ssh_log = os.path.join(self.tmp, "fake-ssh.log")
        ssh = os.path.join(bindir, "fake-ssh")
        with open(ssh, "w") as handle:
            handle.write('#!/bin/sh\nprintf "%%s\\n" "$2" >> %s\n'
                         'cd %s || exit 1\nexec sh -c "$2"\n'
                         % (shlex.quote(self.ssh_log),
                            shlex.quote(self.hosted)))
        os.chmod(ssh, 0o755)
        home = os.path.join(self.tmp, "fake-home")
        os.makedirs(home)
        # KEY BY KEY, NOT mock.patch.dict: a patch started here would restore
        # its whole snapshot after the base's tearDown, and so put back the
        # HELM_* values that tearDown had just removed.
        for key, value in (("GIT_SSH_COMMAND", ssh),
                           ("GIT_SSH_VARIANT", "simple"), ("HOME", home),
                           ("XDG_CONFIG_HOME", os.path.join(home, ".config"))):
            self.addCleanup(self._restore_env, key, os.environ.get(key))
            os.environ[key] = value
        self.objects = self.git("rev-parse", "--path-format=absolute",
                                "--git-common-dir") + "/objects"
        self.host(PRIVATE_SLUG, main=self.c, side=self.side)
        self.host(PUBLIC_SLUG, main=self.b)
        self.git("remote", "add", "origin", self.url(PRIVATE_SLUG))
        self.git("remote", "add", "aspublic", self.url(PUBLIC_SLUG))
        self.git("update-ref", "refs/remotes/aspublic/main", self.b)
        self.git("update-ref", "refs/remotes/origin/main", self.c)
        self.git("update-ref", "refs/remotes/origin/side", self.side)
        self.answers = {PRIVATE_SLUG: _answer("private"),
                        PUBLIC_SLUG: _answer("public")}
        self.refreshed = None

    @staticmethod
    def _restore_env(key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    @staticmethod
    def url(slug):
        return "git@github.com:%s.git" % slug

    def bare(self, slug):
        return os.path.join(self.hosted, slug + ".git")

    def host(self, slug, **heads):
        """A bare repository `slug` under the sandbox that advertises
        `heads` ({branch: sha}). Its objects are the fixture's own (an
        alternates file), so it can advertise any commit made here."""
        subprocess.run(["git", "init", "-q", "--bare", self.bare(slug)],
                       check=True, capture_output=True)
        with open(os.path.join(self.bare(slug), "objects", "info",
                               "alternates"), "w") as handle:
            handle.write(self.objects + "\n")
        for branch, sha in heads.items():
            self.advertise(slug, branch, sha)

    def advertise(self, slug, branch, sha):
        self.git("update-ref", "refs/heads/" + branch, sha,
                 cwd=self.bare(slug))

    def asked_upload_pack(self):
        """-> the upload-pack commands the fake ssh ran, one per read."""
        if not os.path.exists(self.ssh_log):
            return []
        with open(self.ssh_log) as handle:
            return handle.read().splitlines()

    def _visibility(self, slugs, ask=True):
        return {slug: dict(self.answers.get(slug) or _answer("pending"))
                for slug in slugs}

    def _drain(self, _timeout=None):
        if self.refreshed is None:
            return False
        self.answers.update(self.refreshed)
        return True

    @contextlib.contextmanager
    def or_free(self):
        """The recipient runs the class route, and gh is the fixture's
        cache."""
        with mock.patch.object(dispatches, "_recipient_data_terms",
                               return_value=(
                                   seat_catalog.DATA_TERMS_PUBLIC_ONLY,
                                   CLASS, FAMILY)), \
                mock.patch.object(repofacts, "visibility",
                                  side_effect=self._visibility) as vis, \
                mock.patch.object(repofacts, "drain",
                                  side_effect=self._drain) as drain:
            yield vis, drain

    def add(self, lane, ref, kind="review"):
        # FORCE ON PURPOSE, as in TheDispatchDoorAsksTheRungTest: it clears
        # the roster and usability rungs for a synthetic recipient, and the
        # data-terms rung must not yield to it.
        if kind == "review":
            work, why = tasks.add("public-route review fixture", "integrator",
                                  project="helm-test", force_new=True)
            self.assertIsNone(why, why)
        return dispatches.add("seat-under-test", lane, ref=ref,
                              repo=self.repo, kind=kind, new_work=True,
                              notify=False, force=True, _reason=True,
                              task=work["id"] if kind == "review" else None)

    def local_commit(self):
        """A commit on a local branch that no remote-tracking ref holds."""
        self.git("checkout", "-q", "-b", "unpushed", self.c)
        return self.commit("unpushed", path="u")


class ACommitOnNoPublicBranchNeverReachesTheClassTest(
        _HelmsOwnRemotes, _landreq.LandReqBase):
    """PRIVACY IS PER COMMIT, NOT PER REPOSITORY. helm's own checkout pushes
    to a PRIVATE origin and exports to a PUBLIC remote, so a rung that asks
    whether the REPOSITORY has a public remote admits every commit in it,
    including a lane that exists only on the private origin and a pre-public
    review branch. A row is admitted to a public-code-only recipient only
    when its tip is reachable from a remote-tracking ref of a remote whose
    FRESH gh answer is public."""

    def test_a_tip_only_on_the_private_origin_is_refused(self):
        """THE DEFECT: a lane nobody exported, in a repository that has a
        public remote."""
        with self.or_free():
            row, err = self.add("lane/unexported", self.side)
        self.assertIsNone(row)
        self.assertIn("not on any public branch", err)
        self.assertIn("origin/side", err)
        self.assertIn("%s reads private" % PRIVATE_SLUG, err)
        self.assertIn("helm reviewers", err)

    def test_a_trunk_commit_past_the_export_is_refused(self):
        """The same defect on trunk: c is on the private origin's main, and
        the export stops at b."""
        with self.or_free():
            row, err = self.add("lane/past-the-export", self.c)
        self.assertIsNone(row)
        self.assertIn("not on any public branch", err)
        self.assertIn("origin/main", err)

    def test_a_commit_on_no_remote_is_refused(self):
        tip = self.local_commit()
        with self.or_free():
            row, err = self.add("lane/unpushed", tip)
        self.assertIsNone(row)
        self.assertIn("not on any public branch", err)
        self.assertIn("no remote-tracking branch", err)

    def test_a_tip_the_public_export_carries_admits(self):  # noqa: VACUOUS_ASSERTION — each written row's tip is asserted EQUAL to the commit it names
        """CONTROL through the same door: a is an ancestor of the export's
        main and b is that ref itself, so both are public code."""
        with self.or_free():
            below, below_err = self.add("lane/public-ancestor", self.a)
            at, at_err = self.add("lane/public-tip", self.b)
        self.assertIsNone(below_err, below_err)
        self.assertEqual(below["tip"], self.a)
        self.assertIsNone(at_err, at_err)
        self.assertEqual(at["tip"], self.b)

    def test_a_fresh_public_remote_cannot_vouch_for_a_tip_it_lacks(self):
        """A second public remote, FRESH, holding only a, beside the export
        whose answer for b is a day old and was not refreshed. The fresh
        public remote does not carry b, so it cannot vouch for b; the remote
        that does carry it has only a stale answer."""
        self.git("remote", "add", "mirror",
                 "https://github.com/" + MIRROR_SLUG)
        self.git("update-ref", "refs/remotes/mirror/main", self.a)
        self.answers[MIRROR_SLUG] = _answer("public")
        self.answers[PUBLIC_SLUG] = _answer("public", stale=True)
        with self.or_free() as (_vis, drain):
            row, err = self.add("lane/stale-carrier", self.b)
        self.assertIsNone(row)
        self.assertIn("%s reads public, stale" % PUBLIC_SLUG, err)
        drain.assert_called_once()

    def test_a_stale_public_carrier_the_re_ask_confirms_admits(self):  # noqa: VACUOUS_ASSERTION — the written row's tip is asserted EQUAL and the wait asserted to have run once
        """CONTROL: the same wait, and gh says public again."""
        self.answers[PUBLIC_SLUG] = _answer("public", stale=True)
        self.refreshed = {PUBLIC_SLUG: _answer("public")}
        with self.or_free() as (_vis, drain):
            row, err = self.add("lane/stale-confirmed", self.a)
        self.assertIsNone(err, err)
        self.assertEqual(row["tip"], self.a)
        drain.assert_called_once()

    def test_retip_onto_a_private_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal's words are asserted, and the ledger row is re-read and asserted still EQUAL to the public tip
        """A NEW TIP IS A NEW COMMIT FOR THE SAME READER. A row admitted on a
        public commit must not be re-pointed at a private one: retip writes
        its hop without passing the door every minted row passes."""
        with self.or_free():
            row, err = self.add("lane/retip-base", self.a, kind="build")
            self.assertIsNone(err, err)
            moved, why = dispatches.retip(row["id"], self.side,
                                          reason="rebased onto the lane",
                                          repo=self.repo, notify=False)
        self.assertIsNone(moved)
        self.assertIn("not on any public branch", why)
        self.assertIn("origin/side", why)
        current, _unavailable = dispatches.snapshot()
        self.assertEqual(current[row["id"]]["tip"], self.a)

    def test_retip_onto_a_public_tip_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the retipped row's tip is asserted EQUAL to the export's commit
        """CONTROL: the same row, fast-forwarded to the export's own tip."""
        with self.or_free():
            row, err = self.add("lane/retip-public", self.a, kind="build")
            self.assertIsNone(err, err)
            moved, why = dispatches.retip(row["id"], self.b,
                                          reason="fast-forward",
                                          repo=self.repo, notify=False)
        self.assertIsNone(why, why)
        self.assertEqual(moved["tip"], self.b)

    def test_a_replacement_object_cannot_carry_a_private_tip(self):
        """A `refs/replace/` object rewrites parentage at every lookup. The
        export's tip b, replaced by a commit whose parent is `side`, makes
        the unexported lane read as contained in aspublic/main (measured on
        git 2.53). The reachability read runs with replacement and grafts
        switched off, like every other witness read in this module."""
        tree = self.git("rev-parse", self.b + "^{tree}")
        forged = self.git("commit-tree", tree, "-p", self.side,
                          "-m", "b, re-parented onto the lane")
        self.git("replace", self.b, forged)
        with self.or_free():
            row, err = self.add("lane/replaced-export", self.side)
        self.assertIsNone(row)
        self.assertIn("not on any public branch", err)
        self.assertIn("origin/side", err)
        self.assertNotIn("aspublic/main", err)

    def test_a_non_github_remote_under_the_exports_name_is_ambiguous(self):
        """`git remote add` refuses a name that is a subset of another
        remote's, but a hand-written config does not: a backup remote named
        `aspublic/mirror` fetches into refs/remotes/aspublic/mirror/, under
        the public export's namespace. It is not a GitHub remote, so gh is
        never asked about it, and its refs must not be credited to the
        export for that reason."""
        self.git("config", "remote.aspublic/mirror.url",
                 "/srv/backup/widget.git")
        self.git("update-ref", "refs/remotes/aspublic/mirror/side", self.side)
        with self.or_free():
            row, err = self.add("lane/mirrored", self.side)
        self.assertIsNone(row)
        self.assertIn("not on any public branch", err)
        self.assertIn("origin/side", err)
        self.assertNotIn("aspublic/mirror/side", err)

    def test_a_rewriting_release_admits_only_its_own_commit(self):  # noqa: VACUOUS_ASSERTION — the refused row's words and the admitted row's tip EQUAL to the release commit are both asserted
        """helm's REAL export: the public main is a line of release commits,
        each a NEW commit whose tree is the trunk tree minus the omit list
        (scripts/release/release.py), so it shares no commit with the
        private side. The trunk commit a release was built from is refused
        even when the trees match, and the release commit is admitted."""
        tree = self.git("rev-parse", self.c + "^{tree}")
        release = self.git("commit-tree", tree, "-m", "widget 0.1.0")
        self.git("update-ref", "refs/remotes/aspublic/main", release)
        self.advertise(PUBLIC_SLUG, "main", release)
        with self.or_free():
            trunk, trunk_err = self.add("lane/released-trunk", self.c)
            public, public_err = self.add("lane/the-release", release)
        self.assertIsNone(trunk)
        self.assertIn("not on any public branch", trunk_err)
        self.assertIn("origin/main", trunk_err)
        self.assertIsNone(public_err, public_err)
        self.assertEqual(public["tip"], release)


class AnUnreadableGitRefusesTest(_HelmsOwnRemotes, _landreq.LandReqBase):
    """Whether a public branch carries the tip is a git read, and a read that
    failed is an unknown, never a yes."""

    def fake_run(self, rc=128, raises=None):
        """subprocess.run, except the reachability read, which exits `rc`
        (or raises) while printing the export's ref: a reader that parsed
        stdout without the exit code would admit."""
        real = subprocess.run
        seen = []

        def run(argv, *args, **kwargs):
            if "for-each-ref" in argv and any(
                    str(a).startswith("--contains") for a in argv):
                seen.append(list(argv))
                if raises is not None:
                    raise raises
                return subprocess.CompletedProcess(
                    argv, rc, stdout="refs/remotes/aspublic/main\n",
                    stderr="fatal: bad object\n")
            return real(argv, *args, **kwargs)
        return run, seen

    def test_a_non_zero_exit_refuses_a_tip_that_is_public(self):
        run, seen = self.fake_run(rc=128)
        with self.or_free(), mock.patch.object(subprocess, "run",
                                               side_effect=run):
            row, err = self.add("lane/git-exited", self.a)
        self.assertIsNone(row)
        self.assertIn("could not be read", err)
        self.assertIn("128", err)
        self.assertEqual(len(seen), 1)

    def test_a_git_that_does_not_answer_refuses(self):
        run, seen = self.fake_run(raises=subprocess.TimeoutExpired("git", 5))
        with self.or_free(), mock.patch.object(subprocess, "run",
                                               side_effect=run):
            row, err = self.add("lane/git-hung", self.a)
        self.assertIsNone(row)
        self.assertIn("could not be read", err)
        self.assertEqual(len(seen), 1)

    def test_a_missing_object_refuses(self):
        """A well-formed id this repository does not hold: git exits 129."""
        missing = "de" * 20
        with self.or_free():
            ok, why = dispatches._data_terms_rung("seat-under-test",
                                                  self.repo, missing)
        self.assertFalse(ok)
        self.assertIn("could not be read", why)
        self.assertIn(missing[:12], why)


class TheAmbientEnvironmentNamesNoPublicRemoteTest(
        _HelmsOwnRemotes, _landreq.LandReqBase):
    """WHERE A REMOTE POINTS IS THE CHECKOUT'S OWN FACT, and the verdict
    pairs it with the refs that remote's name owns. git also reads the
    process environment: GIT_DIR picks another repository, and
    GIT_CONFIG_COUNT with its KEY/VALUE pairs, or GIT_CONFIG_PARAMETERS (the
    `git -c` channel), adds configuration above the checkout's own. MEASURED
    on the reviewed tip (cursor, helm chat row 1649, and again for this
    cure): each one made a tip that only the PRIVATE origin carries read as
    public, because the remotes read ran in the ambient environment while
    the reachability read ran scrubbed, so a public slug was paired with the
    private origin's refs.

    Two more shapes reach the same pairing through configuration a scrub of
    the environment leaves alone, and both were measured to admit: a HOME
    whose .gitconfig gives a checkout's non-GitHub remote a public GitHub
    URL, and a remote the checkout's own config names with two URLs (git
    fetches from the first, and a reader that keeps the last credits the
    other repository).

    Each arm plants its shape and asks the rung about `side`, which only
    origin/side carries, and about `b`, the export's own tip. The first must
    be refused and the second still admitted under the same plant, so a rung
    that refused everything, or a read that broke under the plant, cannot
    pass."""

    def verdicts(self):
        """The rung's (ok, why) for `side`, then for `b`."""
        with self.or_free():
            return tuple(dispatches._data_terms_rung("seat-under-test",
                                                     self.repo, tip)
                         for tip in (self.side, self.b))

    def assert_side_refused_b_admitted(self, got, *words):
        (side_ok, side_why), (b_ok, b_why) = got
        self.assertFalse(side_ok, "the private tip was admitted")
        self.assertIn("not on any public branch", side_why)
        for word in words:
            self.assertIn(word, side_why)
        self.assertTrue(b_ok, b_why)
        self.assertIsNone(b_why)

    def test_an_ambient_git_dir_cannot_lend_another_checkouts_remotes(self):  # noqa: VACUOUS_ASSERTION — the shared helper asserts side REFUSED by its words and b ADMITTED as (True, None) on the same rung, unconditionally
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "init", "-q", other], check=True,
                       capture_output=True)
        self.git("remote", "add", "origin", "https://github.com/" + PUBLIC_SLUG,
                 cwd=other)
        with mock.patch.dict(os.environ,
                             {"GIT_DIR": os.path.join(other, ".git")}):
            got = self.verdicts()
        self.assert_side_refused_b_admitted(
            got, "origin/side", "%s reads private" % PRIVATE_SLUG)

    def test_injected_config_cannot_rename_the_origin(self):  # noqa: VACUOUS_ASSERTION — the shared helper asserts side REFUSED by its words and b ADMITTED as (True, None) on the same rung, unconditionally
        """GIT_CONFIG_COUNT, and the read after it: the remotes are memoized
        per config file, and the injection does not touch that file, so a
        planted answer that reached the memo would outlive the plant."""
        with mock.patch.dict(os.environ, {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "remote.origin.url",
                "GIT_CONFIG_VALUE_0": "https://github.com/" + PUBLIC_SLUG}):
            planted = self.verdicts()
        after = self.verdicts()
        for got in (planted, after):
            self.assert_side_refused_b_admitted(
                got, "origin/side", "%s reads private" % PRIVATE_SLUG)

    def test_the_git_dash_c_channel_cannot_rename_the_origin(self):  # noqa: VACUOUS_ASSERTION — the shared helper asserts side REFUSED by its words and b ADMITTED as (True, None) on the same rung, unconditionally
        """`git -c k=v` exports its settings to every child git as
        GIT_CONFIG_PARAMETERS, so a helm run under a git hook inherits them.
        MEASURED on git 2.53: this value made `git config --get-regexp`
        print a second origin URL, after the checkout's own."""
        with mock.patch.dict(os.environ, {
                "GIT_CONFIG_PARAMETERS":
                    "'remote.origin.url'='https://github.com/%s'"
                    % PUBLIC_SLUG}):
            got = self.verdicts()
        self.assert_side_refused_b_admitted(
            got, "origin/side", "%s reads private" % PRIVATE_SLUG)

    def test_an_environment_git_cannot_parse_does_not_decide(self):  # noqa: VACUOUS_ASSERTION — the shared helper asserts side REFUSED by its words and b ADMITTED as (True, None) on the same rung, unconditionally
        """CONTROL for the scrub on EVERY read: a GIT_CONFIG_COUNT that is
        not a number, or a GIT_CONFIG_PARAMETERS git cannot parse, makes any
        git that reads it exit 128 (measured, git 2.53). Both verdicts must
        stand as they do in a clean environment, which holds only when
        neither the remotes read nor the reachability read sees them."""
        for plant in ({"GIT_CONFIG_COUNT": "not a count"},
                      {"GIT_CONFIG_PARAMETERS": "not a parameter"}):
            with self.subTest(plant=sorted(plant)):
                repofacts._REMOTES_MEMO.clear()
                with mock.patch.dict(os.environ, plant):
                    got = self.verdicts()
                self.assert_side_refused_b_admitted(
                    got, "origin/side", "%s reads private" % PRIVATE_SLUG)

    def test_a_home_config_cannot_put_a_backup_remote_on_github(self):  # noqa: VACUOUS_ASSERTION — the shared helper asserts side REFUSED by its words and b ADMITTED as (True, None) on the same rung, unconditionally
        """The environment scrub keeps HOME, because git needs it, and git
        reads HOME's .gitconfig before the checkout's own. A backup remote
        the checkout points at a plain path, given a public GitHub URL
        there, is not a public remote: its refs came from the backup."""
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, ".gitconfig"), "w") as handle:
            handle.write('[remote "backup"]\n\turl = https://github.com/%s\n'
                         % PUBLIC_SLUG)
        self.git("remote", "add", "backup", "/srv/backup/widget.git")
        self.git("update-ref", "refs/remotes/backup/side", self.side)
        with mock.patch.dict(os.environ, {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config")}):
            # a runner that pins its own global config would hide HOME's
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
            got = self.verdicts()
        self.assert_side_refused_b_admitted(
            got, "origin/side", "%s reads private" % PRIVATE_SLUG,
            "backup is not credited", "global")

    def test_a_remote_with_two_urls_is_credited_with_neither(self):  # noqa: VACUOUS_ASSERTION — the shared helper asserts side REFUSED by its words and b ADMITTED as (True, None) on the same rung, unconditionally
        """`git remote set-url --add` gives one remote a second URL. git
        fetches from the first, so the refs under that name came from the
        private origin; which one they came from is not something the
        verdict can read back, so the remote vouches for nothing."""
        self.git("config", "--add", "remote.origin.url",
                 "https://github.com/" + PUBLIC_SLUG)
        got = self.verdicts()
        self.assert_side_refused_b_admitted(
            got, "origin is not credited", "2 different URLs")


class APublicRemoteVouchesOnlyForWhatItAdvertisesTest(
        _HelmsOwnRemotes, _landreq.LandReqBase):
    """task/3410: A REMOTE-TRACKING REF IS A LOCAL RECORD OF SOME FETCH, NOT
    EVIDENCE OF WHAT THE REPOSITORY ITS REMOTE NOW NAMES HOLDS. The door
    credited a PUBLIC remote's tracking refs, so three plants let a commit
    only the PRIVATE origin holds read as public and reach a free model:
    a remote that fetched the private repository and was then set to the
    public one, a fetch refspec that writes a private branch into the public
    remote's namespace, and a hand `git update-ref` there. The same class
    task/3395 closed in the host-path guard. Now the remote must also
    ADVERTISE NOW a tip that reaches the commit (`git ls-remote` of its own
    plain GitHub URL, nothing steering it, redirects off, bounded), and any
    read of that which fails counts nothing.

    `side` is the private commit: origin/side carries it, and the export
    advertises only main -> b, which does not reach it. Each RED arm plants
    one shape that puts `side` under refs/remotes/aspublic/."""

    def refused(self, lane, tip, *words):
        with self.or_free():
            row, err = self.add(lane, tip)
        self.assertIsNone(row, "admitted: nothing the export advertises "
                               "now was shown to reach the tip")
        self.assertIn("not on any public branch", err)
        for word in words:
            self.assertIn(word, err)
        return err

    NOT_REACHED = ("%s reads public, but aspublic is not counted: no tip it "
                   "advertises now reaches it" % PUBLIC_SLUG)

    def test_a_remote_set_to_public_after_a_private_fetch_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper asserts the refusal's words IN the message, beside the tracking ref asserted EQUAL to side and the export's upload-pack asserted IN the log
        """RED on trunk: the fetch wrote aspublic/side from the PRIVATE
        repository, and `git remote set-url` then named the public one; the
        tracking ref stayed and read as the public remote's."""
        self.git("remote", "set-url", "aspublic", self.url(PRIVATE_SLUG))
        self.git("fetch", "-q", "aspublic")
        self.assertEqual(self.git("rev-parse", "refs/remotes/aspublic/side"),
                         self.side, "must-hit: the private fetch wrote it")
        self.git("remote", "set-url", "aspublic", self.url(PUBLIC_SLUG))
        self.refused("lane/set-url", self.side, "aspublic/side",
                     self.NOT_REACHED)
        self.assertIn("git-upload-pack 'acme/widget-public.git'",
                      self.asked_upload_pack(),
                      "must-hit: the export's advertisement was read")

    def test_a_cross_refspec_into_the_public_namespace_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper asserts the refusal's words IN the message, beside the refspec's tracking ref asserted EQUAL to side
        """RED on trunk: a fetch refspec of origin's that writes its side
        branch under refs/remotes/aspublic/."""
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/side:refs/remotes/aspublic/side")
        self.git("fetch", "-q", "origin")
        self.assertEqual(self.git("rev-parse", "refs/remotes/aspublic/side"),
                         self.side, "must-hit: the refspec wrote it")
        self.refused("lane/cross-refspec", self.side, "aspublic/side",
                     self.NOT_REACHED)

    def test_an_update_ref_into_the_public_namespace_is_refused(self):  # noqa: VACUOUS_ASSERTION — the helper asserts the refusal's words IN the message: the planted ref and the unreached advertisement
        """RED on trunk: a hand `git update-ref`, no fetch at all."""
        self.git("update-ref", "refs/remotes/aspublic/planted", self.side)
        self.refused("lane/update-ref", self.side, "aspublic/planted",
                     self.NOT_REACHED)

    def test_a_url_git_sends_elsewhere_is_not_credited(self):  # noqa: VACUOUS_ASSERTION — each subtest asserts the remote row IN the read with no slug and the refusal's words IN the message; the empty upload-pack log is the contract
        """RED on trunk: `repofacts.slug_of` read each of these as
        github.com/acme/widget-public, while git fetches from the host before
        the `#` or the `?`, or from the local path /acme/widget-public
        (task/3413 measured each), so the refs its fetch wrote read as the
        public export's. A URL that is not a plain GitHub repository URL
        now names no repository, and its remote is credited with nothing."""
        for url in ("https://evil.example#@github.com/" + PUBLIC_SLUG,
                    "https://evil.example?@github.com/" + PUBLIC_SLUG,
                    "file://github.com/" + PUBLIC_SLUG):
            with self.subTest(url=url):
                self.git("remote", "set-url", "aspublic", url)
                self.git("update-ref", "refs/remotes/aspublic/side",
                         self.side)
                rows, why = repofacts.remotes(self.repo)
                self.assertIsNone(why, why)
                self.assertIn(("aspublic", None),
                              [(r["remote"], r["slug"]) for r in rows],
                              "must-hit: the remote is read, as no slug")
                err = self.refused("lane/elsewhere", self.side,
                                   "origin/side",
                                   "%s reads private" % PRIVATE_SLUG)
                self.assertNotIn("aspublic/side", err)
                self.assertEqual(self.asked_upload_pack(), [])

    def test_a_tip_the_public_remote_advertises_now_is_admitted(self):  # noqa: VACUOUS_ASSERTION — each written row's tip is asserted EQUAL to its commit, and the one upload-pack per verdict is asserted EQUAL
        """CONTROL: b is the export's advertised main, and a is below it.
        Each verdict reads the export's advertisement once, through the
        sandbox's ssh."""
        with self.or_free():
            at, at_err = self.add("lane/advertised-tip", self.b)
            below, below_err = self.add("lane/advertised-ancestor", self.a)
        self.assertIsNone(at_err, at_err)
        self.assertEqual(at["tip"], self.b)
        self.assertIsNone(below_err, below_err)
        self.assertEqual(below["tip"], self.a)
        self.assertEqual(self.asked_upload_pack(),
                         ["git-upload-pack 'acme/widget-public.git'"] * 2)

    def test_an_advertisement_that_cannot_be_read_counts_nothing(self):  # noqa: VACUOUS_ASSERTION — each case asserts its refusal's words IN the message and a non-empty upload-pack log; the timeout's one read is asserted EQUAL to the door's bound
        """CONTROL for the cure, and RED on trunk too, which read no
        advertisement: b is the export's own tip, and each failure refuses
        it. The export's repository is gone (the ssh read fails), holds no
        ref, or answers after the door's bound."""
        cases = (("gone", "aspublic is not counted: not reachable"),
                 ("empty", "aspublic is not counted: it advertises no branch "
                           "or tag"))
        for case, words in cases:
            with self.subTest(case=case):
                shutil.rmtree(self.bare(PUBLIC_SLUG), ignore_errors=True)
                if os.path.exists(self.ssh_log):
                    os.remove(self.ssh_log)
                if case == "empty":
                    self.host(PUBLIC_SLUG)
                self.refused("lane/unread-" + case, self.b, "aspublic/main",
                             "%s reads public, but %s" % (PUBLIC_SLUG, words))
                self.assertTrue(self.asked_upload_pack(),
                                "must-hit: the read was attempted")
        seen, real = [], hostpath_guard._git

        def hang(root, *args, **kw):
            if args[:1] == ("ls-remote",):
                seen.append(kw.get("timeout"))
                raise subprocess.TimeoutExpired(["git", "ls-remote"],
                                                kw.get("timeout"))
            return real(root, *args, **kw)
        with mock.patch.object(hostpath_guard, "_git", side_effect=hang):
            self.refused("lane/unread-timeout", self.b,
                         "aspublic is not counted: ls-remote timed out")
        self.assertEqual(seen, [15], "the door's bound on one read")

    def test_a_steered_url_is_never_asked(self):  # noqa: VACUOUS_ASSERTION — each subtest asserts the steering reason IN the refusal; the empty upload-pack log is the contract
        """RED on trunk: an insteadOf that rewrites the export's URL, and a
        remote named as that URL (which `git ls-remote <url>` resolves
        first), each answer the ls-remote with the PRIVATE repository, which
        advertises side. The URL is not asked at all."""
        public, private = self.url(PUBLIC_SLUG), self.url(PRIVATE_SLUG)
        plants = ((("url.%s.insteadOf" % private, public),
                   "an insteadOf rewrite applies to its URL"),
                  (("remote.%s.url" % public, private),
                   "a remote is configured under its URL's own name"))
        self.git("update-ref", "refs/remotes/aspublic/planted", self.side)
        for (key, value), words in plants:
            with self.subTest(key=key.split(".")[0]):
                self.git("config", key, value)
                try:
                    self.refused("lane/steered", self.side,
                                 "aspublic is not counted: " + words)
                    self.assertEqual(self.asked_upload_pack(), [])
                finally:
                    self.git("config", "--unset", key)

    def test_the_reach_read_sees_the_history_the_ids_name(self):  # noqa: VACUOUS_ASSERTION — each subtest's must-hit is merge-base exiting 0 under the rewrite (check=True), and the helper asserts the unreached advertisement IN the refusal
        """RED on trunk: the export's b, replaced by a commit whose parent
        is `side`, or given `side` as a parent in a grafts file, makes side
        read as reachable from what the export advertises; the planted
        tracking ref gets it past the first read. The reach read runs with
        replacement objects and the grafts file off."""
        self.git("update-ref", "refs/remotes/aspublic/planted", self.side)
        tree = self.git("rev-parse", self.b + "^{tree}")
        forged = self.git("commit-tree", tree, "-p", self.side,
                          "-m", "b, re-parented onto the lane")
        grafts = os.path.join(self.git("rev-parse", "--path-format=absolute",
                                       "--git-common-dir"), "info", "grafts")
        for rewrite in ("replace", "grafts"):
            with self.subTest(rewrite=rewrite):
                if rewrite == "replace":
                    self.git("replace", self.b, forged)
                else:
                    os.makedirs(os.path.dirname(grafts), exist_ok=True)
                    with open(grafts, "w") as handle:
                        handle.write("%s %s\n" % (self.b, self.side))
                try:
                    # must-hit: exit 0, so side reads as below b here
                    self.git("merge-base", "--is-ancestor", self.side, self.b)
                    self.refused("lane/rewritten", self.side,
                                 self.NOT_REACHED)
                finally:
                    if rewrite == "replace":
                        self.git("replace", "-d", self.b)
                    else:
                        os.remove(grafts)

    def test_the_ambient_environment_cannot_steer_the_advertisement(self):  # noqa: VACUOUS_ASSERTION — each subtest asserts side IN a bare ls-remote under the plant (must-hit) and the unreached advertisement IN the door's refusal
        """RED on trunk: an insteadOf in the process environment (the `git
        -c` channel, or GIT_CONFIG_COUNT) that rewrites the export's URL to
        the private repository would answer the ls-remote with side. Every
        read of the door runs with injected configuration removed, the
        remote config read and the ls-remote alike."""
        public, private = self.url(PUBLIC_SLUG), self.url(PRIVATE_SLUG)
        self.git("update-ref", "refs/remotes/aspublic/planted", self.side)
        plants = ({"GIT_CONFIG_PARAMETERS":
                   "'url.%s.insteadof'='%s'" % (private, public)},
                  {"GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": "url.%s.insteadOf" % private,
                   "GIT_CONFIG_VALUE_0": public})
        for plant in plants:
            with self.subTest(plant=sorted(plant)[0]):
                with mock.patch.dict(os.environ, plant):
                    got = subprocess.run(
                        ["git", "ls-remote", "--heads", public],
                        cwd=self.repo, capture_output=True, text=True)
                    self.assertIn(self.side, got.stdout,
                                  "must-hit: the plant steers a bare read")
                    self.refused("lane/ambient", self.side, self.NOT_REACHED)

    def test_one_verdict_reads_a_bounded_number_of_advertisements(self):  # noqa: VACUOUS_ASSERTION — the upload-pack log is asserted EQUAL to exactly the reads made, and the unasked remote's reason IN the refusal
        """CONTROL for the bound: each read is limited to 15 s
        (`test_an_advertisement_that_cannot_be_read_counts_nothing`), and
        one verdict reads at most three remote URLs, one read per URL. Five
        public remotes carry the private tip here, two of them under one
        URL; each advertises only b."""
        for n in (1, 2, 3, 4):
            slug = "acme/widget-pub%d" % n
            self.host(slug, main=self.b)
            self.answers[slug] = _answer("public")
            self.git("remote", "add", "pub%d" % n, self.url(slug))
            self.git("update-ref", "refs/remotes/pub%d/planted" % n,
                     self.side)
        self.git("remote", "add", "pub1-again", self.url("acme/widget-pub1"))
        self.git("update-ref", "refs/remotes/pub1-again/planted", self.side)
        self.refused("lane/bounded", self.side,
                     "pub1 is not counted: no tip it advertises now reaches "
                     "it", "pub1-again is not counted: no tip it advertises "
                     "now reaches it", "pub4 is not counted: not asked: a "
                     "verdict reads at most 3 remotes' advertisements")
        self.assertEqual(self.asked_upload_pack(), [
            "git-upload-pack 'acme/widget-pub%d.git'" % n for n in (1, 2, 3)])

    def test_a_stale_public_answer_asks_no_advertisement(self):  # noqa: VACUOUS_ASSERTION — the helper asserts 'reads public, stale' IN the refusal; the empty upload-pack log is the contract
        """CONTROL: only a FRESH public answer admits, and the advertisement
        is read only for one; a stale answer the wait did not refresh is
        refused before any ls-remote."""
        self.answers[PUBLIC_SLUG] = _answer("public", stale=True)
        self.refused("lane/stale-no-read", self.b,
                     "%s reads public, stale" % PUBLIC_SLUG)
        self.assertEqual(self.asked_upload_pack(), [])

    def test_a_recipient_on_another_route_asks_nothing_new(self):  # noqa: VACUOUS_ASSERTION — the absent reads ARE the contract; the written row's tip is asserted EQUAL, and the or-free control in the same world asserts the read ran
        """CONTROL: a recipient whose route is private-code-safe is admitted
        from the catalog alone: no remotes read, no gh, no ls-remote. The
        or-free recipient in the same world reads the advertisement."""
        with mock.patch.object(dispatches, "_recipient_data_terms",
                               return_value=(seat_catalog.DATA_TERMS_SAFE,
                                             "x-code", "xfam")), \
                mock.patch.object(repofacts, "remotes") as remote_read, \
                mock.patch.object(repofacts, "visibility") as vis_read, \
                mock.patch.object(hostpath_guard, "_advertisement") as adv, \
                mock.patch.object(hostpath_guard, "_gh_visibility") as gh:
            row, err = self.add("lane/safe-route", self.side)
        self.assertIsNone(err, err)
        self.assertEqual(row["tip"], self.side)
        for read in (remote_read, vis_read, adv, gh):
            read.assert_not_called()
        self.assertEqual(self.asked_upload_pack(), [])
        with self.or_free():
            row, err = self.add("lane/class-route", self.b)
        self.assertIsNone(err, err)
        self.assertEqual(self.asked_upload_pack(),
                         ["git-upload-pack 'acme/widget-public.git'"])


class TheVisibilityIsAskedOfGitHubTest(unittest.TestCase):
    """`gh repo view OWNER/NAME` asks the host GH_HOST names, and github.com
    only when it names none. MEASURED with gh 2.46: GH_HOST=bogus.invalid
    sent `akapug/helm` to bogus.invalid, while `github.com/akapug/helm` was
    answered by github.com. A slug is only ever parsed out of a github.com
    URL, so an enterprise host where the same OWNER/NAME is public would
    otherwise answer for a private GitHub repository, and that answer is
    cached for a day. The stand-in gh below resolves the host the same way."""

    FAKE_GH = ('#!/bin/sh\n'
               'case "$3" in\n'
               '  */*/*) host="${3%%/*}" ;;\n'
               '  *) host="${GH_HOST:-github.com}" ;;\n'
               'esac\n'
               'if [ "$host" = github.com ]; then\n'
               '  echo \'{"visibility":"PRIVATE"}\'\n'
               'else\n'
               '  echo \'{"visibility":"PUBLIC"}\'\n'
               'fi\n')

    def setUp(self):
        self.bindir = tempfile.mkdtemp(prefix="helm-fake-gh-")
        self.addCleanup(shutil.rmtree, self.bindir, True)
        with open(os.path.join(self.bindir, "gh"), "w") as handle:
            handle.write(self.FAKE_GH)
        os.chmod(os.path.join(self.bindir, "gh"), 0o755)

    def ask(self, gh_host=None):
        path = self.bindir + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": path}):
            os.environ.pop("GH_HOST", None)
            if gh_host:
                os.environ["GH_HOST"] = gh_host
            return repofacts._gh_visibility(PRIVATE_SLUG)

    def test_gh_host_cannot_move_the_ask_off_github(self):
        self.assertEqual(self.ask(), ("PRIVATE", None))     # the control
        self.assertEqual(self.ask("ghe.example.com"), ("PRIVATE", None))


class APrivateRepositoryNeverReachesTheClassTest(
        _HelmsOwnRemotes, _landreq.LandReqBase):
    """Free models may log or train on prompts. MEASURED through the funded key: with no
    data policy on the request the router served
    nvidia/nemotron-3-ultra-550b-a55b:free, whose one endpoint is the free tier
    the dots3 entry refuses on data terms. What these arms keep from the
    repository-level rung: the visibility half, read on the remotes whose
    refs carry the tip."""

    def test_a_repository_whose_remotes_are_all_private_is_refused(self):
        self.answers[PUBLIC_SLUG] = _answer("private")
        with self.or_free():
            row, err = self.add("lane/all-private", self.a)
        self.assertIsNone(row)
        self.assertIn(seat_catalog.DATA_TERMS_PUBLIC_ONLY, err)
        self.assertIn("%s reads private" % PRIVATE_SLUG, err)
        self.assertIn("%s reads private" % PUBLIC_SLUG, err)
        self.assertIn(CLASS, err)

    def test_an_unanswered_visibility_is_refused_not_assumed(self):  # noqa: VACUOUS_ASSERTION — each case asserts the refusal and the words that name the reading
        for vis in ("pending", "unknown", "internal"):
            with self.subTest(vis=vis):
                self.answers = {PRIVATE_SLUG: _answer(vis),
                                PUBLIC_SLUG: _answer(vis)}
                with self.or_free():
                    row, err = self.add("lane/unanswered-" + vis, self.a)
                self.assertIsNone(row)
                self.assertIn("%s reads %s" % (PUBLIC_SLUG, vis), err)

    def test_a_checkout_with_no_github_remote_is_refused(self):
        for name in ("origin", "aspublic"):
            self.git("remote", "remove", name)
        self.git("remote", "add", "backup", "/srv/backup/widget.git")
        with self.or_free():
            row, err = self.add("lane/no-github", self.a)
        self.assertIsNone(row)
        self.assertIn("no GitHub remote", err)

    def test_the_repair_never_advises_a_relaunch_that_still_leaks(self):
        """or-free IS the family default, and the built-in subagent ids ride
        the default whatever alias the seat launched on, so another alias of
        the same seat is no repair and the refusal must not offer one."""
        self.answers[PUBLIC_SLUG] = _answer("private")
        with self.or_free():
            row, err = self.add("lane/repair", self.a)
        self.assertIsNone(row)
        self.assertIn("no relaunch of @seat-under-test", err)
        self.assertNotIn("helm seat launch", err)

    def test_a_stale_public_answer_is_asked_again_before_it_admits(self):
        """OFFLINE, the re-ask records `unknown`, and the row is refused
        rather than admitted on yesterday's answer."""
        self.answers[PUBLIC_SLUG] = _answer("public", stale=True)
        self.refreshed = {PUBLIC_SLUG: _answer(
            "unknown", why="gh did not answer (Timeout)")}
        with self.or_free() as (vis, drain):
            row, err = self.add("lane/stale-offline", self.a)
        self.assertIsNone(row)
        self.assertIn("%s reads unknown" % PUBLIC_SLUG, err)
        drain.assert_called_once()
        self.assertEqual(vis.call_args_list[1][1], {"ask": False})

    def test_a_stale_public_the_wait_did_not_refresh_is_refused(self):
        """THE WAIT RAN OUT: gh hung past the 20 s bound, or its answer
        could not be written, so the cache still holds yesterday's `public`
        after the wait. The FINAL admission must read freshness too, or the
        wait is decoration and the stale answer admits exactly as before."""
        self.answers[PUBLIC_SLUG] = _answer("public", stale=True)
        with self.or_free() as (_vis, drain):
            row, err = self.add("lane/stale-unrefreshed", self.a)
        self.assertIsNone(row)
        self.assertIn("%s reads public, stale" % PUBLIC_SLUG, err)
        drain.assert_called_once()

    def test_a_fresh_public_answer_waits_for_nothing(self):  # noqa: VACUOUS_ASSERTION — the written row's tip and the one visibility read are asserted EQUAL; the absent drain is the contract
        """CONTROL: a fresh public answer admits with no drain."""
        with self.or_free() as (vis, drain):
            row, err = self.add("lane/fresh", self.a)
        self.assertIsNone(err, err)
        self.assertEqual(row["tip"], self.a)
        drain.assert_not_called()
        self.assertEqual(vis.call_count, 1)


class ARecipientWithNoPublicOnlyRouteRunsNoGitTest(_landreq.LandReqBase):
    """Every family but the few that map a public-code-only route answers
    from the catalog alone: no remotes read, no gh, and no git."""

    def test_a_private_code_safe_family_makes_no_git_call(self):  # noqa: VACUOUS_ASSERTION — the absent spawn and reads ARE the contract, and the rung's own admission is asserted True
        def refuse(*args, **kwargs):
            raise AssertionError("the rung spawned a process: %r" % (args,))
        family = "codex"  # noqa: SEAT_NAME — a catalog FAMILY with no public-code-only route, the fast path's subject
        with mock.patch.object(dispatches, "_verified_family",
                               return_value=family), \
                mock.patch.object(subprocess, "run", side_effect=refuse), \
                mock.patch.object(subprocess, "Popen", side_effect=refuse), \
                mock.patch.object(repofacts, "remotes") as remote_read, \
                mock.patch.object(repofacts, "visibility") as vis_read:
            ok, why = dispatches._data_terms_rung("seat-under-test",
                                                  self.repo, self.side)
        self.assertTrue(ok)
        self.assertIsNone(why)
        remote_read.assert_not_called()
        vis_read.assert_not_called()


class TheHelpSaysPrivacyIsPerCommitTest(unittest.TestCase):
    """The dispatch help states the rule the door enforces."""

    def test_the_dispatch_help_names_the_public_branch_rule(self):
        from helm import cli_help
        text = cli_help._VERB_HELP["dispatch"]
        self.assertIn("send, add and retip REFUSE", text)
        self.assertIn("not on any PUBLIC branch", text)
        self.assertNotIn("when its repository has no PUBLIC GitHub remote",
                         text)


class TheRecipientsRouteIsReadMeasuredFirstTest(unittest.TestCase):
    """WHICH route a recipient runs decides the terms: the measured route when
    proxywatch has stamped one, else the model the seat was launched on, AND
    the family DEFAULT route beside it, because a per-model family gives CC's
    built-in subagent ids to its default block whatever alias the seat
    launched on."""

    def terms(self, family, measured=None, persisted=None, table=None):
        extra = {family: table} if table is not None else {}
        with mock.patch.dict(seat_catalog.FAMILIES, extra), \
                mock.patch.object(dispatches, "_verified_family",
                                  return_value=family), \
                mock.patch.object(seat_lifecycle, "measured_seat_route",
                                  return_value=measured) as measured_read, \
                mock.patch.object(seat, "_persisted_model",
                                  return_value=persisted):
            got = dispatches._recipient_data_terms("seat-under-test")
        return got, measured_read

    @staticmethod
    def safe_default_table():
        """A family whose DEFAULT route is private-code-safe, with ONE
        public-code-only alias a seat must choose to run."""
        table = _class_table(default=False)
        table["model"] = "x-code"
        table["model_providers"]["x-two"]["default"] = True
        return table

    def test_the_family_default_runs_the_class(self):
        got, _m = self.terms(FAMILY)
        self.assertEqual(got, (seat_catalog.DATA_TERMS_PUBLIC_ONLY, CLASS,
                               FAMILY))

    def test_a_safe_alias_still_hands_its_subagents_the_class(self):  # noqa: VACUOUS_ASSERTION — the control asserts the frontmatter block IS the class first, and each subtest asserts an exact tuple
        """THE SUBAGENT ROUTE. or-code and or-deep are private-code-safe, but
        the openrouter default block — the class — is the only block the
        built-in subagent ids ride, so a seat launched on either still sends
        an Explore child's reads of the checkout to the router."""
        fam = seat.FAMILIES[FAMILY]
        default = seat_catalog.family_default_provider(fam)
        self.assertEqual(fam["model_providers"][default]["alias"], CLASS)
        for over in ({"measured": {"model": "or-code"}},
                     {"persisted": "or-deep"}):
            with self.subTest(**over):
                got, _m = self.terms(FAMILY, **over)
                self.assertEqual(got, (seat_catalog.DATA_TERMS_PUBLIC_ONLY,
                                       CLASS, FAMILY))

    def test_a_measured_route_outranks_the_declared_default(self):
        table = self.safe_default_table()
        got, _m = self.terms("xfam", measured={"model": "x-class"},
                             table=table)
        self.assertEqual(got, (seat_catalog.DATA_TERMS_PUBLIC_ONLY,
                               "x-class", "xfam"))
        # CONTROL: a measured SAFE route outranks a persisted public-only one
        got, _m = self.terms("xfam", measured={"model": "x-code"},
                             persisted="x-class", table=table)
        self.assertEqual(got, (seat_catalog.DATA_TERMS_SAFE, "x-code",
                               "xfam"))

    def test_a_persisted_launch_model_outranks_the_default(self):
        table = self.safe_default_table()
        got, _m = self.terms("xfam", persisted="x-class", table=table)
        self.assertEqual(got, (seat_catalog.DATA_TERMS_PUBLIC_ONLY,
                               "x-class", "xfam"))
        # CONTROL: the safe default alone reads safe
        got, _m = self.terms("xfam", table=table)
        self.assertEqual(got, (seat_catalog.DATA_TERMS_SAFE, "x-code",
                               "xfam"))

    def test_a_family_with_no_public_only_route_reads_nothing_more(self):
        got, measured_read = self.terms("codex")  # noqa: SEAT_NAME — a catalog FAMILY with no per-model table, the fast path's subject
        self.assertEqual(got, (None, None, "codex"))  # noqa: SEAT_NAME — the same catalog family, echoed
        measured_read.assert_not_called()


class TheDispatchDoorAsksTheRungTest(_landreq.LandReqBase):
    """Through the one writer every dispatch door shares (`_base`)."""

    def add(self, lane):
        # FORCE ON PURPOSE: it clears the roster and usability rungs for a
        # synthetic recipient, and the data-terms rung must NOT yield to it —
        # `force` says a seat is unreachable, never that its model may read.
        work, why = tasks.add("public-route review fixture", "integrator",
                              project="helm-test", force_new=True)
        self.assertIsNone(why, why)
        return dispatches.add("seat-under-test", lane, ref=self.side,
                              repo=self.repo, kind="review", new_work=True,
                              notify=False, force=True, _reason=True,
                              task=work["id"])

    def test_a_public_only_recipient_is_refused_this_remoteless_checkout(self):
        with mock.patch.object(dispatches, "_recipient_data_terms",
                               return_value=(
                                   seat_catalog.DATA_TERMS_PUBLIC_ONLY,
                                   CLASS, FAMILY)):
            row, err = self.add("lane/or-free-private")
        self.assertIsNone(row)
        self.assertIn(seat_catalog.DATA_TERMS_PUBLIC_ONLY, err)
        self.assertIn("no GitHub remote", err)
        # CONTROL through the same door: the same recipient, whose name
        # resolves to no family with a public-only route, writes the row.
        row, err = self.add("lane/or-free-control")
        self.assertIsNone(err, err)
        self.assertEqual(row["lane"], "or-free-control")


class TheGeneratorMintsTheClassRouteTest(unittest.TestCase):
    """helm's OWN mint writes the class route and helm's OWN config checks
    read it clean — in a temporary helm home, never the live seat."""

    KEY = "sk-or-v1-TEST-NOT-A-KEY"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-or-free-mint-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm-home"),
            "HELM_PROC": os.path.join(self.tmp, "proc"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat")})
        env.start()
        self.addCleanup(env.stop)
        for key in ("OPENROUTER_API_KEY", "HELM_CHAT_NAME",
                    "HELM_SEAT_STORAGE", "HELM_CHAT_ROOM",
                    "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM",
                    "MELD_CHAT_ROOM_SOURCE", "MELD_HOME"):
            os.environ.pop(key, None)
        pin_suite_guard(self, self.tmp)
        timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        timer.start()
        self.addCleanup(timer.stop)

    def mint(self):
        envfile = os.path.join(self.tmp, "openrouter.env")
        with open(envfile, "w", encoding="utf-8") as f:
            f.write("OPENROUTER_API_KEY=%s\n" % self.KEY)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["add", FAMILY, "--key-from", envfile])
        return rc, out.getvalue(), err.getvalue()

    def test_the_mint_routes_the_class_and_the_checks_read_it_clean(self):  # noqa: VACUOUS_ASSERTION — rc 0, the class block's exact row count, the launch model and the window are all asserted EQUAL unconditionally before and beside the clean reads
        rc, out, err = self.mint()
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self.KEY, out + err)       # the key is never printed
        fam = seat.FAMILIES[FAMILY]
        path = os.path.join(seat.seat_dir(FAMILY), "config.yaml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        rows = seat_launch_assets.config_model_rows(_compat_block(text))
        # the class route is its own block, and the subagent frontmatter ids
        # ride the DEFAULT block, which is now the class
        self.assertEqual(
            sum(1 for provider, model in rows
                if (provider, model) == ("openrouter-free", ROUTER)),
            len(seat_catalog.CC_AGENT_FRONTMATTER_MODELS) + 1)
        # helm's own regeneration plan: a fixpoint, no alias drift
        plan = seat_launch_assets.proxy_config_plan(path, FAMILY)
        self.assertFalse(plan["changed"])
        self.assertIsNone(plan["alias_drift"])
        # helm's own start preflight (money, terms, credential isolation),
        # against the vendor listing's shape for every mapped id
        listing = {row["upstream_model"]:
                   {"pricing": {"prompt": "0", "completion": "0"},
                    "supported_parameters": ["tools"]}
                   for row in fam["model_providers"].values()}
        gate, notes = seat_launch_assets.family_start_refusal(
            FAMILY, fam, text, listing=listing)
        self.assertIsNone(gate)
        self.assertEqual(notes, ())
        # `helm seat doctor`'s config-drift lines, over this temp home
        self.assertEqual(seat_health._config_drift_lines(), [])
        # and the minted launch script starts the class, on its own window
        with open(os.path.join(seat.seat_dir(FAMILY), "launch.sh"),
                  encoding="utf-8") as f:
            launch = f.read()
        self.assertEqual(seat_health._model_from_launch_text(launch), CLASS)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d"
                      % fam["model_context"][CLASS], launch)
        self.assertNotIn(self.KEY, launch)


if __name__ == "__main__":
    unittest.main()
