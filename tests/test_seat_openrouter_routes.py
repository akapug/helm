#!/usr/bin/env python3
"""Task/2643: per-model route generation, the credential-isolation invariant,
the zero-cost guard and the data-terms gate for a per-model proxy family.

WHY A FILE OF ITS OWN. The property under test is not "openrouter works"; it
is that a family whose credential is the owner's FUNDED account cannot be
started on a model that bills, on a model whose vendor trains on the submitted
prompt, or on a config that has quietly collapsed its per-model credential
split. Those three failures are silent in production — a price change is
upstream, a training clause is in prose, and a collapsed split reads as a
vendor wall — so each one needs an arm that goes red BEFORE anybody sees it.

EVERY SYNTHETIC TABLE HERE IS SYNTHETIC ON PURPOSE. FAMILIES is validated at
IMPORT, so an arm that mutated it would either be impossible to write or would
be testing a world no seat ever runs in. The real table is read, never edited.
"""
import os
import tempfile
import unittest

from tests._tmphome import pin_suite_guard
from helm import seat
from helm import seat_catalog
from helm import seat_launch_assets
from helm import seat_provision


FAMILY = "openrouter"  # noqa: SEAT_NAME — the configured family identity IS the subject of every arm in this file
#: The SECOND free lane on the same vendor and the same key (task/2805),
#: a family of its own because the shipped gates give a second free seat
#: no other shape: `instance_model_error` refuses `instance_models` on
#: mode "proxy-key", and the instance-name grammar admits only
#: `<family>` and `<family>-<N>` — neither of which can say which MODEL
#: a reviewer is. Its arms live here because the property under test is
#: the same one: a FUNDED key may only reach a model that is free and
#: whose data terms were read.
SECOND_FREE_LANE = "dots3"  # noqa: SEAT_NAME — the second configured free family IS the subject of the arms that name it


def _compat_table(**over):
    """A minimal per-model family this file can legally break.

    Derived from the SHAPE the real entry declares (mode, key_env, base_url,
    model, model_providers, model_fallback) rather than invented, so an arm
    that passes here is answering a question about the real predicate. Every
    row starts VALID; each arm breaks exactly one thing, which is what makes
    the refusal attributable to that thing.
    """
    fam = {"port": 9999, "model": "x-fast", "mode": "proxy-key",
           "base_url": "https://example.invalid/api/v1",
           "key_env": "X_API_KEY", "provider": "x",
           "model_fallback": "x-code",
           "model_providers": {
               "x-one": {"alias": "x-fast", "default": True,
                         "upstream_model": "vendor/one:free",
                         "pricing": {"prompt": "0", "completion": "0",
                                     "read": "2026-09-16"},
                         "terms": {"verdict": "private-code-safe",
                                   "read": "2026-09-16", "by": "a-test"}},
               "x-two": {"alias": "x-code",
                         "upstream_model": "vendor/two:free",
                         "pricing": {"prompt": "0", "completion": "0",
                                     "read": "2026-09-16"},
                         "terms": {"verdict": "private-code-safe",
                                   "read": "2026-09-16", "by": "a-test"}},
           }}
    fam.update(over)
    return fam


def _free(**over):
    """A vendor /models listing in which both synthetic models are free."""
    listing = {"vendor/one:free": {"pricing": {"prompt": "0", "completion": "0"},
                                   "supported_parameters": ["tools"]},
               "vendor/two:free": {"pricing": {"prompt": "0", "completion": "0"},
                                   "supported_parameters": ["tools"]}}
    listing.update(over)
    return listing


