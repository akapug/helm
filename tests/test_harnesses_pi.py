#!/usr/bin/env python3
"""pi as a discovered harness — 0.3's first horizontal slice.

WHAT "HORIZONTAL" MEANT, in the owner's words: "not deepning already-made
stuff, adding new stuff in existing categories." Session discovery is an
existing category with three entries (claude, codex, opencode). pi is a fourth.
No new concept, no new coordination primitive — one more scanner yielding the
same observation shape.

THE CONVERGED PREMISE WAS FALSE, and finding that out cost one grep. The 0.3
planning seed's §E-1 was converged across three model families AND owner-
ratified ("those all seem correct to me"), and it pinned pi detection on a
`PI_SESSION_ID` env marker. That variable does not exist anywhere in pi's
source. What exists is `PI_CODING_AGENT="true"` (set unconditionally in
cli.ts, so it propagates to children) and a session directory whose env
override name is DERIVED FROM APP_NAME — `${APP_NAME.toUpperCase()}_CODING_AGENT_SESSION_DIR`,
where APP_NAME comes from package.json's `piConfig.name` and pi's own comments
name `TAU_CODING_AGENT_DIR` as the rebranded case.

That is the `converged-design-on-unverified-premise` class exactly: cross-family
agreement satisfies author != reviewer and still misses this, because
frame-blindness is a property of the shared FRAME, not the shared family. Three
families agreed about a variable none of them looked for.

WHAT IS ACTUALLY TRUE, measured against a live session file:
    ~/.pi/agent/sessions/<path-slug>/<timestamp>_<uuid>.jsonl
    line 1: {"type":"session","version":3,"id":"<uuid>","cwd":"<REAL PATH>"}
so pi obeys the same law claude and codex do — the real working directory is
recorded INSIDE the transcript, and the directory slug
(`--home-owner-dev-acme-widget--`) is lossy by construction: it cannot
distinguish a hyphen inside a directory name from a path separator. Decoding
the slug would be the exact mistake helm's second principle forbids.
"""
import json
import os
import shutil
import tempfile
import time
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import harnesses  # noqa: E402


class PiScannerTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-pi-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def session(self, slug, cwd, uuid="019e4b52-1c75-7bd2-9beb-79be4b65fe73",
                ts="2026-05-21T16-15-32-981Z", extra=()):
        d = os.path.join(self.root, slug)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%s_%s.jsonl" % (ts, uuid))
        rows = [{"type": "session", "version": 3, "id": uuid,
                 "timestamp": "2026-05-21T16:15:32.981Z", "cwd": cwd}]
        rows.extend(extra)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return p

    def test_the_real_cwd_comes_from_LINE_ONE_not_the_slug(self):
        """The load-bearing assertion. `--home-owner-dev-acme-widget--` cannot be
        decoded back into `/home/owner/dev/acme/widget` without guessing which
        hyphens were separators — a directory legitimately named `my-project`
        is indistinguishable from `my/project`. The transcript records the
        truth; the slug is a filename."""
        self.session("--home-owner-dev-acme-widget--", "/home/owner/dev/acme/widget")
        out = harnesses.pi_observations(self.root)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["cwd"], "/home/owner/dev/acme/widget")
        self.assertEqual(out[0]["harness"], "pi")

    def test_a_hyphenated_directory_name_survives(self):
        """The case that proves the point above rather than merely asserting
        it: a real path whose own name contains hyphens."""
        self.session("--home-owner-dev-my-cool-repo--", "/home/owner/dev/my-cool-repo")
        self.assertEqual(harnesses.pi_observations(self.root)[0]["cwd"],
                         "/home/owner/dev/my-cool-repo")

    def test_sessions_in_one_cwd_are_GROUPED_and_counted(self):
        self.session("--a--", "/repo", uuid="aaa", ts="2026-05-21T10-00-00-000Z")
        self.session("--a--", "/repo", uuid="bbb", ts="2026-05-22T10-00-00-000Z")
        out = harnesses.pi_observations(self.root)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sessions"], 2)
        self.assertEqual(sorted(out[0]["refs"]), ["aaa", "bbb"])

    def test_the_ref_is_the_UUID_pi_itself_accepts(self):
        """`pi --session <path|id>` takes the uuid, so the ref must be the uuid
        and not the whole filename — a ref you cannot paste back into the
        harness is decoration."""
        self.session("--a--", "/repo", uuid="019e4b52-1c75-7bd2-9beb-79be4b65fe73")
        self.assertEqual(harnesses.pi_observations(self.root)[0]["refs"],
                         ["019e4b52-1c75-7bd2-9beb-79be4b65fe73"])

    def test_an_EMPTY_slug_directory_yields_nothing(self):
        """Measured on the live host: two of three slug dirs held zero files.
        An empty directory is not a session and must not invent an
        observation."""
        os.makedirs(os.path.join(self.root, "--home-owner-scratch--"))
        self.assertEqual(harnesses.pi_observations(self.root), [])

    def test_a_session_with_NO_cwd_is_skipped_not_guessed(self):
        """Fail-open per file is the scanner law — and the failure must be a
        SKIP, never a slug-derived guess, which would silently invent a project
        at a path that does not exist."""
        d = os.path.join(self.root, "--home-owner--")
        os.makedirs(d)
        with open(os.path.join(d, "x_abc.jsonl"), "w") as f:
            f.write('{"type":"session","id":"abc"}\n')
        self.assertEqual(harnesses.pi_observations(self.root), [])

    def test_a_corrupt_session_never_breaks_the_map(self):
        self.session("--a--", "/repo", uuid="good")
        d = os.path.join(self.root, "--b--")
        os.makedirs(d)
        with open(os.path.join(d, "x_bad.jsonl"), "wb") as f:
            f.write(b"\xff\xfe not json at all")
        out = harnesses.pi_observations(self.root)
        self.assertEqual([o["cwd"] for o in out], ["/repo"])

    def test_an_absent_pi_install_is_silent(self):
        """Most machines have no pi. A scanner that raised, or warned, on a
        missing home would make helm noisy for everyone who does not use it."""
        self.assertEqual(
            harnesses.pi_observations(os.path.join(self.root, "nope")), [])

    def test_last_seen_and_days_track_the_newest_session(self):
        p = self.session("--a--", "/repo", uuid="one")
        os.utime(p, (1000000, 1000000))
        q = self.session("--a--", "/repo", uuid="two",
                         ts="2026-05-22T10-00-00-000Z")
        os.utime(q, (2000000, 2000000))
        o = harnesses.pi_observations(self.root)[0]
        self.assertEqual(o["last_seen"], 2000000)
        self.assertEqual(len(o["days"]), 2)


class PiSessionRootTest(unittest.TestCase):
    """The env override, whose NAME is not a constant."""

    def test_the_default_is_the_documented_path(self):
        self.assertTrue(harnesses.pi_session_root(env={}).endswith(
            os.path.join(".pi", "agent", "sessions")))

    def test_an_APP_NAME_DERIVED_override_is_honoured(self):
        """pi builds the key as `${APP_NAME.toUpperCase()}_CODING_AGENT_SESSION_DIR`,
        so a rebranded build sets TAU_… and a hardcoded PI_… lookup would miss
        it entirely. Matching on the SUFFIX is what makes this survive the
        rename pi's own source anticipates."""
        self.assertEqual(
            harnesses.pi_session_root(env={"TAU_CODING_AGENT_SESSION_DIR": "/x"}),
            "/x")
        self.assertEqual(
            harnesses.pi_session_root(env={"PI_CODING_AGENT_SESSION_DIR": "/y"}),
            "/y")

    def test_an_EMPTY_override_falls_back_rather_than_scanning_the_root(self):
        """An empty string is not a path. Honouring it would point the scanner
        at "" and, depending on the join, at the filesystem root."""
        self.assertTrue(harnesses.pi_session_root(
            env={"PI_CODING_AGENT_SESSION_DIR": ""}).endswith("sessions"))

    def test_an_unrelated_variable_is_not_mistaken_for_the_override(self):
        self.assertTrue(harnesses.pi_session_root(
            env={"MY_CODING_AGENT_SESSION_DIRECTORY": "/nope"}).endswith("sessions"))


class AllObservationsTest(unittest.TestCase):
    def test_pi_is_IN_the_aggregate(self):
        """The wiring rung, in one assertion: a scanner nobody calls is the
        thing `helm wiring` exists to catch. If pi is not in all_observations,
        `helm sync` never sees a pi project."""
        import inspect
        src = inspect.getsource(harnesses.all_observations)
        self.assertIn("pi_observations", src)


if __name__ == "__main__":
    unittest.main()
