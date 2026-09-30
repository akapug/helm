#!/usr/bin/env python3
"""A seat's launch.sh can never silently run a different model than the
catalog (or the seat's explicit choice) says, and one verb re-mints it.

THE LIVE SHAPE THIS PINS (2026-09-28): the gemini seat's launch.sh, last
written three days earlier, carried `--model gemini-3.6-flash-high` and the
same subagent pin while the catalog, the proxy config and the seat's own
settings.json all said 3.8. Its spawn.json held no model at all. Started from
that script, the seat came up on a route the proxy had no credential for and
sat dead until the owner typed /model by hand. Nothing on any surface said
the script and the catalog disagreed.

Three things are measured here: `seat doctor` names such a seat as DRIFT;
`seat remint` rewrites the script (dry run first, `--apply` to write) without
starting or stopping anything; and a model spawn.json persisted as the family
DEFAULT follows the catalog, while an explicit choice keeps outranking it.

Hermetic: HELM_HOME, the chat dir and the codex-homes root are temp dirs; no
proxy is started and no claude is launched.
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
from unittest import mock

from tests._tmphome import pin_suite_guard
from helm import seat, seat_health

GEMINI_NOW = "gemini-3.8-flash-high"
GEMINI_OLD = "gemini-3.6-flash-high"
ASTRA = "gpt-6-astra"
SOL = "gpt-5.6-sol"


def _jwt(claims):
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    head = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=")
    return head.decode() + "." + seg.decode() + ".fake-sig"


def _drift(family, seat_name):
    from helm import seat_remint
    return seat_remint.launch_drift(family, seat_name)


def _snapshot(root, skip=None):
    """{relpath: bytes-or-link} for every file under root, `skip` excluded."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if skip and os.path.realpath(dirpath).startswith(
                os.path.realpath(skip)):
            dirnames[:] = []
            continue
        for name in filenames + [d for d in dirnames
                                 if os.path.islink(os.path.join(dirpath, d))]:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                out[rel] = "link:" + os.readlink(p)
            else:
                with open(p, "rb") as f:
                    out[rel] = f.read()
    return out


class SeatRemintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-remint-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "HELM_CHAT_DIR", "HELM_PROC",
                      "HELM_CHAT_NAME", "HELM_SEAT_STORAGE", "HELM_CHAT_ROOM",
                      "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM",
                      "MELD_CHAT_ROOM_SOURCE", "HELM_PROXY_BIN")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_CHAT_DIR"])
        for key in ("HELM_CHAT_NAME", "HELM_SEAT_STORAGE", "HELM_CHAT_ROOM",
                    "HELM_CHAT_ROOM_SOURCE", "MELD_CHAT_ROOM",
                    "MELD_CHAT_ROOM_SOURCE", "HELM_PROXY_BIN"):
            os.environ.pop(key, None)
        pin_suite_guard(self, self.tmp)
        self._codex_homes = seat.CODEX_HOMES
        seat.CODEX_HOMES = os.path.join(self.tmp, "codex-homes")
        os.makedirs(seat.CODEX_HOMES)
        self._hermes = seat.HERMES_AUTH
        seat.HERMES_AUTH = os.path.join(self.tmp, "hermes-auth.json")
        self._opencode = seat.OPENCODE_AUTHSTORE
        seat.OPENCODE_AUTHSTORE = os.path.join(self.tmp, "opencode-auth.json")
        self.timer = mock.patch.object(seat, "_ensure_autocompact_timer")
        self.timer.start()

    def tearDown(self):
        self.timer.stop()
        seat.CODEX_HOMES = self._codex_homes
        seat.HERMES_AUTH = self._hermes
        seat.OPENCODE_AUTHSTORE = self._opencode
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------
    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(args))
        return rc, out.getvalue(), err.getvalue()

    def _mint_gemini(self):
        """The gemini seat, minted from the catalog: a proxy-oauth family
        whose proxy holds an adopted credential."""
        donor = os.path.join(self.tmp, "donor")
        os.makedirs(donor)
        with open(os.path.join(
                donor, "antigravity-someone@example.com.json"), "w") as f:
            f.write('{"type":"antigravity","access_token":"ya29.test",'
                    '"refresh_token":"1//test","disabled":false,'
                    '"expired":"2099-01-01T00:00:00Z"}\n')
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(seat._add("gemini", ["--auth-from", donor]), 0)
        return seat.seat_dir("gemini")

    def _mint_codex(self):
        home = os.path.join(seat.CODEX_HOMES, "home-a")
        os.makedirs(home)
        auth = {"OPENAI_API_KEY": None, "auth_mode": "chatgpt",
                "tokens": {"id_token": _jwt({
                    "email": "fake@example.com",
                    "https://api.openai.com/auth":
                        {"chatgpt_plan_type": "pro"}}),
                    "access_token": _jwt({"exp": int(time.time()) + 3600,
                                          "sub": "fake"}),
                    "refresh_token": "fake-refresh-token",
                    "account_id": "acct-a"},
                "last_refresh": "2026-07-09T14:52:47.713051089Z"}
        with open(os.path.join(home, "auth.json"), "w") as f:
            json.dump(auth, f)
        # MINTED ON ASTRA, whatever the live catalog names: these arms read a
        # seat minted on one default and judged after the catalog moved to
        # another, so the mint pins its own starting model rather than
        # tracking the shipped codex default (gpt-6.1-sol today)
        with self._codex_catalog(ASTRA):
            rc, _out, err = self._run("add", "codex")
        self.assertEqual(rc, 0, err)
        return seat.seat_dir("codex")

    @staticmethod
    def _read(path):
        with open(path) as f:
            return f.read()

    def _age_launch(self, d, old, new):
        """Rewrite a minted launch.sh as an older mint left it: every
        occurrence of the current model carries the old one."""
        path = os.path.join(d, "launch.sh")
        text = self._read(path)
        self.assertIn("--model %s" % new, text)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=%s" % new, text)
        with open(path, "w") as f:
            f.write(text.replace(new, old))
        return path

    def _persist(self, d, seat_name, model, source=None):
        rec = {"v": 1, "seat": seat_name, "model": model}
        if source is not None:
            rec["model_source"] = source
        with open(os.path.join(d, "spawn.json"), "w") as f:
            json.dump(rec, f)

    def _doctor(self):
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

    def _codex_catalog(self, model):
        """The codex catalog after a default-model change (the lane moving
        every tier to one model is the live case)."""
        return mock.patch.dict(seat.FAMILIES["codex"], {"model": model})

    # -- 1. drift is measured ----------------------------------------------
    def test_the_gemini_shape_reads_DRIFT_and_remints_to_the_catalog(self):
        d = self._mint_gemini()
        self.assertEqual(_drift("gemini", "gemini")["state"], "CURRENT")
        launch = self._age_launch(d, GEMINI_OLD, GEMINI_NOW)
        self.assertFalse(os.path.exists(os.path.join(d, "spawn.json")))

        row = _drift("gemini", "gemini")
        self.assertEqual(row["state"], "DRIFT")
        fields = {name: (have, want) for name, have, want in row["fields"]}
        self.assertEqual(fields["model"], (GEMINI_OLD, GEMINI_NOW))
        self.assertEqual(fields["subagent"], (GEMINI_OLD, GEMINI_NOW))
        self.assertEqual(row["want_from"], "catalog")

        rc, text = self._doctor()
        self.assertEqual(rc, 1, text)
        drift = [ln for ln in text.splitlines() if "launch drift" in ln]
        self.assertEqual(len(drift), 1, text)
        self.assertIn("gemini", drift[0])
        self.assertIn("DRIFT", drift[0])
        self.assertIn(GEMINI_OLD, drift[0])
        self.assertIn(GEMINI_NOW, drift[0])
        self.assertIn("helm seat remint gemini --apply", drift[0])

        rc, out, err = self._run("remint", "gemini", "--apply")
        self.assertEqual(rc, 0, out + err)
        sh = self._read(launch)
        self.assertIn("--model %s" % GEMINI_NOW, sh)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=%s" % GEMINI_NOW, sh)
        self.assertNotIn(GEMINI_OLD, sh)
        self.assertEqual(_drift("gemini", "gemini")["state"], "CURRENT")
        rc, text = self._doctor()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("launch drift", text)

    # -- 2. re-mint without launching --------------------------------------
    def test_a_dry_run_names_the_change_and_writes_nothing(self):
        d = self._mint_gemini()
        self._age_launch(d, GEMINI_OLD, GEMINI_NOW)
        before = _snapshot(self.tmp)
        for args in (("remint", "gemini"), ("remint", "--all")):
            rc, out, err = self._run(*args)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("gemini", out)
            self.assertIn("DRIFT", out)
            self.assertIn("%s -> %s" % (GEMINI_OLD, GEMINI_NOW), out)
            self.assertIn("--apply", out)
        self.assertEqual(_snapshot(self.tmp), before)
        self.assertEqual(_drift("gemini", "gemini")["state"], "DRIFT")

    def test_apply_writes_launch_sh_and_nothing_outside_the_seat_dir(self):
        d = self._mint_gemini()
        launch = self._age_launch(d, GEMINI_OLD, GEMINI_NOW)
        outside = _snapshot(self.tmp, skip=d)
        aged = self._read(launch)
        with mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down, \
                mock.patch.object(subprocess, "Popen",
                                  side_effect=AssertionError(
                                      "remint started a process")):
            rc, out, err = self._run("remint", "--all", "--apply")
        self.assertEqual(rc, 0, out + err)
        up.assert_not_called()
        down.assert_not_called()
        self.assertNotEqual(self._read(launch), aged)
        self.assertEqual(_snapshot(self.tmp, skip=d), outside)
        # the second pass has nothing left to do
        rc, out, _err = self._run("remint", "gemini")
        self.assertEqual(rc, 0)
        self.assertNotIn("DRIFT", out)
        self.assertIn("CURRENT", out)

    # -- 3. a default is not a choice --------------------------------------
    def test_an_explicit_persisted_choice_survives_a_catalog_change(self):
        d = self._mint_codex()
        self._persist(d, "codex", ASTRA, "explicit")
        with self._codex_catalog(SOL):
            self.assertEqual(_drift("codex", "codex")["state"], "CURRENT")
            rc, out, err = self._run("remint", "codex", "--apply")
            self.assertEqual(rc, 0, out + err)
        sh = self._read(os.path.join(d, "launch.sh"))
        self.assertIn("--model %s" % ASTRA, sh)
        self.assertNotIn("--model %s" % SOL, sh)

    def test_a_persisted_default_follows_the_catalog(self):
        d = self._mint_codex()
        self._persist(d, "codex", ASTRA, "default")
        self.assertIsNone(seat._persisted_model(d, "codex"))
        with self._codex_catalog(SOL):
            row = _drift("codex", "codex")
            self.assertEqual(row["state"], "DRIFT")
            fields = {n: (h, w) for n, h, w in row["fields"]}
            self.assertEqual(fields["model"], (ASTRA, SOL))
            rc, out, err = self._run("remint", "codex", "--apply")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(_drift("codex", "codex")["state"], "CURRENT")
        sh = self._read(os.path.join(d, "launch.sh"))
        self.assertIn("--model %s" % SOL, sh)
        self.assertNotIn("--model %s" % ASTRA, sh)

    def test_a_legacy_record_is_kept_and_reported_source_unknown(self):
        """spawn.json written before the source marker: helm cannot tell a
        choice from a default it copied, so the model is KEPT and the row
        says so until the operator settles it one way or the other."""
        d = self._mint_codex()
        self._persist(d, "codex", ASTRA)
        self.assertEqual(seat._persisted_model(d, "codex"), ASTRA)
        launch = os.path.join(d, "launch.sh")
        with self._codex_catalog(SOL):
            row = _drift("codex", "codex")
            self.assertEqual(row["state"], "DRIFT")
            self.assertEqual(row["want_from"], "persisted, source unknown")
            rc, text = self._doctor()
            self.assertEqual(rc, 1, text)
            self.assertIn("persisted, source unknown", text)
            # a plain re-mint keeps the persisted model
            rc, out, err = self._run("remint", "codex", "--apply")
            self.assertIn("persisted, source unknown", out)
            self.assertIn("--model %s" % ASTRA, self._read(launch))
            # the operator's decision: follow the catalog
            rc, out, err = self._run("remint", "codex", "--follow-catalog")
            self.assertEqual(rc, 0, out + err)
            with open(os.path.join(d, "spawn.json")) as f:
                self.assertNotIn("model_source", json.load(f))
            rc, out, err = self._run("remint", "codex", "--follow-catalog",
                                     "--apply")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("--model %s" % SOL, self._read(launch))
            with open(os.path.join(d, "spawn.json")) as f:
                self.assertEqual(json.load(f)["model_source"], "default")
            self.assertEqual(_drift("codex", "codex")["state"], "CURRENT")

    def test_keep_persisted_records_the_legacy_model_as_a_choice(self):
        d = self._mint_codex()
        self._persist(d, "codex", ASTRA)
        with self._codex_catalog(SOL):
            rc, out, err = self._run("remint", "codex", "--keep-persisted",
                                     "--apply")
            self.assertEqual(rc, 0, out + err)
            with open(os.path.join(d, "spawn.json")) as f:
                self.assertEqual(json.load(f)["model_source"], "explicit")
            self.assertEqual(_drift("codex", "codex")["state"], "CURRENT")
        self.assertIn("--model %s" % ASTRA,
                      self._read(os.path.join(d, "launch.sh")))

    # -- 4. an unread record is never a verdict ----------------------------
    def test_an_unreadable_spawn_json_reads_UNKNOWN_and_remint_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the no-write snapshot and the absent cure text are the finding's observables, beside unconditional positives per shape: state UNKNOWN, the why naming spawn.json, rc 1 from both remint forms; the CONTROL reads the same seat CURRENT with the record readable
        """An explicit choice helm cannot READ is not an absent choice. The
        lenient read answered None for a truncated or non-object spawn.json,
        so the row read DRIFT from the catalog, the doctor recommended
        `remint --apply`, and the apply re-minted the seat off its chosen
        model — the overstated-window wedge `_persisted_model` exists to
        prevent, triggered by a record nobody could read."""
        d = self._mint_codex()
        launch = os.path.join(d, "launch.sh")
        self._persist(d, "codex", ASTRA, "explicit")
        with self._codex_catalog(SOL):
            # CONTROL: the readable record keeps the choice, and reads CURRENT
            self.assertEqual(_drift("codex", "codex")["state"], "CURRENT")
            for shape in ('{"v": 1, "seat": "codex", "model": "gpt-6-a',
                          "[1, 2]"):
                with open(os.path.join(d, "spawn.json"), "w") as f:
                    f.write(shape)
                row = _drift("codex", "codex")
                self.assertEqual(row["state"], "UNKNOWN", shape)
                self.assertIn("spawn.json unreadable", row["why"])
                rc, text = self._doctor()
                self.assertEqual(rc, 0, text)
                drift = [ln for ln in text.splitlines()
                         if "launch drift" in ln]
                self.assertEqual(len(drift), 1, text)
                self.assertIn("UNKNOWN", drift[0])
                self.assertIn("spawn.json unreadable", drift[0])
                self.assertNotIn("remint codex --apply", text)
                before = _snapshot(self.tmp)
                for args in (("remint", "codex", "--apply"),
                             ("remint", "--all", "--apply")):
                    rc, out, err = self._run(*args)
                    self.assertEqual(rc, 1, out + err)
                    self.assertIn("UNKNOWN", out)
                    self.assertIn("nothing written", out)
                self.assertEqual(_snapshot(self.tmp), before, shape)
                self.assertIn("--model %s" % ASTRA, self._read(launch))

    def test_a_spawn_minted_room_seat_reads_CURRENT(self):
        """A fresh mint compares equal to itself. The proxy spawn stamps
        `HELM_CHAT_ROOM_SOURCE=explicit` beside an explicit --room, and the
        child reads that exactly as it reads no stamp at all
        (`seats_identity.resolve_homing`); the fresh mint the drift row
        compares against spells the same room without it. Read as spelled,
        every seat spawned with a room was DRIFT the moment its own spawn
        returned, and the doctor failed on it."""
        d = self._mint_codex()
        home = os.path.join(self.tmp, "seat-home")
        os.makedirs(home)
        popen = mock.Mock(return_value=mock.Mock(pid=4242))
        from helm import harness
        with mock.patch.object(harness, "detect", return_value=None), \
                mock.patch.object(seat, "_seat_home_cwd", return_value=home), \
                mock.patch.object(seat.subprocess, "Popen", popen), \
                mock.patch.object(seat, "_pid_identity",
                                  return_value="test-start"), \
                mock.patch.object(seat, "_up", return_value=0):
            rc, out, err = self._run("spawn", "codex", "--room", "team-z")
        self.assertEqual(rc, 0, out + err)
        popen.assert_called_once()
        sh = self._read(os.path.join(d, "launch.sh"))
        self.assertIn("HELM_CHAT_ROOM=team-z", sh)
        self.assertIn("HELM_CHAT_ROOM_SOURCE=explicit", sh)
        row = _drift("codex", "codex")
        self.assertEqual(row["state"], "CURRENT", row["fields"])
        rc, text = self._doctor()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("launch drift", text)

    def test_apply_refuses_while_a_spawn_is_in_flight(self):
        """A spawn releases the lifecycle lock while its child starts, and
        only its PENDING attempt keeps other writers out. Every mutating verb
        asks `_refuse_in_flight_spawn` before its first write; a re-mint that
        did not would rewrite launch.sh under the child being brought up.
        CONTROL: the same record with its attempt COMPLETE re-mints."""
        d = self._mint_codex()
        launch = os.path.join(d, "launch.sh")
        parent = os.getppid()
        live = dict(seat._new_spawn_attempt(), pid=parent,
                    pid_identity=seat._pid_identity(parent))
        self.assertIsNotNone(live["pid_identity"])

        def pending(attempt):
            with open(os.path.join(d, "spawn.json"), "w") as f:
                json.dump({"v": 1, "seat": "codex", "family": "codex",
                           "harness": "headless", "session": None,
                           "attempt": attempt}, f)

        with self._codex_catalog(SOL):
            pending(live)
            self.assertEqual(_drift("codex", "codex")["state"], "DRIFT")
            before = _snapshot(self.tmp)
            rc, out, err = self._run("remint", "codex", "--apply")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("is IN FLIGHT", err)
            self.assertIn(live["id"], err)
            # the door is asked under the lock, so the lock file is the one
            # thing the refusal may leave; every seat byte is untouched
            after = _snapshot(self.tmp)
            lock = os.path.relpath(os.path.join(d, ".spawn.lock"), self.tmp)
            self.assertEqual(after.pop(lock, b""), b"")
            self.assertEqual(after, before)
            # CONTROL: the settled attempt, same fixture, re-mints
            pending(dict(live, state=seat.SPAWN_ATTEMPT_COMPLETE))
            rc, out, err = self._run("remint", "codex", "--apply")
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("IN FLIGHT", err)
        self.assertIn("--model %s" % SOL, self._read(launch))

    def test_the_two_decisions_refuse_each_other(self):
        rc, _out, err = self._run("remint", "codex", "--follow-catalog",
                                  "--keep-persisted")
        self.assertEqual(rc, 2)
        self.assertIn("--follow-catalog", err)


if __name__ == "__main__":
    unittest.main()