class RouteGenerationTest(unittest.TestCase):
    """helm GENERATES the per-model config instead of a human writing it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-or-routes-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        pin_suite_guard(self, self.tmp)
        self.fam = seat.FAMILIES[FAMILY]
        self.addCleanup(self._restore)

    def _restore(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior

    def _generated(self, key="sk-or-TEST"):
        rows = seat_provision._mint_providers(FAMILY, self.fam,
                                              self.fam["base_url"])
        return seat_launch_assets._config_yaml_key(
            self.fam["port"], "tok", self.fam["provider"],
            self.fam["base_url"], self.fam["model"], key,
            providers=rows)

    def _compat_block(self, text):
        _prefix, blocks = seat_launch_assets._top_blocks(text)
        return "".join(body for key, body in blocks
                       if key == "openai-compatibility")

    def test_one_route_per_catalogued_model_default_first(self):  # noqa: VACUOUS_ASSERTION — the ds4pro leg is an unconditional positive control on the same observable: the OTHER shape's routes all share one alias where these are all distinct
        """`openai-compatibility maps to 0 catalogued routes` was the refusal
        this lane exists to end, and it came from `proxy_routes` answering
        nothing for a family whose models differ per BLOCK rather than per
        endpoint."""
        routes = seat.proxy_routes(FAMILY)
        declared = seat_catalog.family_model_providers(self.fam)
        self.assertEqual(len(routes), len(declared))
        self.assertEqual(routes[0]["alias"], self.fam["model"])
        self.assertEqual(len({r["alias"] for r in routes}), len(routes))
        self.assertEqual(len({r["provider"] for r in routes}), len(routes))
        for route in routes:
            row = declared[route["provider"]]
            self.assertEqual(route["upstream_model"], row["upstream_model"])
        # CONTROL on a family of the OTHER shape: ds4pro is ONE model across
        # several vendors, so every one of its routes still carries the SAME
        # alias. If this ever reads like the block above, the two shapes have
        # been folded into one and the per-model reading is gone.
        pool = seat.proxy_routes("ds4pro")  # noqa: SEAT_NAME — the OTHER declaration shape's configured identity is the control
        self.assertGreater(len(pool), 1)
        self.assertEqual({r["alias"] for r in pool},
                         {seat.FAMILIES["ds4pro"]["model"]})  # noqa: SEAT_NAME — same control, its declared model

    def test_the_generated_config_is_a_fixpoint_of_the_plan(self):  # noqa: VACUOUS_ASSERTION — `assertEqual(plan["text"], text)` is the unconditional positive control: a planner that produced nothing would fail it, so the three absence assertions cannot pass on an empty plan
        """A generator whose own output the planner wants to rewrite would
        make `seat doctor --ensure` restage a config every three minutes."""
        text = self._generated()
        path = os.path.join(self.tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        plan = seat_launch_assets.proxy_config_plan(path, FAMILY)
        self.assertFalse(plan["changed"], plan["text"])
        self.assertIsNone(plan["alias_drift"])
        self.assertEqual(plan["opaque"], [])
        self.assertEqual(plan["text"], text)

    def test_drift_in_a_NON_default_block_is_seen_and_cured(self):  # noqa: VACUOUS_ASSERTION — `assertNotEqual(broken, text)` proves the surgery had input and `assertEqual(plan["text"], text)` proves the cure produced the canonical bytes; neither can pass on an empty observable
        """THE ARM THAT WAS IMPOSSIBLE BEFORE THIS LANE. `_eligible_providers`
        matched every block against `fam["model"]`, so on a per-model family
        exactly ONE block could ever match and the other three were invisible:
        stale, unreported, and passed through byte-for-byte forever."""
        text = self._generated()
        row = self.fam["model_providers"]["openrouter-cohere"]
        broken = text.replace('- name: "%s"\n        alias: "%s"'
                              % (row["upstream_model"], row["alias"]),
                              '- name: "cohere/WRONG"\n        alias: "%s"'
                              % row["alias"])
        # CONTROL: the surgery really changed the bytes, so a green cure below
        # cannot be green because nothing was broken.
        self.assertNotEqual(broken, text)
        path = os.path.join(self.tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(broken)
        plan = seat_launch_assets.proxy_config_plan(path, FAMILY)
        self.assertTrue(plan["changed"])
        self.assertIn(row["alias"], plan["alias_drift"])
        self.assertIn(row["upstream_model"], plan["alias_drift"])
        self.assertEqual(plan["text"], text)

    def test_the_frontmatter_ids_land_on_exactly_one_provider(self):  # noqa: VACUOUS_ASSERTION — the counting assertEqual before the loop is the unconditional positive control: it fails outright on a config carrying none of the ids, so the per-alias arms never read an empty world
        """Five providers in front of `claude-opus-5` is the shared-cooldown
        failure again, one alias over: the proxy would pool their
        credentials."""
        text = self._generated()
        rows = seat_launch_assets.config_model_rows(self._compat_block(text))
        block = seat_catalog.family_default_provider(self.fam)
        upstream = self.fam["model_providers"][block]["upstream_model"]
        # UNCONDITIONAL POSITIVE CONTROL, before any loop: the ids are really
        # in the file, on the default block, once each plus the family's own
        # row. A generator that emitted none of them would fail HERE.
        ids = tuple(seat_catalog.CC_AGENT_FRONTMATTER_MODELS)
        self.assertEqual(sum(1 for provider, model in rows
                             if provider == block and model == upstream),
                         len(ids) + 1)
        for alias in ids:
            owners = {provider for provider, model in rows
                      if model == upstream}
            self.assertEqual(owners, {block}, alias)
            self.assertEqual(text.count('alias: "%s"' % alias), 1, alias)

    #: The families that take the per-model path, NAMED so that a third one
    #: is a visible event here rather than a silent exclusion from the arm
    #: below. A family arrives in this tuple by declaring `model_providers`;
    #: the arm proves the declaration and the behaviour agree.
    PER_MODEL = (FAMILY, SECOND_FREE_LANE)

    def test_every_other_family_still_mints_one_block(self):  # noqa: VACUOUS_ASSERTION — the closing loop is an unconditional positive control on the same call: each family that DOES take the path answers with a count, so a `_mint_providers` that returned None for everybody fails there
        """The per-model path must be invisible to the families that have one
        model: `_mint_providers` answering anything for them would rewrite
        every existing config's bytes."""
        for name, fam in seat.FAMILIES.items():
            if name in self.PER_MODEL:
                # the named set and the DECLARATION must agree, or the skip
                # above is hiding a family from the arm
                self.assertTrue(fam.get("model_providers"), name)
                continue
            self.assertFalse(fam.get("model_providers"), name)
            self.assertIsNone(
                seat_provision._mint_providers(name, fam, "https://x.invalid"),
                name)
        # POSITIVE CONTROL: every family that DOES take the path answers, one
        # block per declared model.
        for name in self.PER_MODEL:
            fam = seat.FAMILIES[name]
            self.assertEqual(
                len(seat_provision._mint_providers(name, fam,
                                                   fam["base_url"])),
                len(fam["model_providers"]), name)


