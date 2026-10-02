#!/usr/bin/env python3
"""OpenCode Go seats carry a per-conversation session header (task/3824).

OpenCode Go refuses a request without `x-opencode-session` (HTTP 400
MissingSessionID), and one static value for a whole family would make every
conversation one. The proxy fork stamps the header per conversation when a
provider block names it (`session-header`, CLIProxyAPI
lane/opencode-go-session-3824), so the catalog declares the header on every
Go row, the generator writes it into that row's block, the reconcile keeps it,
and a proxy binary that predates the field is refused before it starts a
listener every Go request would bounce off.
"""
import os
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import seat, seat_catalog  # noqa: E402
from helm import seat_launch_assets as sla  # noqa: E402

FLASH = "ds4flash"  # noqa: SEAT_NAME — the catalog FAMILY key, never a seat
PRO = "ds4pro"  # noqa: SEAT_NAME — the catalog FAMILY key, never a seat
GO_URL = "https://opencode.ai/zen/go/v1"
HEADER = "x-opencode-session"
HEADER_LINE = '    session-header: "%s"\n' % HEADER


def _go_rows():
    """(family, provider, row) for every pool row on the Go endpoint."""
    return [(family, row.get("proxy_provider") or name, row)
            for family, fam in sorted(seat.FAMILIES.items())
            for name, row in sorted((fam.get("pool_providers") or {}).items())
            if isinstance(row, dict)
            and seat_catalog.pool_base_url(row)[0] == GO_URL]


class CatalogDeclaresTheHeaderOnEveryGoRow(unittest.TestCase):

    def test_ds4flash_is_activatable(self):  # noqa: VACUOUS_ASSERTION — the census over every family is the positive control
        self.assertNotIn("activation_refusal", seat.FAMILIES[FLASH])
        # CONTROL: no family carries a refusal any more, so the key is gone
        # rather than moved
        self.assertEqual([f for f, fam in seat.FAMILIES.items()
                          if fam.get("activation_refusal")], [])

    def test_every_go_row_names_the_session_header(self):  # noqa: VACUOUS_ASSERTION — assertEqual on the two Go families and on the header value are positive
        rows = _go_rows()
        self.assertEqual(sorted({f for f, _p, _r in rows}), [FLASH, PRO])
        for family, provider, _row in rows:
            with self.subTest(family=family):
                self.assertEqual(seat_catalog.provider_session_header(
                    family, provider), HEADER)
        # CONTROL: the DeepSeek direct key needs no session, and a provider
        # the family does not declare answers None
        self.assertIsNone(seat_catalog.provider_session_header(
            PRO, "deepseek-direct"))
        self.assertIsNone(seat_catalog.provider_session_header(PRO, "bogus"))

    def test_ds4pro_can_ride_go_on_the_same_model_without_a_second_source(self):  # noqa: VACUOUS_ASSERTION — assertEqual on the row fields and the route family are positive
        """The owner: "ds4pro works through the account". The Go
        row serves the SAME model, so the pool is still one model across two
        vendors, and the mint writes ONE of them: the direct key stays the
        default until an operator names opencode-go at the mint."""
        fam = seat.FAMILIES[PRO]
        self.assertEqual(fam["pool_default"], "deepseek")
        go = fam["pool_providers"]["opencode-go"]
        self.assertEqual(go["upstream_model"], "deepseek-v4-pro")
        self.assertEqual(go["rung"], "free")
        self.assertEqual(go["authstore"], "opencode-go")
        self.assertEqual(go["vendor"], "opencode")
        self.assertNotIn("billing_window", go)
        route = {"alias": "ds4-pro", "provider": "opencode-go",
                 "upstream_model": "deepseek-v4-pro", "base_url": GO_URL}
        self.assertIn(route, seat.proxy_routes(PRO))
        self.assertEqual(seat.proxy_route_family(route), (PRO, None))
        self.assertIsNone(seat_catalog._pool_serves_one_model())


class GeneratorWritesTheHeader(unittest.TestCase):

    def test_the_block_names_the_header_only_when_asked(self):
        text = sla._config_yaml_key(8330, "tok", "opencode-go", GO_URL,
                                    "deepseek-v4-flash", "key",
                                    "deepseek-v4.1-flash",
                                    session_header=HEADER)
        self.assertIn('    base-url: "%s"\n%s    api-key-entries:\n'
                      % (GO_URL, HEADER_LINE), text)
        control = sla._config_yaml_key(8330, "tok", "opencode-go", GO_URL,
                                       "deepseek-v4-flash", "key",
                                       "deepseek-v4.1-flash")
        self.assertEqual(text.replace(HEADER_LINE, ""), control)

    def test_the_many_block_path_carries_it_per_row(self):
        rows = [{"provider": "opencode-go", "base_url": GO_URL,
                 "upstream": "deepseek-v4-pro", "alias": "ds4-pro",
                 "session_header": HEADER},
                {"provider": "deepseek-direct",
                 "base_url": "https://api.deepseek.com/v1",
                 "upstream": "deepseek-v4-pro", "alias": "ds4-pro",
                 "frontmatter": False}]
        text = sla._config_yaml_key(8360, "tok", None, None, None, "key",
                                    providers=rows)
        self.assertEqual(text.count("session-header:"), 1)
        go, direct = text.split('  - name: "deepseek-direct"\n')
        self.assertIn(HEADER_LINE, go)
        self.assertNotIn("session-header", direct)


