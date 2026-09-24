#!/usr/bin/env python3
"""Task/2098: a family may catalogue SEVERAL routes for one alias.

Refusing that shape made a whole family UNSUPERVISABLE: `helm seat doctor
--ensure` could compute no desired state, answered UNKNOWN, and by its own
fail-closed law refused to touch the row -- so that seat got no respawn on a
dead proxy, no drift reconciliation and no health proof.

SELECTION IS THE WRONG FRAME, and an earlier cut of this cure got it wrong by
picking the first provider in file order. A multi-provider alias has NO
singular route until an authenticated canary names the credential the proxy
actually picked (proxywatch._proxy_config_route says so), because the proxy
selects across credentials by availability and priority, not by a block's
position. So the desired state covers the WHOLE ELIGIBLE SET: a provider that
can be selected must be able to serve what it is selected for.
"""
import os
import tempfile
import unittest

from helm import seat            # seeds the late-bound names into the impls
from helm import seat_launch_assets as assets
from helm.seat_catalog import FAMILIES

FAMILY = "ds4pro"                # noqa: SEAT_NAME — the family IS the subject:
ALIAS = "ds4-pro"                # its catalogue is what carries two routes for
MODEL = "deepseek-v4-pro"        # one alias, and a pseudonym resolves nowhere.


def _provider(name, base_url, key="k-" + "x" * 8, disabled=None):
    body = ('  - name: "%s"\n'
            '    base-url: "%s"\n' % (name, base_url))
    if disabled is not None:
        body += '    disabled: %s\n' % ("true" if disabled else "false")
    body += ('    api-key-entries:\n'
             '      - api-key: "%s"\n'
             '    models:\n'
             '      - name: "%s"\n'
             '        alias: "%s"\n' % (key, MODEL, ALIAS))
    return body


DIRECT = _provider("deepseek-direct", "https://api.deepseek.com/v1")
FALLBACK = _provider("opencode-go", "https://opencode.ai/zen/go/v1")


def _block(*providers):
    return "openai-compatibility:\n" + "".join(providers)


class EligibleRouteTest(unittest.TestCase):
    """WHICH providers are part of the desired state."""

    def eligible(self, block):
        return assets._eligible_providers(block, FAMILY, FAMILIES[FAMILY])

    def names(self, block):
        return [info["provider"] for info, _item, _index in self.eligible(block)]

    def test_two_catalogued_routes_are_BOTH_eligible_not_one(self):
        """The whole point: neither is discarded and neither is 'selected'."""
        self.assertEqual(self.names(_block(DIRECT, FALLBACK)),
                         ["deepseek-direct", "opencode-go"])

    def test_the_answer_does_not_depend_on_ORDER(self):
        """The premise this cure replaced was that file order picks a winner.
        It does not, so reversing the block must not change the answer -- only
        its sequence. An arm that pinned order was pinning a rule the proxy
        never implemented.
        """
        want = ["deepseek-direct", "opencode-go"]
        # ANCHORED ON THE LITERAL, not on each other: two empty lists are also
        # equal, and an eligibility check that returned nothing at all would
        # satisfy a comparison between its own two answers.
        self.assertEqual(sorted(self.names(_block(DIRECT, FALLBACK))), want)
        self.assertEqual(sorted(self.names(_block(FALLBACK, DIRECT))), want)

    def test_a_DISABLED_provider_is_not_a_route(self):
        """HOSTILE STATE 1. The proxy skips a disabled credential at selection,
        so alias rows written there satisfy nothing while every enabled sibling
        stays stale -- and the plan would then report no drift, because the
        rows it looked for are technically present somewhere.
        """
        off = _provider("deepseek-direct", "https://api.deepseek.com/v1",
                        disabled=True)
        self.assertEqual(self.names(_block(off, FALLBACK)), ["opencode-go"])
        # MUST-DIFFER on the same pair: enabled, it IS eligible, so the
        # exclusion above is the flag and not the provider.
        on = _provider("deepseek-direct", "https://api.deepseek.com/v1",
                       disabled=False)
        self.assertEqual(self.names(_block(on, FALLBACK)),
                         ["deepseek-direct", "opencode-go"])

    def test_an_UNREADABLE_disabled_flag_removes_the_provider(self):
        """Unreadable is not enabled: a flag this reader cannot parse takes the
        provider OUT of the desired state rather than silently admitting it.
        """
        weird = _provider("deepseek-direct", "https://api.deepseek.com/v1")
        weird = weird.replace('    api-key-entries:',
                              '    disabled: maybe\n    api-key-entries:', 1)
        self.assertEqual(self.names(_block(weird, FALLBACK)), ["opencode-go"])

    def test_an_UNSAFE_endpoint_is_never_eligible(self):
        insecure = _provider("deepseek-direct", "http://api.deepseek.com/v1")
        self.assertEqual(self.names(_block(insecure, FALLBACK)),
                         ["opencode-go"])

    def test_an_outside_table_endpoint_does_not_SHADOW_an_exact_route(self):
        """HOSTILE STATE 3. Eligibility is decided per provider rather than by
        position, so a pool endpoint outside the static table cannot shadow
        anything: the exact catalogued route is eligible on its own terms
        whatever precedes it in the file.
        """
        stranger = _provider("not-in-the-catalogue",
                             "https://example.invalid/v1")
        self.assertIn("deepseek-direct", self.names(_block(stranger, DIRECT)))

    def test_ZERO_eligible_routes_still_REFUSES(self):
        """The refusal is narrowed, not deleted: an empty eligible set is still
        an unreadable desired state, and the watchdog must keep saying UNKNOWN
        rather than inventing a provider.
        """
        stranger = _provider("not-in-the-catalogue",
                             "https://example.invalid/v1")
        with self.assertRaises(ValueError) as caught:
            self.eligible(_block(stranger))
        self.assertIn("0 catalogued routes", str(caught.exception))

    def test_ONE_catalogued_route_is_unchanged(self):
        self.assertEqual(self.names(_block(DIRECT)), ["deepseek-direct"])