class CredentialIsolationTest(unittest.TestCase):
    """CLIProxyAPI cools a CREDENTIAL, not a model.

    MEASURED on a live seat: with every model under one api-key entry, a 429 on
    ONE free preview benched the shared credential and every OTHER model began
    refusing locally with "no available credential, 1 cooling down" while that
    same model answered upstream in 0.3s on a direct curl. One rate-limited
    model took the whole family dark, and the local refusal was
    byte-indistinguishable from a vendor wall.

    So the arms below are not about the generator's formatting. They state the
    PROPERTY — no provider block serves two of this family's models, and every
    block that serves one holds a credential entry of its own — and they check
    it against bytes, so a future edit that collapsed the blocks back fails
    here whether it happened in the generator, in the mint door, or by hand.
    """

    def setUp(self):
        self.fam = seat.FAMILIES[FAMILY]
        self.tmp = tempfile.mkdtemp(prefix="helm-test-or-isolation-")
        pin_suite_guard(self, self.tmp)

    def _compat_block(self, text):
        _prefix, blocks = seat_launch_assets._top_blocks(text)
        return "".join(body for key, body in blocks
                       if key == "openai-compatibility")

    def _generated(self):
        rows = seat_provision._mint_providers(FAMILY, self.fam,
                                              self.fam["base_url"])
        return seat_launch_assets._config_yaml_key(
            self.fam["port"], "tok", self.fam["provider"],
            self.fam["base_url"], self.fam["model"], "sk-or-TEST",
            providers=rows)

    def test_every_model_gets_its_own_block_and_its_own_key_entry(self):  # noqa: VACUOUS_ASSERTION — `assertEqual(set(by_model), mapped)` and `assertEqual(len(items), len(mapped))` are unconditional positive controls: an empty config fails both before any loop runs
        block = self._compat_block(self._generated())
        mapped = {row["upstream_model"]
                  for row in self.fam["model_providers"].values()}
        by_model = {}
        for provider, model in seat_launch_assets.config_model_rows(block):
            if model in mapped:
                by_model.setdefault(model, set()).add(provider)
        # TOTAL: every declared model is actually in the file.
        self.assertEqual(set(by_model), mapped)
        # INJECTIVE, BOTH WAYS: one block per model and one model per block.
        owners = [next(iter(v)) for v in by_model.values()]
        for model, providers in by_model.items():
            self.assertEqual(len(providers), 1, model)
        self.assertEqual(len(set(owners)), len(owners))
        # AND EACH OF THOSE BLOCKS HOLDS A CREDENTIAL ENTRY OF ITS OWN —
        # the property, not the count: a block with no api-key-entries cannot
        # be isolated from a neighbour's cooldown however many blocks exist.
        _prefix, items = seat_launch_assets._provider_sections(block)
        # UNCONDITIONAL, so the per-block loop below cannot pass on an empty
        # split: there is one readable provider block per declared model.
        self.assertEqual(len(items), len(mapped))
        for name, item in items:
            info = seat_launch_assets._provider_info(item, None)
            self.assertEqual(len(info["api_keys"]), 1, name)
        self.assertIsNone(
            seat_launch_assets.credential_isolation_reason(block, self.fam))

    def test_a_collapsed_config_is_REFUSED_and_names_both_models(self):
        """THE ARM A FUTURE COLLAPSE FAILS. The collapsed config is derived by
        surgery on the REAL generator output — one model row moved into its
        neighbour's block and the emptied block deleted — so it is the config
        the pre-fix shape actually produced, not one this test imagined."""
        text = self._generated()
        block = self._compat_block(text)
        moved = self.fam["model_providers"]["openrouter-cohere"]
        home = self.fam["model_providers"]["openrouter-nex"]
        row = ('      - name: "%s"\n        alias: "%s"\n'
               % (moved["upstream_model"], moved["alias"]))
        self.assertIn(row, block)             # CONTROL: the surgery has input
        collapsed = block.replace(row, "")
        collapsed = collapsed.replace(
            '      - name: "%s"\n        alias: "%s"\n'
            % (home["upstream_model"], home["alias"]),
            '      - name: "%s"\n        alias: "%s"\n%s'
            % (home["upstream_model"], home["alias"], row))
        self.assertNotEqual(collapsed, block)
        reason = seat_launch_assets.credential_isolation_reason(
            collapsed, self.fam)
        self.assertIsNotNone(reason)
        self.assertIn(moved["upstream_model"], reason)
        self.assertIn(home["upstream_model"], reason)
        self.assertIn("cools a CREDENTIAL", reason)
        # CONTROL on the same predicate: the untouched block still passes, so
        # the refusal above is about the collapse and not about the reader.
        self.assertIsNone(
            seat_launch_assets.credential_isolation_reason(block, self.fam))

    def test_a_block_with_no_credential_entry_is_refused(self):  # noqa: VACUOUS_ASSERTION — `assertNotEqual(stripped, block)` is the unconditional control that the surgery had real input, and the refusal text is asserted positively
        text = self._generated()
        block = self._compat_block(text)
        stripped = block.replace(
            '    api-key-entries:\n      - api-key: "sk-or-TEST"\n', "", 1)
        self.assertNotEqual(stripped, block)   # CONTROL: surgery had input
        reason = seat_launch_assets.credential_isolation_reason(
            stripped, self.fam)
        self.assertIsNotNone(reason)
        self.assertIn("api-key-entries", reason)

    def test_a_single_model_family_is_not_subject_to_the_invariant(self):
        """A one-model family cannot have "two models in one block", and the
        reader must not invent an opinion about its config."""
        other = seat.FAMILIES["kimi"]  # noqa: SEAT_NAME — the configured one-model family IS the control
        text = seat_launch_assets._config_yaml_key(
            other["port"], "tok", other["provider"], other["base_url"],
            other["model"], "sk-x")
        block = self._compat_block(text)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the reader
        # really parsed this config and found the block's model rows, so the
        # None below is a decision and not a failure to look.
        self.assertTrue(seat_launch_assets.config_model_rows(block))
        self.assertIsNone(
            seat_launch_assets.credential_isolation_reason(block, other))

    def test_the_start_gate_refuses_a_collapsed_config_before_anything_runs(self):  # noqa: VACUOUS_ASSERTION — the canonical config is run through the SAME gate as an unconditional control, so the refusal is attributable to the collapse rather than to the reader
        """`seat up` reads this before it stages a byte or stops a listener,
        so a refusal leaves the running family exactly as it was."""
        text = self._generated()
        block = self._compat_block(text)
        moved = self.fam["model_providers"]["openrouter-cohere"]
        home = self.fam["model_providers"]["openrouter-nex"]
        row = ('      - name: "%s"\n        alias: "%s"\n'
               % (moved["upstream_model"], moved["alias"]))
        collapsed = text.replace(row, "", 1).replace(
            '      - name: "%s"\n        alias: "%s"\n'
            % (home["upstream_model"], home["alias"]),
            '      - name: "%s"\n        alias: "%s"\n%s'
            % (home["upstream_model"], home["alias"], row), 1)
        self.assertNotEqual(collapsed, text)
        gate, _notes = seat_launch_assets.family_start_refusal(
            FAMILY, self.fam, collapsed, listing=None)
        self.assertIsNotNone(gate)
        self.assertIn("CREDENTIAL", gate)
        # CONTROL: the canonical config passes the same gate, so the refusal
        # is attributable to the collapse.
        self.assertEqual(
            seat_launch_assets.family_start_refusal(
                FAMILY, self.fam, text, listing=None)[0], None)