class _Home(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp.name, "helm-home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp.name, "chat")})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["HELM_HOME"])

    def mint(self, family, text):
        d = seat.seat_dir(family)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


def _flash_config(**kw):
    return sla._config_yaml_key(8330, "tok", "opencode-go", GO_URL,
                                "deepseek-v4-flash", "key",
                                "deepseek-v4.1-flash", **kw)


class ReconcileKeepsAndRestoresTheHeader(_Home):

    def test_a_minted_go_config_is_already_the_desired_state(self):  # noqa: VACUOUS_ASSERTION — the restore arm below plans a change on the same generator
        path = self.mint(FLASH, _flash_config(session_header=HEADER))
        plan = sla.proxy_config_plan(path, FLASH)
        self.assertFalse(plan["changed"], plan["text"])
        self.assertIsNone(plan["alias_drift"])

    def test_a_go_block_without_the_header_is_given_it(self):
        """The config staged before the seam carried one static header for
        the whole family; the reconcile adds the per-conversation field, and
        the proxy's value replaces the static one on the wire."""
        stale = _flash_config().replace(
            '    api-key-entries:\n',
            '    headers:\n      %s: "ses_helmstaticfamilywide00000"\n'
            '    api-key-entries:\n' % HEADER, 1)
        path = self.mint(FLASH, stale)
        plan = sla.proxy_config_plan(path, FLASH)
        self.assertTrue(plan["changed"])
        self.assertIn(HEADER_LINE, plan["text"])
        self.assertEqual(plan["text"].count("session-header:"), 1)

    def test_the_direct_key_block_gets_no_header(self):  # noqa: VACUOUS_ASSERTION — the Go arms above assert the header present on the same plan
        """CONTROL: ds4pro's minted direct-key config is untouched."""
        route = [r for r in seat.proxy_routes(PRO)
                 if r["provider"] == "deepseek-direct"][0]
        text = sla._config_yaml_key(8360, "tok", route["provider"],
                                    route["base_url"], route["alias"], "key",
                                    route["upstream_model"])
        path = self.mint(PRO, text)
        with mock.patch("helm.offpeak.apply_gate",
                        side_effect=lambda old, *a, **k: old):
            plan = sla.proxy_config_plan(path, PRO)
        self.assertNotIn("session-header", plan["text"])


class ProxyBinaryMustKnowTheField(unittest.TestCase):
    """A binary that predates `session-header` ignores the key, sends no
    session, and every Go request is refused 400 MissingSessionID. The start
    preflight reads the binary for the field before a listener starts."""

    def binary(self, carries):
        fd, path = tempfile.mkstemp(prefix="fake-cli-proxy-api-")
        self.addCleanup(os.unlink, path)
        body = b"\x7fELF" + b"\0" * 4096
        if carries:
            body += b'json:"compaction-cap,omitempty"yaml:"session-header,omitempty"'
        body += b"\0" * 4096
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return path

    def test_a_binary_without_the_field_is_refused(self):
        refusal, notes = sla.family_start_refusal(
            FLASH, seat.FAMILIES[FLASH], _flash_config(session_header=HEADER),
            binary=self.binary(False))
        self.assertEqual(notes, ())
        self.assertIn("session-header", refusal)
        self.assertIn("MissingSessionID", refusal)

    def test_a_binary_with_the_field_is_admitted(self):  # noqa: VACUOUS_ASSERTION — the refused arm above reads the same preflight
        refusal, notes = sla.family_start_refusal(
            FLASH, seat.FAMILIES[FLASH], _flash_config(session_header=HEADER),
            binary=self.binary(True))
        self.assertIsNone(refusal)
        self.assertEqual(notes, ())

    def test_a_config_that_asks_for_no_header_needs_nothing_of_the_binary(self):  # noqa: VACUOUS_ASSERTION — the refused arm above reads the same preflight with the header
        refusal, _notes = sla.family_start_refusal(
            FLASH, seat.FAMILIES[FLASH], _flash_config(),
            binary=self.binary(False))
        self.assertIsNone(refusal)

    def test_an_unreadable_binary_is_refused_not_admitted(self):
        refusal, _notes = sla.family_start_refusal(
            FLASH, seat.FAMILIES[FLASH], _flash_config(session_header=HEADER),
            binary=os.path.join(tempfile.gettempdir(), "no-such-proxy-3824"))
        self.assertIn("session-header", refusal)


if __name__ == "__main__":
    unittest.main()
