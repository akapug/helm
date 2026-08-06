"""gate run --box — the job-routing seam, proven without a network.

Every arm drives `gateroute.route` through a FAKE transport that records the
one remote session (script, bundle path, timeout) and answers with scripted
output, so the suite ships no bytes anywhere. Receipts are built by the REAL
minting grammar (`gate._receipt_id`, v4 with a host block, over a real commit
in a real scratch repo) exactly as tests/test_gate_import.py does — the
import ladder at the end of the route is the REAL one, and the ledger is
read back to prove what did (or did not) enter it. The refusal arms
additionally assert the transport was NEVER CALLED: a consent refusal that
already shipped the tree would not be a refusal.

The trust-boundary arms encode the first trust-boundary review's twenty findings —
authority (probe consent is not execute consent), identity (unique
resolution, exact booleans, channel-bound node, per-invocation challenge),
shipment (captured-sha bundle, streamed stdin, atomic artifact, contained
paths), clocks (None-vs-0 timeout, queue-aware ceiling), and protocol
(challenge-framed markers, laundered clone diagnostics, honest inventory
warnings) — and the second review's five: an incomplete inventory refuses
BEFORE resolution, `fab_accept` is an eligibility note and never a
capability, payload is framed by ENCODING and ARITY rather than by a
challenge the job under test can read out of /proc, concurrent routes of one
HEAD cannot share a bundle path, and every blocking remote stage is
signal-reachable. Where the review proved a live repro, the hermetic
equivalent lives here — including `RemoteScriptShellTest`, which runs the
far-side script under a REAL /bin/sh against stub binaries, so the transcript
the parser reads is one the script actually produced."""
import base64
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helm import gate, gateimport, gateroute, home, pk

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            # the box-provider chain must stay inert: this suite always
            # injects `nodes` (or patches inventory), and hermeticity keeps a
            # future arm from running the REAL external inventory CLI. The
            # JOB consent env is cleared so no ambient grant leaks in.
            "HELM_STORAGE_MATRIX_HOSTS", "HELM_BOXES_SSH_CONSENT",
            "HELM_BOXES_SSH_CONFIG", "HELM_BOXES_EXTERNAL_CLI",
            "HELM_BOXES_JOB_CONSENT")

# The row carries NO capability of its own: the job grant is the owner's env
# and only that (the second review), so RouteBase.setUp names this box in
# HELM_BOXES_JOB_CONSENT and the authority arms below delete it to prove
# absence refuses. `fab_accept` is deliberately absent from the consented
# fixture — it is an eligibility note, and a fixture carrying it would let a
# regression that re-reads it as consent stay invisible in every other arm.
CONSENTED = {"host": "box-a", "label": "box-a-main", "ssh_host": "box-a",
             "probe_mode": "ssh", "reachable": True, "provider": "declared"}

_CHALLENGE_RE = re.compile(r"::helm-job-([0-9a-f]{32}) ")


def _reap(proc):
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args),
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise AssertionError("git %s: %s" % (args, p.stderr))
    return p.stdout.strip()


class FakeTransport:
    """Records the session; answers from a script. `reply` builds the
    transcript FROM the script (so it can carry the invocation's challenge);
    `err` simulates a transport-layer failure; `on_run` is a mid-transport
    side effect (the TOCTOU arm dirties the caller repo with it)."""

    def __init__(self, reply=None, rc=0, out="", errout="", err=None,
                 on_run=None):
        self.calls = []
        self.reply, self.rc, self.out = reply, rc, out
        self.errout, self.err, self.on_run = errout, err, on_run

    def run(self, script, stdin_path, timeout):
        head = b""
        try:
            with open(stdin_path, "rb") as fh:
                head = fh.read(16)
        except OSError:
            pass
        self.calls.append({"script": script, "stdin_path": stdin_path,
                           "stdin_head": head, "timeout": timeout})
        if self.on_run:
            self.on_run()
        if self.err:
            return None, None, None, self.err
        out = self.reply(script) if self.reply else self.out
        return self.rc, out, self.errout, None


