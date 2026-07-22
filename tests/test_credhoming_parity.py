"""Credhoming parity — the owner's twin concern proven end-to-end:

  (A) LAUNCH.SH AUTHORITATIVE — a seat's minted launch.sh pins its own
      CLAUDE_CONFIG_DIR / unsets ANTHROPIC_API_KEY / strips the child stamp
      via `env VAR=val … claude`, so it WINS over whatever a metaharness host
      (orca/herdr) leaked into the parent environment. This is the
      orca-can't-clobber-us proof, and it is proven by ACTUALLY RUNNING the
      real minted launch.sh as a subprocess under a deliberately polluted
      parent env and reading back what the exec'd `claude` actually saw —
      never a string match on the launch line.

  (B) PER-ACCOUNT ISOLATION — two seats homed on two different accounts get
      DISTINCT config homes and DISTINCT proxy tokens; neither's credential
      material ever reaches the other's child, and neither launch.sh carries
      the other's token. The seat-layer isolation is proven by running both
      launch.sh files and diffing what each child saw; the account-layer
      (codexhomes pool) isolation is proven by constructing the exact leak
      the dedup could cause — two DIFFERENT accounts sharing one email — and
      asserting they stay two records, each carrying only its own token.

  (C) REBOOT CHECKLIST — the docs/REBOOT_CHECKLIST_1.4.149.md exists and
      names the exact checks (with expected results) an operator runs at the
      live claude 1.4.149 switch, so credhoming parity is re-verifiable by
      hand at the moment it matters.

Hermetic: HELM_HOME/HELM_CHAT_DIR/HELM_PROC + seat.CODEX_HOMES all point at
tmp dirs; the exec'd `claude` is a throwaway shim on a scoped PATH; no real
claude, no real credential home, no login, no network.
"""
import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from helm import codexhomes, seat


def _b64seg(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims):
    return _b64seg({"alg": "RS256", "typ": "JWT"}) + "." + _b64seg(claims) + ".fake-sig"


# The env a hostile/careless metaharness parent (orca launched from an owner
# shell) might carry: a foreign config home, the owner's real key, and the
# child-session stamp that silently kills a seat's transcript persistence.
PARENT_POLLUTION = {
    "CLAUDE_CONFIG_DIR": "/orca/host/OWNER-CONFIG-must-not-win",
    "ANTHROPIC_API_KEY": "sk-LEAKED-OWNER-KEY-DEADBEEF",
    "CLAUDE_CODE_CHILD_SESSION": "leaked-child-stamp",
    "CLAUDE_CODE_SESSION_ID": "leaked-sid",
    "CLAUDE_CODE_BRIDGE_SESSION_ID": "leaked-bridge",
}


