"""Fake settings only: installed planner ownership, order and coverage controls."""
from copy import deepcopy
import shlex
import unittest
from unittest import mock

from helm import hooks, posttool, record

BIN = "/tmp/fake helm's directory/bin/helm"
EVENT = "PostToolUse"


class PosttoolPlanTest(unittest.TestCase):
    def setUp(self):
        self.record, self.deliver = posttool.installed_specs()
        self.specs = (self.record, self.deliver)
        self.foreign = {"type": "command", "command": "foreign --keep", "tag": [1, {"a": 2}]}
        self.failure = {"matcher": "Bash", "hooks": [{
            "type": "command", "command": "failure --keep", "custom": True}], "extra": [3]}

    def leaf(self, spec, historic=False, **changes):
        command = ("timeout %d %s %s || true" % (
            spec["timeout"], shlex.quote(BIN), spec["args"]) if historic
            else hooks.spec_command(spec, executable=BIN))
        return dict({"type": "command", "command": command,
                     "timeout": hooks._gate_outer_timeout(spec)}, **changes)

    def settings(self, *leaves):
        return {"permissions": {"allow": ["keep"]}, "other": [1, {"x": 2}],
                "hooks": {EVENT: [{"hooks": list(leaves)}],
                          "PostToolUseFailure": [deepcopy(self.failure)]}}

    def plan(self, settings, specs=None):
        before = deepcopy(settings)
        requested = deepcopy(self.specs if specs is None else specs)
        snapshot = deepcopy(requested)
        with mock.patch.object(hooks, "helm_bin", side_effect=AssertionError("filesystem discovery")):
            result = posttool.prepare(settings, requested, executable=BIN)
        self.assertEqual(settings, before)
        self.assertEqual(requested, snapshot)
        return result

    def complete(self, data, specs=None):
        before = deepcopy(data)
        with mock.patch.object(hooks, "helm_bin", return_value=BIN), \
                mock.patch.object(hooks, "repair_lane_room_commands", return_value=[]):
            out, actions = hooks._merge_all(data, self.specs if specs is None else specs)
        self.assertEqual(data, before)
        return out, actions

    def assert_composite(self, out):
        leaves = [h for g in out.get("hooks", {}).get(EVENT, []) for h in g["hooks"]]
        composites = [h for h in leaves if h.get("command") ==
                      posttool.entry(executable=BIN)["hooks"][0]["command"]]
        self.assertEqual(len(composites), 1)
        self.assertEqual(composites[0]["timeout"], self.record["timeout"]
                         + 2 * self.deliver["timeout"] + 2 * hooks.GRACE_S)
        self.assertEqual(posttool.covered_members(out, executable=BIN), {"record", "deliver"})
        return leaves

    def refuse(self, data, reason, specs=None):
        before = deepcopy(data)
        with self.assertRaisesRegex(ValueError, reason):
            self.plan(data, specs)
        self.assertEqual(data, before)

    def test_empty_both(self):
        out, remaining, actions = self.plan({})
        self.assert_composite(out)
        self.assertEqual(remaining, ())
        self.assertEqual(actions, {"record": "add", "deliver": "add"})

    def test_single_populations_stay_single(self):
        for spec in self.specs:
            with self.subTest(name=spec["name"]):
                out, remaining, actions = self.plan({}, (spec,))
                self.assertEqual(out, {})
                self.assertEqual(remaining, (spec,))
                self.assertEqual(actions, {})
                data = self.settings(self.leaf(spec))
                self.assertEqual(self.plan(data, (spec,))[0], data)

    def test_both_installer_orders_keep_foreign_position(self):
        for existing, requested in (self.specs, self.specs[::-1]):
            with self.subTest(existing=existing["name"]):
                data = self.settings(deepcopy(self.foreign), self.leaf(existing),
                                     {"type": "command", "command": "after"})
                out, remaining, actions = self.plan(data, (requested,))
                leaves = self.assert_composite(out)
                self.assertEqual(leaves[0], self.foreign)
                self.assertEqual(leaves[-1], data["hooks"][EVENT][0]["hooks"][-1])
                self.assertEqual(out["hooks"]["PostToolUseFailure"], [self.failure])
                self.assertEqual(len(leaves), 3)
                self.assertEqual(remaining, ())
                self.assertEqual(actions, {requested["name"]: "update"})

    def test_existing_adjacent_pair_narrowed_request(self):
        for requested in ((self.record,), (self.deliver,), ()):
            data = self.settings(self.leaf(self.record), self.leaf(self.deliver), deepcopy(self.foreign))
            out, remaining, actions = self.plan(data, requested)
            leaves = self.assert_composite(out)
            self.assertEqual(leaves[-1], self.foreign)
            self.assertEqual(len(leaves), 2)
            self.assertEqual(remaining, ())
            self.assertEqual(set(actions), {s["name"] for s in requested})

    def test_cross_group_pair_and_original_empty_unknown_group(self):
        data = self.settings()
        data["hooks"][EVENT] = [
            {"hooks": [deepcopy(self.foreign)]},
            {"hooks": [self.leaf(self.record)]},
            {"hooks": [], "unknown": {"preserve": True}},
            {"matcher": "*", "hooks": [self.leaf(self.deliver)]},
            {"matcher": "Read", "hooks": [deepcopy(self.foreign)], "unknown": 7}]
        out, _, _ = self.plan(data)
        self.assert_composite(out)
        self.assertEqual(out["hooks"][EVENT][0], data["hooks"][EVENT][0])
        self.assertEqual(out["hooks"][EVENT][2], data["hooks"][EVENT][2])
        self.assertEqual(out["hooks"][EVENT][-1], data["hooks"][EVENT][-1])
        self.assertEqual(len(out["hooks"][EVENT]), 4)

    def test_whole_commands_spaced_apostrophe_paths_and_history(self):
        for spec in self.specs:
            for historic in (False, True):
                leaf = self.leaf(spec, historic)
                self.assertEqual(posttool.recognize(leaf["command"]), (spec["name"], spec["timeout"]))
        data = self.settings(self.leaf(self.record, True), self.leaf(self.deliver, True))
        for leaf in data["hooks"][EVENT][0]["hooks"]:
            del leaf["timeout"]
        self.assert_composite(self.plan(data)[0])
        for command in ("timeout 10 helm record --hook-json || true",
                        "timeout 10 ./helm record --hook-json || true",
                        "timeout 10 /tmp/helm record --hook-json --extra || true",
                        "timeout 10 /tmp/helm record --hook-json || true; foreign",
                        "env X=1 timeout 10 /tmp/helm record --hook-json || true"):
            self.assertIsNone(posttool.recognize(command))

    def test_every_rendering_helm_has_written_is_still_owned(self):  # noqa: VACUOUS_ASSERTION — `assertTrue(hooks.HISTORICAL_COMMANDS)` and the exact `len(written)` equality are unconditional, so an empty history fails rather than making every loop body vacuous
        """THE SIBLING OF THE DEFECT CURED IN `cred`, one layer over.

        An installed leaf carries the ladder that was current when it was
        written. Asking today's template alone makes ownership expire the next
        time the template changes: `recognize` returns None, `protected` then
        returns True, and a standalone member helm ITSELF wrote is refused as
        foreign and left in place. Nothing breaks loudly — the estate simply
        keeps leaves helm believes it converted.
        """
        self.assertTrue(hooks.HISTORICAL_COMMANDS,
                        "MUST-HIT: there are no historical renderings to read, "
                        "so every assertion below is about an empty set")
        written = [(spec, command)
                   for spec in self.specs
                   for command in (h(spec, BIN) for h in hooks.HISTORICAL_COMMANDS)
                   if command is not None]
        # NOT `len(specs) * len(HISTORICAL_COMMANDS)`: the history is per-KIND
        # now (a gate ladder, an advisory ladder at two vintages, the posttool
        # runner's), and a frozen renderer returns None for a spec it never
        # rendered. Both specs here are ADVISORY, so the count that has to hold
        # is "the same number for each, and at least the two advisory ladders
        # helm has written" — which still goes red if a renderer silently stops
        # producing for one of them.
        per_spec = {spec["name"]: [c for c in
                                   (h(spec, BIN)
                                    for h in hooks.HISTORICAL_COMMANDS)
                                   if c is not None]
                    for spec in self.specs}
        self.assertGreaterEqual(
            min(len(v) for v in per_spec.values()), 2,
            "MUST-HIT: fewer renderings than the two advisory ladders helm "
            "has written, so the ownership below is about almost nothing")
        self.assertEqual(len({len(v) for v in per_spec.values()}), 1,
                         "two advisory specs are rendered by different "
                         "numbers of historical renderers: %r" % per_spec)
        self.assertEqual(len(written), sum(len(v) for v in per_spec.values()))
        for spec, command in written:
            with self.subTest(spec=spec["name"]):
                self.assertEqual(posttool.recognize(command),
                                 (spec["name"], spec["timeout"]))
                self.assertFalse(posttool.protected(command),
                                 "helm's own older rendering reads as foreign, "
                                 "so it can never be converted or removed")
        # THE CONTROL: with the record of what helm wrote taken away, every one
        # of those strings goes straight back to unowned-and-protected. The
        # ownership above is therefore the history being read, not a looser
        # match that would admit a foreign wrapper.
        with mock.patch.object(hooks, "HISTORICAL_COMMANDS", ()):
            for spec, command in written:
                with self.subTest(spec=spec["name"], control=True):
                    self.assertIsNone(posttool.recognize(command))
                    self.assertTrue(posttool.protected(command))

    def test_an_older_rendering_standalone_pair_converts_rather_than_refuses(self):  # noqa: VACUOUS_ASSERTION — `assert_composite` asserts the canonical composite is PRESENT in the planned settings; there is no absence assertion in this arm
        """The consequence, at the door the installer actually uses."""
        data = self.settings(*[
            {"type": "command",
             "command": hooks.HISTORICAL_COMMANDS[0](spec, BIN),
             "timeout": hooks._gate_outer_timeout(spec)}
            for spec in self.specs])
        self.assert_composite(self.plan(data)[0])

    def test_suffix_foreign_content_control(self):
        foreign = self.leaf(self.record, True)
        foreign["command"] += "; printf KEEP_FOREIGN_SENTINEL"
        data = self.settings(foreign, self.leaf(self.deliver))
        self.assertIn("KEEP_FOREIGN_SENTINEL", data["hooks"][EVENT][0]["hooks"][0]["command"])
        out, actions = self.complete(data)
        self.assertIn(foreign, [h for g in out["hooks"][EVENT] for h in g["hooks"]])
        self.assertEqual(actions["posttool_refusals"], ("non-whole-PostToolUse-member",))
        self.assertEqual(self.complete(out)[0], out)

    def test_unknown_wrappers_preserve_leaf_and_report_partial_refusal(self):
        for command in (["record --hook-json"], "echo 'record --hook-json'",
                        "sh -c 'helm record --hook-json'",
                        "timeout 2 /tmp/helm chat deliver --hook-json || true & foreign",
                        self.leaf(self.record)["command"] + "; foreign"):
            leaf = {"type": "command", "command": command}
            data = self.settings(leaf)
            out, actions = self.complete(data)
            self.assertIn(leaf, [h for g in out["hooks"][EVENT] for h in g["hooks"]])
            self.assertTrue(actions["posttool_refusals"])
            self.assert_composite(out)
            again, actions = self.complete(out)
            self.assertEqual(again, out)
            self.assertTrue(actions["posttool_refusals"])

    def test_duplicates_reversed_foreign_interposed(self):
        r, d = self.leaf(self.record), self.leaf(self.deliver)
        for leaves in ((r, r, d), (r, d, d), (r, r, d, d)):
            original = self.settings(*deepcopy(leaves))
            out, _, _ = self.plan(original)
            self.assertEqual(len(self.assert_composite(out)), 1)
            self.assertEqual(self.plan(out)[0], out)
        for leaves in ((d, r), (r, d, r), (r, self.foreign, d),
                       (r, self.foreign, r, d), (r, d, self.foreign, d)):
            data = self.settings(*deepcopy(leaves))
            out, remaining, actions = self.plan(data)
            self.assertEqual((out, remaining, actions), (data, self.specs, {}))
            self.assertEqual(posttool.covered_members(out, executable=BIN), set())
        composite = posttool.entry(executable=BIN)["hooks"][0]
        self.refuse(self.settings(composite, composite), "duplicate")
        self.refuse(self.settings(composite, r), "coexists")

    def test_delivery_only_repairs_precede_eligible_conversion(self):
        for cotenants in (False, True):
            with self.subTest(cotenants=cotenants):
                data = self.settings()
                data["hooks"][EVENT] = [
                    {"matcher": matcher, "hooks": ([deepcopy(self.foreign)] if cotenants else [])
                     + [self.leaf(self.deliver)]}
                    for matcher in ("Bash", "Edit")]
                out, _ = self.complete(data)
                if not cotenants:
                    # Main repairs these groups in place; the new recorder is
                    # appended AFTER them. Do not invent record-first ordering.
                    self.assertEqual([posttool.recognize(h["command"])[0]
                        for g in out["hooks"][EVENT] for h in g["hooks"]],
                        ["deliver", "deliver", "record"])
                    self.assertEqual(posttool.covered_members(out, executable=BIN), set())
                if cotenants:
                    self.assert_composite(out)
                    self.assertEqual(out["hooks"][EVENT][:2], [
                        {"matcher": matcher, "hooks": [self.foreign]}
                        for matcher in ("Bash", "Edit")])
                self.assertEqual(self.plan(out)[0], out)
                # Unrequested delivery scope is not permission to normalize it
                # or convert it: leave the requested standalone writer its job.
                untouched, remaining, actions = self.plan(data, (self.record,))
                self.assertEqual((untouched, remaining, actions), (data, (self.record,), {}))

    def test_exact_member_type_repair_is_not_foreign_type_ownership(self):
        foreign = {"type": "http", "command": "foreign-endpoint", "url": "keep"}
        for spec, other in (self.specs, self.specs[::-1]):
            ours = self.leaf(spec, type="http")
            data = self.settings(foreign, ours)
            out, _ = self.complete(data)
            self.assertEqual(self.assert_composite(out)[0], foreign)
            self.assertEqual(data["hooks"][EVENT][0]["hooks"][1], ours)
            # No requested repair authority over the wrong-type member.
            out, remaining, actions = self.plan(data, (other,))
            self.assertEqual((out, remaining, actions), (data, (other,), {}))
            damaged = deepcopy(data)
            damaged["hooks"][EVENT][0]["hooks"][1]["url"] = "unknown-policy"
            out, _ = self.complete(damaged)
            self.assertEqual([h["url"] for g in out["hooks"][EVENT] for h in g["hooks"]
                              if h.get("url") == "unknown-policy"], ["unknown-policy"])
            self.assertEqual(posttool.covered_members(out, executable=BIN), set())

    def test_opaque_orphan_policies_remain_distinct(self):
        data = self.settings()
        data["hooks"][EVENT] = [
            {"matcher": matcher, "hooks": [deepcopy(self.foreign),
                self.leaf(self.deliver, policy={"keep": matcher})]}
            for matcher in ("Bash", "Edit")]
        out, _ = self.complete(data, (self.deliver,))
        leaves = [h for g in out["hooks"][EVENT] for h in g["hooks"]]
        self.assertEqual([h["policy"] for h in leaves if "policy" in h],
                         [{"keep": "Bash"}, {"keep": "Edit"}])
        self.assertEqual(sum(posttool.recognize(h.get("command")) is not None for h in leaves), 2)
        self.assertEqual(out["hooks"][EVENT][:2], [
            {"matcher": matcher, "hooks": [self.foreign]} for matcher in ("Bash", "Edit")])
        self.assertEqual(self.complete(out, (self.deliver,))[0], out)

    def test_mixed_orphan_exact_review_fixture(self):
        ordinary = self.leaf(self.deliver)
        opaque = dict(ordinary, **{"async": True})
        data = {"hooks": {EVENT: [
            {"matcher": "Bash", "hooks": [deepcopy(self.foreign), ordinary]},
            {"matcher": "Edit", "hooks": [deepcopy(self.foreign), opaque]},
        ]}}
        out, actions = self.complete(data, (self.deliver,))
        self.assertEqual(out["hooks"][EVENT], [
            {"matcher": "Bash", "hooks": [self.foreign]},
            {"matcher": "Edit", "hooks": [self.foreign]},
            {"matcher": "*", "hooks": [opaque]},
            {"matcher": "*", "hooks": [ordinary]},
        ])
        self.assertEqual(actions["deliver"], "update")
        again, actions = self.complete(out, (self.deliver,))
        self.assertEqual(again, out)
        self.assertEqual(actions["deliver"], "ok")

    def test_mixed_orphans_keep_ordinary_home_distinct_from_opaque_policies(self):
        for kinds in ((False, False), (True, True), (False, True),
                      (True, False), (False, True, False, True)):
            for home_kind in (None, "ordinary", "opaque", "exclusive-opaque"):
                with self.subTest(opaque=kinds, home=home_kind):
                    ordinary = self.leaf(self.deliver)
                    opaque = dict(ordinary, **{"async": True})
                    groups = [{"matcher": "Bash", "hooks": [
                        deepcopy(self.foreign), deepcopy(opaque if kind else ordinary)],
                        "keepGroup": i} for i, kind in enumerate(kinds)]
                    survivors = [dict(g, hooks=[deepcopy(self.foreign)]) for g in groups]
                    if home_kind:
                        groups.append({"matcher": "Edit" if home_kind == "exclusive-opaque" else "*",
                            "hooks": [deepcopy(ordinary if home_kind == "ordinary" else opaque)]})
                    data = self.settings()
                    data["hooks"][EVENT] = groups
                    out, actions = self.complete(data, (self.deliver,))
                    leaves = [h for g in out["hooks"][EVENT] for h in g["hooks"]]
                    self.assertEqual(out["hooks"][EVENT][:len(kinds)], survivors)
                    self.assertEqual(sum(h == ordinary for h in leaves),
                                     int(home_kind == "ordinary" or not all(kinds)))
                    self.assertEqual(sum(h == opaque for h in leaves),
                                     sum(kinds) + int(home_kind in ("opaque", "exclusive-opaque")))
                    self.assertEqual(actions["deliver"], "update")
                    self.assertEqual(posttool.covered_members(out, executable=BIN), set())
                    again, repeated = self.complete(out, (self.deliver,))
                    self.assertEqual(again, out)
                    self.assertEqual(repeated["deliver"], "ok")
                    self.assertEqual(out["hooks"]["PostToolUseFailure"], [self.failure])

    def test_historic_recorder_repair_preserves_unrequested_drift_and_keys(self):
        historical = self.leaf(dict(self.record, timeout=99), historic=True)
        historical.pop("timeout")
        data = self.settings(self.leaf(self.record), historical, self.leaf(self.deliver))
        self.assert_composite(self.complete(data)[0])
        out, _ = self.complete(data, (self.deliver,))
        self.assertIn(historical, out["hooks"][EVENT][0]["hooks"])
        for changes in ({"timeout": 104}, {"unknown": True}):
            damaged = deepcopy(data)
            damaged["hooks"][EVENT][0]["hooks"][1].update(changes)
            out, _ = self.complete(damaged)
            if "unknown" in changes:
                self.assertTrue(out["hooks"][EVENT][0]["hooks"][1]["unknown"])
                self.assertEqual(posttool.covered_members(out, executable=BIN), set())
            else:
                self.assert_composite(out)
        damaged = deepcopy(data)
        damaged["hooks"][EVENT][0]["hooks"][1]["command"] += "; foreign"
        protected = deepcopy(damaged["hooks"][EVENT][0]["hooks"][1])
        out, actions = self.complete(damaged)
        self.assertIn(protected, out["hooks"][EVENT][0]["hooks"])
        self.assertTrue(actions["posttool_refusals"])

    def test_unknown_keys_matchers_types_and_shapes(self):
        for update in ({"async": True}, {"unknown": 1}):
            data = self.settings(self.leaf(self.record, **update), self.leaf(self.deliver))
            out, _ = self.complete(data)
            for key, value in update.items():
                self.assertEqual(out["hooks"][EVENT][0]["hooks"][0][key], value)
            self.assertEqual(posttool.covered_members(out, executable=BIN), set())
        self.assert_composite(self.complete(self.settings(
            self.leaf(self.record, type="prompt"), self.leaf(self.deliver)))[0])
        data = self.settings(self.leaf(self.record), self.leaf(self.deliver))
        data["hooks"][EVENT][0]["unknown"] = 1
        out, _ = self.complete(data)
        self.assertEqual(out["hooks"][EVENT][0]["unknown"], 1)
        self.assertEqual(posttool.covered_members(out, executable=BIN), set())
        for matcher in ("Read", ""):
            data = self.settings(self.leaf(self.record), self.leaf(self.deliver))
            data["hooks"][EVENT][0]["matcher"] = matcher
            out, remaining, actions = self.plan(data)
            self.assertEqual((out, remaining, actions), (data, self.specs, {}))
            self.assertEqual(posttool.covered_members(out, executable=BIN), set())
        for data in (None, [], {"hooks": None}, {"hooks": {EVENT: {}}}):
            self.refuse(data, "objects|list")

    def test_ignored_foreign_shapes_are_preserved_conversion_barriers(self):
        for group in (None, {"hooks": None}, {"hooks": "bad"}, {"hooks": ["bad"]}):
            data = {"hooks": {EVENT: [
                {"hooks": [self.leaf(self.record)]}, group,
                {"matcher": "*", "hooks": [self.leaf(self.deliver)]}]}}
            out, remaining, actions = self.plan(data)
            self.assertEqual((out, remaining, actions), (data, self.specs, {}))
            out, _ = self.complete(data)
            self.assertEqual(out["hooks"], data["hooks"])
            self.assertEqual(posttool.covered_members(out, executable=BIN), set())
        data = self.settings(self.leaf(self.record), "bad", self.leaf(self.deliver))
        self.assertEqual(self.plan(data), (data, self.specs, {}))

    def test_requested_budget_policy_stays_standalone_installed_drift_repairs(self):
        requests = [(record.HOOK_SPECS[0], self.deliver)]
        for update in ({"timeout": 3}, {"matcher": "Read"}, {"gate": True}, {"external": "/tmp/tool"}):
            requests.append((self.record, dict(self.deliver, **update)))
        for requested in requests:
            out, remaining, actions = self.plan({}, requested)
            self.assertEqual((out, remaining, actions), ({}, requested, {}))
        for data in (self.settings(self.leaf(dict(self.record, timeout=5)), self.leaf(self.deliver)),
                     self.settings(self.leaf(self.record, timeout=99), self.leaf(self.deliver))):
            self.assert_composite(self.complete(data)[0])

    def test_aliased_requests_cannot_resurrect_standalones(self):
        for member in self.specs:
            with self.subTest(member=member["name"]):
                alias = dict(member, name="renamed-" + member["name"])
                data = self.settings(deepcopy(self.foreign))
                data["hooks"][EVENT].append(posttool.entry(executable=BIN))
                before, out, refused = deepcopy(data), deepcopy(data), False
                try:
                    out, remaining, _ = self.plan(data, (alias,))
                except ValueError:
                    refused = True
                else:
                    with mock.patch.object(hooks, "helm_bin", return_value=BIN):
                        for spec in remaining:
                            hooks._merge_event(out, spec)
                # Exercise the REAL downstream owner. A v1 planner returns the
                # alias to it and resurrects an extra execution beside posttool.
                self.assertEqual(out["hooks"][EVENT][0]["hooks"][0], self.foreign)
                self.assertEqual(out["hooks"][EVENT], before["hooks"][EVENT])
                self.assertEqual(data, before)
                self.assertTrue(refused)

    def test_requested_unknown_or_changed_fields_refuse(self):
        data = {"hooks": {EVENT: [posttool.entry(executable=BIN)]}}
        for member in self.specs:
            for update in ({"unknown": True}, {"scope": "elsewhere"}, {"own": ("helm",)}):
                with self.subTest(member=member["name"], update=update):
                    self.refuse(data, "incompatible requested", (dict(member, **update),))

    def test_other_requested_owner_cannot_replace_composite(self):
        custom = {"name": "audit", "event": EVENT, "args": "audit --hook-json",
                  "timeout": 2, "matcher": "*", "own": ("helm",)}
        data = {"hooks": {EVENT: [posttool.entry(executable=BIN)]}}
        before, out, refused = deepcopy(data), deepcopy(data), False
        try:
            out, remaining, _ = self.plan(data, (custom,))
        except ValueError:
            refused = True
        else:
            with mock.patch.object(hooks, "helm_bin", return_value=BIN):
                for spec in remaining:
                    hooks._merge_event(out, spec)
        self.assertEqual(out["hooks"][EVENT], before["hooks"][EVENT])
        self.assertEqual(data, before)
        self.assertTrue(refused)

    def test_full_coverage_rejects_broken_composite(self):
        healthy = posttool.entry(executable=BIN)
        self.assertEqual(posttool.covered_members({"hooks": {EVENT: [healthy]}}, executable=BIN), {"record", "deliver"})
        for update in ({"type": "prompt"}, {"timeout": None}, {"timeout": 5},
                       {"command": healthy["hooks"][0]["command"] + "; foreign"}):
            group = deepcopy(healthy)
            group["hooks"][0].update(update)
            self.assertFalse(posttool.covers(group, group["hooks"][0], executable=BIN))
            self.assertEqual(posttool.covered_members({"hooks": {EVENT: [group]}}, executable=BIN), set())
        for matcher in ("Read", ""):
            group = dict(healthy, matcher=matcher)
            self.assertFalse(posttool.covers(group, group["hooks"][0], executable=BIN))
        bad = dict(posttool.descriptor(), timeout=12)
        leaf = self.leaf(bad)
        self.assertFalse(posttool.covers({"hooks": [leaf]}, leaf, executable=BIN))
        self.refuse(self.settings(leaf), "budget")
        self.assertEqual(posttool.covered_members({"hooks": {"PostToolUseFailure": [healthy]}}, executable=BIN), set())

    def test_missing_composite_deadline_repaired_not_trusted(self):
        data = {"hooks": {EVENT: [posttool.entry(executable=BIN)]}}
        del data["hooks"][EVENT][0]["hooks"][0]["timeout"]
        self.assertFalse(posttool.covered_members(data, executable=BIN))
        out, remaining, actions = self.plan(data, (self.deliver,))
        self.assert_composite(out)
        self.assertEqual(actions, {"deliver": "update"})
        self.assertEqual(remaining, ())
        stale = {"hooks": {EVENT: [posttool.entry(executable="/tmp/old room/bin/helm")]}}
        self.assertFalse(posttool.covered_members(stale, executable=BIN))
        refreshed, _, actions = self.plan(stale, (self.deliver,))
        self.assert_composite(refreshed)
        self.assertEqual(actions, {"deliver": "update"})
        for update in ({"type": "prompt"}, {"timeout": 5}):
            damaged = deepcopy(out)
            damaged["hooks"][EVENT][0]["hooks"][0].update(update)
            self.refuse(damaged, "type|deadline")

    def test_idempotence_narrowed_requests_and_failure_preserved(self):
        failure_spec = record.deployed_spec("PostToolUseFailure")
        data = self.settings(self.leaf(self.record), self.leaf(self.deliver))
        out, _, _ = self.plan(data)
        for requested in ((self.record,), (self.deliver,), self.specs, (failure_spec,), ()):
            again, remaining, actions = self.plan(out, requested)
            self.assertEqual(again, out)
            self.assertEqual(again["hooks"]["PostToolUseFailure"], [self.failure])
            self.assertEqual(remaining, (failure_spec,) if requested == (failure_spec,) else ())
            self.assertTrue(all(a == "ok" for a in actions.values()))
        out["other"][1]["x"] = 9
        self.assertEqual(data["other"][1]["x"], 2)

    def test_descriptor_is_separate_and_budgets_unchanged(self):
        self.assertEqual([s["timeout"] for s in posttool.installed_specs()], [10, 2])
        self.assertEqual(record.HOOK_SPECS[0]["timeout"], 5)
        self.assertEqual(posttool.descriptor()["args"], "hooks run PostToolUse --installed --hook-json")
        budget = self.record["timeout"] + 2 * self.deliver["timeout"]
        self.assertEqual(posttool.preparation_budget(), self.deliver["timeout"])
        self.assertEqual(posttool.descriptor()["timeout"], budget + hooks.GRACE_S)
        self.assertNotIn("posttool", [s["name"] for s in hooks.SPECS + record.HOOK_SPECS])
        self.assertFalse(posttool.descriptor().get("gate"))
        self.assertEqual(posttool.entry(executable=BIN)["hooks"][0]["timeout"], budget + 2 * hooks.GRACE_S)
        with self.assertRaisesRegex(ValueError, "absolute"):
            posttool.entry(executable="helm")
        for update in ({"event": "PostToolUseFailure"}, {"matcher": "Read"}, {"gate": True}):
            with mock.patch.object(record, "deployed_spec", return_value=dict(self.record, **update)):
                with self.assertRaises(ValueError):
                    posttool.installed_specs()

    def test_renderer_argument_and_foreign_deep_equality(self):
        calls = []

        def render(spec, *, executable):
            calls.append((spec["name"], executable))
            return hooks.spec_command(spec, executable=executable)

        data = self.settings(self.leaf(self.record), self.leaf(self.deliver), deepcopy(self.foreign))
        data["hooks"]["PostToolUseFailure"] = [{"opaque": [1, 2]}]
        out, _, _ = posttool.prepare(data, self.specs, executable=BIN, renderer=render)
        self.assertEqual(out["hooks"][EVENT][0]["hooks"][-1], self.foreign)
        self.assertEqual(out["hooks"]["PostToolUseFailure"], data["hooks"]["PostToolUseFailure"])
        self.assertEqual(out["permissions"], data["permissions"])
        self.assertTrue(calls)
        self.assertTrue(all(path == BIN for _, path in calls))
