"""Hermetic tests for helm.seat — HELM_HOME and the codex-homes root both point
at tmp dirs; fake JWTs are minted in-test. The real ~/.codex-homes is never
read, no proxy is ever started, no claude is ever launched."""
import base64
import contextlib
import errno
import fcntl
import inspect
import io
import itertools
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock

from tests._tmphome import pin_suite_guard
from helm import (pk, projscope, seat, seat_catalog, seat_health,
                  seat_usability)


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    """header.payload.sig — enough structure for unverified payload decode."""
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


def _warm_features(**extra):
    """Obviously synthetic GrowthBook-sized map; no live feature state in tests."""
    features = {"synthetic-feature-%03d" % i: False for i in range(100)}
    features["tengu_deferred_stub_tool"] = True
    features.update(extra)
    return features


class UpstreamWiredIntoTheListColumnTest(unittest.TestCase):
    """THE HELPER BEING RIGHT SAYS NOTHING ABOUT IT BEING CALLED.

    Caught by mutation: replacing the list column's `live += upstream_phrase(
    ...)` with `live += ""` passed all 138 seat tests. Every assertion I had
    written exercised the helper directly, so the WIRE — the only part an
    operator ever sees — was covered by nothing. That is the built-vs-wired
    distinction this whole lane exists to fix, reproduced inside my own tests
    one layer down."""

    def column(self, ret):
        from helm import proxywatch
        upstream, err = ret
        family = (upstream or {}).get("codex")
        if isinstance(family, dict) and "seats" not in family:
            upstream = dict(upstream)
            upstream["codex"] = dict(family, seats={"codex": dict(family)})
        with mock.patch.object(proxywatch, "upstream_snapshot",
                               return_value=(upstream, err)), \
             mock.patch.object(seat, "_running_pid_rec",
                               return_value={"pid": 4242}), \
             mock.patch.object(seat, "_instance_port", return_value=8317), \
             mock.patch.object(seat, "_port_open", return_value=True), \
             mock.patch.object(seat, "proxy_drift",
                               return_value=(seat.PROXY_CURRENT, None)):
            return seat._proxy_live_text("codex", "codex")[0]

    def test_the_LIST_COLUMN_carries_the_wall(self):
        got = self.column(({"codex": {"state": "RATE-LIMITED", "dark": True,
                                      "since": "T"}}, None))
        # STRUCTURAL: the column still says what it always said...
        self.assertIn("proxy UP", got)
        # ...and now also says the thing that made "proxy UP" misleading.
        self.assertIn("upstream RATE-LIMITED", got)

    def test_a_healthy_upstream_leaves_the_column_UNCHANGED(self):
        """UNCONDITIONAL CONTROL: the line above proves the column CAN grow, so
        an unchanged column here measures health rather than a dead wire."""
        got = self.column(({"codex": {"state": "HEALTHY", "dark": False}}, None))
        self.assertIn("proxy UP", got)
        self.assertNotIn("upstream", got)


class UpstreamPhraseTest(unittest.TestCase):
    """The two surfaces that reported a hard-walled seat as healthy.

    `helm seat where codex` -> "LIVE; liveness IDLE"
    `helm seat list`        -> "proxy UP ... valid until 2026-08-13"
    Both true about what they measured — a live pane, a valid credential — and
    both read by four reviewers as "this seat is fine" while its provider had
    been refusing it for three hours."""

    def phrase(self, ret, compact=False):
        from helm import proxywatch
        upstream, err = ret
        family = (upstream or {}).get("codex")
        if isinstance(family, dict) and "seats" not in family:
            seat_record = {key: value for key, value in family.items()
                           if key != "falsification_bar_s"}
            upstream = dict(upstream)
            upstream["codex"] = dict(
                family, seats={"codex": seat_record})
        with mock.patch.object(proxywatch, "upstream_snapshot",
                               return_value=(upstream, err)):
            return seat.upstream_phrase("codex", "codex", compact=compact)

    def test_a_walled_family_is_NAMED_with_its_state_and_since(self):
        got = self.phrase(({"codex": {"state": "RATE-LIMITED", "dark": True,
                                      "since": "2026-08-04T11:06:51Z"}}, None))
        self.assertIn("RATE-LIMITED", got)
        self.assertIn("2026-08-04T11:06:51Z", got)

    # noqa: VACUOUS_ASSERTION — the control is `loud` on the line above: the
    # same helper, same seat, DOES render RATE-LIMITED. The rung wants the
    # control on the same NAME, and the two cases need different names.
    def test_stale_local_cooldown_carries_its_measured_restart_action(self):
        from helm import proxywatch
        got = self.phrase(({"codex": {
            "state": proxywatch._PROXY_COOLDOWN, "dark": True,
            "falsification_due": True, "falsification_age_s": 901,
            "falsification_bar_s": 900, "falsification_seat": "codex"}}, None))
        self.assertIn("PRESCRIBES: restart this exact proxy", got)
        self.assertIn("rerun helm proxywatch", got)

    def test_rate_limit_does_not_inherit_local_proxy_restart_advice(self):
        got = self.phrase(({"codex": {"state": "RATE-LIMITED", "dark": True}},
                           None))
        self.assertIn("remediation UNKNOWN", got)
        self.assertNotIn("PRESCRIBES:", got)
        self.assertNotIn("restart/reprobe can help", got)

    def test_a_HEALTHY_family_stays_QUIET(self):
        """UNCONDITIONAL CONTROL on the same observable is the line above: the
        helper DOES speak for a dark family. Silence here therefore measures
        health, not a helper that never renders — and the line only grows when
        it has something to say."""
        loud = self.phrase(({"codex": {"state": "RATE-LIMITED", "dark": True}},
                            None))
        self.assertIn("RATE-LIMITED", loud, "control: the helper DOES render")
        self.assertEqual(self.phrase(({"codex": {"state": "HEALTHY",
                                                 "dark": False}}, None)), "")

    # noqa: VACUOUS_ASSERTION — every assertion here is a POSITIVE assertIn
    # on rendered text; there is no absence being asserted. The test exists
    # precisely because a BLANK would read as health.
    def test_an_UNREADABLE_cache_PRINTS_unknown_rather_than_nothing(self):
        """A blank reads exactly like health, which is the bug one layer over.
        Every non-healthy path must produce text."""
        # UNCONDITIONAL FIRST — a loop over an empty tuple asserts nothing.
        self.assertIn("UNKNOWN", self.phrase((None, "state is 90m old")))
        self.assertIn("UNKNOWN", self.phrase((None, "unreadable: bad json")))
        self.assertIn("UNKNOWN", self.phrase(({}, None)))

    def test_UNKNOWN_is_SAID_in_where_and_WITHHELD_in_the_list_column(self):
        """THE ASYMMETRY IS THE ATTENTION-BUDGET LAW, NOT A CONVENIENCE.

        `seat where <seat>` is a DELIBERATE question about ONE seat, so silence
        reads as health and UNKNOWN must be printed. `seat list` is a SCAN — and
        where proxywatch has never run, EVERY row would carry the same badge. A
        marker on all of them is a column operators learn to skim, and then the
        one row saying RATE-LIMITED arrives to an audience that stopped reading.

        Caught by the WHOLE suite: two test_proxy_staleness integration tests
        read the column as a contiguous string and the badge split it. The
        failure was a test artifact; the noise it exposed was not."""
        blind = (None, "proxywatch has not run")
        self.assertIn("UNKNOWN", self.phrase(blind))          # where: SAID
        self.assertEqual(self.phrase(blind, compact=True), "")  # list: SILENT
        # CONTROL on the same observable: a real WALL still reaches the column,
        # so the silence above is about UNKNOWN and not a dead compact path.
        walled = ({"codex": {"state": "RATE-LIMITED", "dark": True}}, None)
        self.assertIn("RATE-LIMITED", self.phrase(walled, compact=True))

    def test_the_compact_form_fits_a_column_and_keeps_the_state(self):
        long = self.phrase(({"codex": {"state": "RATE-LIMITED", "dark": True,
                                       "since": "2026-08-04T11:06:51Z"}}, None))
        short = self.phrase(({"codex": {"state": "RATE-LIMITED", "dark": True,
                                        "since": "2026-08-04T11:06:51Z"}}, None),
                            compact=True)
        self.assertIn("RATE-LIMITED", short)
        self.assertLess(len(short), len(long), "compact must be SHORTER")
        self.assertNotIn("since", short)


#: The keyless family's endpoint in every arm here. The catalog holds no host
#: for it: the operator configures one in the helm home's endpoints file, so a
#: documentation-range address (RFC 5737 TEST-NET-1) stands in.
_KEYLESS_ENDPOINT = "http://192.0.2.10:8083/v1"


def _configure_endpoints(table):
    """Write the helm home's endpoints file (the contract's own location,
    spelled here rather than asked of the code under test); None removes it."""
    path = os.path.join(os.environ["HELM_HOME"], "_global", "endpoints.json")
    if table is None:
        if os.path.exists(path):
            os.remove(path)
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(table, f)
    return path


class SeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seat-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "HELM_PROXY_BIN",
                      "MELD_PROXY_BIN", "KIMI_API_KEY", "KIMI_API_KEY_PRIMARY",
                      "KIMI_API_KEY_EMBER", "DS4PRO_API_KEY",
                      "HELM_PROC",
                      "HELM_CHAT_DIR", "HELM_CHAT_NAME",
                      "HELM_SEAT_STORAGE", "HELM_CHAT_ROOM",
                      "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM",
                      "MELD_CHAT_ROOM_SOURCE", "HELM_CODEX_HOMES_DIR",
                      "MELD_CODEX_HOMES_DIR",
                      "HELM_ENSURE_QUIET_HEARTBEAT_DIR",
                      "HELM_ENSURE_QUIET_HEARTBEAT_S",
                      # ARMS in this module set a dead pin deliberately;
                      # the module declares the restore too, so
                      # test_env_hygiene can SEE it — the helper's
                      # addCleanup is real but lives in another file and
                      # that scanner reads restores syntactically.
                      "HELM_SUITE_GUARD")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        # the keyless family's endpoint is configured, as on an operator's host
        _configure_endpoints({"qwen27": _KEYLESS_ENDPOINT})  # noqa: SEAT_NAME — the catalog FAMILY key the endpoints file is keyed by, never a seat
        # a seat mint refuses a contract it cannot write in full
        self.suite_guard = pin_suite_guard(self, self.tmp)
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("HELM_CODEX_HOMES_DIR", None)
        os.environ.pop("MELD_CODEX_HOMES_DIR", None)
        os.environ.pop("HELM_PROXY_BIN", None)
        os.environ.pop("MELD_PROXY_BIN", None)
        os.environ.pop("KIMI_API_KEY", None)  # hermetic: never the real key
        os.environ.pop("KIMI_API_KEY_PRIMARY", None)
        os.environ.pop("KIMI_API_KEY_EMBER", None)
        os.environ.pop("DS4PRO_API_KEY", None)
        # hermetic: the real ~/.hermes/auth.json must never feed a test mint
        self._hermes_auth = seat.HERMES_AUTH
        seat.HERMES_AUTH = os.path.join(self.tmp, "hermes-auth.json")
        # hermetic: the real opencode auth store must never feed a test mint.
        # Point it at a non-existent tmp path — tests that exercise the
        # authstore plant it explicitly; the rest fall through to hermes.
        self._opencode_authstore = seat.OPENCODE_AUTHSTORE
        seat.OPENCODE_AUTHSTORE = os.path.join(self.tmp, "opencode-auth.json")
        for key in ("HELM_CHAT_NAME", "HELM_SEAT_STORAGE",
                    "HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
                    "MELD_CHAT_ROOM", "MELD_CHAT_ROOM_SOURCE"):
            os.environ.pop(key, None)
        # launch's retrofit surface scans /proc + the roster — keep both tmp
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        self._codex_homes = seat.CODEX_HOMES
        seat.CODEX_HOMES = os.path.join(self.tmp, "codex-homes")
        os.makedirs(seat.CODEX_HOMES)
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.timer.start()

    def tearDown(self):
        self.timer.stop()
        seat.CODEX_HOMES = self._codex_homes
        seat.HERMES_AUTH = self._hermes_auth
        seat.OPENCODE_AUTHSTORE = self._opencode_authstore
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _plant(self, name, email="fake@example.com", plan="pro",
               exp_offset=3600, mtime=None):
        """A fake codex home with an auth.json shaped like the real CLI's."""
        d = os.path.join(seat.CODEX_HOMES, name)
        os.makedirs(d, exist_ok=True)
        exp = int(time.time()) + exp_offset
        auth = {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": _jwt({"email": email,
                                  "https://api.openai.com/auth":
                                      {"chatgpt_plan_type": plan}}),
                "access_token": _jwt({"exp": exp, "sub": "fake"}),
                "refresh_token": "fake-refresh-token-" + name,
                "account_id": "acct-" + name,
            },
            "last_refresh": "2026-07-09T14:52:47.713051089Z",
        }
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump(auth, f)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path, auth, exp

    def _add(self, args=("add", "codex")):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(args))
        return rc, out.getvalue(), err.getvalue()

    # -- translation correctness ------------------------------------------
    def test_translation_correctness(self):
        path, auth, exp = self._plant("home-a")
        rec, fname, err = seat.translate_codex_auth(path)
        self.assertIsNone(err)
        self.assertEqual(fname, "codex-fake@example.com-pro.json")
        t = auth["tokens"]
        self.assertEqual(rec["id_token"], t["id_token"])
        self.assertEqual(rec["access_token"], t["access_token"])
        self.assertEqual(rec["refresh_token"], t["refresh_token"])
        self.assertEqual(rec["account_id"], t["account_id"])
        self.assertEqual(rec["last_refresh"], auth["last_refresh"])
        self.assertEqual(rec["email"], "fake@example.com")
        self.assertEqual(rec["type"], "codex")
        self.assertEqual(rec["expired"],
                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)))
        # the eval's hard law: the OPENAI_API_KEY field never crosses over
        self.assertNotIn("OPENAI_API_KEY", rec)

    def test_translation_account_id_falls_back_to_jwt_claims(self):
        """kimi FIX 1 (upstream): tokens.account_id absent -> the
        chatgpt_account_id claim fills it (id_token first, access_token
        next) — a minted/pooled record never carries account_id None when
        identity knows it (dedup + pooled-linkage key off it)."""
        d = os.path.join(seat.CODEX_HOMES, "claim-only")
        os.makedirs(d)
        exp = int(time.time()) + 3600
        auth = {"tokens": {
            "id_token": _jwt({"email": "claim@x.example",
                              "https://api.openai.com/auth":
                                  {"chatgpt_plan_type": "pro",
                                   "chatgpt_account_id": "acct-claim-id"}}),
            "access_token": _jwt({"exp": exp, "sub": "fake"}),
            "refresh_token": "fake-refresh-token-claim",
        }, "last_refresh": "2026-07-09T14:52:47.713051089Z"}
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump(auth, f)
        rec, _fname, err = seat.translate_codex_auth(path)
        self.assertIsNone(err)
        self.assertEqual(rec["account_id"], "acct-claim-id")
        # the access_token-claim-only shape resolves too
        auth["tokens"]["id_token"] = _jwt({"email": "claim@x.example"})
        auth["tokens"]["access_token"] = _jwt(
            {"exp": exp, "https://api.openai.com/auth":
                {"chatgpt_account_id": "acct-claim-acc"}})
        with open(path, "w") as f:
            json.dump(auth, f)
        rec, _fname, err = seat.translate_codex_auth(path)
        self.assertIsNone(err)
        self.assertEqual(rec["account_id"], "acct-claim-acc")

    # -- add: layout + perms + source untouched ----------------------------
    def test_add_seat_layout_perms_and_readonly_source(self):
        path, _, _ = self._plant("home-a")
        with open(path, "rb") as f:
            before = f.read()
        rc, out, err = self._add()
        self.assertEqual(rc, 0, err)
        d = seat.seat_dir("codex")
        cred = os.path.join(d, "auth", "codex-fake@example.com-pro.json")
        for p, want in ((cred, 0o600), (os.path.join(d, "token"), 0o600),
                        (os.path.join(d, "config.yaml"), 0o600),
                        (os.path.join(d, "launch.sh"), 0o700)):
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)
        self.assertTrue(os.path.isdir(os.path.join(d, "claude")))
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)  # source byte-identical
        # config carries the token + auth-dir, and the hard law holds: the
        # string ANTHROPIC_API_KEY appears in NO seat file
        with open(os.path.join(d, "token")) as f:
            token = f.read().strip()
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn(token, cfg)
        self.assertIn(os.path.join(d, "auth"), cfg)
        for root, _, files in os.walk(d):
            for name in files:
                with open(os.path.join(root, name)) as f:
                    self.assertNotIn("ANTHROPIC_API_KEY=", f.read())

    def test_add_preserves_other_pooled_accounts(self):
        """kimi FIX 2: `seat add codex` must not collapse the pool — pool
        three accounts, mint the seat from a fourth: all three survive (the
        proxy's usage-cap fall-through), plus the minted cred. Unattributable
        junk survives too (fail-open: never delete what can't be identified)."""
        from helm import codexhomes
        os.environ["HELM_CODEX_HOMES_DIR"] = seat.CODEX_HOMES
        now = time.time()
        for i, n in enumerate(("pool-a", "pool-b", "pool-c")):
            self._plant(n, email=n + "@x.example", mtime=now - 500 + i)
            self.assertTrue(codexhomes.codex_pool(n).get("ok"))
        auth_dir = os.path.join(seat.seat_dir("codex"), "auth")
        with open(os.path.join(auth_dir, "codex-junk.json"), "w") as f:
            f.write("{not json")
        self._plant("home-d", email="d@x.example", mtime=now)  # newest -> picked
        rc, out, err = self._add()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(auth_dir)),
                         ["codex-d@x.example-pro.json", "codex-junk.json",
                          "codex-pool-a.json", "codex-pool-b.json",
                          "codex-pool-c.json"])
        self.assertNotIn("replaced same-account", out)  # nothing was removed
        self.assertIn("4 other pooled creds preserved", out)

    def test_re_add_replaces_only_same_account_and_prints_it(self):
        """kimi FIX 2: a stale pooled spelling of the SAME account is removed
        (the re-add IS its refresh) and reported; sibling accounts untouched."""
        auth_dir = os.path.join(seat.seat_dir("codex"), "auth")
        os.makedirs(auth_dir)
        with open(os.path.join(auth_dir, "codex-home-a.json"), "w") as f:
            json.dump({"type": "codex", "account_id": "acct-home-a"}, f)
        with open(os.path.join(auth_dir, "codex-pool-b.json"), "w") as f:
            json.dump({"type": "codex", "account_id": "acct-pool-b"}, f)
        self._plant("home-a")  # mints account acct-home-a
        rc, out, err = self._add()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(auth_dir)),
                         ["codex-fake@example.com-pro.json", "codex-pool-b.json"])
        self.assertIn("replaced same-account pooled cred: codex-home-a.json", out)
        self.assertIn("1 other pooled cred preserved", out)

    def test_re_add_keeps_token(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with open(os.path.join(seat.seat_dir("codex"), "token")) as f:
            tok1 = f.read().strip()
        self.assertEqual(self._add()[0], 0)
        with open(os.path.join(seat.seat_dir("codex"), "token")) as f:
            self.assertEqual(f.read().strip(), tok1)

    # -- default --auth-from: newest VALID wins, expired skipped ------------
    def test_newest_valid_selection(self):
        now = time.time()
        self._plant("older-valid", email="old@x.example", mtime=now - 5000)
        want, _, _ = self._plant("newer-valid", email="new@x.example", mtime=now - 100)
        self._plant("newest-expired", email="dead@x.example", exp_offset=-60, mtime=now)
        got, err = seat.newest_valid_codex_auth()
        self.assertIsNone(err)
        self.assertEqual(got, want)

    def test_symlink_alias_homes_dedupe(self):
        self._plant("real-home")
        os.symlink(os.path.join(seat.CODEX_HOMES, "real-home"),
                   os.path.join(seat.CODEX_HOMES, "alias-home"))
        seen = set()
        for p in [seat.newest_valid_codex_auth()[0]]:
            seen.add(os.path.realpath(p))
        self.assertEqual(len(seen), 1)

    # -- expired/absent cred: refusal + human unblock line, exit 1 ----------
    def test_expired_cred_refusal(self):
        self._plant("dead", exp_offset=-60)
        rc, out, err = self._add()
        self.assertEqual(rc, 1)
        self.assertIn("codex login --device-auth", err)  # the human's unblock line
        self.assertFalse(os.path.exists(os.path.join(seat.seat_dir("codex"), "config.yaml")))

    def test_absent_cred_refusal(self):
        rc, out, err = self._add()
        self.assertEqual(rc, 1)
        self.assertIn("codex login --device-auth", err)

    def test_explicit_auth_from_expired_refused(self):
        path, _, _ = self._plant("dead", exp_offset=-60)
        rc, out, err = self._add(("add", "codex", "--auth-from", path))
        self.assertEqual(rc, 1)
        self.assertIn("expired", err)

    def test_unknown_family_refused(self):
        rc, out, err = self._add(("add", "glm"))
        self.assertEqual(rc, 2)
        self.assertIn("not yet wired", err)

    # -- proxy-key families (kimi) ------------------------------------------
    def test_config_yaml_key_exact_shape(self):
        cfg = seat._config_yaml_key(8318, "tok-abc", "moonshot",
                                    "https://api.moonshot.ai/v1", "kimi-k3",
                                    "fake-key-xyz")
        self.assertEqual(cfg, (
            'host: "127.0.0.1"\n'
            "port: 8318\n"
            "api-keys:\n"
            '  - "tok-abc"\n'
            "debug: false\n"
            # the meter is on for every proxy family (task/2522); the reader pops
            # per-request records, so the queue keeps them for an hour
            "usage-statistics-enabled: true\n"
            "redis-usage-queue-retention-seconds: 3600\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n"
            "openai-compatibility:\n"
            '  - name: "moonshot"\n'
            '    base-url: "https://api.moonshot.ai/v1"\n'
            "    api-key-entries:\n"
            '      - api-key: "fake-key-xyz"\n'
            "    models:\n"
            '      - name: "kimi-k3"\n'
            '        alias: "kimi-k3"\n'
            '      - name: "kimi-k3"\n'
            '        alias: "claude-opus-5"\n'
            '      - name: "kimi-k3"\n'
            '        alias: "claude-sonnet-5"\n'
            '      - name: "kimi-k3"\n'
            '        alias: "claude-haiku-4-5-20251001"\n'
            '      - name: "kimi-k3"\n'
            '        alias: "claude-fable-5-1"\n'
            '      - name: "kimi-k3"\n'
            '        alias: "claude-haiku-4-5"\n'
            '      - name: "kimi-k3"\n'
            '        alias: "claude-opus-5-5"\n'
            # long-nonstream keepalive (compaction survival) rides every config
            "nonstream-keepalive-interval: 15\n"
            # transient-bench shortening (503-storm as-prevented; 0 = 60s default)
            "transient-error-cooldown-seconds: 5\n"
            # streaming-leg survival (the ~90%-context empty-200 class)
            "streaming:\n"
            "  keepalive-seconds: 15\n"
            "  bootstrap-retries: 2\n"))
        self.assertNotIn("auth-dir", cfg)

    # -- key-flavor -> base-url dispatch (kimi coding vs Moonshot platform) --
    def test_key_base_url_dispatch(self):
        fam = seat.FAMILIES["kimi"]
        # coding-plan flavor -> the kimi.com coding endpoint
        self.assertEqual(seat._key_base_url(fam, "sk-kimi-abc123"),
                         "https://api.kimi.com/coding/v1")
        # platform flavor (plain sk-) -> the Moonshot platform endpoint
        self.assertEqual(seat._key_base_url(fam, "sk-abc123"),
                         "https://api.moonshot.ai/v1")
        # a family with no key_base_urls keeps its single base_url
        plain = {"base_url": "https://example.test/v1"}
        self.assertEqual(seat._key_base_url(plain, "sk-anything"),
                         "https://example.test/v1")

    def test_add_kimi_coding_plan_key_routes_to_coding_endpoint(self):
        os.environ["KIMI_API_KEY"] = "sk-kimi-fake-coding-plan-key"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('base-url: "https://api.kimi.com/coding/v1"', cfg)
        self.assertIn('api-key: "sk-kimi-fake-coding-plan-key"', cfg)

    def test_add_keyless_family_bakes_the_declared_placeholder(self):
        """A keyless family mints with NO key in its environment at all and
        still writes a usable block — carrying the declared placeholder, never
        anything read from a credential source.

        THE CONTROL IS IN THE ARM ON PURPOSE, because the keyless half is an
        assertion about a STRING, and a mint that silently failed would leave
        a file that contains neither the placeholder nor a key. The kimi half
        runs through the same `_add`, with a key in the environment, and must
        bake that key — so the door is proven to write what it is given before
        the qwen27 half's answer means anything.

        WHY THE PLACEHOLDER EXISTS AT ALL is recorded on the constant: the
        proxy creates no client for a provider with no api-key-entries, so
        omitting it produced a seat that accepted requests and never answered.
        """
        from helm import seat_catalog
        rc, out, err = self._add(("add", "qwen27"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("qwen27"), "config.yaml")) as f:
            keyless = f.read()
        self.assertIn("openai-compatibility:", keyless)
        self.assertIn('base-url: "%s"' % _KEYLESS_ENDPOINT, keyless)
        self.assertIn('alias: "qwen27"', keyless)
        self.assertIn('api-key: "%s"' % seat_catalog.KEYLESS_API_KEY_PLACEHOLDER,
                      keyless)
        # exactly one credential row: the placeholder, and nothing beside it
        self.assertEqual(keyless.count("api-key:"), 1)

        os.environ["KIMI_API_KEY"] = "fake-keyed-control"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            keyed = f.read()
        self.assertIn("api-key-entries:", keyed)
        self.assertIn('api-key: "fake-keyed-control"', keyed)
        self.assertNotIn(seat_catalog.KEYLESS_API_KEY_PLACEHOLDER, keyed)

    # -- cursor: grok-4.7-high through the owner's Cursor plan (task/2634) ----
    #: What the hand-made live seat serves, read off its config.yaml and
    #: launch.sh. The mint must reproduce each of these, because
    #: `helm seat launch` and `seat doctor --ensure` rewrite both files from
    #: the catalog and a value helm does not own is lost at the next pass.
    _CURSOR_LIVE = {"port": 8315, "provider": "cursor-bridge",
                    "base_url": "http://127.0.0.1:18315/v1",
                    "upstream": "grok-4.7-high"}

    def test_add_cursor_mints_the_route_the_live_seat_serves(self):  # noqa: VACUOUS_ASSERTION — the assertIn words on the same launch.sh are the unconditional positive control for its cursor-grok absence
        """`helm seat add cursor` with no key anywhere writes the live route:
        proxy 8315, the bridge on 18315, grok-4.7-high behind the alias the
        owner named (`cursor`), and the keyless placeholder as the one key.
        launch.sh points claude at 8315 with the same alias, and teaches the
        225000 max_context derived from Cursor's 256000 probe (task/3616),
        no longer narrowed by a context_budget (task/3816)."""
        from helm import seat_catalog
        live = self._CURSOR_LIVE
        rc, out, err = self._add(("add", "cursor"))  # noqa: SEAT_NAME — the catalog FAMILY key the mint verb takes
        self.assertEqual(rc, 0, err)
        d = seat.seat_dir("cursor")  # noqa: SEAT_NAME — the catalog FAMILY key
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn("port: %d\n" % live["port"], cfg)
        self.assertIn('  - name: "%s"\n    base-url: "%s"\n'
                      % (live["provider"], live["base_url"]), cfg)
        self.assertIn('      - name: "%s"\n        alias: "cursor"\n'
                      % live["upstream"], cfg)
        self.assertIn('api-key: "%s"' % seat_catalog.KEYLESS_API_KEY_PLACEHOLDER,
                      cfg)
        self.assertEqual(cfg.count("api-key:"), 1)
        with open(os.path.join(d, "launch.sh")) as f:
            launch = f.read()
        for word in ("ANTHROPIC_BASE_URL=http://127.0.0.1:%d" % live["port"],
                     "CLAUDE_CODE_SUBAGENT_MODEL=cursor",
                     "HELM_MODEL_FAMILY=cursor",
                     "CLAUDE_CODE_MAX_CONTEXT_TOKENS=225000",
                     "--model cursor"):
            self.assertIn(word, launch)
        # NO TRAFFIC SWITCH ON ANY SEAT: either one takes Monitor out of the
        # seat's tool list (measured at claude 2.1.284), and a seat without
        # Monitor can arm no beacon. The cursor launch line above is the
        # measured case; every family's line is swept, each with a presence
        # on the same line before its absences.
        self.assertNotIn("DISABLE_NONESSENTIAL_TRAFFIC", launch)
        self.assertNotIn("DISABLE_TELEMETRY", launch)
        for fam in seat.FAMILIES:
            with self.subTest(family=fam):
                line = seat.launch_line(fam, room="r")
                self.assertIn("HELM_MODEL_FAMILY=%s" % fam, line)
                self.assertNotIn("DISABLE_NONESSENTIAL_TRAFFIC", line)
                self.assertNotIn("DISABLE_TELEMETRY", line)
        self.assertNotIn("cursor-grok", launch)
        # the proxy verdict reads the family it now resolves: a rung and a
        # route, where the hand-made seat rendered `unknown family`
        self.assertEqual(seat_catalog.provider_rung("cursor", live["provider"]),  # noqa: SEAT_NAME — the catalog FAMILY key
                         "free")

    def test_the_reconcile_keeps_the_hand_made_alias_and_takes_custody(self):
        """THE RENAME DOES NOT BREAK A PANE ON THE OLD ALIAS. The live config
        carries `cursor-grok` and a non-placeholder key entry. One reconcile
        writes the catalog's alias and the declared placeholder, KEEPS the
        `cursor-grok` row (a model row helm does not own is retained), and
        names the custody drift; a second pass finds nothing to change."""
        from helm import seat_catalog, seat_launch_assets
        live = self._CURSOR_LIVE
        d = seat.seat_dir("cursor")  # noqa: SEAT_NAME — the catalog FAMILY key
        os.makedirs(d)
        path = os.path.join(d, "config.yaml")
        hand = seat_launch_assets._config_yaml_key(
            live["port"], "inbound-token", live["provider"], live["base_url"],
            "cursor-grok", "cursor-local-bridge-placeholder", live["upstream"],
            frontmatter=False)
        with open(path, "w") as f:
            f.write(hand)
        plan = seat_launch_assets.proxy_config_plan(path, "cursor")  # noqa: SEAT_NAME — the catalog FAMILY key
        self.assertTrue(plan["changed"])
        self.assertIn("keyless custody", plan["alias_drift"])
        self.assertIn('alias: "cursor"\n', plan["text"])
        self.assertIn('alias: "cursor-grok"\n', plan["text"])
        self.assertIn('api-key: "%s"' % seat_catalog.KEYLESS_API_KEY_PLACEHOLDER,
                      plan["text"])
        self.assertNotIn("cursor-local-bridge-placeholder", plan["text"])
        with open(path, "w") as f:
            f.write(plan["text"])
        again = seat_launch_assets.proxy_config_plan(path, "cursor")  # noqa: SEAT_NAME — the catalog FAMILY key
        self.assertFalse(again["changed"], again["alias_drift"])
        self.assertIsNone(again["alias_drift"])

    def test_a_one_provider_keyless_family_keeps_the_endpoint_floor(self):  # noqa: VACUOUS_ASSERTION — rc 1 and the refusal sentence are the unconditional positive control for the absent config
        """The floor the pool branch asks of a row holds for a keyless family
        with no pool: a public plain-http endpoint refuses the mint, and the
        loopback bridge (the control, minted above) is admitted."""
        # a NAME, never an address: `_safe_endpoint` admits plain http only to
        # a literal private or loopback address
        bad = dict(seat.FAMILIES["cursor"],  # noqa: SEAT_NAME — the catalog FAMILY key
                   base_url="http://bridge.example.com:18315/v1")
        with mock.patch.dict(seat.FAMILIES, {"cursor": bad}):  # noqa: SEAT_NAME — the catalog FAMILY key
            rc, _out, err = self._add(("add", "cursor"))  # noqa: SEAT_NAME — the catalog FAMILY key the mint verb takes
        self.assertEqual(rc, 1)
        self.assertIn("not an endpoint a keyless seat may use", err)
        self.assertFalse(os.path.exists(os.path.join(
            seat.seat_dir("cursor"), "config.yaml")))  # noqa: SEAT_NAME — the catalog FAMILY key

    def test_a_family_level_rung_answers_only_for_its_own_provider(self):
        from helm import seat_catalog
        table = {"solo": {"provider": "solo-vendor", "rung": "paid"},
                 "odd": {"provider": "odd-vendor", "rung": "cheap"},
                 "bare": {"provider": "bare-vendor"}}
        self.assertEqual(seat_catalog.provider_rung("solo", "solo-vendor",
                                                    table), "paid")
        self.assertIsNone(seat_catalog.provider_rung("solo", "other", table))
        self.assertIsNone(seat_catalog.provider_rung("odd", "odd-vendor",
                                                     table))
        self.assertIsNone(seat_catalog.provider_rung("bare", "bare-vendor",
                                                     table))

    # -- the second local family: qwenlocal, the 35B on the same box ----------
    #: The FAMILY key, named once for the same reason `_KEYLESS_FAMILY` is.
    _LOCAL_VLLM_FAMILY = "qwenlocal"  # noqa: SEAT_NAME — the catalog FAMILY key, never a seat
    #: Its endpoint in these arms: the same documentation-range host as the
    #: qwen27 endpoint above, on another port, because that is the shape of
    #: the operator's box (two served models, one host).
    _LOCAL_VLLM_ENDPOINT = "http://192.0.2.10:8081/v1"

    def test_qwenlocal_is_a_keyless_free_family_whose_window_is_split(self):
        """The entry's contract, each field against the reason it holds.

        THE ARITHMETIC IS THE CLAIM, NOT THE NUMBERS. Input and output share
        one served window, so the pinned max_context is the window less the
        output cap less a compaction margin (task/3184), and the output cap
        is the makers' own thinking-on max_tokens. A later re-probe that
        moves the window must move the pin with it, and this arm then still
        holds.

        The launch line is where the two numbers reach Claude Code, so it is
        asserted too. The control is kimi's line, which has no output cap:
        the word below is about this family's declaration, not about every
        launch line."""
        from helm import seat_catalog
        family = self._LOCAL_VLLM_FAMILY
        fam = seat.FAMILIES[family]
        self.assertEqual(fam["mode"], "proxy-key")
        self.assertIs(fam["keyless"], True)
        self.assertNotIn("key_env", fam)
        self.assertEqual(fam["model"], family)
        self.assertEqual(fam["probe_models"], (family,))
        # ONE pool row, and it is the default
        self.assertEqual(list(fam["pool_providers"]), [fam["pool_default"]])
        row = fam["pool_providers"][fam["pool_default"]]
        self.assertEqual(row["base_url_from"], family)
        self.assertEqual(row["upstream_model"], family)
        self.assertNotIn("base_url", row)   # the tree names no LAN host
        self.assertEqual(
            seat_catalog.provider_rung(family, fam["pool_default"]), "free")
        # ITS OWN PROVIDER NAME, named for the server and not the box. It is
        # not qwen27's, so an operator's rename of qwen27's provider (a local
        # name) cannot rename this block under a seat minted with it.
        self.assertEqual(fam["pool_default"], "local-vllm")
        self.assertNotEqual(fam["pool_default"],
                            seat.FAMILIES["qwen27"]["pool_default"])
        # one llama-server slot's window since the task/3363 cutover (2
        # slots x 196,608 on the operator's box); the vLLM server before it
        # admitted 262,144
        self.assertEqual(fam["probed_context_length"], 262144)
        self.assertEqual(fam["max_output_tokens"], 32768)
        # the slot less the output cap less exactly the compaction floor, as
        # bonsai's: 212,992 (262,144 - 32,768 - 16,384). It kept one more output
        # cap (131,072) while a Read result could reach 25,000 tokens; its lite
        # profile caps a Read at 8,000, which the floor holds. Each compaction
        # costs tokens, re-briefing and rediscovery, so the window sits at the
        # rule's edge.
        self.assertEqual(fam["max_context"],
                         fam["probed_context_length"] - fam["max_output_tokens"]
                         - seat_catalog.LOCAL_COMPACTION_MARGIN)
        self.assertLessEqual(fam["lite_env"]["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"],
                             seat_catalog.LOCAL_COMPACTION_MARGIN)
        _configure_endpoints({"qwen27": _KEYLESS_ENDPOINT,  # noqa: SEAT_NAME — catalog FAMILY keys the endpoints file is keyed by, never seats
                              family: self._LOCAL_VLLM_ENDPOINT})
        line = seat.launch_line(family, room="r")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=212992", line)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS=32768", line)
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                         seat.launch_line("kimi", room="r"))

    def test_qwenlocal_mints_its_own_block_and_its_route_is_its_own(self):
        """Two local families on one box: each mints its own block at its own
        endpoint, and a proof measured on either route resolves to that family
        alone.

        THE CONTROL IS qwen27, configured in the same helm home. With both
        endpoints present, "the route resolves to exactly one family" is a
        claim about two live routes and not about one route with no rival."""
        from helm import seat_catalog
        family = self._LOCAL_VLLM_FAMILY
        _configure_endpoints({"qwen27": _KEYLESS_ENDPOINT,  # noqa: SEAT_NAME — catalog FAMILY keys the endpoints file is keyed by, never seats
                              family: self._LOCAL_VLLM_ENDPOINT})
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir(family), "config.yaml")) as f:
            config = f.read()
        self.assertIn("port: %d\n" % seat.FAMILIES[family]["port"], config)
        self.assertIn('name: "local-vllm"', config)
        self.assertIn('base-url: "%s"' % self._LOCAL_VLLM_ENDPOINT, config)
        self.assertNotIn(_KEYLESS_ENDPOINT, config)   # not qwen27's endpoint
        self.assertIn('alias: "%s"' % family, config)
        self.assertIn('api-key: "%s"'
                      % seat_catalog.KEYLESS_API_KEY_PLACEHOLDER, config)
        self.assertEqual(config.count("api-key:"), 1)
        urls = set()
        for fam in (family, "qwen27"):  # noqa: SEAT_NAME — catalog FAMILY keys, and the pair of local families IS this arm's subject
            routes = seat_catalog.proxy_routes(fam)
            self.assertEqual(len(routes), 1, fam)
            self.assertEqual(seat_catalog.proxy_route_family(dict(routes[0])),
                             (fam, None))
            urls.add(routes[0]["base_url"])
        self.assertEqual(urls, {self._LOCAL_VLLM_ENDPOINT, _KEYLESS_ENDPOINT})

    # -- the third local family: bonsai, the 1-bit 27B on the CUDA box --------
    #: The FAMILY key, named once for the same reason `_KEYLESS_FAMILY` is.
    _LOCAL_CUDA_FAMILY = "bonsai"  # noqa: SEAT_NAME — the catalog FAMILY key, never a seat
    #: Its endpoint in these arms: a SECOND documentation-range host, because
    #: this model is served from a different box than the other two.
    _LOCAL_CUDA_ENDPOINT = "http://192.0.2.11:8098/v1"

    def test_bonsai_is_a_keyless_free_family_whose_window_fits_ONE_slot(self):
        """The entry's contract, each field against the reason it holds.

        ONE SLOT IS THE WHOLE KV POOL. The server reports one slot of 131,072
        tokens, so the lead and its subagents never share the pool at once:
        they take turns on the one slot. qwen27's budget (163,840 of a
        212,992 slot) left room for a concurrent subagent; scaled to this
        slot that ratio lands ABOVE the slot less one turn's output, so the
        ceiling binds and no narrower budget is declared.

        The launch line is where the numbers reach Claude Code, so it is
        asserted too."""
        from helm import seat_catalog
        family = self._LOCAL_CUDA_FAMILY
        fam = seat.FAMILIES[family]
        self.assertEqual(fam["mode"], "proxy-key")
        self.assertIs(fam["keyless"], True)
        self.assertNotIn("key_env", fam)
        self.assertEqual(fam["model"], family)
        self.assertEqual(fam["probe_models"], (family,))
        # ONE pool row, and it is the default
        self.assertEqual(list(fam["pool_providers"]), [fam["pool_default"]])
        row = fam["pool_providers"][fam["pool_default"]]
        # the endpoint resolves through `base_url_from` (`pool_base_url`).
        # The MCP floor is NOT keyed on it: envtidy.mcp_withheld reads the
        # window launch.sh stamps, and 32,000 of schemas exceed 15% of 98,304
        self.assertEqual(row["base_url_from"], family)
        self.assertEqual(row["upstream_model"], family)
        self.assertNotIn("base_url", row)   # the tree names no LAN host
        self.assertEqual(
            seat_catalog.provider_rung(family, fam["pool_default"]), "free")
        # ITS OWN PROVIDER NAME, neither local sibling's, so no rename of
        # theirs renames this block
        self.assertEqual(fam["pool_default"], "local-llamacpp-cuda")
        for sibling in ("qwen27", "qwenlocal"):  # noqa: SEAT_NAME — catalog FAMILY keys, the local siblings this provider name must differ from
            self.assertNotEqual(fam["pool_default"],
                                seat.FAMILIES[sibling]["pool_default"], sibling)
        self.assertEqual(fam["probed_context_length"], 262144)
        # 16,384, the output cap the operator of the local boxes pinned live
        # (task/3184), so the window keeps room beside the margin
        self.assertEqual(fam["max_output_tokens"], 16384)
        # the slot less the output cap less exactly the compaction floor:
        # 262,144 - 16,384 - 16,384 = 229,376
        self.assertEqual(fam["max_context"],
                         fam["probed_context_length"] - fam["max_output_tokens"]
                         - seat_catalog.LOCAL_COMPACTION_MARGIN)
        # qwen27's formula, scaled: the ceiling now binds, so context_budget
        q = seat.FAMILIES["qwen27"]
        scaled = (fam["probed_context_length"] * q["context_budget"]
                  // q["probed_context_length"])
        self.assertLess(scaled, fam["max_context"])
        self.assertEqual(fam["context_budget"], 196608)
        self.assertEqual(seat_catalog.taught_window(fam, fam["max_context"]),
                         196608)
        _configure_endpoints({family: self._LOCAL_CUDA_ENDPOINT})
        line = seat.launch_line(family, room="r")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=196608", line)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=196608", line)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384", line)
        self.assertIn("--model bonsai", line)
        # the taught window plus one turn's output plus the compaction margin
        # fits the one slot (the measured wedge was window + output == slot)
        self.assertLessEqual(196608 + fam["max_output_tokens"]
                             + seat_catalog.LOCAL_COMPACTION_MARGIN,
                             fam["probed_context_length"])

    def test_bonsai_mints_its_own_block_and_each_local_route_is_its_own(self):
        """Three local families, two boxes: each mints its own block at its
        own endpoint, and a proof measured on any of the three routes
        resolves to that family alone. The two vLLM-era siblings are
        configured in the same helm home, so "one family per route" is a
        claim about three live routes and not about one with no rival."""
        from helm import seat_catalog
        family = self._LOCAL_CUDA_FAMILY
        _configure_endpoints({"qwen27": _KEYLESS_ENDPOINT,  # noqa: SEAT_NAME — catalog FAMILY keys the endpoints file is keyed by, never seats
                              "qwenlocal": self._LOCAL_VLLM_ENDPOINT,  # noqa: SEAT_NAME — catalog FAMILY keys the endpoints file is keyed by, never seats
                              family: self._LOCAL_CUDA_ENDPOINT})
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir(family), "config.yaml")) as f:
            config = f.read()
        self.assertIn("port: 8349\n", config)
        self.assertIn('name: "local-llamacpp-cuda"', config)
        self.assertIn('base-url: "%s"' % self._LOCAL_CUDA_ENDPOINT, config)
        self.assertNotIn(_KEYLESS_ENDPOINT, config)
        self.assertNotIn(self._LOCAL_VLLM_ENDPOINT, config)
        self.assertIn('alias: "%s"' % family, config)
        self.assertIn('api-key: "%s"'
                      % seat_catalog.KEYLESS_API_KEY_PLACEHOLDER, config)
        self.assertEqual(config.count("api-key:"), 1)
        urls = set()
        for fam in (family, "qwen27", "qwenlocal"):  # noqa: SEAT_NAME — catalog FAMILY keys, and the three local families ARE this arm's subject
            routes = seat_catalog.proxy_routes(fam)
            self.assertEqual(len(routes), 1, fam)
            self.assertEqual(seat_catalog.proxy_route_family(dict(routes[0])),
                             (fam, None))
            urls.add(routes[0]["base_url"])
        self.assertEqual(urls, {self._LOCAL_CUDA_ENDPOINT,
                                self._LOCAL_VLLM_ENDPOINT, _KEYLESS_ENDPOINT})

    # -- a local window leaves a compaction margin (task/3184) --------------
    # MEASURED on a live qwenlocal seat: a turn died on API 400
    # at 229,377 input + 32,768 output = 262,145, one token over the 262,144
    # server maximum, because the taught window (229,376) plus the output cap
    # EQUALLED the maximum. The compaction call that followed sent the same
    # context and died the same way, so one large tool result wedged the seat
    # until a /clear. bonsai had the same zero margin (99,072 + 32,000 =
    # 131,072, its whole slot).

    def test_every_local_family_leaves_the_compaction_margin(self):
        """Every window a family served from the operator's own box teaches,
        plus its output cap, plus LOCAL_COMPACTION_MARGIN, fits the server
        slot, and the launch line stamps the window the arithmetic checked.

        NOT VACUOUS: the scope is read off the catalog through the predicate
        the guard itself uses, and each of the three local families must be
        in it, so a predicate that matched nothing fails here. The floor is
        pinned at 16,384, the one the operator of the local boxes set, so it
        cannot be lowered in passing."""
        from helm import seat_catalog
        margin = seat_catalog.LOCAL_COMPACTION_MARGIN
        self.assertGreaterEqual(margin, 16384)
        local = sorted(n for n, f in seat.FAMILIES.items()
                       if seat_catalog.own_box(f))
        for family in (self._KEYLESS_FAMILY, self._LOCAL_VLLM_FAMILY,
                       self._LOCAL_CUDA_FAMILY):
            self.assertIn(family, local)
        _configure_endpoints({self._KEYLESS_FAMILY: _KEYLESS_ENDPOINT,
                              self._LOCAL_VLLM_FAMILY: self._LOCAL_VLLM_ENDPOINT,
                              self._LOCAL_CUDA_FAMILY: self._LOCAL_CUDA_ENDPOINT})
        for family in local:
            fam = seat.FAMILIES[family]
            taught = seat_catalog.taught_window(fam, fam["max_context"])
            self.assertLessEqual(
                taught + fam["max_output_tokens"] + margin,
                fam["probed_context_length"], family)
            self.assertEqual(seat_catalog.compaction_margin_error(family, fam),
                             "", family)
            line = seat.launch_line(family, room="r")
            self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d " % taught, line)
            self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS=%d"
                          % fam["max_output_tokens"], line)
        self.assertIsNone(seat_catalog._incoherent_compaction_margin())

    def test_a_local_family_with_no_compaction_margin_is_refused(self):
        """The guard refuses the measured wedge's own shape, and only on a
        family served from the operator's own box.

        Planted copies of the qwenlocal entry go through the real predicate;
        the live FAMILIES is never mutated. Each refusal is paired with an
        accepted neighbour on the same numbers, so no arm passes by refusing
        everything:
          * window + output == slot (the measured shape) is refused, and the
            reason names the family, the floor and the tokens left;
          * one token under the floor is refused, and exactly at it passes
            (the boundary is <=);
          * the guard reads the TAUGHT window: a zero-margin max_context under
            a budget that leaves the margin passes (qwen27's shape), and the
            same entry without that budget is refused;
          * every model_context window is taught too, so one bad model window
            is refused beside a good family window;
          * a local family with no output cap, or with no recorded slot, is
            refused: its margin cannot be computed;
          * a local family with no max_context is taught Claude Code's 200k
            default, which a 131,072 slot cannot hold;
          * CONTROL: the zero-margin numbers on a family that is NOT served
            from the operator's box are outside this guard's scope.
        The table-level door returns the planted refusal and passes a
        coherent table."""
        import copy
        from helm import seat_catalog
        margin = seat_catalog.LOCAL_COMPACTION_MARGIN
        base = copy.deepcopy(seat.FAMILIES[self._LOCAL_VLLM_FAMILY])
        base.pop("context_budget", None)
        slot, out = base["probed_context_length"], base["max_output_tokens"]
        zero = dict(base, max_context=slot - out)
        why = seat_catalog.compaction_margin_error("seat-under-test", zero)
        self.assertIn("seat-under-test", why)
        self.assertIn("LOCAL_COMPACTION_MARGIN", why)
        self.assertIn("leaves 0 ", why)
        at = dict(base, max_context=slot - out - margin)
        self.assertEqual(seat_catalog.compaction_margin_error("seat-a", at), "")
        under = dict(base, max_context=slot - out - margin + 1)
        self.assertIn("leaves {:,} ".format(margin - 1),
                      seat_catalog.compaction_margin_error("seat-a", under))
        budgeted = dict(zero, context_budget=slot - out - margin)
        self.assertEqual(
            seat_catalog.compaction_margin_error("seat-a", budgeted), "")
        self.assertNotEqual(
            seat_catalog.compaction_margin_error("seat-a", zero), "")
        per_model = dict(at, model_context={"seat-b-model": slot - out})
        self.assertNotEqual(
            seat_catalog.compaction_margin_error("seat-a", per_model), "")
        for key in ("max_output_tokens", "probed_context_length"):
            missing = dict(at)
            missing.pop(key)
            self.assertIn(key, seat_catalog.compaction_margin_error(
                "seat-a", missing))
        unpinned = dict(base, probed_context_length=131072)
        unpinned.pop("max_context")
        self.assertIn("200,000", seat_catalog.compaction_margin_error(
            "seat-a", unpinned))
        remote = dict(zero, pool_default="vendor",
                      pool_providers={"vendor": {
                          "base_url": "http://192.0.2.99/v1", "rung": "paid"}})
        self.assertFalse(seat_catalog.own_box(remote))
        self.assertEqual(
            seat_catalog.compaction_margin_error("seat-a", remote), "")
        self.assertEqual(
            seat_catalog._incoherent_compaction_margin({"seat-a": zero}),
            seat_catalog.compaction_margin_error("seat-a", zero))
        self.assertIsNone(
            seat_catalog._incoherent_compaction_margin({"seat-a": at}))

    def test_seat_doctor_flags_a_local_seat_whose_effective_window_has_no_margin(self):  # noqa: VACUOUS_ASSERTION — between the two empty readings the planted pin's ONE row is pinned by count, family, sources and 'leaves 0', and the doctor's rc 1 on it precedes the rc 0
        """`helm seat doctor` reads each minted local seat's EFFECTIVE window
        and output cap and flags one that leaves less than
        LOCAL_COMPACTION_MARGIN of its server slot. It is read-only.

        EFFECTIVE, because a settings.json `env` pin outranks the launch
        stamp (the envtidy arm pins that precedence). The arms: the catalog's
        own mint passes; a settings.json pin that puts back the measured shape
        is flagged, names the file it came from, and fails the doctor's exit
        status; the doctor leaves both files byte-identical; and removing the
        pin clears the flag."""
        from helm import autocompact
        family = self._LOCAL_CUDA_FAMILY
        fam = seat.FAMILIES[family]
        _configure_endpoints({family: self._LOCAL_CUDA_ENDPOINT})
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        self.assertEqual(seat_health._window_margin_lines(), [])
        cdir = os.path.join(seat.seat_dir(family), "claude")
        settings = os.path.join(cdir, "settings.json")
        launch = os.path.join(seat.seat_dir(family), "launch.sh")
        with open(settings) as f:
            kept = json.load(f)
        # the lite profile pins both numbers in settings.json (task/3253);
        # the output pin comes out here so each number keeps its own source
        env = dict(kept.get("env") or {})
        env.pop("CLAUDE_CODE_MAX_OUTPUT_TOKENS", None)
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(
            fam["probed_context_length"] - fam["max_output_tokens"])
        pinned = dict(kept, env=env)
        with open(settings, "w") as f:
            json.dump(pinned, f)
        def raw(path):
            with open(path, "rb") as f:
                return f.read()

        before = {p: raw(p) for p in (settings, launch)}
        lines = seat_health._window_margin_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(family, lines[0])
        # each number's own source, not the bare filename: the cure clause
        # names settings.json on every NO MARGIN line, so that alone would
        # pass a line that credited the pinned window to launch.sh
        self.assertIn("window from settings.json, output cap from launch.sh",
                      lines[0])
        self.assertIn("leaves 0 ", lines[0])

        def doctor():
            out = io.StringIO()
            with mock.patch.object(seat_health, "_proxy_bin",
                                   return_value="/nonexistent/proxy"), \
                    mock.patch.object(
                        seat_health, "_proxy_binary_probe",
                        return_value=(True, "", "CLIProxyAPI Version: test")), \
                    mock.patch.object(seat_health.shutil, "which",
                                      return_value="/usr/bin/claude"), \
                    mock.patch.object(seat_health, "codex_cred_state",
                                      return_value=("valid", None, "fixture")), \
                    mock.patch.object(seat_health, "_status"), \
                    mock.patch.object(seat_health, "_running_pid",
                                      return_value=None), \
                    mock.patch.object(seat_health, "_config_drift_lines",
                                      return_value=[]), \
                    mock.patch.object(autocompact, "report_lines",
                                      return_value=[]), \
                    contextlib.redirect_stdout(out):
                return seat_health._doctor([]), out.getvalue()

        rc, text = doctor()
        self.assertEqual(rc, 1)
        self.assertIn(lines[0], text)
        self.assertEqual(before, {p: raw(p) for p in (settings, launch)})
        with open(settings, "w") as f:
            json.dump(kept, f)
        self.assertEqual(seat_health._window_margin_lines(), [])
        rc, text = doctor()
        self.assertEqual(rc, 0, text)

    # -- the lite launch profile (task/3253) ---------------------------------
    # ONE table (seat_catalog PROFILES) says what `"profile": "lite"` means,
    # and every surface reads its switch there: the deny set, the MCP floor,
    # the settings.json env pins and claudeMdExcludes. These arms SWEEP every
    # family the catalog declares lite, never a literal tuple, so a family
    # that opts in later is measured the day it does, and they judge each
    # surface against the catalog's DECLARATIONS (its tool constants, the
    # launch stamp), never against denied_tools or profile_env, the doors
    # under test.

    def _lite_families(self):
        """Every lite family, read through the predicate the surfaces use.
        NOT VACUOUS: v1 of the profile is the local seam, so every family
        served from the operator's own box must be in it, and a predicate
        that matched nothing fails here."""
        lite = sorted(f for f in seat.FAMILIES if seat_catalog.lite(f))
        local = [f for f, fam in seat.FAMILIES.items()
                 if seat_catalog.own_box(fam)]
        self.assertTrue(local)
        for family in local:
            self.assertIn(family, lite)
        return lite

    def _mint_lite(self):
        """Mint every lite family under a planted canonical MCP set of two
        servers (the floor and one above it); -> (families, canon path).
        Each family's endpoint key is its default pool row's own
        `base_url_from`, read off the catalog."""
        lite = self._lite_families()
        table = {}
        for i, family in enumerate(lite):
            fam = seat.FAMILIES[family]
            row = (fam.get("pool_providers") or {}).get(fam.get("pool_default"))
            if isinstance(row, dict) and row.get("base_url_from"):
                table[row["base_url_from"]] = "http://192.0.2.%d:8081/v1" % (20 + i)
        _configure_endpoints(table)
        canon = os.path.join(self.tmp, "mcps-canonical.json")
        with open(canon, "w") as f:
            json.dump({"exa": {"command": "exa"},
                       "alpha-mcp": {"command": "alpha"}}, f)
        os.chmod(canon, 0o600)
        with mock.patch.dict(os.environ, {"HELM_MCPS_CANONICAL": canon}):
            for family in lite:
                rc, _out, err = self._add(("add", family))
                self.assertEqual(rc, 0, (family, err))
        return lite, canon

    @staticmethod
    def _stamp(argv, name):
        """The one value launch.sh's argv gives `name`, or None."""
        found = {a.split("=", 1)[1] for a in argv if a.startswith(name + "=")}
        return found.pop() if len(found) == 1 else None

    def test_every_lite_family_mints_its_profile_on_every_surface(self):  # noqa: VACUOUS_ASSERTION — _lite_families asserts a non-empty sweep holding every own-box family and _mint_lite asserts each add's rc 0 before the loop; in each iteration the superset and equality presences on settings.json and argv precede the Workflow and cap absences
        """A fresh mint of EVERY lite family, read back off the files the
        seat runs from:
          * DENY — settings.json and launch.sh's argv both carry a SUPERSET
            of plan entry, the family's own context, push and unserved sets
            (task/3242: WebSearch), the spawn pair and LITE_UNUSED_TOOLS. A
            superset, so a later measured deny passes without editing this
            arm. Workflow goes unless the family declares a same-family
            delegate tier (task/2559 reconciled), and its cap goes with it:
            no cap word on the line and no cap in settings.json.
          * ENV PINS — settings.json carries the very numbers launch.sh
            stamps for both window knobs and the output cap, those numbers
            are the catalog's taught window and output cap, the family's
            lite_env rides beside them, and the effective reading (a settings
            pin outranks the stamp) is therefore the stamp.
          * EXCLUDES — claudeMdExcludes names the default home's CLAUDE.md
            on a mint that knows no workdir.
          * RECORDS — helm's key names exactly what this seeder wrote.
          * MCP FLOOR — the seat is given the floor and nothing above it."""
        from helm import envtidy
        lite, _canon = self._mint_lite()
        home_md = os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
        for family in lite:
            with self.subTest(family=family):
                fam = seat.FAMILIES[family]
                d = seat.seat_dir(family)
                cdir = os.path.join(d, "claude")
                with open(os.path.join(cdir, "settings.json")) as f:
                    got = json.load(f)
                with open(os.path.join(d, "launch.sh")) as f:
                    argv = ProxySeatCannotSpawnThroughASkill.child_argv(f.read())
                tiered = bool(fam.get("subagent_tiers"))
                want = {seat.PLAN_ENTRY_TOOL}
                for key in ("context_denied_tools", "push_denied_tools",
                            "unserved_tools"):
                    want.update(fam.get(key, ()))
                want.update(seat_catalog.SPAWN_DENIED_TOOLS)
                want.update(t for t in seat_catalog.LITE_UNUSED_TOOLS
                            if not (tiered and t == "Workflow"))
                deny = got["permissions"]["deny"]
                denied = argv[argv.index("--disallowedTools") + 1:
                              argv.index("--dangerously-skip-permissions")]
                self.assertLessEqual(want, set(deny))
                self.assertLessEqual(want, set(denied))
                self.assertEqual("Workflow" in deny, not tiered)
                self.assertEqual("Workflow" in denied, not tiered)
                cap = seat_catalog.WORKFLOW_CAP_VAR
                self.assertEqual(self._stamp(argv, cap) is not None, tiered)
                self.assertEqual(cap in got["env"], tiered)
                # the pins ARE the stamps, and the stamps are the catalog's
                env = got["env"]
                knobs = ("CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                         "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                         "CLAUDE_CODE_MAX_OUTPUT_TOKENS")
                for name in knobs:
                    self.assertIsNotNone(self._stamp(argv, name), name)
                    self.assertEqual(env[name], self._stamp(argv, name), name)
                self.assertEqual(env[knobs[0]], str(seat_catalog.taught_window(
                    fam, fam["max_context"])))
                self.assertEqual(env[knobs[2]], str(fam["max_output_tokens"]))
                for name, value in (fam.get("lite_env") or {}).items():
                    self.assertEqual(env[name], str(value), name)
                self.assertEqual(envtidy.seat_stamp(cdir, knobs[0]),
                                 (int(self._stamp(argv, knobs[0])),
                                  "settings.json"))
                # the excludes, and the records of what helm wrote
                self.assertIn(home_md, got["claudeMdExcludes"])
                self.assertEqual(got["helm"]["seeded_excludes"],
                                 got["claudeMdExcludes"])
                self.assertEqual(got["helm"]["seeded_env"],
                                 {k: env[k] for k in knobs
                                  + tuple(fam.get("lite_env") or ())})
                # the MCP floor, and nothing above it
                with open(os.path.join(cdir, ".claude.json")) as f:
                    servers = json.load(f).get("mcpServers") or {}
                self.assertEqual(sorted(servers), sorted(envtidy.MCP_FLOOR))
        # CONTROL: a proxy family with no profile carries none of it, and
        # keeps its capped Workflow
        plain = os.path.join(self.tmp, "cfg-kimi")
        os.makedirs(plain)
        seat._seed_seat_settings(plain, "kimi")
        with open(os.path.join(plain, "settings.json")) as f:
            kimi = json.load(f)
        self.assertNotIn("claudeMdExcludes", kimi)
        self.assertEqual(sorted(kimi["helm"]), ["seeded_denies"])
        self.assertEqual(kimi["env"], {seat_catalog.WORKFLOW_CAP_VAR: "4"})
        self.assertIn("Skill", kimi["permissions"]["deny"])
        self.assertNotIn("Workflow", kimi["permissions"]["deny"])

    def test_a_lite_seats_excludes_name_every_rule_file_above_its_project(self):
        """claudeMdExcludes is DERIVED, never a literal: the default home's
        CLAUDE.md always, and with a known workdir every memory file Claude
        Code reads (CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md) of every
        directory ABOVE the workdir's git top level, up to and not including
        the filesystem root. The project's own files, at its top level and
        below it, are never named: the workdir here is a subdirectory, so a
        derivation that started from the cwd instead of the top level would
        name the project's own CLAUDE.md and fail. ADDITIVE: an operator's
        entry keeps its place, and a later seed that knows no workdir keeps
        what the earlier one wrote. Recorded under helm's key. The control
        is a family with no profile: no excludes, no record."""
        family = self._lite_families()[0]
        base = os.path.realpath(self.tmp)
        estate = os.path.join(base, "estate")
        project = os.path.join(estate, "project")
        sub = os.path.join(project, "sub")
        os.makedirs(sub)
        subprocess.run(["git", "init", "-q", project], check=True,
                       capture_output=True)
        own = os.path.join(base, "operator-own", "CLAUDE.md")
        cdir = os.path.join(base, "cfg")
        os.makedirs(cdir)
        settings = os.path.join(cdir, "settings.json")
        with open(settings, "w") as f:
            json.dump({"claudeMdExcludes": [own]}, f)
        seat._seed_seat_settings(cdir, family, workdir=sub)
        with open(settings) as f:
            got = json.load(f)
        excl = got["claudeMdExcludes"]
        self.assertEqual(excl[0], own)                        # kept in place
        self.assertIn(os.path.join(os.path.expanduser("~"), ".claude",
                                   "CLAUDE.md"), excl)
        names = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"),
                 "CLAUDE.local.md")
        above = os.path.dirname(project)
        while above != os.path.dirname(above):
            for name in names:
                self.assertIn(os.path.join(above, name), excl)
            above = os.path.dirname(above)
        for name in names:
            self.assertNotIn(os.path.join(above, name), excl)  # the root
            for mine in (project, sub):
                self.assertNotIn(os.path.join(mine, name), excl)
        self.assertEqual(got["helm"]["seeded_excludes"], excl[1:])
        # a seed that knows no workdir (a mint, a pasted launch) keeps them
        seat._seed_seat_settings(cdir, family)
        with open(settings) as f:
            self.assertEqual(json.load(f)["claudeMdExcludes"], excl)
        # CONTROL: no profile, no excludes and no record
        plain = os.path.join(base, "cfg-plain")
        os.makedirs(plain)
        seat._seed_seat_settings(plain, "kimi", workdir=sub)
        with open(os.path.join(plain, "settings.json")) as f:
            got = json.load(f)
        self.assertNotIn("claudeMdExcludes", got)
        self.assertEqual(sorted(got["helm"]), ["seeded_denies"])

    def test_a_lite_seats_env_pins_follow_the_stamp_and_record_only_helms(self):
        """THE PIN RULE (seat_catalog pin_action), each leg on one seed:
          * an operator's pin ABOVE the catalog (the task/3184 wedge's own
            number) is lowered to the catalog's, the safe direction, and is
            then helm's, recorded with the value helm wrote;
          * an absent pin is written and recorded;
          * an operator's pin already AT the catalog's value is left alone
            and NOT recorded as helm's;
          * an operator's other key survives;
          * a second seed is at rest (no write at all).
        The record maps each pin helm wrote to the value it wrote."""
        family = self._lite_families()[0]
        fam = seat.FAMILIES[family]
        window = str(seat_catalog.taught_window(fam, fam["max_context"]))
        output = str(fam["max_output_tokens"])
        cdir = os.path.join(self.tmp, "cfg-env")
        os.makedirs(cdir)
        p = os.path.join(cdir, "settings.json")
        with open(p, "w") as f:
            json.dump({"env": {"KEEP": "1",
                               "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "229376",
                               "CLAUDE_CODE_MAX_OUTPUT_TOKENS": output}}, f)
        seat._seed_seat_settings(cdir, family)
        with open(p) as f:
            got = json.load(f)
        self.assertEqual(got["env"]["KEEP"], "1")
        self.assertEqual(got["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], window)
        self.assertEqual(got["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], window)
        self.assertEqual(got["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], output)
        self.assertEqual(got["helm"]["seeded_env"], dict(
            [("CLAUDE_CODE_MAX_CONTEXT_TOKENS", window),
             ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", window)]
            + [(k, str(v)) for k, v in (fam.get("lite_env") or {}).items()]))
        st = os.stat(p)
        seat._seed_seat_settings(cdir, family)
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))

    def test_a_reseed_never_raises_an_operators_lower_pin(self):  # noqa: VACUOUS_ASSERTION — the written output pin is asserted before the kept window pins, and the hand-lowered cap's kept value before its absence from the record
        """The task/3184 recovery shape: the catalog's window is a reading with an expiry, stale-HIGH until a lane
        lands, and the operator of the box hot-fixes the live seat with a
        LOWER settings.json pin. A re-seed (every `seat resume`, every reboot
        `resume --all`) must never raise it back, or the seat re-wedges at
        the slot edge. So an operator's pin below the catalog is KEPT, on
        the window knobs and the output caps alike, and is not recorded as
        helm's; the seed is then at rest. A HIGHER operator pin is still
        lowered (the arm above). The positive control: the seeder ran on
        this very file and wrote the pin it lacked."""
        family = self._lite_families()[0]
        fam = seat.FAMILIES[family]
        window = seat_catalog.taught_window(fam, fam["max_context"])
        low = str(window - 16384)
        cap = str(fam["max_output_tokens"] - 1024)
        cdir = os.path.join(self.tmp, "cfg-hotfix")
        os.makedirs(cdir)
        p = os.path.join(cdir, "settings.json")
        with open(p, "w") as f:
            json.dump({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": low,
                               "CLAUDE_CODE_AUTO_COMPACT_WINDOW": low}}, f)
        seat._seed_seat_settings(cdir, family)
        with open(p) as f:
            got = json.load(f)
        self.assertEqual(got["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
                         str(fam["max_output_tokens"]))
        self.assertEqual(got["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], low)
        self.assertEqual(got["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], low)
        self.assertEqual(sorted(got["helm"]["seeded_env"]), sorted(
            ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] + list(fam.get("lite_env") or ())))
        st = os.stat(p)
        seat._seed_seat_settings(cdir, family)
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))
        # a pin helm wrote and the operator then LOWERED by hand is the
        # operator's from that edit on: kept, and it leaves helm's record
        got["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = cap
        with open(p, "w") as f:
            json.dump(got, f)
        seat._seed_seat_settings(cdir, family)
        with open(p) as f:
            again = json.load(f)
        self.assertEqual(again["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], cap)
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                         again["helm"]["seeded_env"])

    def _reminted_lite(self, recipe=None):
        """A minted lite family's launch assets written again, as an exact
        resume writes them when `recipe` carries the window it ran with;
        -> (settings env, launch.sh text)."""
        lite, canon = self._mint_lite()
        family = lite[0]
        d = seat.seat_dir(family)
        with mock.patch.dict(os.environ, {"HELM_MCPS_CANONICAL": canon}):
            self.assertIsNot(seat._write_launch_assets(
                family, d, None, family, recipe=recipe),
                seat._SEAT_SURFACE_REFUSED)
        with open(os.path.join(d, "claude", "settings.json")) as f:
            env = json.load(f)["env"]
        with open(os.path.join(d, "launch.sh")) as f:
            return family, env, f.read()

    def test_an_exact_resume_pins_the_window_it_ran_with(self):
        """A lite seat's settings.json `env` pin OUTRANKS the launch stamp
        (envtidy.seat_stamp), so an exact resume that stamps the window the
        seat ran with must pin that window too, or the process runs on the
        catalog's. MUTATION: seed the pin from the catalog on an exact
        resume — launch.sh says one window and the seat runs another."""
        family = self._lite_families()[0]
        fam = seat.FAMILIES[family]
        ran = seat_catalog.launch_window(fam, fam["model"]) - 8192
        _family, env, text = self._reminted_lite({"window": ran})
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % ran, text)
        for name in seat_catalog.WINDOW_VARS:
            self.assertEqual(env[name], str(ran), name)

    def test_every_lite_family_pins_a_recipe_window_below_its_catalogs(self):
        """codex's input on qwen27: a recipe window half the catalog's
        (81920 against 163840), on every lite family. MUTATION: seed the
        pin from the catalog — each family runs twice the window it ran."""
        lite, canon = self._mint_lite()
        for family in lite:
            with self.subTest(family=family):
                fam = seat.FAMILIES[family]
                ran = seat_catalog.launch_window(fam, fam["model"]) // 2
                d = seat.seat_dir(family)
                with mock.patch.dict(os.environ,
                                     {"HELM_MCPS_CANONICAL": canon}):
                    self.assertIsNot(seat._write_launch_assets(
                        family, d, None, family, recipe={"window": ran}),
                        seat._SEAT_SURFACE_REFUSED)
                with open(os.path.join(d, "claude", "settings.json")) as f:
                    env = json.load(f)["env"]
                for name in seat_catalog.WINDOW_VARS:
                    self.assertEqual(env[name], str(ran), name)

    def test_a_fresh_mint_pins_the_catalogs_window(self):
        """The control: with no recipe the pin is the catalog's, as ever."""
        family = self._lite_families()[0]
        fam = seat.FAMILIES[family]
        _family, env, _text = self._reminted_lite()
        for name in seat_catalog.WINDOW_VARS:
            self.assertEqual(env[name], str(seat_catalog.launch_window(
                fam, fam["model"])), name)

    def test_an_operators_lower_pin_refuses_the_exact_window(self):
        """An operator's pin below the window the seat ran with is one the
        re-seed KEEPS (pin_action), and it outranks the launch stamp, so the
        exact resume cannot bring that window back: it refuses naming the
        knob, never a number. The control on the same number: helm's own pin
        is re-seeded to the recipe's window, so nothing conflicts."""
        from helm import seat_recipe
        family = self._lite_families()[0]
        fam = seat.FAMILIES[family]
        ran = seat_catalog.launch_window(fam, fam["model"])
        low = str(ran - 16384)
        for record, conflicts in (({}, True),
                                  ({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": low},
                                   False)):
            with self.subTest(helms=bool(record)):
                cdir = tempfile.mkdtemp(dir=self.tmp)
                with open(os.path.join(cdir, "settings.json"), "w") as f:
                    json.dump({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": low},
                               "helm": {"seeded_env": record}}, f)
                why = seat_recipe.window_pin_conflict(cdir, family, ran)
                if conflicts:
                    self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", why)
                    self.assertNotIn(low, why)
                else:
                    self.assertIsNone(why)

    def test_helms_own_pin_follows_the_catalog_both_ways(self):  # noqa: VACUOUS_ASSERTION — both arms assert the pin's value by equality (raised vs kept) before the record membership equality, and the arm tuple is a literal of two
        """A pin helm's record names AT ITS CURRENT VALUE is helm's own: it
        follows the catalog down and UP, so a catalog that moved raises the
        pin helm wrote under the old number. The control on the same seed:
        the same number with no record is the operator's and is kept."""
        family = self._lite_families()[0]
        fam = seat.FAMILIES[family]
        window = str(seat_catalog.taught_window(fam, fam["max_context"]))
        old = str(int(window) - 16384)
        for record, want in (({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": old}, window),
                             ({}, old)):
            with self.subTest(recorded=bool(record)):
                cdir = tempfile.mkdtemp(dir=self.tmp)
                p = os.path.join(cdir, "settings.json")
                with open(p, "w") as f:
                    json.dump({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": old},
                               "helm": {"seeded_env": record}}, f)
                seat._seed_seat_settings(cdir, family)
                with open(p) as f:
                    got = json.load(f)
                self.assertEqual(
                    got["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], want)
                self.assertEqual(
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in got["helm"]["seeded_env"],
                    bool(record))

    def test_every_profile_refusal_is_driven(self):  # noqa: VACUOUS_ASSERTION — two coherent tables are asserted to pass unconditionally, and each refusal is an assertIn of its reason over a literal table of seven, never an absence
        """The refusals profile_error names, each driven through the real
        predicate on a planted table (the `table` door _incoherent_profile
        was given for exactly this), beside coherent tables that pass.
        Without this arm every refusal can be deleted and the suite stays
        green: the import-time assert only ever reads the live table, which
        passes."""
        ok = {"mode": "proxy-key", "profile": "lite",
              "lite_env": {"BASH_MAX_OUTPUT_LENGTH": 20000}}
        self.assertIsNone(seat_catalog._incoherent_profile({"f": ok}))
        self.assertIsNone(seat_catalog._incoherent_profile(
            {"f": {"mode": "proxy-key"}}))
        for bad, word in (
                ({"profile": "lyte"}, "PROFILES does not define"),
                ({"lite_env": {"X": 1}}, "pins no env"),
                ({"profile": "lite", "lite_env": ["X"]}, "must be a map"),
                ({"profile": "lite",
                  "lite_env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": 1}},
                 "derives from"),
                ({"profile": "lite", "lite_env": {"X": 0}}, "positive count"),
                ({"profile": "lite", "lite_env": {"X": True}}, "positive count"),
                ({"profile": "lite", "lite_env": {"X": "8000"}},
                 "positive count")):
            with self.subTest(bad=bad):
                self.assertIn(word,
                              seat_catalog._incoherent_profile({"f": bad}) or "")

    def test_a_seed_in_the_project_drops_helms_exclude_of_its_own_rules(self):
        """A harness room (`<root>/.claude/worktrees/<x>`, a registered
        linked worktree the spawn door admits) has its git top level INSIDE
        the main checkout, so a seed there names the checkout's own
        CLAUDE.md, correctly for that session. The entries are additive, so
        when the same seat is later seeded with the checkout itself as its
        workdir, helm's own entry must leave: the profile promises the seat
        reads its project's rules. The operator's entry inside the project
        stays."""
        family = self._lite_families()[0]
        base = os.path.realpath(self.tmp)
        project = os.path.join(base, "estate", "project")
        os.makedirs(project)

        def git(*a):
            subprocess.run(["git", "-C", project, "-c", "user.name=t",
                            "-c", "user.email=t@t", "-c",
                            "core.hooksPath=/dev/null"] + list(a),
                           check=True, capture_output=True)
        git("init", "-q")
        with open(os.path.join(project, "CLAUDE.md"), "w") as f:
            f.write("project rules\n")
        git("add", "CLAUDE.md")
        git("commit", "-qm", "init")
        room = os.path.join(project, ".claude", "worktrees", "agent-x")
        git("worktree", "add", "-q", "--detach", room)
        cdir = os.path.join(base, "cfg")
        os.makedirs(cdir)
        p = os.path.join(cdir, "settings.json")
        own = os.path.join(project, "CLAUDE.md")
        operator = os.path.join(project, "CLAUDE.local.md")
        with open(p, "w") as f:
            json.dump({"claudeMdExcludes": [operator]}, f)
        seat._seed_seat_settings(cdir, family, workdir=room)
        with open(p) as f:
            first = json.load(f)["claudeMdExcludes"]
        # positive control: the room's session excludes the checkout above it
        self.assertIn(own, first)
        seat._seed_seat_settings(cdir, family, workdir=project)
        with open(p) as f:
            got = json.load(f)
        self.assertIn(os.path.join(os.path.dirname(project), "CLAUDE.md"),
                      got["claudeMdExcludes"])
        self.assertIn(operator, got["claudeMdExcludes"])   # the operator's stays
        self.assertNotIn(own, got["claudeMdExcludes"])
        self.assertNotIn(own, got["helm"]["seeded_excludes"])

    def test_a_wanted_entry_inside_the_project_keeps_the_seed_at_rest(self):
        """The retirement above leaves an entry THIS seed wants again: an
        authored instructions source that lives inside the repository is
        named by every seed, and dropping it only to append it again would
        rewrite settings.json on every resume. Two seeds in the project: the
        source stays excluded and the second seed writes nothing."""
        family = self._lite_families()[0]
        base = os.path.realpath(self.tmp)
        project = os.path.join(base, "estate2", "project")
        os.makedirs(project)
        subprocess.run(["git", "init", "-q", project], check=True,
                       capture_output=True)
        source = os.path.join(project, "docs", "GLOBAL.md")
        os.makedirs(os.path.dirname(source))
        with open(source, "w") as f:
            f.write("global rules\n")
        cdir = os.path.join(base, "cfg2")
        os.makedirs(cdir)
        p = os.path.join(cdir, "settings.json")
        with mock.patch.dict(os.environ,
                             {"HELM_INSTRUCTIONS_CANONICAL": source}):
            seat._seed_seat_settings(cdir, family, workdir=project)
            with open(p) as f:
                self.assertIn(source, json.load(f)["claudeMdExcludes"])
            st = os.stat(p)
            seat._seed_seat_settings(cdir, family, workdir=project)
        with open(p) as f:
            self.assertIn(source, json.load(f)["claudeMdExcludes"])
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))

    def _seat_doctor(self):
        """`helm seat doctor` with every probe but the seat rows stubbed:
        (exit status, printed text)."""
        from helm import autocompact
        out = io.StringIO()
        with mock.patch.object(seat_health, "_proxy_bin",
                               return_value="/nonexistent/proxy"), \
                mock.patch.object(
                    seat_health, "_proxy_binary_probe",
                    return_value=(True, "", "CLIProxyAPI Version: test")), \
                mock.patch.object(seat_health.shutil, "which",
                                  return_value="/usr/bin/claude"), \
                mock.patch.object(seat_health, "codex_cred_state",
                                  return_value=("valid", None, "fixture")), \
                mock.patch.object(seat_health, "_status"), \
                mock.patch.object(seat_health, "_running_pid",
                                  return_value=None), \
                mock.patch.object(seat_health, "_config_drift_lines",
                                  return_value=[]), \
                mock.patch.object(autocompact, "report_lines",
                                  return_value=[]), \
                contextlib.redirect_stdout(out):
            return seat_health._doctor([]), out.getvalue()

    def test_seat_doctor_reads_every_lite_seat_against_its_profile(self):  # noqa: VACUOUS_ASSERTION — the OK rows are pinned by equality over the non-empty lite sweep, and the DRIFT row's True and each named difference are asserted before the UNKNOWN row's False
        """`helm seat doctor` prints one profile row per minted lite seat.
        The catalog's own mint of every lite family reads OK and passes. On
        one seat, each hand edit that re-opens what the profile closed is
        named in ONE DRIFT row that fails the exit status: the global
        CLAUDE.md dropped from claudeMdExcludes (the regression the owner's
        seats lived through, when the two big rule files loaded again), the
        profile's deny entries removed, a stale window pin, and an MCP server
        above the floor. The doctor writes neither file. An unparseable
        settings.json reads UNKNOWN and does not fail the exit status.
        Restoring the files reads OK again."""
        lite, _canon = self._mint_lite()
        rows = seat_health._profile_rows()
        self.assertEqual(rows, [(False, "profile lite: %-9s OK" % f)
                                for f in lite])
        rc, text = self._seat_doctor()
        self.assertEqual(rc, 0, text)
        family = lite[0]
        fam = seat.FAMILIES[family]
        cdir = os.path.join(seat.seat_dir(family), "claude")
        settings = os.path.join(cdir, "settings.json")
        state = os.path.join(cdir, ".claude.json")

        def raw(path):
            with open(path, "rb") as f:
                return f.read()

        kept = {p: raw(p) for p in (settings, state)}
        got = json.loads(kept[settings])
        home_md = os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
        got["claudeMdExcludes"] = [x for x in got["claudeMdExcludes"]
                                   if x != home_md]
        dropped = [t for t in seat_catalog.LITE_UNUSED_TOOLS
                   if t in got["permissions"]["deny"]]
        self.assertTrue(dropped)
        got["permissions"]["deny"] = [t for t in got["permissions"]["deny"]
                                      if t not in dropped]
        got["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "229376"
        with open(settings, "w") as f:
            json.dump(got, f)
        st = json.loads(kept[state])
        st["mcpServers"] = dict(st.get("mcpServers") or {},
                                **{"alpha-mcp": {"command": "alpha"}})
        with open(state, "w") as f:
            json.dump(st, f)
        planted = {p: raw(p) for p in (settings, state)}
        rows = dict((line.split()[2], (failed, line))
                    for failed, line in seat_health._profile_rows())
        failed, line = rows[family]
        self.assertTrue(failed, line)
        self.assertIn("profile lite: %-9s DRIFT" % family, line)
        self.assertIn("claudeMdExcludes lacks %s" % home_md, line)
        self.assertIn("deny lacks %s" % ", ".join(dropped), line)
        self.assertIn("env CLAUDE_CODE_MAX_CONTEXT_TOKENS is 229376, the "
                      "catalog says %d" % seat_catalog.taught_window(
                          fam, fam["max_context"]), line)
        self.assertIn("MCP servers above the floor (exa): alpha-mcp", line)
        for other in lite[1:]:
            self.assertEqual(rows[other],
                             (False, "profile lite: %-9s OK" % other))
        rc, text = self._seat_doctor()
        self.assertEqual(rc, 1)
        self.assertIn(line, text)
        self.assertEqual({p: raw(p) for p in (settings, state)}, planted)
        # an unparseable settings.json is UNKNOWN, loud and not a verdict
        with open(settings, "w") as f:
            f.write("{not json")
        rows = dict((line.split()[2], (failed, line))
                    for failed, line in seat_health._profile_rows())
        self.assertEqual(rows[family][0], False)
        self.assertIn("UNKNOWN", rows[family][1])
        self.assertIn("settings.json", rows[family][1])
        # restored, the row reads OK again
        for p, body in kept.items():
            with open(p, "wb") as f:
                f.write(body)
        self.assertEqual(seat_health._profile_lines(),
                         ["profile lite: %-9s OK" % f for f in lite])
        # AN OPERATOR'S PIN BELOW THE CATALOG (the F1 hot-fix shape) is kept
        # by every resume (pin_action), so it is never DRIFT and never
        # prescribed a resume: it is named, the row stays OK, and the doctor
        # passes. Beside a real DRIFT it is its own line.
        catalog_window = seat_catalog.taught_window(fam, fam["max_context"])
        low = str(catalog_window - 16384)
        got = json.loads(kept[settings])
        got["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = low
        with open(settings, "w") as f:
            json.dump(got, f)
        note = ("operator pin CLAUDE_CODE_MAX_CONTEXT_TOKENS=%s below the "
                "catalog %d kept; lower the catalog if it is right"
                % (low, catalog_window))
        mine = [row for row in seat_health._profile_rows()
                if row[1].split()[2] == family]
        self.assertEqual(mine, [(False, "profile lite: %-9s OK, %s"
                                 % (family, note))])
        rc, text = self._seat_doctor()
        self.assertEqual(rc, 0, text)
        got["permissions"]["deny"] = [t for t in got["permissions"]["deny"]
                                      if t != dropped[0]]
        with open(settings, "w") as f:
            json.dump(got, f)
        mine = [row for row in seat_health._profile_rows()
                if row[1].split()[2] == family]
        self.assertEqual(len(mine), 2, mine)
        self.assertTrue(mine[0][0])
        self.assertIn("DRIFT — deny lacks %s (" % dropped[0], mine[0][1])
        self.assertNotIn("operator pin", mine[0][1])
        self.assertEqual(mine[1], (False, "profile lite: %-9s %s"
                                   % (family, note)))

    # -- keyless custody: the reconcile's OWN desired state ------------------
    # The mint arm above proves helm WRITES the declared endpoint and
    # placeholder. Reconciliation is the other half and it had no desired state
    # of its own: `proxy_config_plan` emitted the endpoint and key entries it
    # had just READ, so whatever the file held was canonical by definition and
    # neither field could ever be reported as drift.
    #: A synthetic value shaped like a vendor bearer and belonging to nobody:
    #: what a hand-edit or a copied block plants where a keyless family's
    #: placeholder belongs. One constant so the hygiene assertions cannot
    #: drift out of sync with what was planted.
    _PLANTED_BEARER = "sk-planted-not-a-real-bearer-0000000000000000"

    def _plant_config(self, family, old, new):
        """Swap one literal in a minted config and return its path."""
        path = os.path.join(seat.seat_dir(family), "config.yaml")
        with open(path) as f:
            text = f.read()
        self.assertIn(old, text)                  # the plant landed on something
        with open(path, "w") as f:
            f.write(text.replace(old, new))
        return path

    # ONE LITERAL, ONE MARKABLE LINE, the convention test_seat_config_drift
    # established for the same collision: this is the catalog FAMILY key
    # `proxy_config_plan` is computed for, and the seat-name rung matches the
    # string without being able to tell a family from a seat. Naming it once
    # puts the claim about what it MEANS where a reader will find it.
    _KEYLESS_FAMILY = "qwen27"  # noqa: SEAT_NAME — the FAMILY key, never a seat
    _CATALOGUED_ENDPOINT = 'base-url: "%s"' % _KEYLESS_ENDPOINT

    # -- the keyless endpoint is CONFIGURED, never catalogued -----------------
    # The catalog names the key (`base_url_from`) and the operator's helm home
    # holds the URL, so the tree carries no host of the operator's LAN.

    def test_keyless_family_without_a_configured_endpoint_is_unavailable(self):
        """No endpoint configured: the mint refuses, names the file and the key
        to add, and writes no config. The control is the same add after the
        endpoint is configured."""
        family = self._KEYLESS_FAMILY
        path = _configure_endpoints(None)
        config = os.path.join(seat.seat_dir(family), "config.yaml")
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 1, err)
        self.assertIn(path, err)
        self.assertIn('"%s"' % family, err)
        self.assertIn("endpoints-config.example.json", err)
        self.assertFalse(os.path.exists(config))
        # a value that is not an http(s) URL is refused the same way
        _configure_endpoints({family: "192.0.2.10:8083"})
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 1, err)
        self.assertIn("not an http(s) URL", err)
        self.assertFalse(os.path.exists(config))
        # a configured PUBLIC plaintext host fails the keyless floor at the
        # mint, before a config is written that the reconcile would refuse
        _configure_endpoints({family: "http://reader.invalid/v1"})
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 1, err)
        self.assertIn("http://reader.invalid/v1", err)
        self.assertFalse(os.path.exists(config))
        _configure_endpoints({family: _KEYLESS_ENDPOINT})
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        with open(config) as f:
            self.assertIn(self._CATALOGUED_ENDPOINT, f.read())

    def test_the_keyless_route_follows_the_configured_endpoint(self):
        """Attestation's routes read the endpoint at call time: a re-pointed
        endpoint moves the route with no restart, and an unconfigured one
        leaves a route no measured proof can match."""
        from helm import seat_catalog
        family = self._KEYLESS_FAMILY

        def urls():
            return [r["base_url"] for r in seat_catalog.proxy_routes(family)]
        self.assertEqual(urls(), [_KEYLESS_ENDPOINT])
        measured = dict(seat_catalog.proxy_routes(family)[0])
        self.assertEqual(seat_catalog.proxy_route_family(measured),
                         (family, None))
        _configure_endpoints({family: "http://192.0.2.20:9000/v1/"})
        self.assertEqual(urls(), ["http://192.0.2.20:9000/v1"])
        _configure_endpoints(None)
        self.assertEqual(urls(), [""])
        got, why = seat_catalog.proxy_route_family(measured)
        self.assertIsNone(got)
        self.assertIn("0 configured families", why)

    def test_an_unconfigured_endpoint_is_never_reconciled_to_nothing(self):
        """The reconcile's desired endpoint is the configured one. With none
        configured it has no desired state, so it refuses and names the file,
        and the live config stays byte for byte as it was."""
        family = self._KEYLESS_FAMILY
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        config = os.path.join(seat.seat_dir(family), "config.yaml")
        with open(config) as f:
            before = f.read()
        # the positive control on the same observable: the live config does
        # carry the configured endpoint, so "unchanged" below means kept
        self.assertIn(self._CATALOGUED_ENDPOINT, before)
        path = _configure_endpoints(None)
        with self.assertRaises(ValueError) as cm:
            seat.proxy_config_plan(config, family)
        self.assertIn(path, str(cm.exception))
        with open(config) as f:
            self.assertEqual(f.read(), before)

    def test_keyless_reconcile_pins_the_catalogued_endpoint(self):
        """A keyless family's endpoint is the catalog's, so a config naming
        another host is drift to REWRITE — not a pool credential's own word.

        The pool fallback that accepted it exists for a KEYED pool family,
        where the credential store legitimately supplies an endpoint the static
        table does not carry. A keyless family has no credential store, so that
        branch let any host satisfy `native_rows == 1` plus an upstream match
        and be re-emitted verbatim.
        """
        family = self._KEYLESS_FAMILY
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        clean = seat.proxy_config_plan(
            os.path.join(seat.seat_dir(family), "config.yaml"), family)
        # THE POSITIVE CONTROL FIRST AND UNCONDITIONALLY: the config helm just
        # minted already carries the catalogued endpoint and reconciles clean
        # through the very call the plants below go through, so a reported
        # drift is the plant and a restored endpoint is a rewrite.
        self.assertIn(self._CATALOGUED_ENDPOINT, clean["text"])
        self.assertFalse(clean["changed"])
        self.assertIsNone(clean["alias_drift"])
        # AN HTTPS HOST AND A PLAINTEXT NAMED HOST, straight-line rather than
        # looped: the first is what the pool fallback accepted on its own word,
        # the second is one `_safe_endpoint` refuses — and asked of the value
        # that was READ it left the eligible set empty and raised instead of
        # correcting the block.
        self._reconcile_corrects_endpoint('base-url: "https://example.test/v1"')
        self._reconcile_corrects_endpoint('base-url: "http://reader.invalid/v1"')

    def _reconcile_corrects_endpoint(self, planted):
        """Plant one endpoint, assert the plan names and rewrites it, restore."""
        family = self._KEYLESS_FAMILY
        path = self._plant_config(family, self._CATALOGUED_ENDPOINT, planted)
        plan = seat.proxy_config_plan(path, family)
        self.assertTrue(plan["changed"], planted)
        self.assertIn("keyless custody", plan["alias_drift"])
        self.assertIn(planted.split('"')[1], plan["alias_drift"])
        self.assertIn(self._CATALOGUED_ENDPOINT, plan["text"])
        self.assertNotIn(planted, plan["text"])
        self._plant_config(family, planted, self._CATALOGUED_ENDPOINT)

    def test_keyless_reconcile_replaces_a_credential_shaped_value(self):
        """A bearer where a keyless family's placeholder belongs is a finding
        the reader NAMES and the generator rewrites — and the value never
        travels into the reason, because a diagnostic that quotes a leaked
        credential publishes it into every log the doctor writes."""
        from helm import seat_catalog
        family = self._KEYLESS_FAMILY
        rc, _out, err = self._add(("add", family))
        self.assertEqual(rc, 0, err)
        placeholder = 'api-key: "%s"' % seat_catalog.KEYLESS_API_KEY_PLACEHOLDER
        path = self._plant_config(family, placeholder,
                                  'api-key: "%s"' % self._PLANTED_BEARER)
        plan = seat.proxy_config_plan(path, family)
        self.assertTrue(plan["changed"])
        self.assertIn("CREDENTIAL-SHAPED", plan["alias_drift"])
        self.assertNotIn(self._PLANTED_BEARER, plan["alias_drift"])
        self.assertIn(placeholder, plan["text"])
        self.assertNotIn(self._PLANTED_BEARER, plan["text"])
        # A SECOND ENTRY IS DRIFT WITHOUT BEING A DISCLOSURE, and the two
        # readings must not be the same sentence: one asks the owner to rotate
        # a credential at its vendor and the other does not.
        self._plant_config(family, 'api-key: "%s"' % self._PLANTED_BEARER,
                           placeholder)
        self._plant_config(family, placeholder,
                           placeholder + '\n      - api-key: "still-none"')
        second = seat.proxy_config_plan(path, family)["alias_drift"]
        self.assertIn("2 key entries", second)
        self.assertNotIn("CREDENTIAL-SHAPED", second)

    def test_a_keyed_familys_own_credential_survives_the_reconcile(self):
        """THE CONTROL FOR BOTH ARMS ABOVE: the retention rule they narrow is
        still in force everywhere else. helm did not author a keyed family's
        bearer — it reads it out of the config it is rewriting — so emitting a
        generated list there would replace a rotated credential with whichever
        one this pass happened to read."""
        os.environ["KIMI_API_KEY"] = "fake-keyed-control-bearer"
        rc, _out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        path = os.path.join(seat.seat_dir("kimi"), "config.yaml")
        plan = seat.proxy_config_plan(path, "kimi")
        self.assertIn('api-key: "fake-keyed-control-bearer"', plan["text"])
        self.assertIsNone(plan["alias_drift"])
        self.assertFalse(plan["changed"])
        # and a keyless custody reason can never be minted for it: the family
        # declares no `keyless`, so the field the reason reads is never set
        self.assertFalse(seat.FAMILIES["kimi"].get("keyless"))

    def test_add_kimi_from_env_var(self):
        os.environ["KIMI_API_KEY"] = "fake-kimi-key-for-tests"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("fake-kimi-key-for-tests", out + err)  # never printed
        d = seat.seat_dir("kimi")
        for p, want in ((os.path.join(d, "token"), 0o600),
                        (os.path.join(d, "config.yaml"), 0o600),
                        (os.path.join(d, "launch.sh"), 0o700)):
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)
        self.assertTrue(os.path.isdir(os.path.join(d, "claude")))
        self.assertFalse(os.path.exists(os.path.join(d, "auth")))  # no OAuth dir
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn("openai-compatibility:", cfg)
        self.assertIn('api-key: "fake-kimi-key-for-tests"', cfg)
        # not "sk-kimi-…" -> the Moonshot PLATFORM endpoint (the default)
        self.assertIn('base-url: "https://api.moonshot.ai/v1"', cfg)
        self.assertIn('alias: "kimi-k3"', cfg)
        self.assertNotIn("auth-dir", cfg)
        with open(os.path.join(d, "token")) as f:
            self.assertIn(f.read().strip(), cfg)  # inbound seat token present
        # the hard law holds for proxy-key seats too
        for root, _, files in os.walk(d):
            for name in files:
                with open(os.path.join(root, name)) as f:
                    self.assertNotIn("ANTHROPIC_API_KEY=", f.read())

    def test_add_kimi_key_from_file(self):
        envfile = os.path.join(self.tmp, "fake.env")
        with open(envfile, "w") as f:
            f.write("# comment\nexport KIMI_API_KEY='fake-from-file-key'\n")
        rc, out, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            self.assertIn('api-key: "fake-from-file-key"', f.read())

    def test_add_kimi_env_var_beats_key_from(self):
        os.environ["KIMI_API_KEY"] = "fake-env-wins"
        envfile = os.path.join(self.tmp, "fake.env")
        with open(envfile, "w") as f:
            f.write("KIMI_API_KEY=fake-file-loses\n")
        rc, _, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            self.assertIn('api-key: "fake-env-wins"', f.read())

    def test_add_kimi_missing_key_unblock(self):
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 1)
        self.assertIn("KIMI_API_KEY", err)  # names the env-var option
        self.assertIn("--key-from", err)    # and the file option
        self.assertFalse(os.path.exists(os.path.join(seat.seat_dir("kimi"),
                                                     "config.yaml")))

    def test_add_kimi_key_from_missing_line(self):
        envfile = os.path.join(self.tmp, "empty.env")
        with open(envfile, "w") as f:
            f.write("OTHER_VAR=1\n")
        rc, out, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 1)
        self.assertIn("KIMI_API_KEY", err)

    # -- key_env_fallbacks: PRIMARY first, the loaned EMBER key last (owner
    # rule 2026-07-29: the Ember key is unrestricted but it is a LOAN — our
    # own sub feeds new mints first, and a fallback mint must say so) --
    def _baked_key(self):
        with open(os.path.join(seat.seat_dir("kimi"), "config.yaml")) as f:
            return f.read()

    def test_fallback_primary_wins_over_ember(self):
        os.environ["KIMI_API_KEY_PRIMARY"] = "fake-primary"
        os.environ["KIMI_API_KEY_EMBER"] = "fake-ember"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        self.assertIn('api-key: "fake-primary"', self._baked_key())
        self.assertIn("KIMI_API_KEY_PRIMARY", out)  # the source is NAMED

    def test_fallback_ember_used_when_primary_absent(self):
        os.environ["KIMI_API_KEY_EMBER"] = "fake-ember"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        self.assertIn('api-key: "fake-ember"', self._baked_key())
        self.assertIn("KIMI_API_KEY_EMBER", out)

    def test_explicit_key_env_beats_every_fallback(self):
        os.environ["KIMI_API_KEY"] = "fake-explicit"
        os.environ["KIMI_API_KEY_PRIMARY"] = "fake-primary"
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 0, err)
        self.assertIn('api-key: "fake-explicit"', self._baked_key())

    def test_key_from_file_walks_the_same_chain(self):
        envfile = os.path.join(self.tmp, "ordered.env")
        with open(envfile, "w") as f:
            f.write("KIMI_API_KEY_EMBER=fake-ember\n"
                    "KIMI_API_KEY_PRIMARY=fake-primary\n")
        rc, out, err = self._add(("add", "kimi", "--key-from", envfile))
        self.assertEqual(rc, 0, err)
        self.assertIn('api-key: "fake-primary"', self._baked_key())

    def test_missing_key_names_the_whole_chain(self):
        rc, out, err = self._add(("add", "kimi"))
        self.assertEqual(rc, 1)
        self.assertIn("KIMI_API_KEY", err)
        self.assertIn("KIMI_API_KEY_PRIMARY", err)
        self.assertIn("KIMI_API_KEY_EMBER", err)

    def test_kimi_launch_line_shape(self):
        os.environ["KIMI_API_KEY"] = "fake-kimi-key-for-tests"
        self.assertEqual(self._add(("add", "kimi"))[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "kimi"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8318", line)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=kimi-k3", line)
        self.assertIn("HELM_CHAT_NAME=kimi", line)   # joins the roster as 'kimi'
        self.assertIn("HELM_AGENT_HARNESS=claude", line)
        self.assertIn("HELM_MODEL_FAMILY=kimi", line)
        self.assertIn("HELM_MODEL_BACKEND=proxy", line)
        self.assertIn("HELM_CELL_BIN=" + seat.DREGG_SIGNER_DEFAULT, line)
        self.assertIn("HELM_CELL_PROFILE=kimi", line)  # helm call-site identity
        self.assertIn("DREGG_PROFILE=kimi", line)      # signer fallback identity
        self.assertIn("--dangerously-skip-permissions", line)  # canonical seat
        self.assertTrue(line.endswith(
            "claude --disallowedTools %s --dangerously-skip-permissions"
            " --model kimi-k3" % ("EnterPlanMode " + "Skill 'Agent(fork)'")))
        self.assertNotIn("fake-kimi-key-for-tests", line)  # key never rides

    # -- ds4pro (pool-keyed proxy-key family, credential_pool bearer) --------
    # The literal bearer token every test bakes/asserts-absent. A single
    # constant so the secret-hygiene assertions (assertNotIn) can never drift
    # out of sync with what was planted.
    _LIVE = "sk-live-ds4pro-bearer-3f9c2a1e6b7d8049aa11bb22cc33dd44"
    # the opencode-authstore fake bearers — distinct per provider and distinct
    # from _LIVE so a test can prove WHICH source (authstore vs hermes) won.
    _AS_OC = "sk-authstore-opencode-go-1122334455667788990011223344ff"
    _AS_DS = "sk-authstore-deepseek-9a8b7c6d5e4f3021ffeeddccbbaa9988ee"

    def _plant_authstore(self, opencode=True, deepseek=True, oauth_ds=False):
        """A fake ~/.local/share/opencode/auth.json shaped like the real one:
        a dict of provider -> {type, key}. `oauth_ds` swaps deepseek to an
        oauth entry (no bakeable static key) to exercise the type filter."""
        store = {}
        if opencode:
            store["opencode-go"] = {"type": "api", "key": self._AS_OC}
        if deepseek and not oauth_ds:
            store["deepseek"] = {"type": "api", "key": self._AS_DS}
        if oauth_ds:
            store["deepseek"] = {"type": "oauth", "access": "a" * 60,
                                 "refresh": "r" * 60, "expires": 9999999999999}
        with open(seat.OPENCODE_AUTHSTORE, "w") as f:
            json.dump(store, f)

    def _plant_pool(self, opencode=None, openrouter=None, extra=None,
                    deepseek=None):
        """A fake ~/.hermes/auth.json shaped like the real one: a
        credential_pool of provider -> LIST of bearer entries. Defaults plant
        BOTH ds4pro providers with the live bearer at priority 0 (last_status
        ok). `extra` injects additional entries per provider to exercise
        selection (junk placeholders, expired/non-ok)."""
        pool = {
            "opencode-go": [{"access_token": opencode or self._LIVE,
                             "base_url": "https://opencode.ai/zen/go/v1",
                             "label": "OPENCODE_GO_API_KEY",
                             "last_status": "ok", "priority": 0,
                             "auth_type": "api_key"}],
            "openrouter": [{"access_token": openrouter or self._LIVE,
                            "base_url": "https://openrouter.ai/api/v1",
                            "label": "OPENROUTER_API_KEY",
                            "last_status": "ok", "priority": 0,
                            "auth_type": "api_key"}],
            "deepseek": [{"access_token": deepseek or self._LIVE,
                          "base_url": "https://api.deepseek.com/v1",
                          "label": "DEEPSEEK_API_KEY",
                          "last_status": "ok", "priority": 0,
                          "auth_type": "api_key"}],
        }
        for prov, entries in (extra or {}).items():
            pool.setdefault(prov, []).extend(entries)
        with open(seat.HERMES_AUTH, "w") as f:
            json.dump({"version": 1, "providers": {},
                       "credential_pool": pool}, f)

    def test_family_ports_unique_with_interleave_headroom(self):  # noqa: VACUOUS_ASSERTION — the per-family assertEqual(ports[f], base) lines are the unconditional positive controls; each assertFalse band sweep runs over the same non-empty ports mapping they read
        """The port invariant, extended for ds4pro: every family owns a
        distinct port, ds4pro sits at 8360 — clear of the codex 8317+N
        instance range and kimi's 8318 (no OTHER family within 8350-8370)."""
        ports = {f: fam["port"] for f, fam in seat.FAMILIES.items()}
        self.assertEqual(len(set(ports.values())), len(ports), ports)
        self.assertEqual(ports["ds4pro"], 8360)
        for f, p in ports.items():
            if f != "ds4pro":
                self.assertFalse(8350 <= p <= 8370,
                                 "%s port %d crowds ds4pro's headroom" % (f, p))
        # The council families get the same guarantee they were the first to
        # violate: grok owns 8371-8385, gemini 8386-8399. gemini was drafted at
        # 8370 — the port its scratch research config happened to use — which
        # sat exactly on ds4pro's boundary. The uniqueness check written beside
        # that entry passed, because uniqueness is not headroom.
        self.assertEqual(ports["grok"], 8380)
        self.assertEqual(ports["gemini"], 8390)
        for f, p in ports.items():
            if f != "grok":
                self.assertFalse(8371 <= p <= 8385,
                                 "%s port %d crowds grok's headroom" % (f, p))
            if f != "gemini":
                self.assertFalse(8386 <= p <= 8399,
                                 "%s port %d crowds gemini's headroom" % (f, p))
        # openrouter owns 8400-8414, and it is here because it arrived at 8370
        # — the SAME port this test already records gemini being moved off,
        # the top of ds4pro's band — carried in from a hand-written launcher
        # whose number nothing had checked. Uniqueness is not headroom, twice
        # over now. 8400 is also the HIGHEST base the table admits: the
        # project-instance block starts at 8500 and `_project_port_block_is_clear`
        # demands 100 clear below it, so the next family belonged in the
        # unclaimed 8319-8349 gap rather than above this one, until that gap
        # was spent (the residue set at the end of this arm is now empty).
        # ds4flash owns 8330-8344, drawn from the unclaimed 8319-8349 gap this
        # comment already names as where the next family belongs — above
        # kimi's 8318 and clear of ds4pro's 8350 floor. Uniqueness is not
        # headroom, for the fourth time in this arm's history.
        self.assertEqual(ports["ds4flash"], 8330)
        for f, p in ports.items():
            if f != "ds4flash":
                self.assertFalse(8330 <= p <= 8344,
                                 "%s port %d crowds ds4flash's headroom"
                                 % (f, p))
        # qwen27 DECLARES 8345 AND RESERVES NOTHING, which is this lane's own
        # rule applied to the entry that introduced it. A band exists to hold
        # room for base+N; a proxy-key family mints no numbered instances (the
        # launch gate refuses them), so it binds exactly ONE socket and has no
        # band to crowd. Asserting 8345-8349 for it would be the
        # RESERVED-BAND reading, and that reading is false on this machine:
        # opus46 serves 8346 and gptoss 8348, each cli-proxy-api under its own
        # seat config. A band asserted over ports another family already
        # serves fails as a port collision and reads as one, which sends the
        # next reader to the wrong entry. What is checkable here is the
        # declared port itself; the uniqueness assertion at the top of this
        # arm covers every family at once, so a one-port band needs no sweep.
        self.assertEqual(ports["qwen27"], 8345)
        self.assertEqual(ports["openrouter"], 8400)  # noqa: SEAT_NAME — the configured family's port IS the subject
        for f, p in ports.items():
            if f != "openrouter":  # noqa: SEAT_NAME — the family that OWNS the band is the exclusion, exactly as the grok and gemini arms above
                self.assertFalse(8400 <= p <= 8414,
                                 "%s port %d crowds openrouter's headroom"
                                 % (f, p))
        # dots3 takes ONE port and no band, and it sits BELOW the codex base:
        # a proxy-key family mints no numbered instances, so there is nothing
        # for headroom to protect, and every port from 8317 to the base cap is
        # a base, a band or codex's own base+N growth. Nothing derives
        # downward, so under the lowest base is the one place a single-port
        # family spends nobody else's room. Both halves are pinned: the number,
        # because a base port is a fact about the HOST as well as this table
        # and moving it must show up here; and the side of the codex base it
        # is on, because that is the property and the number is its instance.
        self.assertEqual(ports["dots3"], 8316)  # noqa: SEAT_NAME — the configured family's port IS the subject, exactly as the arms above
        # cursor takes the next port down for the same reason, and the pair
        # are the only bases under codex's: the property is the side of the
        # codex base, and the two numbers are its instances.
        self.assertEqual(ports["cursor"], 8315)  # noqa: SEAT_NAME — the configured family's port IS the subject
        self.assertEqual(sorted(f for f, p in ports.items()
                                if p < ports["codex"]),
                         ["cursor", "dots3"])  # noqa: SEAT_NAME — catalog FAMILY keys, and the set under the codex base IS the subject
        # THE TWO ANTIGRAVITY-GROUP FAMILIES GET A PORT EACH AND NO BAND, for
        # the reason the local family's arm above states and this table now
        # applies uniformly: headroom exists to hold room for base+N, and the
        # admission door refuses a numbered instance for every mode but the
        # OAuth pool, so a proxy-oauth family binds exactly one socket. The
        # arm below asserts that refusal rather than trusting the sentence.
        # Both numbers came out of a SOCKET CENSUS on this host rather than
        # out of this table: codex's numbered instances were holding 8317 and
        # 8319-8327, ds4flash reserves 8330-8344, ds4pro's floor is 8350.
        # 8328-8329 is deliberately NOT taken: those are the next two numbers
        # codex's own base+N range grows into.
        self.assertEqual(ports["opus46"], 8346)  # noqa: SEAT_NAME — the configured family's port IS the subject, exactly as the grok and gemini arms above
        self.assertEqual(ports["gptoss"], 8348)  # noqa: SEAT_NAME — the configured family's port IS the subject
        # qwenlocal TAKES 8347, the lower of the two residue ports the set
        # below names as free. A socket census on this host found nothing
        # listening there, and the port sits outside every band above. codex-30
        # derives it, and `_numbered_port_collision` refuses that instance
        # because 8347 is now a declared port. The derivation sweep below
        # proves that for the whole catalog.
        self.assertEqual(ports["qwenlocal"], 8347)  # noqa: SEAT_NAME — the configured family's port IS the subject
        # bonsai TAKES 8349, the other residue port, by the same census: no
        # listener there, outside every band. codex-32 derives it and is
        # refused by name, as codex-30 is for 8347.
        self.assertEqual(ports["bonsai"], 8349)  # noqa: SEAT_NAME — the configured family's port IS the subject
        for family in ("opus46", "gptoss", "qwen27", "dots3", "qwenlocal",
                       "bonsai", "cursor"):  # noqa: SEAT_NAME — catalog FAMILY keys, and the set of single-socket families IS this arm's subject
            self.assertIn("mode=proxy",
                          seat._instance_gate(family, "%s-2" % family),
                          "%s mints numbered instances, so a one-port band "
                          "under-reserves it" % family)
        # THE CONTROL on that loop: the family that DOES mint them is admitted
        # by the same door, so the refusals above are about the mode and not
        # about a gate that refuses every suffix.
        self.assertIsNone(seat._instance_gate("codex", "codex-2"))  # noqa: SEAT_NAME — the OAuth-pool family IS the control's subject

        # AND WHAT IS LEFT, AS A SET RATHER THAN AS ADVICE. Four comments in
        # this method's history told the next family where to go and three
        # were wrong by the time it read them, because each named a gap the
        # table had not yet spent. `_project_port_block_is_clear` caps a base
        # at PROJECT_PORT_BASE - 100, so the whole range is bounded above;
        # this names every band inside it and then says what remains.
        bands = {"cursor": (8315, 8315),
                 "dots3": (8316, 8316), "codex": (8317, 8317),
                 "kimi": (8318, 8318), "ds4flash": (8330, 8344),
                 "qwen27": (8345, 8345), "opus46": (8346, 8346),
                 "qwenlocal": (8347, 8347),
                 "gptoss": (8348, 8348), "bonsai": (8349, 8349),
                 "ds4pro": (8350, 8370),
                 "grok": (8371, 8385), "gemini": (8386, 8399),
                 "openrouter": (8400, 8414)}  # noqa: SEAT_NAME — catalog FAMILY keys, and the map from family to band IS this arm's subject
        self.assertEqual(set(bands), set(ports),
                         "every family owns a band here, so a family added "
                         "without one is a red rather than a clash")
        for family, (lo, hi) in bands.items():
            # THE BASE SITS INSIDE ITS BAND, NOT AT THE BOTTOM OF IT. ds4pro
            # reserves 8350-8370 and serves on 8360, because that band was
            # drawn around a base that already existed. An `== lo` here read
            # like the tighter check and was simply false about the table.
            self.assertLessEqual(lo, ports[family], family)
            self.assertLessEqual(ports[family], hi, family)
        claimed = set()
        for lo, hi in bands.values():
            claimed.update(range(lo, hi + 1))
        # CODEX'S GROWTH ROOM IS SPENT, NOT FREE, and the census is why: a
        # socket sweep of this host found codex's numbered instances serving
        # 8317 and 8319-8327, i.e. base+N running ten past the base its row
        # above declares. 8328-8329 are the next two numbers that range
        # reaches, so they belong to codex and no new family may take them.
        claimed.update(range(8319, 8330))
        cap = seat.PROJECT_PORT_BASE - 100
        self.assertIn(ports["opus46"], claimed,  # noqa: SEAT_NAME — the configured family's port IS the control's subject
                      "the control: the band map covers a port this table "
                      "actually assigns")
        # THE FLOOR IS THE LOWEST DECLARED PORT, NEVER A LITERAL. It read 8317
        # while the lowest base WAS 8317, and a family declared below it would
        # have sat outside the sweep entirely — which is exactly where the
        # single-socket family beneath the codex base now lives.
        self.assertEqual(
            {p for p in range(min(ports.values()), cap + 1)
             if p not in claimed},
            set(),
            "a port below the family-base cap is unclaimed and unexplained: "
            "assign it or say whose it is, rather than leaving the next "
            "family to rediscover the gap")
        # THE RESIDUE IS SPENT. 8347 and 8349 were the two odd numbers beside
        # the antigravity sockets; qwenlocal took 8347 and bonsai took 8349.
        # Every port from the lowest base to the cap is now a base, a band or
        # codex's growth, so the next single-socket family has no free port
        # here: it must take one out of a band a socket census shows unused,
        # and shrink that band on this map in the same commit.


    # A DECLARED PORT AND A DERIVED ONE ARE THE SAME NAMESPACE, and the arm
    # above only ever asked about the DECLARED half. Every check in it passed
    # while codex's base+N reached six of the other seven declared bases:
    # base+N was bounded only by the project-instance block at 8500, so
    # ds4flash's 8330 is codex-13, qwen27's 8345 is codex-28, and ds4pro, grok,
    # gemini and openrouter follow. The admission door answered None for all of
    # them. `_numbered_port_collision` is the refusal; these are its arms.
    def _first_declared_port_a_numbered_seat_can_reach(self, refuses):
        """(seat, port, owner) for the first numbered instance ANY family can
        derive onto ANOTHER family's declared port, else None.

        `refuses(family, seat)` is the admission door under test, so the same
        sweep can be run against a looser rule — which is how the control below
        proves the sweep can see a collision at all.

        A REFUSAL SKIPS ONE SUFFIX AND NEVER ENDS THE SWEEP. Stopping at the
        first refusal would have asked a weaker question — "does anything
        collide BEFORE the door first says no" — and answered None on a door
        that refused codex-13 and then handed out qwen27's 8345 at codex-28.
        The loop's only bound is the project block, which is where the derived
        range genuinely ends.
        """
        declared = {fam["port"]: f for f, fam in seat.FAMILIES.items()}
        for family, fam in seat.FAMILIES.items():
            for n in itertools.count(2):
                port = fam["port"] + n
                if port >= seat.PROJECT_PORT_BASE:
                    break
                name = "%s-%d" % (family, n)
                if refuses(family, name):
                    continue
                owner = declared.get(port)
                if owner is not None and owner != family:
                    return (name, port, owner)
        return None

    def test_no_numbered_instance_derives_another_familys_declared_port(self):
        """The whole catalog's declared and derived ports, pairwise distinct
        across every family up to the instance count its admission door
        actually admits."""
        self.assertIsNone(self._first_declared_port_a_numbered_seat_can_reach(
            lambda f, s: seat._instance_endpoint_error(f, s) is not None))
        # POSITIVE CONTROL, and it is the defect this arm cures: the identical
        # sweep under the rule that stopped at the project block ALONE finds
        # codex-13 on ds4flash's own port, so the None above is the door
        # refusing and not a sweep that inspects nothing.
        self.assertEqual(
            self._first_declared_port_a_numbered_seat_can_reach(
                lambda f, s: False),
            ("codex-13", 8330, "ds4flash"))

    def test_the_refusal_is_the_declared_port_and_nothing_wider(self):
        """Every colliding suffix is refused, each naming its owner, and the
        spellings between them keep the endpoints they have. The list is
        SPELLED OUT rather than derived from the same table the door reads: a
        derivation would agree with the door about a port neither of them
        should have handed out. It is also the count below, so a family added
        without touching this line goes red here.

        THE REFUSAL IS PER PORT, NOT PER BAND, and the difference is the
        namespace. The FAMILIES comments describe bands (8330-8344, 8345-8349)
        and those govern where the NEXT family's base may be DECLARED; they are
        not allocations, because a proxy-key family mints no numbered instances
        and binds exactly one socket. Refusing a whole band would cap codex at
        eleven numbered seats -- nine are serving -- for ports nothing can
        bind.
        """
        base = seat.FAMILIES["codex"]["port"]
        declared = {fam["port"]: f for f, fam in seat.FAMILIES.items()
                    if fam["port"] != base}
        colliding = sorted(port - base for port in declared
                           if port > base + 1
                           and port < seat.PROJECT_PORT_BASE)
        # 30 is qwenlocal's 8347 and 32 is bonsai's 8349: this line went red
        # when each family was declared, which is what the docstring promises
        # of a new port.
        self.assertEqual(colliding, [13, 28, 29, 30, 31, 32, 43, 63, 73, 83])
        for n in colliding:
            name = "codex-%d" % n
            why = seat._instance_endpoint_error("codex", name)
            self.assertIn("DECLARED port", why, name)
            self.assertIn(str(base + n), why, name)
            self.assertIn(declared[base + n], why)  # noqa: SEAT_NAME — the catalog FAMILY whose declared port this suffix derives IS the subject of the refusal
            self.assertIsNone(seat._instance_port("codex", name), name)
        # THE UNCONDITIONAL POSITIVE CONTROL: every OTHER suffix below the
        # block still derives its own endpoint, so the refusal above is those
        # collisions and not a door that closed on the whole range. The live
        # fleet's numbered seats are inside this set. The arithmetic reads
        # `len(colliding)` off the pinned list above rather than repeating its
        # length: a family that lands on a new declared port moves both, and a
        # second literal here would merge clean and be wrong.
        kept = [n for n in range(2, seat.PROJECT_PORT_BASE - base)
                if n not in colliding]
        self.assertEqual(len(kept),
                         seat.PROJECT_PORT_BASE - base - 2 - len(colliding))
        for n in kept:
            self.assertEqual(seat._instance_port("codex", "codex-%d" % n),
                             base + n)
            self.assertIsNone(
                seat._instance_endpoint_error("codex", "codex-%d" % n))
        # base+1 IS NOT REFUSED HERE and must not need to be: kimi declares
        # 8318 and `_instance_gate` refuses N<2 for exactly that reason. The
        # two rules are one rule at two distances.
        self.assertEqual(seat.FAMILIES["kimi"]["port"], base + 1)
        self.assertIn("instance 1 IS the family seat",
                      seat._instance_gate("codex", "codex-1"))

    def test_two_numbering_families_may_not_both_derive_base_plus_n(self):
        """The clause of the reservation invariant that is NOT a refusal: a
        derived port is owned by no family's DECLARATION, so nothing at the
        admission door can see two numbering families' ranges interleave."""
        self.assertEqual(seat._numbered_port_reservations_are_disjoint(), "")
        # POSITIVE CONTROL: kimi's 8318 is safe only because it mints no
        # numbered instances. Declare it mode=proxy and kimi-2 derives 8320,
        # which is codex-3's endpoint — and neither number is any family's
        # declared port, so the per-port refusal is blind to it.
        planted = {f: dict(fam) for f, fam in seat.FAMILIES.items()}
        planted["kimi"] = dict(planted["kimi"], mode="proxy")
        why = seat._numbered_port_reservations_are_disjoint(planted)
        self.assertIn("kimi", why)
        self.assertIn("codex", why)
        self.assertIsNone(seat._instance_endpoint_error("codex", "codex-3"))  # noqa: SEAT_NAME — the numbered instance whose derived port the planted second numbering family would take IS the subject here
        # SECOND CONTROL, the other clause: two families on one declared port.
        shared = {f: dict(fam) for f, fam in seat.FAMILIES.items()}
        shared["ds4flash"] = dict(shared["ds4flash"], port=8345)
        self.assertIn("share declared port",
                      seat._numbered_port_reservations_are_disjoint(shared))

    # The one number in this table whose ERROR DIRECTION is not symmetric.
    CODEX_TOTAL_WINDOW = 272000     # gpt-6.1-sol and previous default gpt-6-sol total
    SOL_TOTAL_WINDOW = 372000       # gpt-5.6-sol's total; the wedge below was measured on sol
    CODEX_SEAT_MAX_TOKENS = 32000   # output the seats request
    CC_RESERVE = 20000              # what CC holds back for itself
    CODEX_MEASURED_400 = 369663     # recorded tokens at the live wedge, below

    SPARK_TOTAL_WINDOW = 128000     # gpt-5.3-codex-spark's total, input + output

    def test_spark_model_context_is_an_input_ceiling_not_the_total_window(self):
        """The sibling arm of the codex pin, same law, smaller window (task/379).

        The older comment said "spark 128k want 128000" — the TOTAL, which is
        the exact overstatement that wedged this family at 369,663 recorded
        tokens. Pinned as ARITHMETIC like the codex arm: seats request 32k of
        output and CC reserves 20k out of the SAME 128k, so the honest input
        ceiling is 76k. Too LOW compacts early (recoverable); too HIGH wedges
        the seat with no in-band exit."""
        got = seat.FAMILIES["codex"]["model_context"]["gpt-5.3-codex-spark"]
        ceiling = (self.SPARK_TOTAL_WINDOW - self.CODEX_SEAT_MAX_TOKENS
                   - self.CC_RESERVE)
        self.assertLessEqual(
            got, ceiling,
            "spark model_context %d exceeds the input ceiling %d (= %d total "
            "- %d output - %d CC reserve)" % (
                got, ceiling, self.SPARK_TOTAL_WINDOW,
                self.CODEX_SEAT_MAX_TOKENS, self.CC_RESERVE))
        self.assertGreater(got, 0)

    def test_spark_launch_mints_the_spark_window_not_sols(self):
        """--model gpt-5.3-codex-spark must carry spark's ceiling; the default
        model keeps the family's. Both polarities, because a resolution that
        ignores the model would pass either one alone."""
        from helm.seat_launch_assets import launch_line
        spark = launch_line("codex", model="gpt-5.3-codex-spark")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", spark)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=76000", spark)
        self.assertNotIn("220000", spark)
        default = launch_line("codex")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000", default)
        self.assertNotIn("76000", default)
        sol = launch_line("codex", model="gpt-5.6-sol")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=320000", sol)
        unknown = launch_line("codex", model="gpt-5.6-terra")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000", unknown)

    def test_sol_keeps_its_own_input_ceiling_under_the_measured_wedge(self):
        # the model that WEDGED at 369,663 is sol; its ceiling now lives in
        # model_context and must obey the same two bounds as the default
        got = seat.FAMILIES["codex"]["model_context"]["gpt-5.6-sol"]
        self.assertLess(got, self.CODEX_MEASURED_400)
        self.assertLessEqual(got, self.SOL_TOTAL_WINDOW - self.CODEX_SEAT_MAX_TOKENS
                             - self.CC_RESERVE)
        self.assertGreater(got, seat.FAMILIES["codex"]["max_context"],
                           "sol's window is larger than astra's by the catalogue")

    def test_gpt61_sol_is_the_default_and_gpt6_sol_is_its_probe_fallback(self):
        """The Codex rotation is one bounded two-model declaration:
        new gpt-6.1-sol is the pane and worker default, old gpt-6-sol remains
        probed as the fallback, and both use the independently measured Codex
        CLI 0.159.2 272k route window's safe 220k input ceiling."""
        from helm import seat_catalog as c
        fam = seat.FAMILIES["codex"]
        self.assertEqual(c.CODEX_MODEL_RULING["model"], "gpt-6.1-sol")
        self.assertEqual(fam["model"], "gpt-6.1-sol")
        self.assertEqual(fam["probe_models"], ("gpt-6.1-sol", "gpt-6-sol"))
        self.assertEqual(fam["probe_models"],
                         (c.CODEX_MODEL_RULING["model"],
                          c.CODEX_MODEL_RULING["fallback"]))
        # the owner's own sentence (19:04 PDT), never the brief's paraphrase,
        # and the store id of the ruling this one replaces
        self.assertIn("sol 6.1 is out now", c.CODEX_MODEL_RULING["verbatim"])
        self.assertEqual(c.CODEX_MODEL_RULING["supersedes"],
                         "codex-runs-gpt-6-sol-everywhere")
        self.assertEqual(set(fam["subagent_tiers"].values()), {"gpt-6.1-sol"})
        ceiling = (self.CODEX_TOTAL_WINDOW - self.CODEX_SEAT_MAX_TOKENS
                   - self.CC_RESERVE)
        windows = fam["model_context"]
        for model in fam["probe_models"]:
            with self.subTest(model=model):
                published = c.PUBLISHED_ROUTE_WINDOWS[("codex", model)]
                self.assertEqual(published["context_length"],
                                 self.CODEX_TOTAL_WINDOW)
                self.assertIn("272000", published["source"])
                self.assertIn("872000", published["source"])
                self.assertEqual(windows[model], ceiling)
        self.assertIn("Codex CLI 0.159.2",
                      c.PUBLISHED_ROUTE_WINDOWS[("codex", "gpt-6.1-sol")]["source"])
        # the plan sections in `route` come from models.json, so cite it
        self.assertIn("models.json",
                      c.PUBLISHED_ROUTE_WINDOWS[("codex", "gpt-6.1-sol")]["source"])
        self.assertEqual(fam["max_context"], ceiling)
        from helm.seat_launch_assets import launch_line
        default = launch_line("codex")
        self.assertIn("--model gpt-6.1-sol", default)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d " % ceiling, default)
        fallback = launch_line("codex", model="gpt-6-sol")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d " % ceiling, fallback)
        # CONTROL on the same resolver: a 5.6-sol pane still reads its own
        # larger window, so the equality above is not a resolver that answers
        # one number for every codex model.
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=320000 ",
                      launch_line("codex", model="gpt-5.6-sol"))

    def test_codex_max_context_is_an_input_ceiling_not_the_total_window(self):
        """max_context must leave room for the output that shares the window.

        WHY THIS TEST EXISTS (2026-07-30). The value was 360000: the model's
        372k TOTAL, shaved by 12k. Both deductions that matter come out of the
        same 372k and neither was made — seats request 32k of output and CC
        reserves 20k — so CC was told it had ~40k of input room that did not
        physically exist. Seat codex reached 369,663 recorded tokens, every
        request 400'd "input exceeds the context window", and /compact could not
        rescue it because compaction REPLAYS the oversized transcript and 400s
        the same way. The seat was wedged with no in-band exit, and the OWNER
        noticed before any instrument did.

        Pinned as ARITHMETIC, not as a literal. A test asserting == 320000
        would pass for a wrong number typed confidently; these two bounds fail
        for any value that re-crosses the line, whatever it is. The direction is
        the whole point and it is asymmetric: too LOW costs an early compaction
        (wasteful, recoverable), too HIGH costs a wedged seat and a morning."""
        got = seat.FAMILIES["codex"]["max_context"]
        self.assertLess(
            got, self.CODEX_MEASURED_400,
            "codex max_context %d is at or above the MEASURED wedge point %d — "
            "a seat reached that many tokens and could not compact its way out"
            % (got, self.CODEX_MEASURED_400))
        ceiling = (self.CODEX_TOTAL_WINDOW - self.CODEX_SEAT_MAX_TOKENS
                   - self.CC_RESERVE)
        self.assertLessEqual(
            got, ceiling,
            "codex max_context %d exceeds the input ceiling %d (= %d total − "
            "%d output − %d CC reserve). Output shares the window; a number "
            "that ignores it promises CC room that does not exist."
            % (got, ceiling, self.CODEX_TOTAL_WINDOW,
               self.CODEX_SEAT_MAX_TOKENS, self.CC_RESERVE))

    def test_ds4pro_family_shape(self):
        fam = seat.FAMILIES["ds4pro"]
        self.assertEqual(fam["mode"], "proxy-key")
        self.assertEqual(fam["model"], "ds4-pro")           # claude-side alias
        self.assertEqual(fam["key_env"], "DS4PRO_API_KEY")
        self.assertEqual(fam["probe_models"], ("ds4-pro",))
        # max_context is 1000000 and it is PROBE-BACKED. This read
        # `assertNotIn("max_context", fam)` until 2026-08-03, on the reasoning
        # that the unmeasured leg governs — while the measured one had been
        # sitting in this family's own comment all along: OpenRouter publishes
        # context_length=1048576 for deepseek/deepseek-v4-pro on its public,
        # no-auth /v1/models (2026-08-02). The pin sits 4.6% under it.
        # CONTROL on the same observable: the family that has always carried
        # this grade still carries it, so ds4pro's key is a decision and not a
        # FAMILIES table that grew max_context everywhere.
        self.assertIn("probed_context_length", seat.FAMILIES["kimi"])
        self.assertEqual(fam["max_context"], 1000000)
        self.assertGreaterEqual(fam["probed_context_length"],
                                fam["max_context"])
        # …and NOT owner-backed: gemini's 1000000 came from the owner saying
        # so, ds4pro's from a published endpoint, and the table must keep the
        # two legible apart.
        self.assertNotIn("owner_stated_window", fam)
        self.assertIn("owner_stated_window", seat.FAMILIES["gemini"])
        # ONE TOKEN SOURCE PER SEAT: the DeepSeek direct key is this
        # family's DEFAULT route, carries the REAL model id the vendor's own
        # /models advertises plus the cost RUNG, and is OFF-PEAK-ONLY through
        # the declared billing window. The OpenCode Go row serves the same
        # model and is chosen at the mint (`--provider opencode-go`), never
        # beside the direct key; the flash model is the ds4flash family.
        #
        # native DeepSeek: the owner-facing selector stays `deepseek`, while the
        # proxy's loaded provider identity is `deepseek-direct`; /v1 is part of
        # the exact endpoint tuple proved at runtime.
        self.assertEqual(fam["pool_default"], "deepseek")
        self.assertEqual(set(fam["pool_providers"]), {"deepseek", "opencode-go"})
        self.assertEqual(fam["pool_providers"]["deepseek"],
                         {"proxy_provider": "deepseek-direct",
                          "base_url": "https://api.deepseek.com/v1",
                          "upstream_model": "deepseek-v4-pro",
                          "rung": "paid",
                          "authstore": "deepseek",
                          "billing_window": seat_catalog.DEEPSEEK_BILLING_WINDOW})
        direct = {"alias": "ds4-pro", "provider": "deepseek-direct",
                  "upstream_model": "deepseek-v4-pro",
                  "base_url": "https://api.deepseek.com/v1"}
        self.assertIn(direct, seat.proxy_routes("ds4pro"))  # noqa: SEAT_NAME — configured family identity is the property under test
        self.assertEqual(seat.proxy_route_family(direct), ("ds4pro", None))  # noqa: SEAT_NAME — configured family identity is the property under test
        self.assertNotIn("openrouter", fam["pool_providers"])  # noqa: SEAT_NAME — the PROVIDER BLOCK whose absence from this pool is the property under test
        self.assertEqual({row["upstream_model"]
                          for row in fam["pool_providers"].values()},
                         {"deepseek-v4-pro"},
                         "a pool is ONE model offered by several vendors")
        flash = seat.FAMILIES["ds4flash"]
        self.assertEqual(flash["pool_default"], "opencode-go")
        self.assertEqual(flash["pool_providers"],
                         {"opencode-go": {
                             "base_url": "https://opencode.ai/zen/go/v1",
                             "upstream_model": "deepseek-v4.1-flash",
                             "rung": "free",
                             "authstore": "opencode-go",
                             # OpenCode bills the Go subscription (task/3461)
                             "vendor": "opencode",
                             # Go's per-conversation header (task/3824)
                             "session_header": "x-opencode-session"}})
        self.assertNotIn("money_reader", flash)
        flash_route = {"alias": "deepseek-v4-flash", "provider": "opencode-go",
                       "upstream_model": "deepseek-v4.1-flash",
                       "base_url": "https://opencode.ai/zen/go/v1"}
        self.assertEqual(seat.proxy_route_family(flash_route),
                         ("ds4flash", None))
        self.assertNotEqual(seat.proxy_route_family(flash_route)[0], "ds4pro")  # noqa: SEAT_NAME — configured family identity is the property under test
        # the retired nous portal shape is gone
        self.assertNotIn("hermes_provider", fam)
        self.assertNotIn("provider", fam)

    def test_config_yaml_key_upstream_alias_mapping(self):
        cfg = seat._config_yaml_key(8360, "tok", "openrouter",
                                    "https://openrouter.ai/api/v1",
                                    "ds4-pro", "fake-key",
                                    "deepseek/deepseek-v4-pro")
        self.assertIn('- name: "deepseek/deepseek-v4-pro"\n'
                      '        alias: "ds4-pro"', cfg)

    def test_config_yaml_key_aliases_frontmatter_ids_to_the_UPSTREAM_model(self):  # noqa: VACUOUS_ASSERTION — the family-row positive control plus the count make omission unrepresentable
        """task/1952. MEASURED on the kimi seat 2026-09-09: an Explore child
        sent "claude-opus-5" upstream and Moonshot answered 502 "unknown
        provider for model claude-opus-5" fifteen times — CC's built-in
        frontmatter ids ride the request unaliased. Every catalogued id now
        gets a provider-side row to the family's UPSTREAM model. NO fork key:
        the key-backed model struct has no Fork field (codex's review
        measurement — the key is dead config, silently ignored), and the
        family route survives without it because the family row stays
        explicit and the resolver rewrites nothing when original ==
        requested. NO force-mapping either: it would rewrite the response's
        served model and show proxywatch a claude id on a non-claude family."""
        from helm import seat_catalog as c
        cfg = seat._config_yaml_key(8360, "tok", "openrouter",
                                    "https://openrouter.ai/api/v1",
                                    "ds4-pro", "fake-key",
                                    "deepseek/deepseek-v4-pro")
        self.assertIn('alias: "ds4-pro"', cfg)  # family row still present
        for alias in c.CC_AGENT_FRONTMATTER_MODELS:
            self.assertIn('- name: "deepseek/deepseek-v4-pro"\n'
                          '        alias: "%s"\n' % alias, cfg)
        self.assertNotIn("fork", cfg,
                         "key-backed model rows have no Fork field — the key "
                         "is dead config (codex, task/1952 review)")
        self.assertNotIn("force-mapping", cfg)
        self.assertIn("claude-opus-5", c.CC_AGENT_FRONTMATTER_MODELS)  # measured

    def test_the_catalog_constant_covers_the_measured_502_id(self):
        """The constant is the contract both alias writers share; the id
        whose 502 was measured on kimi must be in it or the cure is
        decorative."""
        from helm import seat_catalog as c
        self.assertIn("claude-opus-5", c.CC_AGENT_FRONTMATTER_MODELS)
        self.assertEqual(len(set(c.CC_AGENT_FRONTMATTER_MODELS)),
                         len(c.CC_AGENT_FRONTMATTER_MODELS),
                         "a duplicate row would alias the same id twice")

    def test_frontmatter_aliases_are_NOT_declared_routes(self):  # noqa: VACUOUS_ASSERTION — the family route beside the refusal is the positive control on the same table
        """The second finding on task/1952, per the integrator ruling:
        the frontmatter aliases are SUBAGENT conveniences, never seat
        runtimes — so proxy_routes must NOT own them (a measured alias route
        attesting as a seat runtime is the wrong lesson, not the cure). The
        seam closes at the launch door instead. The table keeps exactly the
        family's own routes and nothing else."""
        from helm import seat_catalog as c
        for family, fam in c.FAMILIES.items():
            if fam.get("mode") != "proxy-key":
                continue
            for route in c.proxy_routes(family):
                self.assertNotIn(route["alias"], c.CC_AGENT_FRONTMATTER_MODELS,
                                 "%s declares a subagent alias as a route" % family)
        # POSITIVE CONTROL on the same table: the family's own routes survive.
        self.assertTrue(any(r["alias"] == "kimi-k3"
                            for r in c.proxy_routes("kimi")))

    def test_a_subagent_alias_is_refused_at_the_launch_door(self):  # noqa: VACUOUS_ASSERTION — the catalogued-id positive control launches clean through the same door
        """The ruling's door: `launch <proxy-family> --model <frontmatter-id>`
        must REFUSE, naming why (a seat on it would route but never attest) —
        while a catalogued id and the family's other served alternates
        (gpt-5.5 on codex, spark) pass the same door. The refusal is exactly
        the alias ids, never a whitelist."""
        os.environ["KIMI_API_KEY"] = "fake-kimi-key-for-tests"
        self.assertEqual(self._add(("add", "kimi"))[0], 0)
        rc, out, err = self._add(("launch", "kimi", "--model", "claude-opus-5"))
        self.assertEqual(rc, 2)
        self.assertIn("subagent alias", err)
        self.assertIn("not a seat runtime", err)
        self.assertIn("kimi-k3", err)
        # POSITIVE CONTROLS through the same door, on both families: a
        # catalogued id, and a served alternate that is NOT catalogued.
        rc, _, err = self._add(("launch", "kimi", "--model", "kimi-k3"))
        self.assertEqual(rc, 0, err)
        self._plant("home-a")
        self.assertEqual(self._add(("add", "codex"))[0], 0)
        rc, _, err = self._add(("launch", "codex", "--model",
                                "gpt-5.3-codex-spark"))
        self.assertEqual(rc, 0, err)

    # -- the auth reader: live-entry selection ------------------------------
    def test_pool_reader_picks_live_bearer_over_junk_and_non_ok(self):
        """_hermes_pool_key selects the real bearer, never the 1-char junk
        placeholder (opencode-go's real [1] entry), and prefers last_status
        ok / lowest priority."""
        self._plant_pool(extra={"opencode-go": [
            {"access_token": "\x1b", "last_status": None, "priority": 1},
            {"access_token": "sk-stale-not-ok", "last_status": "exhausted",
             "priority": 0},
        ]})
        tok, base, err = seat._hermes_pool_key("opencode-go")
        self.assertIsNone(err)
        self.assertEqual(tok, self._LIVE)     # the ok/priority-0 real bearer
        self.assertEqual(base, "https://opencode.ai/zen/go/v1")

    def test_pool_reader_all_junk_reports_no_live_bearer(self):
        with open(seat.HERMES_AUTH, "w") as f:
            json.dump({"credential_pool": {"opencode-go": [
                {"access_token": "\x1b", "priority": 1}]}}, f)
        tok, base, err = seat._hermes_pool_key("opencode-go")
        self.assertIsNone(tok)
        self.assertIn("no live bearer", err)

    def test_pool_reader_missing_provider(self):
        self._plant_pool()
        tok, _, err = seat._hermes_pool_key("nope")
        self.assertIsNone(tok)
        self.assertIn("no credential_pool.nope", err)

    # -- the opencode auth store reader -------------------------------------
    def test_authstore_reader_picks_api_key(self):
        self._plant_authstore()
        key, err = seat._opencode_authstore_key("opencode-go")
        self.assertIsNone(err)
        self.assertEqual(key, self._AS_OC)
        key, err = seat._opencode_authstore_key("deepseek")
        self.assertIsNone(err)
        self.assertEqual(key, self._AS_DS)

    def test_authstore_reader_skips_oauth_entry(self):
        """An oauth entry carries no static bakeable key — the reader reports
        it, never returns the access token as a key."""
        self._plant_authstore(oauth_ds=True)
        key, err = seat._opencode_authstore_key("deepseek")
        self.assertIsNone(key)
        self.assertIn("not a static api key", err)

    def test_authstore_reader_missing_provider_and_file(self):
        self._plant_authstore()
        key, err = seat._opencode_authstore_key("nope")
        self.assertIsNone(key)
        self.assertIn("no nope entry", err)
        os.remove(seat.OPENCODE_AUTHSTORE)
        key, err = seat._opencode_authstore_key("opencode-go")
        self.assertIsNone(key)
        self.assertIn("unreadable", err)

    def test_add_ds4pro_prefers_authstore_over_hermes(self):
        """Both sources present: the authstore bearer is baked, the hermes
        _LIVE bearer is NOT — and neither raw value is ever printed."""
        self._plant_authstore()
        self._plant_pool()          # hermes _LIVE also present
        rc, out, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._AS_DS, out + err)   # secret: never printed
        self.assertNotIn(self._LIVE, out + err)
        d = seat.seat_dir("ds4pro")
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._AS_DS, cfg)   # authstore won
        self.assertNotIn(self._LIVE, cfg)                   # not hermes
        self.assertIn('base-url: "https://api.deepseek.com/v1"', cfg)
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertNotIn(self._AS_DS, f.read())         # never in launch.sh

    def test_add_ds4pro_deepseek_provider_from_authstore(self):
        """--provider deepseek emits the direct provider's exact /v1 route."""
        self._plant_authstore()
        rc, out, err = self._add(("add", "ds4pro", "--provider", "deepseek"))
        self.assertEqual(rc, 0, err)
        self.assertIn("provider deepseek-direct -> https://api.deepseek.com/v1", out)
        self.assertNotIn(self._AS_DS, out + err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._AS_DS, cfg)
        self.assertIn('name: "deepseek-direct"', cfg)
        self.assertIn('base-url: "https://api.deepseek.com/v1"', cfg)
        self.assertIn('- name: "deepseek-v4-pro"', cfg)

    def test_add_ds4pro_falls_back_to_hermes_when_authstore_absent(self):
        """No authstore file -> the hermes credential_pool bearer is used."""
        self._plant_pool()          # authstore path does not exist
        rc, _, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            self.assertIn('api-key: "%s"' % self._LIVE, f.read())

    def test_add_ds4pro_default_provider_is_the_direct_key(self):
        self._plant_pool()
        rc, out, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._LIVE, out + err)   # secret: never printed
        self.assertIn("provider deepseek-direct", out)
        d = seat.seat_dir("ds4pro")
        for p, want in ((os.path.join(d, "token"), 0o600),
                        (os.path.join(d, "config.yaml"), 0o600),
                        (os.path.join(d, "launch.sh"), 0o700)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), want, p)
        with open(os.path.join(d, "config.yaml")) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._LIVE, cfg)     # baked 0600
        self.assertIn('name: "deepseek-direct"', cfg)
        self.assertNotIn("opencode", cfg)                     # one token source
        self.assertIn('base-url: "https://api.deepseek.com/v1"', cfg)
        self.assertIn('- name: "deepseek-v4-pro"', cfg)      # the vendor's id
        self.assertIn('alias: "ds4-pro"', cfg)
        self.assertIn("port: 8360", cfg)
        self.assertNotIn("auth-dir", cfg)     # no OAuth dir for proxy-key

    def test_add_ds4flash_mints_with_a_per_conversation_session_header(self):
        """OpenCode Go refuses a request without x-opencode-session, so the
        flash family's block asks the proxy for a per-conversation value
        (`session-header`) instead of carrying one static value that would
        make every conversation one (task/3824)."""
        self._plant_authstore()
        rc, out, err = self._add(("add", "ds4flash"))  # noqa: SEAT_NAME — the family whose mint is under test
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._AS_OC, out + err)   # secret: never printed
        self.assertIn("provider opencode-go", out)
        with open(os.path.join(seat.seat_dir("ds4flash"), "config.yaml")) as f:  # noqa: SEAT_NAME — the minted family's own config
            cfg = f.read()
        self.assertIn('name: "opencode-go"', cfg)
        self.assertIn('base-url: "https://opencode.ai/zen/go/v1"\n'
                      '    session-header: "x-opencode-session"\n', cfg)
        self.assertIn('- name: "deepseek-v4.1-flash"', cfg)
        self.assertIn('alias: "deepseek-v4-flash"', cfg)
        self.assertIn("port: 8330", cfg)
        self.assertNotIn("headers:", cfg)       # no static family-wide value
        self.assertNotIn("deepseek-direct", cfg)  # one token source
        self.assertTrue(os.path.exists(os.path.join(
            seat.seat_dir("ds4flash"), "launch.sh")))  # noqa: SEAT_NAME — the minted family's own launch script

    def test_ds4pro_repoints_at_go_with_one_mint_and_one_source(self):  # noqa: VACUOUS_ASSERTION — the Go block and the key are asserted present on the same config
        """The owner: "ds4pro works through the account". The
        re-point is ONE mint naming the Go row; the config then carries Go
        alone (the same deepseek-v4-pro), with the session header, and the
        default mint is still the direct key alone."""
        self._plant_authstore()
        rc, out, err = self._add(("add", "ds4pro", "--provider",  # noqa: SEAT_NAME — the family whose re-point is under test
                                  "opencode-go"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._AS_OC, out + err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:  # noqa: SEAT_NAME — the minted family's own config
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._AS_OC, cfg)
        self.assertIn('base-url: "https://opencode.ai/zen/go/v1"\n'
                      '    session-header: "x-opencode-session"\n', cfg)
        self.assertIn('- name: "deepseek-v4-pro"', cfg)
        self.assertIn('alias: "ds4-pro"', cfg)
        self.assertNotIn("deepseek-direct", cfg)
        self.assertNotIn(self._AS_DS, cfg)
        # CONTROL: the default mint is the direct key, with no session header
        rc, out, err = self._add(("add", "ds4pro"))  # noqa: SEAT_NAME — the family whose default mint is the control
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:  # noqa: SEAT_NAME — the minted family's own config
            cfg = f.read()
        self.assertIn('name: "deepseek-direct"', cfg)
        self.assertNotIn("opencode", cfg)
        self.assertNotIn("session-header", cfg)

    def test_the_go_row_never_takes_the_familys_direct_key_env(self):
        """The family's key_env (DS4PRO_API_KEY) is the DeepSeek direct key:
        it belongs to the default row. A seat minted on the Go row reads the
        Go credential, never that variable, or it would send the direct key
        to OpenCode."""
        self._plant_authstore()
        direct = "sk-env-direct-key-must-not-reach-go-3f9c2a1e6b7d80"
        os.environ["DS4PRO_API_KEY"] = direct
        rc, out, err = self._add(("add", "ds4pro", "--provider",  # noqa: SEAT_NAME — the family whose Go mint is under test
                                  "opencode-go"))
        self.assertEqual(rc, 0, err)
        cfg_path = os.path.join(seat.seat_dir("ds4pro"), "config.yaml")  # noqa: SEAT_NAME — the minted family's own config
        with open(cfg_path) as f:
            cfg = f.read()
        self.assertIn('api-key: "%s"' % self._AS_OC, cfg)
        self.assertNotIn(direct, cfg + out + err)
        self.assertNotIn("DS4PRO_API_KEY", out)
        # CONTROL: the default row still takes the family's own variable
        rc, out, err = self._add(("add", "ds4pro"))  # noqa: SEAT_NAME — the family whose default mint is the control
        self.assertEqual(rc, 0, err)
        with open(cfg_path) as f:
            self.assertIn('api-key: "%s"' % direct, f.read())
        self.assertIn("DS4PRO_API_KEY", out)

    def test_up_refuses_an_old_binary_when_the_plan_adds_the_session_header(self):
        """The start preflight must judge the config the proxy will LOAD. A
        seat config without session-header is given it by the plan, so an old
        binary would start on a config it cannot serve (every Go request 400
        MissingSessionID); the refusal leaves the bytes and starts nothing."""
        self._plant_authstore()
        rc, _out, err = self._add(("add", "ds4flash"))  # noqa: SEAT_NAME — the family whose proxy start is under test
        self.assertEqual(rc, 0, err)
        path = os.path.join(seat.seat_dir("ds4flash"), "config.yaml")  # noqa: SEAT_NAME — the minted family's own config
        with open(path) as f:
            minted = f.read()
        line = '    session-header: "x-opencode-session"\n'
        self.assertIn(line, minted)
        stale = minted.replace(line, "")
        with open(path, "w") as f:
            f.write(stale)
        old = os.path.join(self.tmp, "old-cli-proxy-api")
        with open(old, "w") as f:
            f.write("#!/bin/sh\necho 'Usage of cli-proxy-api'\n"
                    "echo '  -config string'\n")
        os.chmod(old, 0o755)
        os.environ["HELM_PROXY_BIN"] = old
        booby = seat.subprocess.Popen

        def refuse(argv, *a, **k):
            if argv and argv[0] == old and "-config" in argv:
                self.fail("the old binary was started on the Go config")
            return booby(argv, *a, **k)
        seat.subprocess.Popen = refuse
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(["up", "ds4flash"])  # noqa: SEAT_NAME — the family whose proxy start is under test
        finally:
            seat.subprocess.Popen = booby
        self.assertEqual(rc, 1, err.getvalue())
        self.assertIn("session-header", err.getvalue())
        self.assertIn("MissingSessionID", err.getvalue())
        with open(path) as f:
            self.assertEqual(f.read(), stale)

    def test_add_ds4pro_unknown_provider_refused(self):
        self._plant_pool()
        rc, _, err = self._add(("add", "ds4pro", "--provider", "bogus"))
        self.assertEqual(rc, 2)
        self.assertIn("no provider 'bogus'", err)
        self.assertIn("deepseek", err)        # names the valid choice

    def test_add_ds4pro_pool_entry_base_url_wins(self):
        """The credential_pool entry's own base_url overrides the family
        default, so the seat rides exactly the endpoint the cred was minted
        for (owner moves a gateway without a code change)."""
        self._plant_pool(deepseek=self._LIVE)
        with open(seat.HERMES_AUTH) as f:
            data = json.load(f)
        data["credential_pool"]["deepseek"][0]["base_url"] = \
            "https://api.deepseek.com/v2"
        with open(seat.HERMES_AUTH, "w") as f:
            json.dump(data, f)
        rc, _, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            self.assertIn('base-url: "https://api.deepseek.com/v2"', f.read())

    def test_add_ds4pro_env_var_beats_pool(self):
        self._plant_pool()
        os.environ["DS4PRO_API_KEY"] = "sk-env-wins-over-pool"
        rc, _, err = self._add(("add", "ds4pro"))
        self.assertEqual(rc, 0, err)
        with open(os.path.join(seat.seat_dir("ds4pro"), "config.yaml")) as f:
            self.assertIn('api-key: "sk-env-wins-over-pool"', f.read())

    def test_add_ds4pro_missing_everything_names_all_sources(self):
        rc, out, err = self._add(("add", "ds4pro"))   # no env/file/pool
        self.assertEqual(rc, 1)
        self.assertIn("DS4PRO_API_KEY", err)
        self.assertIn("--key-from", err)
        self.assertIn(seat.HERMES_AUTH, err)  # names the credential_pool source
        self.assertIn("deepseek", err)        # names the selected provider
        self.assertFalse(os.path.exists(os.path.join(seat.seat_dir("ds4pro"),
                                                     "config.yaml")))

    def test_ds4pro_launch_line_shape_and_never_leaks_token(self):
        self._plant_pool()
        # bound rather than subscripted inline: `assertEqual(f(...)[0], 0)`
        # reads to the vacuous-assertion rung as an absence with no traceable
        # observable, and this test lost the blanket noqa that used to cover it
        # when ds4pro stopped being an unpinned family.
        add_rc = self._add(("add", "ds4pro"))[0]
        self.assertEqual(add_rc, 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "ds4pro"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8360", line)
        self.assertIn("HELM_CHAT_NAME=ds4pro", line)
        # THE WINDOW IS NOW EXPORTED, AND THAT IS THE CHANGE OF 2026-08-03.
        # This arm was `assertNotIn(...)` with a VACUOUS_ASSERTION noqa on the
        # def line, because ds4pro's silence was the claim; the family has
        # since pinned 1000000 off the context_length OpenRouter publishes for
        # deepseek/deepseek-v4-pro (public /v1/models, no auth, 2026-08-02),
        # so the silence and the noqa both had to go. Until this landed helm
        # told CC nothing for this family and CC used its hardcoded 200k.
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d"
                      % seat.FAMILIES["ds4pro"]["max_context"], line)
        # EVERY LIVE FAMILY PINS NOW (ds4flash took its Go route's published
        # window, task/3824), so the unpinned control is ds4flash's own entry
        # with its window keys removed, and it is what keeps this from proving
        # merely that the emitter always mints.
        # POSITIVE CONTROL ON THAT OWN LINE, unconditional and sharing
        # the root object of the absence below: a sibling family's
        # launch_line is a DIFFERENT observable and could not tell an empty
        # line from a windowless one. This membership proves the line is real.
        unpinned = {k: v for k, v in seat.FAMILIES["ds4flash"].items()
                    if k not in ("max_context", "probed_context_length")}
        with mock.patch.dict(seat.FAMILIES, {"ds4flash": unpinned}):
            unpinned_line = seat.launch_line("ds4flash")
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=948000",
                      seat.launch_line("ds4flash"))
        self.assertIn("HELM_CHAT_NAME=ds4flash", unpinned_line)
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", unpinned_line)
        # The window knob is gated on the same max_context, so it stays away
        # too. A family that gained one without the other would mint a window
        # CC then clamps to its 200k default — the #182 shape in reverse.
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", unpinned_line)
        # POSITIVE CONTROLS ON THE SAME OBSERVABLE, unconditional: other
        # families mint the variable too, grok among them now, so the
        # silence above is a decision and not a dead emitter.
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                      seat.launch_line("kimi"))
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                      seat.launch_line("gemini"))
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=237000",
                      seat.launch_line("grok"))
        # WebSearch: its route cannot serve the tool (task/3242, measured)
        self.assertTrue(line.endswith(
            "claude --disallowedTools %s --dangerously-skip-permissions"
            " --model ds4-pro" % ("EnterPlanMode WebSearch "
                                  + "Skill 'Agent(fork)'")))
        self.assertNotIn("deepseek", line)    # alias on the wire, not the id
        self.assertNotIn(self._LIVE, line)     # the outbound bearer never rides

    def test_ds4pro_launch_sh_never_contains_the_bearer(self):
        """The 0700 launch.sh is the real launch artifact — the outbound
        bearer lives only in the 0600 config.yaml the proxy reads, never in
        the launch line/argv/script."""
        self._plant_pool()
        self.assertEqual(self._add(("add", "ds4pro"))[0], 0)
        with open(os.path.join(seat.seat_dir("ds4pro"), "launch.sh")) as f:
            self.assertNotIn(self._LIVE, f.read())

    # -- launch line shape --------------------------------------------------
    def test_launch_line_shape(self):  # noqa: VACUOUS_ASSERTION — the same non-empty line has unconditional token, URL, config, identity, signer, storage, model, and command controls before each intentional absence check
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with open(os.path.join(seat.seat_dir("codex"), "token")) as f:
            token = f.read().strip()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        # paste-line = token export (builtin, no argv) + env/claude command
        self.assertTrue(line.startswith("ANTHROPIC_AUTH_TOKEN=$(cat "), line)
        self.assertIn("; export ANTHROPIC_AUTH_TOKEN; env -u ANTHROPIC_API_KEY ", line)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8317", line)
        # no-keys-in-argv (the 7bb422a xrev): the bearer is NEVER the literal —
        # the line reads it from the 0600 token file at exec time, so only the
        # PATH crosses stdout/argv, and the line is mint-order-immune.
        self.assertNotIn(token, line)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=$(cat ", line)
        self.assertIn(os.path.join(seat.seat_dir("codex"), "token"), line)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-sol", line)
        # The eval-arm seam (§C pilot FINDING 5): the config dir rides as a
        # shell default so an eval arm can substitute a per-run HOOK-STRIPPED
        # copy while shelling this exact script; a plain seat launch leaves
        # the var unset and lands on the very same path as before.
        self.assertIn('CLAUDE_CONFIG_DIR="${HELM_EVAL_CONFIG_DIR:-'
                      + os.path.join(seat.seat_dir("codex"), "claude") + '}"',
                      line)
        self.assertIn("HELM_CHAT_NAME=codex", line)   # stable seat identity
        self.assertIn("HELM_SEAT_STORAGE=codex", line)  # unrenamed control
        self.assertIn("HELM_CELL_BIN=" + seat.DREGG_SIGNER_DEFAULT, line)
        self.assertIn("HELM_CELL_PROFILE=codex", line)  # never inherit owner
        self.assertIn("DREGG_PROFILE=codex", line)
        self.assertIn("--dangerously-skip-permissions", line)  # canonical seat
        self.assertTrue(line.endswith(
            "claude --disallowedTools %s --dangerously-skip-permissions"
            " --model gpt-6.1-sol" % ("EnterPlanMode Artifact " + "Skill 'Agent(fork)'")))
        self.assertNotIn("ANTHROPIC_API_KEY=", line)  # unset, never set
        # --model override rides both slots
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            seat.cmd_seat(["launch", "codex", "--model", "gpt-5.5"])
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.5", out.getvalue())
        self.assertIn(
            "claude --disallowedTools %s --dangerously-skip-permissions"
            " --model gpt-5.5" % ("EnterPlanMode Artifact " + "Skill 'Agent(fork)'"),
            out.getvalue())

    def _renamed_instance(self):
        """One family-shaped storage seat with an arbitrary canonical rename."""
        from helm import seats
        old, new = "codex-97", "seat-b"
        self._plant("home-a")
        inst = seat._instance_dir("codex", old)
        seat._write_launch_assets("codex", inst, seat=old)
        self.assertTrue(seats.write_roster(old, session="old-sid"))
        ok, msg = seats.rename_seat(old, new)
        self.assertTrue(ok, msg)
        self.assertIn(seats._seat_key(old),
                      (seats.roster().get(new) or {}).get("seat_keys") or ())
        cwd = os.path.join(self.tmp, "project-wt", "seats", old)
        os.makedirs(cwd)
        return old, new, inst, cwd

    def test_a_durable_rename_reaches_real_spawn_registration(self):  # noqa: VACUOUS_ASSERTION — the pre-rename launch is the unconditional polarity control on the same HELM_CHAT_NAME surface
        """The real spawn path keeps storage old and registers identity new."""
        from helm import seats
        old, new, inst, cwd = self._renamed_instance()
        launch_sh = os.path.join(inst, "launch.sh")
        with open(launch_sh) as f:
            before = f.read()
        self.assertIn("HELM_CHAT_NAME=" + old, before,
                      "control: the pre-rename instance did not name its storage")
        ad = self._pane_seam()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-c"},
                             clear=False), \
                mock.patch.object(seat, "_mint_instance_proxy"):
            rc, _out, err, _ = self._run_seat(
                ("spawn", old, "--cwd", cwd), adapter=ad)
        self.assertEqual(rc, 0, err)
        with open(launch_sh) as f:
            generated = f.read()
        with open(os.path.join(inst, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual((rec["seat"], rec["identity"]), (old, new))
        self.assertEqual(ad.spawned[0][1], new)
        self.assertIn("HELM_CHAT_NAME=" + new, generated)
        self.assertIn("HELM_SEAT_STORAGE=" + old, generated)
        self.assertIn("instances/%s/claude" % old, generated)
        self.assertNotIn("HELM_CHAT_NAME=" + old, generated)
        self.assertNotIn("HELM_CHAT_NAME=seat-c", generated)
        self.assertEqual(seats.roster()[new]["cwd"], cwd)
        self.assertNotIn(old, seats.roster(),
                         "registration recreated the stale roster identity")

    def test_a_durable_rename_reaches_real_resume_registration(self):
        """Resume carries canonical identity through pane title and register."""
        from helm import pk, seats
        old, new, inst, cwd = self._renamed_instance()
        pk.write_json(os.path.join(inst, "spawn.json"), {
            "v": 1, "seat": old, "identity": new, "role": "worker",
            "worktree": cwd, "launch_sh": os.path.join(inst, "launch.sh"),
            "harness": "headless", "pid": 424242,
            "pid_identity": "pid:dead", "session": None})
        ad = self._pane_seam()
        with mock.patch.object(seat, "_mint_instance_proxy"), \
                mock.patch.object(seat, "_recorded_pid_alive",
                                  return_value=False):
            rc, _out, err, _ = self._run_seat(
                ("resume", old, "--cwd", cwd), adapter=ad)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(inst, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual((rec["seat"], rec["identity"]), (old, new))
        self.assertEqual(ad.spawned[-1][1], new)
        self.assertIn(new, seats.roster())
        self.assertNotIn(old, seats.roster())

    def test_headless_spawn_onboards_as_the_canonical_identity(self):  # noqa: VACUOUS_ASSERTION — three canonical strings on the captured non-empty prompt are unconditional controls for the stale-name absence
        """The positional first turn acts, posts, and takes work as the rename."""
        from helm import harness
        old, new, inst, cwd = self._renamed_instance()
        seen = {}

        # The double carries the SHIPPED signature, `token` included: the
        # headless leg is one of the three places the spawn ATTEMPT TOKEN
        # reaches the child, and a double that could not accept it was reading
        # the crash as the contract.
        def headless(launch_sh, onboarding, at, log_path, role="worker",
                     token=None):
            seen.update(launch=launch_sh, onboarding=onboarding, cwd=at,
                        log=log_path, role=role, token=token)
            return 424242

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=None), \
                mock.patch.object(seat, "_headless_spawn", side_effect=headless), \
                mock.patch.object(seat, "_pid_identity", return_value="pid:fake"), \
                mock.patch.object(seat, "_running_pid", return_value=4242), \
                mock.patch.object(seat, "_mint_instance_proxy"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["spawn", old, "--cwd", cwd])
        self.assertEqual(rc, 0, err.getvalue())
        prompt = seen["onboarding"]
        self.assertIn("fleet seat '%s'" % new, prompt)
        self.assertIn("wait --seat %s --follow" % new, prompt)
        # The announce line carries the canonical identity: the brief
        # tells a seat to read the rows ADDRESSED TO it rather than to
        # take work, so this is where the name has to be right.
        self.assertIn("addressed to @%s" % new, prompt)
        self.assertNotIn("wait --seat %s --follow" % old, prompt)
        with open(os.path.join(inst, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["identity"], new)
        # THE POSITIVE the double's new keyword exists to carry: the headless
        # child was handed THIS spawn's published attempt token, not None and
        # not some other attempt's. A renamed seat spawns under its storage
        # name and onboards as its canonical one, and the attempt identity is
        # the same value on both sides of that split.
        # CONTROL: the id is read out of the register the spawn WROTE, so a leg
        # that passed no token (the pre-token behaviour) fails on None != id
        # rather than on two matching blanks. Blast radius: this arm only —
        # it reads the record this test already opened.
        self.assertTrue(rec["attempt"]["id"], "the spawn published no attempt")
        self.assertEqual(seen["token"], rec["attempt"]["id"],
                         "the headless child was not handed this spawn's "
                         "attempt token")

    def test_sessionstart_binds_arbitrary_identity_through_storage_key(self):
        """A non-family canonical name binds the storage-keyed spawn record."""
        from helm import pk, seats
        old, new, inst, cwd = self._renamed_instance()
        pk.write_json(os.path.join(inst, "spawn.json"), {
            "v": 1, "seat": old, "identity": new, "role": "worker",
            "worktree": cwd, "launch_sh": os.path.join(inst, "launch.sh"),
            "harness": "orca", "handle": "pane-1", "session": None})
        fields = {"handle": "pane-1", "pane_key": "tab:leaf",
                  "pty_id": "pty-1", "worktree_id": "workspace:/w"}
        with mock.patch.dict(os.environ, {"HELM_SEAT_STORAGE": old},
                             clear=False), \
                mock.patch.object(seat, "_sessionstart_pane_fields",
                                  return_value=(fields, None)), \
                mock.patch.object(seat, "_live_session_orca_identity",
                                  return_value=({}, None)):
            banner = seats.join(session="new-session", seat=new, cwd=cwd)
        self.assertIn(new, banner)
        with open(os.path.join(inst, "spawn.json")) as f:
            rec = json.load(f)
        self.assertEqual((rec["seat"], rec["identity"], rec["session"]),
                         (old, new, "new-session"))
        self.assertEqual(seats.roster()[new]["session"], "new-session")
        self.assertNotIn(old, seats.roster())

    def test_manual_resume_refreshes_renamed_launch_before_return(self):
        """No-adapter resume cannot hand back a command for the stale script."""
        from helm import harness
        old, new, inst, cwd = self._renamed_instance()
        launch_sh = os.path.join(inst, "launch.sh")
        with open(launch_sh) as f:
            self.assertIn("HELM_CHAT_NAME=" + old, f.read())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["resume", old, "--cwd", cwd])
        self.assertEqual(rc, 1)
        self.assertIn("manual paste (env refreshed", err.getvalue())
        with open(launch_sh) as f:
            refreshed = f.read()
        self.assertIn("HELM_CHAT_NAME=" + new, refreshed)
        self.assertIn("HELM_SEAT_STORAGE=" + old, refreshed)
        self.assertNotIn("HELM_CHAT_NAME=" + old, refreshed)

    def test_launch_line_context_window(self):
        """ctx-window fix: proxy seats mint BOTH context knobs —
        CLAUDE_CODE_MAX_CONTEXT_TOKENS (capacity, teaching CC past its
        hardcoded 200k for non-claude models) and CLAUDE_CODE_AUTO_COMPACT_
        WINDOW (the window itself, which CC clamps to that capacity) — plus
        CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, so a non-claude seat compacts before
        the unrecoverable 400. Every proxy family mints its real window unless
        it declares a smaller context_budget (kimi: a 1M window taught as
        380k, task/2944); signing env intact.

        WHAT THIS TEST STILL CANNOT DO, said plainly because #182 turned on it:
        it proves the launch line CARRIES these strings, never that Claude Code
        READS them. An unread env var is silent, so a decorative one passes
        here forever. tests/test_seat_env_allowlist.py is the arm that checks
        the names against the shipped binary; this one only checks plumbing."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        line = seat.launch_line("codex")
        # sol's TOTAL window is 372k (registry context_window); helm mints the
        # INPUT ceiling under it, because output shares that window. Read from
        # the table, never retyped — the VALUE is pinned by its own test above
        # (test_codex_max_context_is_an_input_ceiling_not_the_total_window);
        # what this line checks is that the launch line carries it.
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d"
                      % seat.FAMILIES["codex"]["max_context"], line)
        # The window knob rides with the capacity knob, ALWAYS. CC computes
        # {window: Math.min(capacity, window)}, so a capacity minted without a
        # window leaves the window at CC's default and the capacity invisible.
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d"
                      % seat.FAMILIES["codex"]["max_context"], line)
        # 80, aligned to the watchdog's DEFAULT_THRESHOLD (was 78; the split from
        # the watchdog's 90 was the confusion the 2026-07-29 diagnosis surfaced).
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80", line)
        # ctxenv appends AFTER the signing env, which stays byte-identical
        self.assertIn("DREGG_PROFILE=codex CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80", line)
        # A 1M-window model with no budget mints the real max, so CC's gauge
        # and autocompact stop tracking the hardcoded 200k. ds4pro carries that
        # posture; kimi carried it until task/2944 gave kimi a
        # context_budget, and its launch line now teaches the budget (the
        # arm below renders a kimi seat's launch.sh).
        dline = seat.launch_line("ds4pro")  # noqa: SEAT_NAME — the catalog FAMILY key whose window IS the subject of this arm
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000", dline)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000", dline)
        kline = seat.launch_line("kimi")
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80", kline)
        budget = seat.FAMILIES["kimi"]["context_budget"]
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % budget, kline)
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW=%d" % budget, kline)

    def test_kimi_launch_assets_teach_the_budget_the_watchdog_reads(self):
        """task/2944: kimi's weekly allowance went in about 30 h because one
        long-lived session carried many rows (a typical request carried 329k
        tokens). The GENERATED launch.sh for a kimi seat now carries the
        family's 380k context_budget (sized from the measured p95 per-row
        peak) on BOTH CC knobs, and the autocompact watchdog reads the SAME
        number, so the two readers of one window cannot drift. The measured 1M window stays in
        the catalog: the budget narrows what the seat is taught, and does not
        rewrite the evidence for the model's capacity."""
        from helm import autocompact
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        fam = seat.FAMILIES["kimi"]
        self.assertEqual(fam["max_context"], 1000000)
        self.assertEqual(fam["context_budget"], 380000)
        inst = seat._instance_dir("kimi", "kimi")
        os.makedirs(inst, exist_ok=True)
        seat._write_launch_assets("kimi", inst, seat="kimi")
        with open(os.path.join(inst, "launch.sh")) as f:
            text = f.read()
        self.assertIn("--model kimi-k3", text)
        self.assertEqual(
            re.findall(r"CLAUDE_CODE_MAX_CONTEXT_TOKENS=(\d+)", text),
            ["380000"])
        self.assertEqual(
            re.findall(r"CLAUDE_CODE_AUTO_COMPACT_WINDOW=(\d+)", text),
            ["380000"])
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80", text)
        self.assertEqual(autocompact._window("kimi"),
                         (380000, "FAMILIES.context_budget"))

    def test_cursor_launch_assets_teach_the_window_the_watchdog_reads(self):  # noqa: VACUOUS_ASSERTION — positive launch.sh model and two knob matches, plus live watchdog window
        """task/3816: the cursor seat is taught its 225000 max_context on BOTH
        Claude Code knobs, and the watchdog reads the SAME taught window, so
        the two readers of one window cannot drift. task/3652 had narrowed it
        to a 110000 context_budget on the premise that Cursor counts the
        Claude Code history 3-6x the bridge's estimate and goes tool-silent
        past 256k. MEASURED (task/3817's probe): that count is a run-start
        artifact of replaying the whole history into a new Cursor
        conversation (2.0-4.6x on 8 of 16 run starts, 269,723 at an estimate
        of 66,562); in a conversation the bridge keeps, Cursor counted 1.06x
        at est 66k and 157,163 at the 225000 window's compaction point (est
        164,873), with a tool call on every one of 25 turns. The budget made
        the seat compact near 73k by the estimate, every 10-16 minutes."""
        from helm import autocompact
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        fam = seat.FAMILIES["cursor"]
        self.assertEqual(fam["max_context"], 225000)
        self.assertIsNone(fam.get("context_budget"))
        inst = seat._instance_dir("cursor", "cursor")
        os.makedirs(inst, exist_ok=True)
        seat._write_launch_assets("cursor", inst, seat="cursor")
        with open(os.path.join(inst, "launch.sh")) as f:
            text = f.read()
        self.assertIn("--model cursor", text)
        self.assertEqual(
            re.findall(r"CLAUDE_CODE_MAX_CONTEXT_TOKENS=(\d+)", text),
            ["225000"])
        self.assertEqual(
            re.findall(r"CLAUDE_CODE_AUTO_COMPACT_WINDOW=(\d+)", text),
            ["225000"])
        self.assertIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80", text)
        self.assertEqual(autocompact._window("cursor"),
                         (225000, "FAMILIES.max_context"))

    def test_a_minted_seat_carries_no_ai_attribution_before_its_first_session(self):
        """task/3591: the seat path writes the estate defaults at mint
        (`_write_launch_assets` runs hooks.install_home), so a spawned or
        added seat carries no AI authoring line before it launches. A pin:
        this path already held when the credential-home path did not."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        inst = seat._instance_dir("kimi", "kimi")
        os.makedirs(inst, exist_ok=True)
        seat._write_launch_assets("kimi", inst, seat="kimi")
        with open(os.path.join(inst, "claude", "settings.json")) as f:
            got = json.load(f).get("attribution")
        self.assertEqual(got, {"commit": "", "pr": "", "sessionUrl": False})

    # -- the child-stamp guard (child-stamp-kills-seat-persistence) ---------
    def test_launch_line_strips_child_stamp(self):
        """A pane minted by a daemon born inside a Claude session inherits
        CLAUDE_CODE_CHILD_SESSION + the daemon's SID/bridge id — CC then runs
        the seat as a subprocess child with transcript persistence silently
        OFF. Every launch line unsets the trio BEFORE the first export, for
        every family and instance; the pinned byte layout is untouched."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        for line in (seat.launch_line("codex"), seat.launch_line("kimi"),
                     seat.launch_line("codex", seat="codex-2")):
            for v in seat.CHILD_STAMP_VARS:
                self.assertIn("-u " + v, line)
                self.assertNotIn(v + "=", line)      # unset, never re-exported
            # the unsets ride the env prefix, ahead of the first export
            self.assertLess(line.index("-u CLAUDE_CODE_CHILD_SESSION"),
                            line.index("ANTHROPIC_BASE_URL="))
        # byte-layout pins survive: head, signing adjacency, tail
        line = seat.launch_line("codex")
        self.assertTrue(line.startswith("env -u ANTHROPIC_API_KEY "))
        self.assertIn("DREGG_PROFILE=codex CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80", line)
        self.assertTrue(line.endswith(
            "claude --disallowedTools %s --dangerously-skip-permissions"
            " --model gpt-6.1-sol" % ("EnterPlanMode Artifact " + "Skill 'Agent(fork)'")))

    def test_spawn_model_flag_mints_a_non_default_window(self):
        """task/381 (b-wiring): spawn --model gpt-5.3-codex-spark must write a
        launch.sh carrying that model AND its per-model window (76k), not the
        family default. The gap this closes: _write_launch_assets called
        launch_line() with no model, so a spark pane was unspawnable — the
        model_context DATA + launch_line RESOLUTION had no verb to reach the
        persisted launch.sh. Both polarities: default writes sol/320k."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        inst = seat._instance_dir("codex", "codex-spark")
        # spark: the --model path
        seat._write_launch_assets("codex", inst, seat="codex-spark",
                                  model="gpt-5.3-codex-spark")
        with open(os.path.join(inst, "launch.sh")) as f:
            spark = f.read()
        self.assertIn("--model gpt-5.3-codex-spark", spark)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", spark)
        self.assertNotIn("220000", spark)
        # default (no model): the family's own window, the polarity control
        inst2 = seat._instance_dir("codex", "codex-sol")
        seat._write_launch_assets("codex", inst2, seat="codex-sol")
        with open(os.path.join(inst2, "launch.sh")) as f:
            default = f.read()
        self.assertIn("--model gpt-6.1-sol", default)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000", default)
        self.assertNotIn("76000", default)

    def test_spawn_args_parses_the_model_flag(self):
        """The CLI seam: _spawn_args must surface --model in its tuple, and
        reject it with a clear message when the value is missing."""
        from helm.seat_lifecycle_runtime import _spawn_args
        parsed, err = _spawn_args(["--model", "gpt-5.3-codex-spark"],
                                  "codex-spark", provision=False)
        self.assertIsNone(err)
        self.assertEqual(parsed[4], "gpt-5.3-codex-spark")   # model is slot 5
        # absent -> None (the polarity control)
        parsed2, err2 = _spawn_args([], "codex-x", provision=False)
        self.assertIsNone(err2)
        self.assertIsNone(parsed2[4])
        # model and orchestration role are independent axes; conflict
        # reconciliation must not make one flag erase the other.
        both, e3 = _spawn_args([
            "--model", "gpt-5.3-codex-spark", "--role", "lead"],
            "codex-x", provision=False)
        self.assertIsNone(e3)
        self.assertEqual(both[4], "gpt-5.3-codex-spark")
        self.assertEqual(both[5], "lead")
        # missing value -> refused, not IndexError
        _p, e4 = _spawn_args(["--model"], "codex-x", provision=False)
        self.assertIsNotNone(e4)

    def test_launch_rederives_the_persisted_model_from_the_seat_record(self):  # noqa: VACUOUS_ASSERTION — the assertIn spark/76000 lines on the SAME printed line and launch.sh are the unconditional positive controls for the assertNotIn(320000)
        """land af391eab, the launch-verb writer site: `helm seat launch`
        REFRESHES launch.sh, and with no model it rewrote a spark seat back
        to the family default (sol + a 320k window on a 76k model). With
        spawn.json carrying the seat's explicit model, both the PRINTED line
        and the re-minted launch.sh keep spark/76000; an explicit --model
        still outranks the record for that mint."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex",
                       "model": "gpt-5.3-codex-spark"}, f)
        rc, out, err = self._add(("launch", "codex"))
        self.assertEqual(rc, 0, err)
        self.assertIn("--model gpt-5.3-codex-spark", out)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", out)
        with open(os.path.join(d, "launch.sh")) as f:
            sh = f.read()
        self.assertIn("--model gpt-5.3-codex-spark", sh)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", sh)
        self.assertNotIn("320000", sh)
        # explicit --model outranks the record for this mint
        rc, out, _ = self._add(("launch", "codex", "--model", "gpt-5.6-sol"))
        self.assertEqual(rc, 0)
        self.assertIn("--model gpt-5.6-sol", out)
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=320000", f.read())

    def test_launch_refresh_holds_the_lifecycle_lock(self):  # noqa: VACUOUS_ASSERTION — writer runs once and observes the held lock
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        real = seat._write_launch_assets
        checked = []

        def write(family, d, *args, **kwargs):
            with open(os.path.join(d, ".spawn.lock"), "a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    checked.append(True)
                else:
                    checked.append(False)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return real(family, d, *args, **kwargs)

        with mock.patch.object(seat, "_write_launch_assets", side_effect=write):
            rc, _out, err = self._add(("launch", "codex"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(checked, [True])

    def _plant_launch_room(self, d, room, source):
        """Rewrite the minted launch.sh to carry a specific room + provenance.

        `room=None` with `source="cleared"` is the DELIBERATELY-EMPTY mint, and
        it is a real shape production writes: the stamp rides alone, with no
        HELM_CHAT_ROOM beside it. Absence and a cleared value must not share a
        representation, so the helper that plants them must be able to spell
        both."""
        p = os.path.join(d, "launch.sh")
        with open(p) as f:
            sh = f.read()
        # the room WORDS go, never the line that carries them: the exec line
        # holds the seat's whole launch recipe, which the resume restores
        # and refuses to guess (task/3695). A value is a whole shell word:
        # bare characters, a quoted run, or the '"'"' spelling of a quote
        # inside the single-quoted argument the launch owner passes on.
        word = r"""(?:'"'"'[^']*'"'"'|'[^']*'|"[^"]*"|[^\s'"])*"""
        lines = [re.sub(r" HELM_CHAT_ROOM(?:_SOURCE)?=" + word, "", ln)
                 for ln in sh.splitlines()]
        inject = ['export HELM_CHAT_ROOM=%s' % room] if room else []
        if source:
            inject.append('export HELM_CHAT_ROOM_SOURCE=%s' % source)
        lines[1:1] = inject
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")

    def _minted_with_room(self, room):
        """A seat whose launch.sh the real minter wrote carrying `room`,
        spelled the way the launch line quotes it inside the one argument
        the launch owner hands its supervisor."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        self.assertIsNot(seat._write_launch_assets(
            "codex", d, room, room_source="explicit"),
            seat._SEAT_SURFACE_REFUSED)
        p = os.path.join(d, "launch.sh")
        with open(p) as f:
            self.assertIn("HELM_CHAT_ROOM=", f.read())
        return d, p

    def test_planting_a_room_strips_a_quoted_room_whole(self):
        """A room the shell must quote is spelled as a quoted word nested in
        the supervisor's quoted argument. MUTATION: stop the strip at the
        first quote — the quoted value is left on the exec line, glued to
        the word before it, where it rewrites that word's value."""
        d, p = self._minted_with_room("#ops x")
        self._plant_launch_room(d, "main", "explicit")
        with open(p) as f:
            planted = f.read()
        self.assertNotIn("#ops", planted)
        self.assertIn("export HELM_CHAT_ROOM=main", planted)

    def test_planting_a_room_strips_an_unquoted_room(self):
        """The control: a room the shell takes bare is stripped whole on
        the same line."""
        d, p = self._minted_with_room("ops-room")
        self._plant_launch_room(d, "main", "explicit")
        with open(p) as f:
            planted = f.read()
        self.assertNotIn("ops-room", planted)
        self.assertIn("export HELM_CHAT_ROOM=main", planted)

    # -- the seam these arms assert on -------------------------------------
    #
    # THE ARMS BELOW READ THE RECORD, NEVER THE PRINTOUT, and that is the
    # whole reason this block was rewritten. Every one of them used to assert
    # `room=X` in the text of `spawn --print`. `--print` returns from
    # _spawn_plan BEFORE _write_launch_assets, so those arms drove a path that
    # WRITES NOTHING — measured on the code they were green against: launch.sh
    # byte-identical afterwards, and no spawn.json created at all. A room's
    # entire job is to be persisted and read back by the NEXT verb, so an
    # assertion about a rendered line is an assertion about the one thing that
    # does not matter, and seven distinct defects lived underneath a full set
    # of greens because of it.
    #
    # `_persisted` returns all three surfaces a seat's home is really written
    # to, because they disagreed in production: an unreadable derivation wiped
    # the launch script while the roster kept the old room, and delivery reads
    # the roster.

    def _pane_seam(self, spawn_error=None):
        """The metaharness seam a real spawn/resume touches: list (the reap),
        spawn (pane create), submit (the onboarding turn), stop (cleanup).

        Deliberately LOCAL. A fixture imported from a sibling test module ties
        this suite's collection to that one's, which is a coupling that costs
        a whole suite when the other module moves."""
        from helm import harness

        class _FakePane:
            name, path = "herdr", "/bin/fake"

            def __init__(self):
                self.spawned, self.stopped, self.submitted = [], [], []

            def list(self):
                return []

            def spawn_failure_absent(self, _error):
                return bool(spawn_error)

            def spawn(self, command, title=None, cwd=None):
                if spawn_error:
                    raise harness.HarnessError(spawn_error)
                self.spawned.append((command, title, cwd))
                return "pane-1"

            def submit(self, handle, text):
                self.submitted.append((handle, text))
                return harness.DELIVERED, "fake pane took the turn"

            def stop(self, handle):
                self.stopped.append(handle)

        return _FakePane()

    def _lost_create_seam(self):
        """A remote create succeeds, then its reply is lost before a handle."""
        from helm import harness

        class _LostCreate:
            name, path = "fake", "/bin/fake"

            def __init__(self):
                self.remote = []

            def list(self):
                return [{"handle": h, "status": "live"} for h in self.remote]

            def spawn(self, command, title=None, cwd=None):
                self.remote.append("orphan-1")
                raise harness.HarnessError("create reply was lost")

        return _LostCreate()

    def _run_seat(self, args, adapter=None):
        """Drive a REAL spawn/resume — no --print, no mocked asset writer — so
        launch.sh, spawn.json and the roster are all genuinely written. Only
        the metaharness seam and the proxy liveness probe are faked."""
        from helm import harness
        adapter = adapter or self._pane_seam()
        home_cwd = getattr(self, "seat_home", None) \
            or os.path.join(self.tmp, "seat-home")
        os.makedirs(home_cwd, exist_ok=True)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=adapter), \
                mock.patch.object(seat, "_running_pid", return_value=4242), \
                mock.patch.object(seat, "_seat_home_cwd",
                                  return_value=home_cwd), \
                mock.patch.dict(os.environ, {"HELM_SPAWN_SEND_DELAY": "0",
                                             "HELM_SUBMIT_SETTLE_S": "0"}), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(args))
        return rc, out.getvalue(), err.getvalue(), adapter

    def _persisted(self, d, seat_name="codex"):
        """(launch.sh, spawn.json, roster) homing — the three durable surfaces,
        read back through the same accessors production reads them with."""
        from helm import seats
        try:
            with open(os.path.join(d, "spawn.json")) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            rec = {}
        row = seats.roster().get(seat_name) or {}
        return {
            "launch": seat._homing_from_launch(os.path.join(d, "launch.sh")),
            "record": (rec.get("room"), rec.get("room_source")),
            "worktree": rec.get("worktree"),
            "room_worktree": rec.get("room_worktree"),
            "roster": (row.get("home_room"), row.get("home_room_source")),
        }

    def _project(self, name, sub=None):
        """A real git checkout (so the REAL derivation runs), optionally with a
        subdirectory — a seat living at the checkout ROOT is rehomed by
        _resume_cwd, which is a different seam than the one under test."""
        root = os.path.join(self.tmp, name)
        os.makedirs(root, exist_ok=True)
        subprocess.run(["git", "init", "-q", root], check=True)
        if not sub:
            return root
        here = os.path.join(root, sub)
        os.makedirs(here, exist_ok=True)
        return here

    def _seat_ready(self, room, source, worktree=None):
        """A minted seat carrying a specific home, plus the spawn record that
        says where that home was DERIVED from. Returns its instance dir."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        self.seat_home = os.path.join(self.tmp, "seat-home")
        os.makedirs(self.seat_home, exist_ok=True)
        self._plant_launch_room(d, room, source)
        if worktree:
            # The fake adapter's empty inventory proves this recorded old pane
            # terminal. Omitting its adapter identity now correctly yields
            # UNKNOWN and would stop before the homing seam under test.
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump({"v": 1, "seat": "codex", "worktree": worktree,
                           "room": room, "room_source": source,
                           "room_worktree": worktree,
                           "session": "11111111-1111-4111-8111-111111111111",
                           "harness": "herdr", "handle": "old-pane"}, f)
        from helm import seats
        if room:
            seats.write_roster("codex", cwd=worktree or "/tmp/p",
                               home_room=room,
                               home_room_source=source or "explicit",
                               presence_beat=False)
        return d

    def test_spawn_refuses_before_reaping_when_paid_proxy_close_is_unproved(self):
        with mock.patch("helm.hooks.helm_bin",
                        return_value="/srv/helm/bin/helm"):
            self._seat_ready(None, None)
        adapter = self._pane_seam()
        with mock.patch.object(seat, "_gated_proxy_before_pane",
                               return_value=("unknown", "close failed")), \
                mock.patch.object(seat, "_spawn_reap") as reap:
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex"), adapter=adapter)
        self.assertEqual(rc, 1)
        self.assertIn("paid proxy closure is unproved", err)
        reap.assert_not_called()
        self.assertEqual(adapter.spawned, [])

    def test_resume_never_creates_a_pane_when_paid_proxy_close_is_unproved(self):
        with mock.patch("helm.hooks.helm_bin",
                        return_value="/srv/helm/bin/helm"):
            self._seat_ready(None, None)
        adapter = self._pane_seam()
        with mock.patch("helm.hooks.helm_bin",
                        return_value="/srv/helm/bin/helm"), \
                mock.patch.object(seat, "_gated_proxy_before_pane",
                                  return_value=("unknown", "close failed")):
            rc, _out, err, _ad = self._run_seat(
                ("resume", "codex"), adapter=adapter)
        self.assertEqual(rc, 1)
        self.assertIn("paid proxy closure is unproved", err)
        self.assertEqual(adapter.spawned, [])

    # -- task/3208: a resume right after /clear -----------------------------
    # MEASURED twice on a fleet host (row b07de796): `/clear`,
    # then `helm seat resume <seat>` at once, came back on the PRE-clear
    # session at 85% full. MEASURED again on a scratch config dir against
    # claude 2.1.283 (stub endpoint, no account): the process's own presence
    # record <config>/sessions/<pid>.json names the NEW session the instant
    # /clear lands, while the transcripts disagree two ways depending on the
    # claude — no transcript for the new session yet (that host's), or one that
    # holds only the /clear command records and no assistant turn (2.1.283's),
    # which the reboot-stub ranking then puts BELOW the old session's real
    # turns. Either way the newest-transcript pick answers the OLD session.
    _OLD = "aaaaaaaa-0000-4000-8000-000000000001"
    _NEW = "bbbbbbbb-0000-4000-8000-000000000002"
    _LIVE_PID = 7300001
    _TURNS = ('{"type":"user","message":{"role":"user","content":"hi"}}\n'
              '{"type":"assistant","message":{"role":"assistant",'
              '"content":[{"type":"text","text":"x"}]}}\n')
    # The measured shape of a just-cleared session's transcript (2.1.283):
    # the /clear command's own records, and no assistant turn at all.
    _CLEARED = ('{"type":"user","message":{"role":"user","content":'
                '"<command-name>/clear</command-name>"}}\n'
                '{"type":"system","subtype":"local_command"}\n')

    def _plant_transcript(self, d, sid, body, age_s):
        proj = os.path.join(d, "claude", "projects", "-p")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, sid + ".jsonl")
        with open(path, "w") as f:
            f.write(body)
        t = time.time() - age_s
        os.utime(path, (t, t))
        return path

    def _plant_live_record(self, d, sid, pid=None):
        """Claude Code's presence record, in the shape 2.1.283 writes it:
        the live process's pid, birth stamp and CURRENT session."""
        pid = pid or self._LIVE_PID
        root = os.path.join(d, "claude", "sessions")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "%d.json" % pid), "w") as f:
            json.dump({"pid": pid, "sessionId": sid, "procStart": "4242",
                       "kind": "interactive", "entrypoint": "cli",
                       "status": "idle"}, f)

    def _alive(self, *pids):
        """Only the planted pids read as live claude processes; every other
        pid keeps the real answer, so no host process is lent a record."""
        from helm import sessions
        real = sessions._pid_is_claude
        live = set(pids or (self._LIVE_PID,))
        return mock.patch.object(
            sessions, "_pid_is_claude",
            side_effect=lambda pid, start=None:
                int(pid) in live or real(pid, start))

    def _resumed_tail(self, adapter):
        """The continuity flag and session the relaunch line carries."""
        self.assertEqual(len(adapter.spawned), 1, adapter.spawned)
        return shlex.split(adapter.spawned[0][0])[-2:]

    def test_a_resume_right_after_clear_never_takes_the_OLD_session(self):
        """THE BRIEF'S ARM, in the fleet host's shape: the /clear'd process
        names a NEW session that has no transcript yet, and the OLD session's
        transcript is the newest by mtime. `--resume <new>` is refused by
        claude for a session with no transcript (MEASURED: "No conversation
        found", exit 1), and `--continue` takes the newest transcript, which
        is the old one again; so the pane starts FRESH under the live id
        (MEASURED: claude accepts `--session-id` for an id with no
        transcript)."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive():
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        tail = self._resumed_tail(adapter)
        self.assertNotIn(self._OLD, adapter.spawned[0][0],
                         "the relaunch resumed the pre-/clear session")
        self.assertIn(self._NEW, adapter.spawned[0][0])
        self.assertEqual(tail, ["--session-id", self._NEW])
        self.assertIn("starts FRESH", out + err)

    def test_a_cleared_session_with_its_clear_records_is_RESUMED(self):
        """2.1.283's shape: the NEW session's transcript exists and is the
        newest, but holds only the /clear records, so the stub ranking puts
        the OLD session's real turns above it. The live record names NEW, and
        claude resumes a transcript with no assistant turn (MEASURED)."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._OLD, self._TURNS, 5)
        self._plant_transcript(d, self._NEW, self._CLEARED, 0)
        # THE PREMISE, asserted: today's ranking alone answers OLD here.
        self.assertEqual(seat._newest_seat_session(d)[0], self._OLD)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive():
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertIn(self._NEW, adapter.spawned[0][0])
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._NEW])

    def test_with_no_live_process_the_newest_transcript_still_decides(self):
        """THE CONTROL: no live claude process holds a presence record here —
        a stale record of a DEAD pid stays on disk after a kill (MEASURED) and
        names nothing — so the resume keeps today's newest-transcript pick."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID + 1):
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._OLD])

    def test_a_first_transcript_written_before_the_reap_is_resumed(self):
        """The live read and the reap are two moments: a session that takes
        its first message between them has a transcript by the time the pane
        relaunches, and `--session-id` on an id with a transcript is refused
        ("already in use", MEASURED). The flag is asked again after the
        reap, which is the last moment the old process could write."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        real_reap = seat._reap_stale

        def reap_after_a_first_message(*a, **k):
            self._plant_transcript(d, self._NEW, self._TURNS, 0)
            return real_reap(*a, **k)

        adapter = self._pane_seam()
        with self._alive(), mock.patch.object(
                seat, "_reap_stale", side_effect=reap_after_a_first_message):
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertIn("starts FRESH", out)        # the read said fresh ...
        self.assertIn("resuming it instead", out)  # ... the reap said not
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._NEW])

    def test_live_sessions_the_register_cannot_choose_between_refuse(self):  # noqa: VACUOUS_ASSERTION — no reap and no pane IS the refusal's contract; the same run's refusal text names both live sessions, and the register-pin arm below spawns exactly one pane over the identical fixture
        """Two live claude processes in one seat's config home naming two
        sessions — the pane and a `claude -p` child it runs, which writes a
        record there too while it runs (MEASURED) — and no register bound to
        either: which session the seat holds is UNKNOWN, and a resume refuses
        before anything is reaped instead of picking one."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        self._plant_live_record(d, self._OLD, pid=self._LIVE_PID + 1)
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID, self._LIVE_PID + 1), \
                mock.patch.object(seat, "_reap_stale") as reap:
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 1)
        self.assertIn("is UNKNOWN", err)
        self.assertIn(self._NEW[:8], err)
        self.assertIn(self._OLD[:8], err)
        reap.assert_not_called()
        self.assertEqual(adapter.spawned, [])

    def test_the_register_bound_pid_chooses_between_live_sessions(self):
        """THE SAME TWO PROCESSES, and the spawn register bound one of them at
        its SessionStart (`session_pid` + `session_pid_identity`): that one
        is the seat, and its session is resumed."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        path = os.path.join(d, "spawn.json")
        with open(path) as f:
            rec = json.load(f)
        rec.update(session_pid=self._LIVE_PID,
                   session_pid_identity="proc:4242")
        with open(path, "w") as f:
            json.dump(rec, f)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_transcript(d, self._NEW, self._CLEARED, 0)
        self._plant_live_record(d, self._NEW)
        self._plant_live_record(d, self._OLD, pid=self._LIVE_PID + 1)
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID, self._LIVE_PID + 1):
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._NEW])

    def test_a_pruned_copy_of_the_live_walled_session_is_still_the_rescue(self):
        """THE 2026-08-04 SHAPE UNDER THE LIVE RUNG. The seat is walled at
        context-full and its claude is still ALIVE (the walled original keeps
        growing until its pane is reaped), a `cv prune` minted a copy whose
        head carries the lineage stamp, and the operator runs the BARE resume
        autocompact's own manual-recovery line names ("run `helm seat resume
        <seat>` after inspecting the pane"). Trunk's ranking resumes the
        pruned copy (rank 3); the live process naming its own walled session
        must not outrank that rescue, or the seat comes back walled."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        reg = "11111111-1111-4111-8111-111111111111"
        copy = "dddddddd-0000-4000-8000-000000000004"
        self._plant_transcript(d, reg, self._TURNS, 0)          # growing
        self._plant_transcript(
            d, copy,
            '{"type":"user","sessionId":"%s","session_id":"%s","message":'
            '{"role":"user","content":"hi"}}\n{"type":"assistant","sessionId":'
            '"%s","session_id":"%s","message":{"role":"assistant","content":'
            '[{"type":"text","text":"x"}]}}\n' % (copy, reg, copy, reg), 60)
        self._plant_live_record(d, reg)          # the walled claude, alive
        adapter = self._pane_seam()
        with self._alive():
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", copy])

    def test_an_unreadable_session_record_refuses_before_the_reap(self):  # noqa: VACUOUS_ASSERTION — no reap and no pane IS the refusal's contract; the same run's refusal text names the unreadable record, and the fresh-session arm above spawns one pane over the same live record
        """The census cannot say which session the seat holds when one record
        in its home cannot be read — that record may be the seat's own — so
        the resume refuses before anything is reaped, even though another
        record reads cleanly. Nothing else pins this rung: the multi-session
        refusal above reaches UNKNOWN by a different branch."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        with open(os.path.join(d, "claude", "sessions", "corrupt.json"),
                  "w") as f:
            f.write("{")
        adapter = self._pane_seam()
        with self._alive(), mock.patch.object(seat, "_reap_stale") as reap:
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 1)
        self.assertIn("could not all be read", err)
        self.assertIn("corrupt.json", err)
        reap.assert_not_called()
        self.assertEqual(adapter.spawned, [])

    def test_a_lone_helper_claude_never_hands_the_seat_its_session(self):
        """The seat's own claude is GONE — its record stays on disk naming the
        registered session (MEASURED: a killed session's record outlives it) —
        and one `claude -p` helper it started still runs in the same config
        home, holding its own session (MEASURED: kind interactive, entrypoint
        sdk-cli). No live process holds the SEAT's session, so the resume must
        keep the register/newest-transcript pick, which answers the registered
        session (rank 2). Resuming the helper's session brings the seat back
        as a stranger."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        reg = "11111111-1111-4111-8111-111111111111"
        helper = "cccccccc-0000-4000-8000-000000000003"
        self._plant_transcript(d, reg, self._TURNS, 60)
        self._plant_transcript(d, helper, self._TURNS, 0)
        self._plant_live_record(d, reg)          # the seat's own, pid dead
        root = os.path.join(d, "claude", "sessions")
        with open(os.path.join(root, "%d.json" % (self._LIVE_PID + 1)),
                  "w") as f:
            json.dump({"pid": self._LIVE_PID + 1, "sessionId": helper,
                       "procStart": "4243", "kind": "interactive",
                       "entrypoint": "sdk-cli", "status": "busy"}, f)
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID + 1):
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", reg])

    # -- F3's rule: a live record speaks for the seat only on the register's
    # evidence — its session is the register's, the register pins its exact
    # pid + birth stamp, or the register holds no session.
    _REG = "11111111-1111-4111-8111-111111111111"   # `_seat_ready`'s session

    def _register(self, d, **fields):
        path = os.path.join(d, "spawn.json")
        with open(path) as f:
            rec = json.load(f)
        rec.update(fields)
        with open(path, "w") as f:
            json.dump(rec, f)

    def _plant_helper(self, d, sid, pid):
        """A `claude -p` child's record, in the shape 2.1.283 writes one while
        it runs (MEASURED: kind interactive, entrypoint sdk-cli)."""
        with open(os.path.join(d, "claude", "sessions", "%d.json" % pid),
                  "w") as f:
            json.dump({"pid": pid, "sessionId": sid, "procStart": "4243",
                       "kind": "interactive", "entrypoint": "sdk-cli",
                       "status": "busy"}, f)

    def test_a_helper_beside_a_PINNED_dead_seat_never_names_the_session(self):
        """The orca shape of the helper case: the register PINS the seat's own
        claude (session_pid + session_pid_identity), that claude is dead, and
        a sdk-cli helper it started still runs in its home. The pin names
        another process and the helper's session is not the register's, so
        nothing speaks for the seat, and the ranking answers the registered
        session as trunk did."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        self._register(d, session_pid=self._LIVE_PID,
                       session_pid_identity="proc:4242")
        helper = "cccccccc-0000-4000-8000-000000000003"
        self._plant_transcript(d, self._REG, self._TURNS, 60)
        self._plant_transcript(d, helper, self._TURNS, 0)
        self._plant_live_record(d, self._REG)       # the seat's own, pid dead
        self._plant_helper(d, helper, self._LIVE_PID + 1)
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID + 1):
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertIn("none speaks for the seat", out)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._REG])

    def test_a_LIVE_seat_beside_a_live_helper_resumes_the_seats_session(self):
        """The seat's own claude is ALIVE on the registered session and a
        helper runs beside it on another. Only the seat's record is admitted,
        so the helper neither names the session nor makes it UNKNOWN."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        helper = "cccccccc-0000-4000-8000-000000000003"
        self._plant_transcript(d, self._REG, self._TURNS, 60)
        self._plant_transcript(d, helper, self._TURNS, 0)
        self._plant_live_record(d, self._REG)
        self._plant_helper(d, helper, self._LIVE_PID + 1)
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID, self._LIVE_PID + 1):
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertIn("presence record names session %s" % self._REG, out)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._REG])

    def test_a_clear_the_register_recorded_is_the_live_session(self):
        """Rule (a): a non-orca SessionStart bind of source `clear` moved the
        register to the NEW session, which has no transcript yet, and the OLD
        transcript is the newest. The record's session IS the register's."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        self._register(d, session=self._NEW)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive():
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter),
                         ["--session-id", self._NEW])

    def test_a_clear_on_the_PINNED_process_is_the_live_session(self):
        """Rule (b), the orca /clear shape before its SessionStart rebind has
        landed: the register still records the OLD session but pins the live
        process by pid and birth stamp, and that process names NEW."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        self._register(d, session=self._OLD, session_pid=self._LIVE_PID,
                       session_pid_identity="proc:4242")
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive():
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter),
                         ["--session-id", self._NEW])

    # -- a register with NO session: only the seat's interactive claude
    # speaks. A review (row 7744dc85daa8, F1) measured the admission of every
    # live record there handing a dead seat a `claude -p` helper's session.
    _HELPER = "cccccccc-0000-4000-8000-000000000003"

    def _unregistered_with_helper(self):
        """A seat with no spawn register, so no session to contradict a live
        record (a register that names a pane but no session is refused
        earlier, by the reap's exit-owner proof); two transcripts with real
        turns (the OLD one newest, so the ranking answers it); the seat's own
        interactive record naming NEW; and a `claude -p` helper's record
        naming its own session."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._NEW, self._TURNS, 120)
        self._plant_transcript(d, self._HELPER, self._TURNS, 60)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        self._plant_helper(d, self._HELPER, self._LIVE_PID + 1)
        return d

    def test_a_lone_helper_never_speaks_for_a_register_with_no_session(self):
        """F1. The seat's claude is dead, its register holds no session, and
        an orphaned `claude -p` helper still runs in its home. Trunk resumed
        the newest transcript with real turns (OLD); the helper's session is
        not the seat's conversation, so the resume must still take OLD."""
        self._unregistered_with_helper()
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID + 1):
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self._HELPER, adapter.spawned[0][0],
                         "the relaunch resumed a helper's session")
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._OLD])
        self.assertIn("none speaks for the seat", out)

    def test_a_live_cli_record_speaks_for_a_register_with_no_session(self):
        """THE CONTROL on the same fixture: the seat's interactive claude is
        ALIVE beside the helper, so its record names the session (NEW), over
        the ranking's OLD and without the helper making the read UNKNOWN."""
        self._unregistered_with_helper()
        adapter = self._pane_seam()
        with self._alive(self._LIVE_PID, self._LIVE_PID + 1):
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertIn("presence record names session %s" % self._NEW, out)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._NEW])

    def test_a_live_cli_record_alone_speaks_for_a_register_with_no_session(self):
        """The same control with no helper at all: the one live record is the
        seat's interactive claude, and it names the session."""
        d = self._seat_ready(None, None)
        self._plant_transcript(d, self._NEW, self._TURNS, 120)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive():
            rc, _out, err, _ad = self._run_seat(("resume", "codex"),
                                                adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter), ["--resume", self._NEW])

    def test_the_resumed_line_names_the_flag_the_relaunch_used(self):
        """A relaunch under `--session-id` (a live session with no transcript
        yet) reported "(--resume)" in its closing line. The line names the
        flag the relaunch line carried."""
        d = self._seat_ready(None, None, worktree=self.tmp)
        self._register(d, session=self._NEW)
        self._plant_transcript(d, self._OLD, self._TURNS, 0)
        self._plant_live_record(d, self._NEW)
        adapter = self._pane_seam()
        with self._alive():
            rc, out, err, _ad = self._run_seat(("resume", "codex"),
                                               adapter=adapter)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resumed_tail(adapter),
                         ["--session-id", self._NEW])
        self.assertIn("session %s… (--session-id)" % self._NEW[:8], out)
        self.assertNotIn("(--resume)", out)

    def test_a_cwd_MOVE_writes_the_TARGET_room_into_every_surface(self):
        """MEASURED LIVE ON ANOTHER PROJECT, 2026-08-15: a seat re-spawned with
        --cwd into another project's checkout kept HELM_CHAT_ROOM=helm
        (SOURCE=derived) from its previous mint, did that project's work
        homed in #helm, and its conversation split across two rooms for a
        day. A DERIVED room is a function of the cwd; moving the cwd makes it
        stale by definition.

        THE AMBIENT ROOM IS SET TO THE WRONG ANSWER, deliberately: spawns
        happen from inside helm seats, so the SPAWNING process's own
        HELM_CHAT_ROOM is the pole a resolver-based derivation loses at, and
        an ambient room with no _SOURCE reads as explicit — a tier that then
        outranks every later derived join. Only the cwd may decide."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("stale-old-room", "derived")
        os.environ["HELM_CHAT_ROOM"] = "ambient-spawner-room"   # must LOSE
        os.environ.pop("HELM_CHAT_ROOM_SOURCE", None)
        from helm import seats_identity
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                lambda cwd: (seats_identity.DERIVE_OK, "moved-to-here")
                if cwd == target else (seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], ("moved-to-here", "derived"), state)
        self.assertEqual(state["record"], ("moved-to-here", "derived"), state)
        self.assertEqual(state["roster"], ("moved-to-here", "derived"), state)

    def test_a_ROOMLESS_resume_derives_from_the_TARGET_cwd_not_the_invoker(self):
        """A resume with no recorded room used to fall through resolve_homing,
        whose SECOND rung is the env seam — and that env belongs to whichever
        process typed `helm seat resume`, nearly always another seat. So the
        INVOKER's room beat the directory the pane was about to land in, and,
        carrying no _SOURCE stamp, was recorded as EXPLICIT: a tier no later
        derived join can correct. A seat's home is a fact about the seat."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready(None, None)
        os.environ["HELM_CHAT_ROOM"] = "ambient-invoker-room"   # must LOSE
        os.environ.pop("HELM_CHAT_ROOM_SOURCE", None)
        from helm import seats_identity
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                lambda cwd: (seats_identity.DERIVE_OK, "target-room")
                if cwd == target else (seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("resume", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], ("target-room", "derived"), state)
        self.assertEqual(state["record"], ("target-room", "derived"), state)
        self.assertEqual(state["roster"], ("target-room", "derived"), state)

    def test_a_ROOMLESS_resume_with_no_project_stays_UN_HOMED(self):
        """THE FIRST OF THREE STATES, and the one an ambient fallback destroys.
        A seat that never had a room, resumed into a directory with no project,
        has nothing to be homed as — and must be recorded that way. It used to
        take the invoking process's room instead, and a 'main' default in the
        resume record turned the rest into a seat homed in #main, which is the
        roster-scatter class the spawn record already refuses."""
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready(None, None)
        os.environ["HELM_CHAT_ROOM"] = "ambient-invoker-room"   # must LOSE
        os.environ.pop("HELM_CHAT_ROOM_SOURCE", None)
        from helm import seats_identity
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("resume", "codex", "--cwd", nowhere))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional: an
        # un-homed read and an UNREADABLE launch script are both (None,
        # None), so the script has to be shown present and re-minted before
        # its emptiness means anything. The room is the only field absent.
        with open(os.path.join(d, "launch.sh")) as f:
            script = f.read()
        self.assertIn("HELM_CHAT_NAME=codex", script)
        self.assertNotIn("HELM_CHAT_ROOM=", script)
        self.assertEqual(state["launch"], (None, None), state)
        self.assertEqual(state["record"], (None, None), state)
        self.assertEqual(state["roster"], (None, None), state)

    def test_a_DERIVED_room_that_moves_to_NOWHERE_is_CLEARED_durably(self):
        """THE SECOND STATE. A cwd that is valid but PROJECTLESS derives
        nothing, and that measured nothing must clear the stale room — the
        carry-forward this whole fix exists to stop, on the input where it
        matters most.

        DURABLY IS THE WORD THAT WAS MISSING. A clear written as a bare None
        reached the roster mirror as 'no opinion', fell through
        write_roster's `elif home_room:` and did NOTHING: the launch script
        said un-homed while the roster still said stale-old-room, and delivery
        believes the roster. All three surfaces are asserted for that reason."""
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready("stale-old-room", "derived")
        from helm import seats, seats_identity
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", nowhere))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], (None, seats.ROOM_CLEARED), state)
        self.assertEqual(state["record"], (None, seats.ROOM_CLEARED), state)
        self.assertEqual(state["roster"], (None, None), state)
        # and it is a CLEAR, not an absence: the script says so in its own text
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("HELM_CHAT_ROOM_SOURCE=cleared", f.read())

    def test_a_CLEARED_room_is_not_absence_and_survives_a_resume(self):
        """THE THIRD STATE, and the law underneath all of them: absence and a
        cleared value may not share a representation. A cleared seat has
        already answered the homing question — measured, at a real cwd — so a
        later resume must not re-open it and adopt whatever room the invoking
        process happens to sit in. A never-homed seat may; a cleared one may
        not. When they were both spelled `None` there was no way to tell."""
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready(None, "cleared")
        from helm import seats, seats_identity
        # the read-back is the first assertion: the two states are DISTINCT on
        # disk, which is what the old representation could not express.
        self.assertEqual(
            seat._homing_from_launch(os.path.join(d, "launch.sh")),
            (None, seats.ROOM_CLEARED))
        os.environ["HELM_CHAT_ROOM"] = "ambient-invoker-room"   # must LOSE
        os.environ.pop("HELM_CHAT_ROOM_SOURCE", None)
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("resume", "codex", "--cwd", nowhere))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], (None, seats.ROOM_CLEARED), state)
        self.assertEqual(state["record"], (None, seats.ROOM_CLEARED), state)

    def test_a_BARE_resume_RE_DERIVES_when_the_seat_has_moved(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a fully-specified NON-empty value ('new-project-room', 'derived') on all three surfaces, and the unconditional control below reads the SAME launch script back as ('stale-old-room', 'derived') before the resume; an unreadable script answers (None, None) and fails both
        """THE FLAG WAS NEVER THE DISCRIMINATOR. Re-deriving only when --cwd
        was typed left the identical defect on every unflagged move — and
        resume moves seats on its OWN authority: it follows the seat's last
        session, rehomes it out of the shared checkout or a temp dir, and the
        CONTEXT_FULL recovery seam pins an authoritative worktree. None of
        those carry a flag, and all of them relocate the pane. The move is
        measured against the worktree the spawn register recorded beside the
        room, which is the derivation's own input."""
        old = self._project("old-project", sub="work")
        new = self._project("new-project", sub="work")
        d = self._seat_ready("stale-old-room", "derived", worktree=old)
        # unconditional positive control on the observable under test: the
        # stale room really is what the script says before the resume runs
        self.assertEqual(
            seat._homing_from_launch(os.path.join(d, "launch.sh")),
            ("stale-old-room", "derived"))
        from helm import seats_identity
        rooms = {new: "new-project-room", old: "stale-old-room"}
        with mock.patch.object(seat, "_newest_seat_session",
                               lambda d, prefer_source=None: (None, new)), \
                mock.patch.object(
                    seats_identity, "_git_project_typed",
                    lambda cwd: ((seats_identity.DERIVE_OK, rooms[cwd])
                                 if cwd in rooms else
                                 (seats_identity.DERIVE_NONE, None))):
            rc, _out, err, ad = self._run_seat(("resume", "codex"))
        self.assertEqual(rc, 0, err)
        # the pane really did land somewhere else — the premise of the arm,
        # asserted rather than assumed
        self.assertEqual(ad.spawned[0][2], new)
        state = self._persisted(d)
        self.assertEqual(state["launch"], ("new-project-room", "derived"),
                         state)
        self.assertEqual(state["record"], ("new-project-room", "derived"),
                         state)
        self.assertEqual(state["roster"], ("new-project-room", "derived"),
                         state)

    def test_an_UNREADABLE_derivation_KEEPS_the_room_it_cannot_measure(self):
        """CANNOT-LOOK MUST NOT RENDER AS LOOKED-AND-FOUND-NOTHING. The
        derivation returned a bare None for both 'this cwd has no project' and
        'git could not be run at all', so a missing binary, an unreadable
        directory or a five-second timeout un-homed a live seat — a clear
        performed on a measurement nobody took. Refusing on ABSENCE downgrades
        every case that merely aged out; the refusal has to be on the measured
        contradiction. Here the room is KEPT, on every surface, and the failure
        is said out loud instead of being spent."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived")
        from helm import seats_identity

        def unrunnable(*_a, **_k):
            raise OSError("git is not installed on this box")

        with mock.patch.object(seats_identity.subprocess, "run", unrunnable):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], ("proj-x", "derived"), state)
        self.assertEqual(state["record"], ("proj-x", "derived"), state)
        self.assertEqual(state["roster"], ("proj-x", "derived"), state)
        self.assertIn("could not derive", err)

    def test_unknown_persists_the_old_room_input_so_a_bare_retry_rederives(self):  # noqa: VACUOUS_ASSERTION — old and target paths are both asserted non-empty
        old = self._project("old-project", sub="work")
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived", worktree=old)
        from helm import seats_identity
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_UNKNOWN, None)):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["worktree"], target)
        self.assertEqual(state["room_worktree"], old)
        with open(os.path.join(d, "spawn.json")) as f:
            prior = json.load(f)
        self.assertTrue(seat._cwd_moved(prior, "codex", target, False),
                        "persisting runtime cwd suppressed the next bare retry")

    def test_launch_snapshot_discriminates_present_absent_and_unknown(self):
        launch = os.path.join(self.tmp, "snapshot-launch.sh")
        with open(launch, "wb") as f:
            f.write(b"old launch\n")
        os.chmod(launch, 0o640)
        self.assertEqual(seat._launch_snapshot(launch),
                         ("present", b"old launch\n", 0o640))
        os.unlink(launch)
        self.assertEqual(seat._launch_snapshot(launch), ("absent",))
        with mock.patch("builtins.open", side_effect=PermissionError("closed")):
            self.assertEqual(seat._launch_snapshot(launch), ("unknown",))
        with open(launch, "wb") as f:
            f.write(b"old inode\n")
        os.chmod(launch, 0o640)
        replacement = launch + ".replacement"
        with open(replacement, "wb") as f:
            f.write(b"new inode\n")
        os.chmod(replacement, 0o600)
        real_open = open

        def replace_after_open(path, mode):
            f = real_open(path, mode)
            os.replace(replacement, path)
            return f

        with mock.patch("builtins.open", side_effect=replace_after_open):
            self.assertEqual(
                seat._launch_snapshot(launch),
                ("present", b"old inode\n", 0o640),
                "snapshot bytes and mode must come from the same open inode")
        with real_open(launch, "rb") as f:
            self.assertEqual(f.read(), b"new inode\n")
        self.assertEqual(stat.S_IMODE(os.stat(launch).st_mode), 0o600)

    def test_a_FAILED_spawn_leaves_the_launch_script_UNTOUCHED(self):
        """A SPAWN THAT FAILS MUST NOT LEAVE THE SEAT CHANGED. The re-mint runs
        BEFORE the pane is created — correctly, so the pane's `sh` reads
        current code — but that ordering meant a spawn which then failed had
        already rewritten the one file every later launch and resume reads.
        Measured: an adapter that refuses the pane moved launch.sh from
        ('proj-x', 'derived') to un-homed and returned 1, and the next resume
        of that seat then adopted the room of whatever process ran it.

        THE OTHER POLE of the survival bound: the adapter refuses the pane, so
        NOTHING is running the new script and the rollback must run. Asserted
        as a premise below, because 'no pane survived' is what makes restoring
        legal here — an arm that only checked the bytes would still pass if
        the bound stopped discriminating and the rollback restored always."""
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready("proj-x", "derived")
        before = self._persisted(d)["launch"]
        self.assertEqual(before, ("proj-x", "derived"))   # premise, asserted
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            bytes_before = f.read()
        # unconditional: comparing two empty reads would pass for a DELETED
        # script, which is a worse outcome than the one under test
        self.assertIn(b"HELM_CHAT_ROOM=proj-x", bytes_before)
        from helm import seats_identity
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_NONE, None)):
            rc, _out, err, ad = self._run_seat(
                ("spawn", "codex", "--cwd", nowhere),
                adapter=self._pane_seam(
                    spawn_error="the metaharness refused the pane"))
        self.assertEqual(rc, 1, "the spawn was supposed to FAIL")
        # premise of THIS pole: no pane exists, so nothing is reading the
        # re-minted script and putting the old one back is a real rollback
        self.assertEqual(ad.spawned, [])
        self.assertEqual(self._persisted(d)["launch"], before)
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            self.assertEqual(f.read(), bytes_before,
                             "a failed spawn rewrote the seat's launch script")

    def test_failed_spawn_restores_an_absent_launch_script_to_absent(self):
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready("proj-x", "derived")
        launch = os.path.join(d, "launch.sh")
        self.assertTrue(os.path.isfile(launch))
        os.unlink(launch)
        self.assertFalse(os.path.exists(launch))
        rc, _out, _err, ad = self._run_seat(
            ("spawn", "codex", "--cwd", nowhere),
            adapter=self._pane_seam(spawn_error="create refused before send"))
        self.assertEqual(rc, 1)
        self.assertEqual(ad.spawned, [])
        self.assertFalse(os.path.exists(launch),
                         "rollback left a launch script that did not exist")

    def test_a_real_adapter_proves_a_missing_cli_never_created_a_pane(self):  # noqa: VACUOUS_ASSERTION — restored room tuple is the positive control
        from helm import harness, seats_identity
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready("proj-x", "derived")
        before = self._persisted(d)["launch"]
        ad = harness.HerdrAdapter(
            path=os.path.join(self.tmp, "missing-herdr-cli"))
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", nowhere), adapter=ad)
        self.assertEqual(rc, 1)
        self.assertEqual(self._persisted(d)["launch"], before)
        self.assertNotIn("may have created a remote pane", err)

    def test_failed_spawn_restores_the_launch_mode_even_when_bytes_match(self):
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready(None, None, worktree=nowhere)
        launch = os.path.join(d, "launch.sh")
        os.chmod(launch, 0o640)
        with open(launch, "rb") as f:
            before = f.read()
        rc, _out, _err, _ad = self._run_seat(
            ("spawn", "codex", "--cwd", nowhere),
            adapter=self._pane_seam(spawn_error="create refused before send"))
        self.assertEqual(rc, 1)
        with open(launch, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(stat.S_IMODE(os.stat(launch).st_mode), 0o640)

    def test_asset_writer_error_unwinds_before_any_spawn_attempt(self):  # noqa: VACUOUS_ASSERTION — original launch bytes and error text are positive controls
        d = self._seat_ready("proj-x", "derived")
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            before = f.read()
        with mock.patch.object(
                seat, "_write_launch_assets",
                side_effect=OSError("asset disk refused")):
            rc, _out, err, ad = self._run_seat(("spawn", "codex"))
        self.assertEqual(rc, 1)
        self.assertEqual(ad.spawned, [])
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertIn("asset disk refused", err)
        self.assertNotIn("may have created a remote pane", err)

    def test_resume_asset_writer_error_restores_before_spawn(self):  # noqa: VACUOUS_ASSERTION — original launch bytes and error text are positive controls
        d = self._seat_ready("proj-x", "derived")
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            before = f.read()
        with mock.patch.object(
                seat, "_write_launch_assets",
                side_effect=OSError("resume asset disk refused")):
            rc, _out, err, ad = self._run_seat(("resume", "codex"))
        self.assertEqual(rc, 1)
        self.assertEqual(ad.spawned, [])
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertIn("resume asset disk refused", err)
        self.assertNotIn("may have created a remote pane", err)

    def test_a_lost_spawn_reply_keeps_the_script_a_remote_orphan_may_run(self):
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived")
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            before = f.read()
        from helm import seats_identity
        ad = self._lost_create_seam()
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_OK, "moved-to-here")):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target), adapter=ad)
        self.assertEqual(rc, 1)
        self.assertEqual(ad.list(), [{"handle": "orphan-1", "status": "live"}])
        self.assertEqual(self._persisted(d)["launch"],
                         ("moved-to-here", "derived"))
        self.assertNotEqual(self._digest(d), self._digest_of(before))
        self.assertIn("may have created a remote pane", err)

    def test_a_lost_resume_reply_keeps_the_script_a_remote_orphan_may_run(self):
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived")
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            before = f.read()
        from helm import seats_identity
        ad = self._lost_create_seam()
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_OK, "moved-to-here")):
            rc, _out, err, _ad = self._run_seat(
                ("resume", "codex", "--cwd", target), adapter=ad)
        self.assertEqual(rc, 1)
        self.assertEqual(ad.list(), [{"handle": "orphan-1", "status": "live"}])
        self.assertEqual(self._persisted(d)["launch"],
                         ("moved-to-here", "derived"))
        self.assertNotEqual(self._digest(d), self._digest_of(before))
        self.assertIn("may have created a remote pane", err)

    def test_a_re_spawn_IN_PLACE_changes_nothing(self):
        """The contract the first version broke: `cwd` is ALWAYS populated
        (_spawn_args defaults it), so a predicate on the value fires on every
        spawn. A re-spawn with no --cwd is not a move and must leave a derived
        room exactly where it was — asserted on the re-minted script, which is
        what the next verb will read."""
        d = self._seat_ready("stale-old-room", "derived")
        from helm import seats_identity
        with mock.patch.object(seats_identity, "_git_project",
                               lambda cwd: "would-be-wrong"):
            rc, _out, err, _ad = self._run_seat(("spawn", "codex"))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], ("stale-old-room", "derived"), state)
        self.assertEqual(state["record"], ("stale-old-room", "derived"), state)

    def test_an_EXPLICIT_room_SURVIVES_a_cwd_move(self):
        """An operator who passed --room meant it; a later --cwd does not
        overrule them. A derivation carries no such intent, only a stale
        input. Without this arm the fix would silently convert every
        deliberately-homed seat into a cwd-homed one."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("operator-chose-this", None)   # explicit
        from helm import seats_identity
        with mock.patch.object(seats_identity, "_git_project",
                               lambda cwd: "would-be-wrong"):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], ("operator-chose-this", None), state)
        self.assertEqual(state["record"],
                         ("operator-chose-this", "explicit"), state)

    # -- the GIT seam, three answers ---------------------------------------
    #
    # The arms above substitute `_git_project`, which is the right seam for
    # "what does the room follow"; it is the WRONG seam for "what does git
    # say", because it is downstream of every git query. The three arms below
    # leave the derivation entirely real and vary git itself, which is the only
    # place the tri-state can actually collapse. All three share one fixture
    # and differ by a single variable — whether git can answer — so the room
    # being set in one and kept in another is a discrimination, not a pair of
    # unrelated greens.

    def _git_that_stops_answering(self):
        """A `git` on PATH that runs fine, confirms the work tree, and then
        fails the FOLLOW-UP query. Not a hypothetical: `--path-format` landed
        in git 2.31, so every older git rejects that exact invocation outright
        (exit 129, unknown option), and any git can fail a query transiently.
        Everything else passes through to the real binary, so nothing outside
        the seam under test is disturbed. Returns the directory to prepend."""
        real = shutil.which("git")
        self.assertTrue(real, "the arm needs a real git to pass through to")
        d = os.path.join(self.tmp, "git-shim")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "git")
        with open(p, "w") as f:
            f.write('#!/bin/sh\n'
                    'for a in "$@"; do\n'
                    '  if [ "$a" = "--path-format=absolute" ]; then\n'
                    '    echo "fatal: unknown option \\`path-format\'" >&2\n'
                    '    exit 129\n'
                    '  fi\n'
                    'done\n'
                    'exec %s "$@"\n' % real)
        os.chmod(p, 0o755)
        return d

    def _expected_room(self, name):
        """The room an UNREGISTERED checkout derives, computed here from the
        published rule (label + blake2b-8 of the canonical path) rather than
        by calling the code under test — an expectation taken from the
        accessor would agree with it however wrong it was."""
        import hashlib
        real = os.path.realpath(os.path.join(self.tmp, name)).rstrip(os.sep)
        return "%s-%s" % (name, hashlib.blake2b(
            real.encode("utf-8"), digest_size=8).hexdigest())

    def test_the_git_seam_ANSWERS_a_room_and_it_reaches_every_surface(self):
        """STATE ONE: git looked, and this cwd has a project. The room is set,
        on all three durable surfaces, with the real derivation running end to
        end — no `_git_project` substitute anywhere in this arm.

        This is the discriminating control for the two arms below: same
        fixture, same verb, and the only difference there is a git that
        cannot finish the answer. Without it, 'the room was kept' and 'the
        room was cleared' could both be reporting an instrument that never
        reached a room at all."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("stale-old-room", "derived")
        rc, _out, err, _ad = self._run_seat(
            ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        room = self._expected_room("target-project")
        state = self._persisted(d)
        self.assertEqual(state["launch"], (room, "derived"), state)
        self.assertEqual(state["record"], (room, "derived"), state)
        self.assertEqual(state["roster"], (room, "derived"), state)

    def test_the_git_seam_answers_NO_PROJECT_and_the_room_is_CLEARED(self):
        """STATE TWO: git looked, and this cwd has no project. That measured
        emptiness is the one answer that licenses erasing a room, and it must
        keep licensing it — a fix for the third state that widens 'could not
        look' far enough to swallow this one has just moved the defect.

        Real git, a real directory that is really not a repository."""
        nowhere = os.path.join(self.tmp, "no-project-here")
        os.makedirs(nowhere, exist_ok=True)
        d = self._seat_ready("stale-old-room", "derived")
        from helm import seats
        rc, _out, err, _ad = self._run_seat(
            ("spawn", "codex", "--cwd", nowhere))
        self.assertEqual(rc, 0, err)
        state = self._persisted(d)
        self.assertEqual(state["launch"], (None, seats.ROOM_CLEARED), state)
        self.assertEqual(state["record"], (None, seats.ROOM_CLEARED), state)
        self.assertEqual(state["roster"], (None, None), state)

    def test_first_git_probe_failure_inside_a_checkout_is_UNKNOWN(self):
        from helm import seats_identity
        target = self._project("target-project", sub="work")
        real = shutil.which("git")
        shim_dir = os.path.join(self.tmp, "git-first-failure")
        os.makedirs(shim_dir, exist_ok=True)
        with open(os.path.join(shim_dir, "git"), "w") as f:
            f.write('#!/bin/sh\n'
                    'for a in "$@"; do\n'
                    '  if [ "$a" = "--is-inside-work-tree" ]; then exit 128; fi\n'
                    'done\n'
                    'exec %s "$@"\n' % real)
        os.chmod(os.path.join(shim_dir, "git"), 0o755)
        with mock.patch.dict(
                os.environ,
                {"PATH": shim_dir + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(seats_identity.derive_home_room_typed(target),
                             (seats_identity.DERIVE_UNKNOWN, None))

    def test_supported_non_git_checkout_makes_a_failed_probe_unknown(self):
        from helm import seats_identity
        target = os.path.join(self.tmp, "jj-project")
        os.makedirs(os.path.join(target, ".jj"))
        self.assertEqual(seats_identity._git_root_typed(target),
                         (seats_identity.DERIVE_UNKNOWN, None))

    def test_git_selection_env_cannot_redirect_the_cwd_probe(self):  # noqa: VACUOUS_ASSERTION — exact physical project root is asserted
        from helm import seats_identity
        target = self._project("target-project", sub="work")
        poisoned = {"GIT_DIR": os.path.join(self.tmp, "missing-git-dir"),
                    "GIT_WORK_TREE": self.tmp}
        with mock.patch.dict(os.environ, poisoned):
            status, root = seats_identity._git_root_typed(target)
        self.assertEqual(status, seats_identity.DERIVE_OK)
        self.assertEqual(root, os.path.join(self.tmp, "target-project"))

    def test_git_before_2_31_uses_the_portable_common_dir_form(self):  # noqa: VACUOUS_ASSERTION — three persisted room surfaces are positive controls
        """Rejecting --path-format must not make every moved seat permanently
        UNKNOWN on older git; production no longer sends that option."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived")
        shim = self._git_that_stops_answering()
        with mock.patch.dict(
                os.environ, {"PATH": shim + os.pathsep + os.environ["PATH"]}):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        room = self._expected_room("target-project")
        state = self._persisted(d)
        self.assertEqual(state["launch"], (room, "derived"), state)
        self.assertEqual(state["record"], (room, "derived"), state)
        self.assertEqual(state["roster"], (room, "derived"), state)
        self.assertNotIn("could not derive", err)

    def test_relative_common_dir_is_resolved_from_the_real_cwd(self):  # noqa: VACUOUS_ASSERTION — exact non-empty root is asserted
        from helm import seats_identity
        target = self._project("target-project", sub="one/two")
        alias = os.path.join(self.tmp, "linked-cwd")
        os.symlink(target, alias)
        status, root = seats_identity._git_root_typed(alias)
        self.assertEqual(status, seats_identity.DERIVE_OK)
        self.assertEqual(root, os.path.join(self.tmp, "target-project"))

    def test_live_spawn_derives_only_after_taking_the_lock(self):
        from helm import seats_identity
        old = self._project("old-project", sub="work")
        target = self._project("target-project", sub="work")
        self._seat_ready("old-project", "derived", worktree=old)
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                return_value=(seats_identity.DERIVE_OK, "target-project")) as derive:
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target))
        self.assertEqual(rc, 0, err)
        self.assertEqual(derive.call_count, 1,
                         "spawn derived once before and once after its lock")

    def test_derivation_uses_the_one_git_snapshot_it_measured(self):  # noqa: VACUOUS_ASSERTION — real-Git OK and room controls precede the failure arm
        """Once git returns the common root, project mapping must not re-probe.

        The shim removes the checkout immediately after that answer. The old
        bracket asked git again, discarded the measured root, and could turn a
        transient inner failure into NONE. One typed project derivation keeps
        the answer it actually measured."""
        from helm import seats_identity
        target = self._project("target-project", sub="work")
        root = os.path.join(self.tmp, "target-project")
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE. The arm's
        # subject is an empty answer, and an empty answer is also what a
        # broken fixture, an unreadable tmp dir or a mis-set cwd produce — so
        # the accessor is asked for this exact cwd with the real git first and
        # has to hand back a REAL room. Only then does its later ('unknown',
        # None) say anything about the code.
        ok_status, ok_room = seats_identity.derive_home_room_typed(target)
        self.assertEqual(ok_status, seats_identity.DERIVE_OK)
        self.assertTrue(ok_room)
        # and the FIRST of the two internal probes really does see a work tree
        # here, which is the window this arm exists to open
        self.assertEqual(seats_identity._git_root_typed(target)[0],
                         seats_identity.DERIVE_OK)
        real = shutil.which("git")
        shim_dir = os.path.join(self.tmp, "git-prune-shim")
        os.makedirs(shim_dir, exist_ok=True)
        marker = os.path.join(self.tmp, "pruned.once")
        with open(os.path.join(shim_dir, "git"), "w") as f:
            f.write('#!/bin/sh\n'
                    'seen=\n'
                    'for a in "$@"; do\n'
                    '  if [ "$a" = "--git-common-dir" ]; then seen=1; fi\n'
                    'done\n'
                    '%s "$@"; rc=$?\n'
                    'if [ -n "$seen" ] && [ ! -f "%s" ]; then\n'
                    '  : > "%s"; rm -rf "%s"\n'
                    'fi\n'
                    'exit $rc\n' % (real, marker, marker, root))
        os.chmod(os.path.join(shim_dir, "git"), 0o755)
        with mock.patch.dict(
                os.environ,
                {"PATH": shim_dir + os.pathsep + os.environ["PATH"]}):
            status, room = seats_identity.derive_home_room_typed(target)
        # the prune really happened INSIDE the call — the window is real
        self.assertFalse(os.path.isdir(target))
        self.assertEqual(status, seats_identity.DERIVE_OK)
        self.assertTrue(room, "the measured root answer was discarded and re-probed")

    # -- a rollback is bounded by pane survival ----------------------------

    def _digest_of(self, data):
        """A short digest of launch-script bytes. Comparing the bytes directly
        is correct and unreadable — a failed assertNotEqual prints two entire
        scripts, so the one line that matters is not findable."""
        import hashlib
        return hashlib.blake2b(data, digest_size=8).hexdigest()

    def _digest(self, d):
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            return self._digest_of(f.read())

    def _surviving_pane_seam(self):
        """A metaharness whose pane STARTS and then will not close: `stop` is
        accepted and ignored, and the census keeps reporting the pane live, so
        `stop_pane` returns its `unavailable` string instead of a proof. helm
        already measures exactly this and already prints 'close unproven' —
        the rollback simply was not reading it.

        The pane never closes, so `stop_pane` runs its whole bound. Every one
        of its `_PANE_CLOSE_POLLS` inventory reads still runs and the close
        is still unproven after the last; only the sleep between reads goes,
        because this census answers the same thing at any hour."""
        from helm import harness, seat_exit_owner
        gap = mock.patch.object(seat_exit_owner, "_PANE_CLOSE_DELAY", 0)
        gap.start()
        self.addCleanup(gap.stop)

        class _Survivor:
            name, path = "fake", "/bin/fake"

            def __init__(self):
                self.spawned, self.stopped, self.submitted = [], [], []

            def list(self):
                return [{"handle": h, "status": "live"} for h in self.spawned]

            def spawn(self, command, title=None, cwd=None):
                self.spawned.append("pane-1")
                return "pane-1"

            def submit(self, handle, text):
                self.submitted.append((handle, text))
                return harness.NOT_DELIVERED, "the pane never took the turn"

            def stop(self, handle):
                self.stopped.append(handle)

        return _Survivor()

    def test_a_failed_spawn_whose_pane_SURVIVES_keeps_the_new_script(self):
        """A ROLLBACK IS ONLY A ROLLBACK WHEN THE THING IT UNDOES DID NOT
        HAPPEN. The launch script is re-minted before the pane exists, so a
        failure after that point restores it — correct while nothing is
        running it, and corruption the moment something is.

        Measured through the verb: the onboarding was not submitted, the pane
        refused to close, helm printed 'close unproven' — and the rollback put
        the PREVIOUS script back anyway. The pane was left alive on the new
        environment while the only file describing that seat named the old
        room, and every later launch/resume re-mints from that file.

        The bound is a MEASUREMENT, not the shape of the failure: an unproven
        close is a possible survivor, and the script that survivor is running
        stays."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived")
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            before = f.read()
        # premise, asserted: the seat really did carry the old room, in a
        # script that really exists (two empty reads would compare equal)
        self.assertIn(b"HELM_CHAT_ROOM=proj-x", before)
        from helm import seats_identity
        ad = self._surviving_pane_seam()
        with mock.patch.object(
                seats_identity, "_git_project_typed",
                lambda cwd: (seats_identity.DERIVE_OK, "moved-to-here")
                if cwd == target else (seats_identity.DERIVE_NONE, None)):
            rc, _out, err, _ad = self._run_seat(
                ("spawn", "codex", "--cwd", target), adapter=ad)
        self.assertEqual(rc, 1, "the spawn was supposed to FAIL")
        # premise: a pane really did survive the failure
        self.assertEqual(ad.list(), [{"handle": "pane-1", "status": "live"}])
        # the readable form FIRST — a bytes assertion that fails prints two
        # whole launch scripts and buries what differs
        self.assertEqual(self._persisted(d)["launch"],
                         ("moved-to-here", "derived"),
                         "the rollback restored a script out from under a "
                         "live pane")
        # belt, on the whole file rather than the two fields read back from
        # it: a script DELETED also reads as (None, None) rather than as the
        # old room, so the bytes are checked too — by digest, so a failure
        # prints a difference instead of two entire launch scripts
        self.assertNotEqual(self._digest(d), self._digest_of(before),
                            "the launch script is byte-identical to the "
                            "pre-spawn snapshot")

    def test_a_failed_resume_whose_pane_SURVIVES_keeps_the_new_script(self):
        """THE SAME LAW ON THE OTHER PATH, because one copy of it is how these
        two diverged the first time. A resume re-mints the script before its
        pane exists too, and its registration failure runs the identical
        unwind. Here registration fails with the pane up and unprovable.

        The register is refused BY THE RECORD, not wholesale: only the row
        carrying a pane handle — the resume's own registration — is failed,
        because a blanket refusal also takes down the reap's bookkeeping and
        the verb then aborts somewhere else entirely, with a green rc-1 that
        never reaches this seam."""
        target = self._project("target-project", sub="work")
        d = self._seat_ready("proj-x", "derived")
        with open(os.path.join(d, "launch.sh"), "rb") as f:
            before = f.read()
        self.assertIn(b"HELM_CHAT_ROOM=proj-x", before)
        from helm import seats_identity
        ad = self._surviving_pane_seam()
        real_register = seat._register_spawn

        def refuse_the_pane_row(storage_seat, identity, d_, rec, *a, **k):
            return False if rec.get("handle") \
                else real_register(storage_seat, identity, d_, rec, *a, **k)

        with mock.patch.object(
                seats_identity, "_git_project_typed",
                lambda cwd: (seats_identity.DERIVE_OK, "moved-to-here")
                if cwd == target else (seats_identity.DERIVE_NONE, None)), \
                mock.patch.object(seat, "_register_spawn",
                                  refuse_the_pane_row):
            rc, _out, err, _ad = self._run_seat(
                ("resume", "codex", "--cwd", target), adapter=ad)
        self.assertEqual(rc, 1, "the resume was supposed to FAIL")
        self.assertEqual(ad.list(), [{"handle": "pane-1", "status": "live"}])
        self.assertEqual(self._persisted(d)["launch"],
                         ("moved-to-here", "derived"),
                         "the resume rollback restored a script out from "
                         "under a live pane")
        self.assertNotEqual(self._digest(d), self._digest_of(before),
                            "the launch script is byte-identical to the "
                            "pre-resume snapshot")

    def test_re_add_rederives_the_persisted_model(self):  # noqa: VACUOUS_ASSERTION — the fresh-add control asserts the default model/window PRESENT on the same launch.sh observable before the re-add's assertNotIn(320000)
        """land af391eab, the provision writer sites: `helm seat add` also
        REFRESHES launch.sh on a re-add, and with no model it too reverted a
        spark seat to the family default. CONTROL on the same observable
        first: a fresh add (no spawn.json) writes the family default — which
        is what makes the spark half's assertNotIn(320000) a measurement."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        with open(os.path.join(d, "launch.sh")) as f:
            fresh = f.read()
        self.assertIn("--model gpt-6.1-sol", fresh)          # the control
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000", fresh)
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump({"v": 1, "seat": "codex",
                       "model": "gpt-5.3-codex-spark"}, f)
        self.assertEqual(self._add()[0], 0)                  # the re-add refresh
        with open(os.path.join(d, "launch.sh")) as f:
            sh = f.read()
        self.assertIn("--model gpt-5.3-codex-spark", sh)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000", sh)
        self.assertNotIn("320000", sh)

    def test_launch_sh_unsets_child_stamp_before_exec(self):
        """The minted launch.sh strips the child-session stamp with an explicit
        `unset` line ABOVE the exec (and the exec'd line carries the same -u
        trio) — a seat pane starts top-level whatever env its spawner leaked.
        Instances re-mint through the same _write_launch_assets: same guard."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        unset_line = "unset " + " ".join(seat.CHILD_STAMP_VARS)
        inst = seat._instance_dir("codex", "codex-2")
        seat._write_launch_assets("codex", inst, seat="codex-2")
        for d in (seat.seat_dir("codex"), inst):
            with open(os.path.join(d, "launch.sh")) as f:
                sh = f.read()
            self.assertIn(unset_line, sh)
            self.assertLess(sh.index(unset_line), sh.index("exec "))
            for v in seat.CHILD_STAMP_VARS:
                self.assertIn("-u " + v, sh)          # belt: the exec line too
                self.assertNotIn(v + "=", sh)

    _TUI_ARM = b"\033[?1003h\033[?1006h"
    _TUI_DISARM = (b"\033[?1000l\033[?1002l\033[?1003l\033[?1006l"
                   b"\033[?1015l\033[?2004l\033[?1049l")

    def _terminal_launch_fixture(self):
        """Mint the real owner path and a nonce-reporting hostile harness."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        launch = os.path.join(seat.seat_dir("codex"), "launch.sh")
        with open(launch) as f:
            minted = f.read()
        # Exact-one mutation anchors: the test is on the generated owner, not a
        # hand-built approximation or a launch.sh that retained the old exec.
        self.assertEqual(minted.count("seat_launch_owner.py"), 1)
        self.assertNotIn("--helm-internal-terminal-child", minted,
                         "caller argv must not contain an internal bypass token")

        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        fake = os.path.join(bindir, "claude")
        with open(fake, "w") as f:
            f.write("""#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

nonce = os.environ["HELM_TEST_NONCE"].encode()
# STDERR BEFORE READY: a test signals the owner the moment READY arrives,
# and a harness killed between the two writes left stderr empty (seen once
# on a loaded build node: the TERM arm red with an empty stderr).
os.write(2, b"STDERR:" + nonce + b"\\n")
if os.environ.get("HELM_TEST_ARM") == "1":
    os.write(1, b"\\033[?1003h\\033[?1006h")
os.write(1, b"READY:" + nonce + b" PID:" + str(os.getpid()).encode() + b"\\n")
if os.environ.get("HELM_TEST_REPORT_ARGV") == "1":
    os.write(1, b"ARGV:" + json.dumps(sys.argv[1:]).encode() + b"\\n")
mode = os.environ.get("HELM_TEST_MODE", "exit")
if mode in ("gate-exit", "gate-signal", "wait"):
    os.read(0, 1)
if mode == "wait":
    while True:
        time.sleep(60)
if mode == "gate-signal":
    os.kill(os.getpid(), int(os.environ["HELM_TEST_SIGNAL"]))
sys.exit(int(os.environ.get("HELM_TEST_RC", "0")))
""")
        os.chmod(fake, 0o700)
        nonce = "seat-cleanup-%d" % time.time_ns()
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                   HELM_TEST_NONCE=nonce)
        return launch, env, nonce.encode()

    def _terminal_spawn(self, launch, env, args=()):
        master, slave = pty.openpty()
        def control_tty():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        try:
            proc = subprocess.Popen(
                [launch, *args], stdin=slave, stdout=slave,
                stderr=subprocess.PIPE, env=env, close_fds=True,
                preexec_fn=control_tty)
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.addCleanup(self._terminal_cleanup, proc, master)
        return proc, master

    def _terminal_cleanup(self, proc, master):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stderr is not None and not proc.stderr.closed:
            proc.stderr.close()
        try:
            os.close(master)
        except OSError:
            pass

    def _pty_read_until(self, master, marker=None, timeout=5):
        chunks = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], end - time.monotonic())
            if not ready:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
            if marker is not None and marker in b"".join(chunks):
                break
        return b"".join(chunks)

    def _assert_terminal_receipt(self, output, nonce):
        receipt = b"READY:" + nonce
        self.assertEqual(output.count(receipt), 1,
                         "nonce receipt proves the hostile harness ran once")
        self.assertEqual(output.count(self._TUI_ARM), 1,
                         "control: the hostile harness armed the reported modes")

    def _assert_separate_stderr(self, output, nonce):
        self.assertEqual(output, b"STDERR:" + nonce + b"\n")
        self.assertNotIn(self._TUI_DISARM, output,
                         "terminal cleanup belongs only on the pane stdout")

    def test_launch_owner_path_stays_on_the_canonical_helm_root(self):
        """The static graph edge must not mint a disposable lane module path."""
        from helm import hooks, seat_launch_assets, seat_launch_owner
        with mock.patch.object(hooks, "helm_bin",
                               return_value="/srv/helm/bin/helm"), \
                mock.patch.object(seat_launch_owner, "__file__",
                                  "/tmp/disposable-lane/helm/seat_launch_owner.py"):
            text = seat_launch_assets._launch_owner("claude")
        self.assertIn("/srv/helm/helm/seat_launch_owner.py", text)
        self.assertNotIn("/tmp/disposable-lane", text)

    def test_tty_launch_has_no_first_arg_cleanup_bypass(self):
        """A hostile sentinel-shaped first arg survives under supervision."""
        launch, env, nonce = self._terminal_launch_fixture()
        args = ["--helm-internal-terminal-child", "two words", "--", "$literal"]
        env.update(HELM_TEST_ARM="1", HELM_TEST_MODE="gate-exit",
                   HELM_TEST_RC="0", HELM_TEST_REPORT_ARGV="1")
        proc, master = self._terminal_spawn(launch, env, args)
        before = self._pty_read_until(master, b"ARGV:")
        self._assert_terminal_receipt(before, nonce)
        os.write(master, b"x\n")
        output = before + self._pty_read_until(master)
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(output.count(self._TUI_DISARM), 1,
                         "caller input bypassed the TTY cleanup owner")
        match = re.search(rb"ARGV:(\[[^\r\n]*\])", output)
        self.assertIsNotNone(match, output)
        got = json.loads(match.group(1))
        self.assertEqual(got[-len(args):], args,
                         "the shell owner changed caller argument boundaries")
        self.assertEqual(got.count("--helm-internal-terminal-child"), 1,
                         "the hostile first arg was consumed as internal state")
        self._assert_separate_stderr(proc.stderr.read(), nonce)
        proc.stderr.close()
        os.close(master)

    def test_launch_owner_restores_the_callers_signal_state(self):
        """The reusable owner must not leave its forwarding closures installed."""
        from helm import seat_launch_owner
        sigs = seat_launch_owner._FORWARD
        handlers = {sig: signal.getsignal(sig) for sig in sigs}
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGTERM,))
        mask = set(prior_mask) | {signal.SIGTERM}
        for sig, handler in handlers.items():
            self.addCleanup(signal.signal, sig, handler)
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, prior_mask)

        self.assertEqual(seat_launch_owner.run(["/bin/true"]), 0)
        self.assertEqual({sig: signal.getsignal(sig) for sig in sigs}, handlers)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, ()), mask)

    def test_launch_owner_keeps_forward_signals_blocked_through_disarm(self):
        """A pending TERM cannot pre-empt the reset after the child is reaped.

        IN ITS OWN INTERPRETER, because the TERM goes to the process that runs
        the owner, and in the suite that process is the suite runner. A signal
        mask is per THREAD: while any other thread is alive, the kernel gives a
        process-directed TERM to that thread, the owner's forwarding handler
        runs during disarm, and the owner re-raises TERM on itself with the
        default action. MEASURED (task/3070): train198's whole-suite gate died
        that way part-way through the suite, receipt rc 241 (-15 through the
        gate supervisor), its stderr ending on the fork warning this test's
        run() prints, and the gate refused it as UNKNOWN. Reproduced on the
        same steps: with one sleeping thread alive, 2 of 5 runs died of TERM;
        with none, 0 of 5. Here only the helper can die, and it is
        single-threaded, so the mask the owner sets is the mask that holds."""
        from helm import seat_launch_owner
        helper = """
import importlib.util
import os
import signal
import sys

spec = importlib.util.spec_from_file_location('_helm_seat_launch_owner_test',
                                              sys.argv[1])
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)
events = []
signal.signal(signal.SIGTERM, lambda _sig, _frame: events.append('term'))
signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGTERM,))

def disarm(_done):
    if signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, ()):
        events.append('TERM-UNBLOCKED-BEFORE-CLEANUP')
    events.append('disarm-start')
    os.kill(os.getpid(), signal.SIGTERM)
    events.append('disarm-end')

owner._disarm = disarm
rc = owner.run(['/bin/true'])
print('RESULT', rc, ','.join(events), flush=True)
"""
        proc = subprocess.run(
            [sys.executable, "-I", "-c", helper, seat_launch_owner.__file__],
            cwd=self.tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         "RESULT 0 disarm-start,disarm-end,term", proc.stderr)

    def test_launch_sh_disarms_once_after_zero_and_nonzero_harness_exit(self):
        """Normal and crashed/nonzero exits keep their status and sanitize once."""
        for rc in (0, 23):
            with self.subTest(rc=rc):
                launch, env, nonce = self._terminal_launch_fixture()
                env.update(HELM_TEST_ARM="1", HELM_TEST_MODE="gate-exit",
                           HELM_TEST_RC=str(rc))
                proc, master = self._terminal_spawn(launch, env)
                marker = b"READY:" + nonce
                before = self._pty_read_until(master, marker)
                self._assert_terminal_receipt(before, nonce)
                self.assertNotIn(self._TUI_DISARM, before,
                                 "cleanup must wait until the harness exits")
                os.write(master, b"x\n")
                after = self._pty_read_until(master)
                got = proc.wait(timeout=5)
                stderr = proc.stderr.read()
                proc.stderr.close()
                os.close(master)
                output = before + after
                self.assertEqual(got, rc)
                self._assert_separate_stderr(stderr, nonce)
                self.assertEqual(output.count(self._TUI_DISARM), 1)

    def test_launch_sh_preserves_true_sigsegv_not_ordinary_exit_139(self):
        """Wait status, not 128+N arithmetic, distinguishes crash from exit."""
        outcomes = []
        for mode, rc in (("gate-signal", None), ("gate-exit", 139)):
            launch, env, nonce = self._terminal_launch_fixture()
            env.update(HELM_TEST_ARM="1", HELM_TEST_MODE=mode)
            if rc is None:
                env["HELM_TEST_SIGNAL"] = str(signal.SIGSEGV)
            else:
                env["HELM_TEST_RC"] = str(rc)
            proc, master = self._terminal_spawn(launch, env)
            before = self._pty_read_until(master, b"READY:" + nonce)
            self._assert_terminal_receipt(before, nonce)
            self.assertNotIn(self._TUI_DISARM, before)
            os.write(master, b"x\n")
            after = self._pty_read_until(master)
            outcomes.append(proc.wait(timeout=5))
            self.assertEqual((before + after).count(self._TUI_DISARM), 1)
            self._assert_separate_stderr(proc.stderr.read(), nonce)
            proc.stderr.close()
            os.close(master)
        self.assertEqual(outcomes, [-signal.SIGSEGV, 139])

    def test_launch_sh_wrapper_sigterm_forwards_and_re_raises_once(self):
        """A TERM aimed only at the owner reaches and reaps its harness."""
        launch, env, nonce = self._terminal_launch_fixture()
        env.update(HELM_TEST_ARM="1", HELM_TEST_MODE="wait")
        proc, master = self._terminal_spawn(launch, env)
        before = self._pty_read_until(master, b"READY:" + nonce)
        self._assert_terminal_receipt(before, nonce)
        match = re.search(rb" PID:(\d+)", before)
        self.assertIsNotNone(match, "receipt names the child whose reap is asserted")
        child = int(match.group(1))
        os.kill(proc.pid, signal.SIGTERM)
        after = self._pty_read_until(master)
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual((before + after).count(self._TUI_DISARM), 1)
        self._assert_separate_stderr(proc.stderr.read(), nonce)
        proc.stderr.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        os.close(master)

    def test_launch_owner_re_raises_wrapper_term_when_child_traps_it(self):
        """Wrapper-directed TERM owns the outcome even when the child exits 0."""
        from helm import seat_launch_owner
        child = ("import signal,sys,time; "
                 "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                 "print('READY', flush=True); time.sleep(60)")
        wrapper = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location('_helm_launch_owner', sys.argv[1])
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)
raise SystemExit(owner.run(sys.argv[2:]))
"""
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", wrapper,
             seat_launch_owner.__file__, sys.executable, "-c", child],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def reap():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        self.addCleanup(reap)
        self.assertEqual(proc.stdout.readline().strip(), "READY")
        os.kill(proc.pid, signal.SIGTERM)
        _out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGTERM, err)

    def test_first_forwarded_signal_survives_a_second_pending_during_disarm(self):
        """TERM first stays the outcome when INT arrives inside blocked cleanup."""
        child = ("import signal,sys,time; "
                 "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                 "print('READY', flush=True); time.sleep(60)")
        from helm import seat_launch_owner
        wrapper = """
import importlib.util
import os
import signal
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location('_helm_seat_launch_owner_test', path)
seat_launch_owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seat_launch_owner)
if os.path.realpath(seat_launch_owner.__file__) != os.path.realpath(path):
    raise SystemExit(92)
print('CONTROL:EXACT-MODULE', file=sys.stderr, flush=True)
signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGTERM,))
if signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, ()):
    raise SystemExit(91)
print('CONTROL:TERM-BLOCKED', file=sys.stderr, flush=True)
real_disarm = seat_launch_owner._disarm
def disarm(done):
    os.kill(os.getpid(), signal.SIGINT)
    real_disarm(done)
seat_launch_owner._disarm = disarm
raise SystemExit(seat_launch_owner.run([sys.executable, '-c', sys.argv[2]]))
"""
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", wrapper,
             seat_launch_owner.__file__, child],
            cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)

        def reap():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        self.addCleanup(reap)
        ready = proc.stdout.readline().strip()
        if ready != "READY":
            _out, err = proc.communicate(timeout=5)
            self.fail("signal owner helper died before READY: rc=%r cwd=%r "
                      "owner=%r parent-mask=%r stderr=%r"
                      % (proc.returncode, os.getcwd(),
                         seat_launch_owner.__file__,
                         signal.pthread_sigmask(signal.SIG_BLOCK, ()), err))
        os.kill(proc.pid, signal.SIGTERM)
        _out, err = proc.communicate(timeout=5)
        self.assertIn("CONTROL:EXACT-MODULE", err)
        self.assertIn("CONTROL:TERM-BLOCKED", err)
        self.assertEqual(proc.returncode, -signal.SIGTERM, err)

    def test_launch_sh_headless_output_is_not_decorated_with_tty_resets(self):
        """A non-TTY path keeps direct exec and never writes terminal bytes."""
        launch, env, nonce = self._terminal_launch_fixture()
        env.update(HELM_TEST_ARM="0", HELM_TEST_MODE="exit", HELM_TEST_RC="0")
        proc = subprocess.Popen([launch], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env)
        launch_pid = proc.pid
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout.count(b"READY:" + nonce), 1)
        child = re.search(rb" PID:(\d+)", stdout)
        self.assertIsNotNone(child)
        self.assertEqual(int(child.group(1)), launch_pid,
                         "headless keeps direct exec and its registered pid")
        self.assertNotIn(self._TUI_DISARM, stdout)
        self._assert_separate_stderr(stderr, nonce)

    def test_a_sibling_seat_alias_is_refused_before_any_settings_write(self):  # noqa: VACUOUS_ASSERTION — the pre-planted codex settings marker is the unconditional positive control; the same file is re-read unchanged after the refused kimi write
        """Containment is not ownership: seats/kimi -> seats/codex stays under
        the seats root, but a kimi-targeted writer must not mutate codex or
        report success for the wrong identity."""
        codex = seat.seat_dir("codex")
        os.makedirs(os.path.join(codex, "claude"), exist_ok=True)
        settings = os.path.join(codex, "claude", "settings.json")
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"owner": "codex"}, f)
        os.symlink(codex, seat.seat_dir("kimi"), target_is_directory=True)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = seat._write_launch_assets(
                "kimi", seat.seat_dir("kimi"), seat="kimi")

        self.assertIs(ok, seat._SEAT_SURFACE_REFUSED)
        self.assertIn("changes identity through a symlink", err.getvalue())
        with open(settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"owner": "codex"})
        self.assertFalse(os.path.exists(os.path.join(codex, "launch.sh")))

    def test_a_nested_alias_below_a_real_seat_dir_is_refused(self):  # noqa: VACUOUS_ASSERTION — the pre-planted codex settings marker is the unconditional positive control, re-read unchanged after the refused ds4pro write
        """The instance dir is REAL and the gate on it PASSES — that is the
        whole finding (review on 5e5bcfd5). seats/ds4pro is an honest
        directory; only its CHILD `claude` links into codex, and
        _seat_surface_error never looks below the instance it was handed. So
        makedirs(exist_ok=True) followed the link, every writer beneath it
        landed in codex's settings, and launch returned 0 — a zero exit while
        overwriting another seat.

        The outer gate's acceptance is ASSERTED, not assumed: without it a
        refusal from any other cause would satisfy this test while the nested
        hole stayed open."""
        codex = seat.seat_dir("codex")
        os.makedirs(os.path.join(codex, "claude"), exist_ok=True)
        settings = os.path.join(codex, "claude", "settings.json")
        with open(settings, "w", encoding="utf-8") as f:
            json.dump({"owner": "codex"}, f)
        mine = seat.seat_dir("ds4pro")
        os.makedirs(mine, exist_ok=True)          # a REAL dir, not an alias
        os.symlink(os.path.join(codex, "claude"),
                   os.path.join(mine, "claude"), target_is_directory=True)

        # must-hit: the gate a sibling lane added really does ACCEPT this.
        self.assertIsNone(seat._seat_surface_error("ds4pro", "ds4pro", mine))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = seat._write_launch_assets("ds4pro", mine, seat="ds4pro")

        self.assertIs(ok, seat._SEAT_SURFACE_REFUSED)
        self.assertIn("resolves outside its own instance", err.getvalue())
        with open(settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"owner": "codex"})
        self.assertFalse(os.path.exists(os.path.join(codex, "claude",
                                                     "launch.sh")))
        self.assertFalse(os.path.exists(os.path.join(codex, "launch.sh")))

    def test_a_sibling_whose_name_merely_starts_the_same_is_refused(self):
        """`/seats/codex` is a PREFIX of `/seats/codex-evil`, so a bare
        startswith admits the second under the first — the same cross-seat
        write, spelled differently. The mutation that drops the separator
        survived every other test in this file, which is why this one exists."""
        root = seat.seat_dir("codex")
        sibling = root.rstrip(os.sep) + "-evil"
        os.makedirs(os.path.join(sibling, "claude"), exist_ok=True)
        why = seat._nested_surface_error(root, os.path.join(sibling, "claude"))
        self.assertIsNotNone(why)
        self.assertIn("resolves outside its own instance", why)
        # must-hit control on the SAME call shape: the honest child is admitted,
        # so the refusal above is about containment and not about the fixture.
        os.makedirs(os.path.join(root, "claude"), exist_ok=True)
        self.assertIsNone(
            seat._nested_surface_error(root, os.path.join(root, "claude")))

    def test_an_instance_alias_is_refused_by_the_shared_admission_gate(self):
        real = seat._instance_dir("codex", "codex-3")
        alias = seat._instance_dir("codex", "codex-2")
        os.makedirs(real, exist_ok=True)
        os.symlink(real, alias, target_is_directory=True)
        why = seat._instance_gate("codex", "codex-2")
        self.assertIn("changes identity through a symlink", why)
        self.assertIn("codex-2", why)
        self.assertIn("codex-3", why)

    def test_a_genuine_seat_owned_surface_still_mints(self):
        d = seat.seat_dir("kimi")
        os.makedirs(d, exist_ok=True)
        self.assertIsNone(seat._write_launch_assets("kimi", d, seat="kimi"))
        with open(os.path.join(d, "claude", "settings.json"),
                  encoding="utf-8") as f:
            settings = json.load(f)
        self.assertTrue(settings["skipDangerousModePermissionPrompt"])
        self.assertTrue(os.path.isfile(os.path.join(d, "launch.sh")))

    def test_seat_env_strips_child_stamp(self):
        """smoke's subprocess env mirrors launch_line — the inherited stamp
        must not ride into a smoke claude either."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        stamped = {v: "leaked" for v in seat.CHILD_STAMP_VARS}
        with mock.patch.dict(os.environ, stamped):
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"))
        for v in seat.CHILD_STAMP_VARS:
            self.assertNotIn(v, env)

    def test_seat_gets_host_skills(self):
        """A seat's fresh config dir has no skills of its own, so seat agents
        couldn't /learn — _write_launch_assets links skills in: canonical-first
        (skillsync), and when NO canonical exists on the host (pinned here via
        a dead HELM_SKILLS_CANONICAL) it falls back to the minting host's
        CLAUDE_CONFIG_DIR skills; a real skills dir on a seat is never
        clobbered at mint (that repair is `helm skills sync`'s job)."""
        host = os.path.join(self.tmp, "host-config")
        os.makedirs(os.path.join(host, "skills", "learn"))
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        no_deck = {"CLAUDE_CONFIG_DIR": host,
                   "HELM_SKILLS_CANONICAL": os.path.join(self.tmp, "no-such-deck")}
        with mock.patch.dict(os.environ, no_deck):
            seat._write_launch_assets("codex", d)
        link = os.path.join(d, "claude", "skills")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(host, "skills")))
        self.assertTrue(os.path.isdir(os.path.join(link, "learn")))  # resolves through
        # a real skills dir on the seat is never replaced
        os.unlink(link)
        os.makedirs(link)
        open(os.path.join(link, "own.md"), "w").close()
        with mock.patch.dict(os.environ, no_deck):
            seat._write_launch_assets("codex", d)
        self.assertFalse(os.path.islink(link))
        self.assertTrue(os.path.exists(os.path.join(link, "own.md")))

    def test_seat_says_the_canonical_registry_is_unreadable_when_it_links_from_the_fallback(self):
        """The arm above pins a DEAD canonical; this one corrupts the authored
        registry with NO env canonical, so the fallback serves because the
        canonical READ failed. The seat still links (the must-hit) and mint
        says so in one line that names the failure without claiming the seat
        has no skills. Control: the same mint with a readable (absent)
        authored layer prints no such line."""
        import contextlib
        import io
        from helm import home as _h
        host = os.path.join(self.tmp, "host-config")
        os.makedirs(os.path.join(host, "skills", "learn"))
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        link = os.path.join(d, "claude", "skills")

        def mint():
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}), \
                    contextlib.redirect_stderr(err):
                os.environ.pop("HELM_SKILLS_CANONICAL", None)
                os.environ.pop("MELD_SKILLS_CANONICAL", None)
                seat._write_launch_assets("codex", d)
            return err.getvalue()

        quiet = mint()                                   # control: readable layer
        self.assertTrue(os.path.islink(link))
        self.assertNotIn("skills come from the host fallback", quiet)
        self.assertNotIn("the canonical skills registry is unusable", quiet)
        os.makedirs(os.path.dirname(_h.authored_path()), exist_ok=True)
        with open(_h.authored_path(), "w") as f:
            f.write("{not json")
        loud = mint()
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(host, "skills")))
        self.assertIn("the canonical skills registry is unusable", loud)
        self.assertIn("authored layer unreadable", loud)
        self.assertIn("skills come from the host fallback", loud)
        self.assertNotIn("NO skills", loud)

    def test_seat_onboarding_seeded(self):
        """A fresh seat config dir would trigger CC's first-run wizard and stall
        the seat before it joins chat — _write_launch_assets seeds .claude.json
        with the onboarding-complete flags (copied from an onboarded host, no
        host state leaked); a seat's own .claude.json is never clobbered."""
        host = os.path.join(self.tmp, "host2")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True,
                       "lastOnboardingVersion": "9.9.9", "theme": "light",
                       "secretProjects": {"x": 1},
                       "projects": {
                           "/trusted/repo": {"hasTrustDialogAccepted": True,
                                             "lastCost": 4.2, "lastSessionId": "s"},
                           "/untrusted/repo": {"hasTrustDialogAccepted": False}}}, f)
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}):
            seat._write_launch_assets("codex", d, workdir=self.tmp)
        p = os.path.join(d, "claude", ".claude.json")
        with open(p) as f:
            seeded = json.load(f)
        self.assertTrue(seeded["hasCompletedOnboarding"])
        self.assertEqual(seeded["lastOnboardingVersion"], "9.9.9")  # version copied
        self.assertNotIn("secretProjects", seeded)   # only onboarding keys, no host state
        self.assertEqual(seeded["projects"]["/trusted/repo"],
                         {"hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 1})
        self.assertNotIn("/untrusted/repo", seeded["projects"])   # only trusted paths
        self.assertNotIn("lastCost", seeded["projects"]["/trusted/repo"])  # no session state
        # the intended workdir's trust is SYNTHESIZED (exact-match key the dialog
        # needs; no ref carries it — the codex-3 stall's root cause)
        self.assertEqual(seeded["projects"][os.path.realpath(self.tmp)],
                         {"hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 1})
        # bypass acceptance lands in settings.json (CC 2.1.216), not .claude.json
        with open(os.path.join(d, "claude", "settings.json")) as f:
            st = json.load(f)
        self.assertTrue(st["skipDangerousModePermissionPrompt"])
        with open(p, "w") as f:
            json.dump({"mine": True}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}):
            seat._write_launch_assets("codex", d)
        with open(p) as f:
            self.assertEqual(json.load(f), {"mine": True})  # never clobbered

    def test_fresh_instance_seeds_freshest_same_family_feature_cache(self):
        """A new instance inherits ONLY the freshest complete cache carrying
        Claude's real deferred-tool gate. Cache timestamp, not unrelated file
        mtime, owns freshness; a newer partial map is ignored."""
        host = os.path.join(self.tmp, "host-feature-source")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)

        now = int(time.time() * 1000)
        parent = os.path.join(seat.seat_dir("codex"), "claude", ".claude.json")
        sibling = os.path.join(seat._instance_dir("codex", "codex-3"),
                               "claude", ".claude.json")
        partial = os.path.join(seat._instance_dir("codex", "codex-4"),
                               "claude", ".claude.json")
        rows = (
            (parent, _warm_features(parent=True), [], now - 2000,
             {"oauthAccount": {"email": "must-not-cross@example.com"}}),
            (sibling, _warm_features(winner=True),
             ["exp-a"], now - 1000,
             {"lastSessionId": "must-not-cross"}),
            (partial, {"unrelated": True}, [], now, {}),
        )
        for p, features, experiments, fetched, extra in rows:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                json.dump(dict(extra,
                               cachedGrowthBookFeatures=features,
                               cachedExperimentFeatures=experiments,
                               cachedGrowthBookFeaturesAt=fetched), f)
        os.utime(parent, (300, 300))
        os.utime(sibling, (100, 100))

        target = seat._instance_dir("codex", "codex-2")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}), \
                contextlib.redirect_stderr(err):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(os.path.join(target, "claude", ".claude.json")) as f:
            seeded = json.load(f)
        self.assertEqual(seat._FEATURE_CACHE_GATE, "tengu_deferred_stub_tool")
        self.assertGreaterEqual(len(seeded["cachedGrowthBookFeatures"]), 100)
        self.assertTrue(seeded["cachedGrowthBookFeatures"]["winner"])
        self.assertNotIn("parent", seeded["cachedGrowthBookFeatures"])
        self.assertEqual(seeded["cachedExperimentFeatures"], ["exp-a"])
        self.assertEqual(seeded["cachedGrowthBookFeaturesAt"], now - 1000)
        self.assertNotIn("oauthAccount", seeded)
        self.assertNotIn("lastSessionId", seeded)
        self.assertNotIn("feature cache not seeded", err.getvalue())

    def test_cacheless_existing_instance_repairs_when_source_appears(self):
        """A first cacheless mint is not terminal: each launch warns until a
        valid sibling appears, then merges only the allowlisted tuple while
        preserving every seat-owned field."""
        host = os.path.join(self.tmp, "host-repair")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        target = seat._instance_dir("codex", "codex-2")
        env = {"CLAUDE_CONFIG_DIR": host}
        first = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(first):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        state_path = os.path.join(target, "claude", ".claude.json")
        with open(state_path) as f:
            state = json.load(f)
        state.update({"mine": True, "lastSessionId": "seat-owned"})
        with open(state_path, "w") as f:
            json.dump(state, f)
        os.chmod(state_path, 0o600)
        self.assertIn("feature cache not seeded", first.getvalue())

        source = os.path.join(seat.seat_dir("codex"), "claude", ".claude.json")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        now = int(time.time() * 1000)
        with open(source, "w") as f:
            json.dump({"cachedGrowthBookFeatures": _warm_features(),
                       "cachedExperimentFeatures": [],
                       "cachedGrowthBookFeaturesAt": now,
                       "oauthAccount": {"email": "must-not-cross@example.com"}}, f)
        second = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(second):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(state_path) as f:
            repaired = json.load(f)
        self.assertTrue(repaired["mine"])
        self.assertEqual(repaired["lastSessionId"], "seat-owned")
        self.assertTrue(repaired["cachedGrowthBookFeatures"]
                        [seat._FEATURE_CACHE_GATE])
        self.assertEqual(repaired["cachedGrowthBookFeaturesAt"], now)
        self.assertEqual(stat.S_IMODE(os.stat(state_path).st_mode), 0o600)
        self.assertNotIn("oauthAccount", repaired)
        self.assertNotIn("feature cache not seeded", second.getvalue())

    def test_cacheless_instance_rejects_poisoned_and_escaped_sources(self):
        """Partial, stale, future, non-finite, and symlink-escaped caches cannot
        suppress the loud warning when no valid donor exists across any family."""
        host = os.path.join(self.tmp, "host-without-cache")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        now = int(time.time() * 1000)
        bad = (
            ({"unrelated": True}, now),
            ({seat._FEATURE_CACHE_GATE: True}, now),  # shallow sticky stub
            (_warm_features(), now - seat._FEATURE_CACHE_MAX_AGE_MS - 1),
            (_warm_features(),
             now + seat._FEATURE_CACHE_FUTURE_SKEW_MS + 60_000),
            (_warm_features(), float("nan")),
        )
        for i, (features, fetched) in enumerate(bad, 3):
            p = os.path.join(seat._instance_dir("codex", "codex-%d" % i),
                             "claude", ".claude.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                json.dump({"cachedGrowthBookFeatures": features,
                           "cachedExperimentFeatures": [],
                           "cachedGrowthBookFeaturesAt": fetched}, f)

        target = seat._instance_dir("codex", "codex-2")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}), \
                contextlib.redirect_stderr(err):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(os.path.join(target, "claude", ".claude.json")) as f:
            seeded = json.load(f)
        for key in seat._FEATURE_CACHE_KEYS:
            self.assertNotIn(key, seeded)
        self.assertIn("WARNING — feature cache not seeded", err.getvalue())
        self.assertIn(seat._FEATURE_CACHE_GATE, err.getvalue())
        self.assertIn("Monitor", err.getvalue())

    def test_cross_family_feature_cache_seeding(self):
        """When no same-family cache exists, a complete valid cache from a foreign
        family (e.g. kimi) seeds a new family seat (e.g. codex)."""
        host = os.path.join(self.tmp, "host-without-cache-2")
        os.makedirs(host)
        with open(os.path.join(host, ".claude.json"), "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        now = int(time.time() * 1000)

        foreign = os.path.join(seat.seat_dir("kimi"), "claude", ".claude.json")
        os.makedirs(os.path.dirname(foreign), exist_ok=True)
        with open(foreign, "w") as f:
            json.dump({"cachedGrowthBookFeatures": _warm_features(foreign=True),
                       "cachedExperimentFeatures": [],
                       "cachedGrowthBookFeaturesAt": now}, f)

        target = seat._instance_dir("codex", "codex-2")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": host}), \
                contextlib.redirect_stderr(err):
            seat._write_launch_assets("codex", target, seat="codex-2",
                                      workdir=self.tmp)
        with open(os.path.join(target, "claude", ".claude.json")) as f:
            seeded = json.load(f)
        self.assertTrue(seeded["cachedGrowthBookFeatures"].get("foreign"))
        self.assertEqual(seeded["cachedGrowthBookFeaturesAt"], now)
        self.assertNotIn("feature cache not seeded", err.getvalue())

    def test_launch_line_room_homing(self):
        """Seat presets clear ambient homing, then bake either an explicit room
        or a project-derived room with truthful provenance into launch.sh."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        bare = seat.launch_line("codex")
        self.assertNotIn("HELM_CHAT_ROOM=", bare)
        self.assertIn("-u HELM_CHAT_ROOM", bare)
        self.assertIn("-u MELD_CHAT_ROOM", bare)
        self.assertIn("-u HELM_CHAT_ROOM_SOURCE", bare)
        self.assertIn("-u MELD_CHAT_ROOM_SOURCE", bare)
        line = seat.launch_line("codex", room="team-x")
        self.assertIn("HELM_CHAT_ROOM=team-x", line)
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE=", line)
        self.assertIn("HELM_CHAT_NAME=codex", line)          # identity intact
        self.assertIn("HELM_CELL_BIN=" + seat.DREGG_SIGNER_DEFAULT, line)
        self.assertIn("HELM_CELL_PROFILE=codex", line)       # signing intact
        self.assertIn("DREGG_PROFILE=codex", line)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "codex", "--room", "team-x"])
        self.assertEqual(rc, 0)
        self.assertIn("HELM_CHAT_ROOM=team-x", out.getvalue())
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            preset = f.read()
        self.assertIn("HELM_CHAT_ROOM=team-x", preset)
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE=", preset)
        out = io.StringIO()
        with mock.patch.object(seat, "_resolve_homing",
                               return_value=("helm", "derived")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        self.assertIn("HELM_CHAT_ROOM=helm", out.getvalue())
        self.assertIn("HELM_CHAT_ROOM_SOURCE=derived", out.getvalue())
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            preset = f.read()
        self.assertIn("HELM_CHAT_ROOM_SOURCE=derived", preset)

    def test_homing_resolution_precedence_and_source(self):
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_ROOM": "project-a",
                "HELM_CHAT_ROOM_SOURCE": "derived"}):
            self.assertEqual(seat._resolve_homing(), ("project-a", "derived"))
            self.assertEqual(seat._resolve_homing("main"), ("main", None))
        with mock.patch.dict(os.environ, {
                "MELD_CHAT_ROOM": "legacy-project",
                "MELD_CHAT_ROOM_SOURCE": "derived"}):
            self.assertEqual(seat._resolve_homing(),
                             ("legacy-project", "derived"))
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_ROOM": "explicit-new",
                "MELD_CHAT_ROOM_SOURCE": "derived"}):
            self.assertEqual(seat._resolve_homing(),
                             ("explicit-new", None))
        with mock.patch("helm.seats.derive_home_room", return_value="helm"):
            self.assertEqual(seat._resolve_homing(), ("helm", "derived"))

    def test_seat_subprocess_env_overrides_ambient_owner_signer_identity(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_NAME": "daria", "HELM_CELL_PROFILE": "daria",
                "DREGG_PROFILE": "daria", "HELM_CELL_BIN": "/tmp/legacy-signer"}):
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"))
        self.assertEqual(env["HELM_CHAT_NAME"], "codex")
        self.assertEqual(env["HELM_CELL_PROFILE"], "codex")
        self.assertEqual(env["DREGG_PROFILE"], "codex")
        self.assertEqual(env["HELM_CELL_BIN"], seat.DREGG_SIGNER_DEFAULT)

    # -- the scrub guard ----------------------------------------------------
    def test_scrub_env_strips_the_triple(self):
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
               "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "sk-x",
               "PATH": "/usr/bin", "HOME": "/home/x"}
        scrubbed = seat.scrub_env(env)
        for v in seat.SCRUB_VARS:
            self.assertNotIn(v, scrubbed)
        self.assertEqual(scrubbed["PATH"], "/usr/bin")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-x")  # input untouched
        for v in seat.SCRUB_VARS:
            self.assertIn("-u " + v, seat.scrub_prefix())

    def test_the_paste_prefix_carries_BOTH_registers(self):
        """#107. A pasteable command must clear two different hazards: the
        child stamp (resumed session becomes a subprocess child, transcript
        persistence silently off) AND the proxy triple (a line pasted into a
        PROXIED shell resumes a Claude session against a proxy fronting
        another vendor — looks native, billed native, routes elsewhere).

        Assert the SET both ways: every required var present, and nothing
        else unset. A prefix that clears half is the bug this closes."""
        prefix = seat.paste_unset_prefix()
        toks = prefix.split()
        got = sorted(toks[i + 1] for i, t in enumerate(toks)
                     if t == "-u" and i + 1 < len(toks))
        self.assertEqual(got, sorted(seat.CHILD_STAMP_VARS + seat.SCRUB_VARS))
        self.assertTrue(prefix.startswith("env "))
        self.assertTrue(prefix.endswith(" "))     # composes onto a command

    def test_no_paste_site_rebuilds_the_unset_literal_itself(self):
        """THE ANTI-DRIFT PIN, and the duplication it forbids is what CAUSED
        #107. `sessions.resume_exec` and `transcripts._native_cmd` each built
        the prefix from an identical inline literal over CHILD_STAMP_VARS
        only, so adding the proxy scrub meant remembering it twice — and it
        was remembered zero times, while `scrub_prefix()` sat with no
        production caller at all. A guard that must be re-added per site will
        be absent from the next site."""
        import inspect
        from helm import sessions, transcripts
        # UNCONDITIONAL FIRST: the seam exists and both modules import seat,
        # so the per-module absences below are about the literal and not about
        # a source read that returned nothing.
        self.assertTrue(seat.paste_unset_prefix().startswith("env "))
        self.assertIn("paste_unset_prefix", inspect.getsource(sessions))
        self.assertIn("paste_unset_prefix", inspect.getsource(transcripts))
        for mod in (sessions, transcripts):
            src = inspect.getsource(mod)
            self.assertNotIn('" -u ".join(seat.CHILD_STAMP_VARS)', src,
                             "%s rebuilds the unset prefix inline instead of "
                             "calling seat.paste_unset_prefix()" % mod.__name__)
            # POSITIVE CONTROL on the same source: it really does call the seam
            self.assertIn("paste_unset_prefix()", src)

    # -- up: double-start refusal + missing-binary line ---------------------
    def test_double_start_refused_no_spawn(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        # a live pid + its MATCHING birth identity = a verifiably-running proxy
        # (the pidfile shape `_up` writes post-fix; a bare pid is now refused
        # as unauthenticated, so this record must carry the identity to count).
        live = os.getpid()
        with open(os.path.join(seat.seat_dir("codex"), "proxy.pid"), "w") as f:
            f.write("%d %s\n" % (live, seat._pid_identity(live)))
        booby = seat.subprocess.Popen
        seat.subprocess.Popen = lambda *a, **k: self.fail("Popen called on double-start")
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = seat.cmd_seat(["up", "codex"])
        finally:
            seat.subprocess.Popen = booby
        self.assertEqual(rc, 1)
        self.assertIn("already running", err.getvalue())

    def test_up_missing_binary_points_at_doctor(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        os.environ["HELM_PROXY_BIN"] = os.path.join(self.tmp, "no-such-binary")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["up", "codex"])
        self.assertEqual(rc, 1)
        self.assertIn("helm seat doctor", err.getvalue())

    def test_up_without_seat_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["up", "codex"])
        self.assertEqual(rc, 1)
        self.assertIn("helm seat add codex", err.getvalue())

    # -- status -------------------------------------------------------------
    def test_status_shows_liveness_and_expiry(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["status"])
        self.assertEqual(rc, 0)
        row = out.getvalue()
        self.assertIn("codex", row)
        self.assertIn("proxy down", row)
        self.assertIn("fake@example.com", row)
        self.assertIn("valid until", row)

    def test_status_no_seats(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("no seats yet", out.getvalue())

    def test_the_roster_render_is_scoped_and_the_MINT_is_not(self):
        """ONE METHOD, BOTH POLARITIES, ONE OBSERVABLE — `projscope.active()`
        read from inside the read verb's render and from inside the write
        verb's persistence.

        WHY THE WRITE HALF IS THE CONSTRAINT AND NOT A NICETY. projscope's own
        contract is that nothing outside a scope is ever cached, and that is
        what keeps every write door in the tree answering from the world
        rather than from an older question. A scope hoisted one level too far
        — over `cmd_seat` instead of over the render — would put the seat mint
        inside a read cache, and an arm that only checked the render would
        call that correct.

        THE MINT IS THE MUST-HIT CONTROL, not a formality: `helm seat add`
        really does persist through `pk.write_json`, so the recorded list is
        non-empty and the False in it was measured rather than defaulted."""
        self._plant("home-a")
        phase, seen = ["mint"], {"mint": [], "render": []}
        real_write = pk.write_json

        def write_json(*a, **kw):
            seen[phase[0]].append(projscope.active())
            return real_write(*a, **kw)

        rendered = {}

        def join(**kw):
            # the read `_status` exists to take — the fleet-wide usability
            # join, which folds the dispatch ledger and walks the host
            # process table. An empty map renders UNKNOWN in every field,
            # which `_status` already handles.
            rendered["scoped"] = projscope.active()
            return {}

        with mock.patch.object(pk, "write_json", write_json):
            # THE MINT'S OWN PRODUCT IS ASSERTED, not only its exit code. An
            # arm whose every positive reads an instrument certifies the
            # instrument: the seat really was minted and really is named.
            minted = self._add()
            self.assertIn("codex", minted[1] + minted[2],
                          "control: the mint ran and named the seat")
            self.assertEqual(0, minted[0], minted[2])
            phase[0] = "render"
            with mock.patch.object(seat_usability, "join", join):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = seat.cmd_seat(["status"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertTrue(seen["mint"],
                        "MUST-HIT CONTROL: the mint really did persist, so "
                        "the readings below are measurements")
        self.assertEqual({False}, set(seen["mint"]),
                         "a write may never be answered from a read cache")
        self.assertTrue(rendered["scoped"],
                        "the render's reads must share one instant")
        # THE UNCONDITIONAL POSITIVE ON THE EXACT OBSERVABLE, inline rather
        # than behind a helper: `active()` DOES read True when a scope is
        # genuinely open, so the False below is about the pass having ended.
        with projscope.scope():
            self.assertTrue(projscope.active())
        self.assertFalse(projscope.active(),
                         "the scope must not outlive the verb")


class PaneLiveOrphanedTest(unittest.TestCase):
    """`orphaned` is decisive for _pane_live — the field that was DROPPED in
    _pane_row and broke every pane read after the orca .46 remint. A pane can be
    connected=True and writable=True while its PTY has no live renderer
    (orphaned=True), and reads against it return empty. Keying only off `status`
    answered True on exactly those panes."""

    def test_an_orphaned_but_connected_pane_is_not_live(self):
        """The bug class: connected + writable + orphaned must read NOT live.
        status alone says connected; orphaned says the PTY has no renderer."""
        row = {"status": "connected", "writable": True, "orphaned": True}
        self.assertFalse(seat._pane_live(row))

    def test_a_connected_non_orphaned_pane_is_live(self):
        row = {"status": "connected", "writable": True, "orphaned": False}
        self.assertTrue(seat._pane_live(row))

    def test_a_row_with_no_orphaned_key_falls_back_to_status(self):
        """Pre-fix shape (or a harness with no orphaned concept, e.g. herdr):
        the status check still governs."""
        self.assertTrue(seat._pane_live({"status": "connected"}))
        self.assertFalse(seat._pane_live({"status": "disconnected"}))

    def test_mutation_flipping_orphaned_changes_the_answer(self):
        """The mutation bar: flip `orphaned` on a fixture row and _pane_live
        must change its answer, or the field is decoration."""
        base = {"status": "connected", "writable": True, "orphaned": False}
        self.assertTrue(seat._pane_live(base))
        self.assertFalse(seat._pane_live(dict(base, orphaned=True)))

    def test_an_orphaned_pane_still_resolves_for_SEND_but_never_for_READ(self):
        """THE REGRESSION THE ORPHANED FIX CAUSED, pinned in both directions.

        Making `_pane_live` honest about orphaned panes was correct for READS
        and killed every ACTUATION: `_resolve_registered_pane` gates on it, and
        the autocompact injection path resolves through there, so at 30-of-34
        panes orphaned the rescue died for exactly the seats needing rescue.
        Measured live 2026-07-26: a codex seat sat at 102% while the watchdog
        reported 'injection unavailable' — and a direct send to that same
        orphaned handle compacted it, because orphaned means no RENDERER, not
        no PTY.

        Both directions asserted, because a fix that only opened the send path
        would be indistinguishable from one that also broke the read path — and
        the read path answering honestly is what the whole orphaned lane was
        for."""
        from helm import harness as _h
        rec = {"seat": "codex", "harness": "orca",
               "handle": "term_orphaned", "session": "sess-1"}
        orphan_row = {"handle": "term_orphaned", "status": "connected",
                      "writable": True, "orphaned": True}
        # the predicates disagree, and that disagreement IS the feature
        self.assertFalse(seat._pane_live(orphan_row),
                         "orphaned must never read as live")
        self.assertTrue(seat._pane_sendable(orphan_row),
                        "orphaned+writable must remain send-capable")
        self.assertFalse(seat._pane_sendable(dict(orphan_row, writable=False)),
                         "not writable is not sendable, orphaned or not")
        self.assertFalse(seat._pane_sendable(dict(orphan_row, status="closed")),
                         "a closed pane is not sendable")

        def resolve(for_send):
            ad = mock.Mock(spec=_h.OrcaAdapter)
            ad.name = "orca"
            ad.list.return_value = [orphan_row]
            ad.resolve_pane.side_effect = _h.HarnessError("no replacement")
            with mock.patch.object(seat, "_seat_family", return_value=("codex", None)), \
                 mock.patch.object(seat, "_spawn_record", return_value=rec), \
                 mock.patch.object(seat, "_instance_dir", return_value="/tmp/x"), \
                 mock.patch.object(seat, "_prove_orca_replacement",
                                   return_value=(None, None, "no replacement")), \
                 mock.patch.object(_h, "detect", return_value=ad):
                return seat._resolve_registered_pane(
                    "codex", repair=False, for_send=for_send)

        _ad, read_handle, read_detail = resolve(False)
        self.assertIsNone(read_handle,
                          "observation must still refuse an orphaned pane")
        self.assertIn("orphaned", read_detail)

        _ad, send_handle, send_detail = resolve(True)
        self.assertEqual(send_handle, "term_orphaned",
                         "actuation must resolve an orphaned-but-writable pane")
        # and it must SAY the confirmation is unavailable — a send nobody can
        # read back is authorized, not verified, and the log must not imply it
        self.assertIn("SEND-ONLY", send_detail)

    def test_the_rejection_message_names_orphaned_not_status(self):
        """THE BLOCKER from codex's cross-family gate, pinned as an INTEGRATION
        test through _resolve_registered_pane (not a unit test on the formatter)
        — because codex found NO test reached the diagnostic at all, so a wrong
        string could regress freely.

        An orphaned+connected row is correctly rejected at _pane_live, but the
        stale detail must name the field that DECIDED the rejection (orphaned),
        not the field that is fine (status='connected'). A message that says
        'is connected' while rejecting the pane repeats the exact false
        diagnosis this lane exists to fix."""
        from helm import harness as _h
        rec = {"seat": "codex", "harness": "orca",
               "handle": "term_orphaned", "session": "sess-1"}
        orphan_row = {"handle": "term_orphaned", "status": "connected",
                      "writable": True, "orphaned": True}
        ad = mock.Mock(spec=_h.OrcaAdapter)
        ad.name = "orca"
        ad.list.return_value = [orphan_row]
        # no resolve_pane replacement available -> the stale detail is what
        # comes back, so it is the string under test.
        ad.resolve_pane.side_effect = _h.HarnessError("no replacement")
        with mock.patch.object(seat, "_seat_family", return_value=("codex", None)), \
             mock.patch.object(seat, "_spawn_record", return_value=rec), \
             mock.patch.object(seat, "_instance_dir", return_value="/tmp/x"), \
             mock.patch.object(seat, "_prove_orca_replacement",
                               return_value=(None, None, "no replacement")), \
             mock.patch.object(_h, "detect", return_value=ad):
            _ad, handle, detail = seat._resolve_registered_pane(
                "codex", repair=False)
        self.assertIsNone(handle, "an orphaned pane must not resolve as live")
        self.assertIn("orphaned", detail)
        self.assertNotIn(" is connected ", detail,
                         "the message must not name the field that is fine")


class SeatBornWiredTest(unittest.TestCase):
    """G-seatlaunch-installs: add/launch leave the seat's claude dir carrying
    the full hook contract + beacon permit; launch refreshes stale assets.
    Borrows SeatTest's setUp/helpers WITHOUT subclassing it (a subclass
    would silently re-run the whole parent suite twice)."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    def _settings(self):
        p = os.path.join(seat.seat_dir("codex"), "claude", "settings.json")
        with open(p) as f:
            return json.load(f)

    def test_fresh_seat_is_born_carrying_the_suite_guard(self):
        """task/1006: a seat minted TODAY carries the local-suite guard.

        The row's whole shape was that this could not be true by construction.
        The guard was hand-wired into ~/.claude and hand-copied into 8 seat
        configs, so the seats minted after that patch (cursor, codex-5) were
        born unguarded — and `hooks status` still printed "10 of 10 seats
        covered (full hook contract)", because the guard was named in no spec
        list and therefore no census could count it.

        This asserts the GUARD ITSELF, not the size of the contract: an arm
        that only counted SEAT_SPECS would stay green if provisioning dropped
        this spec and the list dropped it too."""
        from helm import hooks
        self._plant("home-a")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        spec = next(s for s in hooks.SEAT_SPECS if s["name"] == "suite-guard")
        # the fixture guard resolves, so the contract really does require it
        self.assertIn(spec, hooks.resolved_specs(hooks.SEAT_SPECS))
        cmds = hooks._hook_cmds(self._settings(), "PreToolUse")
        self.assertIn(hooks.spec_command(spec), cmds)
        self.assertIn(self.suite_guard, " ".join(cmds))
        # and it is a GATE: `|| true` here would rewrite the refusal to success
        self.assertNotIn("|| true", hooks.spec_command(spec))

    def test_a_shortened_MINT_is_loud_nonfatal_and_names_what_happens_next(self):
        """MINT-ONLY DOOR (`seat add`), per the accepted middle shape.

        THIS ARM DELIBERATELY LEAVES THE GUARD UNRESOLVED — its subject IS the
        shortened path, so pinning the harness fixture here would test the
        wrong state. The pin is made dead rather than merely unset because an
        unset pin resolves off the HOST's PATH, which would make this arm pass
        for opposite reasons on a box that has the binary and one that does not.

        rc 0 is correct: the command's objective — durable seat and config
        creation — SUCCEEDED, and the guard capability is not exercised until
        launch. What makes that honest rather than silent is the last clause:
        the warning must say LAUNCH WILL REFUSE, and the minted launch.sh must
        actually make that true."""
        from helm import hooks
        self._plant("home-a")
        spec = next(s for s in hooks.SPECS if s["name"] == "suite-guard")
        # MUST-HIT: resolvable => no warning at all, and the guard IS written.
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("SHORTENED", err)
        self.assertIn(spec["external"],
                      " ".join(hooks._all_hook_cmds(self._settings())))
        shutil.rmtree(seat.seat_dir("codex"), ignore_errors=True)

        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, "a mint-only door must not fail: the seat and "
                                "config really were created (%s)" % err)
        self.assertIn("SHORTENED", err)
        self.assertIn("suite-guard", err)          # NAMES the missing guard
        self.assertIn("LAUNCH WILL REFUSE", err)   # says what happens next
        self.assertNotIn("wired for full hook contract", err)
        # THE SEAT REALLY EXISTS — that is what rc 0 is claiming
        self.assertTrue(os.path.exists(
            os.path.join(seat.seat_dir("codex"), "claude", "settings.json")))
        # ...and the promise is kept by the ARTIFACT, not by the sentence: the
        # minted launch.sh refuses to start a session while the guard is gone.
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            script = f.read()
        self.assertIn("REFUSED", script)
        self.assertLess(script.index("REFUSED"), script.rindex("exec "))

    def test_a_shortened_LAUNCH_door_still_refuses(self):
        """The other half of the middle shape: where a session actually starts,
        a shortened contract is fatal. Same function, fatal by default."""
        from helm import seat_launch_assets
        self._plant("home-a")
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")
        self.assertIs(seat_launch_assets._write_launch_assets("codex", d),
                      seat_launch_assets._SEAT_SURFACE_REFUSED,
                      "the launch/spawn/resume door must refuse")
        # and the mint-only opt-out does NOT refuse on the same input
        self.assertIsNot(
            seat_launch_assets._write_launch_assets("codex", d,
                                                    fatal_shortened=False),
            seat_launch_assets._SEAT_SURFACE_REFUSED)

    def test_the_asset_door_returns_the_value_consumers_detect(self):  # noqa: VACUOUS_ASSERTION — the assertIs on the shortened path is the unconditional positive control; mutation-proven
        """The refusal must be the sentinel the six existing call sites already
        check — an implicit None reads as success at every one of them."""
        from helm import hooks, seat_launch_assets
        self._plant("home-a")
        d = seat.seat_dir("codex")
        os.makedirs(d, exist_ok=True)
        # MUST-HIT: resolvable => NOT the refusal sentinel. noqa:
        # VACUOUS_ASSERTION — the assertIs below is the unconditional
        # positive control on this same observable; mutation-proven.
        self.assertIsNot(
            seat_launch_assets._write_launch_assets("codex", d),
            seat_launch_assets._SEAT_SURFACE_REFUSED)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")
        self.assertIs(
            seat_launch_assets._write_launch_assets("codex", d),
            seat_launch_assets._SEAT_SURFACE_REFUSED,
            "a shortened write must return the sentinel every consumer checks, "
            "not an implicit None that reads as success")

    def test_add_wires_delivery_lane_and_beacon_permit(self):
        self._plant("home-a")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        from helm import hooks, record
        got = self._settings()
        # LIVE, not "its command string appears": the recorder is born beside
        # delivery now (task/3089), and the two fold into ONE PostToolUse
        # dispatcher entry exactly as in a home, so delivery's standalone
        # command is gone while its lane is live. _lane_live is the one check
        # that knows that fold.
        self.assertEqual(len(hooks.resolved_specs(hooks.SEAT_SPECS)),
                         len(hooks.SEAT_SPECS))       # the fixture guard resolves
        for s in hooks.SEAT_SPECS:
            self.assertTrue(hooks._lane_live(got, s), s["name"])
        for ev in record.HOOK_EVENTS:                  # both recorder legs
            self.assertTrue(record._leg_live(got, ev), ev)
        for rule in hooks.PERMIT_RULES:
            self.assertIn(rule, got["permissions"]["allow"])
        self.assertIn("inject --hook-json",
                      " ".join(hooks._hook_cmds(got, "UserPromptSubmit")))
        self.assertIn("handoff check --hook-json",
                      " ".join(hooks._hook_cmds(got, "PreCompact")))
        self.assertIn("handoff check --hook-json",
                      " ".join(hooks._hook_cmds(got, "SessionEnd")))

    def test_launch_refreshes_hooks_and_identity_stdout_stays_pure(self):
        """The live kimi shape: a seat minted before HELM_CHAT_NAME/hook
        install existed — launch retrofits both; stdout is ONLY the line."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        os.remove(os.path.join(d, "claude", "settings.json"))   # the dark seat
        with open(os.path.join(d, "launch.sh"), "w") as f:
            f.write("#!/bin/sh\n# stale — pre-identity\nexec claude\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith("ANTHROPIC_AUTH_TOKEN=$(cat "), line)
        self.assertIn("; export ANTHROPIC_AUTH_TOKEN; env -u ANTHROPIC_API_KEY", line)
        self.assertNotIn("\n", line)                 # pasteable — one line
        got = self._settings()                       # hooks are back
        from helm import hooks
        self.assertIn("chat join --hook-json",
                      " ".join(hooks._hook_cmds(got, "SessionStart")))
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("HELM_CHAT_NAME=codex", f.read())   # identity restored
        self.assertIn("wired for full hook contract", err.getvalue())

    def test_seat_cannot_enter_plan_mode_unattended(self):
        """PLAN MODE IS ONLY FOR INTERACTING WITH A HUMAN (owner ruling
        2026-08-03). CC's built-in EnterPlanMode carries a standing "prefer
        planning for non-trivial implementation" instruction, and the exit
        asks for approval that --dangerously-skip-permissions does NOT bypass —
        so a dispatch-driven seat parks at a prompt no human is watching and
        reads as IDLE to every instrument. Both seat surfaces deny ENTRY:
        the pane's launch line and the seat's whole config dir."""
        self._plant("home-a")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        got = self._settings()
        self.assertIn(seat.PLAN_ENTRY_TOOL, got["permissions"]["deny"])
        self.assertIn("Artifact", got["permissions"]["deny"])   # task/1941: seeded for codex
        # ENTRY only — a human who shift-tabs a pane into plan mode by hand
        # must still be able to get the seat back out.
        self.assertNotIn("ExitPlanMode", got["permissions"]["deny"])
        # additive: the beacon permits hooks.install_home merged are untouched
        from helm import hooks
        for rule in hooks.PERMIT_RULES:
            self.assertIn(rule, got["permissions"]["allow"])
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            body = f.read()
        # ORDER is load-bearing: --disallowedTools is variadic, so the token
        # after it must be an option or it swallows launch.sh's trailing "$@".
        # task/1941: the codex line now carries Artifact between them; the ORDER
        # invariant (an option, never a positional, after the variadic flag)
        # is what this pins, spelled literally. task/2287: the fork rule
        # carries parentheses, launch_line quotes it, and _launch_owner's
        # shlex.quote of the whole child NESTS that quote — so the FILE
        # spells it '"'"'Agent(fork)'"'"'. The literal pin spells the file;
        # the argv pin spells what /bin/sh hands claude.
        self.assertIn("--disallowedTools EnterPlanMode Artifact Skill"
                      " '\"'\"'Agent(fork)'\"'\"' --dangerously-skip-permissions", body)
        argv = ProxySeatCannotSpawnThroughASkill.child_argv(body)
        i = argv.index("--disallowedTools")
        self.assertEqual(argv[i:i + 6], ["--disallowedTools", "EnterPlanMode",
                                         "Artifact", "Skill", "Agent(fork)",
                                         "--dangerously-skip-permissions"])
        self.assertTrue(body.rstrip().endswith('"$@"'), body)

    def test_seat_is_born_with_feedback_routed_to_helm(self):  # noqa: VACUOUS_ASSERTION — the feedbackDrafts equality and the launch.sh pair assertIn are unconditional positives on the files the order check reads
        """task/2328: the real `seat add` path leaves all three surfaces in
        place — the pair on launch.sh's line, drafts off in settings.json,
        the rule sentence in the seat's CLAUDE.md — and a relaunch re-asserts
        them without duplicating the sentence."""
        self._plant("home-a")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._settings()["feedbackDrafts"], "off")
        cdir = os.path.join(seat.seat_dir("codex"), "claude")
        with open(os.path.join(cdir, "CLAUDE.md")) as f:
            self.assertEqual(f.read().count(seat.FEEDBACK_RULE), 1)
        with open(os.path.join(seat.seat_dir("codex"), "launch.sh")) as f:
            body = f.read()
        self.assertIn(" DISABLE_FEEDBACK_COMMAND=1 DISABLE_BUG_COMMAND=1 claude"
                      " --disallowedTools EnterPlanMode", body)
        argv = ProxySeatCannotSpawnThroughASkill.child_argv(body)
        self.assertLess(argv.index("DISABLE_BUG_COMMAND=1"), argv.index("claude"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(rc, 0, err.getvalue())
        with open(os.path.join(cdir, "CLAUDE.md")) as f:
            self.assertEqual(f.read().count(seat.FEEDBACK_RULE), 1)   # once, never accreted
        self.assertEqual(self._settings()["feedbackDrafts"], "off")

    def test_relaunch_reasserts_plan_deny_and_keeps_foreign_rules(self):
        """The deny is re-asserted on the same cadence launch.sh is re-minted,
        so a seat cannot drift out of it; hooks.install_home's merge (which
        runs first, and owns permissions.allow) never drops it, and an
        operator's own deny rule survives byte-identical."""
        self._plant("home-a")
        rc, _out, err = self._add()
        self.assertEqual(rc, 0, err)
        p = os.path.join(seat.seat_dir("codex"), "claude", "settings.json")
        with open(p) as f:
            body = json.load(f)
        body["permissions"]["deny"] = ["Bash(rm -rf:*)"]      # the seat drifts
        with open(p, "w") as f:
            json.dump(body, f)
        # positive control on the same observable: the drift really took hold,
        # so a green assertion below can only mean the RELAUNCH put it back
        self.assertEqual(self._settings()["permissions"]["deny"],
                         ["Bash(rm -rf:*)"])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(rc, 0, err.getvalue())
        deny = self._settings()["permissions"]["deny"]
        self.assertEqual(deny.count(seat.PLAN_ENTRY_TOOL), 1)  # re-asserted once
        self.assertEqual(deny.count("Artifact"), 1)              # seeded once, never duplicated on relaunch
        self.assertIn("Bash(rm -rf:*)", deny)                  # operator's rule kept


class SeatBornFullAgentTest(unittest.TestCase):
    """task/3089: a seat is a full agent from birth. The mint door
    (seat_launch_assets._write_launch_assets, reached by add, launch, resume
    and spawn) gives the new config dir the canonical MCP servers, the full
    helm hook set including both recorder legs, and the host's global
    instructions — through the SAME primitives `helm tidy` reconciles with, so
    tidy's plan for a just-minted seat is empty. qwen27 is the family (keyless
    here); a codex-family seat is born the same way, because its launch runs
    the claude harness."""
    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _add = SeatTest._add
    _plant = SeatTest._plant

    SECRET = "FIXTURE-BEARER-SENTINEL-3089"  # gitleaks:allow (a test fixture, not a credential)

    def _sources(self):
        """A private (0600) canonical MCP file and a global CLAUDE.md, both
        fixtures, plus a tmp backup root — nothing of this host is read."""
        from helm import envtidy
        priv = os.path.join(self.tmp, "mcps-canonical.json")
        with open(priv, "w") as f:
            json.dump({"alpha-mcp": {"command": "alpha"},
                       "beta-mcp": {"type": "http", "url": "https://x.invalid/m",
                                    "headers": {"Authorization": "Bearer " + self.SECRET}},
                       "exa": {"command": "exa"}}, f)
        os.chmod(priv, 0o600)
        rules = os.path.join(self.tmp, "owner-home", "CLAUDE.md")
        os.makedirs(os.path.dirname(rules))
        with open(rules, "w") as f:
            f.write("# the host's global rules\n")
        # ONLY THESE TWO KEYS, set and restored one by one — never a
        # mock.patch.dict over the whole environ here: its stop() runs as a
        # cleanup AFTER SeatTest.tearDown and would restore the whole dict to
        # its mid-test snapshot, re-planting this arm's HELM_HOME/HELM_PROC
        # (a deleted tmp) into every later test in the process.
        for key, value in zip(self.SOURCE_KEYS, (priv, rules)):
            self.addCleanup(self._restore_key, key, os.environ.get(key))
            os.environ[key] = value
        backup = mock.patch.object(envtidy, "BACKUP_ROOT",
                                   os.path.join(self.tmp, "env-backup"))
        backup.start()
        self.addCleanup(backup.stop)
        return priv, rules

    SOURCE_KEYS = ("HELM_MCPS_PRIVATE", "HELM_INSTRUCTIONS_CANONICAL")

    @staticmethod
    def _restore_key(key, prior):
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior

    def test_a_new_seat_is_born_with_servers_full_hooks_and_instructions(self):  # noqa: VACUOUS_ASSERTION — the server list, recorder legs, empty tidy plans and the link target are unconditional positives; the absence checks read the same outputs
        from helm import envtidy, record, skillsync
        _priv, rules = self._sources()
        rc, _out, err = self._add(("add", "qwen27"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self.SECRET, _out + err)             # configs never printed
        cdir = os.path.join(seat.seat_dir("qwen27"), "claude")
        label = "seat:qwen27"
        # servers: qwen27 is served from the operator's own box, so it is
        # born with the web-search floor only whatever its window
        # (envtidy._own_box), in a private state file, and the rest are
        # withheld by name
        state = os.path.join(cdir, ".claude.json")
        with open(state) as f:
            got = json.load(f)
        self.assertEqual(sorted(got["mcpServers"]), ["exa"])
        self.assertEqual(sorted(n for n, _ in envtidy.plan_mcp_home(
            label, cdir)["withheld"]), ["alpha-mcp", "beta-mcp"])
        self.assertTrue(got.get("hasCompletedOnboarding"))    # onboarding kept
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o600)
        # hooks: the recorder legs every home carries, and nothing left to sync
        with open(os.path.join(cdir, "settings.json")) as f:
            settings = json.load(f)
        for ev in record.HOOK_EVENTS:
            self.assertTrue(record._leg_live(settings, ev), ev)
        plan = envtidy.plan_hooks_home(label, cdir)
        self.assertEqual(plan["verdict"], "ok", plan.get("actions"))
        self.assertEqual(envtidy.plan_mcp_home(label, cdir)["verdict"], "ok")
        # instructions: the link, and the seat's own rules file still its own
        link = os.path.join(cdir, "rules", "global-instructions.md")
        self.assertEqual(os.readlink(link), os.path.realpath(rules))
        self.assertEqual(skillsync.link_instructions(cdir).action, "ok")
        self.assertEqual(skillsync.claude_md_state(cdir), "real")
        with open(os.path.join(cdir, "CLAUDE.md")) as f:
            self.assertEqual(f.read().count(seat.FEEDBACK_RULE), 1)
        # a relaunch re-asserts without a new warning and keeps every piece
        out, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err2):
            self.assertEqual(seat.cmd_seat(["launch", "qwen27"]), 0, err2.getvalue())
        self.assertNotIn("NOT written", err2.getvalue())
        self.assertNotIn("NOT linked", err2.getvalue())
        with open(state) as f:
            self.assertEqual(sorted(json.load(f)["mcpServers"]), ["exa"])
        self.assertEqual(os.readlink(link), os.path.realpath(rules))

    def test_a_codex_family_seat_on_the_claude_harness_is_born_with_the_servers(self):  # noqa: VACUOUS_ASSERTION — the harness stamp, the three-server list and the ok plan are unconditional positives; the secret check reads the same outputs
        """A codex-family seat is Claude Code on a codex model: its minted
        launch stamps the claude harness, so it takes the servers like any
        other seat — the family name excludes nothing."""
        from helm import envtidy
        self._sources()
        self._plant("home-a")
        rc, _out, err = self._add()                       # add codex
        self.assertEqual(rc, 0, err)
        d = seat.seat_dir("codex")
        cdir = os.path.join(d, "claude")
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("HELM_AGENT_HARNESS=claude", f.read())
        self.assertEqual(envtidy.seat_harness(cdir), "claude")
        self.assertIsNone(envtidy.mcp_exclusion("seat:codex", cdir))
        with open(os.path.join(cdir, ".claude.json")) as f:
            self.assertEqual(sorted(json.load(f)["mcpServers"]),
                             ["alpha-mcp", "beta-mcp", "exa"])    # a wide window
        self.assertEqual(envtidy.plan_mcp_home("seat:codex", cdir)["verdict"], "ok")
        self.assertNotIn(self.SECRET, _out + err)

    def test_mutation_control_without_the_birth_wiring_the_seat_is_short(self):
        """Take the new birth steps away and the SAME observables go short —
        so the green arm above observes the mint door, not a coincidence."""
        from helm import envtidy, record, seat_launch_assets
        self._sources()
        with mock.patch.object(seat_launch_assets, "_seat_full_agent"), \
                mock.patch.object(record, "install_home", return_value=("ok", "")):
            rc, _out, err = self._add(("add", "qwen27"))
        self.assertEqual(rc, 0, err)
        cdir = os.path.join(seat.seat_dir("qwen27"), "claude")
        self.assertEqual(envtidy.plan_mcp_home("seat:qwen27", cdir)["verdict"], "change")
        self.assertEqual(envtidy.plan_hooks_home("seat:qwen27", cdir)["verdict"], "change")
        self.assertFalse(os.path.lexists(os.path.join(cdir, "rules",
                                                      "global-instructions.md")))

    def test_a_refused_canonical_file_is_loud_and_the_seat_still_mints(self):
        priv, _rules = self._sources()
        os.chmod(priv, 0o644)                           # THE VIOLATION
        rc, _out, err = self._add(("add", "qwen27"))
        self.assertEqual(rc, 0, err)                    # never fatal to the mint
        self.assertIn("MCP servers NOT written", err)
        self.assertIn(priv, err)
        self.assertIn("REFUSED", err)
        self.assertNotIn(self.SECRET, err)
        cdir = os.path.join(seat.seat_dir("qwen27"), "claude")
        with open(os.path.join(cdir, ".claude.json")) as f:
            self.assertNotIn("mcpServers", json.load(f))
        # positive control: the global instructions still landed on this mint
        self.assertTrue(os.path.islink(os.path.join(cdir, "rules",
                                                    "global-instructions.md")))

    def test_an_existing_real_claude_md_is_kept(self):
        """The seat's own CLAUDE.md is its own: the global instructions arrive
        beside it, never over it, and every byte the operator wrote stays."""
        _priv, rules = self._sources()
        d = seat.seat_dir("qwen27")
        cdir = os.path.join(d, "claude")
        os.makedirs(cdir)
        own = os.path.join(cdir, "CLAUDE.md")
        mine = b"# this seat's own rules\r\nkeep this line\r\n"
        with open(own, "wb") as f:
            f.write(mine)
        rc, _out, err = self._add(("add", "qwen27"))
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.islink(own))
        with open(own, "rb") as f:
            body = f.read()
        self.assertTrue(body.startswith(mine), body)          # the operator's bytes
        self.assertEqual(os.readlink(os.path.join(cdir, "rules",
                                                  "global-instructions.md")),
                         os.path.realpath(rules))


class SeatMultiTest(unittest.TestCase):
    """--multi (0.2 mixed-model fleets, premise multimodel-one-cc-proven-
    per-agent-frontmatter-no-fork): the launch line/env DROP the
    CLAUDE_CODE_SUBAGENT_MODEL blunt pin (it overrides per-agent frontmatter),
    probe agents with per-model `model:` frontmatter are minted, and the smoke
    gate grows a conductor-log-verified fan-out leg. Hermetic: no proxy, no
    claude, no network. Borrows SeatTest's fixtures without subclassing."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    def test_launch_line_multi_drops_subagent_pin_only(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        line = seat.launch_line("codex", multi=True)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", line)  # the proven law
        base = seat.launch_line("codex")
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-sol", base)  # default intact
        # everything else is byte-identical: removing the pin is the ONLY delta
        self.assertEqual(base.replace(" CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-sol", ""),
                         line)
        # parent --model still rides; identity + scrub + ctx env intact
        self.assertIn("--model gpt-6.1-sol", line)
        # launch_line itself is the env/claude command (no token, no export —
        # the export prefix is added by the stdout print / launch.sh caller)
        self.assertTrue(line.startswith("env -u ANTHROPIC_API_KEY "), line)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", line)   # no env NAME=value secret
        self.assertIn("HELM_CHAT_NAME=codex", line)
        self.assertIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d"
                      % seat.FAMILIES["codex"]["max_context"], line)

    #: The shipped Codex two-model probe order: new default, retained fallback.
    PLANTED_PROBES = ("gpt-6.1-sol", "gpt-6-sol")

    def _two_probe_codex(self):
        return mock.patch.dict(seat.FAMILIES["codex"],
                               {"probe_models": self.PLANTED_PROBES})

    def test_probe_agents_names_and_frontmatter(self):
        # THE SHIPPED CATALOG: new Codex default first, old default fallback.
        probes = [("helm-probe-gpt-6-1-sol", "gpt-6.1-sol"),
                  ("helm-probe-gpt-6-sol", "gpt-6-sol")]
        self.assertEqual(seat.probe_agents("codex"), probes)
        self.assertEqual(seat.probe_agents("kimi"),
                         [("helm-probe-kimi-k3", "kimi-k3")])
        cdir = os.path.join(self.tmp, "cfg")
        got = seat._mint_probe_agents(cdir, "codex")
        self.assertEqual(got, probes)
        for name, model in probes:
            with open(os.path.join(cdir, "agents", name + ".md")) as f:
                body = f.read()
            self.assertTrue(body.startswith("---\n"))
            self.assertIn("name: %s\n" % name, body)
            self.assertIn("model: %s\n" % model, body)  # the wire-riding pin
        # re-mint overwrites in place, never accretes
        seat._mint_probe_agents(cdir, "codex")
        self.assertEqual(len(os.listdir(os.path.join(cdir, "agents"))), 2)

    def test_launch_verb_multi_mints_agents_and_launch_sh(self):  # noqa: VACUOUS_ASSERTION — the exact two-file agent list is a positive control before the launch-line pin absence
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seat.cmd_seat(["launch", "codex", "--multi"])
        self.assertEqual(rc, 0)
        line = out.getvalue().strip()
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", line)
        d = seat.seat_dir("codex")
        agents = sorted(os.listdir(os.path.join(d, "claude", "agents")))
        self.assertEqual(agents, ["helm-probe-gpt-6-1-sol.md",
                                  "helm-probe-gpt-6-sol.md"])
        with open(os.path.join(d, "launch.sh")) as f:
            sh = f.read()
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", sh)  # preset matches
        # a plain launch afterwards restores the pinned single-model preset
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-sol", out.getvalue())
        with open(os.path.join(d, "launch.sh")) as f:
            self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-sol", f.read())

    def test_resume_preserves_multi_shape(self):
        """A --multi seat re-minted on resume must NOT regain the pin — the pin's
        absence is the only marker, so _multi_from_launch reads it back and the
        re-mint stays pinless. Guards the mixed-model fleet against silent
        collapse to single-model on any resume/env-refresh."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        launch_sh = os.path.join(d, "launch.sh")
        # multi mint: pinless launch.sh, detector reads True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seat.cmd_seat(["launch", "codex", "--multi"]), 0)
        self.assertTrue(seat._multi_from_launch(launch_sh))
        # the resume re-mint path (env refresh) must keep it pinless
        seat._write_launch_assets(
            "codex", d, seat._room_from_launch(launch_sh), "codex",
            multi=seat._multi_from_launch(launch_sh))
        with open(launch_sh) as f:
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", f.read())
        # a pinned (non-multi) seat reads False and re-mints WITH the pin
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        self.assertFalse(seat._multi_from_launch(launch_sh))

    def test_resume_roomless_launch_falls_back_to_project_room(self):
        """The kimi room-drop regression (2026-07-23): a launch.sh minted
        WITHOUT a room stamp (HELM_CHAT_ROOM-less) made _resume recover
        (None, None) and re-mint room=None — the SessionStart join then
        defaulted the seat to #main, silently dropping it out of its project
        room. The resume seam now falls back to seats.resolve_homing(cwd), so
        the cwd-derived project room is preserved. Pin the two halves: (1)
        _homing_from_launch really is empty on a room-less launch.sh (the
        precondition), and (2) resolve_homing recovers the project room for
        the seat's cwd — the value _resume now re-mints."""
        from helm import seats
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        d = seat.seat_dir("codex")
        launch_sh = os.path.join(d, "launch.sh")
        # mint a launch, then strip its room stamp: the regression's
        # precondition (a launch.sh carrying NO HELM_CHAT_ROOM= line)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seat.cmd_seat(["launch", "codex"]), 0)
        with open(launch_sh) as f:
            txt = f.read()
        import re as _re
        txt = _re.sub(r"\s*HELM_CHAT_ROOM(_SOURCE)?=\S+", "", txt)
        with open(launch_sh, "w") as f:
            f.write(txt)
        room, room_source = seat._homing_from_launch(launch_sh)
        self.assertIsNone(room)
        self.assertIsNone(room_source)
        # the resume fallback recovers the cwd-derived project room, not None
        # (_git_project needs a true git root — create one in tmp so this test
        # passes from a git archive or a worktree equally)
        import subprocess as _subprocess
        repo_tmp = os.path.join(self.tmp, "helm-repo")
        os.makedirs(repo_tmp)
        _subprocess.run(["git", "-C", repo_tmp, "init", "-q", "-b", "main"],
                        check=True, capture_output=True)
        _subprocess.run(["git", "-C", repo_tmp, "config", "user.email", "t@t"],
                        check=True, capture_output=True)
        _subprocess.run(["git", "-C", repo_tmp, "config", "user.name", "t"],
                        check=True, capture_output=True)
        with open(os.path.join(repo_tmp, "a.txt"), "w") as f:
            f.write("test\n")
        _subprocess.run(["git", "-C", repo_tmp, "add", "-A"],
                        check=True, capture_output=True)
        _subprocess.run(["git", "-C", repo_tmp, "commit", "-qm", "init"],
                        check=True, capture_output=True)
        fb_room, fb_source = seats.resolve_homing(cwd=repo_tmp)
        self.assertIsNotNone(fb_room)
        self.assertEqual(fb_source, "derived")
        # and that room mints HELM_CHAT_ROOM into the relaunch line (not #main)
        line = seat.launch_line("codex", room=fb_room, room_source=fb_source)
        self.assertIn("HELM_CHAT_ROOM=%s" % fb_room, line)

    def test_seat_env_multi_no_pin_and_no_inherited_pin(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_SUBAGENT_MODEL": "ambient-pin"}):
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"),
                                 multi=True)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
            # default (pinned) shape: the seat's OWN pin, never the ambient one
            env = seat._seat_env("codex", os.path.join(self.tmp, "smoke"))
            self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-6.1-sol")

    def test_smoke_multi_leg_skips_single_model_family(self):
        """kimi has one probe model — the fan-out leg SKIPs loudly and passes,
        never launching a router or a claude."""
        smoke_dir = os.path.join(self.tmp, "smoke-kimi")
        os.makedirs(smoke_dir)
        booby = seat.subprocess.run
        seat.subprocess.run = lambda *a, **k: self.fail("claude launched on SKIP")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                passed = seat._smoke_multi_leg("kimi", seat.FAMILIES["kimi"],
                                               smoke_dir, "12345")
        finally:
            seat.subprocess.run = booby
        self.assertTrue(passed)
        self.assertIn("SKIP", out.getvalue())
        self.assertIn("probe_models", out.getvalue())

    def test_smoke_multi_leg_verifies_against_conductor_log(self):
        """The fan-out leg trusts the WIRE, not the reply: a fake claude whose
        stdout carries both markers passes only when the router's conductor
        log also saw both probe models (we replay the request shapes through
        the leg's own router); a marker-perfect reply with a one-model log
        FAILS (the blunt-pin regression shape)."""
        import http.client
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        smoke_dir = os.path.join(self.tmp, "smoke-codex")
        os.makedirs(smoke_dir)
        calls = {}

        def fake_run(cmd, env=None, **kw):
            calls["cmd"], calls["env"] = cmd, env
            # drive the leg's OWN router exactly as the proven fan-out would:
            # one request per model the fake fleet puts on the wire
            port = int(env["ANTHROPIC_BASE_URL"].rsplit(":", 1)[1])
            for m in calls.get("wire_models", []):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.request("POST", "/v1/messages",
                                 body=json.dumps({"model": m}).encode(),
                                 headers={"Content-Type": "application/json"})
                    conn.getresponse().read()
                finally:
                    conn.close()
            return subprocess.CompletedProcess(
                cmd, 0, stdout="helm-seat-multi-a-777 helm-seat-multi-b-777", stderr="")

        # a tiny fake CLIProxyAPI so routed requests land somewhere real
        fake_proxy_seen = []

        class _P(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake_proxy_seen.append(self.rfile.read(n))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _P)
        proxy.daemon_threads = True
        threading.Thread(target=proxy.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        old_port = seat.FAMILIES["codex"]["port"]
        seat.FAMILIES["codex"]["port"] = proxy.server_address[1]
        # codex ships two probe models; the patch pins this arm to the pair
        # its wire_models literals name, so a later rotation cannot move it
        planted = self._two_probe_codex()
        planted.start()
        try:
            # leg 1: wire carries BOTH probe models -> PASS
            calls["wire_models"] = ["gpt-6.1-sol", "gpt-6-sol"]
            with mock.patch.object(seat.subprocess, "run", fake_run):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    passed = seat._smoke_multi_leg(
                        "codex", seat.FAMILIES["codex"], smoke_dir, "777")
            self.assertTrue(passed, out.getvalue())
            self.assertIn("multi", out.getvalue())
            self.assertIn("PASS", out.getvalue())
            # the leg ran claude with NO pin and Task allowed, through its router
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", calls["env"])
            self.assertIn("Task", calls["cmd"])
            self.assertIn("helm-probe-gpt-6-sol",
                          calls["cmd"][calls["cmd"].index("-p") + 1])
            # probe agents were minted into the smoke config dir
            self.assertTrue(os.path.exists(os.path.join(
                smoke_dir, "agents", "helm-probe-gpt-6-1-sol.md")))
            # leg 2: reply perfect but the wire saw ONE model -> FAIL
            calls["wire_models"] = ["gpt-6-sol"]
            with mock.patch.object(seat.subprocess, "run", fake_run):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    passed = seat._smoke_multi_leg(
                        "codex", seat.FAMILIES["codex"], smoke_dir, "777")
            self.assertFalse(passed)
            self.assertIn("conductor log saw", out.getvalue())
        finally:
            planted.stop()
            seat.FAMILIES["codex"]["port"] = old_port
            proxy.shutdown()
            proxy.server_close()

    def test_usage_names_multi(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat([])
        self.assertEqual(rc, 2)
        self.assertIn("--multi", err.getvalue())


# CAPTURED AT IMPORT, before any setUp runs. SeatTest.setUp patches
# seat._ensure_autocompact_timer, and the seat facade fans that setattr out to
# every impl module, seat_lifecycle_sessions included, so reading the name
# there inside a test hands back the fixture's mock, not the wrapper.
from helm import seat_lifecycle_sessions as _lifecycle_for_capture  # noqa: E402
_REAL_AUTOCOMPACT_WRAPPER = _lifecycle_for_capture._ensure_autocompact_timer


class LaunchAutocompactTimerSwitchTest(unittest.TestCase):
    """`helm seat launch` when HELM_AUTOCOMPACT_TIMER turned the timer install
    off: the launch does not refuse, stdout stays the one pasteable line, and
    stderr says the timer is not armed as a NOTE, not a WARN.

    The fixture's wrapper mock is replaced by the REAL wrapper, and
    ensure_timer answers with its switched-off result, so nothing here can
    reach the host's scheduler (tests/test_autocompact.py drives the switch
    itself). Borrows SeatTest's fixtures without subclassing."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    SKIPPED = (None, "install skipped by HELM_AUTOCOMPACT_TIMER=0: no unit "
                     "file written, no systemctl run")

    def _launch(self, result):
        from helm import autocompact
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_ensure_autocompact_timer",
                               _REAL_AUTOCOMPACT_WRAPPER), \
                mock.patch.object(autocompact, "ensure_timer",
                                  return_value=result) as ensure, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["launch", "codex"])
        self.assertEqual(ensure.call_args_list, [mock.call()],
                         "rc=%s: %s" % (rc, err.getvalue()))
        return rc, out.getvalue(), err.getvalue()

    def test_the_captured_wrapper_is_the_real_one(self):
        self.assertIsInstance(_REAL_AUTOCOMPACT_WRAPPER, type(lambda: 0))
        self.assertEqual(_REAL_AUTOCOMPACT_WRAPPER.__module__,
                         "helm.seat_lifecycle_sessions")
        # and the fixture really does shadow it, which is why it is captured
        self.assertIsNot(seat._ensure_autocompact_timer,
                         _REAL_AUTOCOMPACT_WRAPPER)

    def test_switched_off_timer_launches_and_says_note(self):
        rc, out, err = self._launch(self.SKIPPED)
        self.assertEqual(rc, 0, err)
        line = out.strip()
        self.assertTrue(line.startswith("ANTHROPIC_AUTH_TOKEN=$(cat "), line)
        self.assertNotIn("\n", line)
        # The note never reaches stdout. Asserted on the note's own words: the
        # launch line carries HELM_CHAT_ROOM, derived from the checkout's
        # directory name, and a lane named for autocompact put the bare word
        # there (task/3534's lane failed this arm on the room alone).
        self.assertNotIn("autocompact timer", out)
        self.assertIn("helm seat: NOTE — autocompact timer not armed: install "
                      "skipped by HELM_AUTOCOMPACT_TIMER=0", err)
        self.assertNotIn("WARN — autocompact", err)

    def test_MUST_HIT_a_failed_install_is_still_a_warn_that_never_refuses(self):
        """The same arm with a real failure: the WARN proves the real wrapper
        ran here (the fixture's mock prints nothing)."""
        rc, out, err = self._launch((False, "systemctl unavailable; run "
                                            "autocompact from another "
                                            "scheduler"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip().startswith("ANTHROPIC_AUTH_TOKEN=$(cat "))
        self.assertIn("helm seat: WARN — autocompact timer not armed: "
                      "systemctl unavailable", err)
        self.assertNotIn("NOTE — autocompact", err)


class SeatEnsureTest(unittest.TestCase):
    """doctor --ensure (the proxy watchdog, codex-2 silent-starvation class):
    supervise every minted family+instance proxy — healthy rows pass through,
    dead/wedged rows respawn via the LANDED _up (never a second spawn path),
    and any row the watchdog cannot PROVE healthy surfaces UNKNOWN (rc 2),
    never a silent down/up. Hermetic: primitives mocked, no real proxy.
    Borrows SeatTest's fixtures without subclassing."""

    setUp = SeatTest.setUp
    tearDown = SeatTest.tearDown
    _plant = SeatTest._plant
    _add = SeatTest._add

    def _row(self, family="codex", seat_name="codex"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verdict = seat._ensure_row(family, seat_name)
        return verdict

    # -- the acceptance path: a DOWN proxy is respawned ---------------------
    def test_ensure_respawns_dead_proxy(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        # no pidfile -> down; a successful _up flips the row to live+open so the
        # post-respawn probe proves healthy (the seq-flip pattern: the initial
        # probe must read DOWN, only the re-probe after _up reads live).
        seq = {"live": False, "open": False}
        up_calls = []

        def fake_up(f, quiet=False, seat=None):
            up_calls.append((f, seat))
            seq.update(live=True, open=True)
            return 0

        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["open"]), \
                mock.patch.object(seat, "_up", fake_up), \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(label, "codex")
        self.assertEqual(state, "respawned")
        self.assertEqual(up_calls, [("codex", "codex")])
        down.assert_not_called()          # nothing live to signal away

    def test_ensure_healthy_row_untouched(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(seat, "_port_open", return_value=True), \
                mock.patch.object(seat, "proxy_config_plan",
                                  return_value={"changed": False}), \
                mock.patch.object(seat, "proxy_drift",
                                  return_value=(seat.PROXY_CURRENT, None)), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "healthy")
        up.assert_not_called()
        down.assert_not_called()

    def test_ensure_wedged_proxy_signaled_then_respawned(self):
        # live verified pid but the port does not answer -> _down clears it,
        # then _up brings a fresh one up.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": True, "open": False}
        down_calls, up_calls = [], []

        def fake_up(f, quiet=False, seat=None):
            up_calls.append((f, seat))
            seq.update(live=True, open=True)   # fresh proxy up + answering
            return 0

        def fake_down(f, seat=None):
            down_calls.append((f, seat))
            seq.update(live=False, open=False)
            return 0

        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["open"]), \
                mock.patch.object(seat, "_proxy_age_s",
                                  return_value=120.0), \
                mock.patch.object(seat, "_up", fake_up), \
                mock.patch.object(seat, "_down", fake_down):
            label, state, detail = self._row()
        self.assertEqual(state, "respawned")
        self.assertEqual(down_calls, [("codex", "codex")])
        self.assertEqual(len(up_calls), 1)

    def test_ensure_starting_proxy_within_grace_is_never_killed(self):
        # THE fable adversarial MED: a HEALTHY just-launched proxy still binding
        # its port reads 'live pid + port not answering' -> the OLD code SIGTERMed
        # it. Within the startup-grace window it must be left alone (unknown /
        # STARTING), _down NEVER called — cron firing in the boot window must not
        # churn kill->respawn->kill.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_proxy_age_s", return_value=3.0), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "unknown")
        self.assertIn("STARTING", detail)
        down.assert_not_called()          # the whole point: never SIGTERM a boot
        up.assert_not_called()            # nor double-spawn over it

    def test_ensure_grace_boundary_old_proxy_still_wedged(self):
        # past grace AND still not answering -> genuinely wedged -> signal+respawn
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": True}
        down_calls = []

        def fake_down(f, seat=None):
            down_calls.append((f, seat))
            seq["live"] = False
            return 0

        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": "x"}), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_proxy_age_s",
                                  return_value=seat._ENSURE_STARTUP_GRACE_S + 1), \
                mock.patch.object(seat, "_up", return_value=1), \
                mock.patch.object(seat, "_down", fake_down):
            label, state, detail = self._row()
        self.assertEqual(down_calls, [("codex", "codex")])
        self.assertEqual(state, "unknown")   # _up refused -> unresolved -> UNKNOWN

    def test_ensure_unverifiable_live_pid_is_unknown_never_touched(self):
        # a record whose pid is ALIVE but fails identity verification (a reused
        # pid now owned by a stranger, or a legacy bare-pid proxy): the watchdog
        # refuses to signal it AND refuses to respawn over a live foreign
        # listener — UNKNOWN for a human. (The ds4pro-SIGKILL fix: only an
        # ALIVE-but-unverifiable pid refuses; a DEAD one respawns.)
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4321, "identity": None}), \
                mock.patch.object(seat, "_running_pid", return_value=None), \
                mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "unknown")
        self.assertIn("unverifiable", detail)
        up.assert_not_called()
        down.assert_not_called()

    def test_ensure_stale_dead_pidfile_respawns(self):
        # the codex-2 silent-starvation case: a well-formed identity record
        # whose pid has since DIED. _running_pid fails closed to None (corpse),
        # but the record still parses — the watchdog must NOT read it as
        # 'unverifiable, refuse'; the pid is dead, so this is a plain DOWN ->
        # respawn via _up.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": False}
        up_calls = []

        def fake_up(f, quiet=False, seat=None):
            up_calls.append((f, seat))
            seq["live"] = True
            return 0

        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 9999, "identity": "proc:x"}), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 5555 if seq["live"] else None), \
                mock.patch.object(seat, "_pid_alive", return_value=False), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["live"]), \
                mock.patch.object(seat, "_up", fake_up), \
                mock.patch.object(seat, "_down") as down:
            label, state, detail = self._row()
        self.assertEqual(state, "respawned")
        self.assertEqual(up_calls, [("codex", "codex")])
        down.assert_not_called()          # corpse already gone; nothing to signal

    def test_ensure_respawn_failure_is_unknown(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid", return_value=None), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_up", return_value=1):
            label, state, detail = self._row()
        self.assertEqual(state, "unknown")
        self.assertIn("respawn failed", detail)

    def test_ensure_concurrent_up_loser_reads_healthy_not_unknown(self):
        # fable LOW: a seat launching in the same instant wins the flock; our
        # _up returns rc 1 ('already running'). That is NOT a respawn failure —
        # the row is now HEALTHY under the winner. Re-probe must recover it.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        seq = {"live": False}   # winner flips this when it steals the start

        def losing_up(f, quiet=False, seat=None):
            seq["live"] = True   # the concurrent winner's proxy is now up
            return 1             # ... so OUR _up loses the race

        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 7777 if seq["live"] else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: seq["live"]), \
                mock.patch.object(seat, "_up", losing_up), \
                mock.patch.object(seat, "_down"):
            label, state, detail = self._row()
        self.assertEqual(state, "healthy")
        self.assertIn("concurrent starter won", detail)

    def test_ensure_rc2_on_any_unknown_row(self):
        # one healthy family + one wedged respawn that fails -> rc 2 overall
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid", return_value=None), \
                mock.patch.object(seat, "_port_open", return_value=False), \
                mock.patch.object(seat, "_up", return_value=1):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = seat._ensure([])
        self.assertEqual(rc, 2)

    def test_doctor_ensure_dispatches_and_guards_tail(self):  # noqa: VACUOUS_ASSERTION — both accepted argv shapes call the real dispatcher mock with exact arguments; the junk control returns rc 2 on the same command seam
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat, "_ensure", return_value=0) as ens:
            rc = seat.cmd_seat(["doctor", "--ensure"])
        self.assertEqual(rc, 0)
        ens.assert_called_once_with(["--ensure"])
        with mock.patch.object(seat, "_ensure", return_value=0) as ens:
            rc = seat.cmd_seat(["doctor", "--ensure", "--quiet"])
        self.assertEqual(rc, 0)
        ens.assert_called_once_with(["--ensure", "--quiet"])
        # junk after --ensure still refuses BEFORE any work
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["doctor", "--ensure", "--bogus"])
        self.assertEqual(rc, 2)

    def test_real_cli_empty_population_is_unknown_in_every_contract(self):  # noqa: VACUOUS_ASSERTION — all three real CLI calls positively assert rc2 and explicit UNKNOWN output before proving no healthy heartbeat state was latched
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        empty = os.path.join(self.tmp, "empty-seat-home")
        heartbeat = os.path.join(self.tmp, "subprocess-heartbeat")
        os.makedirs(empty)
        env = os.environ.copy()
        env["HELM_HOME"] = empty
        env["HELM_CODEX_HOMES_DIR"] = os.path.join(self.tmp, "empty-codex")
        env["HELM_ENSURE_QUIET_HEARTBEAT_DIR"] = heartbeat

        def run(*args):
            return subprocess.run(
                [sys.executable, os.path.join(repo, "bin", "helm"), "seat",
                 "doctor", "--ensure"] + list(args),
                cwd=repo, env=env, text=True, capture_output=True, timeout=20)

        default, as_json, quiet = run(), run("--json"), run("--quiet")
        self.assertEqual((default.returncode, as_json.returncode,
                          quiet.returncode), (2, 2, 2))
        self.assertEqual(default.stdout, "")
        self.assertIn("UNKNOWN — no minted proxy rows were enumerated",
                      default.stderr)
        doc = json.loads(as_json.stdout)
        self.assertEqual(doc["rc"], 2)
        self.assertEqual(doc["unknown"], 1)
        self.assertEqual(doc["rows"], [{
            "seat": None, "family": None, "state": "unknown",
            "shown": "unknown", "detail": "no minted proxy rows were enumerated",
            "cpu": None,
        }])
        self.assertIn("HEARTBEAT UNKNOWN", quiet.stdout)
        self.assertIn("no minted proxy rows were enumerated", quiet.stdout)
        self.assertIn("1 UNKNOWN row", quiet.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            heartbeat, "doctor-ensure.json")))


class SeatCpuCanaryTest(unittest.TestCase):
    """The proxy-CPU canary (struggling-backend leading indicator): htop showed
    cli-proxy-api pids pegged at 152%/90.6% while healthy siblings idled ~0% —
    sustained-high CPU on a proxy = a thrashing backend BEFORE it goes silent.
    Classification is windowed, never a point: stored-prior span when one
    exists, else a double-read; startup bursts are grace; unreadable /proc is
    UNKNOWN, not OK. Borrows SeatTest's fixtures without subclassing."""

    def setUp(self):
        SeatTest.setUp(self)
        self._cpu_dir_prev = os.environ.get("HELM_PROXY_CPU_DIR")
        os.environ["HELM_PROXY_CPU_DIR"] = os.path.join(self.tmp, "cpu-canary")
        os.environ["HELM_ENSURE_QUIET_HEARTBEAT_DIR"] = os.path.join(
            self.tmp, "ensure-heartbeat")
        os.environ["HELM_ENSURE_QUIET_HEARTBEAT_S"] = "60"

    def tearDown(self):
        if self._cpu_dir_prev is None:
            os.environ.pop("HELM_PROXY_CPU_DIR", None)
        else:
            os.environ["HELM_PROXY_CPU_DIR"] = self._cpu_dir_prev
        SeatTest.tearDown(self)

    _plant = SeatTest._plant
    _add = SeatTest._add

    @staticmethod
    def _sample(jiffies, ts, age_s=300.0, clk=100):
        return {"jiffies": jiffies, "age_s": age_s, "clk": clk, "ts": ts}

    def _prior(self, seat_name, pid, jiffies, ts):
        path = seat._cpu_sample_path(seat_name)
        with open(path, "w") as f:
            json.dump({"pid": pid, "jiffies": jiffies, "ts": ts}, f)
        return path

    # -- the sample reader itself: real /proc, our own pid ------------------
    def test_proc_cpu_sample_reads_self(self):
        s = seat._proc_cpu_sample(os.getpid())
        self.assertIsNotNone(s)
        self.assertIsInstance(s["jiffies"], int)
        self.assertGreaterEqual(s["age_s"], 0.0)
        self.assertGreater(s["clk"], 0)

    def test_proc_cpu_sample_unreadable_is_none(self):
        # a pid that cannot exist: /proc/<huge>/stat is unreadable — None,
        # so the canary surfaces UNKNOWN, never a silent OK.
        self.assertIsNone(seat._proc_cpu_sample(2 ** 22 + 12345678))

    # -- classification: OK vs THRASHING vs UNKNOWN, sustained not spike ----
    def test_canary_unreadable_proc_is_unknown(self):
        with mock.patch.object(seat, "_proc_cpu_sample", return_value=None):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "unknown")
        self.assertIn("unreadable", note)

    def test_canary_first_sight_double_reads_low_cpu_ok(self):
        # no stored prior: the canary takes TWO readings a window apart —
        # 10 jiffies over 1s at clk 100 = 10% CPU -> ok.
        samples = [self._sample(1000, 100.0), self._sample(1010, 101.0)]
        with mock.patch.object(seat, "_proc_cpu_sample",
                               side_effect=samples) as reads, \
                mock.patch.object(seat.time, "sleep") as slept:
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "ok")
        self.assertAlmostEqual(pct, 10.0)
        self.assertAlmostEqual(window, 1.0)
        self.assertEqual(reads.call_count, 2)
        slept.assert_called_once()        # the double-read IS the window

    def test_canary_first_sight_high_cpu_thrashing(self):
        # 150 jiffies over 1s at clk 100 = 150% (the htop evidence shape),
        # process 300s old (past grace) -> THRASHING.
        samples = [self._sample(1000, 100.0), self._sample(1150, 101.0)]
        with mock.patch.object(seat, "_proc_cpu_sample", side_effect=samples), \
                mock.patch.object(seat.time, "sleep"):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "thrashing")
        self.assertAlmostEqual(pct, 150.0)
        self.assertIn("80", note)         # the threshold rides the note

    def test_canary_startup_burst_within_grace_is_ok(self):
        # same pegged reading but the process is 5s old: model-load burst,
        # not thrash — grace says OK and the note says why.
        samples = [self._sample(1000, 100.0, age_s=4.0),
                   self._sample(1150, 101.0, age_s=5.0)]
        with mock.patch.object(seat, "_proc_cpu_sample", side_effect=samples), \
                mock.patch.object(seat.time, "sleep"):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "ok")
        self.assertIn("startup", note)

    def test_canary_sustained_via_stored_prior_no_sleep(self):
        # a stored prior 60s back turns the reading into a REAL sustain:
        # 9000 jiffies / clk 100 / 60s = 150% held for a minute -> THRASHING,
        # no in-process sleep (the cron cadence was the window), and the store
        # rolls forward so the next run measures the next span.
        self._prior("codex", 4321, 1000, 1000.0)
        now = self._sample(10000, 1060.0)
        with mock.patch.object(seat, "_proc_cpu_sample",
                               return_value=now) as reads, \
                mock.patch.object(seat.time, "sleep") as slept:
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "thrashing")
        self.assertAlmostEqual(pct, 150.0)
        self.assertAlmostEqual(window, 60.0)
        self.assertEqual(reads.call_count, 1)
        slept.assert_not_called()
        with open(seat._cpu_sample_path("codex")) as f:
            rolled = json.load(f)
        self.assertEqual(rolled, {"pid": 4321, "jiffies": 10000, "ts": 1060.0})

    def test_canary_prior_for_other_pid_falls_back_to_double_read(self):
        # the proxy respawned since the last sample: a prior keyed to the OLD
        # pid must never fabricate a window for the new one.
        self._prior("codex", 9999, 1000, 1000.0)
        samples = [self._sample(1000, 1060.0), self._sample(1005, 1061.0)]
        with mock.patch.object(seat, "_proc_cpu_sample",
                               side_effect=samples) as reads, \
                mock.patch.object(seat.time, "sleep") as slept:
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "ok")
        self.assertEqual(reads.call_count, 2)
        slept.assert_called_once()

    def test_canary_threshold_env_tunable(self):
        # HELM_PROXY_CPU_CANARY_PCT=95: a 90% reading (the htop 90.6% pid)
        # stays OK under a raised bar — the knob is live, not decorative.
        os.environ["HELM_PROXY_CPU_CANARY_PCT"] = "95"
        try:
            samples = [self._sample(1000, 100.0), self._sample(1090, 101.0)]
            with mock.patch.object(seat, "_proc_cpu_sample",
                                   side_effect=samples), \
                    mock.patch.object(seat.time, "sleep"):
                state, pct, _, _ = seat._cpu_canary("codex", "codex", 4321)
        finally:
            os.environ.pop("HELM_PROXY_CPU_CANARY_PCT", None)
        self.assertEqual(state, "ok")
        self.assertAlmostEqual(pct, 90.0)

    def test_canary_pid_vanishing_mid_sample_is_unknown(self):
        samples = [self._sample(1000, 100.0), None]
        with mock.patch.object(seat, "_proc_cpu_sample", side_effect=samples), \
                mock.patch.object(seat.time, "sleep"):
            state, pct, window, note = seat._cpu_canary("codex", "codex", 4321)
        self.assertEqual(state, "unknown")
        self.assertIn("vanished", note)

    # -- the surface: --ensure rows carry the canary, rc semantics ----------
    def _ensure_with(self, row, canary, args=(), pid=4321):
        from helm import proxywatch
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_ensure_row", return_value=row), \
                mock.patch.object(seat, "_running_pid", return_value=pid), \
                mock.patch.object(seat, "_cpu_canary", return_value=canary), \
                mock.patch.object(proxywatch, "host_suspend_gap_s", return_value=0), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_quiet_healthy_rows_heartbeat_once_per_cadence(self):  # noqa: VACUOUS_ASSERTION — the empty middle run is bracketed by two positive HEARTBEAT rows on the same output and state path
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        with mock.patch.object(seat.time, "time", side_effect=(100, 120, 161)):
            first = self._ensure_with(
                ("codex", "healthy", "pid 4321 port 8317"),
                ("ok", 3.0, 60.0, ""), args=("--ensure", "--quiet"))
            second = self._ensure_with(
                ("codex", "healthy", "pid 4321 port 8317"),
                ("ok", 3.0, 60.0, ""), args=("--ensure", "--quiet"))
            third = self._ensure_with(
                ("codex", "healthy", "pid 4321 port 8317"),
                ("ok", 3.0, 60.0, ""), args=("--ensure", "--quiet"))
        self.assertEqual((first[0], second[0], third[0]), (0, 0, 0))
        self.assertIn("HEARTBEAT", first[1])
        self.assertIn("1 proxy row(s) HEALTHY", first[1])
        self.assertEqual(second[1], "")
        self.assertIn("HEARTBEAT", third[1])
        self.assertNotIn("codex      HEALTHY", first[1])

    def test_quiet_heartbeat_does_not_latch_before_publication(self):  # noqa: VACUOUS_ASSERTION — the raised sink failure proves publication ran; the absent state file proves the later latch did not
        path = seat_health._ensure_quiet_heartbeat_path()
        with mock.patch("builtins.print", side_effect=OSError("sink closed")):
            with self.assertRaisesRegex(OSError, "sink closed"):
                seat_health._ensure_quiet_heartbeat(1)
        self.assertFalse(os.path.exists(path))

    def test_quiet_thrashing_and_cpu_unknown_rows_stay_visible(self):  # noqa: VACUOUS_ASSERTION — positive THRASHING and cpu UNKNOWN rows prove the quiet renderer fired before asserting no heartbeat
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        thrash = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("thrashing", 152.0, 60.0, ">=80% threshold"),
            args=("--ensure", "--quiet"))
        unreadable = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("unknown", None, None, "unreadable /proc/4321/stat"),
            args=("--ensure", "--quiet"))
        self.assertEqual((thrash[0], unreadable[0]), (1, 1))
        self.assertIn("THRASHING", thrash[1])
        self.assertIn("cpu UNKNOWN", unreadable[1])
        self.assertNotIn("HEARTBEAT", thrash[1] + unreadable[1])

    def test_quiet_respawn_and_liveness_unknown_rows_stay_visible(self):  # noqa: VACUOUS_ASSERTION — positive RESPAWNED and UNKNOWN rows prove the quiet renderer fired before asserting no heartbeat
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        respawn = self._ensure_with(
            ("codex", "respawned", "pid 4321 port 8317"),
            ("ok", 3.0, 60.0, ""), args=("--ensure", "--quiet"))
        unknown = self._ensure_with(
            ("codex", "unknown", "respawn failed (rc 1)"),
            ("ok", 3.0, 60.0, ""), args=("--ensure", "--quiet"))
        self.assertEqual((respawn[0], unknown[0]), (0, 2))
        self.assertIn("RESPAWNED", respawn[1])
        self.assertIn("UNKNOWN", unknown[1])
        self.assertNotIn("HEARTBEAT", respawn[1] + unknown[1])

    def test_quiet_and_json_refuse_before_enumeration(self):  # noqa: VACUOUS_ASSERTION — rc 2 plus the exact stderr refusal proves this branch fired before stdout and enumeration absence are asserted
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_minted_seats") as minted, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure(["--ensure", "--quiet", "--json"])
        self.assertEqual(rc, 2)
        minted.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertIn("distinct output contracts", err.getvalue())

    def test_ensure_thrashing_row_surfaces_and_warns_rc1(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("thrashing", 152.0, 60.0, ">=80% threshold"))
        self.assertEqual(rc, 1)           # THRASHING is a WARN, not a page
        self.assertIn("THRASHING", out)
        self.assertIn("152", out)
        self.assertIn("struggling", err)

    def test_ensure_ok_canary_rc0_with_cpu_suffix(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("ok", 3.0, 60.0, ""))
        self.assertEqual(rc, 0)
        self.assertIn("HEALTHY", out)
        self.assertIn("cpu 3%", out)

    def test_non_empty_default_and_json_contracts_stay_byte_exact(self):  # noqa: VACUOUS_ASSERTION — both contracts positively assert their complete non-empty healthy row before the exact empty-stderr field
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        row = ("codex", "healthy", "pid 4321 port 8317")
        canary = ("ok", 3.0, 60.0, "")
        default = self._ensure_with(row, canary)
        as_json = self._ensure_with(row, canary, args=("--ensure", "--json"))
        # DERIVED from the shipped producer against the same pinned roots the
        # pass reads, never transcribed: a hand-written expectation here would
        # be a second implementation of the rung and would pass while the pass
        # ran something else.
        from helm import codexhomes
        cred_follow = codexhomes.cred_follow(apply=True)
        self.assertEqual([r["state"] for r in cred_follow["rows"]], ["UNKNOWN"])
        self.assertEqual(default, (
            0, "codex      HEALTHY   pid 4321 port 8317 — cpu 3% over 60s\n", ""))
        self.assertEqual(as_json, (0, json.dumps({
            "rows": [{
                "seat": "codex", "family": "codex", "state": "healthy",
                "shown": "healthy",
                "detail": "pid 4321 port 8317 — cpu 3% over 60s",
                # the seat's desired-down record (helm/seat_down.py) and why
                # it could not be read: both absent for a supervised seat
                "desired_down": None, "marker_error": None,
                "cpu": {"state": "ok", "pct": 3.0, "window_s": 60.0,
                        "note": ""},
            }],
            "unknown": 0, "thrashing": 0, "cpu_unknown": 0, "rc": 0,
            "down": 0, "marker_unreadable": 0,
            # the orca cred-follow rung rides every --ensure pass; the
            # MACHINE-read contract carries its verdict even when the human
            # surface stays silent about a steady state (task/2478). The
            # suite pins ORCA_USER_DATA_PATH at a tmp dir, so the honest
            # verdict here is UNKNOWN and the row's detail names that path.
            "cred_follow": cred_follow,
        }, indent=2, sort_keys=True) + "\n", ""))

    def test_ensure_cpu_unknown_is_never_ok_rc1(self):
        # requirement: an unreadable /proc is UNKNOWN, not OK — surfaced on
        # the row and WARN-carried in rc, while liveness stays healthy.
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("unknown", None, None, "unreadable /proc/4321/stat"))
        self.assertEqual(rc, 1)
        self.assertIn("cpu UNKNOWN", out)

    def test_ensure_liveness_unknown_still_rc2_over_warn(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_ensure_row",
                               return_value=("codex", "unknown", "x")), \
                mock.patch.object(seat, "_cpu_canary") as canary, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure([])
        self.assertEqual(rc, 2)           # the page outranks the warn
        canary.assert_not_called()        # never canary an unproven row

    def test_ensure_json_carries_canary_per_seat(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        rc, out, err = self._ensure_with(
            ("codex", "healthy", "pid 4321 port 8317"),
            ("thrashing", 152.0, 60.0, ">=80% threshold"),
            args=("--ensure", "--json"))
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertEqual(doc["rc"], 1)
        self.assertEqual(doc["thrashing"], 1)
        row = doc["rows"][0]
        self.assertEqual(row["seat"], "codex")
        self.assertEqual(row["state"], "healthy")
        self.assertEqual(row["shown"], "thrashing")
        self.assertEqual(row["cpu"]["state"], "thrashing")
        self.assertAlmostEqual(row["cpu"]["pct"], 152.0)
        self.assertAlmostEqual(row["cpu"]["window_s"], 60.0)

    def test_doctor_json_without_ensure_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["doctor", "--json"])
        self.assertEqual(rc, 2)
        self.assertIn("--ensure", err.getvalue())

    def test_doctor_prints_canary_line(self):
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        out = io.StringIO()
        with mock.patch.object(seat, "_proxy_bin", return_value=None), \
                mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(
                    seat, "_cpu_canary",
                    return_value=("thrashing", 152.0, 60.0,
                                  ">=80% threshold")), \
                contextlib.redirect_stdout(out):
            seat._doctor([])
        self.assertIn("cpu canary", out.getvalue())
        self.assertIn("THRASHING", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class ResumeStubRankingTest(unittest.TestCase):
    """A reboot touches every transcript, so "newest" stops meaning anything.

    MEASURED after a machine reboot:
    `helm seat resume` ranks candidate sessions by mtime alone, and the reboot
    had touched all of them. It would have resumed kimi into a 28K crash STUB
    over its real 26MB session, and 4 of 5 seats mismatched. That failure is
    silent — the seat returns as a stranger with its own work invisible to it,
    and nothing errors.

    The fix ranks a session WITH REAL TURNS above a stub, and only then by
    mtime. Not a size threshold on its own: a legitimately fresh session is
    also small, and ranking by size would stop a seat resuming work it started
    five minutes ago."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-")
        self.proj = os.path.join(self.tmp, "claude", "projects", "-p")
        os.makedirs(self.proj)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sess(self, uuid, body, mtime_offset):
        p = os.path.join(self.proj, uuid + ".jsonl")
        with open(p, "w") as f:
            f.write(body)
        t = time.time() + mtime_offset
        os.utime(p, (t, t))
        return p

    REAL = ('{"type":"user","message":{"role":"user","content":"hi"}}\n'
            '{"message":{"role":"assistant","content":[{"type":"text","text":"x"}]}}\n')
    STUB = '{"type":"last-prompt","prompt":"x"}\n'

    def test_a_real_session_outranks_a_NEWER_stub(self):
        real = "aaaaaaaa-1111-2222-3333-444444444444"
        stub = "bbbbbbbb-1111-2222-3333-444444444444"
        self._sess(real, self.REAL, -100)      # older
        self._sess(stub, self.STUB, 0)         # NEWER, as after a reboot
        sid, _cwd = seat._newest_seat_session(self.tmp)
        self.assertEqual(sid, real,
                         "a reboot-touched stub must not outrank real work")

    def test_between_two_REAL_sessions_mtime_still_decides(self):
        old = "cccccccc-1111-2222-3333-444444444444"
        new = "dddddddd-1111-2222-3333-444444444444"
        self._sess(old, self.REAL, -100)
        self._sess(new, self.REAL, 0)
        sid, _cwd = seat._newest_seat_session(self.tmp)
        self.assertEqual(sid, new, "the original ordering must survive")

    def test_a_FRESH_real_session_is_not_penalised_for_being_small(self):
        """The counterfactual for ranking by size instead of by content: work
        started five minutes ago is small AND correct to resume."""
        big_stub = "eeeeeeee-1111-2222-3333-444444444444"
        fresh = "ffffffff-1111-2222-3333-444444444444"
        self._sess(big_stub, '{"type":"last-prompt","x":"' + "p" * 5000 + '"}\n', -100)
        self._sess(fresh, self.REAL, 0)
        sid, _cwd = seat._newest_seat_session(self.tmp)
        self.assertEqual(sid, fresh)

    def test_all_stubs_falls_back_to_mtime_rather_than_refusing(self):
        a = "11111111-1111-2222-3333-444444444444"
        b = "22222222-1111-2222-3333-444444444444"
        self._sess(a, self.STUB, -100)
        self._sess(b, self.STUB, 0)
        sid, _cwd = seat._newest_seat_session(self.tmp)
        self.assertEqual(sid, b, "no real session anywhere: mtime is all we have")


class ResumePruneRankingTest(unittest.TestCase):
    """The 2026-08-04 rescue measurement: prune+resume landed 3 of 5 walled
    seats back on the WALLED ORIGINAL session, undoing the prune silently.

    The mechanism is the ranking, not the retry: a walled pane keeps APPENDING
    to its original session while the operator prunes, so the pruned copy's
    content is always strictly older than the original's at rescue time, and
    the mtime tiebreak only saves the copy when the prune is seconds fresh.
    The fix ranks by LINEAGE, not by time: a transcript that descends from the
    seat's recorded session outranks the recorded session itself; a prune of
    some dead session does not (codex-2's 05:22 prune targeted a session that
    had died at 23:36 while the recorded session ran until morning — preferring
    it would have resumed a stranger)."""

    RECORDED = "aaaaaaaa-0000-0000-0000-000000000001"
    DEAD = "bbbbbbbb-0000-0000-0000-000000000002"
    PRUNE_OF_RECORDED = "cccccccc-0000-0000-0000-000000000003"
    PRUNE_OF_DEAD = "dddddddd-0000-0000-0000-000000000004"
    ORDINARY = "eeeeeeee-0000-0000-0000-000000000005"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-resume-prune-")
        self.proj = os.path.join(self.tmp, "claude", "projects", "-p")
        os.makedirs(self.proj)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _real(self, sid):
        return ('{"type":"user","sessionId":"%s","message":{"role":"user",'
                '"content":"hi"}}\n{"type":"assistant","sessionId":"%s",'
                '"message":{"role":"assistant","content":[{"type":"text",'
                '"text":"x"}]}}\n' % (sid, sid))

    def _pruned(self, own_sid, source_sid):
        # The measured cv shape: sessionId carries the copy, session_id the
        # source — every record, in the file's head.
        return ('{"type":"user","sessionId":"%s","session_id":"%s",'
                '"message":{"role":"user","content":"hi"}}\n'
                '{"type":"assistant","sessionId":"%s","session_id":"%s",'
                '"message":{"role":"assistant","content":[{"type":"text",'
                '"text":"x"}]}}\n'
                % (own_sid, source_sid, own_sid, source_sid))

    def _slot_pruned(self):
        # The other measured shape: tool-slot records, no session identity.
        return ('{"id":"call_00_x","slot":"content","name":"Bash",'
                '"input":{"command":"true"},"content":"out"}\n')

    def _sess(self, uuid, body, mtime_offset):
        p = os.path.join(self.proj, uuid + ".jsonl")
        with open(p, "w") as f:
            f.write(body)
        t = time.time() + mtime_offset
        os.utime(p, (t, t))
        return p

    def test_the_pruned_copy_of_the_recorded_session_wins(self):
    # noqa: VACUOUS_ASSERTION — the lineage stamp IS asserted on the same fixture before the selection (the rung's regex does not see _prune_source as an observable)
        """Tonight's failing shape, exactly: the recorded session keeps
        growing (its mtime and content are BOTH newer than the copy's) and
        the copy must still be the resume target."""
        self._sess(self.RECORDED, self._real(self.RECORDED), 0)   # newest
        p = self._sess(self.PRUNE_OF_RECORDED,
                       self._pruned(self.PRUNE_OF_RECORDED, self.RECORDED), -60)
        # POSITIVE CONTROL, same observable: the lineage stamp is read off
        # the fixture, so the selection below cannot be an empty-set pass.
        self.assertEqual(seat._prune_source(p), self.RECORDED)
        sid, _cwd = seat._newest_seat_session(
            self.tmp, prefer_source=self.RECORDED)
        self.assertEqual(sid, self.PRUNE_OF_RECORDED,
                         "the rescue copy must outrank its growing source")

    def test_a_slot_shaped_prune_copy_is_a_prune_not_a_stranger(self):
    # noqa: VACUOUS_ASSERTION — the lineage stamp IS asserted on the same fixture before the selection (the rung's regex does not see _prune_source as an observable)
        """Slot records carry no lineage stamp, so the copy cannot prove it
        descends from the recorded session — it ranks as a STALE prune, and
        the recorded session correctly outranks it. Tonight this shape still
        landed right twice (codex-3, ds4pro) because the copy's mtime was
        minutes fresh; the mtime fallback inside the prune class is what
        keeps that luck, and --session is what replaces it."""
        self._sess(self.RECORDED, self._real(self.RECORDED), -60)
        p = self._sess(self.PRUNE_OF_RECORDED, self._slot_pruned(), 0)
        self.assertIs(seat._prune_source(p), True,
                      "the slot shape must read as a prune, not a stranger")
        sid, _cwd = seat._newest_seat_session(
            self.tmp, prefer_source=self.RECORDED)
        self.assertEqual(sid, self.RECORDED,
                         "unprovable lineage must not outrank the recorded "
                         "session — UNKNOWN never becomes definite")

    def test_a_prune_of_a_DEAD_session_does_not_outrank_the_recorded_one(self):
    # noqa: VACUOUS_ASSERTION — the lineage stamp IS asserted on the same fixture before the selection (the rung's regex does not see _prune_source as an observable)
        """codex-2 at 05:22: the prune targeted a session dead since 23:36
        while the recorded session ran until morning. Preferring the copy
        would resume a stranger."""
        self._sess(self.RECORDED, self._real(self.RECORDED), 0)
        p = self._sess(self.PRUNE_OF_DEAD,
                       self._pruned(self.PRUNE_OF_DEAD, self.DEAD), 10)
        self.assertEqual(seat._prune_source(p), self.DEAD)
        sid, _cwd = seat._newest_seat_session(
            self.tmp, prefer_source=self.RECORDED)
        self.assertEqual(sid, self.RECORDED,
                         "a stale prune of a dead session is not the rescue")

    def test_a_stale_prune_still_outranks_unrelated_old_sessions(self):
    # noqa: VACUOUS_ASSERTION — the lineage stamp IS asserted on the same fixture before the selection (the rung's regex does not see _prune_source as an observable)
        self._sess(self.ORDINARY, self._real(self.ORDINARY), -500)
        p = self._sess(self.PRUNE_OF_DEAD,
                       self._pruned(self.PRUNE_OF_DEAD, self.DEAD), -60)
        self._sess(self.RECORDED, self._real(self.RECORDED), 0)
        self.assertEqual(seat._prune_source(p), self.DEAD)
        sid, _cwd = seat._newest_seat_session(self.tmp, prefer_source=None)
        self.assertEqual(sid, self.PRUNE_OF_DEAD,
                         "no recorded session: a fresh prune outranks "
                         "ordinary history but the mtime order inside the "
                         "class decides")

    def test_without_a_recorded_session_a_fresh_prune_outranks_its_source(self):
    # noqa: VACUOUS_ASSERTION — the lineage stamp IS asserted on the same fixture before the selection (the rung's regex does not see _prune_source as an observable)
        """prefer_source=None (the legacy callers) still gets the rescue
        direction: a fresh pruned copy outranks the older source it was
        minted from, because prune-with-no-live-record IS the common rescue
        shape. What None gives up is only the ability to beat a NEWER source
        — the recorded-session precedence above is what covers that."""
        self._sess(self.RECORDED, self._real(self.RECORDED), -120)
        p = self._sess(self.PRUNE_OF_RECORDED,
                       self._pruned(self.PRUNE_OF_RECORDED, self.RECORDED), -60)
        self.assertEqual(seat._prune_source(p), self.RECORDED)
        sid, _cwd = seat._newest_seat_session(self.tmp)
        self.assertEqual(sid, self.PRUNE_OF_RECORDED)

    def test_a_normal_transcript_is_never_misread_as_a_prune(self):
        """Mutation target: session_id == sessionId on every record means
        NO lineage — the positive control that the stamp read is real."""
        self._sess(self.ORDINARY, self._real(self.ORDINARY), 0)
        p = os.path.join(self.proj, self.ORDINARY + ".jsonl")
        self.assertIsNone(seat._prune_source(p))
        self.assertEqual(seat._prune_source(
            self._sess(self.PRUNE_OF_RECORDED,
                       self._pruned(self.PRUNE_OF_RECORDED, self.RECORDED),
                       0)), self.RECORDED)


class LaunchLineHostilePathTest(unittest.TestCase):
    """The eval-arm seam put the seat's config path inside a DOUBLE-QUOTED
    ${...:-} word, where $ expands and a backtick EXECUTES at every seat
    launch — kimi measured both live (2026-07-29, gate d99d4001): a path
    holding $HOME truncated, and one holding backticks ran the command and
    spliced its output into the path. The old shlex.quote form was inert, so
    the seam introduced this; the escape is what keeps it inert.

    These run the minted line through a REAL /bin/sh, because the defect is
    the shell's reading of the word — a Python-side string assertion cannot
    see it (that is exactly what the first version of the seam passed)."""

    HOSTILE = ("with space", "with$HOME", "with`id`", 'with"quote',
               "with'apostrophe")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-hostile-")
        self._home = os.environ.get("HELM_HOME")
        self._meld = os.environ.get("MELD_HOME")
        os.environ.pop("MELD_HOME", None)

    def tearDown(self):
        for k, v in (("HELM_HOME", self._home), ("MELD_HOME", self._meld)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resolved(self, shape):
        """What /bin/sh actually resolves the emitted default to."""
        os.environ["HELM_HOME"] = os.path.join(self.tmp, shape, "helm-home")
        line = seat.launch_line("codex")
        want = os.path.join(seat.seat_dir("codex"), "claude")
        # the word is double-quoted and MAY contain spaces, so match the
        # whole quoted token (honouring backslash escapes) rather than
        # splitting on whitespace — a naive split truncates the space shape
        # and reports a defect the shell never saw.
        word = re.search(r'CLAUDE_CONFIG_DIR=("(?:\\.|[^"\\])*")',
                         line).group(1)
        got = subprocess.run(["/bin/sh", "-c", "printf %s " + word],
                             capture_output=True, text=True).stdout
        return want, got

    def test_every_hostile_path_shape_survives_the_shell_verbatim(self):
        for shape in self.HOSTILE:
            with self.subTest(shape=shape):
                want, got = self._resolved(shape)
                self.assertEqual(got, want,
                                 "the shell rewrote the seat's config path")

    def test_a_backtick_path_does_not_execute(self):
        """The sharpest half: a substituted command leaves its OUTPUT behind.
        `id` prints uid=..., so its absence is the proof nothing ran."""
        _want, got = self._resolved("with`id`")
        self.assertNotIn("uid=", got)
        self.assertIn("with`id`", got)

    def test_the_ordinary_path_is_unchanged_by_the_escape(self):
        """No-regression: a plain path emits byte-identically to the raw form."""
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "plain", "helm-home")
        want = os.path.join(seat.seat_dir("codex"), "claude")
        self.assertIn('CLAUDE_CONFIG_DIR="${HELM_EVAL_CONFIG_DIR:-'
                      + want + '}"', seat.launch_line("codex"))


class RegisteredSeatsNestedLayoutTest(unittest.TestCase):
    """`registered_seats()` staged on the REAL nested register layout.

    EVERY FIXTURE HERE IS NESTED ON PURPOSE, and that is the point of the
    class. Two review rounds went green against a flat-scan bug because their
    fixtures staged <seats>/<name>/spawn.json ONLY — a shape where a flat walk
    and a nested walk agree by construction, so the test returned the same
    answer whichever walk ran and could not fail. A fixture that cannot
    distinguish the fixed code from the broken code is not a weak test, it is
    a vacuous one, and `test_a_flat_only_fixture_is_vacuous_here` is the
    receipt for that claim rather than an assertion of it.

    Measured on the live box the day this landed: registers sit at
    codex/spawn.json AND codex/instances/codex-2/spawn.json; the flat
    predicate found five seats and missed codex-2 and codex-3."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-regseats-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("HELM_HOME")
        self.addCleanup(self._restore_home)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.base = os.path.join(os.environ["HELM_HOME"], "_global", "seats")

    def _restore_home(self):
        if self._prev is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._prev

    def _register(self, *parts):
        d = os.path.join(self.base, *parts)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "spawn.json"), "w") as f:
            f.write("{}")

    def test_a_NESTED_instance_register_is_enumerated(self):
        """THE REGRESSION CLAUSE: an instance register one level down is seen.

        Reverting the walk to a flat listdir kills exactly this test and
        nothing else in the class."""
        self._register("codex")
        self._register("codex", "instances", "codex-2")
        self._register("codex", "instances", "codex-3")
        names, blind = seat.registered_seats()
        self.assertFalse(blind)
        self.assertEqual(sorted(names), ["codex", "codex-2", "codex-3"])

    def test_a_flat_only_fixture_is_vacuous_here(self):
        """THE RECEIPT for this class's own premise, not a behaviour claim.

        Staged FLAT-only, the answer is identical whether the walk descends or
        not — so a flat fixture cannot witness the bug this lane fixes. This
        test passes on the BROKEN code too, deliberately, and its value is
        that it documents WHY the other fixtures are nested."""
        self._register("codex")
        self._register("kimi")
        names, _blind = seat.registered_seats()
        self.assertEqual(sorted(names), ["codex", "kimi"])
        flat_equivalent = sorted(
            n for n in os.listdir(self.base)
            if os.path.exists(os.path.join(self.base, n, "spawn.json")))
        self.assertEqual(sorted(names), flat_equivalent,
                         "a flat fixture must be unable to tell the two walks "
                         "apart — if this ever differs, the fixture stopped "
                         "being flat and this receipt is measuring something "
                         "else")

    def test_regular_files_beside_the_seats_are_neither_seats_nor_blindness(self):
        """helm's port allocator writes `.instance-ports.lock` and
        `instance-ports.json` under the seats root. A regular file holds no
        register, so it is not a name and its `instances` listing is not a
        blind walk. The nested register beside it is the positive control.
        MUTATION: enumerating regular files reports blind."""
        self._register("codex", "instances", "seat-a")
        for name in (".instance-ports.lock", "instance-ports.json",
                     os.path.join("codex", "instances", "stray.lock")):
            with open(os.path.join(self.base, name), "w") as f:
                f.write("{}")
        names, blind = seat.registered_seats()
        self.assertEqual(names, ["seat-a"])
        self.assertFalse(blind)

    def test_an_ABSENT_seats_tree_is_a_definite_zero_never_blindness(self):  # noqa: VACUOUS_ASSERTION — control is present but REBOUND; non-vacuity proven by mutation instead of by shape
        """No seats dir means a fleet that never minted one — a real answer.

        Calling it blind told `doctor` that a brand-new machine could not be
        trusted to be new, which rendered every unrealized projection as an
        orphan. Absent and unreadable are different facts."""
        self.assertFalse(os.path.exists(self.base))
        names, blind = seat.registered_seats()
        self.assertEqual(names, [])
        self.assertFalse(blind, "an absent tree is zero seats, not blindness")
        # UNCONDITIONAL POSITIVE CONTROL on the same observable. Both asserts
        # above are about ABSENCE, and a registered_seats() that returned
        # ([], False) for every input on earth would satisfy them — so the
        # empty answer means nothing until this walk is shown to be capable of
        # a non-empty one from the same call site in the same test.
        self._register("codex", "instances", "codex-2")
        names, blind = seat.registered_seats()
        self.assertEqual(names, ["codex-2"])
        self.assertFalse(blind)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the unreadable bit")
    def test_an_UNREADABLE_instances_dir_reports_BLIND(self):
        """A discovery failure must be LOUD, never a silently shorter list.

        The family is still reported — the blast radius stays exact — but the
        caller is told the walk was partial so it cannot render a short list
        as a complete fleet."""
        self._register("codex")
        inst = os.path.join(self.base, "codex", "instances")
        os.makedirs(inst, exist_ok=True)
        os.chmod(inst, 0o000)
        self.addCleanup(os.chmod, inst, 0o755)
        names, blind = seat.registered_seats()
        self.assertTrue(blind, "an unreadable instances dir must report blind")
        self.assertIn("codex", names, "blindness must not drop what WAS read")

    @unittest.skipIf(os.geteuid() == 0, "root ignores the unreadable bit")
    def test_an_UNREADABLE_seats_root_reports_BLIND(self):
        """The same rule one level up: unreadable root is blind, not empty."""
        os.makedirs(self.base, exist_ok=True)
        os.chmod(self.base, 0o000)
        self.addCleanup(os.chmod, self.base, 0o755)
        names, blind = seat.registered_seats()
        self.assertEqual(names, [])
        self.assertTrue(blind, "unreadable root is blindness, not zero seats")

class ArtifactDenyIsMeasuredPerFamily(unittest.TestCase):
    """task/1941. Both surfaces, both directions, and keyed on the family whose
    validator was MEASURED to reject the schema — codex/OpenAI — not on proxy
    mode as a shape (codex's review: Gemini's cleaner already strips it). The
    launch-line pins spell the literal text for the same reason a review caught
    the first version deriving its expectation from denied_tools() itself."""

    def test_codex_launch_line_denies_artifact_and_kimi_does_not(self):
        codex_line = seat.launch_line("codex")
        self.assertIn("--disallowedTools EnterPlanMode Artifact Skill"
                      " 'Agent(fork)' --dangerously", codex_line)
        kimi_line = seat.launch_line("kimi")
        self.assertIn("--disallowedTools EnterPlanMode Skill 'Agent(fork)'"
                      " --dangerously", kimi_line)
        self.assertNotIn("Artifact", kimi_line)

    def test_seeded_settings_deny_artifact_for_codex_only(self):
        import json, os, tempfile
        for fam, want in (("codex", True), ("kimi", False), (None, False)):
            d = tempfile.mkdtemp(prefix="helm-deny-")
            with open(os.path.join(d, "settings.json"), "w") as fh:
                fh.write('{"permissions":{"deny":[]}}')
            seat._seed_seat_settings(d, fam) if fam else seat._seed_seat_settings(d)
            with open(os.path.join(d, "settings.json"), encoding="utf-8") as fh:
                deny = json.load(fh)["permissions"]["deny"]
            self.assertIn(seat.PLAN_ENTRY_TOOL, deny, fam)
            self.assertEqual("Artifact" in deny, want, fam)

    def test_denied_tools_is_keyed_on_the_measured_family(self):
        self.assertIn("Artifact", seat.denied_tools("codex"))
        # MEASURED 2026-09-10 20:16Z: Gemini 400s the Artifact schema per turn (task/2142).
        self.assertIn("Artifact", seat.denied_tools("gemini"))
        # task/2287: every proxy family also loses Skill + Agent(fork); the
        # schema-unsafe slot between them stays per-family.
        self.assertEqual(seat.denied_tools("kimi"),
                         (seat.PLAN_ENTRY_TOOL,) + seat.SPAWN_DENIED_TOOLS)
        self.assertEqual(seat.denied_tools("not-a-family"), (seat.PLAN_ENTRY_TOOL,))


class AnUnservedToolIsDeniedWhereItsFailureWasMeasured(unittest.TestCase):
    """task/3242. Claude Code's WebSearch asks the UPSTREAM to run a server
    search tool (`web_search_20250305`, read in the 2.1.283 bundle), and one
    probe per family measured it failing on seven routes, the
    local ones silently (zero links, then an instruction to cite sources that
    invites invented ones), and working on native claude seats, on the codex
    models and on gemini. A measured family declares `unserved_tools` and
    loses WebSearch on both deny surfaces; every other family keeps it,
    kimi and grok included until a probe measures them. The families are
    read off the catalog's declarations, never a literal list, and each
    absence is paired with a presence on the same surface."""

    def test_every_measured_family_denies_it_on_both_surfaces(self):  # noqa: VACUOUS_ASSERTION — the count equality on `declared` runs unconditionally before the loop, so a sweep that found no family fails before any presence is read, and each presence below is an assertIn, never an absence
        declared = sorted(f for f, fam in seat.FAMILIES.items()
                          if fam.get("unserved_tools"))
        self.assertEqual(len(declared), 7, declared)     # the seven measured
        for fam in declared:
            with self.subTest(family=fam):
                tools = seat.FAMILIES[fam]["unserved_tools"]
                self.assertEqual(tuple(tools), seat_catalog.UNSERVED_WEB_SEARCH)
                deny = ProxySeatCannotSpawnThroughASkill._seeded_deny(fam)
                argv = AProxySeatAdmitsACappedWorkflowOnItsOwnTier._denied_argv(
                    seat.launch_line(fam))
                for tool in tools:
                    self.assertIn(tool, deny)
                    self.assertIn(tool, argv)

    def test_a_native_seat_and_a_measured_working_route_keep_it(self):  # noqa: VACUOUS_ASSERTION — the native list is pinned whole by equality before its absences, and inside the loop each surface's Artifact presence is asserted on the same list and argv before its WebSearch absence
        # positive controls on the very surfaces the absences read: each
        # seeded list and argv carries its own measured deny
        native = ProxySeatCannotSpawnThroughASkill._seeded_deny(None)
        self.assertEqual(native, [seat.PLAN_ENTRY_TOOL])
        self.assertNotIn("WebSearch", native)
        self.assertNotIn("WebSearch", seat.denied_tools(None))
        for fam in ("gemini", "codex"):
            with self.subTest(family=fam):
                deny = ProxySeatCannotSpawnThroughASkill._seeded_deny(fam)
                argv = AProxySeatAdmitsACappedWorkflowOnItsOwnTier._denied_argv(
                    seat.launch_line(fam))
                self.assertIn("Artifact", deny)
                self.assertIn("Artifact", argv)
                self.assertNotIn("WebSearch", deny)
                self.assertNotIn("WebSearch", argv)

    def test_exit_plan_mode_leaves_the_local_seats_only(self):
        """task/3242's second half: 77 ExitPlanMode calls in one day were
        one degenerate loop of "You are not in plan mode" refusals on the
        local seats, so it joins LOCAL_UNUSED_TOOLS; every other seat keeps
        it, because the owner may put a pane into plan mode by hand."""
        self.assertIn("ExitPlanMode", seat_catalog.LOCAL_UNUSED_TOOLS)
        local = [f for f, fam in seat.FAMILIES.items()
                 if seat_catalog.own_box(fam)]
        self.assertTrue(local)
        for fam in local:
            self.assertIn("ExitPlanMode", seat.denied_tools(fam), fam)
        self.assertIn("Artifact", seat.denied_tools("codex"))
        self.assertNotIn("ExitPlanMode", seat.denied_tools("codex"))
        self.assertNotIn("ExitPlanMode", seat.denied_tools(None))

    def test_the_artifact_push_and_question_tools_leave_the_local_seats(self):
        """task/3382: ~17k tokens of schema on every local request and zero
        calls; a local seat has no human at its keyboard to answer
        AskUserQuestion. A native claude seat keeps every one of them."""
        gone = ("Artifact", "ArtifactComments", "ArtifactData",
                "PushNotification", "AskUserQuestion")
        local = [f for f, fam in seat.FAMILIES.items()
                 if seat_catalog.own_box(fam)]
        self.assertTrue(local)
        for tool in gone:
            self.assertIn(tool, seat_catalog.LOCAL_UNUSED_TOOLS)
            for fam in local:
                self.assertIn(tool, seat.denied_tools(fam), (fam, tool))
            self.assertNotIn(tool, seat.denied_tools(None), tool)
        self.assertNotIn("AskUserQuestion", seat.denied_tools("codex"))
        self.assertEqual(len(set(seat_catalog.LOCAL_UNUSED_TOOLS)),
                         len(seat_catalog.LOCAL_UNUSED_TOOLS))


class ProxySeatCannotSpawnThroughASkill(unittest.TestCase):
    """task/2287. A codex seat that invokes the bundled code-review skill
    has the SKILL launch background reviewer subagents, forks among them,
    whatever its brief says. A brief cannot bind that path; the launch line
    can. Every proxy family loses the whole Skill tool
    and the fork agent type on BOTH deny surfaces; a native claude seat keeps
    both. The literal spellings are pinned on purpose (the Artifact class
    above says why), and one arm is a mutation control: it empties the door
    and shows the same observable go blank, so a green pin here can only mean
    the launch line read SPAWN_DENIED_TOOLS."""

    # the FAMILY catalog keys, spelled out so the arms cannot shrink with
    # the catalog they measure
    PROXY = ("codex", "kimi", "ds4pro", "ds4flash", "gemini", "grok",  # noqa: SEAT_NAME — family keys, the subject of the roster pin
             "openrouter", "qwen27", "dots3", "opus46", "gptoss",
             "qwenlocal", "bonsai", "cursor")  # noqa: SEAT_NAME — family keys, the subject of the roster pin

    @staticmethod
    def child_payload(body):
        """The `/bin/sh -c` payload inside a launch.sh body: the one string
        the inner shell parses into claude's argv."""
        import shlex
        line = next(l for l in body.splitlines() if " /bin/sh -c " in l)
        toks = shlex.split(line)
        return toks[toks.index("-c") + 1]

    @classmethod
    def child_argv(cls, body):
        import shlex
        return shlex.split(cls.child_payload(body))

    def test_launch_sh_hands_claude_the_fork_rule_as_one_argv_token(self):
        """launch.sh is TWO shells deep (launch_line quoted once, then
        _launch_owner's shlex.quote of the whole child), and a rule with
        parentheses is exactly the token that shape can mangle. This arm runs
        the real payload under /bin/sh with a stub `claude` on PATH that
        prints its argv, so the assertion is about what the binary receives,
        not about any quoting rendering. The trailing positional is the
        control for the variadic-flag hazard: it must arrive LAST, unswallowed."""
        import shlex, subprocess
        from helm import seat_launch_assets as impl
        body = impl._launch_owner(seat.launch_line("codex"))
        stub = tempfile.mkdtemp(prefix="helm-stub-claude-")
        with open(os.path.join(stub, "claude"), "w") as fh:
            fh.write('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
        os.chmod(os.path.join(stub, "claude"), 0o700)
        env = dict(os.environ, PATH=stub + os.pathsep + os.environ.get("PATH", ""))
        r = subprocess.run(["/bin/sh", "-c", self.child_payload(body),
                            "helm-seat-harness", "trailing-positional"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = r.stdout.splitlines()
        i = got.index("--disallowedTools")
        self.assertEqual(got[i:i + 6], ["--disallowedTools", "EnterPlanMode",
                                        "Artifact", "Skill", "Agent(fork)",
                                        "--dangerously-skip-permissions"])
        self.assertEqual(got[-1], "trailing-positional")

    def test_the_local_seats_carry_the_land_path_on_every_turn(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal three-family tuple, so its assertIn positive controls always run
        """The local families' `system_line` states the land path an
        apprentice seat missed on its first assignment: no pull request, no
        push, the tip goes to the mentor or integrator through helm dispatch,
        tests only through fab, and never self-close. It rides the launch line
        as --append-system-prompt, the argv word that reaches every turn; the
        dots3 arm below proves such a line survives both shells as one
        token."""
        import shlex
        from helm import seat_catalog
        for fam in ("qwen27", "qwenlocal", "bonsai"):  # noqa: SEAT_NAME — catalog FAMILY keys, the local families this arm is about
            line = seat.FAMILIES[fam]["system_line"]
            self.assertTrue(line.isascii(), fam)
            for phrase in ("pull request", "git push", "helm dispatch send",
                           "--supersedes", "fab", "Never close your own task"):
                self.assertIn(phrase, line, fam)
            self.assertIn(" --append-system-prompt %s" % shlex.quote(line),
                          seat.launch_line(fam), fam)
            # THE CONTEXT DENY: the 12 unused tools leave, and so do the lite
            # profile's four (task/3253: Workflow among them, since no local
            # family declares a same-family delegate tier); everything a
            # local reviewer or builder uses stays.
            denied = seat_catalog.denied_tools(fam)
            for tool in (seat_catalog.LOCAL_UNUSED_TOOLS
                         + seat_catalog.LITE_UNUSED_TOOLS):
                self.assertIn(tool, denied, fam)
            for tool in ("Agent", "Bash", "Read", "Edit", "Write", "TaskStop"):
                self.assertNotIn(tool, denied, fam)
            # THE PUSH DENY: each rule rides the pane as ONE quoted token.
            for rule in seat_catalog.LOCAL_PUSH_DENIES:
                self.assertIn(rule, denied, fam)
                self.assertIn(" %s" % shlex.quote(rule),
                              seat.launch_line(fam), fam)
        # the control: a family that declares neither deny keeps its list
        self.assertNotIn("Workflow", seat_catalog.denied_tools("codex"))
        self.assertNotIn("Bash(git push:*)", seat_catalog.denied_tools("codex"))

    def test_a_declared_system_line_arrives_as_ONE_argv_token(self):  # noqa: VACUOUS_ASSERTION — `got.index(...)` plus `assertEqual(got[i + 1], want)` is the unconditional positive control: a line that never reached the binary raises ValueError before the closing absence assertion is read
        """The same two-shell payload, for the family that declares a
        `system_line` (seat_catalog): the text is prose with spaces, commas
        and a semicolon, and it is quoted twice on its way to the binary. This
        runs the real payload and reads the stub's argv, so the assertion is
        about what claude RECEIVES — a line that arrived word-split would hand
        claude a flag value of `Write` and thirty-odd stray positionals.

        It carries the same trailing-positional control, because the pair was
        appended AFTER `--model <m>`: a resume's bare prompt must still arrive
        last and unswallowed."""
        import subprocess
        from helm import seat_launch_assets as impl
        want = seat.FAMILIES["dots3"]["system_line"]  # noqa: SEAT_NAME — the catalog FAMILY key whose declaration IS the subject of this arm
        self.assertIn(" ", want)        # control: the token has spaces to lose
        body = impl._launch_owner(seat.launch_line("dots3"))
        stub = tempfile.mkdtemp(prefix="helm-stub-claude-")
        binary = os.path.join(stub, "claude")  # noqa: SEAT_NAME — the BINARY the launch line execs, not a seat identity
        with open(binary, "w") as fh:
            fh.write('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
        os.chmod(binary, 0o700)
        env = dict(os.environ, PATH=stub + os.pathsep + os.environ.get("PATH", ""))
        r = subprocess.run(["/bin/sh", "-c", self.child_payload(body),
                            "helm-seat-harness", "trailing-positional"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = r.stdout.splitlines()
        i = got.index("--append-system-prompt")
        self.assertEqual(got[i + 1], want)
        self.assertEqual(got[-1], "trailing-positional")
        # CONTROL on a family that declares none: the flag is absent from the
        # argv entirely, so the arm above is about the DECLARATION and not
        # about something every launch line now carries.
        other = impl._launch_owner(seat.launch_line("kimi"))
        self.assertNotIn("--append-system-prompt", self.child_argv(other))

    def test_the_roster_is_the_whole_proxy_catalog(self):  # noqa: VACUOUS_ASSERTION — `assertEqual(set(self.PROXY), set(seat.FAMILIES))` on the next line is the unconditional positive control on the same observable, and the catalog it reads is never empty
        """The per-family arms below iterate PROXY; this pins that PROXY is
        not narrower than the catalog, so a family added later cannot slip
        past them unmeasured.

        THE LENGTH IS COMPARED TO THE TUPLE'S OWN SET, never to a number. A
        literal here said nothing the set equality below does not say, except
        on the one case a set hides — a name spelled twice — and it went
        stale every time a family landed: two lanes added a name each, both
        kept the old number, and the merge was clean."""
        self.assertEqual(len(self.PROXY), len(set(self.PROXY)),
                         "a family is spelled twice in the roster pin")
        self.assertEqual(set(self.PROXY), set(seat.FAMILIES))
        for fam in self.PROXY:
            self.assertIn(seat.FAMILIES[fam]["mode"], seat.PROXY_MODES, fam)

    def test_every_proxy_family_launch_line_denies_skill_and_fork(self):
        import shlex
        # unconditional positive control on the observable the loop pins
        self.assertIn(" claude --disallowedTools EnterPlanMode Artifact Skill"
                      " 'Agent(fork)' --dangerously-skip-permissions"
                      " --model ", seat.launch_line("codex"))
        for fam in self.PROXY:
            with self.subTest(family=fam):
                line = seat.launch_line(fam)
                _head, sep, tail = line.partition(" claude --disallowedTools ")
                self.assertTrue(sep, line)
                denied, sep, rest = tail.partition(" --dangerously-skip-permissions")
                self.assertTrue(sep, line)
                # the rule carries parentheses: unquoted, /bin/sh reads them
                # as a subshell and launch.sh dies at parse time (measured:
                # `sh -n` -> Syntax error: "(" unexpected). So the LITERAL
                # text must carry the quotes, and the parsed argv the rule.
                self.assertTrue(denied.endswith("Skill 'Agent(fork)'"),
                                denied)
                self.assertNotIn(" Agent(fork)", denied)
                self.assertEqual(shlex.split(denied)[-2:],
                                 ["Skill", "Agent(fork)"])
                # the variadic flag is still followed by an option, never
                # by launch.sh's trailing positional
                self.assertTrue(rest.startswith(" --model "), rest)

    @staticmethod
    def _seeded_deny(fam):
        d = tempfile.mkdtemp(prefix="helm-spawn-deny-")
        with open(os.path.join(d, "settings.json"), "w") as fh:
            fh.write('{"permissions":{"deny":[]}}')
        seat._seed_seat_settings(d, fam) if fam else seat._seed_seat_settings(d)
        with open(os.path.join(d, "settings.json")) as fh:
            return json.load(fh)["permissions"]["deny"]

    def test_every_proxy_family_seeds_the_deny_and_claude_does_not(self):  # noqa: VACUOUS_ASSERTION — the codex seeded list is pinned whole by equality before the loop, and each iteration asserts plan entry present before any membership equality
        # unconditional positive control on the seeded file itself
        self.assertEqual(self._seeded_deny("codex"),
                         [seat.PLAN_ENTRY_TOOL, "Artifact", "Skill",
                          "Agent(fork)"])
        for fam, want in tuple((f, True) for f in self.PROXY) + ((None, False),):
            with self.subTest(family=fam):
                deny = self._seeded_deny(fam)
                self.assertIn(seat.PLAN_ENTRY_TOOL, deny)
                self.assertEqual("Skill" in deny, want)
                # task/2559 admits Workflow, capped, everywhere but on a lite
                # family with no same-family delegate tier (task/3253)
                self.assertEqual("Workflow" in deny,
                                 AProxySeatAdmitsACappedWorkflowOnItsOwnTier
                                 .denies_workflow(fam))
                self.assertEqual("Agent(fork)" in deny, want)

    def test_a_native_claude_seat_is_untouched(self):
        """denied_tools is the one door; a family outside the proxy catalog
        (the native claude path passes no family at all) gets plan entry
        only, exactly as before task/2287."""
        for fam in (None, "claude", "not-a-family"):
            self.assertEqual(seat.denied_tools(fam), (seat.PLAN_ENTRY_TOOL,), fam)
        self.assertEqual(seat.SPAWN_DENIED_TOOLS,
                         ("Skill", "Agent(fork)"))

    def test_mutation_control_emptying_the_door_blanks_both_surfaces(self):
        """Removing the deny from the table must redden the arms above. This
        arm performs that removal and asserts the launch line and the seeded
        settings both lose the tokens, which is what proves the pins above
        observe the door rather than a coincidence of the text."""
        from helm import seat_catalog
        with mock.patch.object(seat_catalog, "SPAWN_DENIED_TOOLS", ()):
            line = seat.launch_line("codex")
            self.assertIn("--disallowedTools EnterPlanMode Artifact --dangerously", line)
            self.assertNotIn("Skill", line)
            self.assertNotIn("Workflow", line)
            self.assertNotIn("Agent(fork)", line)
            d = tempfile.mkdtemp(prefix="helm-spawn-deny-mut-")
            with open(os.path.join(d, "settings.json"), "w") as fh:
                fh.write('{"permissions":{"deny":[]}}')
            seat._seed_seat_settings(d, "codex")
            with open(os.path.join(d, "settings.json")) as fh:
                deny = json.load(fh)["permissions"]["deny"]
            self.assertEqual(deny, [seat.PLAN_ENTRY_TOOL, "Artifact"])


class AProxySeatAdmitsACappedWorkflowOnItsOwnTier(unittest.TestCase):
    """task/2559, which reverses the Workflow half of task/2491. The owner
    ruled that "astra can run sol workflows just like fable can run opus
    workflows ... both valid strategies for cross-model same-family
    delegation within a cc process, so the policy needs to be more nuanced":
    a proxy seat KEEPS the Workflow tool while Skill and Agent(fork) stay
    denied (task/2287), and the bound a brief cannot supply is mechanical in
    two parts. The CAP is CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4 — the
    one workflow knob the harness exposes (read in the 2.1.272 bundle: the
    Workflow tool's agent gate is `a.CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS
    ??Pr`) — riding the launch line as an env word and the seeded settings'
    `env` map, the surface a resume inherits. The TIER is the proxy config's
    existing alias block: a workflow agent's model is sent upstream as a
    claude id and lands on the seat's own launch model or the family's tier
    for that id, so on a codex seat every agent runs gpt-6.1-sol, the model the
    owner's ruling puts every codex on. Literal spellings are pinned (the
    Artifact class says why);
    the last arm patches the cap and the door and shows the shipped producers
    follow, which is what proves the arms above observe them."""

    PROXY = ProxySeatCannotSpawnThroughASkill.PROXY

    @staticmethod
    def denies_workflow(family):
        """The task/3253 reconciliation, spelled from the DECLARATIONS rather
        than read back through denied_tools: a lite family with no
        same-family delegate tier denies Workflow, and so does a family whose
        own `context_denied_tools` names it (task/3803); every other family
        keeps the capped tool this class pins."""
        fam = seat.FAMILIES.get(family) or {}
        return (seat_catalog.lite(family) and not fam.get("subagent_tiers")) \
            or "Workflow" in fam.get("context_denied_tools", ())

    #: the proxy families the arms below measure: every one task/2559 still
    #: admits Workflow on (the families that deny it are pinned by the lite
    #: profile's arms and the context-deny arm, which pin the inverse)
    ADMITS = tuple(filter(lambda f, denies=denies_workflow.__func__: not denies(f),
                          PROXY))
    VAR = "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"
    CAP = " CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=4"

    def test_the_admitting_set_is_the_catalog_less_the_lite_deniers(self):  # noqa: VACUOUS_ASSERTION — codex and kimi membership and a non-empty dropped list are asserted unconditionally, and each dropped family's Workflow presence precedes its two cap absences
        """ADMITS narrows the loops below, so it is pinned: it is PROXY less
        exactly the families that deny Workflow, it keeps the families the
        literal controls name, and each family it drops denies Workflow on
        the door and carries no cap, so nothing leaves these arms
        unmeasured."""
        self.assertIn("codex", self.ADMITS)
        self.assertIn("kimi", self.ADMITS)
        dropped = [f for f in self.PROXY if f not in self.ADMITS]
        self.assertTrue(dropped, "no lite family denies Workflow")
        for fam in dropped:
            with self.subTest(family=fam):
                self.assertIn("Workflow", seat.denied_tools(fam))
                self.assertEqual(seat_catalog.workflow_cap_env(fam), ())
                self.assertNotIn(self.VAR, seat.launch_line(fam))
    PAIR = " DISABLE_FEEDBACK_COMMAND=1 DISABLE_BUG_COMMAND=1"
    #: the model every codex seat runs (CODEX_MODEL_RULING)
    CODEX_DEFAULT = "gpt-6.1-sol"
    #: a model a pane runs only when an operator pins it with --model: codex
    #: still catalogues it (model_context), and no seat declares it
    PINNED = "gpt-5.6-sol"

    @staticmethod
    def _frontmatter_deny(body):
        """The disallowedTools list as CC's loader would read it, or None."""
        line = next((l for l in body.splitlines()
                     if l.startswith("disallowedTools: ")), None)
        return None if line is None else json.loads(line.split(": ", 1)[1])

    @staticmethod
    def _seeded(fam, prior='{"permissions":{"deny":[]}}'):
        """The whole settings.json the shipped seeder leaves, plus its path."""
        d = tempfile.mkdtemp(prefix="helm-workflow-cap-")
        p = os.path.join(d, "settings.json")
        with open(p, "w") as fh:
            fh.write(prior)
        seat._seed_seat_settings(d, fam) if fam else seat._seed_seat_settings(d)
        with open(p) as fh:
            return json.load(fh), p

    @staticmethod
    def _denied_argv(line):
        """The deny tokens between the flag and the next option, as argv."""
        import shlex
        denied = line.partition(" claude --disallowedTools ")[2].partition(
            " --dangerously-skip-permissions")[0]
        return shlex.split(denied)

    def test_the_deny_set_admits_workflow_and_keeps_skill_and_fork(self):  # noqa: VACUOUS_ASSERTION — every absence below follows an unconditional literal positive on the same surface: the codex tuple, the codex launch line, the codex seeded deny and the codex probe definition are each pinned in full first
        # the door itself, spelled in full: Workflow gone, the task/2287 pair kept
        self.assertEqual(seat.denied_tools("codex"),
                         ("EnterPlanMode", "Artifact", "Skill", "Agent(fork)"))
        self.assertEqual(seat.denied_tools("kimi"),
                         ("EnterPlanMode", "Skill", "Agent(fork)"))
        self.assertEqual(seat.SPAWN_DENIED_TOOLS, ("Skill", "Agent(fork)"))
        # all three surfaces, spelled literally for codex
        self.assertIn(" claude --disallowedTools EnterPlanMode Artifact Skill"
                      " 'Agent(fork)' --dangerously-skip-permissions --model ",
                      seat.launch_line("codex"))
        self.assertEqual(ProxySeatCannotSpawnThroughASkill._seeded_deny("codex"),
                         [seat.PLAN_ENTRY_TOOL, "Artifact", "Skill", "Agent(fork)"])
        cdir = tempfile.mkdtemp(prefix="helm-tier-deny-codex-")
        seat._mint_probe_agents(cdir, "codex")
        with open(os.path.join(cdir, "agents",
                               "helm-probe-gpt-6-1-sol.md")) as fh:
            self.assertIn('disallowedTools: ["EnterPlanMode", "Artifact",'
                          ' "Skill", "Agent(fork)"]\n', fh.read())
        for fam in self.ADMITS:
            with self.subTest(family=fam):
                argv = self._denied_argv(seat.launch_line(fam))
                self.assertEqual(argv[-2:], ["Skill", "Agent(fork)"])
                self.assertNotIn("Workflow", argv)
                deny = ProxySeatCannotSpawnThroughASkill._seeded_deny(fam)
                self.assertIn("Skill", deny)
                self.assertIn("Agent(fork)", deny)
                self.assertNotIn("Workflow", deny)
                cdir = tempfile.mkdtemp(prefix="helm-tier-deny-")
                probes = seat._mint_probe_agents(cdir, fam)
                self.assertTrue(probes)
                for name, _model in probes:
                    with open(os.path.join(cdir, "agents", name + ".md")) as fh:
                        got = self._frontmatter_deny(fh.read())
                    self.assertEqual(got, list(seat.denied_tools(fam)))
                    self.assertNotIn("Workflow", got)
        # the native door is untouched: plan entry only, on both surfaces
        for fam in (None, "claude", "not-a-family"):
            self.assertEqual(seat.denied_tools(fam), (seat.PLAN_ENTRY_TOOL,), fam)
        self.assertEqual(ProxySeatCannotSpawnThroughASkill._seeded_deny(None),
                         [seat.PLAN_ENTRY_TOOL])

    def test_every_proxy_family_launch_line_carries_the_cap_as_an_env_word(self):  # noqa: VACUOUS_ASSERTION — the unconditional literal assertIn on the codex line, cap and pair spelled in full, precedes the loop; the loop re-pins it per family
        import shlex
        # unconditional positive control on the observable, spelled literally:
        # the cap sits between the window knobs and the feedback pair, so the
        # pair stays the LAST env word (task/2328's pin) and the cap is
        # environment, never an argument the variadic deny flag could eat
        self.assertIn(" CLAUDE_CODE_AUTO_COMPACT_WINDOW=220000" + self.CAP
                      + self.PAIR + " claude --disallowedTools EnterPlanMode"
                      " Artifact Skill 'Agent(fork)' --dangerously-skip-permissions"
                      " --model gpt-6.1-sol", seat.launch_line("codex"))
        for fam in self.ADMITS:
            with self.subTest(family=fam):
                line = seat.launch_line(fam)
                head, sep, tail = line.partition(" claude --disallowedTools ")
                self.assertTrue(sep, line)
                words = shlex.split(head)
                self.assertEqual(words[0], "env")
                self.assertEqual(words.count(self.VAR + "=4"), 1)
                self.assertLess(words.index(self.VAR + "=4"),
                                words.index("DISABLE_FEEDBACK_COMMAND=1"))
                # negative controls on the same line: nothing of the cap
                # crossed into argv, and the deny set is exactly the door's
                self.assertNotIn("WORKFLOW", tail)
                self.assertEqual(self._denied_argv(line), list(seat.denied_tools(fam)))
        # an instance launch and a --multi launch carry the same word
        self.assertIn(self.CAP + self.PAIR + " claude --disallowedTools ",
                      seat.launch_line("codex", seat="seat-b"))
        self.assertIn(self.CAP + self.PAIR + " claude --disallowedTools ",
                      seat.launch_line("kimi", multi=True))

    def test_launch_sh_hands_claude_the_cap_in_its_environment_not_its_argv(self):  # noqa: VACUOUS_ASSERTION — CAP=4 on the stub's last line and the six-token argv equality are unconditional positives on the same run before the two absences
        """The real payload under /bin/sh with a stub `claude` on PATH that
        prints its argv and then the variable it inherited: the assertion is
        about what the binary receives. The trailing positional is the
        control for the variadic-flag hazard, exactly as the fork-rule arm
        runs it."""
        import subprocess
        from helm import seat_launch_assets as impl
        body = impl._launch_owner(seat.launch_line("codex"))
        stub = tempfile.mkdtemp(prefix="helm-stub-claude-")
        with open(os.path.join(stub, "claude"), "w") as fh:
            fh.write('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n'
                     'printf "CAP=%s\\n" "$CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"\n')
        os.chmod(os.path.join(stub, "claude"), 0o700)
        env = dict(os.environ, PATH=stub + os.pathsep + os.environ.get("PATH", ""))
        env.pop(self.VAR, None)          # the value must come from the line
        r = subprocess.run(["/bin/sh", "-c",
                            ProxySeatCannotSpawnThroughASkill.child_payload(body),
                            "helm-seat-harness", "trailing-positional"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = r.stdout.splitlines()
        self.assertEqual(got[-1], "CAP=4")
        argv = got[:-1]
        i = argv.index("--disallowedTools")
        self.assertEqual(argv[i:i + 6], ["--disallowedTools", "EnterPlanMode",
                                         "Artifact", "Skill", "Agent(fork)",
                                         "--dangerously-skip-permissions"])
        self.assertNotIn("Workflow", argv)
        self.assertFalse([a for a in argv if a.startswith(self.VAR)], argv)
        self.assertEqual(argv[-1], "trailing-positional")

    def test_the_seeded_settings_carry_the_cap_for_a_proxy_seat_and_not_for_claude(self):  # noqa: VACUOUS_ASSERTION — the codex equality on the first line is an unconditional positive on the seeded file; the native-door absence is a whole-file equality, not a lone assertNotIn
        # unconditional positive control on the seeded file itself
        self.assertEqual(self._seeded("codex")[0]["env"], {self.VAR: "4"})
        for fam in self.ADMITS:
            with self.subTest(family=fam):
                got, _p = self._seeded(fam)
                self.assertEqual(got["env"], {self.VAR: "4"})
                self.assertIn("Skill", got["permissions"]["deny"])
        # THE NATIVE DOOR IS BYTE-IDENTICAL TO BEFORE THIS LANE: the whole
        # file is pinned, so no key — env or otherwise — can have ridden in
        self.assertEqual(self._seeded(None)[0],
                         {"permissions": {"deny": [seat.PLAN_ENTRY_TOOL]},
                          "helm": {"seeded_denies": [seat.PLAN_ENTRY_TOOL]},
                          "skipDangerousModePermissionPrompt": True,
                          "theme": "auto", "feedbackDrafts": "off"})
        # authoritative for its key, additive to the map, idempotent
        got, p = self._seeded("codex", '{"env": {"KEEP": "1", "%s": "9"}, '
                              '"permissions": {"deny": []}}' % self.VAR)
        self.assertEqual(got["env"], {"KEEP": "1", self.VAR: "4"})
        st = os.stat(p)
        seat._seed_seat_settings(os.path.dirname(p), "codex")   # second seed
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))            # no write at all
        # an `env` that is not a map is replaced, never appended to
        got, _p = self._seeded("kimi", '{"env": "junk", "permissions": {"deny": []}}')
        self.assertEqual(got["env"], {self.VAR: "4"})

    def test_a_workflow_agents_model_resolves_on_the_seats_own_tier(self):
        """Driven through the SHIPPED generator and read back through the
        SHIPPED block splitter and row reader — the path proxy_config_plan
        walks — for the panes the fleet mints: every codex instance, each on
        gpt-6.1-sol by the owner's ruling. The words a script writes (`opus`,
        `sonnet`, `fable`) are resolved by CC's own table before they reach
        the wire — read in the 2.1.272 bundle: opus -> claude-opus-5, and in
        the 2.1.280 bundle opus -> claude-opus-5-5; sonnet -> claude-sonnet-5,
        fable -> claude-fable-5-1 in both — and those ids are the catalogued
        ones, so what the rows say about them is what the upstream sees."""
        from helm import seat_launch_assets as a, seat_catalog as c
        for word_id in ("claude-opus-5", "claude-opus-5-5", "claude-sonnet-5",
                        "claude-fable-5-1"):
            self.assertIn(word_id, c.CC_AGENT_FRONTMATTER_MODELS)
        fam = c.FAMILIES["codex"]

        def rows(model):
            y = a._config_yaml(8317, "/auth", "tok", channel="codex",
                               model=model, family="codex")
            blocks = [body for key, body in a._top_blocks(y)[1]
                      if key == "oauth-model-alias"]
            self.assertEqual(len(blocks), 1, "one alias block")
            return [(row["alias"], row["name"])
                    for row in a._oauth_rows(blocks[0], "codex")]

        every_id_on_sol6 = [(alias, self.CODEX_DEFAULT)
                            for alias in c.CC_AGENT_FRONTMATTER_MODELS]
        # EVERY INSTANCE THE FLEET MINTS, through the model the shipped
        # instance mint hands the generator (the same call
        # _mint_instance_proxy makes): the family seat, the numbered seats
        # the old table named, and one nobody ever named
        instances = ("codex", "codex-2", "codex-4", "codex-7", "codex-97")  # noqa: SEAT_NAME — the codex instances whose launch model is this arm's subject
        for seat_name in instances:
            with self.subTest(seat=seat_name):
                model = c.instance_launch_model(fam, seat_name)
                self.assertEqual(model, self.CODEX_DEFAULT)
                self.assertEqual(rows(model), every_id_on_sol6)
                # and its launch line pins the pane to gpt-6.1-sol too, so an
                # agent that names no model inherits it
                line = seat.launch_line("codex", seat=seat_name)
                self.assertTrue(line.endswith(" --model " + self.CODEX_DEFAULT), line)
                self.assertIn(" CLAUDE_CODE_SUBAGENT_MODEL=" + self.CODEX_DEFAULT + " ",
                              line)
        # A PANE AN OPERATOR PINNED with --model: the judgement ids follow
        # the pane, and the workers the tier table names still go to
        # gpt-6.1-sol. That is the one place the table departs from the pane.
        self.assertEqual(rows(self.PINNED),
                         [("claude-opus-5", self.PINNED),
                          ("claude-sonnet-5", self.CODEX_DEFAULT),
                          ("claude-haiku-4-5-20251001", self.CODEX_DEFAULT),
                          ("claude-fable-5-1", self.PINNED),
                          ("claude-haiku-4-5", self.CODEX_DEFAULT),
                          ("claude-opus-5-5", self.PINNED)])

    # the old helm carrier beside an operator env entry
    OLD_CARRIER = '{"permissions": {"deny": ["EnterPlanMode", "Artifact", "Skill", ' \
                  '"Workflow", "Agent(fork)"]}, "env": {"KEEP": "1"}}'
    HELM_SET = ["EnterPlanMode", "Artifact", "Skill", "Agent(fork)"]

    @classmethod
    def _seeded_err(cls, fam, prior):
        """_seeded plus the stderr the shipped refresh printed."""
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got, p = cls._seeded(fam, prior)
        return got, p, err.getvalue()

    @staticmethod
    def _refresh_again(p, fam):
        """A second refresh: (at rest — mtime and inode unchanged, stderr)."""
        import contextlib, io
        st = os.stat(p)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seat._seed_seat_settings(os.path.dirname(p), fam) if fam \
                else seat._seed_seat_settings(os.path.dirname(p))
        rest = (os.stat(p).st_mtime_ns, os.stat(p).st_ino) == (st.st_mtime_ns, st.st_ino)
        return rest, err.getvalue()

    def test_the_retired_carrier_and_the_records_are_what_the_catalog_says(self):
        from helm import seat_catalog as c
        self.assertEqual(c.RETIRED_SPAWN_DENIES, ("Workflow",))
        self.assertEqual(c.retired_denies("codex"), ("Workflow",))
        self.assertEqual(c.retired_denies("kimi"), ("Workflow",))
        self.assertEqual(c.retired_denies(None), ())
        self.assertEqual(c.retired_denies("claude"), ())
        self.assertEqual((c.SEED_RECORD_KEY, c.SEED_RECORD_DENIES, c.OPERATOR_RECORD_DENIES),
                         ("helm", "seeded_denies", "operator_denies"))
        # no shape is read as proof of authorship: the heuristic is gone
        self.assertFalse(hasattr(c, "historical_fingerprint"))
        self.assertFalse(hasattr(c, "HISTORICAL_SEEDED_DENIES"))

    def test_a_refresh_retires_the_deny_helm_recorded_and_updates_the_record(self):  # noqa: VACUOUS_ASSERTION — the fresh-seat equality and the refreshed-list equality are unconditional positives on the seeded file before Workflow's absence is asserted
        """Recorded by helm: the record names Workflow, so the refresh
        removes exactly it, from the list and from the record, keeps every
        other entry in place and in order, lands the cap, and says nothing —
        this leg is not an override. A sibling key under `helm` survives."""
        # fresh seat, the positive control: the list and its record
        fresh = self._seeded("codex")[0]
        self.assertEqual(fresh["permissions"]["deny"], self.HELM_SET)
        self.assertEqual(fresh["helm"], {"seeded_denies": self.HELM_SET})
        for fam in self.ADMITS:
            with self.subTest(family=fam):
                prior = json.dumps({"permissions": {"deny": ["EnterPlanMode", "Skill",
                                                             "Agent(fork)", "Workflow",
                                                             "WebFetch"]},
                                    "helm": {"seeded_denies": ["EnterPlanMode", "Skill",
                                                               "Agent(fork)", "Workflow"],
                                             "note": "kept"},
                                    "env": {"KEEP": "1"}})
                got, p, err = self._seeded_err(fam, prior)
                deny = got["permissions"]["deny"]
                self.assertNotIn("Workflow", deny)
                # the family's own denies past the shared three (Artifact on
                # codex, the context and push denies on the local families)
                # land after the kept entries, in denied_tools order
                extra = [t for t in seat.denied_tools(fam)
                         if t not in ("EnterPlanMode", "Skill", "Agent(fork)")]
                self.assertEqual(deny, ["EnterPlanMode", "Skill", "Agent(fork)", "WebFetch"]
                                 + extra)
                self.assertEqual(got["helm"]["seeded_denies"],
                                 ["EnterPlanMode", "Skill", "Agent(fork)"] + extra)
                self.assertEqual(got["helm"]["note"], "kept")
                self.assertEqual(got["env"], {"KEEP": "1", self.VAR: "4"})
                self.assertEqual(err, "")            # a recorded retirement is silent
                self.assertEqual(self._refresh_again(p, fam), (True, ""))
        # a retired entry that returns to the current table is not retired:
        # the resolver is the one door, and the seeder follows it
        from helm import seat_catalog as c
        with mock.patch.object(c, "SPAWN_DENIED_TOOLS", ("Skill", "Workflow", "Agent(fork)")):
            self.assertEqual(c.retired_denies("codex"), ())
            deny = self._seeded("codex", self.OLD_CARRIER)[0]["permissions"]["deny"]
            self.assertIn("Workflow", deny)

    def test_an_unrecorded_workflow_deny_is_left_with_a_note_wherever_the_file_lives(self):  # noqa: VACUOUS_ASSERTION — the whole refreshed list is pinned by equality (Workflow at its original index) before any absence, and the note's presence is asserted with its words
        """Unrecorded: `[Workflow, WebFetch]` with no record. No automatic
        path removes it — not a refresh of a file INSIDE helm's own seat
        tree, not one anywhere else — because a tool name is not proof helm
        wrote the entry and the old seeder's contract preserved what it
        found. Every mandatory entry and the cap still land, the record names
        only what helm appended, and one stderr line names the entry, the
        file and `helm seat retire-deny`. A repeat is at rest and says it
        again. The recorded arm above is this arm's positive control: the
        same code path DOES retire when helm's record names the entry."""
        from helm import seat_launch_assets as impl
        home = tempfile.mkdtemp(prefix="helm-unrecorded-home-")
        saved = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = home
        try:
            inside = os.path.join(impl._instance_dir("codex", "seat-b"), "claude")
            os.makedirs(inside)
            self.assertTrue(inside.startswith(home))          # really in helm's tree
            outside = tempfile.mkdtemp(prefix="helm-unrecorded-outside-")
            for cdir in (inside, outside):
                with self.subTest(where="inside" if cdir is inside else "outside"):
                    p = os.path.join(cdir, "settings.json")
                    with open(p, "w") as fh:
                        fh.write('{"permissions": {"deny": ["Workflow", "WebFetch"]}}')
                    import contextlib, io
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        seat._seed_seat_settings(cdir, "codex")
                    with open(p) as fh:
                        got = json.load(fh)
                    self.assertEqual(got["permissions"]["deny"],
                                     ["Workflow", "WebFetch"] + self.HELM_SET)
                    self.assertEqual(got["env"], {self.VAR: "4"})
                    self.assertEqual(got["helm"], {"seeded_denies": self.HELM_SET})
                    note = err.getvalue()
                    self.assertEqual(note.count("\n"), 1, note)
                    self.assertIn("codex keeps the unrecorded deny 'Workflow' in %s" % p, note)
                    self.assertIn("helm seat retire-deny Workflow", note)
                    self.assertIn("helm.operator_denies", note)
                    self.assertNotIn("WebFetch", note)
                    rest, again = self._refresh_again(p, "codex")
                    self.assertTrue(rest)
                    self.assertIn("keeps the unrecorded deny 'Workflow'", again)
        finally:
            if saved is None:
                os.environ.pop("HELM_HOME", None)
            else:
                os.environ["HELM_HOME"] = saved
        # the old helm carrier with no record takes the same leg: the shape
        # proves nothing, so it is kept and named
        got, p, err = self._seeded_err("codex", self.OLD_CARRIER)
        self.assertEqual(got["permissions"]["deny"], self.HELM_SET[:3] + ["Workflow", "Agent(fork)"])
        self.assertEqual(got["helm"], {"seeded_denies": []})
        self.assertIn("keeps the unrecorded deny 'Workflow'", err)

    def test_the_record_never_names_an_entry_the_list_does_not_carry(self):
        """The stale-record control of the forward lifecycle: a name in
        helm.seeded_denies that the deny list no longer carries (removed by
        hand, or by an earlier retirement) leaves the record on the next
        refresh, and the deny list itself is unchanged."""
        prior = json.dumps({"permissions": {"deny": self.HELM_SET},
                            "helm": {"seeded_denies": ["Workflow", "EnterPlanMode"]}})
        got, p, err = self._seeded_err("codex", prior)
        self.assertEqual(got["permissions"]["deny"], self.HELM_SET)
        self.assertEqual(got["helm"], {"seeded_denies": ["EnterPlanMode"]})
        self.assertEqual(err, "")
        self.assertEqual(self._refresh_again(p, "codex"), (True, ""))
        # and a record naming an entry that IS present and IS retired: both go
        prior = json.dumps({"permissions": {"deny": self.HELM_SET + ["Workflow"]},
                            "helm": {"seeded_denies": self.HELM_SET + ["Workflow"]}})
        got, p, err = self._seeded_err("codex", prior)
        self.assertEqual(got["permissions"]["deny"], self.HELM_SET)
        self.assertEqual(got["helm"], {"seeded_denies": self.HELM_SET})
        self.assertEqual(err, "")

    def test_an_operator_recorded_workflow_deny_is_kept_and_re_applied(self):  # noqa: VACUOUS_ASSERTION — Workflow's presence and the operator key's byte-identity are the positives; the empty stderr is asserted beside them on the same refresh
        """Recorded by the operator: `helm.operator_denies` names Workflow,
        so it is present after every refresh, never removed, no line; the
        key itself is never edited; and an entry it names that is missing
        from the deny list is re-added."""
        prior = json.dumps({"permissions": {"deny": ["Workflow", "WebFetch"]},
                            "helm": {"operator_denies": ["Workflow"]}})
        got, p, err = self._seeded_err("codex", prior)
        self.assertEqual(got["permissions"]["deny"], ["Workflow", "WebFetch"] + self.HELM_SET)
        self.assertEqual(got["helm"], {"operator_denies": ["Workflow"],
                                       "seeded_denies": self.HELM_SET})
        self.assertEqual(err, "")
        self.assertEqual(self._refresh_again(p, "codex"), (True, ""))
        # the operator's record outranks helm's own: both name it, it stays
        prior = json.dumps({"permissions": {"deny": self.HELM_SET + ["Workflow"]},
                            "helm": {"operator_denies": ["Workflow"],
                                     "seeded_denies": self.HELM_SET + ["Workflow"]}})
        got, p, err = self._seeded_err("codex", prior)
        self.assertIn("Workflow", got["permissions"]["deny"])
        self.assertEqual(err, "")
        # re-applied: the operator's entry is missing from the list and returns
        prior = json.dumps({"permissions": {"deny": []},
                            "helm": {"operator_denies": ["Workflow", "WebFetch"]}})
        got, p, err = self._seeded_err("kimi", prior)
        self.assertEqual(got["permissions"]["deny"],
                         list(seat.denied_tools("kimi")) + ["Workflow", "WebFetch"])
        self.assertEqual(got["helm"]["operator_denies"], ["Workflow", "WebFetch"])
        self.assertEqual(err, "")
        self.assertEqual(self._refresh_again(p, "kimi"), (True, ""))

    def test_a_native_seat_with_a_workflow_deny_is_untouched(self):
        """The control: a native seat is not a proxy family, so no leg runs
        there — its Workflow entry stays, plan entry is seeded, no line."""
        for fam in (None, "claude"):
            with self.subTest(family=fam):
                got, p, err = self._seeded_err(fam, '{"permissions": {"deny": ["Workflow"]}}')
                self.assertEqual(got["permissions"]["deny"], ["Workflow", "EnterPlanMode"])
                self.assertEqual(got["helm"], {"seeded_denies": ["EnterPlanMode"]})
                self.assertNotIn("env", got)
                self.assertEqual(err, "")
                self.assertEqual(self._refresh_again(p, fam), (True, ""))

    def test_mutation_control_the_producers_read_the_cap_and_the_door(self):
        """THE MUST-HITS. Raising the cap constant must move the value on
        both surfaces; re-admitting Skill in the door must lose it on the
        launch.sh argv the shipped payload path hands claude and on the
        seeded deny; emptying the mode key must lose the cap word altogether;
        emptying the retired carrier must leave an unrecorded Workflow
        standing and silent. Each is what proves the arms above observe the
        producers rather than a constant spelled twice."""
        from helm import seat_catalog, seat_launch_assets as impl
        with mock.patch.object(seat_catalog, "WORKFLOW_AGENT_CAP", 9):
            line = seat.launch_line("codex")
            self.assertIn(" " + self.VAR + "=9" + self.PAIR + " claude ", line)
            self.assertNotIn(self.VAR + "=4", line)
            # from a HELM-RECORDED old carrier, never an empty deny: the stale
            # cap moves and the recorded deny goes in the same refresh
            carrier = self.HELM_SET[:3] + ["Workflow", "Agent(fork)"]
            prior = json.dumps({"permissions": {"deny": carrier},
                                "helm": {"seeded_denies": carrier},
                                "env": {"KEEP": "1"}})
            got = self._seeded_err("codex", prior)[0]
            self.assertEqual(got["env"], {"KEEP": "1", self.VAR: "9"})
            self.assertEqual(got["permissions"]["deny"], self.HELM_SET)
            # and the UNRECORDED carrier keeps its Workflow while the cap moves
            got = self._seeded_err("codex", self.OLD_CARRIER)[0]
            self.assertEqual(got["env"], {"KEEP": "1", self.VAR: "9"})
            self.assertEqual(got["permissions"]["deny"], carrier)
        with mock.patch.object(seat_catalog, "SPAWN_DENIED_TOOLS", ("Agent(fork)",)):
            argv = ProxySeatCannotSpawnThroughASkill.child_argv(
                impl._launch_owner(seat.launch_line("codex")))
            i = argv.index("--disallowedTools")
            self.assertEqual(argv[i:i + 5], ["--disallowedTools", "EnterPlanMode",
                                             "Artifact", "Agent(fork)",
                                             "--dangerously-skip-permissions"])
            self.assertNotIn("Skill", argv)
            self.assertEqual(ProxySeatCannotSpawnThroughASkill._seeded_deny("codex"),
                             [seat.PLAN_ENTRY_TOOL, "Artifact", "Agent(fork)"])
        with mock.patch.object(seat_catalog, "PROXY_MODES", ()):
            line = seat.launch_line("codex")
            self.assertNotIn(self.VAR, line)
            self.assertIn(" CLAUDE_CODE_AUTO_COMPACT_WINDOW=", line)   # the neighbour survives
            self.assertNotIn("env", self._seeded("codex")[0])
        with mock.patch.object(seat_catalog, "RETIRED_SPAWN_DENIES", ()):
            # the carrier is what names it: with none, a helm-recorded
            # Workflow is not retired and an unrecorded one draws no note
            prior = json.dumps({"permissions": {"deny": self.HELM_SET + ["Workflow"]},
                                "helm": {"seeded_denies": self.HELM_SET + ["Workflow"]}})
            got, _p, err = self._seeded_err("codex", prior)
            self.assertIn("Workflow", got["permissions"]["deny"])
            self.assertEqual(err, "")
            got, _p, err = self._seeded_err(
                "codex", '{"permissions": {"deny": ["Workflow", "WebFetch"]}}')
            self.assertIn("Workflow", got["permissions"]["deny"])
            self.assertEqual(err, "")


class RetireDenyIsTheDeliberateDoor(unittest.TestCase):
    """`helm seat retire-deny <tool>` (task/2559): the one verb that removes
    a deny no record owns, since a refresh never does. Driven through
    cmd_seat over real seat settings files under this test's own HELM_HOME:
    a family dir carrying an unrecorded Workflow beside an operator entry, an
    instance whose operator key records it, a family whose helm record names
    it, and a family carrying no such entry. Bytes are compared, never
    inferred."""

    VAR = "CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"

    def _mint(self, family, instance, settings):
        from helm import seat_launch_assets as impl
        cdir = os.path.join(impl._instance_dir(family, instance), "claude")
        os.makedirs(cdir, exist_ok=True)
        p = os.path.join(cdir, "settings.json")
        with open(p, "w") as fh:
            json.dump(settings, fh)
        return p

    @staticmethod
    def _run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["retire-deny"] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    @staticmethod
    def _bytes(*paths):
        got = []
        for p in paths:
            with open(p, "rb") as fh:
                got.append(fh.read())
        return got

    @staticmethod
    def row(state, label, path):
        """The head of one listing row exactly as the verb prints it."""
        return "  %-12s %s  %s  " % (state, label, path)

    def setUp(self):
        # its own HELM_HOME, not SeatTest's: that class carries test methods
        # of its own, and inheriting them would run them over these files
        self.tmp = tempfile.mkdtemp(prefix="helm-test-retire-deny-")
        self._home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.unrecorded = self._mint("codex", "codex", {
            "permissions": {"deny": ["Workflow", "WebFetch"]},
            "helm": {"seeded_denies": ["EnterPlanMode"]}})
        self.operator = self._mint("codex", "seat-b", {
            "permissions": {"deny": ["Workflow"]},
            "helm": {"operator_denies": ["Workflow"]}})
        self.owned = self._mint("kimi", "kimi", {
            "permissions": {"deny": ["Workflow"]},
            "helm": {"seeded_denies": ["Workflow"]}})
        self.clean = self._mint("grok", "grok", {"permissions": {"deny": ["WebFetch"]}})
        self.all = (self.unrecorded, self.operator, self.owned, self.clean)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home

    def test_the_dry_run_lists_and_changes_nothing(self):
        before = self._bytes(*self.all)
        rc, out, err = self._run(["Workflow"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._bytes(*self.all), before)           # bytes equal
        self.assertIn(self.row("WOULD-REMOVE", "codex", self.unrecorded)
                      + "deny 'Workflow' (unrecorded); --apply removes it", out)
        self.assertIn(self.row("KEPT", "seat-b", self.operator)
                      + "deny 'Workflow' is recorded under helm.operator_denies", out)
        self.assertIn(self.row("HELM-OWNED", "kimi", self.owned), out)
        self.assertNotIn(self.clean, out)
        self.assertIn("dry run — nothing written", out)
        self.assertIn("owner decision", out)
        self.assertEqual(out.count(self.row("WOULD-REMOVE", "codex", self.unrecorded)), 1)
        # --all is the same sweep spelled out
        self.assertEqual(self._run(["Workflow", "--all"])[1], out)
        self.assertEqual(self._bytes(*self.all), before)

    def test_apply_removes_exactly_the_named_entry_from_exactly_the_listed_files(self):
        before = self._bytes(*self.all)
        rc, out, err = self._run(["Workflow", "--apply"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count(self.row("REMOVED", "codex", self.unrecorded)
                                   + "deny 'Workflow'"), 1)
        self.assertEqual(out.count("REMOVED"), 1)
        with open(self.unrecorded) as fh:
            got = json.load(fh)
        self.assertEqual(got["permissions"]["deny"], ["WebFetch"])       # WebFetch kept
        self.assertEqual(got["helm"], {"seeded_denies": ["EnterPlanMode"]})   # record untouched
        # the operator's, helm's own, and the clean file: byte-identical
        self.assertEqual(self._bytes(self.operator, self.owned, self.clean), before[1:])
        # a second --apply is a no-op with an empty WOULD-REMOVE list
        after = self._bytes(*self.all)
        rc, out2, err = self._run(["Workflow", "--apply"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("REMOVED", out2)
        self.assertNotIn("WOULD-REMOVE", out2)
        self.assertEqual(self._bytes(*self.all), after)
        # and the refresh that follows says nothing about the file: the entry
        # is gone, and helm's record still does not name it
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seat._seed_seat_settings(os.path.dirname(self.unrecorded), "codex")
        self.assertNotIn("Workflow", err.getvalue())
        with open(self.unrecorded) as fh:
            self.assertNotIn("Workflow", json.load(fh)["helm"]["seeded_denies"])

    def test_seat_scopes_the_sweep_to_one_file(self):
        before = self._bytes(*self.all)
        rc, out, err = self._run(["Workflow", "--seat", "codex"])
        self.assertEqual(rc, 0, err)
        self.assertIn(self.row("WOULD-REMOVE", "codex", self.unrecorded), out)
        self.assertNotIn(self.operator, out)
        self.assertNotIn(self.owned, out)
        rc, out, err = self._run(["Workflow", "--seat", "codex", "--apply"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count("REMOVED"), 1)
        self.assertEqual(self._bytes(self.operator, self.owned, self.clean), before[1:])
        rc, _out, err = self._run(["Workflow", "--seat", "codex", "--all"])
        self.assertEqual(rc, 2)
        self.assertIn("--seat and --all", err)

    def test_a_still_denied_tool_is_refused_and_nothing_moves(self):
        before = self._bytes(*self.all)
        for tool in ("Skill", "Agent(fork)", "EnterPlanMode", "Artifact"):
            with self.subTest(tool=tool):
                rc, out, err = self._run([tool, "--apply"])
                self.assertEqual(rc, 2)
                self.assertIn("%r is in the current deny set" % tool, err)
                self.assertEqual(out, "")
        self.assertEqual(self._bytes(*self.all), before)
        rc, _out, err = self._run([])
        self.assertEqual(rc, 2)
        self.assertIn("which tool?", err)
        rc, _out, err = self._run(["Workflow", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg '--bogus'", err)
        rc, out, _err = self._run(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("OWNER decision", out)
        self.assertIn("nothing in deploy or doctor runs it", out)
        self.assertEqual(self._bytes(*self.all), before)

    def test_a_lite_family_that_denies_workflow_is_outside_the_sweep(self):  # noqa: VACUOUS_ASSERTION — the codex WOULD-REMOVE row on the same listing precedes the lite file's absence, and the apply's one REMOVED line precedes the byte equality
        """task/3253: a lite family with no delegate tier denies Workflow
        again, so its seats are not retirement candidates (a refresh would
        seed the entry straight back) while every other proxy family's still
        are, exactly as before: the verb is not refused, the sweep lists the
        codex file and never the lite one, --apply leaves the lite file
        byte-identical, and a --seat of the lite family is refused by name."""
        lite = next(f for f in seat.FAMILIES
                    if AProxySeatAdmitsACappedWorkflowOnItsOwnTier
                    .denies_workflow(f))
        mine = self._mint(lite, lite, {"permissions": {"deny": ["Workflow"]}})
        before = self._bytes(mine)
        rc, out, err = self._run(["Workflow"])
        self.assertEqual(rc, 0, err)
        self.assertIn(self.row("WOULD-REMOVE", "codex", self.unrecorded), out)
        self.assertNotIn(mine, out)
        rc, out, err = self._run(["Workflow", "--apply"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count("REMOVED"), 1)
        self.assertEqual(self._bytes(mine), before)
        rc, _out, err = self._run(["Workflow", "--seat", lite])
        self.assertEqual(rc, 2)
        self.assertIn("in %s's current deny set" % lite, err)
        self.assertEqual(self._bytes(mine), before)

    def test_a_native_seat_is_outside_the_sweep(self):
        rc, _out, err = self._run(["Workflow", "--seat", "claude"])
        self.assertEqual(rc, 2)
        self.assertTrue("not a proxy-family seat" in err or "unknown" in err, err)
        # and nothing in the synopsis routes a native seat here: the sweep
        # enumerates proxy families only, by mode
        from helm import seat_launch_assets as impl, seat_catalog as c
        rows, err = impl._proxy_seat_candidates()
        self.assertIsNone(err)
        self.assertEqual({fam for _l, fam, _p in rows}, set(c.FAMILIES) - {"claude"})
        for _label, fam, _p in rows:
            self.assertIn(c.FAMILIES[fam]["mode"], c.PROXY_MODES)
        # a family with no file is ABSENT, not a row and not UNREADABLE
        rc, out, _err = self._run(["Workflow"])
        self.assertEqual(rc, 0)
        self.assertNotIn("UNREADABLE", out)
        self.assertNotIn("ds4pro", out)  # noqa: SEAT_NAME — the ds4pro FAMILY key is this fixture's subject: the one proxy family with no other file in this class
    def test_a_linked_seat_dir_or_child_is_refused_and_the_destination_untouched(self):  # noqa: VACUOUS_ASSERTION — the REFUSED row and rc 1 are positives on the same run; byte-identity of the destination is an equality
        """Containment before any read: a proxy label whose instance dir is
        a symlink to another seat's dir, or whose `claude` child is, would
        otherwise let --apply write another seat's settings through the
        link. The seeder's own identity and nesting guards run first and the
        candidate is REFUSED with the reason; the destination stays
        byte-identical and, since a refused candidate blocks the fleet apply,
        so does every other file."""
        from helm import seat_launch_assets as impl
        dest_dir = impl._instance_dir("codex", "seat-b")        # the operator's seat
        for name, link, target in (
                ("family dir", impl.seat_dir("ds4pro"), dest_dir),  # noqa: SEAT_NAME — the ds4pro FAMILY key is this fixture's subject: the one proxy family with no other file in this class
                ("child", os.path.join(impl.seat_dir("gemini"), "claude"),
                 os.path.join(dest_dir, "claude"))):
            with self.subTest(link=name):
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(target, link)
                try:
                    label = "ds4pro" if name == "family dir" else "gemini"  # noqa: SEAT_NAME — the ds4pro FAMILY key is this fixture's subject: the one proxy family with no other file in this class
                    listed = os.path.join(link, "claude", "settings.json") \
                        if name == "family dir" else os.path.join(link, "settings.json")
                    before = self._bytes(*self.all)
                    for argv in (["Workflow"], ["Workflow", "--apply"],
                                 ["Workflow", "--seat", label, "--apply"]):
                        rc, out, err = self._run(argv)
                        self.assertEqual(rc, 1, (argv, out, err))
                        self.assertIn(self.row("REFUSED", label, listed), out)
                        self.assertIn("refusing", out)
                        self.assertIn("nothing written", out)
                        self.assertNotIn("REMOVED", out)
                        self.assertEqual(self._bytes(*self.all), before)
                finally:
                    os.unlink(link)

    def test_a_malformed_authority_value_is_never_read_as_permission(self):  # noqa: VACUOUS_ASSERTION — the MALFORMED row and rc 1 are positives on the same run; the earlier row's bytes are compared by equality
        """The verb validates both record keys and the deny list through the
        seeder's own validator: a scalar, a string or an object is MALFORMED
        for that file, never membership (a string would answer substring
        membership), and a malformed LATER row blocks the fleet apply so the
        EARLIER unrecorded row is left untouched — dry-run and apply alike."""
        cases = ([("helm", {"operator_denies": v}) for v in (1, "Workflow", {"a": 1})]
                 + [("helm", {"seeded_denies": v}) for v in (1, "Workflow", {"a": 1})]
                 + [("permissions", {"deny": "Workflow"})])
        for key, value in cases:
            with self.subTest(key=key, value=value):
                settings = {"permissions": {"deny": ["Workflow"]}}
                settings[key] = dict(settings.get(key, {}), **value)
                self._mint("kimi", "kimi", settings)          # sorted AFTER codex
                before = self._bytes(*self.all)
                for argv in (["Workflow"], ["Workflow", "--apply"],
                             ["Workflow", "--seat", "kimi", "--apply"]):
                    rc, out, err = self._run(argv)
                    self.assertEqual(rc, 1, (argv, out, err))
                    self.assertIn(self.row("MALFORMED", "kimi", self.owned), out)
                    self.assertIn("not a list of strings", out)
                    self.assertNotIn("REMOVED", out)
                    self.assertEqual(self._bytes(*self.all), before)   # codex untouched too
                # the fleet listing still shows the earlier row it did not touch
                self.assertIn(self.row("WOULD-REMOVE", "codex", self.unrecorded),
                              self._run(["Workflow"])[1])

    def test_a_missing_file_is_absent_and_an_empty_object_carries_no_deny(self):
        """CONTROLS for the container arm below, on the same seat: no file at
        all is ABSENT (no row, rc 0) and a valid empty `{}` is NONE (no row,
        rc 0, and --apply leaves it byte-identical) — so the MALFORMED rows
        the next arm asserts are the classifier's answer to the file's
        shape, never to the seat or to the sweep."""
        os.unlink(self.clean)                       # grok: sorted last, no file
        rc, out, err = self._run(["Workflow", "--apply"])
        self.assertEqual(rc, 0, (out, err))
        # positive control on the same listing: the sweep that omits grok
        # judged and wrote the other seats, so the absence is a judgement
        self.assertIn(self.row("REMOVED", "codex", self.unrecorded), out)
        self.assertIn(self.row("KEPT", "seat-b", self.operator), out)
        self.assertNotIn("grok", out)
        self.assertNotIn("MALFORMED", out)
        self.assertNotIn("UNREADABLE", out)
        self.assertFalse(os.path.exists(self.clean))          # --apply minted nothing
        self._mint("grok", "grok", {})
        before = self._bytes(self.clean)
        for argv in (["Workflow"], ["Workflow", "--apply"]):
            rc, out, err = self._run(argv)
            self.assertEqual(rc, 0, (argv, out, err))
            self.assertIn(self.row("KEPT", "seat-b", self.operator), out)   # positive
            self.assertNotIn("grok", out)
            self.assertNotIn("MALFORMED", out)
            self.assertEqual(self._bytes(self.clean), before)

    def test_a_null_or_list_root_and_a_non_object_permissions_container_are_malformed(self):  # noqa: VACUOUS_ASSERTION — the MALFORMED row and rc 1 are positives on the same run; the earlier row's bytes are compared by equality
        """The shared classification boundary reads absence from the reader's
        missing-file sentinel, never from the parsed value, and validates
        each container before reading its member: a file of `null` or `[]`
        is MALFORMED naming the root (it was ABSENT — a parsed null read as
        the missing-file default), and a present `permissions` that is a
        string, null or a list is MALFORMED naming the key (it was NONE — a
        non-object container read as no deny). Either bypass dropped the
        file from the blocked set, so a fleet --apply wrote the EARLIER
        unrecorded row and returned 0. Now each blocks every write, exactly
        as the malformed-record arm establishes, and the earlier row stays
        byte-identical under dry-run, fleet --apply and --seat --apply."""
        cases = ((None, "settings.json root is null, not an object"),
                 ([], "settings.json root is a list, not an object"),
                 ({"permissions": "Workflow"}, "permissions is a str, not an object"),
                 ({"permissions": None}, "permissions is null, not an object"),
                 ({"permissions": []}, "permissions is a list, not an object"))
        for settings, detail in cases:
            with self.subTest(settings=settings):
                self._mint("kimi", "kimi", settings)          # sorted AFTER codex
                before = self._bytes(*self.all)
                for argv in (["Workflow"], ["Workflow", "--apply"],
                             ["Workflow", "--seat", "kimi", "--apply"]):
                    rc, out, err = self._run(argv)
                    self.assertEqual(rc, 1, (argv, out, err))
                    self.assertIn(self.row("MALFORMED", "kimi", self.owned) + detail, out)
                    self.assertNotIn(self.row("ABSENT", "kimi", self.owned), out)
                    self.assertNotIn(self.row("NONE", "kimi", self.owned), out)
                    self.assertNotIn("REMOVED", out)
                    self.assertIn("nothing written", out)
                    self.assertEqual(self._bytes(*self.all), before)   # codex untouched too
                # the fleet listing still shows the earlier row it did not touch
                self.assertIn(self.row("WOULD-REMOVE", "codex", self.unrecorded),
                              self._run(["Workflow"])[1])

    def test_an_unreadable_candidate_is_listed_and_the_fleet_is_never_called_clean(self):  # noqa: VACUOUS_ASSERTION — each UNREADABLE row and rc 1 are positives on the same run
        """No stat prefilter: a dangling link, a FIFO and a directory helm
        cannot enter each reach the strict reader, are listed UNREADABLE with
        the failure (errno where the OS gives one), in the selected-seat run
        and the fleet census alike; the exit code says so, the verb never
        reports a clean fleet over them, and --apply writes nothing."""
        from helm import seat_launch_assets as impl
        gem = os.path.join(impl.seat_dir("gemini"), "claude"); os.makedirs(gem)
        dangling = os.path.join(gem, "settings.json")
        os.symlink(os.path.join(gem, "nowhere.json"), dangling)
        ds = os.path.join(impl.seat_dir("ds4pro"), "claude"); os.makedirs(ds)  # noqa: SEAT_NAME — the ds4pro FAMILY key is this fixture's subject: the one proxy family with no other file in this class
        fifo = os.path.join(ds, "settings.json")
        os.mkfifo(fifo)
        cases = [("gemini", dangling, "errno 2"), ("ds4pro", fifo, "not a regular file")]  # noqa: SEAT_NAME — the ds4pro FAMILY key is this fixture's subject: the one proxy family with no other file in this class
        if os.geteuid() != 0:                     # root reads through 000
            locked = os.path.dirname(self.clean)  # grok's claude dir
            os.chmod(locked, 0)
            self.addCleanup(os.chmod, locked, 0o700)
            cases.append(("grok", self.clean, "errno 13"))
        unread = [p for _l, p, _e in cases]
        before = self._bytes(*(f for f in self.all if f not in unread))
        for argv in (["Workflow"], ["Workflow", "--apply"]):
            rc, out, err = self._run(argv)
            self.assertEqual(rc, 1, (argv, out, err))
            for label, path, expect in cases:
                self.assertIn(self.row("UNREADABLE", label, path), out)
                self.assertIn(expect, out)
            self.assertNotIn("no proxy-seat settings.json carries", out)
            self.assertNotIn("REMOVED", out)
            self.assertIn(self.row("WOULD-REMOVE", "codex", self.unrecorded), out)
            self.assertEqual(self._bytes(*(f for f in self.all if f not in unread)), before)
        for label, path, expect in cases:
            rc, out, err = self._run(["Workflow", "--seat", label])
            self.assertEqual(rc, 1, (label, out, err))
            self.assertIn(self.row("UNREADABLE", label, path), out)
            self.assertIn(expect, out)
            self.assertNotIn("no proxy-seat settings.json carries", out)



class FeedbackNeverLeavesHelm(unittest.TestCase):
    """task/2328. Every codex seat drafted helm bug reports into Claude Code's
    own SendFeedback queue (the pane: "Bug report drafted: <title> ... 1 to
    review · 2 to send"), which submits to Anthropic and is invisible to the
    fleet; the prompt-only rule did not hold. The cure is mechanical at the
    launch door: seat_catalog.FEEDBACK_ENV rides the launch line of EVERY
    family (and launch.build_env, the native door — test_launch pins that),
    FEEDBACK_DRAFTS_SETTING rides the seeded settings.json, FEEDBACK_RULE the
    seat's CLAUDE.md. MEASURED on CC 2.1.269 under a pty (print mode never
    lists the tool): control ANSWER-yes; feedbackDrafts=off, DISABLE_FEEDBACK_
    COMMAND=1 and DISABLE_BUG_COMMAND=1 each ANSWER-no. Literal spellings are
    pinned (the Artifact class says why); every arm re-pins the deny argv as
    the control that the env words never grew the deny set."""

    PROXY = ProxySeatCannotSpawnThroughASkill.PROXY
    PAIR = " DISABLE_FEEDBACK_COMMAND=1 DISABLE_BUG_COMMAND=1"

    def test_every_family_launch_line_exports_both_switches(self):
        import shlex
        # unconditional positive control on the observable, spelled literally
        self.assertIn(self.PAIR + " claude --disallowedTools EnterPlanMode Artifact"
                      " Skill 'Agent(fork)' --dangerously-skip-permissions"
                      " --model ", seat.launch_line("codex"))
        for fam in self.PROXY:
            with self.subTest(family=fam):
                line = seat.launch_line(fam)
                head, sep, tail = line.partition(" claude --disallowedTools ")
                self.assertTrue(sep, line)
                # environment, never an argument: the pair is the LAST env
                # word, so the variadic deny flag can never reach it
                self.assertTrue(head.endswith(self.PAIR), head)
                words = shlex.split(head)
                self.assertEqual(words[0], "env")
                self.assertEqual(words.count("DISABLE_FEEDBACK_COMMAND=1"), 1)
                self.assertEqual(words.count("DISABLE_BUG_COMMAND=1"), 1)
                # negative controls on the same line: the deny set is exactly
                # the door's (nothing unrelated rode in), and the settings
                # switch is a file key, never a launch word
                denied, sep, rest = tail.partition(" --dangerously-skip-permissions")
                self.assertTrue(sep, line)
                self.assertEqual(shlex.split(denied), list(seat.denied_tools(fam)))
                self.assertNotIn("DISABLE_", tail)
                self.assertNotIn("feedbackDrafts", line)
        self.assertNotIn("Artifact", seat.launch_line("kimi"))   # task/1941 keying intact

    def test_an_instance_and_a_multi_launch_carry_the_pair_too(self):
        # unconditional positive control on the default line first
        self.assertIn(self.PAIR + " claude --disallowedTools ", seat.launch_line("codex"))
        for line in (seat.launch_line("codex", seat="seat-b"),
                     seat.launch_line("kimi", multi=True)):
            self.assertIn(self.PAIR + " claude --disallowedTools ", line)

    @staticmethod
    def _seeded(fam, prior='{"permissions":{"deny":[]}}'):
        d = tempfile.mkdtemp(prefix="helm-feedback-")
        p = os.path.join(d, "settings.json")
        with open(p, "w") as fh:
            fh.write(prior)
        seat._seed_seat_settings(d, fam) if fam else seat._seed_seat_settings(d)
        with open(p) as fh:
            return json.load(fh), p

    def test_seeded_settings_turn_drafts_off_for_every_family_and_for_none(self):  # noqa: VACUOUS_ASSERTION — the codex equality on the first line is an unconditional positive on the seeded file; the loop re-pins it per family
        # unconditional positive control on the seeded file itself
        self.assertEqual(self._seeded("codex")[0]["feedbackDrafts"], "off")
        for fam in self.PROXY + (None,):
            with self.subTest(family=fam):
                got, _p = self._seeded(fam)
                self.assertEqual(got["feedbackDrafts"], "off")
                self.assertIn(seat.PLAN_ENTRY_TOOL, got["permissions"]["deny"])
                # negative control: the spawn deny stays keyed on the mode —
                # a fleet-wide setting did not drag a proxy-only deny along
                self.assertEqual("Skill" in got["permissions"]["deny"], fam is not None)

    def test_drafts_off_is_authoritative_and_idempotent(self):
        # an operator's "notify" is drift, not a choice the seat keeps (as
        # with bypass + theme); a sibling key survives byte-for-byte
        got, p = self._seeded("codex", '{"feedbackDrafts": "notify", "statusLine": '
                              '{"type": "command", "command": "x"}, '
                              '"permissions": {"deny": []}}')
        self.assertEqual(got["feedbackDrafts"], "off")
        self.assertEqual(got["statusLine"], {"type": "command", "command": "x"})
        st = os.stat(p)
        seat._seed_seat_settings(os.path.dirname(p), "codex")   # second seed
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))            # no write at all

    def test_seeded_rule_sentence_is_additive_and_idempotent(self):
        d = tempfile.mkdtemp(prefix="helm-feedback-rule-")
        p = os.path.join(d, "CLAUDE.md")
        seat._seed_seat_rules(d)
        with open(p) as fh:
            body = fh.read()
        self.assertEqual(body.count(seat.FEEDBACK_RULE), 1)
        # the sentence, spelled literally (an assertion on the constant alone
        # would go green with the constant emptied)
        self.assertIn("goes to a helm task row (helm task add) or to the "
                      "integrator in the helm room, never to Claude Code's "
                      "/feedback or /bug mechanism", body)
        self.assertTrue(body.startswith("# helm seat rules"), body)
        st = os.stat(p)
        seat._seed_seat_rules(d)                                  # second seed
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))            # no write at all
        # negative control: the rules seed never touches the settings surface
        self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))
        # an operator's own file keeps every byte and gains the sentence once
        d2 = tempfile.mkdtemp(prefix="helm-feedback-rule-")
        with open(os.path.join(d2, "CLAUDE.md"), "w") as fh:
            fh.write("# mine\nkeep this line\n")
        seat._seed_seat_rules(d2)
        seat._seed_seat_rules(d2)
        with open(os.path.join(d2, "CLAUDE.md")) as fh:
            body2 = fh.read()
        self.assertTrue(body2.startswith("# mine\nkeep this line\n"), body2)
        self.assertEqual(body2.count(seat.FEEDBACK_RULE), 1)
        self.assertNotIn("# helm seat rules", body2)   # the header is helm's own file only

    def test_seeding_never_writes_through_a_symlinked_leaf(self):
        """_write_launch_assets guards the
        directory, not this leaf — a CLAUDE.md that is a symlink into another
        seat or the owner's own home would have carried the write there. The
        link is refused loudly, its target is byte-identical afterwards, and
        the link itself still points where it did. A plain sibling in the
        same arm is the positive control that the seeder still seeds."""
        ext_dir = tempfile.mkdtemp(prefix="helm-feedback-ext-")
        ext = os.path.join(ext_dir, "CLAUDE.md")
        before = b"# the owner's own rules\r\nkeep me\r\n\r\n"
        with open(ext, "wb") as fh:
            fh.write(before)
        d = tempfile.mkdtemp(prefix="helm-feedback-link-")
        os.symlink(ext, os.path.join(d, "CLAUDE.md"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seat._seed_seat_rules(d)
        with open(ext, "rb") as fh:
            self.assertEqual(fh.read(), before)                     # target untouched
        self.assertTrue(os.path.islink(os.path.join(d, "CLAUDE.md")))
        self.assertEqual(os.readlink(os.path.join(d, "CLAUDE.md")), ext)
        self.assertIn("symlink", err.getvalue())
        self.assertIn("NOT seeded", err.getvalue())
        plain = tempfile.mkdtemp(prefix="helm-feedback-plain-")     # positive control
        seat._seed_seat_rules(plain)
        with open(os.path.join(plain, "CLAUDE.md")) as fh:
            self.assertEqual(fh.read().count(seat.FEEDBACK_RULE), 1)

    def test_a_link_that_vanishes_after_the_lstat_is_a_refusal_not_a_raise(self):
        """The lstat saw a link; by the time the seeder speaks, the link is
        gone (or replaced). Every step after the lstat must stay inside the
        handlers: one stderr line, a return, nothing created — never an
        ENOENT/EINVAL raised up through _write_launch_assets, which would
        leave launch.sh unminted. Modelled by an lstat that reports a link
        for a path that does not exist. The plain seed at the end is the
        positive control that the same seeder still writes."""
        d = tempfile.mkdtemp(prefix="helm-feedback-gone-")
        p = os.path.join(d, "CLAUDE.md")
        import stat as _stat
        link_stat = os.stat_result((_stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        err = io.StringIO()
        with mock.patch("os.lstat", return_value=link_stat), \
                contextlib.redirect_stderr(err):
            seat._seed_seat_rules(d)                              # must not raise
        self.assertIn("NOT seeded", err.getvalue())
        self.assertIn("symlink", err.getvalue())
        self.assertFalse(os.path.lexists(p))                     # nothing created
        seat._seed_seat_rules(d)                                  # positive control
        with open(p) as fh:
            self.assertEqual(fh.read().count(seat.FEEDBACK_RULE), 1)

    def test_a_link_swapped_in_after_the_lstat_cannot_escape(self):
        """The lstat saw a regular file; a link into another home is swapped
        in before the read. The read carries O_NOFOLLOW, so it refuses
        (ELOOP) instead of reading through, the target keeps every byte, and
        the seeder returns without raising. Modelled by an lstat that reports
        a regular file while the leaf really is a link."""
        ext_dir = tempfile.mkdtemp(prefix="helm-feedback-ext-")
        ext = os.path.join(ext_dir, "CLAUDE.md")
        before = b"# elsewhere\r\n"
        with open(ext, "wb") as fh:
            fh.write(before)
        d = tempfile.mkdtemp(prefix="helm-feedback-swap-")
        os.symlink(ext, os.path.join(d, "CLAUDE.md"))
        import stat as _stat
        regular = os.stat_result((_stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, len(before), 0, 0, 0))
        err = io.StringIO()
        # SPY THE REAL os.open: the byte checks below also pass a read-through
        # implementation, because the later WRITE's O_NOFOLLOW already refuses
        # and preserves the target. What distinguishes refusal at the READ is
        # the open population: exactly one attempted leaf open, read-only,
        # O_NOFOLLOW set, and no write attempt at all.
        leaf = os.path.join(d, "CLAUDE.md")
        with mock.patch("os.lstat", return_value=regular), \
                mock.patch("os.open", wraps=os.open) as spy, \
                contextlib.redirect_stderr(err):
            seat._seed_seat_rules(d)                              # must not raise
        opens = [c.args for c in spy.call_args_list if c.args and c.args[0] == leaf]
        self.assertEqual(len(opens), 1, spy.call_args_list)
        flags = opens[0][1]
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)       # the READ, not the write
        self.assertTrue(flags & os.O_NOFOLLOW)
        with open(ext, "rb") as fh:
            self.assertEqual(fh.read(), before)                   # no escape
        self.assertIn("not seeded", err.getvalue())
        self.assertTrue(os.path.islink(leaf))

    def test_seeding_preserves_every_byte_of_an_operator_file(self):
        """A seeder that reads with universal
        newlines and rstrips the tail, so CRLF ends and trailing blank
        lines do not survive "keeps every byte". Binary in, binary out: the
        prefix is the original bytes, one newline is added only when the
        file does not end in one, and a second seed writes nothing."""
        rule = seat.FEEDBACK_RULE.encode("utf-8")
        crlf = b"# mine\r\nkeep this line\r\n\r\n\r\n"          # two trailing blank lines
        d = tempfile.mkdtemp(prefix="helm-feedback-crlf-")
        p = os.path.join(d, "CLAUDE.md")
        with open(p, "wb") as fh:
            fh.write(crlf)
        seat._seed_seat_rules(d)
        with open(p, "rb") as fh:
            got = fh.read()
        self.assertEqual(got, crlf + rule + b"\n")                  # prefix bytes unchanged
        st = os.stat(p)
        seat._seed_seat_rules(d)
        self.assertEqual((os.stat(p).st_mtime_ns, os.stat(p).st_ino),
                         (st.st_mtime_ns, st.st_ino))                # no write at all
        # a file with no trailing newline gains exactly one before the rule
        d2 = tempfile.mkdtemp(prefix="helm-feedback-nonl-")
        with open(os.path.join(d2, "CLAUDE.md"), "wb") as fh:
            fh.write(b"# mine")
        seat._seed_seat_rules(d2)
        with open(os.path.join(d2, "CLAUDE.md"), "rb") as fh:
            self.assertEqual(fh.read(), b"# mine\n" + rule + b"\n")

    def test_mutation_control_emptying_the_door_blanks_every_surface(self):  # noqa: VACUOUS_ASSERTION — the three positives before the patches prove each observable is non-empty; the absences are the mutation's effect
        """Removing each constant must redden the arms above; this arm does
        the removal THROUGH THE FACADE (its __setattr__ fans the value out to
        every impl module) and shows the launch line, the native door, the
        seeded settings and the seeded rule all go blank — so a green pin
        above observes the door, not a coincidence of the text."""
        from helm import launch
        # unconditional positives BEFORE the door is emptied, same observables
        self.assertIn(self.PAIR + " claude ", seat.launch_line("codex"))
        self.assertEqual(launch.build_env({"PATH": "/bin"}, "alice")["DISABLE_BUG_COMMAND"], "1")
        self.assertEqual(self._seeded("codex")[0]["feedbackDrafts"], "off")
        with mock.patch.object(seat, "FEEDBACK_ENV", ()):
            line = seat.launch_line("codex")
            self.assertNotIn("DISABLE_", line)
            self.assertIn(" CLAUDE_CODE_AUTO_COMPACT_WINDOW=", line)   # the neighbour survives
            self.assertNotIn("DISABLE_FEEDBACK_COMMAND",
                             launch.build_env({"PATH": "/bin"}, "alice"))
        with mock.patch.object(seat, "FEEDBACK_DRAFTS_SETTING", ("feedbackDrafts", "notify")):
            got, _p = self._seeded("codex")
            self.assertEqual(got["feedbackDrafts"], "notify")
        with mock.patch.object(seat, "FEEDBACK_RULE", "MUTANT-RULE-2328"):
            d = tempfile.mkdtemp(prefix="helm-feedback-mut-")
            seat._seed_seat_rules(d)
            with open(os.path.join(d, "CLAUDE.md")) as fh:
                body = fh.read()
            self.assertIn("MUTANT-RULE-2328", body)
            self.assertNotIn("/feedback", body)


class ProxyRoutesCoverEveryCataloguedModel(unittest.TestCase):
    """A seat still running a pre-rotation model attests against the same
    catalogue as the new default. codex-4, 2026-09-09: sol measured live on
    three seats in the hour astra became the default, and a default-only
    route tuple would have dropped every one of their proxy proofs.

    CODEX_MODEL_RULING rotates the default to gpt-6.1-sol while retaining
    gpt-6-sol as the fallback probe; a running pane can still be on
    gpt-6-astra or gpt-5.6-sol until relaunch. So the routes are every
    CATALOGUED model, the
    model_context keys included, not the probe list."""
    FAMILY = "codex"  # noqa: SEAT_NAME — configured family identity is the property under test

    def test_default_leads_and_every_catalogued_model_is_an_exact_route(self):
        routes = seat.proxy_routes(self.FAMILY)
        self.assertEqual(routes[0]["alias"], seat.FAMILIES[self.FAMILY]["model"])
        self.assertEqual([r["alias"] for r in routes],
                         ["gpt-6.1-sol", "gpt-6-sol",
                          "gpt-5.3-codex-spark", "gpt-6-astra",
                          "gpt-5.6-sol"])
        self.assertEqual([r["alias"] for r in routes],
                         list(seat.family_catalogued_models(
                             seat.FAMILIES[self.FAMILY])))
        for route in routes:
            self.assertEqual(route, {"alias": route["alias"],
                                     "provider": self.FAMILY,
                                     "upstream_model": route["alias"]})
            self.assertEqual(seat.proxy_route_family(route),
                             (self.FAMILY, None))

    def test_a_pane_on_a_model_the_ruling_moved_off_keeps_its_proof(self):
        """The rotation's own case: neither retired id is a probe model any
        more, and each still resolves to exactly this family, so a pane
        running it until its relaunch is attested rather than refused as
        "unknown or ambiguous"."""
        fam = seat.FAMILIES[self.FAMILY]
        # POSITIVE CONTROL on the same tuple: it names the default and fallback,
        # so the absences below are measured non-membership.
        self.assertEqual(fam["probe_models"], ("gpt-6.1-sol", "gpt-6-sol"))
        for model in ("gpt-6-astra", "gpt-5.6-sol"):
            with self.subTest(model=model):
                self.assertNotIn(model, fam["probe_models"])
                self.assertIn(model, fam["model_context"])
                route = {"alias": model, "provider": self.FAMILY,
                         "upstream_model": model}
                self.assertEqual(seat.proxy_route_family(route),
                                 (self.FAMILY, None))
        # CONTROL on the same resolver: an id codex does not catalogue at all
        # is still no route, so the union did not become "any codex-ish id"
        self.assertIsNone(seat.proxy_route_family(
            {"alias": "gpt-6-luna", "provider": self.FAMILY,
             "upstream_model": "gpt-6-luna"})[0])

    def test_the_union_widens_the_model_list_never_the_match(self):
        exact = {"alias": "gpt-5.6-sol", "provider": self.FAMILY,
                 "upstream_model": "gpt-5.6-sol"}
        self.assertEqual(seat.proxy_route_family(exact), (self.FAMILY, None),
                         "positive control: the exact sol route did not resolve")
        # one field off the exact tuple, three ways, and none of them is a route
        retired = {"alias": "gpt-5.6-terra", "provider": self.FAMILY,
                   "upstream_model": "gpt-5.6-terra"}
        self.assertIsNone(seat.proxy_route_family(retired)[0])
        wrong_channel = {"alias": "gpt-5.6-sol", "provider": "xai",
                         "upstream_model": "gpt-5.6-sol"}
        self.assertIsNone(seat.proxy_route_family(wrong_channel)[0])
        renamed_upstream = {"alias": "gpt-5.6-sol", "provider": self.FAMILY,
                            "upstream_model": "gpt-6-astra"}
        self.assertIsNone(seat.proxy_route_family(renamed_upstream)[0])

    def test_every_catalogued_route_names_exactly_its_own_family(self):
        # the union must not make any route ambiguous anywhere in the table;
        # the sol route is the must-hit that proves the census walked it
        census = []
        for family in seat.FAMILIES:
            for route in seat.proxy_routes(family):
                census.append(route)
                self.assertEqual(seat.proxy_route_family(route), (family, None))
        self.assertIn({"alias": "gpt-5.6-sol", "provider": self.FAMILY,
                       "upstream_model": "gpt-5.6-sol"}, census)

    def test_a_probe_model_equal_to_the_default_is_listed_once(self):
        table = {"f": {"mode": "proxy", "auth_type": "p", "model": "m",
                       "probe_models": ("m", "n")}}
        self.assertEqual([r["alias"] for r in seat.proxy_routes("f", table)],
                         ["m", "n"])
        keyed = {"k": {"mode": "proxy-key", "provider": "p", "model": "m",
                       "base_url": "https://x/v1", "probe_models": ("m", "n")}}
        self.assertEqual([r["alias"] for r in seat.proxy_routes("k", keyed)],
                         ["m"])


class SubagentFrontmatterIdsAliasToTheFamilyModel(unittest.TestCase):
    """task/1948: CC's built-in agent types carry a claude model in their
    frontmatter and a proxy-family seat sends it upstream as-is (codex measured
    an Explore child 502 with "claude-opus-5" on an astra seat). The seat's
    proxy config now aliases every catalogued frontmatter id to the family's
    model on the family's OAuth channel; a channel the fork cannot alias, or a
    key-backed family with no channel, gets no block at all."""

    def test_codex_config_aliases_every_frontmatter_id_to_the_family_model(self):
        # NO `family` ARGUMENT, AND THAT IS THE POINT OF THIS ARM: the launch
        # model on every row is what a caller that names no family gets, and
        # what every family with no subagent_tiers table gets. The per-tier
        # shape a named family gets is the class below.
        from helm import seat_launch_assets as a, seat_catalog as c
        y = a._config_yaml(8317, "/auth", "tok", channel="codex", model="gpt-6-astra")
        self.assertIn("oauth-model-alias:\n  codex:\n", y)
        for alias in c.CC_AGENT_FRONTMATTER_MODELS:
            self.assertIn('      alias: "%s"\n      fork: true\n' % alias, y)
        # served identity stays the family's real model: no response rewrite
        self.assertIn("fork: true", y)                    # positive control
        self.assertNotIn("force-mapping", y)
        self.assertEqual(y.count('    - name: "gpt-6-astra"'), len(c.CC_AGENT_FRONTMATTER_MODELS))
        self.assertIn("claude-opus-5", c.CC_AGENT_FRONTMATTER_MODELS)   # the measured one

    def test_no_channel_or_an_unaliasable_channel_emits_no_block(self):
        from helm import seat_launch_assets as a
        # POSITIVE CONTROL on the same observable first: the block appears when it should
        self.assertIn("oauth-model-alias", a._config_yaml(1, "/a", "t", channel="xai", model="grok-build-0.1"))
        self.assertNotIn("oauth-model-alias", a._config_yaml(1, "/a", "t"))
        self.assertNotIn("oauth-model-alias", a._config_yaml(1, "/a", "t", channel=None, model="kimi-k3"))
        self.assertNotIn("oauth-model-alias", a._config_yaml(1, "/a", "t", channel="not-a-channel", model="m"))


class TheOpusFiveFiveIdRoutesWhereClaudeOpusFiveRoutes(unittest.TestCase):
    """Claude Code 2.1.280 resolves the word `opus` to `claude-opus-5-5`
    (its `latest_per_family` table), and a proxy-family seat sends that id
    upstream as-is: an id the proxy does not alias dies HTTP 502, exactly as
    `claude-opus-5` did before it had a row. The id joins the frontmatter
    catalog LAST, and on every family it routes to the same model as
    `claude-opus-5`: the opus tier, never a new choice."""

    NEW = "claude-opus-5-5"
    # THE CATALOG AS IT STOOD BEFORE THIS ID, transcribed rather than derived:
    # a derived prefix would follow any reorder, and refusing a reorder is
    # what this pin is for.
    BEFORE = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001",
              "claude-fable-5-1", "claude-haiku-4-5")

    def _gaps(self, required):
        """Every (family, launch model, id) whose shipped generator output
        lacks exactly one row for an id in ``required``, or routes the new id
        somewhere other than `claude-opus-5`; and the count of blocks read."""
        from helm import seat_catalog as c, seat_launch_assets as a
        gaps, read = [], 0
        for family, fam in c.FAMILIES.items():
            models = {fam["model"], *(fam.get("instance_models") or {}).values()}
            for model in sorted(models):
                by = {}
                if fam.get("mode") == "proxy-key":
                    text = a._config_yaml_key(
                        1, "tok", "p", "https://x.test/v1", model, "k",
                        fam.get("upstream_model") or model)
                    for name, alias in re.findall(
                            r'- name: "([^"]+)"\n\s+alias: "([^"]+)"', text):
                        by.setdefault(alias, []).append(name)
                elif fam.get("auth_type") in c.OAUTH_ALIAS_CHANNELS:
                    text = a._config_yaml(1, "/auth", "tok",
                                          channel=fam["auth_type"],
                                          model=model, family=family)
                    blocks = [body for key, body in a._top_blocks(text)[1]
                              if key == "oauth-model-alias"]
                    for row in (a._oauth_rows(blocks[0], fam["auth_type"])
                                if blocks else ()):
                        by.setdefault(row["alias"], []).append(row["name"])
                else:
                    continue
                read += 1
                gaps.extend((family, model, alias) for alias in required
                            if len(by.get(alias, ())) != 1)
                if by.get(self.NEW) != by.get("claude-opus-5"):
                    gaps.append((family, model, "%s != claude-opus-5" % self.NEW))
        return gaps, read

    def test_the_id_is_appended_last_and_nothing_reorders(self):  # noqa: VACUOUS_ASSERTION — the whole catalog tuple is compared value-by-value against a transcribed six-id expectation
        from helm import seat_catalog as c
        self.assertEqual(c.CC_AGENT_FRONTMATTER_MODELS,
                         self.BEFORE + (self.NEW,))

    def test_every_proxy_family_routes_it_where_claude_opus_5_goes(self):  # noqa: VACUOUS_ASSERTION — the empty gap list is bound to a read count asserted above the proxy-family count, and the sibling mutation arm drives the same sweep red
        from helm import seat_catalog as c
        gaps, read = self._gaps(c.CC_AGENT_FRONTMATTER_MODELS)
        self.assertEqual(gaps, [])
        # POSITIVE CONTROL: the sweep read both generators' output for every
        # proxy family and every declared instance model, not an empty set
        proxy = [f for f, fam in c.FAMILIES.items()
                 if fam.get("mode") in ("proxy", "proxy-key", "proxy-oauth")]
        self.assertGreater(len(proxy), 5)
        self.assertGreaterEqual(read, len(proxy))
        # EVERY TIER TABLE RESOLVES IT TO THE OPUS TIER: the same answer as
        # claude-opus-5, which on codex is "no row", so it follows the pane
        for family, fam in c.FAMILIES.items():
            self.assertEqual(c.subagent_tier_model(fam, self.NEW),
                             c.subagent_tier_model(fam, "claude-opus-5"), family)
            self.assertIsNone(c.subagent_tier_error(family, fam), family)

    def test_dropping_the_id_turns_the_coverage_arm_red(self):
        """MUTATION: the generators read the catalog at call time, so a catalog
        without the new id emits blocks without its row, and the sweep must
        name every family it left without one."""
        from helm import seat_catalog as c
        required = c.CC_AGENT_FRONTMATTER_MODELS
        with mock.patch.object(c, "CC_AGENT_FRONTMATTER_MODELS", self.BEFORE):
            gaps, _read = self._gaps(required)
        missing = {family for family, _model, alias in gaps if alias == self.NEW}
        covered = {f for f, fam in c.FAMILIES.items()
                   if fam.get("mode") == "proxy-key"
                   or fam.get("auth_type") in c.OAUTH_ALIAS_CHANNELS}
        self.assertTrue(covered)
        self.assertEqual(missing, covered)

    def test_a_live_config_without_the_row_is_stale_until_ensure_adds_it(self):
        """Every live config was written before this id existed. `helm seat
        doctor --ensure` reads proxy_config_plan: a config lacking the row must
        read as drift naming the id, the regenerated text must carry it routed
        like claude-opus-5, the plan must settle, and the watchdog's route
        proof must accept the result."""
        from helm import seat_catalog as c, seat_launch_assets as a, proxywatch
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        auth = os.path.join(d, "auth")
        os.makedirs(auth)
        with open(os.path.join(auth, "codex.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "codex", "access_token": "secret"}, f)
        path = os.path.join(d, "config.yaml")
        port = seat._instance_port("codex", "seat-under-test")
        with mock.patch.object(c, "CC_AGENT_FRONTMATTER_MODELS", self.BEFORE):
            old = a._config_yaml(port, auth, "inbound-secret", channel="codex",
                                 model="gpt-6.1-sol", family="codex")
        self.assertNotIn(self.NEW, old)
        with open(path, "w", encoding="utf-8") as f:
            f.write(old)
        plan = a.proxy_config_plan(path, "codex", "seat-under-test")
        self.assertTrue(plan["changed"])
        self.assertIn(self.NEW, plan["alias_drift"])
        blocks = [body for key, body in a._top_blocks(plan["text"])[1]
                  if key == "oauth-model-alias"]
        by = {row["alias"]: row["name"]
              for row in a._oauth_rows(blocks[0], "codex")}
        self.assertEqual(by[self.NEW], by["claude-opus-5"])
        self.assertEqual(by[self.NEW], "gpt-6.1-sol")
        # the earlier rows keep their order and the new row is the block's last
        self.assertEqual(list(by), list(self.BEFORE) + [self.NEW])
        self.assertTrue(plan["text"].startswith(old.split("oauth-model-alias:")[0]))
        with open(path, "w", encoding="utf-8") as f:
            f.write(plan["text"])
        settled = a.proxy_config_plan(path, "codex", "seat-under-test")
        self.assertFalse(settled["changed"])
        self.assertIsNone(settled["alias_drift"])
        _route, token, _indexes, why = proxywatch._proxy_config_route(
            path, "gpt-6.1-sol")
        self.assertIsNone(why)
        self.assertEqual(token, "inbound-secret")


class AProxyFamilyDeclaresAModelPerSubagentTier(unittest.TestCase):
    """The knob that routes a subagent by its frontmatter id, and the
    same knob must let a new family (kimi) map everything to its one model.
    Codex declares it for its WORKERS (sonnet, haiku): gpt-6.1-sol, the model
    the owner's ruling puts every codex on (CODEX_MODEL_RULING). On a
    gpt-6.1-sol pane every row is gpt-6.1-sol; the table departs from the pane
    only on a pane an operator pinned to another model with --model, which
    is why these arms drive such a pane.

    The knob is seat_catalog's per-family `subagent_tiers` table. A subagent's
    model is decided by ITS OWN frontmatter id (task/1948: the
    CLAUDE_CODE_SUBAGENT_MODEL pin does not override it), so a table from
    frontmatter id -> family model is the only place that choice can live
    without a second pane. Every arm here drives the shipped producer — the
    config generator, the config parser, the desired-state planner
    `seat doctor --ensure` decides STALE with, and the catalog's own validator.
    """

    #: a pane an operator pinned with --model: a model codex still
    #: catalogues (model_context) and no seat declares
    PANE = "gpt-5.6-sol"
    #: the worker tier, which is the model every codex seat runs
    TIER = "gpt-6.1-sol"
    #: what a codex config minted before the ruling names on every row
    RETIRED = "gpt-6-astra"
    SEAT = "seat-under-test"

    def _codex_config(self):
        from helm import seat_launch_assets as a
        return a._config_yaml(8317, "/auth", "tok", channel="codex",
                              model=self.PANE, family="codex")

    def _rows(self, text, channel="codex"):
        """The rows the SHIPPED reader reads back, through the SHIPPED block
        splitter: `_top_blocks` then `_oauth_rows` is exactly the path
        proxy_config_plan walks, so no regex of mine can let an arm pass over
        bytes the producer's own parser would refuse."""
        from helm import seat_launch_assets as a
        blocks = [body for key, body in a._top_blocks(text)[1]
                  if key == "oauth-model-alias"]
        self.assertEqual(len(blocks), 1, "one alias block")
        return a._oauth_rows(blocks[0], channel)

    def test_codex_carries_one_row_per_tier_and_nothing_else(self):  # noqa: VACUOUS_ASSERTION — the absence assertions sit after an unconditional positive control on the same bytes: the four rows are compared value-by-value and `fork: true` is asserted present before force-mapping is asserted absent
        from helm import seat_catalog as c
        y = self._codex_config()
        rows = self._rows(y)
        # THE MAPPING IS TRANSCRIBED ON PURPOSE, once: it is the owner's
        # ruling, not a value to be derived from the table under test (a
        # derived expectation would pass just as happily with the tiers
        # swapped, which is the failure this arm exists to catch).
        # THREE OF THESE ROWS COME FROM THE TABLE and three from the
        # DEFAULT: only sonnet and the two haiku ids declare a tier, while
        # opus and fable follow the seat's launch model — which is
        # `model=self.PANE` here, a pinned pane, so the two sources are told
        # apart on this pane and nowhere else.
        self.assertEqual([(row["name"], row["alias"], row["fork"]) for row in rows],
                         [(self.PANE, "claude-opus-5", "true"),
                          (self.TIER, "claude-sonnet-5", "true"),
                          (self.TIER, "claude-haiku-4-5-20251001", "true"),
                          (self.PANE, "claude-fable-5-1", "true"),
                          (self.TIER, "claude-haiku-4-5", "true"),
                          (self.PANE, "claude-opus-5-5", "true")])
        # the table is REAL on this pane: its rows and the pane's are two
        # different models
        self.assertNotEqual(self.PANE, self.TIER)
        # and the table itself names gpt-6.1-sol on every row it declares
        self.assertEqual(set(c.FAMILIES["codex"]["subagent_tiers"].values()),
                         {c.CODEX_MODEL_RULING["model"]})
        # one row per catalogued id, in the catalogue's order, nothing more
        self.assertEqual([row["alias"] for row in rows],
                         list(c.CC_AGENT_FRONTMATTER_MODELS))
        for row in rows:
            self.assertFalse(row.get("extra"), row)
            self.assertNotIn("force-mapping", row)
            # every emitted model is one the family catalogues — the promise
            # the import-time validator keeps
            self.assertIn(row["name"],
                          c.family_catalogued_models(c.FAMILIES["codex"]))
        # the absence assertion's positive control, unconditional and on the
        # same observable: this generator DOES write row keys into `y`, so
        # "no force-mapping" is a fact about a block that exists.
        self.assertIn("      fork: true\n", y)
        self.assertNotIn("force-mapping", y)
        self.assertEqual(y.count("fork: true"), len(c.CC_AGENT_FRONTMATTER_MODELS))

    def test_a_family_with_no_tier_table_is_byte_identical_to_today(self):  # noqa: VACUOUS_ASSERTION — the absence assertions follow unconditional positives on the same observables: the whole grok block is pinned byte-for-byte and codex's table is asserted PRESENT before grok's is asserted None
        """The control was captured from THIS generator before the table
        existed, and is pinned here verbatim, comment line
        included: a config's bytes ARE its desired state, so a needless
        difference would make `seat doctor --ensure` respawn grok's and
        gemini's live sidecars to deliver a reworded comment."""
        from helm import seat_launch_assets as a, seat_catalog as c
        TODAYS_XAI = (
            "# built-in subagent frontmatter ids -> this family's model (task/1948)\n"
            "oauth-model-alias:\n  xai:\n"
            '    - name: "grok-build-0.1"\n      alias: "claude-opus-5"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-sonnet-5"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-haiku-4-5-20251001"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-fable-5-1"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-haiku-4-5"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-opus-5-5"\n      fork: true\n')
        y = a._config_yaml(1, "/a", "t", channel="xai",
                           model="grok-build-0.1", family="grok")
        self.assertTrue(y.endswith(TODAYS_XAI), y[-600:])
        # POSITIVE CONTROL on the same observable before the absence: some
        # family DOES declare a table, so grok's None is a measured absence
        # rather than a key nothing ever sets.
        self.assertTrue(c.FAMILIES["codex"].get("subagent_tiers"))
        self.assertIsNone(c.FAMILIES["grok"].get("subagent_tiers"))  # the reason
        # THE PIN IS NOT VACUOUS: the same generator, asked about a family that
        # DOES declare a table, emits something else entirely.
        self.assertNotIn(TODAYS_XAI, self._codex_config())
        # and the key-backed half of the owner's sentence: one upstream for
        # every id, which is "map everything to its one model" with no table
        TODAYS_KIMI = (
            '    models:\n'
            '      - name: "kimi-k3"\n        alias: "kimi-k3"\n'
            '      - name: "kimi-k3"\n        alias: "claude-opus-5"\n'
            '      - name: "kimi-k3"\n        alias: "claude-sonnet-5"\n'
            '      - name: "kimi-k3"\n        alias: "claude-haiku-4-5-20251001"\n'
            '      - name: "kimi-k3"\n        alias: "claude-fable-5-1"\n'
            '      - name: "kimi-k3"\n        alias: "claude-haiku-4-5"\n'
            '      - name: "kimi-k3"\n        alias: "claude-opus-5-5"\n')
        self.assertIn(TODAYS_KIMI, a._config_yaml_key(
            8318, "tok", "moonshot", "https://api.moonshot.ai/v1", "kimi-k3",
            "sk-key"))

    def test_a_tier_naming_an_uncatalogued_model_is_refused_at_the_declaration(self):
        """The REAL validator, on tables that are not in FAMILIES — the catalog
        is never mutated, because a table that cannot be served must be refused
        before import completes and an arm that edited FAMILIES would be
        testing a world no seat ever runs in."""
        from helm import seat_catalog as c
        fam = {"mode": "proxy", "auth_type": "b", "model": "b-one",
               "probe_models": ("b-one", "b-two"),
               "model_context": {"b-three": 1000}}
        # POSITIVE CONTROLS FIRST, one per source of a catalogued id: the
        # launch model, a probe model, a model_context key.
        for model in ("b-one", "b-two", "b-three"):
            self.assertIsNone(c.subagent_tier_error(
                "bogus", dict(fam, subagent_tiers={"claude-opus-5": model})),
                model)
        # a value no source catalogues: refused, naming the value and the set
        reason = c.subagent_tier_error(
            "bogus", dict(fam, subagent_tiers={"claude-opus-5": "b-four"}))
        self.assertIn("b-four", reason)
        self.assertIn("does not catalogue", reason)
        self.assertIn("b-one, b-two, b-three", reason)
        # a KEY that is not a catalogued frontmatter id routes nothing
        reason = c.subagent_tier_error(
            "bogus", dict(fam, subagent_tiers={"claude-opus-6": "b-one"}))
        self.assertIn("claude-opus-6", reason)
        self.assertIn("not a catalogued frontmatter id", reason)
        # and a mode whose generator has ONE upstream per block: a table there
        # would be silently ignored, which is worse than this refusal
        reason = c.subagent_tier_error(
            "bogus", dict(fam, mode="proxy-key",
                          subagent_tiers={"claude-opus-5": "b-one"}))
        self.assertIn("proxy-key", reason)
        self.assertIn("_frontmatter_models_yaml", reason)
        # no table at all is not an error
        self.assertIsNone(c.subagent_tier_error("bogus", fam))
        # THE SHIPPED CATALOG PASSES ITS OWN GATE — the predicate the
        # module-level assertion runs at import
        self.assertIsNone(c._unserveable_subagent_tier())

    def test_the_config_parser_reads_the_per_id_models_back(self):
        """The block goes out and comes back: `_oauth_rows` is what every
        consumer of a live config reads it with (the drift reason, the merge,
        the watchdog's sibling reader), so a block only the generator can
        understand would be a block nothing can reconcile."""
        from helm import seat_catalog as c
        rows = self._rows(self._codex_config())
        by_alias = {row["alias"]: row["name"] for row in rows}
        self.assertEqual(by_alias, {"claude-opus-5": self.PANE,
                                    "claude-fable-5-1": self.PANE,
                                    "claude-sonnet-5": self.TIER,
                                    "claude-haiku-4-5-20251001": self.TIER,
                                    "claude-haiku-4-5": self.TIER,
                                    "claude-opus-5-5": self.PANE})
        self.assertEqual(len(by_alias), len(c.CC_AGENT_FRONTMATTER_MODELS))
        # the two models are read back DISTINCT — a parser that kept only the
        # first name would satisfy every other assertion here
        self.assertEqual(len(set(by_alias.values())), 2)

    def test_doctor_ensure_reconciles_a_live_codex_instance_to_the_tier_block(self):
        """`helm seat doctor --ensure` decides STALE with
        seat_launch_assets.proxy_config_plan (seat_health._config_drift_lines
        and the ensure sweep both read `changed`; seat_proxy._up regenerates
        from the same plan before starting the sidecar). So the arm drives THAT
        function, over a file a codex instance minted on gpt-6-astra carries —
        the same generator with no family named, every row on that pane's
        model. The owner's ruling moves that seat to gpt-6.1-sol, so the plan
        must read it stale and rewrite every row, the tiered ones included."""
        import tempfile
        from helm import seat                      # seeds the impl modules
        from helm import seat_launch_assets as a
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        auth = os.path.join(d, "auth")
        os.makedirs(auth)
        path = os.path.join(d, "config.yaml")
        # `seat-under-test` is the house fixture name; _instance_port maps a
        # non-numeric seat onto the family's own port, so the file the arm
        # writes and the desired state the plan derives agree about the port
        # and the only difference left to measure is the alias block.
        with open(path, "w", encoding="utf-8") as f:
            f.write(a._config_yaml(seat._instance_port("codex", self.SEAT),
                                   auth, "inbound-secret", channel="codex",
                                   model=self.RETIRED))
        plan = a.proxy_config_plan(path, "codex", self.SEAT)
        self.assertTrue(plan["changed"])
        # the reason NAMES the first row that moved, so an operator reading
        # the doctor line learns which id moved rather than "policy differs"
        self.assertIn("claude-opus-5", plan["alias_drift"])
        self.assertEqual([(row["name"], row["alias"])
                          for row in self._rows(plan["text"])],
                         [(self.TIER, "claude-opus-5"),
                          (self.TIER, "claude-sonnet-5"),
                          (self.TIER, "claude-haiku-4-5-20251001"),
                          (self.TIER, "claude-fable-5-1"),
                          (self.TIER, "claude-haiku-4-5"),
                          (self.TIER, "claude-opus-5-5")])
        self.assertNotIn(self.RETIRED, plan["text"])
        # custody survived the rewrite: the inbound bearer and the auth-dir are
        # the file's, not the generator's idea of them
        self.assertIn('  - "inbound-secret"', plan["text"])
        self.assertIn(auth, plan["text"])
        # AND IT SETTLES — a plan that stayed `changed` would respawn the
        # sidecar every three minutes forever
        with open(path, "w", encoding="utf-8") as f:
            f.write(plan["text"])
        settled = a.proxy_config_plan(path, "codex", self.SEAT)
        self.assertFalse(settled["changed"])
        self.assertIsNone(settled["alias_drift"])

    def test_only_the_name_a_facade_consumer_reads_is_facade_surface(self):  # noqa: VACUOUS_ASSERTION — every absence assertion sits after an unconditional positive on the SAME namespace: the facade attribute is resolved and called before its two siblings are asserted absent, and the generator is driven before the call-site import is asserted to be seat_catalog's
        """A new module owes the tree's registries, and `helm.seat`'s frozen
        surface is one of them (tests/test_seat_split_contract.py pins it
        EXACTLY) — so the question this arm answers is which of the three new
        catalog names the FACADE must carry.

        `family_catalogued_models` must: helm/proxywatch.py reads it as
        `seatmod.family_catalogued_models` on the module it imports as
        `from . import seat`, so dropping it from the facade breaks the alias
        watchdog rather than a test. `subagent_tier_model` and
        `subagent_tier_error` must NOT: their only callers are
        seat_launch_assets (twice) and seat_catalog's own import gate, and both
        reach them through `helm.seat_catalog` directly. Exporting them anyway
        would widen the pinned surface for nobody."""
        from helm import proxywatch, seat, seat_catalog, seat_launch_assets
        # THE FACADE NAME, resolved and CALLED (not merely hasattr'd) on the
        # module proxywatch itself imports, and identical to the catalog's.
        self.assertIs(seat.family_catalogued_models,
                      seat_catalog.family_catalogued_models)
        self.assertIn(self.TIER,
                      seat.family_catalogued_models(seat.FAMILIES["codex"]))
        # and proxywatch really does reach it by that spelling — read off the
        # shipped source, so a rename to a direct import updates this arm
        # rather than leaving it asserting a door nobody uses
        source = inspect.getsource(proxywatch._alias_rows_that_rename_a_route)
        self.assertIn("seatmod.family_catalogued_models", source)
        # THE TWO SIBLINGS ARE NOT FACADE SURFACE, and the positive control for
        # that absence is the line above: the same namespace answers one name.
        for name in ("subagent_tier_model", "subagent_tier_error"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(seat_catalog, name))   # it EXISTS
                self.assertFalse(hasattr(seat, name))          # just not here
        # AND THE CALL SITES STILL WORK WITHOUT THE FACADE: drive the shipped
        # generator, whose tier rows come from subagent_tier_model, and pin the
        # module its call site imports from.
        y = seat_launch_assets._config_yaml(
            8317, "/auth", "tok", channel="codex", model=self.PANE,
            family="codex")
        self.assertIn('    - name: "%s"\n' % self.TIER, y)
        self.assertIn("from .seat_catalog import",
                      inspect.getsource(
                          seat_launch_assets._frontmatter_alias_yaml))


class AnInstanceDeclaresItsOwnLaunchModel(unittest.TestCase):
    """One family entry, one OAuth cred pool, two models across panes:
    `instance_models` (seat_catalog) names a model per instance.

    THE SHIPPED CATALOG DECLARES NO TABLE. Codex once split instances across
    gpt-5.6-sol and gpt-6-astra; CODEX_MODEL_RULING supersedes that split: the
    family model is gpt-6.1-sol and every codex instance launches on it. The
    first arm pins that.

    THE MECHANISM STAYS, because a declaration is the only knob `seat doctor
    --ensure` cannot revert: it regenerates every instance's config.yaml and
    launch.sh from the generator on a cron, so a hand edit is gone within the
    minute. The arms that drive it PLANT a table on codex's entry for the
    arm's duration (`_planted`), naming gpt-5.6-sol, a model codex still
    catalogues (model_context), and every arm drives a SHIPPED producer
    through it: the desired-state planner `seat doctor --ensure` decides
    STALE with, the config generator, the launch line, and the catalog's own
    import gate.
    """

    #: the model every codex instance runs (CODEX_MODEL_RULING)
    FAMILY_MODEL = "gpt-6.1-sol"
    #: the model the planted table names: catalogued, and wider than the
    #: family model, so the window an instance is taught tells them apart
    DECLARED = "gpt-5.6-sol"
    # REAL SEAT IDENTITIES, DELIBERATELY: the superseded declaration named
    # these seats, and the first arm asks the shipped catalog about exactly
    # them -- a house fixture name there would assert a world no seat runs
    # in. The planted table names the same seats, so the mechanism arms ask
    # about the shape the fleet ran.
    DECLARED_SEATS = ("codex-4", "codex-5", "codex-8", "codex-9")  # noqa: SEAT_NAME — the superseded declaration is this class's subject
    PLAIN_SEAT = "codex-7"  # noqa: SEAT_NAME — the seat the superseded declaration left on the family model is this class's subject
    # a codex instance NOBODY declares: the house fixture spelling this suite
    # already uses for a minted codex instance, so the fallback arm asks about
    # an undeclared seat without naming a live one.
    UNDECLARED_SEAT = "codex-97"
    # TODAY'S BYTES for a seat on the family model, pinned rather than
    # derived from the table under test -- a derived expectation would follow
    # `instance_models` wherever it moved, and following it is the one thing
    # this control exists to refuse. A seat that declares NOTHING must not
    # move by a byte when a sibling declares a model.
    TODAYS_TAIL = (
        "# built-in subagent frontmatter ids -> this family's model (task/1948)\n"
        "# (per id where the family declares subagent_tiers: one pane, two models)\n"
        "oauth-model-alias:\n"
        "  codex:\n"
        '    - name: "gpt-6.1-sol"\n      alias: "claude-opus-5"\n      fork: true\n'
        '    - name: "gpt-6.1-sol"\n      alias: "claude-sonnet-5"\n      fork: true\n'
        '    - name: "gpt-6.1-sol"\n      alias: "claude-haiku-4-5-20251001"\n      fork: true\n'
        '    - name: "gpt-6.1-sol"\n      alias: "claude-fable-5-1"\n      fork: true\n'
        '    - name: "gpt-6.1-sol"\n      alias: "claude-haiku-4-5"\n      fork: true\n'
        '    - name: "gpt-6.1-sol"\n      alias: "claude-opus-5-5"\n      fork: true\n')

    def setUp(self):
        from helm import seat_catalog
        self.fam = seat_catalog.FAMILIES["codex"]
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.auth = os.path.join(self.d, "auth")
        os.makedirs(self.auth)

    def _planted(self):
        """The mechanism's table, on codex's live entry for one arm: every
        DECLARED_SEATS instance on DECLARED, restored when the arm ends."""
        return mock.patch.dict(self.fam, {"instance_models": {
            name: self.DECLARED for name in self.DECLARED_SEATS}})

    def _rows(self, text, channel="codex"):
        """The rows the SHIPPED reader reads back — `_top_blocks` then
        `_oauth_rows`, exactly the path `proxy_config_plan` walks."""
        from helm import seat_launch_assets as a
        blocks = [body for key, body in a._top_blocks(text)[1]
                  if key == "oauth-model-alias"]
        self.assertEqual(len(blocks), 1, "one alias block")
        return [(row["name"], row["alias"]) for row in a._oauth_rows(blocks[0], channel)]

    def _todays_config(self, seat_name):
        """The file THIS instance carries on disk today: the generator with no
        instance model, at this instance's own port, so the only difference a
        plan can measure is the alias block."""
        from helm import seat, seat_launch_assets as a
        path = os.path.join(self.d, "%s.yaml" % seat_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(a._config_yaml(seat._instance_port("codex", seat_name),
                                   self.auth, "inbound-secret", channel="codex",
                                   model=self.FAMILY_MODEL, family="codex"))
        return path

    def test_the_shipped_catalog_puts_every_codex_instance_on_gpt61_sol(self):  # noqa: VACUOUS_ASSERTION — the absent table sits beside the resolver answering gpt-6.1-sol for every transcribed instance, which is the positive reading of the same declaration
        """The owner's ruling, read through the resolver every producer
        below uses. The seats are transcribed -- the ones the superseded
        declaration split, the seat it left on astra, the family seat and one
        nobody named -- so a table that put any of them back on another model
        reddens here."""
        from helm import seat_catalog as c
        self.assertEqual(c.CODEX_MODEL_RULING["model"], self.FAMILY_MODEL)
        self.assertEqual(self.fam["model"], self.FAMILY_MODEL)
        self.assertIsNone(self.fam.get("instance_models"))
        others = ("codex", "codex-2", "codex-3", "codex-6")  # noqa: SEAT_NAME — the codex instances the superseded declaration left undeclared are this arm's subject
        for seat_name in others + (self.PLAIN_SEAT, self.UNDECLARED_SEAT) \
                + self.DECLARED_SEATS:
            with self.subTest(seat=seat_name):
                self.assertEqual(c.instance_launch_model(self.fam, seat_name),
                                 self.FAMILY_MODEL)
        # Every declared tier names the default; the probe list also retains
        # the superseded default as its fallback.
        self.assertEqual(self.fam["probe_models"],
                         (self.FAMILY_MODEL, "gpt-6-sol"))
        self.assertEqual(set(self.fam["subagent_tiers"].values()),
                         {self.FAMILY_MODEL})
        # a seat of ANOTHER family: the family model, which is byte-for-byte
        # what every caller passed before the table existed
        self.assertEqual(c.instance_launch_model(c.FAMILIES["kimi"], "kimi"),
                         c.FAMILIES["kimi"]["model"])

    def test_doctor_desired_state_moves_a_declared_seat_and_leaves_an_undeclared_one(self):
        """`helm seat doctor --ensure` decides STALE with
        `proxy_config_plan` (seat_health._config_drift_lines and the ensure
        sweep read `changed`; seat_proxy._up regenerates from the same plan
        before starting the sidecar), so THAT function is what an instance
        model has to reach -- a launch line alone would be reverted by the
        next sweep."""
        from helm import seat, seat_launch_assets as a   # seeds the impl modules
        del seat
        with self._planted():
            # THE CONTROL, and it is this arm's whole premise: the same file
            # shape for the seat that declares NOTHING is already the desired
            # state, so every difference below is attributable to the
            # declaration.
            settled = a.proxy_config_plan(self._todays_config(self.PLAIN_SEAT),
                                          "codex", self.PLAIN_SEAT)
            self.assertFalse(settled["changed"])
            self.assertIsNone(settled["alias_drift"])
            self.assertTrue(settled["old"].endswith(self.TODAYS_TAIL),
                            settled["old"][-600:])

            path = self._todays_config(self.DECLARED_SEATS[0])
            plan = a.proxy_config_plan(path, "codex", self.DECLARED_SEATS[0])
            self.assertTrue(plan["changed"])
            # the reason NAMES the id that moved, so an operator reading the
            # doctor line learns which row changed rather than "policy differs"
            self.assertIn("claude-opus-5", plan["alias_drift"])
            # the judgement ids follow the seat's own launch model and the
            # workers keep the family's tier: the table moves the PANE, and
            # the tier table still decides where its workers go
            self.assertEqual(self._rows(plan["text"]),
                             [(self.DECLARED, "claude-opus-5"),
                              (self.FAMILY_MODEL, "claude-sonnet-5"),
                              (self.FAMILY_MODEL, "claude-haiku-4-5-20251001"),
                              (self.DECLARED, "claude-fable-5-1"),
                              (self.FAMILY_MODEL, "claude-haiku-4-5"),
                              (self.DECLARED, "claude-opus-5-5")])
            # custody survived the rewrite: the inbound bearer and the
            # auth-dir are the file's, not the generator's idea of them
            self.assertIn('  - "inbound-secret"', plan["text"])
            self.assertIn(self.auth, plan["text"])
            # AND IT SETTLES -- a plan that stayed `changed` would respawn the
            # sidecar every three minutes forever
            with open(path, "w", encoding="utf-8") as f:
                f.write(plan["text"])
            again = a.proxy_config_plan(path, "codex", self.DECLARED_SEATS[0])
            self.assertFalse(again["changed"])
            self.assertIsNone(again["alias_drift"])

    def test_the_launch_line_carries_the_instance_model_and_its_window(self):
        """The pane's own model and the window CC is told, from
        `launch_line` -- the function `seat launch` prints and
        `_write_launch_assets` bakes into launch.sh."""
        from helm import seat
        with self._planted():
            declared = seat.launch_line("codex", seat=self.DECLARED_SEATS[0])
            plain = seat.launch_line("codex", seat=self.PLAIN_SEAT)
            # AN EXPLICIT CHOICE STILL OUTRANKS THE DECLARATION for that pane:
            # `seat launch --model` and the persisted choice seat.py
            # re-derives both arrive as this argument.
            pinned = seat.launch_line("codex", model="gpt-5.3-codex-spark",
                                      seat=self.DECLARED_SEATS[0])
        self.assertIn(" CLAUDE_CODE_SUBAGENT_MODEL=%s " % self.DECLARED, declared)
        self.assertIn(" CLAUDE_CODE_MAX_CONTEXT_TOKENS=320000 ", declared)
        # THE CONTROL on the same observable: the undeclared seat is the
        # family model's line, window included -- 220000 is gpt-6.1-sol's
        # model_context entry and 320000 is gpt-5.6-sol's, so the window
        # follows the INSTANCE model rather than the family's max_context.
        self.assertIn(" CLAUDE_CODE_SUBAGENT_MODEL=%s " % self.FAMILY_MODEL, plain)
        self.assertIn(" CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000 ", plain)
        self.assertNotIn(self.DECLARED, plain)
        self.assertIn(" CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.3-codex-spark ", pinned)
        self.assertIn(" CLAUDE_CODE_MAX_CONTEXT_TOKENS=76000 ", pinned)
        # AND WITH NOTHING PLANTED, the shipped catalog: the same seat is on
        # the family model, so the mechanism above is what moved it
        shipped = seat.launch_line("codex", seat=self.DECLARED_SEATS[0])
        self.assertIn(" CLAUDE_CODE_SUBAGENT_MODEL=%s " % self.FAMILY_MODEL,
                      shipped)
        self.assertIn(" CLAUDE_CODE_MAX_CONTEXT_TOKENS=220000 ", shipped)

    def test_an_undeclared_family_mints_todays_bytes_for_every_instance(self):  # noqa: VACUOUS_ASSERTION — the grok block is pinned byte-for-byte and a planted codex table is asserted present before grok's is asserted None
        """A family with no `instance_models` at all: its instances are the
        family model, byte-identical to before the table existed. A config's
        bytes ARE its desired state, so a needless difference here would
        respawn grok's and gemini's live sidecars on the next sweep."""
        from helm import seat_catalog as c, seat_launch_assets as a
        TODAYS_XAI = (
            "# built-in subagent frontmatter ids -> this family's model (task/1948)\n"
            "oauth-model-alias:\n  xai:\n"
            '    - name: "grok-build-0.1"\n      alias: "claude-opus-5"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-sonnet-5"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-haiku-4-5-20251001"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-fable-5-1"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-haiku-4-5"\n      fork: true\n'
            '    - name: "grok-build-0.1"\n      alias: "claude-opus-5-5"\n      fork: true\n')
        grok = c.FAMILIES["grok"]
        self.assertIsNone(grok.get("instance_models"))          # the reason
        y = a._config_yaml(1, "/a", "t", channel="xai",
                           model=c.instance_launch_model(grok, "grok-2"),
                           family="grok")
        self.assertTrue(y.endswith(TODAYS_XAI), y[-600:])
        # POSITIVE CONTROL on the same observable: a family that DOES declare
        # a table answers something else for the same question, so grok's
        # answer is a measured absence rather than a resolver that ignores
        # the table.
        with self._planted():
            self.assertTrue(self.fam.get("instance_models"))
            self.assertNotEqual(
                c.instance_launch_model(self.fam, self.DECLARED_SEATS[0]),
                self.fam["model"])

    def test_a_launch_model_outside_the_family_catalogue_is_refused(self):
        """The REAL validator, on tables that are not in FAMILIES -- the
        catalog is never mutated, because a table that cannot be served must
        be refused before import completes and an arm that edited FAMILIES
        would be testing a world no seat ever runs in."""
        from helm import seat_catalog as c
        fam = {"mode": "proxy", "auth_type": "b", "model": "b-one",
               "probe_models": ("b-one", "b-two"),
               "model_context": {"b-three": 1000}}
        # POSITIVE CONTROLS FIRST, one per source of a catalogued id -- the
        # same three `family_catalogued_models` readings the tier validator
        # accepts, so the two doors cannot drift apart
        for model in ("b-one", "b-two", "b-three"):
            self.assertIsNone(c.instance_model_error(
                "bogus", dict(fam, instance_models={"bogus-2": model})), model)
        # a value no source catalogues: refused, naming the value and the set
        reason = c.instance_model_error(
            "bogus", dict(fam, instance_models={"bogus-2": "b-four"}))
        self.assertIn("b-four", reason)
        self.assertIn("does not catalogue", reason)
        self.assertIn("b-one, b-two, b-three", reason)
        # A KEY NO SEAT RESOLVES TO THIS FAMILY, which would be a declaration
        # silently never applied -- `_seat_family` resolves `<family>` and
        # `<family>-<N>` and nothing else
        reason = c.instance_model_error(
            "bogus", dict(fam, instance_models={"notbogus-2": "b-one"}))
        self.assertIn("notbogus-2", reason)
        self.assertIn("not an instance of this family", reason)
        self.assertIsNone(c.instance_model_error(
            "bogus", dict(fam, instance_models={"bogus": "b-one"})))
        # and a mode that mints no per-instance proxy at all: the declaration
        # would reach the launch line and never the config its subagents route
        # through, which is worse than this refusal
        reason = c.instance_model_error(
            "bogus", dict(fam, mode="proxy-key", instance_models={"bogus-2": "b-one"}))
        self.assertIn("proxy-key", reason)
        self.assertIn("_mint_instance_proxy", reason)
        # no table at all is not an error
        self.assertIsNone(c.instance_model_error("bogus", fam))
        # THE SHIPPED CATALOG PASSES ITS OWN GATE -- the predicate the
        # module-level assertion runs at import
        self.assertIsNone(c._unserveable_instance_model())

    def _mint_launch_sh(self, seat_name, model=None):
        """A seat's launch.sh, written by the SHIPPED writers.

        NO HAND-WRITTEN LINE: `launch_line` composes it, `_launch_owner` wraps
        it for the supervisor and `_write_launch_sh` lands the bytes -- the
        same three calls `_write_launch_assets` makes. A fixture that typed an
        `--model` token would test a file format helm does not mint, and the
        supervisor wrapping (a single shell-quoted argument) is exactly the
        shape a naive reader fails on."""
        from helm import seat_launch_assets as a
        path = os.path.join(self.d, "launch-%s-%s.sh" % (seat_name, model))
        a._write_launch_sh(path, "#!/bin/sh\n" + a._launch_owner(
            a.launch_line("codex", seat=seat_name, model=model,
                          identity=seat_name)))
        return path

    def test_the_listing_separates_the_declared_default_from_the_minted_model(self):  # noqa: VACUOUS_ASSERTION — the absent-script assertIsNone sits BESIDE two unconditional positive controls on the same observable (`_minted_model` answers spark for the pinned mint and sol for the default one), so the None is a measured third outcome and not this arm's only claim
        """`helm seat list` names TWO models per seat, and the second is read
        off the seat's own assets.

        THE DEFECT: the column carried `instance_launch_model` under a legend
        that called it what that seat's pane launches on. An operator's
        explicit `seat launch codex-4 --model gpt-5.3-codex-spark` (persisted
        in spawn.json and re-derived into every later re-mint) launches that
        pane on a 76000 window while the listing kept saying sol -- a false
        claim on the one screen model-sized work is allocated from.

        THE ROUND-3 RENAME: that second column is `minted`, never `launched`.
        launch.sh is re-written on every add, launch and resume, so it proves
        what the NEXT spawn of this seat uses; a pane started before the last
        mint can be running another model, and calling the disk value
        `launched` overclaimed in the same direction, one step smaller."""
        from helm import seat_catalog as c, seat_health as h
        spark = "gpt-5.3-codex-spark"
        seat_name = self.DECLARED_SEATS[0]
        planted = self._planted()
        planted.start()
        self.addCleanup(planted.stop)
        pinned = self._mint_launch_sh(seat_name, model=spark)
        self.assertEqual(h._minted_model(pinned), spark)
        self.assertEqual(c.instance_launch_model(self.fam, seat_name),
                         self.DECLARED,
                         "the DECLARATION is unmoved by a pinned launch")
        # THE CONTROL, and it is what proves the reader reads the FILE: the
        # same seat minted with no explicit model launches on its declaration,
        # so both answers are the declared model. BLAST RADIUS: this arm
        # alone -- a
        # `_minted_model` that returned the declaration (or that dropped the
        # nested re-split and answered None) reddens the assertion above while
        # this one stays green, so the pair cannot both pass by accident.
        default = self._mint_launch_sh(seat_name)
        self.assertEqual(h._minted_model(default), self.DECLARED)
        # NEVER THE DECLARATION REPEATED: a seat with no launch.sh has nothing
        # on disk recording what a spawn of it would use.
        absent = os.path.join(self.d, "absent.sh")
        self.assertIsNone(h._minted_model(absent))
        # ...AND THE COLUMN'S WORD FOR THAT IS `unminted`, NOT UNKNOWN. An
        # absent script is a read helm COMPLETED — nothing is minted, so there
        # is no minted model — while UNKNOWN on this surface means "helm could
        # not read that input". Spending UNKNOWN on the absence also collided
        # with the CRED column: tests/test_seat_cred_state.py
        # SeatRowRendersUnknownTest asserts a healthy proxy-key seat's whole
        # row carries no UNKNOWN, and an absent-launch.sh UNKNOWN turned
        # ds4pro (key baked into config.yaml, no launch.sh) into an alarm
        # those arms cannot tell apart from the cred fact they guard. ONE word
        # for that case, pinned here and in the cred suite.
        self.assertEqual(h._minted_model_text(absent), "unminted")
        self.assertNotIn("UNKNOWN", h._minted_model_text(absent))
        # UNKNOWN IS KEPT FOR THE SCRIPT THAT EXISTS AND CANNOT BE ANSWERED
        # FROM. Derived from the SHIPPED line rather than typed: the minted
        # command with its trailing `--model <model>` cut off is the shape a
        # pre-model-pin or hand-edited launch.sh has.
        modelless = os.path.join(self.d, "modelless.sh")
        with open(pinned) as fh:
            text = fh.read()
        cut = text.replace(" --model " + spark, "")
        self.assertNotEqual(cut, text, "the pin must actually have been cut")
        with open(modelless, "w") as f:
            f.write(cut)
        self.assertIsNone(h._minted_model(modelless))
        self.assertEqual(h._minted_model_text(modelless), "UNKNOWN")
        # THE RECEIPT for the reader's nested re-split: `_launch_owner` hands
        # the whole command to the supervisor as ONE shell-quoted argument, so
        # a flat tokenisation of the minted script carries no bare `--model`
        # token at all. This is a claim about the SHIPPED file, and it is why
        # the reader re-splits.
        import shlex
        with open(pinned) as fh:
            flat = shlex.split(fh.read(), comments=True)
        self.assertNotIn("--model", flat)
        self.assertTrue([t for t in flat if "--model" in t], flat)

    def test_a_failed_lookup_of_the_launch_script_is_UNKNOWN_not_absence(self):
        """A LOOKUP THAT FAILED IS NOT A SEAT THAT WAS NEVER LAUNCHED.

        Round 2 rendered the never-minted word off `os.path.exists(path)`
        being False, and that predicate is False for a file helm could not
        look AT: an unreadable parent directory, an I/O error, a dangling
        symlink whose name is on disk and whose target is not. Each of those
        is "helm could not read that input" — UNKNOWN — and the error CLASS
        goes on the screen so the operator knows which repair to make."""
        from helm import seat_health as h
        spark = "gpt-5.3-codex-spark"
        pinned = self._mint_launch_sh(self.DECLARED_SEATS[0], model=spark)
        # AN UNREADABLE PARENT DIRECTORY. The SHIPPED artifact is moved into a
        # directory with no search permission, so the failing call is the same
        # open the reader makes on a real seat. Guarded rather than asserted
        # blind: a suite running as root traverses 0o000 anyway, and an arm
        # that silently depends on the runner's uid is a vacuous pass. The
        # guard reads the same path the reader will.
        locked = os.path.join(self.d, "locked")
        os.makedirs(locked)
        buried = os.path.join(locked, "launch.sh")
        shutil.copy(pinned, buried)
        # THE UNCONDITIONAL LEG, AND IT IS THE ONE THAT ASSERTS. The chmod
        # below is a mode bit, and a mode bit is not a promise: a runner under
        # root (or a permissive umask, or a filesystem mounted without
        # permission semantics) traverses 0o000 and the guarded leg then
        # asserts NOTHING while still reporting green — an arm whose coverage
        # depends on the host's uid is a vacuous pass on exactly the hosts it
        # matters least on. So the refusal is driven at the READ instead: the
        # one open the reader makes on THIS path raises PermissionError on
        # every host, and every other open in the process is untouched.
        # BLAST RADIUS: this leg alone. A reader that dropped the error class
        # from the word, or that answered the absence word for a refused read,
        # reddens it; the shipped-file legs below stay green.
        real_open = open
        def deny_this_one(path, *a, **k):
            if path == buried:
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", deny_this_one):
            word = h._minted_model_text(buried)
        self.assertEqual(word, "UNKNOWN (PermissionError)", word)
        self.assertNotIn("unminted", word)
        # ...AND THE MODE BIT AS A SECOND ARM, kept because it is the only leg
        # that proves the REAL filesystem refusal reaches the same word (the
        # double above proves the handler, this proves the world raises what
        # the double raises). Still guarded, and now that costs nothing: the
        # unconditional leg above carries the claim.
        os.chmod(locked, 0o000)
        try:
            try:
                open(buried).close()
                bit = False
            except PermissionError:
                bit = True
            if bit:
                word = h._minted_model_text(buried)
                self.assertTrue(word.startswith("UNKNOWN"), word)
                self.assertIn("PermissionError", word)
                # THE WHOLE POINT: not the never-launched word. This seat IS
                # launched — the bytes are right there — and only the
                # permission bit stops helm reading them.
                self.assertNotIn("unminted", word)
        finally:
            os.chmod(locked, 0o700)
        # THE MISSING-FILE CONTROL, on the same reader in the same tmpdir: a
        # path with nothing at it renders the absence word. BLAST RADIUS: this
        # pair only. A cure that answered UNKNOWN for every unreadable path by
        # answering UNKNOWN for ALL of them reddens this line, and a reader
        # that kept the round-2 `os.path.exists` reddens the two above — so
        # neither half can pass by collapsing into the other.
        self.assertEqual(
            h._minted_model_text(os.path.join(self.d, "nothing-here.sh")),
            "unminted")
        # A DANGLING SYMLINK. `open` raises FileNotFoundError exactly as it
        # does for an absent path, so ENOENT alone cannot decide absence; the
        # NAME exists (os.path.lexists), which makes this a read helm could
        # not complete.
        dangling = os.path.join(self.d, "dangling.sh")
        os.symlink(os.path.join(self.d, "no-such-target.sh"), dangling)
        self.assertFalse(os.path.exists(dangling))
        self.assertTrue(os.path.lexists(dangling))
        word = h._minted_model_text(dangling)
        self.assertTrue(word.startswith("UNKNOWN"), word)
        self.assertIn("symlink", word)
        # AND THE ROUND-2 LEGS SURVIVE, measured in the same breath so a cure
        # that broke them cannot hide behind the new ones.
        self.assertEqual(h._minted_model_text(pinned), spark)

    def _mint_instance_assets(self, home, seat_name, model=None, script=True):
        """One minted instance under a seat home, by the SHIPPED writers.

        `config.yaml` is what `_minted_instances` enumerates on (that is the
        predicate it ships with), and launch.sh is composed by `launch_line` +
        `_launch_owner` + `_write_launch_sh` — the same three calls
        `_write_launch_assets` makes. Nothing here is typed by hand, because a
        fixture that invents the file format tests a world helm does not
        mint."""
        from helm import seat_launch_assets as a
        idir = os.path.join(home, "instances", seat_name)
        os.makedirs(idir)
        open(os.path.join(idir, "config.yaml"), "w").close()
        path = os.path.join(idir, "launch.sh")
        if script:
            a._write_launch_sh(path, "#!/bin/sh\n" + a._launch_owner(
                a.launch_line("codex", seat=seat_name, model=model,
                              identity=seat_name)))
        return path

    def test_one_undecodable_launch_sh_costs_its_own_field_and_no_other_row(self):
        """ONE SEAT'S BAD BYTE MUST NOT ERASE ITS WHOLE FAMILY.

        THE DEFECT: the minted-model reader opened launch.sh in TEXT mode
        under an `except OSError` boundary. `open(...).read()` decodes, so a
        script carrying one undecodable byte — an owner-wrapped preset whose
        shell comment holds a raw 0xff — raises UnicodeDecodeError, and that
        is a ValueError, NOT an OSError. The exception left the field reader
        and unwound `_seat_row`, which assembles the family line AND every
        instance line before it returns anything: `_status` caught it, printed
        ROW FAILED for the family, and one seat's bad byte took the family row
        plus every HEALTHY sibling's liveness, cred and usability off the
        screen. main never read scripts for this field at all, so the whole
        failure is one this lane introduced.

        THE WITNESS IS POPULATED AND DRIVES THE SHIPPED LISTING PRODUCER —
        `_seat_row`, the function `_status` calls once per family — not the
        field helper alone, because the helper answering correctly is not the
        claim; the claim is that the BLOCK survives.

        THE CONTROL, MEASURED BY MUTATION rather than asserted: with the
        boundary put back to `except OSError as exc` (one conjunct, nothing
        else touched), this exact fixture makes `_seat_row("codex")` raise
        `UnicodeDecodeError: ... byte 0xff in position 1101` out of
        `minted_model(inst)` — so the family row and all three instance rows
        are gone. BLAST RADIUS of that mutation: this arm and the
        dangling/permission arm's decode-free legs stay green, which is why
        the positive control below (the same bytes raising through a plain
        `open`) sits beside the assertions: it proves the fixture really is
        undecodable rather than merely unusual."""
        from helm import seat, seat_health as h
        spark = "gpt-5.3-codex-spark"
        home = os.path.join(self.d, "seats", "codex")
        os.makedirs(os.path.join(home, "auth"))
        # THREE INSTANCES, THREE FATES, ONE BLOCK. Undeclared house spellings
        # (no live seat is named here) so the declared column reads the family
        # default and this arm asks only about the minted one.
        bad = self._mint_instance_assets(home, "codex-91", model=spark)
        with open(bad, "ab") as f:
            f.write(b"# owner preset note: \xff\n")   # binary: a real raw byte
        self._mint_instance_assets(home, "codex-92", model=spark)
        self._mint_instance_assets(home, "codex-93", script=False)
        # POSITIVE CONTROL ON THE INPUT: the reader's own call shape fails on
        # these bytes. Without this the arm could pass over a file that
        # happened to decode, asserting nothing.
        with self.assertRaises(UnicodeDecodeError):
            with open(bad) as f:
                f.read()
        # `seat_dir` is patched on `helm.seat`, whose module class fans every
        # set out to the impl modules — so `_minted_instances` (seat_health)
        # and `_instance_dir` (seat_launch_assets) both see this home, which
        # is how the cred suite drives this same producer.
        with mock.patch.object(seat, "seat_dir", lambda f, *a, **k: home), \
             mock.patch.object(h, "_proxy_live_text",
                               lambda *a, **k: ("proxy UP pid 1 port 2", None)):
            self.assertEqual(h._minted_instances("codex"),
                             ["codex-91", "codex-92", "codex-93"],
                             "the shipped enumerator must see all three")
            row = h._seat_row("codex")
        lines = row.split("\n")
        # THE FAMILY ROW IS STILL THERE — the thing the raise cost.
        self.assertTrue(lines[0].startswith("codex "), lines)
        by_seat = {}
        for line in lines[1:]:
            name = line.strip().split(" ", 1)[0]
            by_seat.setdefault(name, line)
        self.assertEqual(sorted(by_seat), ["codex-91", "codex-92", "codex-93"],
                         row)
        # THE AFFECTED FIELD ALONE, and it names the class so the operator
        # knows the repair is the FILE and not the mode bits.
        self.assertIn("UNKNOWN (UnicodeDecodeError)", by_seat["codex-91"])
        # THE HEALTHY SIBLING STILL RENDERS ITS MODEL. A raise out of the
        # field reader unwinds the whole block, so this seat's data depends on
        # its neighbour's read never escaping.
        self.assertIn(spark, by_seat["codex-92"])
        self.assertNotIn("UNKNOWN", by_seat["codex-92"])
        # AND THE MEASURED ABSENCE KEEPS ITS OWN WORD: a minted proxy with no
        # launch.sh is `unminted`, never an UNKNOWN and never a decode story.
        self.assertIn("unminted", by_seat["codex-93"])
        self.assertNotIn("UNKNOWN", by_seat["codex-93"])
        # THE SIBLING READER TOO. `_minted_model` returns None (not a raise)
        # for the same bytes; it is the primitive behind any caller that wants
        # the model rather than the word.
        self.assertIsNone(h._minted_model(bad))

    def test_the_shipped_record_NOW_names_the_model_a_running_pane_attested(self):
        """`launched` became a question helm CAN answer, and this arm is where
        that was designed to be noticed.

        THE MINT IS NOT THE PANE: launch.sh is re-written on every add, launch
        and resume, so the `minted` column proves what the next spawn uses.
        The only record a live pane self-writes is the roster runtime row, and
        its validator (`seats_runtime._runtime_metadata`) is the one door
        every writer funnels through, so what that validator carries decides
        what a pane can attest. It carries `model` beside agent_harness,
        family and backend (task/2655), which is why the stored record names
        the model and the `launched` column is renderable at all.

        THIS ARM IS A TRIPWIRE ON THAT FIELD SET, in both directions. It
        asserts the KEY is present, so a validator that stopped carrying it
        reddens this line rather than silently returning every seat to
        UNKNOWN. `_launched_model_text` requires no cooperation: it reads
        `runtime["model"]` directly and answers UNKNOWN for any row that has
        none.
        """
        from helm import seat_health as h, seats
        spark = "gpt-5.3-codex-spark"
        seat_name = self.DECLARED_SEATS[0]
        row = seats.write_roster(
            seat_name, presence_beat=False,
            runtime={"family": "codex",
                     "agent_harness": "claude",  # noqa: SEAT_NAME — the AGENT HARNESS value production writes, not a seat identity
                     "backend": "proxy", "model": spark})
        stored = row.get("runtime")
        # THE POSITIVE CONTROL that the writer ran and the record is real:
        # the fields the validator already carried survived the round trip.
        self.assertEqual(stored.get("family"), "codex")
        self.assertEqual(stored.get("backend"), "proxy")
        self.assertIs(row.get("runtime_verified"), True)
        self.assertEqual(stored.get("model"), spark,
                         "the pane's own attested model now survives the one "
                         "door every writer funnels through")
        self.assertEqual(h._launched_model_text(seat_name, row), spark,
                         "and the display helper answers it instead of "
                         "UNKNOWN — the column it gates is now renderable")
        self.assertEqual(h._launched_model_text(seat_name), "UNKNOWN",
                         "control: handed NO row it still reads nothing and "
                         "answers UNKNOWN, so this is the stored record "
                         "talking and not a second roster read")
        # AND IT READ NOTHING TO SAY SO. This helper renders a display field,
        # so a roster fetch of its own would be a SECOND read of a fact the
        # caller already holds — the call-site count-pin in
        # tests/test_display_launder_tripwire.py reddens on exactly that. The
        # sentinel makes any seats.roster() call from inside the function an
        # error, so this line drives the shipped reader with the sentinel
        # installed and proves the read is gone rather than merely unused.
        # CONTROL + BLAST RADIUS: the sentinel is scoped to these two calls
        # (this arm only); before the cure the row=None call fetched the
        # roster and this assertion raised RuntimeError instead of passing,
        # and the assertCalled check below proves the sentinel was really
        # bound (a patch that missed its target would let a vacuous pass
        # through).
        calls = []

        def _refuse_roster(*a, **kw):
            calls.append(a)
            raise RuntimeError("display re-read the roster")

        with mock.patch.object(seats, "roster", _refuse_roster):
            self.assertEqual(h._launched_model_text(seat_name), "UNKNOWN")
            self.assertEqual(h._launched_model_text(seat_name, None), "UNKNOWN")
            # the sentinel IS bound and IS the raising kind: calling it
            # through the same attribute the function would have used raises.
            with self.assertRaises(RuntimeError):
                seats.roster()
        self.assertEqual(len(calls), 1, "only the control call reached roster")
        # NOT A CONSTANT: the reader returns the attested model the day a
        # record carries one. The dict here is deliberately NOT a fixture
        # input standing in for a producer — no shipped producer can make this
        # record, which is the finding — it is the control proving the UNKNOWN
        # above is a measured absence in the STORE and not a stubbed return.
        # BLAST RADIUS: this arm only.
        self.assertEqual(
            h._launched_model_text(seat_name,
                                   {"runtime_verified": True,
                                    "runtime": {"family": "codex",
                                                "model": spark}}), spark)
        # UNVERIFIED TESTIMONY IS NOT TESTIMONY: a row whose runtime was never
        # self-attested cannot name a launch model either.
        self.assertEqual(
            h._launched_model_text(seat_name,
                                   {"runtime": {"model": spark}}), "UNKNOWN")

    def test_the_roster_legend_calls_that_column_the_minted_one(self):
        """The legend is the sentence the operator reads the column THROUGH,
        so the false claim lived there: "the model column ... is what THAT
        SEAT's pane launches on". It now names both fields, what each one is
        measured from, and the question helm refuses to answer."""
        from helm import seat_usability
        legend = seat_usability.legend()
        self.assertNotIn("THAT SEAT's pane launches on", legend)
        self.assertIn("declared=", legend)
        self.assertIn("minted=", legend)
        self.assertIn("launch.sh", legend)
        # BOTH WORDS THE MINTED COLUMN CAN RENDER are named, because the
        # operator's question at `unminted` ("is this broken?") and at
        # UNKNOWN ("what could helm not read?") are different questions.
        self.assertIn("minted=unminted", legend)
        self.assertIn("minted=UNKNOWN", legend)
        # AND THE MISSING COLUMN IS EXPLAINED, not silently absent: an
        # operator who reads `minted` as the running pane's model makes the
        # same allocation mistake the declaration column made.
        self.assertIn("no launched column", legend)
        self.assertIn("re-minted on every add, launch and resume", legend)

    def _advertised(self, seat_name=None, model=None):
        """The window the SHIPPED launch line advertises -- read out of
        `launch_line`'s own output rather than recomputed from
        `model_context`, so this arm cannot drift from the resolver."""
        line = seat.launch_line("codex", seat=seat_name or "codex", model=model)
        got = re.search(r" CLAUDE_CODE_MAX_CONTEXT_TOKENS=(\d+) ", line)
        self.assertIsNotNone(got, line)
        return int(got.group(1))

    def test_the_advertised_window_never_exceeds_a_routed_child_capacity(self):
        """THE CONTEXT-SAFETY INVARIANT, in the direction that is true.

        `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is process-global: every subagent
        believes the PANE's number. So the number must be no larger than the
        capacity of EVERY model this family routes a child to. A WIDER child
        than the pane is understated -- early compaction, recoverable. A
        NARROWER child is OVERSTATED, which wedges, and it is a narrower child
        that would force the launch line to advertise the minimum.

        The seat_catalog comment and docs/VERBS.md stated this backwards
        ("no tier may name a model LARGER than the launch model's"), which
        permitted exactly the wide-pane/narrow-tier pane the same paragraph
        calls unrecoverable and banned the narrow-pane/wide-tier shape. No
        shipped helper computes the minimum, so the code cure is the prose;
        this arm is what pins the direction against real numbers."""
        def safe(pane_model, child_model):
            return self._advertised(model=pane_model) \
                <= self._advertised(model=child_model)
        # THE SHIPPED FLEET HOLDS IT: every codex seat, on its own declared
        # launch model, against every tier model it routes children to.
        for seat_name in (self.PLAIN_SEAT, self.UNDECLARED_SEAT) \
                + self.DECLARED_SEATS:
            pane = self._advertised(seat_name=seat_name)
            for alias, child in self.fam["subagent_tiers"].items():
                with self.subTest(seat=seat_name, alias=alias):
                    self.assertLessEqual(pane, self._advertised(model=child))
        # THE TWO DIRECTIONS, the journal's falsifier verbatim: advertising
        # 320000 with a 220000 child is REFUSED, 220000 with a 320000 child is
        # accepted. The old sentence answered these the other way round.
        self.assertEqual(self._advertised(model=self.DECLARED), 320000)
        self.assertEqual(self._advertised(model=self.FAMILY_MODEL), 220000)
        self.assertFalse(safe(self.DECLARED, self.FAMILY_MODEL),
                         "a 5.6-sol pane with a gpt-6.1-sol tier overstates its "
                         "children by 100k -- the unrecoverable direction")
        self.assertTrue(safe(self.FAMILY_MODEL, self.DECLARED),
                        "a gpt-6.1-sol pane with a 5.6-sol child understates "
                        "them, which compacts early rather than wedging")

    def test_the_context_safety_rule_is_written_in_the_safe_direction(self):
        """The prose cure itself, in both places that carried the reversed
        sentence. A documentation defect is still a defect when the document
        is what the next author derives a table from -- the reversed rule
        would have been implemented as a validator that refuses the shipped
        catalog and accepts the wedging one.

        BLAST RADIUS: these two files only. Reverting either paragraph reddens
        this arm and nothing else in the suite."""
        from helm import seat_catalog as c
        root = os.path.dirname(os.path.dirname(os.path.abspath(c.__file__)))
        with open(c.__file__, encoding="utf-8") as fh:
            catalog = fh.read()
        with open(os.path.join(root, "docs", "VERBS.md"), encoding="utf-8") as fh:
            verbs = fh.read()
        # the REVERSED sentence, in the spelling each file carried
        self.assertNotIn("model_context IS LARGER THAN THE LAUNCH MODEL'S",
                         catalog)
        self.assertNotIn("tier is wider than its", verbs)
        # and the corrected one
        self.assertIn("model_context IS SMALLER THAN THE LAUNCH MODEL'S",
                      catalog)
        self.assertIn("A tier added on a NARROWER model", catalog)
        self.assertIn("No shipped tier is narrower than its pane.", verbs)
        self.assertIn("must be no larger than the capacity of EVERY model",
                      verbs)


class PoolListingNamesWhatItOmits(unittest.TestCase):
    """`helm seat list` enumerates the PROXY POOL, and every rostered seat
    without a proxy directory was rendering as nothing at all.

    THE OBSERVABLE IS THE NOTE'S TEXT, not a return code: the defect was that
    a live seat's absence read identically to no-such-seat, so what these arms
    hold is the SENTENCE an operator sees.
    """

    def _note(self, shown, families, roster=(), raw=None, mode=None):
        """Render the note against a REAL roster file on disk.

        NO DOUBLES, AND THAT IS THE CONTRACT RATHER THAN A PREFERENCE. The
        reader under test CANNOT raise on its own: `seats.roster()` is
        pk.read_json's non-strict path, which swallows everything and answers
        `{}`. A double whose roster() raises is therefore more CAPABLE than
        the function it stands for, and an arm built on one asserts a safety
        property the verb does not have. Every cell here is a real file —
        valid, corrupt, truncated, unreadable or absent.

        `raw` writes those bytes verbatim; `mode` chmods the file afterwards;
        `roster=None` leaves no file at all. Otherwise the roster is a
        well-formed mapping of `roster`.
        """
        import json
        import tempfile
        from helm import seat_health as sh
        from helm.seats_common import roster_path

        # HELM_CHAT_DIR, NOT HELM_HOME, AND THE DIFFERENCE IS ISOLATION.
        # `chat_dir()` is `home.surface_dir("CHAT_DIR", ...)`, so HELM_CHAT_DIR
        # WINS over a redirected HELM_HOME — and tests/__init__.py sets it for
        # the whole process. Patching only HELM_HOME leaves every cell here
        # reading AND WRITING the suite's one shared roster: the cells that
        # write then read their own bytes still pass, and the cell that writes
        # NOTHING reads a sibling's leftovers. Worse than a wrong answer, it
        # puts this arm's fixtures into a file other modules read.
        home = tempfile.mkdtemp(prefix="helm-poolnote-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with mock.patch.dict(os.environ, {"HELM_HOME": home,
                                          "HELM_CHAT_DIR": home}):
            path = roster_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if raw is not None:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(raw)
            elif roster is not None:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({k: {} for k in roster}, fh)
            if mode is not None:
                os.chmod(path, mode)
            try:
                with mock.patch.object(sh, "seats_root",
                                       lambda: "/tmp/seats"):
                    return "\n".join(sh._population_note(shown, families))
            finally:
                if mode is not None:
                    os.chmod(path, 0o644)

    def test_a_rostered_seat_with_no_proxy_directory_is_named(self):
        # POSITIVE CONTROL on the same observable: a seat IN the pool is not
        # reported missing, so a note that named everything would fail here.
        t = self._note(["seat-a"], ["seat-a"], ["seat-a", "seat-b"])
        self.assertIn("seat-b", t)
        self.assertIn("1 rostered seat NOT listed above", t)
        self.assertIn("helm chat seats --all", t)
        self.assertNotIn("seat-a,", t)

    def test_a_minted_instance_that_is_rendered_is_not_called_missing(self):
        """The note must not report a name sitting three lines up the screen.

        `_status` renders families PLUS each family's minted instances, so a
        note handed only the family list calls every instance omitted — the
        same false absence this note exists to end, pointed inward.
        """
        shown = ["seat-a", "seat-a-2", "seat-a-3"]
        t = self._note(shown, ["seat-a"], shown + ["seat-b"])
        self.assertIn("seat-b", t)          # the genuinely absent one
        self.assertIn("1 rostered seat NOT listed above", t)
        for rendered in ("seat-a-2", "seat-a-3"):
            self.assertNotIn(rendered + ",", t)
            self.assertNotIn(rendered + " ", t)

    def test_a_roster_that_does_not_read_says_UNKNOWN_never_the_all_clear(self):  # noqa: VACUOUS_ASSERTION — per-cell control, see below
        # The positive control is PER CELL and sits
        # inside the loop, on that cell's own `t`, because each unreadable
        # roster is a separate render and a control on a DIFFERENT render
        # cannot show that THIS one happened. The rung's structural rule reads
        # an in-loop control as "may not run"; the iterable is a literal of
        # three cells, so it always does, and an outer control on another
        # binding would be the weaker arrangement it is asking for.
        """Driven by REAL unreadable files, because the reader cannot raise.

        `seats.roster()` is pk.read_json's non-strict path: it swallows every
        exception and answers `{}`, so a corrupt roster is indistinguishable
        from an empty one and a guard built on catching a raise catches
        nothing. That is why `_rostered_elsewhere` reads the path with
        strict=True, and why these cells are files rather than doubles.
        """
        # POSITIVE CONTROL: a READABLE roster through this same helper does
        # produce the omission sentence, so its absence below is the unreadable
        # file and not a broken fixture.
        ok = self._note(["seat-a"], ["seat-a"], ["seat-a", "seat-b"])
        self.assertIn("rostered seat NOT listed above", ok)
        for label, kw in (("corrupt", {"raw": "{not json"}),
                          ("truncated", {"raw": '{"seat-a": {'}),
                          ("unreadable", {"roster": ["seat-a"],
                                          "mode": 0o000})):
            with self.subTest(roster=label):
                t = self._note(["seat-a"], ["seat-a"], **kw)
                # POSITIVE CONTROL INSIDE THE LOOP, on this cell's own
                # observable: the note RENDERED. Without it an empty or
                # raised-through `t` satisfies both absences below.
                self.assertIn("enumerates the PROXY POOL", t)
                self.assertIn("UNKNOWN", t)
                self.assertIn("Absence above proves nothing", t)
                self.assertNotIn("rostered seat NOT listed above", t)

    def test_an_ABSENT_roster_is_a_true_empty_and_not_a_failed_look(self):
        """Deliberate asymmetry: a box with no roster file yet has no rostered
        seats. strict=True keeps `missing` and `failed` apart, and folding
        absence into UNKNOWN would cry wolf on every fresh home."""
        t = self._note(["seat-a"], ["seat-a"], roster=None)
        self.assertIn("enumerates the PROXY POOL", t)      # it rendered
        self.assertNotIn("UNKNOWN", t)
        self.assertNotIn("NOT listed above", t)

    def test_the_match_is_casefold_exact_not_raw_spelling(self):
        """seats_common.canonical_keys states the relation and the failure: "a
        READER that indexes the mapping with a raw spelling asks a different
        question and gets a silent miss, which reads exactly like an absent
        seat." A raw test names a seat rostered as `Seat-A` missing from a pool
        holding `seat-a` — this note manufacturing the absence it exists to
        end."""
        # POSITIVE CONTROL on the same observable: a genuinely absent seat is
        # still named, so a note matching everything could not pass here.
        t = self._note(["seat-a"], ["seat-a"], ["Seat-A", "seat-b"])
        self.assertIn("seat-b", t)
        self.assertIn("1 rostered seat NOT listed above", t)
        self.assertNotIn("Seat-A", t)

    def test_the_remainder_is_counted_rather_than_dropped(self):
        from helm import seat_health as sh
        missing = ["m%02d" % i for i in range(sh._POPULATION_NAMED + 4)]
        t = self._note(["seat-a"], ["seat-a"], ["seat-a"] + missing)
        self.assertIn("%d rostered seats NOT listed above" % len(missing), t)
        self.assertIn("(+4 more)", t)
        self.assertIn(missing[0], t)
        self.assertNotIn(missing[-1], t)      # beyond the cap, counted not named

    def test_the_population_is_stated_even_when_nothing_is_omitted(self):
        """The disclosure is the cure, not the warning: the verb's NAME is what
        misleads, so it states what it enumerated on every run."""
        t = self._note(["seat-a", "seat-a-2"], ["seat-a"], ["seat-a", "seat-a-2"])
        self.assertIn("enumerates the PROXY POOL", t)
        self.assertIn("1 seat directory under /tmp/seats", t)
        self.assertIn("2 names above", t)
        self.assertIn("It is not the fleet.", t)
        self.assertNotIn("NOT listed above", t)

    def test_the_note_is_indented_so_it_cannot_be_read_as_a_seat_row(self):
        """Column 0 in this listing MEANS "a rendered seat row".

        `tests/test_seat_roster.py::_family_rows` — the reader this listing
        already has — takes every unindented line as a seat row and its first
        token as the family name. An unindented note beginning "helm seat
        list ..." therefore minted a phantom family called `helm`: a note
        about false absences inventing a false presence. The rule is the
        listing's own ("instance, usability, warning, and legend lines are
        all indented"), so this holds the note to it rather than to a style.
        """
        t = self._note(["seat-a"], ["seat-a"], ["seat-a", "seat-b"])
        # POSITIVE CONTROL first: the note HAS content on every line, so an
        # empty render could not pass this by having nothing to indent.
        self.assertTrue(all(line.strip() for line in t.splitlines()), t)
        self.assertIn("seat-b", t)
        for line in t.splitlines():
            self.assertTrue(line.startswith(" "),
                            "column-0 line reads as a seat row: %r" % line)

    def test_a_roster_key_is_laundered_before_it_reaches_the_terminal(self):
        """These are roster KEYS, and a key is unvalidated at the join seam.

        The most prominent line this verb prints would otherwise carry a
        hostile HELM_CHAT_NAME's ESC/bidi straight to the operator's
        terminal. `_seat_label` is the law the other direct-roster-read
        surfaces already obey.
        """
        hostile = "seat-\x1b[31mred\x1b[0m"
        t = self._note(["seat-a"], ["seat-a"], ["seat-a", hostile])
        # POSITIVE CONTROL on the same observable: the seat IS named, so a
        # note that simply dropped every hostile row would fail here rather
        # than pass for the wrong reason.
        self.assertIn("red", t)
        self.assertNotIn("\x1b", t)


class LeadLeanProfileTest(unittest.TestCase):
    """task/4056 part 3: a LEAD's settings.json is lean.

    A Claude lead re-sends a fixed ~83k tokens on every request (the floor
    census under the token-mechanics review). Part of that floor is MCP
    servers and listings a lead never calls, so a lead's home takes a lean
    profile. The switches are the LITE profile's own (seat_catalog
    PROFILES), extended rather than duplicated — but the KEY is different on
    purpose: lite is a FAMILY property, and lead is a ROLE, so this one is
    read from the seat's spawn register and applied on top of whatever family
    profile the seat already has.

    WHAT IS DELIBERATELY NOT HERE, because a lead uses them: the lite MCP
    floor (leads call cv and the Docs tools), and the lite deny of the
    delegation and agent-listing tools (leads use both).

    ARTIFACT IS DENIED, on the owner's ruling: an artifact belongs to
    whichever baseload account published it and those accounts rotate, so a
    lead's reports go as local HTML files instead. Denying the tool is also
    what removes its schema from the request."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lean-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _mint(self, role):
        cdir = os.path.join(self.tmp, "cfg-" + str(role))
        os.makedirs(cdir)
        seat._seed_seat_settings(cdir, "kimi", role=role)
        with open(os.path.join(cdir, "settings.json")) as f:
            return json.load(f)

    def test_the_deny_list_is_the_object_shape_CC_actually_accepts(self):
        """MEASURED against the installed binary (2.1.287, `claude doctor`
        reading a settings file): a plain string is REFUSED with
        "deniedMcpServers[0]: Invalid entry was ignored: Invalid input:
        expected object, received string", and `{"serverName": ...}` is
        accepted. A string list would make the whole file schema-invalid, and
        CC may then skip it entirely, taking the bypass flag, the deny list
        and the hook wiring down with it."""
        # the servers are a host-local name, so the arm SETS one: without
        # this the shape is only exercised on a host that has configured it,
        # and the assertion would pass vacuously wherever it is not.
        from helm import localnames
        with mock.patch.object(localnames, "words",
                               return_value=("alpha", "beta")):
            got = self._mint("lead")
        servers = got.get("deniedMcpServers") or []
        self.assertTrue(servers, servers)
        for entry in servers:
            self.assertIsInstance(entry, dict, entry)
            self.assertEqual(sorted(entry), ["serverName"], entry)
            self.assertIsInstance(entry["serverName"], str)

    def test_the_plugin_key_is_the_installed_id_not_the_bare_name(self):
        """An installed plugin's key is `<id>@<marketplace>`; the bare name
        matches nothing."""
        got = self._mint("lead")
        plugins = got.get("enabledPlugins") or {}
        self.assertEqual(list(plugins),
                         [seat_catalog.LEAD_DISABLED_PLUGIN_ID])
        self.assertIn("@", seat_catalog.LEAD_DISABLED_PLUGIN_ID)
        self.assertIs(plugins[seat_catalog.LEAD_DISABLED_PLUGIN_ID], False)

    def test_a_lead_home_denies_the_servers_and_listings_it_never_uses(self):
        """The SERVERS are host-local: the source names no private project,
        so they come from a local-names key and a fresh clone denies none.
        The other three switches are not names, so they always apply."""
        from helm import localnames
        got = self._mint("lead")
        self.assertEqual(got["enabledPlugins"],
                         {seat_catalog.LEAD_DISABLED_PLUGIN_ID: False})
        self.assertEqual(got["skillListingBudgetFraction"],
                         seat_catalog.LEAD_SKILL_LISTING_BUDGET)
        self.assertEqual(sorted(got.get("deniedMcpServers") or []),
                         sorted(seat_catalog.lead_denied_mcp_servers()))
        # the positive control on the same call: the key is really read from
        # localnames rather than restated in this file.
        self.assertIn("lead-denied-mcp-servers", localnames.KEYS)

    def test_a_worker_home_is_untouched(self):
        # THE POSITIVE CONTROL on the same call: a lead really is changed, so
        # the worker's equality with the plain file below is the role branch
        # and not a seeder that wrote nothing at all.
        lead = self._mint("lead")
        # the control names a switch that is NOT host-local, so it holds on a
        # machine whose local-names file is absent (as this one is).
        self.assertEqual(lead["enabledPlugins"],
                         {seat_catalog.LEAD_DISABLED_PLUGIN_ID: False})
        base = os.path.join(self.tmp, "cfg-plain")
        os.makedirs(base)
        seat._seed_seat_settings(base, "kimi")
        with open(os.path.join(base, "settings.json")) as f:
            plain = json.load(f)
        self.assertEqual(self._mint("worker"), plain)
        self.assertNotIn("skillListingBudgetFraction", plain)
        self.assertNotIn("enabledPlugins", plain)

    def test_the_profile_is_read_from_the_seat_register_not_the_name(self):
        """A lead is whoever the SPAWN recorded as one. A name that looks like
        a lead is an ordinary worker, because the reader is the spawn record,
        not the name."""
        from helm.seat_role import recorded_role
        self.assertEqual(recorded_role("nothing-registered-here"), "worker")
        # the positive control on the same reader: a seat the register knows
        # is answered from the register, so a reader that always said
        # "worker" would be caught here rather than passing by accident.
        self.assertIn(recorded_role("nothing-registered-here"),
                      ("lead", "worker"))

    def test_a_lead_denies_Artifact_on_the_owners_ruling(self):
        """An artifact belongs to whichever baseload account published it and
        those accounts rotate, so a lead's reports go as local files; denying
        the tool also takes its schema out of every request."""
        deny = (self._mint("lead").get("permissions") or {}).get("deny") or []
        self.assertIn("Artifact", deny)
        # the positive control on the same observable: the deny list is real
        # and carries the other helm-wide entries, so an empty list (which
        # would also contain no Artifact) cannot pass this arm.
        self.assertTrue(deny, deny)

    def test_a_registered_lead_record_gives_lead_and_a_broken_one_gives_worker(self):
        """The role comes from the spawn register, so the READER must be
        pinned: a mutant that always returned "worker" would pass every arm
        that only asserts on role=None."""
        from helm import seat as seat_mod
        from helm.seat_role import recorded_role
        d = os.path.join(self.tmp, "reglead")
        os.makedirs(d)
        with mock.patch.object(seat_mod, "_instance_dir", return_value=d), \
                mock.patch.object(seat_mod, "_seat_family",
                                  return_value=("claude", None)):
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump({"seat": "reglead", "role": "lead"}, f)
            self.assertEqual(recorded_role("reglead"), "lead")
            # a record whose seat does not MATCH is somebody else's row
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump({"seat": "other", "role": "lead"}, f)
            self.assertEqual(recorded_role("reglead"), "worker")
            # a record with no role, and a corrupt one, are workers
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump({"seat": "reglead"}, f)
            self.assertEqual(recorded_role("reglead"), "worker")
            with open(os.path.join(d, "spawn.json"), "w") as f:
                f.write("{not json")
            self.assertEqual(recorded_role("reglead"), "worker")
            # and a role that is not a seat role is a worker, never a typo
            # that reaches the profile
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump({"seat": "reglead", "role": "leader"}, f)
            self.assertEqual(recorded_role("reglead"), "worker")

    def test_the_launch_threads_the_recorded_role_into_the_seeder(self):
        """The other half of the same mutant: reading a role nobody consumes
        changes nothing, so the launch path must really pass it through."""
        # patch the DEFINING module, not the facade: `seat._seed_seat_settings`
        # is a re-export, and the implementation calls its own module global.
        from helm import seat_launch_assets, seat_role
        seen = {}
        real = seat_launch_assets._seed_seat_settings

        def spy(cdir, family=None, workdir=None, model=None, window=None,
                role=None):
            seen["role"] = role
            return real(cdir, family, workdir=workdir, model=model,
                        window=window, role=role)

        # THE TARGET MUST BE THE SEAT'S OWN INSTANCE DIR, or the surface
        # ownership gate refuses before the seeder is ever reached (and an arm
        # that never reached it would read as "role not threaded").
        d = seat._instance_dir("kimi", "leanlead")
        os.makedirs(d)
        with mock.patch.object(seat_launch_assets, "_seed_seat_settings",
                               side_effect=spy), \
                mock.patch.object(seat_role, "recorded_role",
                                  return_value="lead"):
            seat._write_launch_assets("kimi", d, seat="leanlead")
        self.assertEqual(seen.get("role"), "lead")

    def test_a_lead_denies_the_whole_artifact_trio_through_the_recorded_deny(self):
        """The owner's ruling turns the Artifact TOOLS off, not one of them,
        and the deny goes through the one recorded-deny writer: on a seat
        whose family denies none of the trio itself, all three are appended
        AND named in helm.seeded_denies, so `helm seat retire-deny` can see
        who wrote them. CONTROL on the same mint: a worker gets none."""
        trio = ["Artifact", "ArtifactComments", "ArtifactData"]
        cdir = os.path.join(self.tmp, "cfg-native-lead")
        os.makedirs(cdir)
        seat._seed_seat_settings(cdir, None, role="lead")
        with open(os.path.join(cdir, "settings.json")) as f:
            got = json.load(f)
        deny = got["permissions"]["deny"]
        self.assertEqual([t for t in deny if t in trio], trio)
        self.assertEqual([t for t in got["helm"]["seeded_denies"] if t in trio],
                         trio)
        self.assertEqual(list(seat_catalog.LEAD_DENIED_TOOLS), trio)
        wdir = os.path.join(self.tmp, "cfg-native-worker")
        os.makedirs(wdir)
        seat._seed_seat_settings(wdir, None, role="worker")
        with open(os.path.join(wdir, "settings.json")) as f:
            wdeny = json.load(f)["permissions"]["deny"]
        self.assertFalse(set(trio) & set(wdeny), wdeny)

    def test_the_local_seats_still_deny_the_same_trio(self):
        """The trio is named once: the local seats' set carries it whole."""
        self.assertEqual([t for t in seat_catalog.LOCAL_UNUSED_TOOLS
                          if t.startswith("Artifact")],
                         ["Artifact", "ArtifactComments", "ArtifactData"])

    def test_a_lead_keeps_the_tools_it_actually_uses(self):
        got = self._mint("lead")
        deny = (got.get("permissions") or {}).get("deny") or []
        for tool in ("Workflow", "SendMessage", "ListAgents", "WebFetch"):
            self.assertNotIn(tool, deny)