class ZeroCostGuardTest(unittest.TestCase):
    """The key behind this family is the owner's FUNDED account.

    BOTH FIELDS DECIDE. A model free on prompt and paid on completion still
    bills, and the vendor's listing carries the two separately — the live
    reading found poolside/laguna-s-2.1 at prompt 0.00000009 /
    completion 0.00000018 one character away from a free twin.
    """

    def test_a_non_zero_PROMPT_price_refuses_the_start(self):
        fam = _compat_table()
        listing = _free(**{"vendor/two:free": {
            "pricing": {"prompt": "0.0000004", "completion": "0"},
            "supported_parameters": ["tools"]}})
        refusal, _notes = seat_catalog.zero_cost_refusal("x", fam, listing)
        self.assertIsNotNone(refusal)
        self.assertIn("vendor/two:free", refusal)
        self.assertIn("prompt", refusal)
        self.assertIn("0.0000004", refusal)

    def test_a_non_zero_COMPLETION_price_refuses_the_start(self):
        """THE ARM THE OWNER NAMED. A price-checker that reads `prompt` alone
        calls this model free, and it is not."""
        fam = _compat_table()
        listing = _free(**{"vendor/one:free": {
            "pricing": {"prompt": "0", "completion": "0.00000018"},
            "supported_parameters": ["tools"]}})
        refusal, _notes = seat_catalog.zero_cost_refusal("x", fam, listing)
        self.assertIsNotNone(refusal)
        self.assertIn("vendor/one:free", refusal)
        self.assertIn("completion", refusal)

    def test_an_all_zero_listing_refuses_nothing(self):  # noqa: VACUOUS_ASSERTION — the set-equality assertion above the call is the unconditional positive control that the listing covers every declared upstream, so the None is a verdict and not an empty read
        """THE CONTROL THAT MAKES THE TWO ARMS ABOVE NON-VACUOUS: the same
        function, the same table, the same shape of listing, and no refusal.
        Without it a reader cannot tell a working guard from one that refuses
        everything."""
        listing = _free()
        # UNCONDITIONAL POSITIVE CONTROL that the listing really covers the
        # table: every declared upstream is a key the reader will look up, so
        # the None below is a verdict and not a read of an empty world.
        self.assertEqual(
            {row["upstream_model"] for row
             in _compat_table()["model_providers"].values()},
            set(listing))
        refusal, notes = seat_catalog.zero_cost_refusal("x", _compat_table(),
                                                        listing)
        self.assertIsNone(refusal)
        self.assertEqual(notes, ())

    def test_an_unreadable_price_counts_as_priced(self):
        """A value that will not parse is not a reason to spend the owner's
        money hoping."""
        self.assertTrue(seat_catalog._priced("free"))
        self.assertTrue(seat_catalog._priced({}))
        # CONTROLS, both directions of the real vendor's own spellings.
        self.assertFalse(seat_catalog._priced("0"))
        self.assertFalse(seat_catalog._priced("0.0"))
        self.assertFalse(seat_catalog._priced(None))
        self.assertTrue(seat_catalog._priced("0.00000009"))

    def test_an_UNREACHABLE_listing_refuses_nothing_and_says_so(self):
        """Missing evidence is not evidence against. A seat that cannot start
        because a probe timed out is a worse failure than the one prevented,
        and the declared receipts already passed the import gate."""
        refusal, notes = seat_catalog.zero_cost_refusal("x", _compat_table(),
                                                        None)
        self.assertIsNone(refusal)
        self.assertEqual(len(notes), 1)
        self.assertIn("not reachable", notes[0])
        self.assertIn("2026-09-16", notes[0])

    def test_a_model_with_no_tool_calling_refuses_the_start(self):
        """Measured from the listing rather than recalled from a table:
        z-ai/glm-5.2:free reads exactly this way."""
        fam = _compat_table()
        listing = _free(**{"vendor/two:free": {
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["max_tokens", "temperature"]}})
        refusal, _notes = seat_catalog.zero_cost_refusal("x", fam, listing)
        self.assertIsNotNone(refusal)
        self.assertIn("vendor/two:free", refusal)
        self.assertIn("tools", refusal)

    def test_the_import_gate_refuses_a_declared_non_zero_price(self):
        """The pure half, which fires before anything starts. A price typo
        committed to the table would otherwise be read next by a billing
        statement."""
        fam = _compat_table()
        fam["model_providers"]["x-two"]["pricing"] = {
            "prompt": "0", "completion": "0.00000018", "read": "2026-09-16"}
        reason = seat_catalog.model_provider_error("x", fam)
        self.assertIsNotNone(reason)
        self.assertIn("completion", reason)
        self.assertIn("FUNDED", reason)
        # CONTROL: the same table with the row restored is accepted, so the
        # refusal is about the price and not about the table's shape.
        self.assertIsNone(seat_catalog.model_provider_error("x", _compat_table()))

    def test_the_import_gate_refuses_a_row_with_no_price_receipt(self):
        fam = _compat_table()
        del fam["model_providers"]["x-two"]["pricing"]
        reason = seat_catalog.model_provider_error("x", fam)
        self.assertIsNotNone(reason)
        self.assertIn("pricing", reason)

    def test_the_live_catalog_passes_its_own_import_gate(self):
        """The assertion that runs at import, re-read here so a reader can see
        WHICH family it binds rather than only that the module loaded."""
        for name, fam in seat.FAMILIES.items():
            self.assertIsNone(seat_catalog.model_provider_error(name, fam), name)
        self.assertTrue(seat_catalog.family_model_providers(
            seat.FAMILIES[FAMILY]))


