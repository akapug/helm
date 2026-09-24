#!/usr/bin/env python3
"""The endpoints file fails CLOSED on every shape it can be broken into.

A keyless pool row names its endpoint by key (`base_url_from`), and the URL
lives in the operator's `<helm home>/_global/endpoints.json`. A file that is
malformed, is not a JSON object, or cannot be read must answer "" with a reason
naming the file, exactly like an absent one, and never a default host: a route
that falls back to a host binds proofs and rewrites live configs to it."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, seat, seat_catalog  # noqa: E402,F401 (seat: the facade the injection audit requires beside seat_catalog)

KEY = "endpoint-under-test"
ROW = {"base_url_from": KEY}
URL = "http://192.0.2.10:8083/v1"


class EndpointsFileFailsClosedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-endpoints-")
        self.env = mock.patch.dict(os.environ, {"HELM_HOME": self.tmp})
        self.env.start()
        self.path = os.path.join(home.global_dir(), seat_catalog.ENDPOINTS_CONFIG)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def refusal(self):
        """The reason, after asserting no host came back and the file is named."""
        url, why = seat_catalog.pool_base_url(ROW)
        self.assertEqual(url, "", "a broken endpoints file must not yield a host")
        self.assertIn(self.path, why)
        return why

    def test_a_configured_endpoint_is_read(self):
        # the positive control on the same call: a good file does answer
        self.write(json.dumps({KEY: URL + "/"}))
        self.assertEqual(seat_catalog.pool_base_url(ROW), (URL, None))

    def test_absent_file_names_the_key_to_add(self):
        self.assertIn("not configured", self.refusal())

    def test_malformed_json_is_refused_not_read_as_absent(self):
        self.write("{not json")
        self.assertIn("did not read", self.refusal())

    def test_a_non_object_is_refused(self):
        self.write(json.dumps([URL]))
        self.assertIn("is not a JSON object", self.refusal())

    def test_an_unreadable_file_is_refused(self):
        # a directory at the path is unreadable as a file for any user,
        # root included, so the arm does not depend on permission bits
        os.makedirs(self.path)
        self.assertIn("did not read", self.refusal())

    def test_a_non_url_value_is_refused(self):
        self.write(json.dumps({KEY: "192.0.2.10:8083"}))
        self.assertIn("not an http(s) URL", self.refusal())


if __name__ == "__main__":
    unittest.main()
