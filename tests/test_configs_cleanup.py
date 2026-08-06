#!/usr/bin/env python3
"""The configs module returns every process-global fixture it borrowed."""
import os
import unittest

from helm import configs
from tests import test_configs as fixture


class ConfigsCleanupTest(unittest.TestCase):
    def test_module_teardown_restores_environment_and_frozen_roots(self):  # noqa: VACUOUS_ASSERTION — the preceding module planted concrete divergent env values, root lists, and a tmp tree before this post-module witness
        self.assertEqual(
            {k: os.environ.get(k) for k in fixture._ENV_KEYS},
            fixture._ENV_PRIOR)
        self.assertEqual(configs.CWD_ROOTS, fixture._CWD_ROOTS_PRIOR)
        self.assertEqual(configs.HOME_ROOTS, fixture._HOME_ROOTS_PRIOR)
        self.assertFalse(os.path.exists(fixture._TMP))


if __name__ == "__main__":
    unittest.main()