class DataTermsGateTest(unittest.TestCase):
    """Checking price is not checking terms.

    Two free models were free because THE PROMPTS WERE THE PRICE, and nothing
    in any pricing field discloses it. So the verdict is DECLARED, attributed
    and dated, and the refused ids are carried with their reasons so a later
    reader cannot re-add one off a latency table.
    """

    def test_a_row_that_cannot_say_private_code_safe_is_refused(self):
        fam = _compat_table()
        fam["model_providers"]["x-two"]["terms"] = {
            "verdict": "trains-on-submitted-data", "read": "2026-09-16",
            "by": "a-test"}
        reason = seat_catalog.model_provider_error("x", fam)
        self.assertIsNotNone(reason)
        self.assertIn("private-code-safe", reason)

    def test_a_row_with_no_terms_record_at_all_is_refused(self):
        """Absence of a training claim is not a finding: a row that never
        looked reads identically to one that looked and found nothing."""
        fam = _compat_table()
        del fam["model_providers"]["x-two"]["terms"]
        reason = seat_catalog.model_provider_error("x", fam)
        self.assertIsNotNone(reason)
        self.assertIn("Checking price is not checking terms", reason)

    def test_terms_must_name_WHO_read_them_and_WHEN(self):  # noqa: VACUOUS_ASSERTION — the intact table is run through the SAME predicate unconditionally first, so each looped refusal is attributable to the one key it removed
        # UNCONDITIONAL CONTROL: the intact table passes the same predicate,
        # so each refusal below is attributable to the key it removed.
        self.assertIsNone(seat_catalog.model_provider_error("x", _compat_table()))
        for missing in ("read", "by"):
            fam = _compat_table()
            del fam["model_providers"]["x-two"]["terms"][missing]
            reason = seat_catalog.model_provider_error("x", fam)
            self.assertIsNotNone(reason, missing)
            self.assertIn("terms", reason)

    def test_a_refused_model_in_the_CONFIG_refuses_the_start(self):
        """THE DOOR THAT MATTERS. The generator preserves a provider block it
        has no custody over byte-for-byte, so a hand-added route to a refused
        model survives every regeneration and every doctor sweep. Only a
        reading of the config's own bytes can see it."""
        fam = seat.FAMILIES[FAMILY]
        refused = "openrouter/free"
        self.assertIn(refused, fam["disqualified_models"])   # CONTROL
        text = ('openai-compatibility:\n'
                '  - name: "openrouter-hand"\n'
                '    base-url: "https://openrouter.ai/api/v1"\n'
                '    api-key-entries:\n'
                '      - api-key: "sk-or-TEST"\n'
                '    models:\n'
                '      - name: "%s"\n'
                '        alias: "or-anything"\n' % refused)
        _prefix, blocks = seat_launch_assets._top_blocks(text)
        block = "".join(body for key, body in blocks
                        if key == "openai-compatibility")
        reason = seat_launch_assets.disqualified_route_reason(block, fam)
        self.assertIsNotNone(reason)
        self.assertIn(refused, reason)
        self.assertIn("RANDOM FREE MODEL", reason)

    def test_the_refused_ids_are_spelled_as_the_vendor_spells_them(self):
        """A table naming ids the vendor does not list blocks NOTHING. The
        first recording of this set named `liquid/lfm-2.5-2.6b` and
        `poolside/laguna-s-2.1`; the served ids carry `:free`, and both
        spellings are carried now because the paid twin is its own hazard."""
        refused = seat.FAMILIES[FAMILY]["disqualified_models"]
        # UNCONDITIONAL: the table exists and every reason is a real sentence,
        # so the paired-spelling loop below reads a populated table.
        self.assertGreaterEqual(len(refused), 8)
        self.assertTrue(all(isinstance(why, str) and len(why) > 40
                            for why in refused.values()))
        for stem in ("liquid/lfm-2.5-2.6b", "poolside/laguna-s-2.1",
                     "z-ai/glm-5.2", "thinkingmachines/inkling-small"):
            self.assertIn(stem, refused, stem)
            self.assertIn(stem + ":free", refused, stem)

    def test_no_declared_route_maps_a_refused_model(self):
        fam = seat.FAMILIES[FAMILY]
        refused = set(fam["disqualified_models"])
        mapped = {row["upstream_model"]
                  for row in fam["model_providers"].values()}
        # UNCONDITIONAL POSITIVE CONTROLS: both sets are populated, so the
        # disjointness below is a fact about two real tables.
        self.assertTrue(refused)
        self.assertTrue(mapped)
        self.assertEqual(mapped & refused, set())
        for name, row in fam["model_providers"].items():
            self.assertNotIn(row["upstream_model"], refused, name)


