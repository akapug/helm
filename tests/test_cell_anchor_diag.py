#!/usr/bin/env python3
"""post_json must be able to say WHY it returned None, and anchor() must say it.

`except HTTPError: return None` collapsed a 401, a 404, a 500 and a refused
connection into one value, so anchor() could only report "node
unreachable/locked at <url>" — a disjunction it had no way to resolve.

Live 2026-07-25: every helm premise attestation had been failing its external
anchor with that message while the node was demonstrably UP — GET /api/receipts
returned 200 on the same host and port that POST /turn/submit answered 401. The
message actively pointed away from the fault, because an operator reading
"unreachable" checks whether the node is running, and it was running.

Third instance of the shape in one evening: the watchdog counting its own input,
`helm chat node up` reporting "the API never answered" while dregg had already
printed the exact remedy, and this. Every time, an error path discarded the
discriminator and then reported the ambiguity as if it were the finding.
"""
import io
import unittest
import urllib.error
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import cell  # noqa: E402


def _http_error(code, body=b""):
    return urllib.error.HTTPError("http://127.0.0.1:8899/turn/submit", code,
                                  "err", {}, io.BytesIO(body))


class PostJsonDiagTest(unittest.TestCase):
    def test_an_http_status_reaches_the_caller(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401, b"locked")):
            diag = {}
            self.assertIsNone(cell.post_json("http://x/turn/submit", {}, diag=diag))
        self.assertEqual(diag["status"], 401)
        self.assertIn("401", diag["reason"])
        self.assertIn("locked", diag["body"])

    def test_a_transport_failure_is_distinguishable_from_a_status(self):
        """The whole point: 'refused' and '401' must not look alike."""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            diag = {}
            cell.post_json("http://x/turn/submit", {}, diag=diag)
        self.assertIsNone(diag["status"])
        self.assertIn("refused", diag["reason"])

    def test_the_fail_open_contract_is_unchanged_for_callers_that_pass_no_diag(self):
        """Every existing caller keeps the None law and needs no edit."""
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            self.assertIsNone(cell.post_json("http://x/y", {}))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(cell.post_json("http://x/y", {}))

    def test_a_success_still_returns_the_parsed_body_and_leaves_diag_empty(self):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"accepted": true}'
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            diag = {}
            self.assertEqual(cell.post_json("http://x/y", {}, diag=diag),
                             {"accepted": True})
        self.assertEqual(diag, {})


class AnchorMessageTest(unittest.TestCase):
    def test_a_401_is_reported_as_AUTH_not_as_unreachable(self):
        """The message must not send the reader to check whether the node is up,
        because for this failure the node IS up and that check comes back fine."""
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)):
            got, err = cell.anchor_submit("deadbeef")
        self.assertIsNone(got)
        self.assertIn("401", err)
        self.assertIn("auth problem", err)
        self.assertNotIn("unreachable", err)

    def test_a_real_transport_failure_still_reads_as_one(self):
        """The fix must not swing the other way and call every failure auth."""
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("Connection refused")):
            got, err = cell.anchor_submit("deadbeef")
        self.assertIsNone(got)
        self.assertIn("refused", err.lower())
        self.assertNotIn("auth problem", err)

    def test_the_error_names_the_url_it_actually_posted_to(self):
        """`/turn/submit`, not just the base — the base answers 200 on GET, so
        naming only the host is what made this look like a liveness problem."""
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(403)):
            _got, err = cell.anchor_submit("deadbeef")
        self.assertIn(cell.ANCHOR_ENDPOINT, err)


if __name__ == "__main__":
    unittest.main()
