"""Canonical checked Seat reachability — lifecycle and hostile-world arms."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helm import seat_reachability as reach


class ReachabilityBase(unittest.TestCase):
    def seat(self, actor, seat, canonical, state="active", aliases=()):
        labels = [{"display_label": canonical,
                   "normalized_label": canonical.casefold(),
                   "role": "canonical", "state": ("retired" if state ==
                                                     "retired" else "active")}]
        labels.extend({"display_label": alias,
                       "normalized_label": alias.casefold(),
                       "role": "alias", "state": ("retired" if state ==
                                                    "retired" else "active")}
                      for alias in aliases)
        return {"actor_id": actor, "seat_bindings": [{
            "seat_id": seat, "seat_state": state, "labels": labels}]}

    def snap(self, parties, authority, roster=None, presence=(), agents=None,
             successors=None, errors=()):
        return reach.snapshot_from_evidence(
            parties, authority=authority, roster=roster or {},
            presence=presence, agents=agents if agents is not None else
            {"by_seat": {}, "by_pid": {}, "home_by_pid": {}},
            successors={} if successors is None else successors,
            errors=errors, captured_at=123.0)

    def one(self, token, authority, **kw):
        result = reach.check_action(
            self.snap((("party", token),), authority, **kw))
        self.assertEqual(len(result.results), 1,
                         "positive control: the named party was evaluated")
        return result.results[0]


class LivingSeatTest(ReachabilityBase):
    def test_verified_native_presence_is_LIVE(self):
        authority = {"a": self.seat("a", "s", "native")}
        row = self.one("native", authority,
                       presence=({"seat": "native", "presence": "fresh"},))
        self.assertEqual(row.state, reach.LIVE)
        self.assertIn("fresh", " ".join(row.evidence))

    def test_verified_quiet_presence_is_QUIET(self):
        authority = {"a": self.seat("a", "s", "quiet-seat")}
        row = self.one("quiet-seat", authority,
                       presence=({"seat": "quiet-seat", "presence": "quiet"},))
        self.assertEqual(row.state, reach.QUIET)
        self.assertIn("quiet", " ".join(row.evidence))

    def test_verified_proxy_process_is_LIVE(self):
        authority = {"a": self.seat("a", "s", "codex")}
        roster = {"codex": {"session": "sid", "runtime_sessions": {
            "sid": {"runtime": {"backend": "proxy", "family": "codex"},
                    "verified": True, "source": "proxywatch"}}}}
        agents = {"by_seat": {"codex": [42]}, "by_pid": {42: ["codex"]},
                  "home_by_pid": {42: None}}
        row = self.one("codex", authority, roster=roster, agents=agents)
        self.assertEqual(row.state, reach.LIVE)
        self.assertIn("pid 42", " ".join(row.evidence))

    def test_proxy_quiet_is_positive_not_absence(self):
        authority = {"a": self.seat("a", "s", "codex")}
        row = self.one("codex", authority,
                       presence=({"seat": "codex", "presence": "quiet"},))
        self.assertEqual(row.state, reach.QUIET)
        self.assertIn("quiet", " ".join(row.evidence))
        self.assertNotEqual(row.state, reach.ABSENT)

    def test_projection_miss_is_UNKNOWN_not_absent(self):
        authority = {"a": self.seat("a", "s", "unrostered-native")}
        row = self.one("unrostered-native", authority)
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("projection miss", " ".join(row.errors))

    def test_identity_unverified_presence_is_UNKNOWN(self):
        authority = {"a": self.seat("a", "s", "split")}
        row = self.one("split", authority,
                       presence=({"seat": "split", "presence": "unverified"},))
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("identity-unverified", " ".join(row.errors))

    def test_family_blind_proxy_collision_is_UNKNOWN(self):
        authority = {"a": self.seat("a", "s", "shared")}
        roster = {"shared": {"session": "proxy", "runtime_sessions": {
            "native": {"runtime": {"backend": "native", "family": "claude"},
                       "verified": True},
            "proxy": {"runtime": {"backend": "proxy", "family": "codex"},
                      "verified": True}}}}
        agents = {"by_seat": {"shared": [41, 42]},
                  "by_pid": {41: ["shared"], 42: ["shared"]},
                  "home_by_pid": {41: None, 42: None}}
        row = self.one("shared", authority, roster=roster, agents=agents)
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("family-blind", " ".join(row.errors))

    def test_unverified_proxy_identity_blocks_a_positive_agent_hit(self):
        authority = {"a": self.seat("a", "s", "codex")}
        roster = {"codex": {"session": "sid", "runtime_sessions": {
            "sid": {"runtime": {"backend": "proxy", "family": "codex"},
                    "verified": False}}}}
        agents = {"by_seat": {"codex": [42]}, "by_pid": {42: ["codex"]},
                  "home_by_pid": {42: None}}
        row = self.one("codex", authority, roster=roster, agents=agents)
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("unverified proxy", " ".join(row.errors))

    def test_mute_governs_waking_not_delivery_or_reachability(self):
        authority = {"a": self.seat("a", "s", "muted")}
        roster = {"muted": {"mute": ["main"]}}
        row = self.one("muted", authority, roster=roster,
                       presence=({"seat": "muted", "presence": "fresh"},))
        self.assertEqual(row.state, reach.LIVE)
        self.assertIn("fresh", " ".join(row.evidence))


class IdentityLifecycleTest(ReachabilityBase):
    def test_rename_alias_resolves_the_same_durable_seat(self):
        authority = {"a": self.seat("a", "s", "new-name",
                                     aliases=("old-name",))}
        row = self.one("old-name", authority,
                       presence=({"seat": "new-name", "presence": "quiet"},))
        self.assertEqual(row.state, reach.QUIET)
        self.assertEqual(row.actionable, "new-name")
        self.assertEqual(row.seat_id, "s")

    def test_exact_tokens_never_substring_match(self):  # noqa: VACUOUS_ASSERTION — exact-token LIVE control runs first on the same authority and projection
        authority = {"a": self.seat("a", "s", "foobar")}
        evidence = ({"seat": "foobar", "presence": "fresh"},)
        exact = self.one("foobar", authority, presence=evidence)
        self.assertEqual(exact.state, reach.LIVE,
                         "positive control: the exact token reaches the row")
        row = self.one("foo", authority, presence=evidence)
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIsNone(row.seat_id)

    def test_alias_to_canonical_successor_edge_is_consumed_once(self):
        authority = {"a": self.seat("a", "s", "new", aliases=("old",))}
        row = self.one("old", authority, successors={"old": "new"},
                       presence=({"seat": "new", "presence": "quiet"},))
        self.assertEqual(row.state, reach.QUIET)
        self.assertEqual(row.actionable, "new")
        self.assertEqual(row.chain, ("old", "new"))
        self.assertNotIn("cyclic", " ".join(row.errors))

    def test_retired_alias_follows_the_canonical_successor(self):
        authority = {
            "a": self.seat("a", "old-seat", "new", state="retired",
                           aliases=("old",)),
            "b": self.seat("b", "seat-b", "seat-b"),
        }
        row = self.one("old", authority, successors={"new": "seat-b"},
                       presence=({"seat": "seat-b", "presence": "fresh"},))
        self.assertEqual(row.state, reach.LIVE)
        self.assertEqual(row.actionable, "seat-b")
        self.assertEqual(row.chain, ("old", "new", "seat-b"))
        self.assertNotEqual(row.state, reach.ABSENT)

    def test_stale_alias_edge_does_not_hijack_the_canonical_token(self):
        authority = {"a": self.seat("a", "s", "new", state="retired",
                                    aliases=("old",))}
        row = self.one("new", authority, successors={"old": "missing"})
        self.assertEqual(row.state, reach.ABSENT)
        self.assertEqual(row.chain, ("new",))

    def test_terminal_authoritative_retirement_is_ABSENT(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        row = self.one("retired", authority)
        self.assertEqual(row.state, reach.ABSENT)
        self.assertIn("strict obligation", " ".join(row.evidence))
        self.assertEqual(row.errors, ())

    def test_terminal_retirement_conflicting_with_current_presence_is_UNKNOWN(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        row = self.one("retired", authority,
                       presence=({"seat": "retired", "presence": "fresh"},))
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("conflicts", " ".join(row.errors))

    def test_unreadable_authority_is_UNKNOWN_not_absent(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        row = self.one("retired", authority,
                       errors=(("authority", "ledger unreadable"),))
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("ledger unreadable", " ".join(row.errors))

    def test_ambiguous_rename_history_is_UNKNOWN(self):
        authority = {
            "a": self.seat("a", "s1", "one", aliases=("old",)),
            "b": self.seat("b", "s2", "two", aliases=("old",)),
        }
        row = self.one("old", authority)
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("2 durable", " ".join(row.errors))

    def test_conflicting_duplicate_stable_identity_is_UNKNOWN(self):
        active = self.seat("a", "s", "same")["seat_bindings"][0]
        retired = self.seat("a", "s", "same", state="retired")[
            "seat_bindings"][0]
        authority = {"a": {"actor_id": "a",
                            "seat_bindings": [active, retired]}}
        row = self.one("same", authority)
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("conflicting duplicate", " ".join(row.errors))
        self.assertNotEqual(row.state, reach.ABSENT)

    def test_conflicting_duplicate_stable_identity_is_censused_before_token_filter(self):
        active = self.seat("a", "s", "same")["seat_bindings"][0]
        retired_other = self.seat("a", "s", "other", state="retired")[
            "seat_bindings"][0]
        authority = {
            "first-row": {"actor_id": "a", "seat_bindings": [active]},
            "second-row": {"actor_id": "a", "seat_bindings": [retired_other]},
        }
        row = self.one("same", authority,
                       presence=({"seat": "same", "presence": "fresh"},))
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("conflicting duplicate", " ".join(row.errors))
        self.assertNotEqual(row.state, reach.LIVE)

    def test_cycle_is_UNKNOWN(self):
        authority = {
            "a": self.seat("a", "s1", "old", state="retired"),
            "b": self.seat("b", "s2", "new", state="retired"),
        }
        row = self.one("old", authority,
                       successors={"old": "new", "new": "old"})
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("cyclic", " ".join(row.errors))

    def test_split_successor_is_UNKNOWN(self):
        authority = {"a": self.seat("a", "s", "old", state="retired")}
        row = self.one("old", authority,
                       successors={"old": ("new-a", "new-b")})
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("ambiguous", " ".join(row.errors))

    def test_decommission_with_successor_follows_the_actionable_successor(self):
        authority = {
            "a": self.seat("a", "s1", "old", state="retired"),
            "b": self.seat("b", "s2", "new"),
        }
        row = self.one("old", authority, successors={"old": "new"},
                       presence=({"seat": "new", "presence": "fresh"},))
        self.assertEqual(row.state, reach.LIVE)
        self.assertEqual(row.actionable, "new")
        self.assertEqual(row.chain, ("old", "new"))

    def test_decommission_alone_never_clears_an_unresolved_successor_obligation(self):
        authority = {"a": self.seat("a", "s", "old", state="retired")}
        row = self.one("old", authority, successors={"old": "missing"})
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertNotEqual(row.state, reach.ABSENT)
        self.assertIn("missing", row.chain)


class ActionSnapshotTest(ReachabilityBase):
    def test_every_party_is_evaluated_and_one_UNKNOWN_blocks_authorization(self):
        authority = {
            "a": self.seat("a", "s1", "dead", state="retired"),
            "b": self.seat("b", "s2", "live"),
        }
        snap = self.snap((reach.Party("sender", "dead"),
                          reach.Party("recipient", "live"),
                          reach.Party("custodian", "missing")), authority,
                         presence=({"seat": "live", "presence": "fresh"},))
        action = reach.check_action(snap)
        self.assertEqual([row.state for row in action.results],
                         [reach.ABSENT, reach.LIVE, reach.UNKNOWN])
        self.assertFalse(action.authorized_absent)
        self.assertIn("every actionable party", action.refusal)
        self.assertEqual(snap.captured_at, 123.0)

    def test_all_authoritatively_retired_parties_authorize_absence(self):
        authority = {
            "a": self.seat("a", "s1", "sender", state="retired"),
            "b": self.seat("b", "s2", "recipient", state="retired"),
        }
        action = reach.check_action(self.snap(
            (("sender", "sender"), ("recipient", "recipient")), authority))
        self.assertTrue(action.authorized_absent)
        self.assertIsNone(action.refusal)

    def test_dispatch_adapter_models_custody_as_a_successor_not_a_third_vote(self):
        parties, successors = reach.dispatch_parties({
            "sender": "old", "recipient": "reviewer", "custodian": "new"})
        self.assertEqual(parties, (reach.Party("sender", "old"),
                                   reach.Party("recipient", "reviewer")))
        self.assertEqual(successors, {"old": ("new",)})

    def test_empty_dispatch_parties_are_explicit_UNKNOWN_results(self):
        authority = {
            "a": self.seat("a", "s1", "<missing sender>", state="retired"),
            "b": self.seat("b", "s2", "<missing recipient>", state="retired"),
        }

        def provider():
            return authority, None

        from helm import beacons, seats_report, seats_roster
        with mock.patch.object(seats_roster, "roster_acquired",
                               return_value=({}, False)), \
                mock.patch.object(seats_report, "presence_report",
                                  return_value=[]), \
                mock.patch.object(beacons, "agent_index", return_value={
                    "by_seat": {}, "by_pid": {}, "home_by_pid": {}}):
            snap = reach.capture_dispatch(
                {"sender": "", "recipient": ""}, forwarding={}, now=123.0,
                authority_provider=provider)
        action = reach.check_action(snap)
        self.assertEqual([row.role for row in action.results],
                         ["sender", "recipient"])
        self.assertEqual([row.state for row in action.results],
                         [reach.UNKNOWN, reach.UNKNOWN])
        self.assertIn("sender identity is absent",
                      " ".join(action.results[0].errors))
        self.assertIn("recipient identity is absent",
                      " ".join(action.results[1].errors))
        self.assertFalse(action.authorized_absent)

    def test_malformed_dispatch_identity_is_UNKNOWN_not_string_coerced(self):
        snap = reach.capture_dispatch(
            {"sender": 42, "recipient": "reviewer"}, forwarding={}, now=123.0)
        action = reach.check_action(snap)
        self.assertFalse(action.authorized_absent)
        self.assertEqual(action.results[0].state, reach.UNKNOWN)
        self.assertIn("dispatch sender", " ".join(action.results[0].errors))

    def test_snapshot_schema_is_replayable_and_captures_negative_separately(self):
        snap = self.snap((("party", "gone"),), {},
                         errors=(("presence", "unreadable"),))
        replay = snap.as_dict()
        self.assertEqual(replay["version"], reach.SNAPSHOT_VERSION)
        self.assertTrue(replay["successors_complete"])
        self.assertEqual(replay["errors"], [("presence", "unreadable")])
        rebuilt = reach.snapshot_from_evidence(**replay)
        self.assertEqual(rebuilt.as_dict(), replay)
        self.assertEqual(reach.check_action(rebuilt).as_dict(),
                         reach.check_action(snap).as_dict())
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("presence: unreadable", row.errors)

    def test_snapshot_deep_freezes_input_and_between_party_evidence(self):  # noqa: VACUOUS_ASSERTION — two captured parties and a complete frontier are unconditional controls before both ABSENT classifications
        authority = {"a": self.seat("a", "s", "dead", state="retired")}
        snap = self.snap((("one", "dead"), ("two", "dead")), authority)
        self.assertEqual(len(snap.parties), 2,
                         "positive control: both parties were captured")
        self.assertTrue(snap.successors_complete,
                        "positive control: absence has a complete frontier")
        authority["a"]["seat_bindings"][0]["seat_state"] = "active"
        first = reach.check_party(snap, snap.parties[0])
        self.assertEqual(first.state, reach.ABSENT)
        with self.assertRaises(TypeError):
            snap.authority["a"]["seat_bindings"][0]["seat_state"] = "active"
        with self.assertRaises(TypeError):
            dict.__setitem__(snap.authority, "forged", {})
        with self.assertRaises(TypeError):
            dict.update(snap.authority["a"], {"seat_bindings": ()})
        second = reach.check_party(snap, snap.parties[1])
        self.assertEqual(second.state, reach.ABSENT)

    def test_empty_complete_successor_frontier_can_prove_retirement(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        snap = reach.snapshot_from_evidence(
            (("party", "retired"),), authority=authority, roster={},
            presence=(), agents={"by_seat": {}, "by_pid": {},
                                 "home_by_pid": {}}, successors={},
            captured_at=123.0)
        self.assertTrue(snap.successors_complete,
                        "positive control: empty mapping is a complete frontier")
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.ABSENT)

    def test_omitted_successor_frontier_cannot_authorize_retirement(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        snap = reach.snapshot_from_evidence(
            (("party", "retired"),), authority=authority, roster={},
            presence=(), agents={"by_seat": {}, "by_pid": {},
                                 "home_by_pid": {}}, captured_at=123.0)
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("cannot prove no successor", " ".join(row.errors))

    def test_falsy_or_empty_target_successor_forms_are_UNKNOWN(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        control = reach.snapshot_from_evidence(
            (("party", "retired"),), authority=authority, roster={},
            presence=(), agents={"by_seat": {}, "by_pid": {},
                                 "home_by_pid": {}}, successors={},
            captured_at=123.0)
        self.assertTrue(control.successors_complete,
                        "positive control: valid empty frontier is complete")
        self.assertEqual(reach.check_action(control).results[0].state,
                         reach.ABSENT)
        for successors in (set(), {"retired": None}, {"retired": ()},
                           {"retired": ""}):
            with self.subTest(successors=successors):
                snap = reach.snapshot_from_evidence(
                    (("party", "retired"),), authority=authority, roster={},
                    presence=(), agents={"by_seat": {}, "by_pid": {},
                                         "home_by_pid": {}},
                    successors=successors, captured_at=123.0)
                row = reach.check_action(snap).results[0]
                self.assertEqual(row.state, reach.UNKNOWN)
                self.assertFalse(snap.successors_complete)
                self.assertIn("successor", " ".join(row.errors))

    def test_missing_agent_snapshot_is_UNKNOWN_not_absent(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        snap = reach.snapshot_from_evidence(
            (("party", "retired"),), authority=authority, roster={},
            presence=(), agents=None, successors={}, captured_at=123.0)
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("agent snapshot", " ".join(row.errors))

    def test_empty_object_agent_census_is_UNKNOWN_not_proven_empty(self):
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        complete = reach.snapshot_from_evidence(
            (("party", "retired"),), authority=authority, roster={},
            presence=(), agents={"by_seat": {}, "by_pid": {},
                                 "home_by_pid": {}},
            successors={}, captured_at=123.0)
        self.assertEqual(reach.check_action(complete).results[0].state,
                         reach.ABSENT,
                         "positive control: complete empty census proves no agent")
        malformed = reach.snapshot_from_evidence(
            (("party", "retired"),), authority=authority, roster={},
            presence=(), agents={}, successors={}, captured_at=123.0)
        row = reach.check_action(malformed).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("by_seat index", " ".join(row.errors))
        self.assertNotEqual(row.state, reach.ABSENT)

    def test_capture_requires_an_explicit_authority_capability(self):
        snap = reach.capture((("party", "anything"),), successors={}, now=123.0)
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn(reach.AUTHORITY_CAPABILITY, " ".join(row.errors))
        self.assertEqual(snap.authority, {})

    def test_capture_uses_the_checked_authority_provider_capability(self):
        from helm import beacons, seats_report, seats_roster
        authority = {"a": self.seat("a", "s", "retired", state="retired")}
        called = []

        def provider():
            called.append("authority")
            return authority, None

        with mock.patch.object(seats_roster, "roster_acquired",
                               return_value=({}, False)), \
                mock.patch.object(seats_report, "presence_report",
                                  return_value=[]), \
                mock.patch.object(beacons, "agent_index", return_value={
                    "by_seat": {}, "by_pid": {}, "home_by_pid": {}}):
            snap = reach.capture((("party", "retired"),), successors={},
                                 now=123.0, authority_provider=provider)
        self.assertEqual(called, ["authority"])
        self.assertIn("a", snap.authority,
                      "positive control: checked authority was captured")
        self.assertTrue(snap.successors_complete,
                        "positive control: absence has a complete frontier")
        self.assertEqual(reach.check_action(snap).results[0].state,
                         reach.ABSENT)

    def test_capture_rejects_disagreeing_agent_indexes(self):
        from helm import beacons, seats_report, seats_roster
        authority = {"a": self.seat("a", "s", "retired", state="retired")}

        def provider():
            return authority, None

        agents = {"by_seat": {}, "by_pid": {42: ["retired"]},
                  "home_by_pid": {42: None}}
        with mock.patch.object(seats_roster, "roster_acquired",
                               return_value=({}, False)), \
                mock.patch.object(seats_report, "presence_report",
                                  return_value=[]), \
                mock.patch.object(beacons, "agent_index",
                                  return_value=agents):
            snap = reach.capture((("party", "retired"),), successors={},
                                 now=123.0, authority_provider=provider)
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("indexes disagree", " ".join(row.errors))
        self.assertNotEqual(row.state, reach.ABSENT)

    def test_capture_rejects_disagreeing_agent_pid_domains(self):
        from helm import beacons, seats_report, seats_roster
        authority = {"a": self.seat("a", "s", "retired", state="retired")}

        def provider():
            return authority, None

        agents = {"by_seat": {}, "by_pid": {42: []},
                  "home_by_pid": {}}
        with mock.patch.object(seats_roster, "roster_acquired",
                               return_value=({}, False)), \
                mock.patch.object(seats_report, "presence_report",
                                  return_value=[]), \
                mock.patch.object(beacons, "agent_index",
                                  return_value=agents):
            snap = reach.capture((("party", "retired"),), successors={},
                                 now=123.0, authority_provider=provider)
        row = reach.check_action(snap).results[0]
        self.assertEqual(row.state, reach.UNKNOWN)
        self.assertIn("pid domains disagree", " ".join(row.errors))
        self.assertNotEqual(row.state, reach.ABSENT)

    def test_malformed_snapshot_sources_become_named_UNKNOWNs(self):  # noqa: VACUOUS_ASSERTION — the named party result and five required source errors are unconditional controls
        snap = reach.snapshot_from_evidence(
            (("party", "seat"),), authority=[], roster=[], presence={},
            agents=[], successors={"seat": 7}, captured_at="not-a-clock")
        action = reach.check_action(snap)
        self.assertEqual(action.results[0].state, reach.UNKNOWN)
        errors = " ".join(action.results[0].errors)
        for source in ("authority", "roster", "presence", "agents",
                       "successors", "captured_at"):
            self.assertIn(source, errors)
        self.assertEqual(snap.captured_at, 0.0)
        self.assertFalse(snap.successors_complete)


if __name__ == "__main__":
    unittest.main()