class ModelChurnTest(unittest.TestCase):
    """Free PREVIEW ids with lifetimes in days. A family that hard-fails on a
    vanished one hands the vendor the power to take a seat down on its own
    schedule."""

    def test_a_vanished_model_does_NOT_refuse_the_start(self):
        fam = _compat_table()
        listing = _free()
        del listing["vendor/two:free"]
        refusal, notes = seat_catalog.zero_cost_refusal("x", fam, listing)
        self.assertIsNone(refusal)
        self.assertEqual(len(notes), 1)
        self.assertIn("vendor/two:free", notes[0])
        self.assertIn("no longer served", notes[0])

    def test_a_vanished_DEFAULT_names_the_fallback_and_the_command(self):
        fam = _compat_table()
        listing = _free()
        del listing["vendor/one:free"]
        refusal, notes = seat_catalog.zero_cost_refusal("x", fam, listing)
        self.assertIsNone(refusal)
        self.assertIn("x-code", notes[0])
        self.assertIn("--model x-code", notes[0])

    def test_the_seat_degrades_to_the_named_fallback(self):
        fam = _compat_table()
        listing = _free()
        # CONTROL FIRST: with the default served, the default stands.
        self.assertEqual(seat_catalog.family_degraded_model(fam, listing),
                         "x-fast")
        del listing["vendor/one:free"]
        self.assertEqual(seat_catalog.family_degraded_model(fam, listing),
                         "x-code")
        # AND AN UNREADABLE LISTING CHANGES NOTHING: with no measurement there
        # is no vanished model.
        self.assertEqual(seat_catalog.family_degraded_model(fam, None),
                         "x-fast")

    def test_a_fallback_that_is_not_a_route_is_refused_at_import(self):
        fam = _compat_table(model_fallback="x-nowhere")
        reason = seat_catalog.model_provider_error("x", fam)
        self.assertIsNotNone(reason)
        self.assertIn("x-nowhere", reason)

    def test_a_family_naming_ITSELF_as_the_fallback_is_refused(self):
        fam = _compat_table(model_fallback="x-fast")
        reason = seat_catalog.model_provider_error("x", fam)
        self.assertIsNotNone(reason)
        self.assertIn("fallback exists for the case", reason)

    def test_the_live_family_declares_a_reachable_fallback(self):
        fam = seat.FAMILIES[FAMILY]
        self.assertIn(fam["model_fallback"],
                      seat_catalog.family_route_aliases(fam))
        self.assertNotEqual(fam["model_fallback"], fam["model"])