class DisabledFlagTest(unittest.TestCase):
    """The flag the eligibility walk depends on, read off the real spellings.

    Eligibility skips a disabled provider, so every misread here is either a
    provider the operator switched off being written into, or one they left on
    going stale. The parser has to REACH the flag before "unreadable is not
    enabled" can mean anything.
    """

    def disabled(self, body):
        return assets._provider_disabled(body)

    def _item(self, *lines):
        return "".join(lines)

    def test_the_flag_as_the_items_FIRST_key_is_still_the_items_flag(self):  # noqa: VACUOUS_ASSERTION — the assertTrue on the same spelling is the unconditional positive control; the assertFalse beside it is the must-differ on the VALUE
        """MEASURED MISS: the list marker is on the same line as the first
        key, so a line-start match never saw `- disabled: true` and read a
        switched-off provider as eligible.
        """
        self.assertTrue(self.disabled(self._item(
            '  - disabled: true\n',
            '    name: "deepseek-direct"\n')))
        # MUST-DIFFER on the same shape: false in the same position is not
        # disabled, so the arm above is about the VALUE and not the position.
        self.assertFalse(self.disabled(self._item(
            '  - disabled: false\n',
            '    name: "deepseek-direct"\n')))

    def test_an_UNREADABLE_value_is_disabled_in_every_position(self):  # noqa: VACUOUS_ASSERTION — the unconditional control below runs outside the loop
        """The rule the docstring always claimed, now true. A value that is
        not exactly a boolean cannot establish that a provider is enabled.
        """
        for line in ('  - disabled: maybe\n', '    disabled: maybe\n',
                     '    disabled:\n', '    disabled: "false#no-space"\n'):
            item = '  - name: "deepseek-direct"\n' + (
                line if line.startswith('    ') else line)
            if line.startswith('  - '):
                item = line + '    name: "deepseek-direct"\n'
            self.assertTrue(self.disabled(item),
                            "unreadable %r must not read as enabled" % line)
        # UNCONDITIONAL CONTROL, outside the loop: an empty spelling list would
        # make every assertion above vacuous, and this proves the same reader
        # still answers False for a value it CAN read.
        self.assertFalse(self.disabled(
            '  - name: "deepseek-direct"\n    disabled: false\n'))

    def test_an_inline_COMMENT_is_not_part_of_the_value(self):  # noqa: VACUOUS_ASSERTION — the true-with-comment assertion is the unconditional positive control on the same observable
        """MEASURED MISS: `false # enabled fallback` compared the whole tail
        against "false" and disabled a provider the operator left enabled.
        """
        self.assertFalse(self.disabled(self._item(
            '  - name: "deepseek-direct"\n',
            '    disabled: false # enabled fallback\n')))
        self.assertTrue(self.disabled(self._item(
            '  - name: "deepseek-direct"\n',
            '    disabled: true # switched off for the week\n')))

    def test_a_flag_nested_UNDER_a_model_is_not_the_providers_flag(self):
        """Depth is part of identity. Reading any `disabled:` at any column as
        the provider's own is the same mistake as matching a section by name
        and taking the first hit.
        """
        self.assertFalse(self.disabled(self._item(
            '  - name: "deepseek-direct"\n',
            '    models:\n',
            '      - name: "%s"\n' % MODEL,
            '        alias: "%s"\n' % ALIAS,
            '        disabled: true\n')))
        # POSITIVE CONTROL on the same observable: the SAME flag moved to the
        # provider\'s own depth does read as disabled, so the answer above is
        # about DEPTH and not about a parser that cannot see this spelling.
        self.assertTrue(self.disabled(self._item(
            '  - name: "deepseek-direct"\n',
            '    disabled: true\n',
            '    models:\n',
            '      - name: "%s"\n' % MODEL,
            '        alias: "%s"\n' % ALIAS)))

    def test_ordinary_true_and_false_still_decide(self):  # noqa: VACUOUS_ASSERTION — the bare `disabled: true` assertion is the unconditional positive control on the same observable
        """The controls the misreads above must not have broken."""
        self.assertTrue(self.disabled(self._item(
            '  - name: "deepseek-direct"\n', '    disabled: true\n')))
        self.assertFalse(self.disabled(self._item(
            '  - name: "deepseek-direct"\n', '    disabled: false\n')))
        self.assertFalse(self.disabled(self._item(
            '  - name: "deepseek-direct"\n')))


