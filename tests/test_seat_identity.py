"""Suggestion-only seat aliases (helm/seat_identity.py) — the boundary pins.

The invariant is STRUCTURAL: the module exposes alias_refusal() -> str|None
and nothing else, so no caller can ever receive a replacement seat operand to
actuate by accident. These tests pin the plan's verification list: exact
canonicals proceed, alias evidence SUGGESTS and REFUSES, ambiguity refuses
without choosing, declarations validate loudly, unknown open-world tokens
stay legal pre-join addresses, and fuzzy hints decorate closed-world
refusals only.

Everything runs against a temporary HELM_HOME/chat dir — never the host's
real roster, spawn register, /proc, OR GIT WORKTREE LIST.

That last clause was a lie for two tests and it turned main red with no
commit. _registered_paths asks GIT for the worktrees under every repo root
reachable from `paths`, and `paths` BEGINS WITH seats.safe_cwd() — the
process's own cwd. So the pane-alias tests were answered by whatever
worktrees the real helm checkout happened to have at that minute: they
passed while helm-wt/seats/console-design existed, and went red the hour it
was reaped. Nothing in the test created that worktree, and nothing in the
test could have known it was gone. A tracked test whose oracle is the live
fleet inverts the whole point of a suite — the fleet decides whether the
tests pass, instead of the tests deciding whether the fleet is sound.
The fixture now PLANTS its own repo and seat worktree (plant_seat_worktree).
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helm import seat_identity


class SeatIdentityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seat-identity-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR",
                      "HELM_SEAT_ALIASES", "HELM_PROC")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("HELM_SEAT_ALIASES", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def plant_roster(self, *names, **kw):
        """Canonical chat identities the way the roster owns them.

        cwd= overrides the seat cwd for every planted name. It exists because
        `paths` in _canonical_sources is built from these cwds (plus the
        process cwd), and `paths` is what _registered_paths turns into repo
        roots — so a test that needs a REGISTERED worktree must be able to
        point the roster at a repo it controls.
        """
        cwd = kw.pop("cwd", None)
        assert not kw, "unexpected kwargs: %r" % sorted(kw)
        path = os.path.join(os.environ["HELM_CHAT_DIR"], ".roster.json")
        rows = {}
        for n in names:
            rows[n] = {"seat": n, "cwd": cwd or os.path.join(self.tmp, n)}
        with open(path, "w") as f:
            json.dump(rows, f)

    def plant_seat_worktree(self, alias):
        """A REAL git repo with the deterministic seat worktree for `alias`.

        _registered_paths only counts a worktree at EXACTLY
        harness.seat_worktree_path(root, alias) on refs/heads/<seat_branch>,
        so the fixture has to build a genuine one — a stub directory does not
        satisfy it and would pin a rule nobody enforces.

        Returns the repo root. Planting this is what stops the host's own
        worktree list from deciding these tests: whether the real checkout
        has (or has just reaped) helm-wt/seats/<alias> becomes irrelevant,
        because the fixture supplies the match itself.
        """
        from helm import harness
        root = os.path.join(self.tmp, "repo")
        os.makedirs(root, exist_ok=True)
        git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
               "-c", "commit.gpgsign=false"]
        run = lambda *a: subprocess.run(list(a), cwd=root, check=True,
                                        capture_output=True)
        run(*git, "init", "-q", "-b", "main")
        run(*git, "commit", "-q", "--allow-empty", "-m", "base")
        wt = harness.seat_worktree_path(root, alias)
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        run(*git, "worktree", "add", "-q", "-b", harness.seat_branch(alias), wt)
        return root


class AliasRefusalTest(SeatIdentityBase):
    def test_an_unknown_open_world_token_is_a_legal_future_address(self):
        self.plant_roster("alice")
        self.assertIsNone(seat_identity.alias_refusal("nosuchseat"))

    def test_an_exact_canonical_proceeds_even_when_declared(self):
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "owner:alice=alice"
        self.assertIsNone(seat_identity.alias_refusal("alice"))

    def test_a_declared_owner_shorthand_suggests_and_refuses(self):
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "owner:AL=alice"
        why = seat_identity.alias_refusal("AL")
        self.assertIn("did you mean alice", why)
        self.assertIn("owner shorthand", why)

    def test_the_incident_shape_is_exact(self):
        """The one historical regression the plan names publicly: an exact
        pane-label alias renders the canonical suggestion and the kind."""
        self.plant_roster("helm-claude-2")
        os.environ["HELM_SEAT_ALIASES"] = "owner:CD=helm-claude-2"
        self.assertEqual(
            seat_identity.alias_refusal("CD"),
            "unknown seat 'CD' — did you mean helm-claude-2? "
            "(CD is its owner shorthand)")

    def test_two_declarations_for_one_token_are_ambiguous_not_a_choice(self):
        self.plant_roster("alice", "bob")
        os.environ["HELM_SEAT_ALIASES"] = "owner:X=alice,owner:X=bob"
        why = seat_identity.alias_refusal("X")
        self.assertIn("ambiguous", why)
        self.assertIn("alice", why)
        self.assertIn("bob", why)

    def test_a_declaration_naming_no_canonical_seat_is_a_loud_config_error(self):
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "owner:X=nosuchseat"
        why = seat_identity.alias_refusal("X")
        self.assertIn("misconfigured", why)
        self.assertIn("nosuchseat", why)

    def test_a_malformed_entry_fails_loudly_never_silently_ignored(self):
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "garbage"
        self.assertIn("misconfigured",
                      seat_identity.alias_refusal("anything"))

    def test_an_unknown_kind_fails_loudly(self):
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "friend:X=alice"
        self.assertIn("misconfigured", seat_identity.alias_refusal("X"))

    def test_a_pane_declaration_without_a_registered_worktree_refuses_loudly(self):
        """A pane: alias claims a Git-registered deterministic worktree;
        without one the declaration itself is the error (no inventing
        evidence from a basename)."""
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "pane:not-a-worktree=alice"
        why = seat_identity.alias_refusal("not-a-worktree")
        self.assertIn("misconfigured", why)

    def test_closed_world_unknown_gets_a_fuzzy_hint_open_world_does_not(self):
        self.plant_roster("alice")
        closed = seat_identity.alias_refusal("alic", closed=True,
                                             fuzzy=["alice"])
        self.assertIsNotNone(closed)
        self.assertIn("alic", closed)
        # open-world: the same near-miss is a legal future seat
        self.assertIsNone(seat_identity.alias_refusal("alic"))

    def test_the_module_exposes_no_resolver(self):
        """The structural invariant: nothing to import that returns a
        canonical operand for a caller to actuate."""
        self.assertFalse(hasattr(seat_identity, "resolve_alias"))


class RecipientGateTest(SeatIdentityBase):
    """resolve_recipient inherits the refusal: alias DM attempts produce no
    routed identity, and unrelated valid tokens keep pre-join delivery."""

    def test_alias_dm_refuses_with_the_suggestion(self):
        from helm import seats
        self.plant_roster("alice")
        os.environ["HELM_SEAT_ALIASES"] = "owner:AL=alice"
        to, err = seats.resolve_recipient("AL")
        self.assertIsNone(to)
        self.assertIn("did you mean alice", err)

    def test_unrelated_valid_token_stays_legal_pre_join(self):
        from helm import seats
        self.plant_roster("alice")
        to, err = seats.resolve_recipient("future-seat")
        self.assertEqual(to, "future-seat")
        self.assertIsNone(err)


    def test_open_world_near_miss_of_a_roster_seat_stays_legal(self):
        """A token one edit from a ROSTER seat is still a legal future
        address — the fuzzy hint decorates CLOSED-world refusals only;
        open-world addressing never refuses on resemblance alone."""
        from helm import seats
        self.plant_roster("alice")
        to, err = seats.resolve_recipient("alic")
        self.assertEqual(to, "alic")
        self.assertIsNone(err)

    def test_exact_canonical_still_resolves(self):
        from helm import seats
        self.plant_roster("alice")
        to, err = seats.resolve_recipient("alice")
        self.assertEqual(to, "alice")
        self.assertIsNone(err)



class OperatorBoundaryTest(SeatIdentityBase):
    """The incident, end to end through the real verbs: exact console-design
    suggests helm-claude-2, identifies the kind, refuses nonzero, and writes
    nothing — no spawn record, no DM lane, no ledger row."""

    def _boundary_env(self):
        # The roster cwd points INTO the planted repo on purpose: _canonical_
        # sources builds `paths` from these cwds, _registered_paths turns
        # `paths` into repo roots, and the pane-alias branch is gated on
        # finding a registered seat worktree among them. Before this, the only
        # repo root in play came from seats.safe_cwd() — the process's own cwd
        # — so the HOST's worktree list answered the test.
        root = self.plant_seat_worktree("console-design")
        self.plant_roster("helm-claude-2", cwd=root)
        os.environ["HELM_SEAT_ALIASES"] = (
            "pane:console-design=helm-claude-2,owner:CD=helm-claude-2")

    def test_chat_dm_to_an_alias_refuses_and_sends_nothing(self):
        from helm import seats
        self._boundary_env()
        to, err = seats.resolve_recipient("console-design")
        self.assertIsNone(to)
        self.assertEqual(
            err,
            "unknown seat 'console-design' — did you mean helm-claude-2? "
            "(console-design is its pane label)")
        # no DM lane materialized for the refused alias
        import glob
        lanes = glob.glob(os.path.join(os.environ["HELM_CHAT_DIR"],
                                       "dm-console-design*"))
        self.assertEqual(lanes, [])

    def test_dispatch_add_refuses_with_the_owner_layer_reason(self):
        from unittest import mock
        from helm import dispatches
        self._boundary_env()
        # a declared author, so the ALIAS refusal under test is what fires —
        # add() itself now refuses an identityless author first (2026-08-02)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "op-fixture"}):
            row, why = dispatches.add("console-design", "some-lane",
                                      ref="0" * 40, kind="review",
                                      notify=False, _reason=True,
                                      new_work=True)
        self.assertIsNone(row)
        self.assertIn("did you mean helm-claude-2", why)

    def test_unknown_add_failure_no_longer_collapses_to_generic(self):
        """The _reason plumbing: ANY base-layer refusal reaches the CLI
        verbatim instead of 'dispatch NOT recorded'."""
        from unittest import mock
        from helm import dispatches
        self.plant_roster("alice")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "op-fixture"}):
            row, why = dispatches.add("bad token!", "lane", ref="0" * 40,
                                      kind="review", notify=False,
                                      _reason=True, new_work=True)
        self.assertIsNone(row)
        self.assertIn("bad token", why)


if __name__ == "__main__":
    unittest.main()
