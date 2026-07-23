#!/usr/bin/env python3
"""docs/NEW_AGENT_GUIDE.md — the one onboarding artifact every new seat is
steered to, and the RSH wiring that steers them: the `helm chat join` banner
ends with a pointer at the guide. Source-driven both ways — the banner's
pointer must name a file that exists (deleting/moving the guide fails the
suite), and the banner must actually carry it (dropping the pointer fails
the suite). Hermetic: HELM_HOME/HELM_CHAT_DIR point at tmp dirs."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seat, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
            "HELM_CHAT_ROOM_SOURCE", "HELM_CHAT_NODE_URL", "HELM_ADOPTED_DIR",
            # the MELD_* legacy fallbacks too: home.env_pair falls back to
            # them, so an ambient MELD_CHAT_ROOM redirects the canary's bare
            # post to a foreign room and false-fires the homing message —
            # and the hazard is realistic (launch_line itself unsets
            # MELD_CHAT_ROOM because launchers leak it)
            "MELD_CHAT_ROOM", "MELD_CHAT_ROOM_SOURCE", "MELD_CHAT_NAME",
            "MELD_CHAT_DIR", "MELD_CHAT_NODE_URL",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(REPO, "docs", "NEW_AGENT_GUIDE.md")
HELM = os.path.join(REPO, "bin", "helm")

# Probe-of-source runners: a guide claim gated on grepping/reading repo files
# is stale-by-construction — rc 2 from any non-repo cwd, and it tests a source
# substring, not runtime capability (the `grep -q per-instance helm/seat.py`
# class). Operational claims must be helm verbs.
SOURCE_PROBE_RUNNERS = {"grep", "egrep", "fgrep", "rg", "cat", "sed", "awk",
                        "head", "tail", "ls", "find", "test", "python",
                        "python3"}

# Named SUBcommands the guide leans on. Depth-1 verb probing (toks[1]) cannot
# see a dropped/renamed subverb while the parent keeps rc0 — these pairs get
# the differential parent+subverb probe below. Every pair must also appear
# verbatim in the guide, so this list cannot drift away from the text.
GUIDE_SUBVERBS = (("chat", "dm"), ("chat", "reply"), ("session", "ls"),
                  ("work", "release"), ("seat", "launch"))

LANE_MARKER = re.compile(r"\(lane/([A-Za-z0-9._~-]+)\)")


def parse_guide_helm_spans(text):
    """(present_verbs, marked_pairs): every `helm <verb> ...` span splits into
    present-tense verbs (must --help-resolve rc0 from a neutral cwd) and
    future-tense (verb, lane) pairs — a span immediately followed by the
    structural marker `(lane/<lane>)` naming where the verb lands."""
    present, marked = set(), []
    for m in re.finditer(r"`(helm [^`\n]+)`", text):
        toks = m.group(1).split()
        if len(toks) < 2 or toks[1].startswith("<"):
            continue  # `helm <verb> --help` is the probe idiom itself
        lane = LANE_MARKER.match(text[m.end():].lstrip())
        if lane:
            marked.append((toks[1], lane.group(1)))
            continue  # marked unlanded — present tense would be a lie
        present.add(toks[1])
    return present, marked


class NewAgentGuideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-guide-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_guide_exists_where_the_banner_points(self):
        # one constant, both truths: the banner interpolates GUIDE_PATH, and
        # GUIDE_PATH must be a real file in this checkout
        self.assertEqual(seats.GUIDE_PATH, GUIDE)
        self.assertTrue(os.path.isfile(seats.GUIDE_PATH),
                        "banner points at a missing guide: %s" % seats.GUIDE_PATH)

    def test_join_banner_carries_the_guide_pointer(self):
        _seat, line = seats.join(seat="newbie", cwd=self.tmp)
        self.assertIn(seats.GUIDE_PATH, line)
        self.assertIn("NEW_AGENT_GUIDE.md", line)

    def test_guide_covers_the_load_bearing_verbs(self):
        # the guide is real onboarding, not a doc dump: every first-10-minutes
        # claim is an executable verb — pin the load-bearing ones so a rewrite
        # cannot silently drop a leg
        text = open(GUIDE, encoding="utf-8").read()
        for needle in ("helm chat wait --seat", "helm chat post",
                       "helm work claim", "helm store resolve", "helm fleet",
                       "helm who", "helm session ls", "helm seat launch",
                       "--room", "Monitor", "ToolSearch"):
            self.assertIn(needle, text, "guide lost its %r leg" % needle)

    def test_guide_claims_are_helm_probes_not_source_greps(self):
        # the finding-1 class, structurally: a backticked command that greps or
        # reads repo files only works from the checkout root and pins a source
        # substring, not a runtime capability — the guide may not carry one
        text = open(GUIDE, encoding="utf-8").read()
        for span in re.findall(r"`([^`\n]+)`", text):
            toks = span.split()
            first = toks[0] if toks else ""
            self.assertNotIn(
                first, SOURCE_PROBE_RUNNERS,
                "guide gates on a source probe %r — cwd-dependent and "
                "stale-by-construction; state it as a helm verb" % span)

    def test_guide_helm_commands_exist_from_a_neutral_cwd(self):
        # every `helm <verb> ...` claim must resolve against the REAL CLI from
        # a NEUTRAL cwd (tmpdir): `helm <verb> --help` exits 0 for a known
        # verb and 2 for an unknown one, so a dropped/renamed verb fails here
        # and no claim can quietly depend on being run from the checkout.
        # Future-tense claims are allowed ONLY with the structural marker: the
        # span immediately followed by "(lane/<lane>)" — `helm fleet`
        # (lane/fleet-truth-verb) — which names where the verb lands.
        text = open(GUIDE, encoding="utf-8").read()
        verbs, _marked = parse_guide_helm_spans(text)
        self.assertTrue(verbs, "guide carries no helm commands to verify")
        neutral = tempfile.mkdtemp(prefix="helm-guide-neutral-")
        try:
            for verb in sorted(verbs):
                p = subprocess.run(
                    [sys.executable, HELM, verb, "--help"], cwd=neutral,
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(
                    p.returncode, 0,
                    "guide names `helm %s` but the CLI refuses it from a "
                    "neutral cwd (rc %d): %s"
                    % (verb, p.returncode, p.stderr.strip()))
        finally:
            shutil.rmtree(neutral, ignore_errors=True)

    def test_lane_markers_expire_and_name_real_lanes(self):
        # the (lane/<lane>) marker is a dated promise, not a permanent skip:
        # (a) EVERY marker anywhere in the guide — helm-span-adjacent or
        #     attached to prose (the per-instance-codex-proxies paragraphs) —
        #     must name a lane that actually exists as a branch; a bogus
        #     marker is a fabricated excuse and fails here;
        # (b) once a span-adjacent marker's verb --help-resolves rc0 from a
        #     neutral cwd the lane LANDED — the marker is now a stale lie
        #     ("supersedes it once landed" about a verb that IS landed) and
        #     fails here: drop the marker and rewrite the claim in the
        #     present tense. Prose markers have no verb to probe; their
        #     expiry is pinned by the launch-line canary below.
        text = open(GUIDE, encoding="utf-8").read()
        _verbs, marked = parse_guide_helm_spans(text)
        all_lanes = {m.group(1) for m in LANE_MARKER.finditer(text)}
        self.assertTrue(
            set(lane for _v, lane in marked) <= all_lanes,
            "span-adjacent markers escaped the all-marker scan")
        neutral = tempfile.mkdtemp(prefix="helm-guide-neutral-")
        try:
            for lane in sorted(all_lanes):
                b = subprocess.run(
                    ["git", "-C", REPO, "branch", "--list", "--all",
                     "lane/%s" % lane, "*/lane/%s" % lane],
                    capture_output=True, text=True, timeout=60)
                self.assertTrue(
                    b.stdout.strip(),
                    "guide marks (lane/%s) but no such lane branch exists — "
                    "a marker must name a real lane" % lane)
            for verb, lane in marked:
                p = subprocess.run(
                    [sys.executable, HELM, verb, "--help"], cwd=neutral,
                    capture_output=True, text=True, timeout=60)
                self.assertNotEqual(
                    p.returncode, 0,
                    "`helm %s` resolves from a neutral cwd — the verb LANDED; "
                    "drop the (lane/%s) marker and state it in the present "
                    "tense" % (verb, lane))
        finally:
            shutil.rmtree(neutral, ignore_errors=True)

    @staticmethod
    def _norm(text, tokens):
        """Collapse each token to @ at word boundaries ([A-Za-z0-9_-] runs):
        boundary-aware so a 2-char sub (`ls`, `dm`) is never eaten out of an
        unrelated word, and applied to a TOKEN SET, not just the probed one."""
        for tok in tokens:
            text = re.sub(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])"
                          % re.escape(tok), "@", text)
        return text

    def _probe_sub(self, parent, probed, sub, cwd):
        """(rc, out, err) of `helm <parent> <probed> --help`. BOTH the probed
        token and the real sub are normalized to @ in the output — the real
        sub in the BOGUS probe's output too, because a parent's unknown-
        subcommand fallback (usage/choice text) may still NAME a dropped sub:
        normalizing only each probe's own token left those byte-identical
        outputs looking different, so a sloppy drop that forgot the usage
        text false-passed the differential (proven with rename plants in
        work/session/chat — 4 of 5 pinned pairs were vulnerable)."""
        p = subprocess.run(
            [sys.executable, HELM, parent, probed, "--help"], cwd=cwd,
            capture_output=True, text=True, timeout=60)
        toks = (probed, sub)
        return (p.returncode, self._norm(p.stdout, toks),
                self._norm(p.stderr, toks))

    def test_guide_subverbs_resolve_differentially(self):
        # depth-2: rc alone cannot prove a SUBverb exists (most known subverbs
        # exit rc 2 on --help, same code as an unknown one). The differential
        # probe can: a real subverb's response must DIFFER from a bogus
        # sibling's — if `chat dm` were dropped it would take the exact
        # unknown-subcommand path the bogus probe takes, and the normalized
        # outputs collapse to equal. `helm work` refuses everything outside a
        # git repo, so the probes run from a scratch git repo — still neutral,
        # not the checkout.
        text = open(GUIDE, encoding="utf-8").read()
        scratch = tempfile.mkdtemp(prefix="helm-guide-subverb-")
        try:
            subprocess.run(["git", "init", "-q", scratch],
                           capture_output=True, text=True, timeout=60,
                           check=True)
            for parent, sub in GUIDE_SUBVERBS:
                self.assertIn(
                    "helm %s %s" % (parent, sub), text,
                    "GUIDE_SUBVERBS pins `helm %s %s` but the guide no longer "
                    "names it — drop the pair or restore the claim"
                    % (parent, sub))
                real = self._probe_sub(parent, sub, sub, scratch)
                bogus = self._probe_sub(parent, "zzqx-no-such-subverb", sub,
                                        scratch)
                self.assertNotEqual(
                    real, bogus,
                    "`helm %s %s` answers exactly like a bogus subverb — "
                    "dropped or renamed while the parent still resolves"
                    % (parent, sub))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_canary_bare_post_from_a_project_cwd_resolves_to_main(self):
        # ============================ MERGE-TIME CANARY =====================
        # This test pins TODAY'S behavior: a bare `helm chat post` (no --room)
        # from a project cwd resolves to `main`, NOT the cwd-derived home room
        # — exactly what guide section 2 states. lane/homing-as-prevented
        # rewires every no---room verb through seats.resolve_homing (env seam
        # > cwd derivation), which INVERTS this default.
        #
        # WHEN THIS TEST FAILS AFTER lane/homing-as-prevented MERGES, THAT IS
        # THE CANARY FIRING, NOT A REGRESSION: flip BOTH together —
        #   1. guide section 2: the bare default now resolves to the derived
        #      home room (drop the "today ... resolves to `main`" sentence and
        #      its lane marker);
        #   2. this test: assert the post lands in the DERIVED room and not in
        #      `main`.
        # Do not delete the test and do not touch the sentence without the
        # pin, or the guide silently inverts (staleness-adversary MED 2).
        # ====================================================================
        proj = os.path.join(self.tmp, "canary-proj")
        os.makedirs(proj)
        subprocess.run(["git", "init", "-q", proj], capture_output=True,
                       text=True, timeout=60, check=True)
        token = "guide-canary-row"
        p = subprocess.run([sys.executable, HELM, "chat", "post", token],
                           cwd=proj, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        in_main = subprocess.run(
            [sys.executable, HELM, "chat", "read", "--room", "main",
             "--since", "0"], cwd=proj, capture_output=True, text=True,
            timeout=60)
        self.assertNotIn(
            token, in_main.stdout,
            "bare post from a project cwd landed in `main` — the homing "
            "default regressed (resolve_homing no longer owns the bare-post "
            "room); guide section 2 and this pin flip together")
        # The derived room is FINGERPRINTED (proj-<16hex>), never the dir
        # basename — discover it from the chat dir instead of guessing.
        chat_dir = os.environ["HELM_CHAT_DIR"]
        rooms = [f[:-6] for f in os.listdir(chat_dir) if f.endswith(".jsonl")]
        derived = [r for r in rooms if r != "main"]
        self.assertEqual(
            len(derived), 1,
            "expected exactly one derived room beside main, got %r" % rooms)
        self.assertRegex(
            derived[0], r"^canary-proj-[0-9a-f]{16}$",
            "derived room is not <project>-<fingerprint> shaped")
        in_derived = subprocess.run(
            [sys.executable, HELM, "chat", "read", "--room", derived[0],
             "--since", "0"], cwd=proj, capture_output=True, text=True,
            timeout=60)
        self.assertIn(
            token, in_derived.stdout,
            "bare post missed the derived home room — homing landed and the "
            "default must resolve there; guide section 2 and this pin flip "
            "together")

    def test_launch_line_instance_port_and_token_file(self):
        # ===================== LANDED-STATE REGRESSION GUARD ================
        # Post lane/per-instance-codex-proxies (the merge-time canary fired at
        # land and both guide-section-6 paragraphs were flipped to present
        # tense). This pin now guards the LANDED invariant, both directions:
        # the printed `helm seat launch codex -i 2` line MUST carry the
        # instance's OWN base+N port (e.g. 8319 for codex-2), MUST NOT carry
        # the shared family port (8317 — shared-port multiplication was the
        # silent-hang vector), and MUST read the bearer from a 0600 token file
        # at exec time rather than embedding it inline (transcripts are the
        # corpus). A revert to the shared-port/inline-token form re-fires this
        # and names guide section 6 to re-sync. Do not delete the test.
        # ====================================================================
        add = subprocess.run(
            [sys.executable, HELM, "seat", "add", "codex"], cwd=self.tmp,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(add.returncode, 0, add.stderr)
        p = subprocess.run(
            [sys.executable, HELM, "seat", "launch", "codex", "-i", "2"],
            cwd=self.tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        line = p.stdout.strip()
        base = seat.FAMILIES["codex"]["port"]
        self.assertIn(
            "ANTHROPIC_BASE_URL=http://127.0.0.1:%d" % (base + 2), line,
            "the -i 2 launch line must carry the instance's OWN proxy port "
            "(base+N) — per-instance proxies are landed; a revert to the "
            "shared family port reintroduces the silent-hang vector (guide "
            "section 6)")
        self.assertNotIn(
            "ANTHROPIC_BASE_URL=http://127.0.0.1:%d " % base, line,
            "the -i 2 launch line must NOT carry the shared family port — "
            "shared-port multiplication was the silent-hang vector (guide "
            "section 6)")
        self.assertNotRegex(
            line, r"ANTHROPIC_AUTH_TOKEN=[0-9a-f]{16,}",
            "the launch line must NOT embed the bearer inline — it reads the "
            "0600 token file at exec time (transcripts are the corpus; guide "
            "section 6)")
        self.assertIn(
            "ANTHROPIC_AUTH_TOKEN=$(cat ", line,
            "the launch line must read the bearer from its 0600 token file "
            "at exec time (guide section 6)")


if __name__ == "__main__":
    unittest.main()