class DuplicateNameCustodyTest(unittest.TestCase):
    """HOSTILE STATE 2: two provider blocks sharing one name."""

    def test_a_STALE_first_section_is_merged_and_the_second_is_untouched(self):
        """A config carrying two same-named blocks must come back carrying
        both: a replacer that appends only while it has not yet replaced and
        sends everything else down an else-branch DELETES the later one --
        endpoint, credentials and comments -- during a regeneration meant only
        to refresh alias rows.

        The first section is deliberately STALE -- no models block at all --
        so "the first was merged" is a change and not a fixture that already
        matched the canonical body. The second is compared BYTE FOR BYTE
        against what went in, which is the only assertion that can tell
        custody from a merge that happened to copy everything through.
        """
        stale = ('  - name: "deepseek-direct"\n'
                 '    base-url: "https://api.deepseek.com/v1"\n'
                 '    api-key-entries:\n'
                 '      - api-key: "k-%s"\n' % ("s" * 8))
        second = _provider("deepseek-direct", "https://api.deepseek.com/v1",
                           key="k-" + "y" * 8)
        block = _block(stale, second)
        canonical = _block(_provider("deepseek-direct",
                                     "https://api.deepseek.com/v1"))
        merged = assets._replace_nested(
            block, canonical, "deepseek-direct",
            r'^  - name:\s*(.+?)\s*$', family_model=ALIAS, selected_index=0)

        _prefix, sections = assets._provider_sections(merged)
        self.assertEqual(len(sections), 2, "both sections must survive")
        self.assertEqual(sections[1][1], second,
                         "the untouched section must be byte-identical")
        self.assertIn('alias: "%s"' % ALIAS, sections[0][1],
                      "the merged section must have gained the alias rows")
        self.assertIn("k-" + "s" * 8, sections[0][1],
                      "the merged section keeps its own credential")

    def test_the_SECOND_section_can_be_the_one_that_is_merged(self):
        """MUST-DIFFER on the index itself. With name-based selection this is
        unreachable: the first hit always won, which is exactly how an
        excluded block got rewritten while the eligible one stayed stale.
        """
        first = _provider("deepseek-direct", "https://api.deepseek.com/v1",
                          key="k-" + "a" * 8)
        stale = ('  - name: "deepseek-direct"\n'
                 '    base-url: "https://api.deepseek.com/v1"\n'
                 '    api-key-entries:\n'
                 '      - api-key: "k-%s"\n' % ("b" * 8))
        block = _block(first, stale)
        canonical = _block(_provider("deepseek-direct",
                                     "https://api.deepseek.com/v1"))
        merged = assets._replace_nested(
            block, canonical, "deepseek-direct",
            r'^  - name:\s*(.+?)\s*$', family_model=ALIAS, selected_index=1)

        _prefix, sections = assets._provider_sections(merged)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0][1], first,
                         "section 0 was not selected and must not change")
        self.assertIn('alias: "%s"' % ALIAS, sections[1][1])
        self.assertIn("k-" + "b" * 8, sections[1][1])

    def test_an_index_outside_the_sections_REFUSES(self):  # noqa: VACUOUS_ASSERTION — assertRaises IS the positive observable; an in-range index on the same call is asserted to merge
        """A plan writing into a block it did not measure is the harm this
        argument exists to end, so it raises rather than falling back."""
        block = _block(DIRECT)
        canonical = _block(_provider("deepseek-direct",
                                     "https://api.deepseek.com/v1"))
        with self.assertRaises(ValueError):
            assets._replace_nested(
                block, canonical, "deepseek-direct",
                r'^  - name:\s*(.+?)\s*$', family_model=ALIAS,
                selected_index=7)
        # POSITIVE CONTROL on the same call: an in-range index does merge, so
        # the refusal above is about the RANGE and not about a call that
        # cannot work at all.
        merged = assets._replace_nested(
            block, canonical, "deepseek-direct",
            r'^  - name:\s*(.+?)\s*$', family_model=ALIAS, selected_index=0)
        self.assertIn('alias: "%s"' % ALIAS, merged)