class SeatDefaultsTest(unittest.TestCase):
    """These are REASONING models that spend before they answer.

    MEASURED on nex-n2.5-pro through the proxy: at max_tokens 1200
    it returned NOTHING (1200 completion tokens consumed, zero text blocks) and
    at 4000 it returned correct code using 2241. A seat under a low output cap
    gets silent empty responses that look exactly like a broken family.
    """

    #: Every family that declares an output cap, and WHY each one does. The
    #: arm below pins the whole SET rather than this family alone, because the
    #: property it protects — no launch line gains the key by accident — is a
    #: statement about the set. It read `== {FAMILY}`, a spelling that could
    #: only ever hold once; the families that arrived beside it did not break
    #: the property, they broke that spelling of it.
    #:
    #: All are REASONING models whose reasoning is drawn from the SAME
    #: max_tokens as the answer, so a low cap returns a completion with no
    #: text and a live family reads as broken. openrouter's cap is measured on
    #: nex-n2.5-pro; the second free lane rides the same vendor and carries
    #: the same derivation against its own mapped rows; qwen27's is measured
    #: on the build host's endpoint and is also bounded above by the serving slot it
    #: shares with the input. gptoss's is measured on the antigravity
    #: endpoint: an 8-token probe came back with 24 completion tokens carrying
    #: 59 characters of reasoning beside a two-character answer. opus46 is a
    #: `-thinking` route on that same endpoint and did NOT empty at 8 tokens,
    #: so its cap is headroom for a work turn rather than a cure for a
    #: measured empty completion.
    CAP_DECLARING = frozenset(("openrouter", "qwen27", SECOND_FREE_LANE,
                               "opus46", "gptoss"))  # noqa: SEAT_NAME — catalog FAMILY keys, and the set of them IS this arm's subject

    #: The cap-declaring families that ALSO map their models per row. Only
    #: these have a `probed_max_completion_tokens` ceiling beside each model,
    #: so only these can be held to "the cap sits under every mapped
    #: ceiling"; a capped family with one upstream and no table is bounded by
    #: its own entry instead. Tied to the catalog in the arm below, so it
    #: cannot drift from the declarations it names.
    CAPPED = (FAMILY, SECOND_FREE_LANE)

    def test_only_reasoning_families_declare_an_output_cap(self):
        """Every other family's launch line must stay byte-identical, which is
        true only while no entry outside the declared set carries the key."""
        declaring = {name for name, fam in seat.FAMILIES.items()
                     if fam.get("max_output_tokens")}
        self.assertEqual(declaring, set(self.CAP_DECLARING))
        self.assertIn(FAMILY, declaring)   # this module's own subject is in it
        self.assertEqual(
            set(self.CAPPED),
            {name for name in declaring
             if seat.FAMILIES[name].get("model_providers")})

    def test_the_cap_sits_under_every_mapped_model_ceiling(self):  # noqa: VACUOUS_ASSERTION — `assertEqual(len(self.CAPPED), 2)` runs unconditionally before the loop, so an empty set fails there rather than passing the loop vacuously
        """DERIVED FROM BOTH ENDS, not chosen: above the measured 4000 floor
        by a margin, and under the SMALLEST top_provider.max_completion_tokens
        across the mapped set (64000, cohere/north-mini-code:free, which both
        capped families map), which each row records beside its own model.

        Every capped family is checked against its OWN rows: a cap is only
        under 'every mapped ceiling' of the table it belongs to."""
        self.assertEqual(len(self.CAPPED), 2)   # control: the loop has input
        for family in self.CAPPED:
            fam = seat.FAMILIES[family]
            cap = fam["max_output_tokens"]
            self.assertGreater(cap, 4000, family)
            ceilings = [row["probed_max_completion_tokens"]
                        for row in fam["model_providers"].values()]
            self.assertTrue(ceilings, family)
            self.assertLess(cap, min(ceilings), family)

    def test_the_launch_line_carries_the_cap(self):  # noqa: VACUOUS_ASSERTION — `assertEqual(len(self.CAPPED), 2)` runs unconditionally before the loop, so an empty set fails there rather than passing the loop vacuously
        self.assertEqual(len(self.CAPPED), 2)   # control: the loop has input
        for family in self.CAPPED:
            line = seat.launch_line(family, room="r")
            self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS=%d"
                          % seat.FAMILIES[family]["max_output_tokens"], line)
        # CONTROL: a family that declares no cap emits no such word, so the
        # assertion above is about the declaration and not about the line.
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                         seat.launch_line("kimi", room="r"))


class RoutingTableTest(unittest.TestCase):
    """A new alias is a bug in every consumer that reads `model` alone."""

    def test_every_alias_is_a_route_the_model_router_can_resolve(self):
        fam = seat.FAMILIES[FAMILY]
        aliases = seat_catalog.family_route_aliases(fam)
        self.assertEqual(aliases[0], fam["model"])
        self.assertEqual(set(aliases),
                         {row["alias"]
                          for row in fam["model_providers"].values()})
        # CONTROL on a one-model family: the reading collapses to `model`,
        # which is what keeps every other family's routing table unchanged.
        self.assertEqual(seat_catalog.family_route_aliases(seat.FAMILIES["kimi"]),
                         (seat.FAMILIES["kimi"]["model"],))


