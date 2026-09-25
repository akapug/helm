#!/usr/bin/env python3
"""helm launch — the metaharness seam, including its shared owner handoff.
Arg split, env build, seat derivation, roster pre-write, and exact child handoff
are pure and pinned here. Hermetic per the seats pattern."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import launch, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_ROOM",
            "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE", "DREGG_PROFILE",
            # the home a launch syncs and reads its model from: never the
            # runner's own credhome
            "CLAUDE_CONFIG_DIR", "ORCA_USER_DATA_PATH",
            "HELM_CREDHOME_ORCA_SYNC",
            # the skills hub a --home launch links: never the host's own
            "HELM_SKILLS_CANONICAL", "MELD_SKILLS_CANONICAL")


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-launch-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["ORCA_USER_DATA_PATH"] = os.path.join(self.tmp, "orca")
        from helm import homes
        roots = mock.patch.dict(homes.ROOTS, {"claude": os.path.join(self.tmp, "claude-homes")})
        defaults = mock.patch.dict(homes.DEFAULTS, {"claude": os.path.join(self.tmp, "default-claude")})
        for p in (roots, defaults):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_splits_flags_from_claude_args(self):
        opts, rest = launch.parse_args(
            ["--seat", "alice", "--room", "dev", "--", "-p", "do the thing"])
        self.assertEqual(opts["seat"], "alice")
        self.assertEqual(opts["room"], "dev")
        self.assertEqual(rest, ["-p", "do the thing"])
        # first unknown token starts the pass-through, no `--` needed
        opts, rest = launch.parse_args(["--seat", "z", "-p", "hi", "--seat", "x"])
        self.assertEqual(opts["seat"], "z")
        self.assertEqual(rest, ["-p", "hi", "--seat", "x"])
        opts, rest = launch.parse_args(["--no-install"])
        self.assertFalse(opts["install"])
        self.assertEqual(rest, [])

    def test_build_env_sets_room_and_truthful_provenance(self):
        env = launch.build_env({"PATH": "/bin"}, "alice")
        self.assertEqual(env["HELM_CHAT_NAME"], "alice")
        self.assertEqual(env["HELM_AGENT_HARNESS"], "claude")
        self.assertEqual(env["HELM_MODEL_FAMILY"], "claude")
        self.assertEqual(env["HELM_MODEL_BACKEND"], "native")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("HELM_CHAT_ROOM", env)
        env = launch.build_env({}, "alice", "/homes/h1", "team-a", "derived")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/homes/h1")
        self.assertEqual(env["HELM_CHAT_ROOM"], "team-a")
        self.assertEqual(env["HELM_CHAT_ROOM_SOURCE"], "derived")
        # Explicit main is meaningful: it prevents project derivation and homes
        # the child to main-only. It must clear a stale inherited derived marker.
        env = launch.build_env(
            {"HELM_CHAT_ROOM_SOURCE": "derived",
             "MELD_CHAT_ROOM": "stale", "MELD_CHAT_ROOM_SOURCE": "derived"},
            "alice", room="main")
        self.assertEqual(env["HELM_CHAT_ROOM"], "main")
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE", env)
        self.assertNotIn("MELD_CHAT_ROOM", env)
        self.assertNotIn("MELD_CHAT_ROOM_SOURCE", env)

    def test_build_env_aligns_signer_identity_with_chat_identity(self):
        """A native launch used to set only HELM_CHAT_NAME. A clean parent made
        the child unsigned; a signed parent made it speak as alice while signing
        as the parent. The launch owner emits one aligned identity tuple."""
        from helm import seat
        env = launch.build_env({}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], seat.DREGG_SIGNER_DEFAULT)
        self.assertEqual(env["HELM_CELL_PROFILE"], "alice")
        self.assertEqual(env["DREGG_PROFILE"], "alice")

        env = launch.build_env(
            {"HELM_CELL_BIN": "/custom/signer",
             "HELM_CELL_PROFILE": "launcher",
             "DREGG_PROFILE": "launcher"}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "/custom/signer")
        self.assertEqual(env["HELM_CELL_PROFILE"], "alice")
        self.assertEqual(env["DREGG_PROFILE"], "alice")

    def test_build_env_NEVER_names_the_seat_to_GIT(self):
        """GIT IS THE ONE PLANE A CHILD DOES NOT SPEAK ITS SEAT ON, and it is
        the exception to the alignment asserted above.

        Every other identity here is internal. Git's author name is PUBLISHED
        the moment anything is pushed, and setting it to the seat rendered
        every commit on GitHub as e.g. `helm-claude-2` — a machine authorship
        note on a plane no commit-message rung can read, which is exactly what
        the owner ruled must never leave this estate.

        ASSERTED AS ABSENT FROM THE ENV, not merely different from the seat: a
        value of any kind here is a name that reaches GitHub, and `setdefault`
        made an inherited one silently win. Nothing set means git falls back to
        the operator's own configured identity.
        """
        env = launch.build_env({}, "alice")
        # the unconditional positive control on the same call: build_env really
        # did run and really did populate this env, so the absences below are
        # facts about git's keys and not about an empty dict.
        self.assertEqual(env["HELM_CELL_PROFILE"], "alice")
        for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            self.assertNotIn(key, env,
                             "%s reaches git, and a seat name there is "
                             "published on every commit page" % key)

    def test_build_env_routes_feedback_to_helm_on_the_native_door(self):
        """task/2328: a native claude seat gets the same pair the proxy
        families' launch line exports (seat.FEEDBACK_ENV) — a law about where
        a seat's feedback GOES, not about a backend. MEASURED on CC 2.1.269:
        either variable alone removes the SendFeedback drafts tool and both
        slash commands. Set, not setdefault: an inherited opposite is not an
        operator choice the child honours."""
        from helm import seat
        env = launch.build_env({"PATH": "/bin"}, "alice")
        self.assertEqual(env["DISABLE_FEEDBACK_COMMAND"], "1")
        self.assertEqual(env["DISABLE_BUG_COMMAND"], "1")
        self.assertEqual(env["HELM_MODEL_BACKEND"], "native")     # same env, positive
        env = launch.build_env({"DISABLE_FEEDBACK_COMMAND": "", "DISABLE_BUG_COMMAND": "0"},
                               "alice")
        self.assertEqual((env["DISABLE_FEEDBACK_COMMAND"], env["DISABLE_BUG_COMMAND"]),
                         ("1", "1"))
        # negative controls on the same env: the settings-file switch is not
        # an env var, and the door still exports no proxy triple
        self.assertNotIn("feedbackDrafts", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(dict(seat.FEEDBACK_ENV),
                         {"DISABLE_FEEDBACK_COMMAND": "1", "DISABLE_BUG_COMMAND": "1"})

    def test_build_env_preserves_explicit_signer_kill_switches(self):
        env = launch.build_env({"HELM_CELL_BIN": ""}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "")
        env = launch.build_env({"MELD_CELL_BIN": ""}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "")
        env = launch.build_env({"MELD_CELL_BIN": "/legacy/signer"}, "alice")
        self.assertEqual(env["HELM_CELL_BIN"], "/legacy/signer")

    def test_launch_REFUSES_when_the_write_is_shortened(self):  # noqa: VACUOUS_ASSERTION — positive controls are the assert_called_once pair on the resolvable path; mutation-proven (dropping `return 1` fails this arm on join.assert_not_called)
        """THE LAUNCH DOOR, asserted on BEHAVIOUR rather than on text.

        The previous version of this arm DISCARDED the return code and asserted
        that a warning printed. That is why its mutation looked live: the
        mutation proved the warning prints, never that the door refuses — and
        the door did not refuse. It warned, then joined the roster and exec'd
        claude anyway, so a session ran without a required guard with the
        warning already scrolled past.

        What must be true: NONZERO rc, NO join, NO exec."""
        import contextlib
        import io
        import json
        from helm import configs, hooks
        home = os.path.join(self.tmp, "a-home")
        os.makedirs(home)
        # configs' gated write requires a registered root, else the install is
        # REFUSED and `action == "fail"` shadows the state under test
        prior_roots = configs.HOME_ROOTS
        configs.HOME_ROOTS = list(prior_roots) + [home]
        self.addCleanup(setattr, configs, "HOME_ROOTS", prior_roots)
        guard = os.path.join(self.tmp, "fab-suite-pretooluse")
        with open(guard, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(guard, 0o755)

        def _launch():
            """-> (rc, stderr, join_mock, exec_mock) — the rc is RETURNED, not
            dropped, because it is the whole property under test."""
            err = io.StringIO()
            with mock.patch.object(hooks, "claude_homes",
                                   return_value=[("a-home", home)]), \
                    mock.patch.object(launch.seats, "join") as join, \
                    mock.patch.object(launch.seat_launch_owner,
                                      "exec_attached") as ex, \
                    contextlib.redirect_stderr(err):
                rc = launch.cmd_launch([])
            return rc, err.getvalue(), join, ex

        # MUST-HIT: with the guard resolvable the door PROCEEDS — it joins and
        # execs — so the refusal below is the shortened state and not a door
        # that refuses unconditionally.
        os.environ["HELM_SUITE_GUARD"] = guard
        # a LAMBDA, not a bare method reference: the env-hygiene scanner
        # reads restores syntactically and cannot see the reference form
        self.addCleanup(lambda: os.environ.pop("HELM_SUITE_GUARD", None))
        rc, clean, join, ex = _launch()
        self.assertNotIn("REFUSED", clean)
        join.assert_called_once()
        ex.assert_called_once()
        with open(os.path.join(home, "settings.json")) as f:
            written = " ".join(hooks._all_hook_cmds(json.load(f)))
        self.assertIn("fab-suite-pretooluse", written)

        os.environ["HELM_SUITE_GUARD"] = os.path.join(self.tmp, "long-gone")
        rc, out, join, ex = _launch()
        self.assertNotEqual(rc, 0, "a shortened install still launched")
        self.assertIn("REFUSED", out)
        self.assertIn("suite-guard", out)
        # THE EFFECT: no session exists. A door that printed REFUSED and then
        # joined and exec'd anyway would satisfy any text-only assertion while
        # leaving exactly the unguarded session this refuses to start.
        # noqa: VACUOUS_ASSERTION — the unconditional positive controls are
        # the assert_called_once pair on the resolvable path above. Proven
        # live by mutation: dropping `return 1` in launch.py fails THIS test
        # on `Expected join to not have been called`, with the warning text
        # still printing — which is the whole point of asserting behaviour.
        join.assert_not_called()
        ex.assert_not_called()

    def test_cmd_launch_exports_derived_project_room(self):
        with mock.patch.object(launch.seats, "derive_home_room",
                               return_value="helm"), \
                mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.seat_launch_owner, "exec_attached") as execvpe:
            launch.cmd_launch(["--no-install"])
        join.assert_called_once_with(
            cwd=os.getcwd(), seat=launch.stable_seat(), room="helm",
            room_explicit=False, room_source="derived",
            runtime={"agent_harness": "claude", "family": "claude",
                     "backend": "native"})
        env = execvpe.call_args.args[1]
        self.assertEqual(env["HELM_CHAT_ROOM"], "helm")
        self.assertEqual(env["HELM_CHAT_ROOM_SOURCE"], "derived")

    def _hub_and_home(self, name):
        """A canonical hub (HELM_SKILLS_CANONICAL, the host's own knob) and one
        bare credhome under the patched claude root, the shape a fresh
        `helm homes prepare` left before this lane. -> (hub, home_path)"""
        hub = os.path.join(self.tmp, "skills-hub")
        os.makedirs(os.path.join(hub, "learn"))
        with open(os.path.join(hub, "learn", "SKILL.md"), "w") as f:
            f.write("# learn")
        os.environ["HELM_SKILLS_CANONICAL"] = hub
        # the Orca sync is a separate door with its own arms; off, and said so
        os.environ["HELM_CREDHOME_ORCA_SYNC"] = "0"
        from helm import homes
        home = os.path.join(homes.ROOTS["claude"], name)
        os.makedirs(home)
        return hub, home

    def _launch_home(self, name):
        """-> (rc, stderr, exec_mock) for `helm launch --no-install --home NAME`."""
        err = io.StringIO()
        with mock.patch.object(launch.seats, "join"), \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  return_value=0) as ex, \
                contextlib.redirect_stderr(err):
            rc = launch.cmd_launch(["--no-install", "--seat", "s1", "--home", name])
        return rc, err.getvalue(), ex

    def test_launch_home_links_the_skills_hub_before_the_exec(self):
        """A --home launch onto a credhome with no skills entry creates
        `skills -> hub` and says so, then execs; the session it starts sees
        the hub's skills instead of none. Second launch: the link is left
        alone and the line is not repeated (idempotent, quiet)."""
        hub, home = self._hub_and_home("bare-home")
        link = os.path.join(home, "skills")
        self.assertFalse(os.path.lexists(link))          # the pre-state IS the bug
        rc, err, ex = self._launch_home("bare-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertTrue(os.path.islink(link), err)
        self.assertEqual(os.readlink(link), hub)
        self.assertIn("skills linked: bare-home/skills -> " + hub, err)
        self.assertTrue(os.path.isfile(os.path.join(link, "learn", "SKILL.md")))
        rc, err, ex = self._launch_home("bare-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertEqual(os.readlink(link), hub)
        self.assertNotIn("skills linked", err)

    def test_launch_home_refuses_to_overwrite_a_foreign_skills_link_and_says_so(self):
        """A skills link that points ELSEWHERE is named on stderr with the
        normalizer, and left exactly as found — launch never rewires a home
        it did not make. The launch still proceeds: the operator is told what
        the session will and will not see."""
        hub, home = self._hub_and_home("foreign-home")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        link = os.path.join(home, "skills")
        os.symlink(elsewhere, link)
        rc, err, ex = self._launch_home("foreign-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertEqual(os.readlink(link), elsewhere)
        self.assertIn("foreign-home/skills -> %s is NOT the skills hub" % elsewhere, err)
        self.assertIn("left untouched", err)
        self.assertIn("helm skills sync --apply", err)
        self.assertNotIn("skills linked", err)

    def test_launch_home_says_the_hub_is_unavailable_and_still_launches(self):
        """A configured hub that is MISSING is said on stderr with its path —
        never the silence of the unconfigured case — and the session still
        launches, told it sees no skills; nothing is linked to a missing dir."""
        hub, home = self._hub_and_home("orphan-home")
        import shutil as sh
        sh.rmtree(hub)
        rc, err, ex = self._launch_home("orphan-home")
        self.assertEqual(rc, 0, err)
        ex.assert_called_once()
        self.assertFalse(os.path.lexists(os.path.join(home, "skills")))
        self.assertIn("skills hub UNAVAILABLE for orphan-home", err)
        self.assertIn(hub + " is configured but MISSING", err)
        self.assertIn("NO helm skills", err)

    def test_an_installing_launch_gives_the_home_it_wires_ultracode_and_opus_xhigh(self):  # noqa: VACUOUS_ASSERTION — the absent settings file on the --no-install control is paired with assertIs/assertEqual on the same file's keys after the installing launch, unconditionally, in this arm
        """ARM (a), the launch door: `helm launch --seat S --home H` wires H
        before the exec, and a Claude home it wires comes out with the two
        keys, through homes.BENEFITS (the one writer). CONTROL: the same launch
        with --no-install, which writes nothing into a home, leaves a second
        bare home without a settings file, so the keys above are this door's
        work and not the fixture's."""
        import json
        from helm import hooks
        _hub, home = self._hub_and_home("wired-home")
        err = io.StringIO()
        with mock.patch.object(launch.hooks, "install_home",
                               return_value=hooks.InstallResult(
                                   "ok", "hook up to date")), \
                mock.patch.object(launch.seats, "join"), \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  return_value=0) as ex, \
                contextlib.redirect_stderr(err):
            rc = launch.cmd_launch(["--seat", "s1", "--home", "wired-home"])
        self.assertEqual(rc, 0, err.getvalue())
        ex.assert_called_once()
        with open(os.path.join(home, "settings.json")) as fh:
            got = json.load(fh)
        self.assertIs(got.get("ultracode"), True, got)
        self.assertEqual(got["modelSettings"]["claude-opus-5-5"]["effortLevel"],
                         "xhigh")
        self.assertIn("settings defaults set", err.getvalue())
        other = os.path.join(os.path.dirname(home), "unwired-home")
        os.makedirs(other)
        rc, out, ex = self._launch_home("unwired-home")
        self.assertEqual(rc, 0, out)
        ex.assert_called_once()
        self.assertFalse(os.path.exists(os.path.join(other, "settings.json")))

    def test_legacy_room_alias_is_inherited_and_canonicalized(self):
        os.environ["MELD_CHAT_ROOM"] = "legacy-project"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.seat_launch_owner, "exec_attached") as execvpe:
            launch.cmd_launch(["--no-install"])
        join.assert_called_once_with(
            cwd=os.getcwd(), seat=launch.stable_seat(), room="legacy-project",
            room_explicit=False, room_source="derived",
            runtime={"agent_harness": "claude", "family": "claude",
                     "backend": "native"})
        env = execvpe.call_args.args[1]
        self.assertEqual(env["HELM_CHAT_ROOM"], "legacy-project")
        self.assertEqual(env["HELM_CHAT_ROOM_SOURCE"], "derived")
        self.assertNotIn("MELD_CHAT_ROOM", env)
        self.assertNotIn("MELD_CHAT_ROOM_SOURCE", env)

    def test_preferred_room_ignores_legacy_source_marker(self):
        os.environ["HELM_CHAT_ROOM"] = "explicit-new"
        os.environ["MELD_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.seat_launch_owner, "exec_attached") as execvpe:
            launch.cmd_launch(["--no-install"])
        self.assertEqual(join.call_args.kwargs["room"], "explicit-new")
        self.assertTrue(join.call_args.kwargs["room_explicit"])
        self.assertIsNone(join.call_args.kwargs["room_source"])
        env = execvpe.call_args.args[1]
        self.assertEqual(env["HELM_CHAT_ROOM"], "explicit-new")
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE", env)

    def test_cli_room_beats_derived_environment_marker(self):
        os.environ["HELM_CHAT_ROOM"] = "project-a"
        os.environ["HELM_CHAT_ROOM_SOURCE"] = "derived"
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.seat_launch_owner, "exec_attached") as execvpe:
            launch.cmd_launch(["--no-install", "--room", "main"])
        self.assertEqual(join.call_args.kwargs["room"], "main")
        self.assertTrue(join.call_args.kwargs["room_explicit"])
        self.assertIsNone(join.call_args.kwargs["room_source"])
        env = execvpe.call_args.args[1]
        self.assertEqual(env["HELM_CHAT_ROOM"], "main")
        self.assertNotIn("HELM_CHAT_ROOM_SOURCE", env)

    def test_build_env_strips_child_stamp(self):
        """child-stamp-kills-seat-persistence: `helm launch` run from inside a
        Claude session inherits the child stamp; the exec'd claude must start
        top-level or its transcript persistence is silently off."""
        from helm import seat
        base = {"PATH": "/bin"}
        base.update({v: "leaked" for v in seat.CHILD_STAMP_VARS})
        env = launch.build_env(base, "alice")
        for v in seat.CHILD_STAMP_VARS:
            self.assertNotIn(v, env)
        self.assertEqual(env["PATH"], "/bin")  # everything else rides through

    def test_build_env_strips_the_proxy_triple(self):
        """#107. Every non-claude family here runs behind a local proxy but
        INSIDE Claude Code's harness, so a proxied seat's shell carries
        ANTHROPIC_BASE_URL/AUTH_TOKEN/API_KEY. `helm launch` execs a seat that
        registers itself family=claude backend=NATIVE — inheriting those aims
        a native seat at a proxy fronting another vendor. It would look
        native, be billed native, and route elsewhere, and nothing downstream
        reports it.

        Values here are synthetic placeholders; only the NAMES are real."""
        from helm import seat
        base = {"PATH": "/bin", "HOME": "/tmp/synthetic-home"}
        base.update({v: "synthetic-placeholder" for v in seat.SCRUB_VARS})
        env = launch.build_env(base, "alice")
        # THE KEY SET, not "a wanted key is somewhere in it" — asserting
        # membership is how a half-scrubbed env passes.
        self.assertEqual(sorted(k for k in env if k in seat.SCRUB_VARS), [])
        self.assertEqual(env["PATH"], "/bin")       # everything else rides
        self.assertEqual(env["HOME"], "/tmp/synthetic-home")

    def test_build_env_is_unchanged_from_a_clean_shell(self):
        """LIFECYCLE, launch #2: nothing to strip is a NO-OP, never a mangled
        env. The positive control for the arm above — without it, a build_env
        that dropped everything would satisfy the emptiness there."""
        from helm import seat
        base = {"PATH": "/bin", "HOME": "/tmp/synthetic-home", "TERM": "xterm"}
        env = launch.build_env(base, "alice")
        # unconditional positives on the SAME env the absence is about
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["TERM"], "xterm")
        for k, v in base.items():
            self.assertEqual(env[k], v)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)   # absent, and stays absent

    def test_the_scrub_does_not_depend_on_the_proxy_being_up(self):
        """LIFECYCLE, launch #3: a seat whose proxy is DOWN still carries the
        variables — they are environment, not liveness. A scrub gated on
        reachability would pass every test on a healthy box and leak on the
        one day the proxy is dead, which is also the day someone relaunches."""
        from helm import seat
        base = {"PATH": "/bin"}
        # a base URL naming a port nothing is listening on: still inherited
        base.update({v: "http://127.0.0.1:1" for v in seat.SCRUB_VARS})
        env = launch.build_env(base, "alice")
        self.assertEqual(env["PATH"], "/bin")      # same env, positive
        self.assertEqual(sorted(k for k in env if k in seat.SCRUB_VARS), [])

    def test_build_env_does_not_mutate_the_caller_environment(self):  # noqa: VACUOUS_ASSERTION — the contract IS non-mutation, so the decisive assertion compares base to a pre-call snapshot and no positive control on that comparison can exist. Three unconditional positives on the SAME call already run first: the returned env is scrubbed, it still carries PATH, and base still carries every SCRUB_VAR.
        """`build_env(os.environ, ...)` is the real call. If the scrub edited
        in place it would strip the LAUNCHER's own proxy config as a side
        effect — breaking the very seat that ran the command."""
        from helm import seat
        base = {"PATH": "/bin"}
        base.update({v: "synthetic-placeholder" for v in seat.SCRUB_VARS})
        before = dict(base)
        env = launch.build_env(base, "alice")
        # the same call's OTHER channel: it really produced a scrubbed env,
        # so `base` being untouched is a copy and not a call that did nothing
        self.assertEqual(sorted(k for k in env if k in seat.SCRUB_VARS), [])
        self.assertEqual(env["PATH"], "/bin")
        # THE PROPERTY THAT MATTERS, stated positively: the caller STILL
        # CARRIES the proxy config. "base is unchanged" is the weaker way to
        # say it and reads as an absence; what breaks if this regresses is a
        # launching seat losing the vars its own proxy needs.
        self.assertEqual(sorted(k for k in base if k in seat.SCRUB_VARS),
                         sorted(seat.SCRUB_VARS))
        self.assertEqual(base, before)

    def test_cmd_launch_routes_the_exact_child_through_one_owner(self):
        with mock.patch.object(launch.seats, "join"), \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  return_value=23) as owner:
            rc = launch.cmd_launch(
                ["--no-install", "--seat", "alice", "--", "-p", "probe"])
        self.assertEqual(rc, 23)
        argv, env = owner.call_args.args
        self.assertEqual(argv, ["claude", "-p", "probe"])
        self.assertEqual(env["HELM_CHAT_NAME"], "alice")
        self.assertEqual(env["HELM_CELL_PROFILE"], "alice")

    def test_the_launch_carries_the_attempt_it_was_handed_and_mints_none(self):  # noqa: VACUOUS_ASSERTION — the two assertNotIn are the "never inherited, never minted" law and each sits beside an unconditional positive from the same producer: the exact pinned token in build_env's answer and in the env the owner receives, plus the stderr line naming it
        """task/2440 round seven — `helm launch` is NOT a second producer of a
        spawn's identity. A spawn threads its ATTEMPT TOKEN into the launch line
        (`HELM_SPAWN_ATTEMPT`); `cmd_launch` reads it through the one reader
        (`spawn_attempt_token`) and PINS it into claude's env, so the child's
        first SessionStart carries exactly the attempt it is the child of. A
        launch handed none writes none — even when the shell it runs in carries
        a token from some other spawn — and mints none.

        Both halves through the shipped producers: `build_env` with and without
        a token over a base that CARRIES a stale one, and `cmd_launch` with and
        without the variable in its own environment, reading the env handed to
        the exec owner. CONTROL: the stale value in the base is a third value the
        child must never come back with. Blast radius: `build_env`'s
        `attempt_token` and the read in `cmd_launch`; before them the child
        inherited whatever the shell had.
        """
        from helm.seat_role import SPAWN_ATTEMPT_ENV
        base = {"PATH": "/bin", SPAWN_ATTEMPT_ENV: "stale-from-the-shell"}
        env = launch.build_env(base, "alice", allocate_scratch=False)
        self.assertNotIn(SPAWN_ATTEMPT_ENV, env)
        env = launch.build_env(base, "alice", allocate_scratch=False,
                               attempt_token="attempt-1")
        self.assertEqual(env[SPAWN_ATTEMPT_ENV], "attempt-1")
        for carried in ("attempt-2", None):
            with self.subTest(carried=carried):
                err = io.StringIO()
                with mock.patch.dict(os.environ, {}, clear=False), \
                        mock.patch.object(launch.seats, "join"), \
                        mock.patch.object(launch.seat_launch_owner,
                                          "exec_attached",
                                          return_value=0) as owner, \
                        contextlib.redirect_stderr(err):
                    if carried:
                        os.environ[SPAWN_ATTEMPT_ENV] = carried
                    else:
                        os.environ.pop(SPAWN_ATTEMPT_ENV, None)
                    rc = launch.cmd_launch(["--no-install", "--seat", "alice"])
                self.assertEqual(rc, 0)
                _argv, child = owner.call_args.args
                if carried:
                    self.assertEqual(child[SPAWN_ATTEMPT_ENV], carried)
                    self.assertIn("[helm launch] spawn attempt attempt-2",
                                  err.getvalue())
                else:
                    self.assertNotIn(SPAWN_ATTEMPT_ENV, child)
                    self.assertNotIn("spawn attempt", err.getvalue())

    def test_cmd_launch_reads_the_attempt_token_through_the_seat_facade(self):
        """task/2440 round eight — `helm launch` reaches the one reader THROUGH
        `helm.seat`, the seat family's single door, and not past it into
        `seat_lifecycle_runtime`.

        `tests/test_seat_facade_injection.py` refuses an impl import with no
        facade import in scope, and the only two files it carves out are carved
        out by NAME with no safety claimed; a third name on that list would have
        bought the coupling back for the price of an allowlist line. So the
        contract is cured at the facade, and this arm proves the route rather
        than the allowlist: the FACADE's attribute is replaced, and the token
        the child comes back with is the replaced one.

        DISCRIMINATING, on an otherwise-valid launch: the facade re-export is a
        DIRECT reference to the impl function captured at import time, so a
        `cmd_launch` that imported from the impl module would never see this
        patch and would answer with the shell's own value instead.

        CONTROL: the process environment carries a DIFFERENT, valid token
        throughout, so an unpatched read cannot pass by accident — it comes back
        as `shell-attempt` and the equality fails on two real values rather than
        on two blanks. Blast radius: `mock.patch.object` on the facade attribute
        for the duration of one `cmd_launch`, restored on exit; no register, no
        spawn, no other module's binding is touched.
        """
        from helm import seat
        from helm.seat_role import SPAWN_ATTEMPT_ENV
        err = io.StringIO()
        with mock.patch.dict(os.environ,
                             {SPAWN_ATTEMPT_ENV: "shell-attempt"},
                             clear=False), \
                mock.patch.object(seat, "spawn_attempt_token",
                                  return_value="through-the-facade") as reader, \
                mock.patch.object(launch.seats, "join"), \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  return_value=0) as owner, \
                contextlib.redirect_stderr(err):
            rc = launch.cmd_launch(["--no-install", "--seat", "alice"])
        self.assertEqual(rc, 0)
        self.assertTrue(reader.called,
                        "cmd_launch never reached the facade's reader")
        _argv, child = owner.call_args.args
        self.assertEqual(child[SPAWN_ATTEMPT_ENV], "through-the-facade")
        self.assertIn("[helm launch] spawn attempt through-the-facade",
                      err.getvalue())

    def test_cmd_launch_survives_a_deleted_cwd(self):  # noqa: VACUOUS_ASSERTION — the contract is reaching the owner with cwd=None rather than raising; the owner call and resolved join tuple are positive controls
        """MED (fable delta pair @2b4d496, probe A6): the seat DEFAULT called
        stable_seat() — a bare os.getcwd() — BEFORE the safe_cwd resolver
        line, so `helm launch` from a pruned worktree with neither --seat nor
        HELM_CHAT_NAME died with FileNotFoundError instead of launching
        un-homed as the adjacent comment promises. safe_cwd is hoisted above
        the seat default; the seat falls to <host>-here, the room to main."""
        # Back to the cwd this arm FOUND. The tests directory is not it, and
        # every module that ran after this one in the same process would run
        # from there.
        saved = os.getcwd()
        self.addCleanup(os.chdir, saved)
        d = tempfile.mkdtemp(dir=self.tmp)
        os.chdir(d)
        os.rmdir(d)
        self.assertRaises(OSError, os.getcwd)   # the probe's precondition
        with mock.patch.object(launch.seats, "join") as join, \
                mock.patch.object(launch.seat_launch_owner, "exec_attached",
                                  return_value=None) as execvpe:
            rc = launch.cmd_launch(["--no-install"])
        self.assertIsNone(rc)                   # reached exec, no traceback
        self.assertTrue(execvpe.called)
        kw = join.call_args.kwargs
        self.assertIsNone(kw["cwd"])            # un-homed, not crashed
        self.assertEqual(kw["room"], "main")
        self.assertTrue(kw["seat"].endswith("-here"))

    def test_stable_seat_sanitized(self):
        s = launch.stable_seat("/tmp/My Proj!x")
        self.assertNotIn(" ", s)
        self.assertNotIn("!", s)
        self.assertTrue(s.endswith("My-Proj-x"))
        self.assertLessEqual(len(s), 64)


if __name__ == "__main__":
    unittest.main()