class MultiRoutePlanTest(unittest.TestCase):
    """THE REAL ENTRY POINT. These call proxy_config_plan on a file.

    A test that reconstructs the plan's own loop by hand can only ever agree
    with the code it copies: every defect living in how the plan CALLS its
    helpers is invisible to it, and two such defects were live in this module.
    Everything here goes through the door production uses.
    """

    def _plan(self, *providers):
        config = ('host: "127.0.0.1"\n'
                  'port: 8360\n'
                  'api-keys:\n'
                  '  - "%s"\n'
                  'auth-dir: "/tmp/does-not-matter"\n' % ("a" * 64)
                  + _block(*providers))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(config)
            return assets.proxy_config_plan(path, FAMILY)

    def _sections(self, text):
        _prefix, blocks = assets._top_blocks(text)
        block = dict(blocks)["openai-compatibility"]
        _p, sections = assets._provider_sections(block)
        return [body for _name, body in sections]

    def test_EVERY_eligible_provider_gets_the_schema_alias_rows(self):
        """A provider that can be selected must be able to serve what it is
        selected for, so the rows go on all of them and not on a winner."""
        plan = self._plan(DIRECT, FALLBACK)
        direct, fallback = self._sections(plan["text"])
        self.assertIn('alias: "claude-opus-5"', direct)
        self.assertIn('alias: "claude-opus-5"', fallback)

    def test_an_EXCLUDED_block_is_untouched_and_the_eligible_one_is_not(self):
        """MEASURED REGRESSION: with the merge bound to a NAME, an excluded
        same-named block that came FIRST absorbed the canonical body while the
        eligible block later in the file stayed stale -- the exact inversion of
        what the plan is for. The excluded section is compared byte for byte.
        """
        excluded = _provider("deepseek-direct", "https://api.deepseek.com/v1",
                             key="k-" + "z" * 8, disabled=True)
        plan = self._plan(excluded, DIRECT)
        first, second = self._sections(plan["text"])
        self.assertEqual(first, excluded,
                         "a disabled provider is not in the desired state and "
                         "must not be written into")
        self.assertIn('alias: "claude-opus-5"', second,
                      "the eligible provider must have received the rows")

    def _flag_provider(self, key, value, first=False):
        name = '  - name: "deepseek-direct"\n'
        flag = '%s: %s\n' % (key, value)
        header = '  - ' + flag + '    name: "deepseek-direct"\n' if first \
            else name + '    ' + flag
        return DIRECT.replace(name, header, 1)

    def test_disabled_key_spellings_preserve_the_excluded_provider(self):
        """The backend decodes all these keys as disabled, in either position."""
        keys = ('disabled', '"disabled"', "'disabled'", 'disabled ',
                '"dis\\u0061bled"')
        for key in keys:
            for first in (False, True):
                with self.subTest(key=key, first=first):
                    excluded = self._flag_provider(key, 'true', first)
                    plan = self._plan(excluded, FALLBACK)
                    direct, fallback = self._sections(plan['text'])
                    self.assertEqual(direct, excluded)
                    self.assertIn('alias: "claude-opus-5"', fallback)
        # Control: this same provider participates when no flag excludes it.
        direct, _fallback = self._sections(self._plan(DIRECT, FALLBACK)['text'])
        self.assertIn('alias: "claude-opus-5"', direct)

    def test_enabled_key_spellings_survive_merging_and_settle(self):
        """Reading a key is insufficient if merging later drops that field."""
        keys = ('disabled', '"disabled"', "'disabled'", 'disabled ',
                '"dis\\u0061bled"')
        for key in keys:
            for first in (False, True):
                with self.subTest(key=key, first=first):
                    enabled = self._flag_provider(key, 'false', first)
                    plan = self._plan(enabled, FALLBACK)
                    direct, fallback = self._sections(plan['text'])
                    self.assertIn('    %s: false\n' % key, direct)
                    self.assertIn('alias: "claude-opus-5"', direct)
                    self.assertIn('alias: "claude-opus-5"', fallback)
                    self.assertEqual(self._plan_from_text(plan['text'])['text'],
                                     plan['text'])
        self.assertTrue(self._plan(DIRECT, FALLBACK)['changed'])

    def test_tab_comment_does_not_disable_the_only_route(self):
        enabled = self._flag_provider('disabled', 'false\t# enabled')
        plan = self._plan(enabled)
        self.assertTrue(plan['changed'])
        self.assertIsNotNone(plan['alias_drift'])
        direct, = self._sections(plan['text'])
        self.assertIn('    disabled: false\t# enabled\n', direct)
        self.assertIn('alias: "claude-opus-5"', direct)
        settled = self._plan_from_text(plan['text'])
        self.assertFalse(settled['changed'])
        self.assertIsNone(settled['alias_drift'])
        # Same comment grammar, opposite flag: the only route really is off.
        with self.assertRaisesRegex(ValueError, '0 catalogued routes'):
            self._plan(self._flag_provider('disabled', 'true\t# disabled'))

    def test_unreadable_flag_values_remain_excluded_in_the_plan(self):
        for value in ('maybe', '', '"false # not a boolean"',
                      "'false # not a boolean'", '"false', 'false#literal',
                      '"false"', "'false'", '"fal\\u0073e"', 'FaLsE'):
            with self.subTest(value=value):
                excluded = self._flag_provider('"disabled"', value)
                direct, fallback = self._sections(
                    self._plan(excluded, FALLBACK)['text'])
                self.assertEqual(direct, excluded)
                self.assertIn('alias: "claude-opus-5"', fallback)
        enabled = self._flag_provider('"disabled"', 'false # comment')
        direct, = self._sections(self._plan(enabled)['text'])
        self.assertIn('alias: "claude-opus-5"', direct)

    def test_nested_disabled_flag_does_not_exclude_the_provider_in_the_plan(self):
        nested = DIRECT + '        disabled: true\n'
        direct, = self._sections(self._plan(nested)['text'])
        self.assertIn('        disabled: true\n', direct)
        self.assertIn('alias: "claude-opus-5"', direct)
        with self.assertRaisesRegex(ValueError, '0 catalogued routes'):
            self._plan(self._flag_provider('"disabled"', 'true'))

    def test_an_uninterpreted_item_is_SKIPPED_and_REPORTED_not_fatal(self):
        """CONTRACT CHANGE, chosen explicitly: an item whose own-depth syntax
        this grammar cannot read is skipped for selection, preserved
        byte-for-byte, and REPORTED -- it no longer takes the whole plan down.

        The earlier rule refused the whole plan, on the reasoning that
        unsupported YAML must not hide a disabled flag behind a healthy
        sibling. Skipping cannot hide a flag, because nothing is ever WRITTEN
        into a block that was skipped. What skipping alone WOULD hide is the
        mirror failure -- a real enabled route quietly missing from the
        desired state while the plan reports no drift -- and that is what the
        report closes. Refusing instead made an unreadable block belonging to
        NOBODY take a whole family dark, which is the failure this module
        exists to end.
        """
        for field in ('    <<: {disabled: true}\n',
                      '    ? disabled\n    : true\n'):
            with self.subTest(field=field):
                unsupported = DIRECT.replace('    base-url:',
                                             field + '    base-url:', 1)
                plan = self._plan(unsupported, FALLBACK)
                # THE PLAN LANDS, on the readable candidate...
                _opaque_body, fallback = self._sections(plan['text'])
                self.assertIn('alias: "claude-opus-5"', fallback)
                # ...AND SAYS SO. A silent skip is the thing this replaces.
                self.assertEqual([row['index'] for row in plan['opaque']], [0])
                self.assertEqual(plan['opaque'][0]['ordinal'], 1)
                # THE NAME, decoded — never the item's raw first line, which
                # can be its credential (see test_seat_config_drift's
                # test_the_opaque_line_never_prints_a_CREDENTIAL).
                self.assertEqual(plan['opaque'][0]['name'], 'deepseek-direct')
                # THE SKIPPED BLOCK IS UNTOUCHED, byte for byte.
                self.assertEqual(_opaque_body, unsupported)
        # UNCONDITIONAL POSITIVE CONTROL: with nothing unsupported, BOTH get
        # their rows and NOTHING is reported opaque -- so the report above is
        # the unreadable block and not a field that is always populated.
        clean = self._plan(DIRECT, FALLBACK)
        direct, fallback = self._sections(clean['text'])
        self.assertIn('alias: "claude-opus-5"', direct)
        self.assertIn('alias: "claude-opus-5"', fallback)
        self.assertEqual(clean['opaque'], [])

    def test_the_opaque_block_survives_a_REAL_repair_of_its_neighbour(self):
        """The boundary: `_provider_sections` is ALSO the splitter the
        final replacement uses, so an eligibility-only skip leaves the merge
        path free to mangle a block nobody could read.

        The neighbour here genuinely NEEDS repair -- its alias rows are stale,
        so the plan actually rewrites that section -- which is what makes the
        opaque block's survival mean something. Against a plan that changed
        nothing, identical bytes would prove only that no write happened.
        """
        stale = ('  - name: "opencode-go"\n'
                 '    base-url: "https://opencode.ai/zen/go/v1"\n'
                 '    api-key-entries:\n'
                 '      - api-key: "k-%s"\n'
                 '    models:\n'
                 '      - name: "%s"\n'
                 '        alias: "stale-alias-nobody-routes"\n'
                 % ("x" * 8, MODEL))
        opaque = DIRECT.replace('    base-url:',
                                '    <<: {disabled: true}\n    base-url:', 1)
        plan = self._plan(opaque, stale)
        # THE REPAIR REALLY HAPPENED -- the precondition for the assertion
        # below, asserted rather than assumed.
        self.assertTrue(plan['changed'])
        before, after = self._sections(plan['text'])
        # THE NEIGHBOUR'S OWN SECTION IS WHAT CHANGED, asserted on the section
        # and not on the whole file -- a plan that rewrote only the opaque
        # block would also satisfy plan['changed']. The merge ADDS the
        # schema's alias rows rather than replacing the operator's, so the
        # stale row survives beside them; what proves the repair is that this
        # section is no longer the bytes that went in.
        self.assertNotEqual(after, stale)
        self.assertIn('alias: "claude-opus-5"', after)
        # AND THE OPAQUE NEIGHBOUR CAME THROUGH THE REPLACEMENT UNCHANGED.
        self.assertEqual(before, opaque)
        self.assertEqual([row['index'] for row in plan['opaque']], [0])

    def test_an_uninterpreted_item_ALONE_still_refuses_with_its_own_reason(self):
        """The refusal survives exactly where it is still true. An unreadable
        block and nothing else IS an unreadable desired state -- and the
        message is the grammar's, not a count of zero, because zero-catalogued
        and cannot-read send an operator to different places.
        """
        opaque = DIRECT.replace('    base-url:',
                                '    <<: {disabled: true}\n    base-url:', 1)
        with self.assertRaisesRegex(ValueError, 'unsupported provider mapping'):
            self._plan(opaque)
        # MUST-DIFFER on the same call: a block with NO catalogued route and
        # no unreadable item still says the OTHER sentence.
        foreign = _provider("nobody-catalogues-this",
                            "https://example.invalid/v1")
        with self.assertRaisesRegex(ValueError, '0 catalogued routes'):
            self._plan(foreign)

    def test_TWO_eligible_same_name_blocks_each_keep_their_own_identity(self):
        """MEASURED REGRESSION: the second pass overwrote the first, so one
        endpoint ended up carrying the other's aliases while retaining its own
        credentials -- a config that looks healthy and routes wrongly.
        """
        pool = _provider("opencode-go", "https://opencode.ai/zen/go/v1",
                         key="k-" + "p" * 8)
        other = _provider("opencode-go", "https://opencode.ai/zen/go/v1",
                          key="k-" + "q" * 8)
        plan = self._plan(pool, other)
        first, second = self._sections(plan["text"])
        self.assertIn("k-" + "p" * 8, first)
        self.assertIn("k-" + "q" * 8, second)
        self.assertNotIn("k-" + "q" * 8, first,
                         "the first section absorbed the second's credential")
        self.assertIn('alias: "claude-opus-5"', first)
        self.assertIn('alias: "claude-opus-5"', second)

    def _canonical_section(self, provider):
        """One provider's section AFTER the plan has made it canonical.

        DERIVED, NEVER TRANSCRIBED. The arm below needs a FIRST route whose
        alias rows are already correct, and hand-writing the schema's expected
        rows into a fixture would make the test agree with whatever I believed
        the schema was rather than with the generator. So the canonical body
        is whatever the plan itself produces for that provider alone.
        """
        plan = self._plan(provider)
        return self._sections(plan["text"])[0]

    def test_alias_drift_reports_a_STALE_LATER_route_not_only_the_first(self):
        """MEASURED REGRESSION: reading drift off eligible[0] answered "is the
        FIRST route canonical" and reported that as the config's health, so a
        canonical provider followed by a stale one returned alias_drift None
        beside changed True -- a watchdog saying nothing is wrong about the
        same pass that just rewrote something.

        THE TWO ROUTES MUST BE ASYMMETRIC OR THIS ARM PROVES NOTHING: the
        first is canonical (its own reason is None) and the second is not.
        With both routes stale every reading agrees, the aggregate cannot
        differ from the first, and the assertion holds without discriminating.
        """
        canonical = self._canonical_section(DIRECT)
        plan = self._plan(canonical, FALLBACK)
        self.assertTrue(plan["changed"],
                        "the stale later route must still be rewritten")
        self.assertIsNotNone(
            plan["alias_drift"],
            "a stale LATER route was reported as no drift because only the "
            "first route's health was consulted")

    def test_a_config_ALREADY_canonical_reports_no_drift(self):  # noqa: VACUOUS_ASSERTION — the pre-plan assertIsNotNone/assertTrue on the SAME config and the SAME instrument are the unconditional positive controls
        """MUST-DIFFER control on the arm above, and on the same shape: the
        first route is canonical there too, so an aggregation that always
        answers 'drift' fails here while satisfying the arm above."""
        first = self._plan(DIRECT, FALLBACK)
        # POSITIVE CONTROL on the same instrument and the same input: before
        # the plan ran, THIS config did report drift. Without it, an
        # aggregation that lost its ability to report anything satisfies the
        # assertions below exactly as well as a correct one.
        self.assertIsNotNone(first["alias_drift"])
        self.assertTrue(first["changed"])
        settled = self._plan_from_text(first["text"])
        self.assertIsNone(settled["alias_drift"])
        self.assertFalse(settled["changed"],
                         "a canonical config must be a no-op second time")

    def _plan_from_text(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return assets.proxy_config_plan(path, FAMILY)


if __name__ == "__main__":
    unittest.main()