class SecondFreeLaneTest(unittest.TestCase):
    """task/2805: the second free findings lane, and the one rule that made it
    a family of its own — A SEAT'S NAME MUST SAY ITS MODEL.

    The lane it replaces was reachable as `or-deep` and rostered as `or-dots`.
    Neither spelling names a model: they name a slot in an alias space, and a
    cross-family verdict rests entirely on WHICH family answered. So these
    arms are about the catalog's spellings as much as its numbers.
    """

    def test_every_alias_this_family_serves_names_its_model(self):  # noqa: VACUOUS_ASSERTION — `assertEqual(len(aliases), 2)` runs unconditionally before the loop, so a family serving no alias fails there rather than passing the loop vacuously
        """The aliases and the family key are all model words — no `or-`
        slot name survives into the second lane."""
        fam = seat.FAMILIES[SECOND_FREE_LANE]
        aliases = seat_catalog.family_route_aliases(fam)
        self.assertEqual(aliases[0], SECOND_FREE_LANE)     # the key IS a model
        self.assertEqual(len(aliases), 2)       # control: the loop has input
        for alias in aliases:
            self.assertFalse(alias.startswith("or-"), alias)
            upstream = [row["upstream_model"]
                        for row in fam["model_providers"].values()
                        if row["alias"] == alias]
            self.assertEqual(len(upstream), 1, alias)
            # THE ALIAS IS SPELLED OUT OF THE UPSTREAM ID, so a reader of a
            # launch line can tell which model answered without the catalog
            # in front of them. Hyphens are dropped on both sides: `dots3`
            # names `dots-studio/dots-3-note-preview:free`.
            stem = upstream[0].split("/", 1)[1].split(":", 1)[0]
            self.assertIn(alias.replace("-", ""), stem.replace("-", ""),
                          "%s does not name %s" % (alias, upstream[0]))
        # CONTROL on the family that has the OTHER convention: its own default
        # alias is a slot name, which is the defect this lane exists to stop
        # repeating, and reading it here proves the check above discriminates.
        self.assertTrue(seat.FAMILIES[FAMILY]["model"].startswith("or-"))

    def test_the_fallback_is_a_route_and_is_not_the_default(self):
        fam = seat.FAMILIES[SECOND_FREE_LANE]
        self.assertIn(fam["model_fallback"],
                      seat_catalog.family_route_aliases(fam))
        self.assertNotEqual(fam["model_fallback"], fam["model"])

    def test_a_degraded_launch_carries_the_FALLBACK_window(self):
        """The fallback model's window is smaller than this family's, so the
        degraded seat must be launched claiming the smaller one. Read off the
        SHIPPED launch line, both models through the same call, because the
        failure is silent: a seat launched claiming 512000 against a 256000
        model wedges with no in-band exit."""
        fam = seat.FAMILIES[SECOND_FREE_LANE]
        fallback = fam["model_fallback"]
        want = fam["model_context"][fallback]
        self.assertLess(want, fam["max_context"])       # control: they differ
        line = seat.launch_line(SECOND_FREE_LANE, model=fallback, room="r")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % want, line)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % want, line)
        # and the DEFAULT launch still carries the family window, so the arm
        # above is about the per-model key and not about a shrunken family.
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % fam["max_context"],
                      seat.launch_line(SECOND_FREE_LANE, room="r"))

    def test_the_degraded_alias_is_what_the_vanished_default_degrades_to(self):  # noqa: VACUOUS_ASSERTION — the first assertEqual runs unconditionally on the intact listing and is the positive control: a chooser that answered nothing fails there, before the deletion
        """The window arm above is only worth anything if the fallback is the
        alias the SHIPPED degrade chooser actually returns."""
        fam = seat.FAMILIES[SECOND_FREE_LANE]
        rows = fam["model_providers"]
        default_row = [r for r in rows.values() if r.get("default")][0]
        alive = {r["upstream_model"]:
                 {"pricing": {"prompt": "0", "completion": "0"},
                  "supported_parameters": ["tools"]}
                 for r in rows.values()}
        # CONTROL: with everything listed, the default stands.
        self.assertEqual(seat_catalog.family_degraded_model(fam, alive),
                         fam["model"])
        del alive[default_row["upstream_model"]]
        self.assertEqual(seat_catalog.family_degraded_model(fam, alive),
                         fam["model_fallback"])

    def test_the_rejected_candidate_is_refused_in_BOTH_spellings(self):
        """The task offered two models. The one not chosen is recorded with
        the reason, in the served spellings and in the spelling the task
        used — a table naming ids the vendor does not list blocks nothing,
        and the short name is the one the next reader will type."""
        refused = seat.FAMILIES[SECOND_FREE_LANE]["disqualified_models"]
        for spelling in ("nvidia/nemotron-3-ultra-550b-a55b",
                         "nvidia/nemotron-3-ultra-550b-a55b:free",
                         "nvidia/nemotron-3-ultra-550b:free"):
            self.assertIn(spelling, refused, spelling)
            self.assertGreater(len(refused[spelling]), 40, spelling)
        # the FREE id is refused on TERMS and the PAID twin on price: two
        # different disqualifications, and folding them would lose one.
        self.assertIn("data policy",
                      refused["nvidia/nemotron-3-ultra-550b-a55b:free"])
        self.assertIn("BILLS", refused["nvidia/nemotron-3-ultra-550b-a55b"])

    def test_the_two_free_lanes_share_a_key_and_nothing_else(self):
        """ONE VENDOR CREDENTIAL, TWO CONFIGS. The credential-isolation
        invariant this file already measures is per PROVIDER BLOCK; two
        families on one key extend it across seats, and they can only do that
        while their ports and config identities differ."""
        one, two = seat.FAMILIES[FAMILY], seat.FAMILIES[SECOND_FREE_LANE]
        # CONTROL: both tables are populated, so the disjointness below is a
        # fact about two real sets rather than about two empty ones.
        self.assertTrue(one["model_providers"] and two["model_providers"])
        self.assertEqual(one["key_env"], two["key_env"])
        self.assertEqual(one["base_url"], two["base_url"])
        self.assertNotEqual(one["port"], two["port"])
        self.assertEqual(set(one["model_providers"])
                         & set(two["model_providers"]), set())


if __name__ == "__main__":
    unittest.main()