class SshTransportTest(unittest.TestCase):
    """The REAL transport argv and bounded readers; FakeTransport cannot
    prove either because it intentionally starts no ssh process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gateroute-ssh-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.bundle = os.path.join(self.tmp, "ship.bundle")
        with open(self.bundle, "wb") as fh:
            fh.write(b"bundle")
        self.prior_path = os.environ.get("PATH")
        os.environ["PATH"] = self.bin + ":/usr/bin:/bin"
        self.addCleanup(os.environ.__setitem__, "PATH", self.prior_path or "")

    def _ssh(self, body):
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def test_ssh_uses_keepalives_and_names_the_exact_channel(self):  # noqa: VACUOUS_ASSERTION — exact rc/stdout/stderr and argv fields are positive transport observables; err=None is the intentional successful absence
        argv_path = os.path.join(self.tmp, "argv")
        self._ssh("printf '%%s\\n' \"$@\" > %s\n"
                  "printf 'answer'\nprintf 'diagnostic' >&2\nexit 7\n"
                  % shlex.quote(argv_path))
        rc, out, errout, err = gateroute.SshTransport(
            "elsewhere.example.net").run("say hi", self.bundle, 5)
        self.assertIsNone(err)
        self.assertEqual((rc, out, errout), (7, "answer", "diagnostic"))
        with open(argv_path) as fh:
            argv = fh.read()
        self.assertIn("ServerAliveInterval=%d"
                      % gateroute._SSH_KEEPALIVE_INTERVAL_S, argv)
        self.assertIn("ServerAliveCountMax=%d"
                      % gateroute._SSH_KEEPALIVE_COUNT_MAX, argv)
        self.assertIn("elsewhere.example.net", argv)

    def test_remote_output_is_drained_but_MEMORY_BOUNDED(self):
        self._ssh("printf '0123456789abcdef'\n")
        with mock.patch.object(gateroute, "_TRANSPORT_OUTPUT_CAP", 8):
            rc, out, errout, err = gateroute.SshTransport(
                "box-a").run("true", self.bundle, 5)
        self.assertIsNone(rc)
        self.assertIsNone(out)
        self.assertIsNone(errout)
        self.assertIn("8-byte output ceiling", err)
        self.assertIn("stdout", err)

    def test_local_transport_timeout_kills_ssh_and_returns_named(self):  # noqa: VACUOUS_ASSERTION — bounded elapsed time plus the named timeout refusal are positive effects; absent rc/output is the required no-partial-result contract
        self._ssh("sleep 10\n")
        started = time.monotonic()
        rc, out, errout, err = gateroute.SshTransport(
            "box-a").run("true", self.bundle, 0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((rc, out, errout), (None, None, None))
        self.assertIn("box-a", err)
        self.assertIn("ceiling", err)


class RouteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-route-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "route-test-seat"
        os.environ["HELM_BOXES_SSH_CONFIG"] = "/nonexistent/ssh-config"
        os.environ["HELM_BOXES_EXTERNAL_CLI"] = "/nonexistent/inventory-cli"
        # THE ONLY JOB CAPABILITY, granted explicitly here because it is the
        # only thing that can grant one: no inventory field admits a job.
        os.environ["HELM_BOXES_JOB_CONSENT"] = "box-a"
        # scratch (bundle + fetched artifact) stays inside this test's tmp
        self.scratch_dir = os.path.join(self.tmp, "scratch")
        os.makedirs(self.scratch_dir)
        patcher = mock.patch(
            "helm.scratch.resolve",
            lambda cls="small", name=None, create=True: (self.scratch_dir,
                                                         "test"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "helm"))
        _git(self.repo, "init", "-q", ".")
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("content\n")
        # the routed command is `python3 -m helm` FROM the shipped tree, so
        # the fixture tree must carry the package (finding 1's preflight)
        with open(os.path.join(self.repo, "helm", "__main__.py"), "w") as f:
            f.write("# stub runner for the shipment preflight\n")
        _git(self.repo, "add", "f.txt", "helm/__main__.py")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "base")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")

    def tearDown(self):
        for key in ENV_KEYS:
            prior = self.prior.get(key)
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _receipt(self, **over):
        """A v4 receipt the REAL grammar would mint on the remote box."""
        row = {"v": 4, "event": "gate", "ts": pk.now_ts(),
               "repo_id": "/dev/shm/helm-job.abc123/tree",
               "head": self.head, "tree": self.tree, "dirty": False,
               "head_after": self.head, "tree_after": self.tree,
               "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.12.3",
                               "language": "3.12.3",
                               "executable": "/usr/bin/python3"},
               "host": {"node": "box-a", "system": "Linux",
                        "release": "6.8.0", "id": "ab" * 8},
               "argv": ["/usr/bin/python3", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": None, "rc": 0, "wall": 100.0,
               "status": "OK", "ran": 5000, "skipped": 8, "detail": "",
               "elapsed": 99.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        return row

    @staticmethod
    def _section(mark, name, text, frame=True, raw_extra=()):
        """One framed section, exactly as the far side emits it: the body
        leaves the box BASE64, which is what makes payload structurally
        unable to spell a marker. `frame=False` is the adversary's (or a
        stale box's) unencoded body, which must refuse rather than be read;
        `raw_extra` is a line INTERLEAVED into the body by a writer that is
        not the script (a child with the session's stdout)."""
        body = base64.b64encode(text.encode("utf-8")).decode("ascii") \
            if frame else text
        return ([mark + name + "-begin"] + ([body] if body else [])
                + [l.replace("{MARK}", mark) for l in raw_extra]
                + [mark + name + "-end"])

    def _remote_out(self, challenge, gate_json=None, receipts=(), gate_rc=0,
                    scratch="tmpfs", node="box-a", cgroup="delegated",
                    done=True, error=None, gate_err_lines=(), inject=(),
                    frame=True, gate_err_raw=()):
        """The transcript. `{MARK}` inside a payload or injected line becomes
        THIS invocation's live marker prefix — the review's point being that
        the challenge is readable from /proc, so the job under test can spell
        one. `inject` lands at top level, where no section is open: that is
        where a child writing straight to the session's stdout mid-run
        arrives, and position alone cannot make it data."""
        mark = "::helm-job-%s " % challenge
        lines = []
        if error is not None:
            lines.append(mark + "error=%s" % error)
            return "\n".join(lines) + "\n"
        if node is not None:
            lines.append(mark + "node=%s" % node)
        lines.append(mark + "scratch=%s" % scratch)
        lines.append(mark + "cgroup=%s" % cgroup)
        lines.extend(l.replace("{MARK}", mark) for l in inject)
        lines.append(mark + "gate-rc=%d" % gate_rc)
        lines.extend(self._section(
            mark, "gate-json",
            "" if gate_json is None else json.dumps(gate_json, indent=1),
            frame))
        lines.extend(self._section(
            mark, "gate-err",
            "\n".join(l.replace("{MARK}", mark) for l in gate_err_lines),
            frame, gate_err_raw))
        lines.extend(self._section(
            mark, "receipts",
            "\n".join(json.dumps(r) for r in receipts), frame))
        if done:
            lines.append(mark + "done")
        return "\n".join(lines) + "\n"

    def _reply(self, gate_json=None, receipts=(), **kw):
        """A transcript builder bound to the SCRIPT's own challenge — the
        honest-box shape (only a session that ran the script can answer
        with its challenge)."""
        def build(script):
            challenge = _CHALLENGE_RE.search(script).group(1)
            return self._remote_out(challenge, gate_json, receipts, **kw)
        return build

    def _happy(self, row, **kw):
        return FakeTransport(reply=self._reply(
            {"minted": True, "receipt": row}, receipts=[row], **kw))

    def _route(self, transport, box="box-a", nodes=None, **kw):
        return gateroute.route(repo=self.repo, box=box, transport=transport,
                               nodes=[CONSENTED] if nodes is None else nodes,
                               **kw)

    def _ledger(self):
        try:
            with open(gate.receipts_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []


class ConsentLadderTest(RouteBase):
    """Refusals fire BEFORE the transport, and each names its lever."""

    def _refused(self, nodes, box="box-a"):
        transport = FakeTransport()
        result, err = self._route(transport, box=box, nodes=nodes)
        self.assertIsNone(result)
        self.assertEqual(transport.calls, [],
                         "a refusal must not ship anything")
        self.assertEqual(self._ledger(), [])
        return err

    def test_a_box_not_in_the_inventory_names_both_consent_flags(self):
        err = self._refused([CONSENTED], box="nosuchbox")
        self.assertIn("HELM_STORAGE_MATRIX_HOSTS", err)
        self.assertIn("HELM_BOXES_SSH_CONSENT", err)

    def test_an_unconsented_candidate_names_the_consent_flag(self):
        row = dict(CONSENTED, probe_mode="candidate", reachable=False,
                   display_only=True)
        self.assertIn("HELM_BOXES_SSH_CONSENT", self._refused([row]))

    def test_a_display_only_box_refuses(self):
        row = dict(CONSENTED, probe_mode="ssh-reach", display_only=True)
        self.assertIn("display-only", self._refused([row]))

    def test_an_unreachable_box_refuses(self):
        self.assertIn("not reachable",
                      self._refused([dict(CONSENTED, reachable=False)]))

    def test_a_truthy_reachable_STRING_is_not_reachability(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a LITERAL 4-tuple so it always runs; each arm asserts the named refusal
        """Review finding 4: an external provider's 'false' is a non-empty
        string; only the exact boolean True may ship code."""
        for value in ("false", "no", "yes", 1):
            row = dict(CONSENTED, reachable=value)
            self.assertIn("not reachable", self._refused([row]),
                          "reachable=%r admitted a job" % (value,))

    def test_this_machine_refuses_and_points_at_the_local_arm(self):
        row = dict(CONSENTED, probe_mode="local")
        self.assertIn("without --box", self._refused([row]))

    def test_a_probe_mode_that_cannot_carry_a_job_is_named(self):
        row = dict(CONSENTED, probe_mode="adb-via-host")
        self.assertIn("adb-via-host", self._refused([row]))

    def test_a_traversal_host_identity_refuses_at_pick(self):
        """Review finding 8: the row's host string later names the artifact
        file; '../../escaped' from an external provider dies here."""
        row = dict(CONSENTED, host="../../escaped")
        self.assertIn("unusable host identity", self._refused([row]))

    def test_an_incomplete_inventory_is_UNKNOWN_not_absence(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        """Review finding 20: a provider timeout must not be misread as
        'the owner never consented this box'."""
        transport = FakeTransport()
        with mock.patch.object(
                gateroute.boxes, "inventory",
                return_value=({"nodes": []},
                              "storage inventory failed: timed out")):
            result, err = gateroute.route(repo=self.repo, box="box-a",
                                          transport=transport, nodes=None)
        self.assertIsNone(result)
        self.assertIn("INCOMPLETE", err)
        self.assertIn("timed out", err)
        self.assertEqual(transport.calls, [])

    def test_an_incomplete_inventory_refuses_EVEN_WHEN_a_row_matched(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        """Second review finding 1, the exact repro: the richer
        provider timed out, a lower declared row matched, the owner had
        granted the job — and round one routed, because it only consulted
        the warning when NOTHING matched. The missing rows may carry a second
        identity for this name (the resolution would be ambiguous) or a
        stronger word about it; a broken chain proves neither, and UNKNOWN
        refuses whatever survived."""
        transport = FakeTransport()
        with mock.patch.object(
                gateroute.boxes, "inventory",
                return_value=({"nodes": [dict(CONSENTED)]},
                              "external inventory failed: timed out")):
            result, err = gateroute.route(repo=self.repo, box="box-a",
                                          transport=transport, nodes=None)
        self.assertIsNone(result, "a broken provider chain routed a job")
        self.assertIn("INCOMPLETE", err)
        self.assertIn("cannot be proven unique", err)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._ledger(), [])


class JobAuthorityTest(RouteBase):
    """First review finding 2 — THE P0: probing consent never
    authorizes running shipped code. Second review finding 2 — and
    neither does an inventory field. The owner's env is the whole grant."""

    def test_probe_consent_alone_refuses_and_names_the_job_lever(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        del os.environ["HELM_BOXES_JOB_CONSENT"]
        transport = FakeTransport()
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("HELM_BOXES_JOB_CONSENT", err)
        self.assertIn("PROBE consent", err)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._ledger(), [])

    def test_the_owner_env_grants_the_job_capability(self):
        os.environ["HELM_BOXES_JOB_CONSENT"] = "box-a"
        result, err = self._route(self._happy(self._receipt()))
        self.assertIsNone(err)
        self.assertEqual(result["verdict"], "imported")

    def test_the_job_grant_binds_the_ACTUAL_ssh_channel_not_its_alias(self):  # noqa: VACUOUS_ASSERTION — the first arm's channel-named refusal and zero calls prove denial; the second arm's imported result and both rendered channel values are unconditional positive controls
        """Final review finding 3: host=box-a is a lookup/display identity;
        ssh_host is where arbitrary code travels. A grant for the former must
        not authorise an unrelated latter, and the operator sees that channel
        on both output surfaces when it IS granted."""
        row = dict(CONSENTED, host="box-a",
                   ssh_host="elsewhere.example.net")
        transport = FakeTransport()
        result, err = self._route(transport, nodes=[row])
        self.assertIsNone(result)
        self.assertEqual(transport.calls, [])
        self.assertIn("elsewhere.example.net", err)
        self.assertIn("actual channel", err)
        os.environ["HELM_BOXES_JOB_CONSENT"] = "elsewhere.example.net"
        result, err = self._route(self._happy(self._receipt()), nodes=[row])
        self.assertIsNone(err)
        self.assertEqual(result["ssh_host"], "elsewhere.example.net")
        with mock.patch.object(gateroute, "route", return_value=(result, None)):
            out, errout = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(errout):
                gateroute.cmd_route("box-a", repo=self.repo)
            self.assertIn("ssh_host elsewhere.example.net", errout.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                gateroute.cmd_route("box-a", repo=self.repo, as_json=True)
            self.assertEqual(json.loads(out.getvalue())["ssh_host"],
                             "elsewhere.example.net")

    def test_fab_accept_yes_is_NOT_an_execution_capability(self):  # noqa: VACUOUS_ASSERTION — the two named refusal strings are the positive effect and the `fab accept` assertNotIn is an intentional absence (the verb does not exist); the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        """Second review finding 2, read at fab's own source: ACCEPT?
        is recomputed per render from up ∧ cargo-ready ∧ disk-above-floor and
        its banner says "eligible, not a routing guarantee". Round one took
        it for an owner switch, so a box that merely had free disk could be
        handed arbitrary code with no human ever granting anything."""
        del os.environ["HELM_BOXES_JOB_CONSENT"]
        transport = FakeTransport()
        result, err = self._route(transport,
                                  nodes=[dict(CONSENTED, fab_accept="yes")])
        self.assertIsNone(result, "a disk reading authorized a job")
        self.assertIn("HELM_BOXES_JOB_CONSENT=box-a", err)
        self.assertIn("transient eligibility proxy", err)
        # and it must not send the operator to a verb that does not exist:
        # `fab accept` is not a command anyone can run (checked in fab)
        self.assertNotIn("fab accept", err)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._ledger(), [])

    def test_a_transient_fab_accept_no_cannot_veto_the_owners_grant(self):
        """The POLARITY FLIP round two demanded, and the reason for it: a box
        that is momentarily busy or low on disk reads `no`, and round one let
        that silently void an explicit HELM_BOXES_JOB_CONSENT. The route now
        proceeds and SAYS what the inventory thinks."""
        result, err = self._route(self._happy(self._receipt()),
                                  nodes=[dict(CONSENTED, fab_accept="no")])
        self.assertIsNone(err, "an eligibility reading vetoed a real grant")
        self.assertEqual(result["verdict"], "imported")
        self.assertEqual(len(self._ledger()), 1)
        self.assertIn("eligibility proxy", result["eligibility"])
        self.assertIn("does not withhold a job you granted",
                      result["eligibility"])

    def test_the_eligibility_note_reaches_the_operators_surface(self):
        """A note nobody sees is not a note. The human arm prints it beside
        the evidence line; the JSON arm carries the same string."""
        row = self._receipt()
        with mock.patch.object(gateroute, "route", return_value=(
                {"receipt": row, "verdict": "imported", "box": "box-a",
                 "node": "box-a", "scratch": "tmpfs", "artifact": "/a.jsonl",
                 "cgroup": "delegated", "remote_rc": "0",
                 "eligibility": "fab says no, which is a disk reading"},
                None)):
            err_buf, out_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err_buf), \
                    contextlib.redirect_stdout(out_buf):
                gateroute.cmd_route("box-a", repo=self.repo)
            self.assertIn("fab says no, which is a disk reading",
                          err_buf.getvalue())
            out_buf = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(out_buf):
                gateroute.cmd_route("box-a", repo=self.repo, as_json=True)
            self.assertEqual(json.loads(out_buf.getvalue())["eligibility"],
                             "fab says no, which is a disk reading")


class IdentityResolutionTest(RouteBase):
    """Review finding 3: one canonical machine or none."""

    def test_a_name_matching_two_rows_refuses_as_ambiguous(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        rows = [dict(CONSENTED, host="box-a", ssh_host="alpha-ssh"),
                dict(CONSENTED, host="other", ssh_host="box-a")]
        transport = FakeTransport()
        result, err = self._route(transport, nodes=rows)
        self.assertIsNone(result)
        self.assertIn("AMBIGUOUS", err)
        self.assertIn("alpha-ssh", err)
        self.assertEqual(transport.calls, [])

    def test_a_label_is_display_not_address(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        rows = [dict(CONSENTED, host="other", ssh_host="other",
                     label="box-a")]
        transport = FakeTransport()
        result, err = self._route(transport, nodes=rows)
        self.assertIsNone(result)
        self.assertIn("not in the inventory", err)
        self.assertEqual(transport.calls, [])


class DirtyTreeTest(RouteBase):
    def test_a_dirty_worktree_refuses_before_anything_ships(self):  # noqa: VACUOUS_ASSERTION — the COMMITTED refusal text is the positive effect; empty calls/ledger are the nothing-shipped half, with HappyPathTest the unconditional positive control on both observables
        with open(os.path.join(self.repo, "f.txt"), "a") as f:
            f.write("uncommitted\n")
        transport = FakeTransport()
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("COMMITTED", err)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self._ledger(), [])

    def test_a_tree_without_helm_refuses_before_anything_ships(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        """Review finding 1 (live repro was `No module named helm` after a
        full transfer): the refusal moves HERE, named, with the alternative."""
        bare = os.path.join(self.tmp, "barerepo")
        os.makedirs(bare)
        _git(bare, "init", "-q", ".")
        with open(os.path.join(bare, "x.txt"), "w") as f:
            f.write("no helm here\n")
        _git(bare, "add", "x.txt")
        _git(bare, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "base")
        transport = FakeTransport()
        result, err = gateroute.route(repo=bare, box="box-a",
                                      transport=transport, nodes=[CONSENTED])
        self.assertIsNone(result)
        self.assertIn("no helm package", err)
        self.assertIn("fab gate", err)
        self.assertEqual(transport.calls, [])


class ShipmentTest(RouteBase):
    """What travels is the exact CAPTURED commit, streamed from disk."""

    def test_stdin_is_a_git_bundle_and_the_script_binds_head_and_tree(self):
        row = self._receipt()
        transport = self._happy(row)
        result, err = self._route(transport, label="parity probe")
        self.assertIsNone(err)
        call = transport.calls[0]
        self.assertTrue(call["stdin_head"].startswith(b"# v2 git bundle"),
                        "the shipment must be a bundle — a bare file copy "
                        "cannot reproduce the commit object")
        self.assertTrue(os.path.isabs(call["stdin_path"]))
        self.assertIn(self.head, call["script"])
        self.assertIn(self.tree, call["script"])
        self.assertIn("'parity probe'", call["script"])
        self.assertEqual(result["scratch"], "tmpfs")

    def test_the_bundle_pins_the_captured_sha_and_cleans_its_ref(self):  # noqa: VACUOUS_ASSERTION — the fetch-refspec assertIn is the positive control; the empty for-each-ref is the intentional no-leak absence
        """Review finding 9: the bundle rides a temp ref at the CAPTURED
        sha (`git bundle create` refuses a raw sha — probed), the far side
        fetches that refspec, and no refs/helm-job/* survives locally."""
        transport = self._happy(self._receipt())
        _result, err = self._route(transport)
        self.assertIsNone(err)
        script = transport.calls[0]["script"]
        self.assertIn("refs/helm-job/*:refs/helm-job/*", script)
        left = _git(self.repo, "for-each-ref", "refs/helm-job")
        self.assertEqual(left, "", "the job ref leaked: %r" % left)

    def test_two_routes_of_the_SAME_HEAD_never_share_a_bundle_path(self):
        """Second review finding 4. `ship-<head12>.bundle` is the SAME
        path for every concurrent route of one commit — and one commit is
        exactly what a fleet gates at once. The second route's create
        truncated, or its cleanup unlinked, a file the first was still
        streaming to ssh. Here the inner route runs and cleans up WHILE the
        outer session holds its bundle open as stdin; the outer file must
        still be there, and still be a bundle."""
        state = {}
        outer = None

        def _second_route_mid_transport():
            inner = FakeTransport(err="ssh died")
            self._route(inner)
            state["inner"] = inner.calls[0]["stdin_path"]
            path = outer.calls[0]["stdin_path"]
            state["alive"] = os.path.exists(path)
            with open(path, "rb") as fh:
                state["magic"] = fh.read(15)

        outer = FakeTransport(reply=self._reply(
            {"minted": True, "receipt": self._receipt()},
            receipts=[self._receipt()]), on_run=_second_route_mid_transport)
        result, err = self._route(outer)
        self.assertIsNone(err)
        self.assertEqual(result["verdict"], "imported")
        self.assertNotEqual(state["inner"], outer.calls[0]["stdin_path"])
        self.assertTrue(state["alive"],
                        "a concurrent route unlinked this route's live stdin")
        self.assertEqual(state["magic"], b"# v2 git bundle")
        # and both private dirs are gone afterwards, while the artifact the
        # route is FOR remains — cleanup is exact, not a scratch sweep
        left = [n for n in os.listdir(self.scratch_dir)
                if n.startswith(gateroute._BUNDLE_DIR_PREFIX)]
        self.assertEqual(left, [], "bundle dirs leaked: %s" % left)
        self.assertTrue(os.path.exists(result["artifact"]))

    def test_the_remote_command_is_the_trees_own_gate(self):  # noqa: VACUOUS_ASSERTION — intentional absence assertion — the assertIn on the routed script is the positive control; the AST assertNotIn pins cap-decided-remotely
        """Cap-respect by construction: the admission decision (suite_cap,
        PSI, FIFO) belongs to the box that owns the /proc it reads, so the
        routed command IS `helm gate run` there — and this module must not
        grow a local imitation of that check."""
        transport = self._happy(self._receipt())
        _result, err = self._route(transport)
        self.assertIsNone(err)
        script = transport.calls[0]["script"]
        self.assertIn(
            'stage "$cmd_env" "HELM_HOME=$dir/home" "$deadline_python"',
            script)
        self.assertIn('-m helm gate run --json', script)
        # the module may TALK about suite_cap in its docstring; it must not
        # TOUCH it — names and attributes are the honest surface, not prose
        import ast
        with open(gateroute.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        touched = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} \
            | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for local_check in ("suite_cap", "_admit_suite", "_acquire_gate"):
            self.assertNotIn(local_check, touched,
                             "the cap check must run THERE, not here")

    def test_the_script_carries_the_scratch_signal_and_substrate_laws(self):  # noqa: VACUOUS_ASSERTION — seven positive assertIns on the same script observable; nothing here asserts absence
        """Structural pins for findings 14 (absolute root), 15 (signals kill
        the gate child and exit signal-derived; the gate is backgrounded so
        sh traps can fire), 16 (absolute inode floor beside the percentage),
        and 17 (home creation failure is named)."""
        transport = self._happy(self._receipt())
        _result, err = self._route(transport)
        self.assertIsNone(err)
        script = transport.calls[0]["script"]
        self.assertIn('case "$root" in /*) : ;; *) root=/tmp ;; esac', script)
        self.assertIn("trap 'finish 15' TERM", script)
        self.assertIn("exit $((128+$1))", script)
        self.assertIn("gpid=$!", script)
        self.assertIn('wait "$gpid"', script)
        self.assertIn(str(gateroute.scratch.BIG_INODE_FLOOR), script)
        self.assertIn(
            'stage "$cmd_mkdir" -p "$dir/home" || err no-home', script)


class ClockTest(RouteBase):
    """Review findings 12 + 13: None is absent; zero is an immediate
    deadline; the session ceiling accounts for queue wait separately."""

    def test_timeout_zero_is_a_deadline_not_an_absence(self):  # noqa: VACUOUS_ASSERTION — the --timeout 0.0 assertIn and exact ceiling equality are the positive effects
        transport = self._happy(self._receipt())
        _result, err = self._route(transport, timeout=0)
        self.assertIsNone(err)
        call = transport.calls[0]
        self.assertIn("--timeout 0.0", call["script"])
        remote = (0.0 + gateroute._REMOTE_QUEUE_ALLOWANCE_S
                  + gateroute._TRANSPORT_SLACK_S)
        self.assertIn("ceiling, ready = float(sys.argv[3]), sys.argv[4]",
                      call["script"])
        self.assertIn('"$$" "$shell_start" %s "$deadline_ready" &' % remote,
                      call["script"])
        self.assertEqual(call["timeout"],
                         remote + gateroute._REMOTE_TEARDOWN_GRACE_S)

    def test_no_timeout_uses_the_default_ceiling_and_no_remote_flag(self):  # noqa: VACUOUS_ASSERTION — the exact default-ceiling equality is the positive control; the assertNotIn is the intentional flag absence
        transport = self._happy(self._receipt())
        _result, err = self._route(transport)
        self.assertIsNone(err)
        call = transport.calls[0]
        self.assertNotIn("--timeout", call["script"])
        self.assertIn("ceiling, ready = float(sys.argv[3]), sys.argv[4]",
                      call["script"])
        self.assertIn('"$$" "$shell_start" %s "$deadline_ready" &'
                      % gateroute._DEFAULT_SESSION_CEILING_S, call["script"])
        self.assertEqual(
            call["timeout"],
            gateroute._DEFAULT_SESSION_CEILING_S
            + gateroute._REMOTE_TEARDOWN_GRACE_S)


class HappyPathTest(RouteBase):
    def test_the_fetched_receipt_enters_the_ledger_verbatim(self):
        row = self._receipt()
        result, err = self._route(self._happy(row))
        self.assertIsNone(err)
        self.assertEqual(result["verdict"], "imported")
        self.assertEqual(result["node"], "box-a")
        stored = self._ledger()
        self.assertEqual(len(stored), 1)
        self.assertEqual(gateimport._canonical(stored[0]),
                         gateimport._canonical(row))
        with open(result["artifact"]) as f:
            self.assertEqual(json.loads(f.read().strip()), row)

    def test_a_replayed_identical_receipt_refuses_not_noops(self):  # noqa: VACUOUS_ASSERTION — the replay refusal text is the positive effect and ledger len==1 (not 0) is an unconditional non-empty read
        """Review finding 6 flipped this arm's polarity: an honest fresh run
        mints a NEW receipt (ts is in the content id), so byte-identity with
        the ledger means the box handed back an OLD artifact."""
        row = self._receipt()
        _r1, err1 = self._route(self._happy(row))
        self.assertIsNone(err1)
        result, err = self._route(self._happy(row))
        self.assertIsNone(result)
        self.assertIn("replayed artifact refused", err)
        self.assertEqual(len(self._ledger()), 1)


class ChallengeTest(RouteBase):
    """Review findings 6 + 18: the transcript must carry THIS invocation's
    random challenge, and marker-shaped payload cannot rewrite state."""

    def test_a_transcript_under_another_challenge_cannot_mint(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        row = self._receipt()
        stale = self._remote_out("f" * 32, {"minted": True, "receipt": row},
                                 receipts=[row])
        transport = FakeTransport(out=stale)
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("challenge", err)
        self.assertIn("replayed", err)
        self.assertEqual(self._ledger(), [])

    def test_a_forged_marker_inside_a_payload_is_data_not_protocol(self):
        """The first review's live repro: an ordinary refusal whose gate.err
        held a marker-looking line became a false no-git."""
        row = self._receipt()
        transport = self._happy(
            row, gate_err_lines=("::helm-job error=no-git",
                                 "::helm-job done"))
        result, err = self._route(transport)
        self.assertIsNone(err, "a payload line rewrote protocol state: %s"
                          % err)
        self.assertEqual(result["verdict"], "imported")


class PayloadFramingTest(RouteBase):
    """Second review finding 3: THE CHALLENGE IS NOT THE FRAMING.

    It rides in the remote `sh -c` argv, so the suite this very session is
    running can read it out of /proc/<ancestor>/cmdline and print it —
    randomness only stops a STALE transcript. What keeps payload out of
    control is structural: sections travel base64, a section's interior is
    data by position, and a control field is accepted exactly once."""

    def test_a_payload_line_carrying_THIS_challenge_stays_data(self):
        """The review's repro verbatim: a current-challenge `error=no-git`
        line emitted INSIDE a payload section. It must arrive as bytes."""
        row = self._receipt()
        forged = "{MARK}error=no-git\n{MARK}node=evil-box"
        transport = self._happy(
            row, gate_err_lines=("Traceback (most recent call last):",
                                 "{MARK}error=no-git", "{MARK}node=evil-box"))
        result, err = self._route(transport)
        self.assertIsNone(err, "a live-challenge payload line became "
                               "protocol: %s" % err)
        self.assertEqual(result["node"], "box-a")
        self.assertEqual(len(self._ledger()), 1)
        # and the same transcript through the parser: the bytes are PRESENT
        # and verbatim (data preserved), while no field was written by them
        challenge = _CHALLENGE_RE.search(
            transport.calls[0]["script"]).group(1)
        out = transport.reply(transport.calls[0]["script"])
        fields, sections, violation = gateroute._parse_markers(out, challenge)
        self.assertIsNone(violation)
        self.assertEqual(
            sections["gate-err"],
            "Traceback (most recent call last):\n"
            + forged.replace("{MARK}", "::helm-job-%s " % challenge))
        self.assertNotIn("error", fields)
        self.assertEqual(fields["node"], "box-a")

    def test_a_control_field_written_twice_refuses_the_transcript(self):  # noqa: VACUOUS_ASSERTION — the "written twice" refusal text is the positive effect; the empty ledger is the harm-not-done half, with HappyPathTest the unconditional positive control on the same read
        """The arm that catches a child writing straight to the session's
        stdout while the suite runs, where no section is open and position
        cannot help. The attack is worth spelling out: re-declare `node=` and
        the receipt-vs-channel host check compares two forgeries and agrees.
        Here the receipt is ABOUT evil-box, so under last-write-wins it would
        import — a receipt about a machine that never ran this job."""
        row = self._receipt(host={"node": "evil-box", "system": "Linux",
                                  "release": "6.8.0", "id": "cd" * 8})
        transport = self._happy(row, inject=("{MARK}node=evil-box",))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("written twice", err)
        self.assertIn("not a framed session", err)
        self.assertEqual(self._ledger(), [],
                         "a re-declared node bound a foreign receipt")

    def test_an_unframed_section_body_refuses_instead_of_being_read(self):  # noqa: VACUOUS_ASSERTION — the base64-framed refusal text is the positive effect; the empty ledger is the nothing-imported half, with HappyPathTest the unconditional positive control on the same read
        """Encoding IS the contract, so a body that did not arrive encoded
        is a session this module cannot separate payload from control in."""
        row = self._receipt()
        transport = self._happy(row, frame=False)
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("did not arrive base64-framed", err)
        self.assertEqual(self._ledger(), [])

    def test_a_marker_INTERLEAVED_into_a_section_body_is_body(self):  # noqa: VACUOUS_ASSERTION — the framing refusal text is the positive effect; the empty ledger is the nothing-imported half, with HappyPathTest the unconditional positive control on the same read
        """Position, the second rung, is what decides this one. A writer that
        is not the script drops a live-challenge line INTO an open section:
        read by position it is body, so the body stops being valid base64 and
        the session refuses as unframed. Read as control it would instead be
        believed — `error=no-git` would have helm report a substrate fault on
        a box whose substrate is fine, which is a lie about someone else's
        machine, delivered with a straight face."""
        row = self._receipt()
        transport = self._happy(row, gate_err_raw=("{MARK}error=no-git",))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("did not arrive base64-framed", err)
        self.assertNotIn("no git on PATH", err)
        self.assertEqual(self._ledger(), [])

    def test_a_section_opened_twice_refuses(self):  # noqa: VACUOUS_ASSERTION — the opened-twice refusal text is the positive effect; the empty ledger is the nothing-imported half, with HappyPathTest the unconditional positive control on the same read
        row = self._receipt()
        transport = self._happy(
            row, inject=("{MARK}gate-json-begin", "{MARK}gate-json-end"))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("opened twice", err)
        self.assertEqual(self._ledger(), [])


class NodeBindingTest(RouteBase):
    """Review finding 5: the receipt is about the machine this session
    actually spoke to — its uname over the same channel — or it refuses."""

    def test_a_receipt_about_another_machine_refuses(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        row = self._receipt(host={"node": "unexpected-box", "system": "Linux",
                                  "release": "6.8.0", "id": "cd" * 8})
        result, err = self._route(self._happy(row))
        self.assertIsNone(result)
        self.assertIn("unexpected-box", err)
        self.assertIn("box-a", err)
        self.assertEqual(self._ledger(), [])

    def test_a_session_that_never_names_its_node_refuses(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        row = self._receipt()
        result, err = self._route(self._happy(row, node=None))
        self.assertIsNone(result)
        self.assertIn("node identity", err)
        self.assertEqual(self._ledger(), [])


class CallerTreeMovedTest(RouteBase):
    """Review finding 7: the exit code is read as 'what I am standing on
    passed'; a worktree that moved mid-run must not get that reading."""

    def test_a_dirtying_edit_during_transport_refuses_the_import(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        row = self._receipt()

        def dirty_now():
            with open(os.path.join(self.repo, "f.txt"), "a") as f:
                f.write("edited while the suite ran remotely\n")

        transport = FakeTransport(reply=self._reply(
            {"minted": True, "receipt": row}, receipts=[row]),
            on_run=dirty_now)
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("MOVED during the remote run", err)
        self.assertEqual(self._ledger(), [])


class ArtifactTest(RouteBase):
    """Review finding 11: no error path leaves a partial provenance-shaped
    artifact behind."""

    def test_a_failed_write_leaves_no_artifact_and_no_temp(self):  # noqa: VACUOUS_ASSERTION — the cannot-write error text is the positive effect; empty leftovers IS the atomicity claim, with HappyPathTest proving the same dir non-empty on success
        row = self._receipt()
        with mock.patch("os.replace",
                        side_effect=OSError("\x1b[31mdisk full\x1b[0m")):
            result, err = self._route(self._happy(row))
        self.assertIsNone(result)
        self.assertIn("cannot write the fetched artifact", err)
        self.assertIn("disk full", err)
        self.assertNotIn("\x1b", err)
        leftovers = [fn for fn in os.listdir(self.scratch_dir)
                     if fn.endswith((".jsonl", ".tmp"))]
        self.assertEqual(leftovers, [])
        self.assertEqual(self._ledger(), [])


class FailedSuiteHonestyTest(RouteBase):
    def _failed_row(self):
        return self._receipt(
            status="FAILED", rc=1, detail="failures=1",
            failures=[{"kind": "FAIL", "test": "tests.test_x.T.test_y",
                       "traceback": "AssertionError: no"}],
            base_check={"verdict": "LANE_OWNED", "reason": "both fail here"})

    def test_a_failed_suite_imports_honestly(self):
        """A red suite is a routing SUCCESS: the receipt lands in the ledger
        saying FAILED, instead of being dropped as if the job never ran."""
        row = self._failed_row()
        result, err = self._route(self._happy(row, gate_rc=1))
        self.assertIsNone(err)
        self.assertEqual(result["verdict"], "imported")
        stored = self._ledger()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "FAILED")
        self.assertEqual(stored[0]["id"], row["id"])

    def test_the_cli_exit_stays_bind_derived_for_a_routed_failed(self):  # noqa: VACUOUS_ASSERTION — rc==1 plus the gate:<id> evidence line on stdout are the positive effects asserted
        row = self._failed_row()
        result, err = self._route(self._happy(row, gate_rc=1))
        self.assertIsNone(err)
        with mock.patch.object(gateroute, "route",
                               return_value=(result, None)):
            out, errout = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(errout):
                rc = gateroute.cmd_route("box-a", repo=self.repo)
        self.assertEqual(rc, 1, "an imported FAILED binds nothing, so the "
                                "routed exit must refuse like the local one")
        self.assertIn("gate:%s" % row["id"], out.getvalue())


class RemoteRefusalTest(RouteBase):
    def test_a_remote_gate_refusal_is_relayed_and_nothing_imports(self):  # noqa: VACUOUS_ASSERTION — the relayed remote reason is the positive effect; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        reason = ("the box is running 2 whole-suite gates "
                  "(cap 2 here: agent panes live on this box)")
        transport = FakeTransport(reply=self._reply(
            {"minted": False, "reason": reason}, gate_rc=1))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn(reason, err)
        self.assertEqual(self._ledger(), [])

    def test_a_missing_remote_substrate_is_named(self):  # noqa: VACUOUS_ASSERTION — the named substrate error is the positive effect; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        transport = FakeTransport(reply=self._reply(error="no-git"))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("no git on PATH", err)
        self.assertEqual(self._ledger(), [])

    def test_a_deadline_helper_failure_preserves_its_named_detail(self):  # noqa: VACUOUS_ASSERTION — generic deadline reason plus exact helper detail are unconditional positive controls; empty ledger is the nothing-imported half
        transport = FakeTransport(reply=self._reply(
            error="deadline-unarmed:no-pidfd"))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("deadline helper did not become ready", err)
        self.assertIn("no-pidfd", err)
        self.assertEqual(self._ledger(), [])

    def test_a_clone_failure_carries_the_git_diagnostic(self):  # noqa: VACUOUS_ASSERTION — the named refusal text is the positive effect; the empty calls/ledger reads are the nothing-shipped/nothing-imported half, with HappyPathTest the unconditional positive control on the same observables
        """Review finding 19: ENOSPC and a bad object format must not both
        collapse to 'clone-failed'."""
        transport = FakeTransport(reply=self._reply(
            error="clone-failed:fatal: write error: No space left on device"))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("No space left on device", err)
        self.assertEqual(self._ledger(), [])

    def test_a_wrong_clone_refuses_before_anything_ran(self):  # noqa: VACUOUS_ASSERTION — both shas in the refusal text are the positive effect; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        transport = FakeTransport(reply=self._reply(
            error="head-mismatch:" + "c" * 40))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("c" * 40, err)
        self.assertIn(self.head, err)
        self.assertEqual(self._ledger(), [])


class RemoteValueSafetyTest(RouteBase):
    def test_a_remote_receipt_id_must_be_canonical_before_it_names_a_path(self):  # noqa: VACUOUS_ASSERTION — the named canonical-id refusal is the positive effect; absent controls and empty ledger prove no raw terminal bytes or artifact import, with HappyPathTest controlling successful import
        """The NUL/control-string edge found beside final review finding 4:
        remote JSON is data until validated, never a filename or terminal row."""
        row = self._receipt()
        row["id"] = "abcd\x00\x1b[31m"
        result, err = self._route(self._happy(row))
        self.assertIsNone(result)
        self.assertIn("invalid receipt id", err)
        self.assertNotIn("\x00", err)
        self.assertNotIn("\x1b", err)
        self.assertEqual(self._ledger(), [])

    def test_import_refusal_text_is_laundered_at_the_route_sink(self):  # noqa: VACUOUS_ASSERTION — preserved refusal text is the positive effect; absent ESC and empty ledger prove laundering and no import, with HappyPathTest controlling successful import
        row = self._receipt()
        with mock.patch.object(
                gateimport, "import_receipt",
                return_value=(None, None, "\x1b[31mremote refusal\x1b[0m")):
            result, err = self._route(self._happy(row))
        self.assertIsNone(result)
        self.assertIn("remote refusal", err)
        self.assertNotIn("\x1b", err)
        self.assertEqual(self._ledger(), [])


class TransportFailureTest(RouteBase):
    def test_an_ssh_failure_names_the_box_and_imports_nothing(self):  # noqa: VACUOUS_ASSERTION — box+exit+stderr in the error are the positive effects; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        transport = FakeTransport(rc=255,
                                  errout="Permission denied (publickey)")
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("box-a", err)
        self.assertIn("255", err)
        self.assertIn("Permission denied", err)
        self.assertEqual(self._ledger(), [])

    def test_a_session_that_dies_midway_is_not_a_result(self):  # noqa: VACUOUS_ASSERTION — the DONE-marker error text is the positive effect; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        def half(script):
            challenge = _CHALLENGE_RE.search(script).group(1)
            return "::helm-job-%s scratch=tmpfs\n" % challenge
        transport = FakeTransport(reply=half)
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("DONE marker", err)
        self.assertEqual(self._ledger(), [])

    def test_a_transport_error_is_named_with_its_box(self):  # noqa: VACUOUS_ASSERTION — the box-naming error text is the positive effect; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        transport = FakeTransport(err="ssh session to box-a exceeded its "
                                      "3600s ceiling")
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("box-a", err)
        self.assertEqual(self._ledger(), [])

    def test_a_remote_gate_that_prints_no_json_is_an_error_not_a_pass(self):  # noqa: VACUOUS_ASSERTION — the did-not-answer error text is the positive effect; the empty ledger is the nothing-imported half, HappyPathTest proves the same read non-empty
        transport = FakeTransport(reply=self._reply(gate_json=None))
        result, err = self._route(transport)
        self.assertIsNone(result)
        self.assertIn("did not answer", err)
        self.assertEqual(self._ledger(), [])


@unittest.skipUnless(os.path.exists("/bin/sh") and shutil.which("base64")
                     and shutil.which("setsid"),
                     "needs a POSIX sh, base64, and setsid to run the far side")
class RemoteScriptShellTest(unittest.TestCase):
    """THE FAR SIDE, EXECUTED. Every arm above scripts a transcript; these
    run the real `_remote_script` under a real /bin/sh with STUB binaries on
    PATH, so what the parser reads is what the script actually emits. No
    network, no box, no real host name in any assertion — head/tree/challenge
    are synthetic and the scratch root is this test's own tmp."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-job-sh-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.root = os.path.join(self.tmp, "scratchroot")
        os.makedirs(self.bin)
        os.makedirs(self.root)
        self.head, self.tree, self.challenge = "a" * 40, "b" * 40, "c" * 32
        # /dev/shm is a real mount under the runner, and the script prefers it
        # when it has headroom. A df stub reporting it full keeps the job dir
        # inside this test's tmp where these arms can watch it; the tmpfs
        # branch itself is pinned structurally by ShipmentTest.
        self._stub("df", "#!/bin/sh\nprintf 'H\\nx 1 1 1 99%% /dev/shm\\n'\n")
        # a delegated scope would be a real side effect on the runner's
        # systemd session; the direct arm is the one under test here
        self._stub("systemd-run", "#!/bin/sh\nexit 1\n")

    def _stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def _stub_python(self, gate_body):
        """One PATH interpreter serves both isolated helper and shipped gate.

        The wrapper execs the real interpreter for the helper argv and keeps the
        existing shell body for `-m helm`, so tests can stub gate behavior while
        production still proves PATH resolution, readiness, and cancellation."""
        body = ("#!/bin/sh\n"
                "if [ \"$1\" = -I ] && [ \"$2\" = -S ] && "
                "[ \"$3\" = -c ]; then exec %s \"$@\"; fi\n%s"
                % (shlex.quote(sys.executable), gate_body))
        return self._stub("python3", body)

    def test_deadline_helper_requires_each_isolation_flag(self):  # noqa: VACUOUS_ASSERTION — positive and two negative interpreter-flag arms bind both switches, then production script inspection proves that exact isolated helper is launched
        guard = ("import sys; raise SystemExit(0 if sys.flags.isolated and "
                 "sys.flags.no_site and 'site' not in sys.modules else 70)")
        cases = (("isolated and no-site", ["-I", "-S"], 0),
                 ("missing isolated", ["-S"], 70),
                 ("missing no-site", ["-I"], 70))
        for label, flags, expected in cases:
            with self.subTest(label=label):
                proc = subprocess.run(
                    [sys.executable, *flags, "-c", guard],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(proc.returncode, expected, proc.stderr)
        script = gateroute._remote_script(
            self.head, self.tree, None, None, self.challenge)
        self.assertIn('"$deadline_setsid" "$deadline_python" -I -S -c',
                      script)
        self.assertIn("sys.flags.isolated", script)
        self.assertIn("os.pidfd_open", script)
        self.assertIn("signal.pidfd_send_signal(target_fd, signal.SIGTERM)",
                      script)

    def test_watchdog_readiness_precedes_scratch_and_substrate(self):  # noqa: VACUOUS_ASSERTION — named refusal, exact statement order, and empty root bind both the fail-closed surface and absence of pre-ack artifacts
        self._stub("python3", "#!/bin/sh\nexit 70\n")
        script = gateroute._remote_script(
            self.head, self.tree, None, None, self.challenge)
        ready = script.index('[ "$ready_v" = v1 ]')
        self.assertLess(ready, script.index("resolve_stage git"))
        self.assertLess(ready, script.index('stage "$cmd_mktemp"'))
        attempts = int(gateroute._DEADLINE_READY_TIMEOUT_S
                       / gateroute._DEADLINE_READY_POLL_S)
        self.assertIn('[ "$n" -lt %d ]' % attempts, script)
        self.assertIn('stage "$deadline_sleep" %r'
                      % gateroute._DEADLINE_READY_POLL_S, script)
        self.assertIn("same-UID isolation is not a", script)
        proc = self._run(b"")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields, _sections, violation = gateroute._parse_markers(
            proc.stdout.decode("utf-8"), self.challenge)
        self.assertIsNone(violation)
        self.assertEqual(fields.get("error"), "deadline-unarmed")
        self.assertEqual(os.listdir(self.root), [],
                         "an unacknowledged watchdog left remote scratch")

    def test_loader_and_python_environment_is_cleared_before_both_runs(self):  # noqa: VACUOUS_ASSERTION — helper and gate wrappers each record five absent variables, while a complete framed session proves both invocations ran
        self._stub("git", "#!/bin/sh\ncase \"$*\" in\n"
                          "*'HEAD^{tree}'*) printf '%%s\\n' %s ;;\n"
                          "*rev-parse*) printf '%%s\\n' %s ;;\n"
                          "esac\nexit 0\n" % (self.tree, self.head))
        helper_env = os.path.join(self.tmp, "helper-env")
        gate_env = os.path.join(self.tmp, "gate-env")
        check = ('printf "%%s %%s %%s %%s %%s\\n" '
                 '"${LD_PRELOAD+x}" "${LD_AUDIT+x}" '
                 '"${LD_LIBRARY_PATH+x}" "${PYTHONPATH+x}" '
                 '"${PYTHONHOME+x}" > %s')
        gate_body = (check % shlex.quote(gate_env)) + "\nexit 0\n"
        body = ("#!/bin/sh\n"
                "if [ \"$1\" = -I ] && [ \"$2\" = -S ] && "
                "[ \"$3\" = -c ]; then\n  %s\n  exec %s \"$@\"\nfi\n%s"
                % (check % shlex.quote(helper_env),
                   shlex.quote(sys.executable), gate_body))
        self._stub("python3", body)
        proc = subprocess.run(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge)],
            input=b"# v2 git bundle\n", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root, LD_PRELOAD="", LD_AUDIT="",
                     LD_LIBRARY_PATH="/hostile", PYTHONPATH="/hostile",
                     PYTHONHOME="/hostile"))
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        for path in (helper_env, gate_env):
            with open(path) as fh:
                self.assertEqual(fh.read(), "    \n")
        fields, _sections, violation = gateroute._parse_markers(
            proc.stdout.decode("utf-8"), self.challenge)
        self.assertIsNone(violation)
        self.assertIs(fields.get("done"), True)

    def _run(self, stdin_bytes, timeout=60):
        proc = subprocess.run(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge)],
            input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        return proc

    def _jobs(self):
        """Actual job scratch, excluding watchdog readiness/probe files."""
        return [n for n in os.listdir(self.root)
                if n.startswith("helm-job.")]

    def test_a_TERM_while_the_bundle_read_blocks_kills_the_whole_session(self):  # noqa: VACUOUS_ASSERTION — a live helm-job scratch dir is the positive control before TERM; rc=143 and the same root empty afterward bind cleanup
        """Second review finding 5, the live repro made hermetic: sh
        defers a trapped signal until the FOREGROUND child exits, so a TERM
        arriving while `cat` waited on a stdin that never ends was never
        delivered — the session and its scratch stayed alive on the box, and
        the only cure left was killing it by hand. Backgrounded and tracked,
        the trap fires at once."""
        self._stub("git", "#!/bin/sh\nexit 0\n")
        self._stub_python("exit 0\n")
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        # held open for WRITING and never written: the bundle read blocks
        # exactly as it does against a stalled ssh channel
        holder = os.open(fifo, os.O_RDWR)
        self.addCleanup(os.close, holder)
        reader = os.open(fifo, os.O_RDONLY)
        proc = subprocess.Popen(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge)],
            stdin=reader, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        os.close(reader)
        deadline = time.time() + 30
        jobs = []
        while not jobs and time.time() < deadline:
            jobs = self._jobs()
            time.sleep(0.02)
        self.assertEqual(len(jobs), 1,
                         "the session never reached its scratch dir")
        proc.terminate()
        rc = proc.wait(timeout=30)
        # 143, not -15: the shell EXITED through its own trap (a signal death
        # would report -15 and prove the trap never ran)
        self.assertEqual(rc, 143)
        self.assertEqual(os.listdir(self.root), [],
                         "the killed session left its scratch on the box")

    def test_the_far_side_deadline_cleans_when_no_local_signal_arrives(self):  # noqa: VACUOUS_ASSERTION — the hostile-site positive control, live scratch before expiry, rc=143, absent startup marker, and empty scratch prove the isolated watchdog fired and cleaned after setup
        """Final review finding 1: sshd's non-pty child survives the local ssh
        client. The remote clock must therefore fire without any signal from
        this process, kill the blocked stage, and remove scratch itself. Its
        startup is isolated so ambient customization cannot fork first."""
        self._stub("git", "#!/bin/sh\nexit 0\n")
        helper_marker = os.path.join(self.tmp, "path-helper-ran")
        gate_marker = os.path.join(self.tmp, "path-gate-ran")
        self._stub(
            "python3", "#!/bin/sh\n"
            "if [ \"$1\" = -I ] && [ \"$2\" = -S ] && "
            "[ \"$3\" = -c ]; then\n"
            "  printf ran > %s\n  exec %s \"$@\"\nfi\n"
            "printf ran > %s\nexit 0\n"
            % (shlex.quote(helper_marker), shlex.quote(sys.executable),
               shlex.quote(gate_marker)))
        real = sys.executable
        self.assertTrue(os.path.isfile(real) and os.access(real, os.X_OK),
                        "the fixture found no executable Python")
        site = os.path.join(self.tmp, "hostile-site")
        os.makedirs(site)
        site_marker = os.path.join(self.tmp, "sitecustomize-ran")
        signal_marker = os.path.join(self.tmp, "shadow-signal-ran")
        with open(os.path.join(site, "sitecustomize.py"), "w") as fh:
            fh.write("with open(%r, 'w') as f: f.write('ran')\n" % site_marker)
        with open(os.path.join(site, "signal.py"), "w") as fh:
            fh.write("with open(%r, 'w') as f: f.write('ran')\n"
                     % signal_marker)
        hostile_env = dict(os.environ, PYTHONPATH=site)
        subprocess.run([real, "-c", "pass"], check=True, env=hostile_env)
        self.assertTrue(os.path.exists(site_marker),
                        "the hostile site control did not execute")
        os.unlink(site_marker)
        subprocess.run([real, "-S", "-c", "import signal"], check=True,
                       env=hostile_env)
        self.assertTrue(os.path.exists(signal_marker),
                        "the hostile import-path control did not execute")
        os.unlink(signal_marker)
        fifo = os.path.join(self.tmp, "deadline-fifo")
        os.mkfifo(fifo)
        holder = os.open(fifo, os.O_RDWR)
        self.addCleanup(os.close, holder)
        reader = os.open(fifo, os.O_RDONLY)
        proc = subprocess.Popen(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge,
                session_ceiling=1.0)],
            stdin=reader, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(hostile_env, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        os.close(reader)
        deadline = time.time() + 0.75
        jobs = []
        while not jobs and time.time() < deadline:
            jobs = self._jobs()
            time.sleep(0.01)
        self.assertEqual(len(jobs), 1,
                         "the session never reached scratch before its deadline")
        self.assertEqual(proc.wait(timeout=5), 143)
        self.assertFalse(os.path.exists(site_marker),
                         "deadline helper ran site customization")
        self.assertFalse(os.path.exists(signal_marker),
                         "deadline helper trusted ambient import paths")
        self.assertTrue(os.path.exists(helper_marker),
                        "deadline helper bypassed the PATH interpreter")
        self.assertFalse(os.path.exists(gate_marker),
                         "the blocked bundle unexpectedly reached the gate")
        self.assertEqual(os.listdir(self.root), [],
                         "far-side deadline left scratch after client silence")

    def test_the_far_side_deadline_cancels_a_blocking_uname_stage(self):  # noqa: VACUOUS_ASSERTION — MUST-HIT marker plus live helm-job scratch prove entry; rc=143, bounded wait, and empty root prove deadline cancellation and cleanup
        """Every post-launch executable belongs to the tracked setsid owner.

        The reviewed tip ran ``uname`` in command substitution. Dash deferred
        its TERM trap while that foreground child waited, so the pidfd watchdog
        fired but the shell and scratch survived. This wrapper ignores TERM and
        never returns: the marker and live job dir are positive controls that
        the exact old failure window was reached before the deadline kills its
        whole group and the shell removes scratch."""
        self._stub("git", "#!/bin/sh\nexit 0\n")
        self._stub_python("exit 0\n")
        entered = os.path.join(self.tmp, "blocking-uname-entered")
        self._stub(
            "uname", "#!/bin/sh\n"
            "printf entered > %s\n"
            "trap '' TERM\n"
            "while :; do sleep 1; done\n" % shlex.quote(entered))
        proc = subprocess.Popen(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge,
                session_ceiling=0.4)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        proc.stdin.write(b"# v2 git bundle\n")
        proc.stdin.close()
        deadline = time.time() + 5
        jobs = []
        while time.time() < deadline:
            jobs = self._jobs()
            if os.path.exists(entered) and jobs:
                break
            time.sleep(0.01)
        self.assertTrue(os.path.exists(entered),
                        "MUST-HIT: the blocking uname stage was never entered")
        self.assertEqual(len(jobs), 1,
                         "the blocking uname stage lacked live job scratch")
        started = time.monotonic()
        self.assertEqual(proc.wait(timeout=5), 143,
                         "the watchdog did not terminate the protocol shell")
        self.assertLess(time.monotonic() - started, 4.0,
                        "deadline cleanup waited for the blocking uname")
        self.assertEqual(os.listdir(self.root), [],
                         "blocking uname left watchdog metadata or scratch")

    def test_SIGPIPE_runs_cleanup_when_the_reader_disappears(self):  # noqa: VACUOUS_ASSERTION — rc=141 proves the shell reached a post-mktemp protocol write and exited through finish(13); empty scratch is the harm-gone assertion
        """Final review finding 2: closing the ssh reader used to kill sh with
        raw SIGPIPE (-13), bypass EXIT and strand the checkout. PIPE is a named
        trap now, so the shell exits 141 through finish and scratch is empty."""
        self._stub("git", "#!/bin/sh\nexit 0\n")
        self._stub_python("exit 0\n")
        proc = subprocess.Popen(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        proc.stdout.close()
        proc.stdin.write(b"# v2 git bundle\n")
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 141,
                         "PIPE bypassed the shell cleanup trap")
        self.assertEqual(os.listdir(self.root), [],
                         "SIGPIPE left remote scratch behind")

    def test_TERM_in_the_launcher_start_tick_gap_is_bounded(self):  # noqa: VACUOUS_ASSERTION — live launcher marker, forced empty deadline_start, rc=143, bounded elapsed, dead launcher, and empty root prove the exact gap is canceled
        """Cancellation must not wait for the start-tick read.

        `$!` is an unreaped child and therefore cannot recycle. This harness
        uses the production script but deterministically replaces the two-line
        start-tick capture with a wait for a live launcher marker while
        ``deadline_start`` remains empty, then signals the shell. The old guard
        skipped the kill and waited forever for this TERM-ignoring launcher."""
        launcher_at = os.path.join(self.tmp, "gap-launcher")
        self._stub(
            "python3", "#!/bin/sh\n"
            "if [ \"$1\" = -I ] && [ \"$2\" = -S ] && "
            "[ \"$3\" = -c ]; then\n"
            "  printf '%%s\\n' \"$$\" > %s\n"
            "  trap '' TERM\n"
            "  while :; do sleep 1; done\n"
            "fi\nexit 0\n" % shlex.quote(launcher_at))
        script = gateroute._remote_script(
            self.head, self.tree, None, None, self.challenge,
            session_ceiling=60.0)
        capture = ('deadline_pid=$!\n'
                   'proc_start "$deadline_pid" || err deadline-unarmed\n'
                   'deadline_start=$proc_start_value\n')
        forced_gap = (
            'deadline_pid=$!\n'
            'deadline_start=""\n'
            'gap_n=0\n'
            'while [ ! -s %s ] && [ "$gap_n" -lt 200 ]; do\n'
            '  stage "$deadline_sleep" 0.01\n'
            '  gap_n=$((gap_n+1))\n'
            'done\n'
            '[ -s %s ] || exit 70\n'
            'kill -TERM "$$"\n'
            % (shlex.quote(launcher_at), shlex.quote(launcher_at)))
        self.assertIn(capture, script,
                      "the test no longer targets the launcher capture gap")
        script = script.replace(capture, forced_gap, 1)
        proc = subprocess.Popen(
            ["/bin/sh", "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        self.addCleanup(proc.stdin.close)
        self.addCleanup(proc.stdout.close)
        started = time.monotonic()
        self.assertEqual(proc.wait(timeout=3), 143,
                         "the empty-start-tick gap waited on the launcher")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(os.path.exists(launcher_at),
                        "MUST-HIT: the live gap launcher never started")
        with open(launcher_at) as fh:
            launcher = int(fh.read().strip())
        try:
            with open("/proc/%d/stat" % launcher) as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            state = None
        self.assertIn(state, (None, "Z", "X"),
                      "the gap launcher survived in state %r" % state)
        self.assertEqual(os.listdir(self.root), [],
                         "the launcher gap left metadata or scratch")

    def test_TERM_before_helper_readiness_kills_the_wrapper_group(self):  # noqa: VACUOUS_ASSERTION — live launcher+child generation/group controls, rc=143, elapsed bound, dead processes, and absent scratch prove cancellation bounds the pre-PDEATHSIG wrapper race
        """A PATH wrapper may fork before the Python helper can arm PDEATHSIG.

        Hold that child in-process before it execs Python. The stable launcher
        PID plus its dedicated setsid group must bound both processes even
        though no readiness record exists yet; readiness-before-scratch also
        means this failure window leaves no shipped artifact to clean."""
        self._stub("git", "#!/bin/sh\nexit 0\n")
        ready = os.path.join(self.tmp, "deadline-wrapper-pids")
        publishing = ready + ".tmp"
        held = self._stub(
            "held-helper", "#!%s\nimport os\nimport sys\nimport time\n"
            "time.sleep(30)\nos.execv(%r, [%r] + sys.argv[1:])\n"
            % (sys.executable, sys.executable, sys.executable))
        self._stub(
            "python3", "#!/bin/sh\n"
            "if [ \"$1\" = -I ] && [ \"$2\" = -S ] && "
            "[ \"$3\" = -c ]; then\n"
            "  %s \"$@\" & child=$!\n"
            "  printf '%%s %%s\\n' \"$$\" \"$child\" > %s\n"
            "  mv %s %s\n  wait \"$child\"\n  exit $?\nfi\nexit 0\n"
            % (shlex.quote(held), shlex.quote(publishing),
               shlex.quote(publishing), shlex.quote(ready)))
        proc = subprocess.Popen(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge,
                session_ceiling=60.0)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        self.addCleanup(proc.stdin.close)
        self.addCleanup(proc.stdout.close)
        deadline = time.time() + 5
        while not os.path.exists(ready) and time.time() < deadline:
            time.sleep(0.005)
        self.assertTrue(os.path.exists(ready),
                        "the PATH wrapper never exposed its pre-helper race")
        with open(ready) as fh:
            launcher, child = (int(v) for v in fh.read().split())

        def identity(pid):
            """(start tick, state, pgrp), or None."""
            try:
                with open("/proc/%d/stat" % pid) as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                return None
            return fields[19], fields[0], int(fields[2])

        before = {pid: identity(pid) for pid in (launcher, child)}
        self.assertTrue(all(v and v[1] not in ("Z", "X")
                            for v in before.values()),
                        "the wrapper race was not live before TERM")
        self.assertEqual({v[2] for v in before.values()}, {launcher},
                         "the launcher and pre-helper child lacked one group")
        self.assertEqual(os.listdir(self.root), [],
                         "scratch was allocated before watchdog readiness")
        started = time.monotonic()
        proc.terminate()
        self.assertEqual(proc.wait(timeout=3), 143,
                         "TERM bypassed or blocked the shell cleanup trap")
        self.assertLess(time.monotonic() - started, 2.0,
                        "cleanup waited for the unready PATH wrapper")
        for pid, original in before.items():
            after = identity(pid)
            if after and after[0] == original[0]:
                self.assertIn(after[1], ("Z", "X"),
                              "deadline process %d survived in state %s"
                              % (pid, after[1]))
        self.assertEqual(os.listdir(self.root), [],
                         "unready watchdog cancellation left scratch behind")

    def test_TERM_escalates_to_KILL_for_the_whole_uncooperative_gate_group(self):  # noqa: VACUOUS_ASSERTION — live pre-TERM pid+start/state controls and rc=143 prove the hostile group/trap ran; empty scratch plus absent/dead same-generation processes are the harm-gone half
        """The finder-cure control the blocked-cat arm could not provide.

        `cat` exits on TERM, so that test proved prompt trap delivery but not
        bounded cleanup of arbitrary suite code. This gate leader AND its
        descendant ignore TERM. The remote shell must still exit through its
        trap, both pids must die, and scratch must be gone — a single-pid TERM
        followed by an unbounded wait fails all three."""
        gate_pid = os.path.join(self.tmp, "gate-pid")
        child_pid = os.path.join(self.tmp, "child-pid")
        self._stub("git", "#!/bin/sh\ncase \"$*\" in\n"
                          "*'HEAD^{tree}'*) printf '%%s\\n' %s ;;\n"
                          "*rev-parse*) printf '%%s\\n' %s ;;\n"
                          "esac\nexit 0\n" % (self.tree, self.head))
        self._stub_python(
            "printf '%%s\\n' \"$$\" > %s\n"
            "trap '' TERM\n"
            "(trap '' TERM; while :; do sleep 1; done) &\n"
            "printf '%%s\\n' \"$!\" > %s\n"
            "while :; do sleep 1; done\n"
            % (shlex.quote(gate_pid), shlex.quote(child_pid)))
        proc = subprocess.Popen(
            ["/bin/sh", "-c", gateroute._remote_script(
                self.head, self.tree, None, None, self.challenge)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, PATH=self.bin + ":/usr/bin:/bin",
                     TMPDIR=self.root))
        self.addCleanup(_reap, proc)
        proc.stdin.write(b"# v2 git bundle\n")
        proc.stdin.close()
        deadline = time.time() + 30
        while (not os.path.exists(child_pid) or not self._jobs()) \
                and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(child_pid),
                        "the session never reached the hostile gate")
        with open(gate_pid) as fh:
            gate = int(fh.read().strip())
        with open(child_pid) as fh:
            child = int(fh.read().strip())

        def identity(pid):
            """(start tick, state), or None; zombies are dead but still named."""
            try:
                with open("/proc/%d/stat" % pid) as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                return None
            return fields[19], fields[0]

        before = {pid: identity(pid) for pid in (gate, child)}
        self.assertTrue(all(v and v[1] not in ("Z", "X")
                            for v in before.values()),
                        "the hostile gate group was not live before TERM")
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 143,
                         "TERM cleanup waited forever on an uncooperative gate")
        self.assertEqual(os.listdir(self.root), [],
                         "forced gate cleanup left its scratch on the box")
        for pid, original in before.items():
            after = identity(pid)
            if after and after[0] == original[0]:
                self.assertIn(after[1], ("Z", "X"),
                              "gate process %d survived cleanup in state %s"
                              % (pid, after[1]))

    def test_the_far_side_emits_a_framed_session_the_parser_reads(self):  # noqa: VACUOUS_ASSERTION — six unconditional equalities on the SAME transcript (shipped byte count, the exact field set, scratch/cgroup/gate-rc values, both decoded section bodies) are the positive controls; the `error` assertNotIn and the empty scratch root are the intentional absences beside them
        """One end-to-end pass over the wire format: the stdin bytes reach
        the BACKGROUNDED bundle read (a POSIX shell gives an async command
        /dev/null unless the redirection is explicit — measured, and the
        reason for `exec 3<&0`), the payload sections arrive base64, and a
        gate that prints a CURRENT-CHALLENGE marker line to stderr — which it
        can, by reading the challenge from /proc — lands as data."""
        payload = b"# v2 git bundle\n" + b"x" * 4096
        size_at = os.path.join(self.tmp, "bundle-size")
        # $2 is the -C directory ($dir/tree), so the shipped bundle beside it
        # is reachable without depending on git's argument order
        self._stub("git", "#!/bin/sh\ncase \"$*\" in\n"
                          "*fetch*) wc -c < \"$2/../ship.bundle\" > %s ;;\n"
                          "*'HEAD^{tree}'*) printf '%%s\\n' %s ;;\n"
                          "*rev-parse*) printf '%%s\\n' %s ;;\n"
                          "esac\nexit 0\n"
                          % (size_at, self.tree, self.head))
        verdict = {"minted": True, "receipt": {"id": "r1"}}
        receipt_line = '{"id": "r1", "status": "OK"}'
        forged = "::helm-job-%s error=no-git" % self.challenge
        self._stub_python(
            "printf '%%s\\n' %s\nprintf '%%s\\n' %s >&2\n"
                   "mkdir -p \"$HELM_HOME/%s\"\n"
                   "printf '%%s\\n' %s > \"$HELM_HOME/%s/gate-receipts.jsonl\""
                   "\n" % (shlex.quote(json.dumps(verdict)),
                           shlex.quote(forged), home.GLOBAL,
                           shlex.quote(receipt_line), home.GLOBAL))
        proc = self._run(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        out = proc.stdout.decode("utf-8")
        with open(size_at) as fh:
            shipped = int(fh.read().strip())
        self.assertEqual(shipped, len(payload),
                         "the backgrounded bundle read got /dev/null, not "
                         "the session's stdin")
        fields, sections, violation = gateroute._parse_markers(
            out, self.challenge)
        self.assertIsNone(violation)
        self.assertEqual(sorted(fields),
                         ["cgroup", "done", "gate-rc", "node", "scratch"])
        self.assertNotIn("error", fields,
                         "a marker the gate printed became protocol state")
        self.assertEqual(fields["scratch"], "disk")
        self.assertEqual(fields["cgroup"], "direct")
        self.assertEqual(fields["gate-rc"], "0")
        self.assertTrue(fields["node"], "the session named no node")
        self.assertEqual(json.loads(sections["gate-json"]), verdict)
        self.assertEqual(sections["receipts"].strip(), receipt_line)
        # the forged line is PRESENT and verbatim — carried as the data it is
        self.assertEqual(sections["gate-err"].strip(), forged)
        self.assertEqual(os.listdir(self.root), [],
                         "the finished session left its scratch behind")


if __name__ == "__main__":
    unittest.main()