class _SeatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-credparity-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "HELM_PROC",
                      "HELM_PROXY_BIN", "MELD_PROXY_BIN", "KIMI_API_KEY",
                      "HELM_CODEX_HOMES_DIR", "MELD_CODEX_HOMES_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        for k in ("MELD_HOME", "HELM_PROXY_BIN", "MELD_PROXY_BIN",
                  "KIMI_API_KEY", "HELM_CODEX_HOMES_DIR", "MELD_CODEX_HOMES_DIR"):
            os.environ.pop(k, None)
        self._codex_homes = seat.CODEX_HOMES
        seat.CODEX_HOMES = os.path.join(self.tmp, "codex-homes")
        os.makedirs(seat.CODEX_HOMES)
        # scoped bin dir: the exec'd `claude` is our shim, nothing else changes
        self.shim = os.path.join(self.tmp, "bin")
        os.makedirs(self.shim)
        claude = os.path.join(self.shim, "claude")
        with open(claude, "w") as f:
            f.write("#!/bin/sh\n"
                    'echo "CFG=$CLAUDE_CONFIG_DIR"\n'
                    'echo "TOKEN=$ANTHROPIC_AUTH_TOKEN"\n'
                    'echo "NAME=$HELM_CHAT_NAME"\n'
                    'echo "BASEURL=$ANTHROPIC_BASE_URL"\n'
                    'echo "APIKEY=${ANTHROPIC_API_KEY-<UNSET>}"\n'
                    'echo "STAMP=${CLAUDE_CODE_CHILD_SESSION-<UNSET>}"\n'
                    'echo "SID=${CLAUDE_CODE_SESSION_ID-<UNSET>}"\n'
                    'echo "BRIDGE=${CLAUDE_CODE_BRIDGE_SESSION_ID-<UNSET>}"\n')
        os.chmod(claude, 0o755)

    def tearDown(self):
        seat.CODEX_HOMES = self._codex_homes
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def _plant_codex(self, name="home-a", email="a@x.test", account="acct-a"):
        d = os.path.join(seat.CODEX_HOMES, name)
        os.makedirs(d, exist_ok=True)
        exp = int(time.time()) + 3600
        auth = {"auth_mode": "chatgpt", "tokens": {
            "id_token": _jwt({"email": email, "https://api.openai.com/auth":
                              {"chatgpt_plan_type": "pro"}}),
            "access_token": _jwt({"exp": exp, "sub": "fake"}),
            "refresh_token": "fake-refresh-" + name,
            "account_id": account,
        }, "last_refresh": "2026-07-09T14:52:47.713051089Z"}
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump(auth, f)

    def _add(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(args))
        return rc, out.getvalue(), err.getvalue()

    # -- the real thing: run a launch.sh, read what claude actually saw ----
    def _exec_launch(self, launch_sh, extra_parent=None):
        env = {"PATH": self.shim + os.pathsep +
               os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(PARENT_POLLUTION)
        if extra_parent:
            env.update(extra_parent)
        p = subprocess.run(["sh", launch_sh], env=env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        seen = {}
        for line in p.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                seen[k] = v
        return seen, p.stdout


class LaunchShAuthoritativeTest(_SeatBase):
    """(A) the orca-can't-clobber-us proof — ACTUAL env precedence."""

    def test_launch_sh_pins_config_dir_over_polluted_parent(self):
        self._plant_codex()
        self.assertEqual(self._add("add", "codex")[0], 0)
        d = seat.seat_dir("codex")
        launch_sh = os.path.join(d, "launch.sh")
        cfgdir = os.path.join(d, "claude")
        with open(os.path.join(d, "token")) as f:
            token = f.read().strip()

        seen, _ = self._exec_launch(launch_sh)

        # the launch.sh's own pin WON over the parent's foreign config home
        self.assertEqual(seen["CFG"], cfgdir)
        self.assertNotEqual(seen["CFG"], PARENT_POLLUTION["CLAUDE_CONFIG_DIR"])
        # the seat's proxy token reached claude; the leaked owner key did NOT
        self.assertEqual(seen["TOKEN"], token)
        self.assertEqual(seen["APIKEY"], "<UNSET>")   # env -u stripped it
        self.assertNotIn("DEADBEEF", seen["TOKEN"])
        # stable seat identity, not an inherited one
        self.assertEqual(seen["NAME"], "codex")
        self.assertEqual(seen["BASEURL"], "http://127.0.0.1:8317")
        # the child-session stamp trio was stripped -> seat starts top-level
        self.assertEqual(seen["STAMP"], "<UNSET>")
        self.assertEqual(seen["SID"], "<UNSET>")
        self.assertEqual(seen["BRIDGE"], "<UNSET>")

    def test_negative_control_unpinned_launch_sh_DOES_leak(self):
        """Teeth: without the env-pin an inherited launch.sh WOULD take the
        parent's polluted home + key — proving the real launch.sh's pin is
        load-bearing, and that this harness actually detects a leak."""
        leaky = os.path.join(self.tmp, "leaky-launch.sh")
        with open(leaky, "w") as f:
            f.write('#!/bin/sh\nexec claude "$@"\n')   # inherits everything
        os.chmod(leaky, 0o755)
        seen, _ = self._exec_launch(leaky)
        self.assertEqual(seen["CFG"], PARENT_POLLUTION["CLAUDE_CONFIG_DIR"])
        self.assertEqual(seen["APIKEY"], PARENT_POLLUTION["ANTHROPIC_API_KEY"])
        self.assertEqual(seen["STAMP"], "leaked-child-stamp")


class PerAccountIsolationTest(_SeatBase):
    """(B) two accounts never cross-pollinate — seat layer AND pool layer."""

    def test_two_seats_get_isolated_homes_and_tokens(self):
        # account 1: a codex seat (OAuth-account proxy)
        self._plant_codex()
        self.assertEqual(self._add("add", "codex")[0], 0)
        # account 2: a kimi seat (a distinct account, key-based)
        os.environ["KIMI_API_KEY"] = "sk-kimi-account-two-fake"
        self.assertEqual(self._add("add", "kimi")[0], 0)
        del os.environ["KIMI_API_KEY"]

        cdir, kdir = seat.seat_dir("codex"), seat.seat_dir("kimi")
        c_sh, k_sh = (os.path.join(cdir, "launch.sh"),
                      os.path.join(kdir, "launch.sh"))
        c_home = os.path.join(cdir, "claude")
        k_home = os.path.join(kdir, "claude")
        with open(os.path.join(cdir, "token")) as f:
            c_tok = f.read().strip()
        with open(os.path.join(kdir, "token")) as f:
            k_tok = f.read().strip()

        # distinct homes + distinct proxy tokens by construction
        self.assertNotEqual(c_home, k_home)
        self.assertNotEqual(c_tok, k_tok)

        # neither launch.sh carries the OTHER seat's token or home (on disk)
        with open(c_sh) as f:
            c_text = f.read()
        with open(k_sh) as f:
            k_text = f.read()
        self.assertNotIn(k_tok, c_text)
        self.assertNotIn(c_tok, k_text)
        self.assertNotIn(k_home, c_text)
        self.assertNotIn(c_home, k_text)

        # and at runtime: each child sees ONLY its own home + own token,
        # never the sibling's and never the polluted parent's
        c_seen, _ = self._exec_launch(c_sh)
        k_seen, _ = self._exec_launch(k_sh)
        self.assertEqual(c_seen["CFG"], c_home)
        self.assertEqual(k_seen["CFG"], k_home)
        self.assertEqual(c_seen["TOKEN"], c_tok)
        self.assertEqual(k_seen["TOKEN"], k_tok)
        self.assertNotEqual(c_seen["CFG"], k_seen["CFG"])
        self.assertNotEqual(c_seen["TOKEN"], k_seen["TOKEN"])
        self.assertNotIn(k_tok, (c_seen["TOKEN"], c_seen["CFG"]))
        self.assertNotIn(c_tok, (k_seen["TOKEN"], k_seen["CFG"]))

    def test_pool_two_different_accounts_one_email_stay_isolated(self):
        """The exact cross-account LEAK the dedup could cause: two DIFFERENT
        accounts that happen to share ONE email. If the fold keyed on email
        they would collapse into one record and one account's refresh token
        would silently shadow the other's. They must stay TWO records, each
        carrying ONLY its own token."""
        os.environ["HELM_CODEX_HOMES_DIR"] = os.path.join(self.tmp, "ch")
        os.makedirs(os.environ["HELM_CODEX_HOMES_DIR"])
        try:
            self._plant_pool("acct-personal", email="dave@shared.test",
                             account="acct-PERSONAL", refresh="rt-personal")
            self._plant_pool("acct-work", email="dave@shared.test",
                             account="acct-WORK", refresh="rt-work")
            codexhomes.codex_pool("acct-personal")
            codexhomes.codex_pool("acct-work")
            pooled = {r["account_id"]: r for r in codexhomes.codex_pooled()}
            # two accounts, not one folded row
            self.assertEqual(len(codexhomes.codex_pooled()), 2)
            self.assertIn("acct-PERSONAL", pooled)
            self.assertIn("acct-WORK", pooled)
            # each pool file carries ONLY its own refresh token — no crossover
            pd = codexhomes.pool_dir()
            with open(os.path.join(pd, "codex-acct-personal.json")) as f:
                personal = json.load(f)
            with open(os.path.join(pd, "codex-acct-work.json")) as f:
                work = json.load(f)
            self.assertEqual(personal["refresh_token"], "rt-personal")
            self.assertEqual(work["refresh_token"], "rt-work")
            self.assertNotIn("rt-work", json.dumps(personal))
            self.assertNotIn("rt-personal", json.dumps(work))
        finally:
            os.environ.pop("HELM_CODEX_HOMES_DIR", None)

    def _plant_pool(self, name, email, account, refresh):
        d = os.path.join(codexhomes.homes_root(), name)
        os.makedirs(d, exist_ok=True)
        exp = int(time.time()) + 3600
        auth = {"auth_mode": "chatgpt", "tokens": {
            "id_token": _jwt({"email": email, "https://api.openai.com/auth":
                              {"chatgpt_plan_type": "pro"}}),
            "access_token": _jwt({"exp": exp, "sub": "fake"}),
            "refresh_token": refresh,
            "account_id": account,
        }, "last_refresh": "2026-07-09T14:52:47.713051089Z"}
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump(auth, f)


class RebootChecklistTest(unittest.TestCase):
    """(C) the 1.4.149 reboot checklist is present and concretely actionable."""

    PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "REBOOT_CHECKLIST_1.4.149.md")

    def test_checklist_exists_and_names_exact_checks(self):
        self.assertTrue(os.path.exists(self.PATH), self.PATH)
        with open(self.PATH) as f:
            text = f.read()
        # each check the operator runs at the switch is present by command…
        for cmd in ("helm codex list", "helm seat launch codex",
                    "helm seat resume codex", "helm doctor",
                    "HELM_METAHARNESS=none"):
            self.assertIn(cmd, text, cmd)
        # …and every check states an EXPECTED result, not just a command
        self.assertIn("EXPECT", text)
        # the two owner concerns are each an explicit row
        self.assertIn("authoritative", text.lower())
        self.assertIn("isolation", text.lower())
        # the without-orca path is a first-class row (helm runs standalone)
        self.assertIn("without", text.lower())


if __name__ == "__main__":
    unittest.main()
