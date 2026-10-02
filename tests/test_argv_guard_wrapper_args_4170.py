#!/usr/bin/env python3
"""task/4170: an exec wrapper (timeout, nice, ionice) before a prose program.

The task/3945 prose cure reads quoted text as prose only where every program
of the command receives prose (`chat._a_program_runs_text`, an allow-list).
`timeout`, `nice` and `ionice` exec their argv and never run a shell string,
so a prose program behind one stays prose — but only where each word the
wrapper reads is a LITERAL. A runtime value there (`timeout $T`, `nice -n
$N`) is split by bash into more words, and those words can name a shell
(`T="5 sh -c …"`), which then runs the prose. So:

- `timeout`, `nice` and `ionice` with literal arguments and a prose program
  after them are transparent: the prose is not read as gh's verbs.
- a wrapper with a runtime-value or glob argument keeps the whole command
  read, judged in real bash below with a stub head.
- a shell, an interpreter or gh itself behind a wrapper is refused as before.

Every spelling is built at runtime, as the sibling argv tests do, so this
FILE holds no whole one.
"""
import os as _os
import shutil
import subprocess
import sys as _sys
import tempfile
import unittest

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-argv-wrapper-4170-", var="HELM_HOME")

from helm import chat  # noqa: E402

GH = "g" + "h"
NOUN = "work" + "flow"
RUN = "run"
PHRASE = "the %s %s finished green" % (NOUN, RUN)


def refused(command):
    return chat.github_actions_refusal(command=command)


class AWrapperBeforeProseIsTransparentTest(unittest.TestCase):
    """The measured post and its siblings pass: the wrapper execs a prose
    program, and the prose is not gh's grammar."""

    def test_the_measured_post_passes(self):  # noqa: VACUOUS_ASSERTION — read beside test_a_runner_behind_a_wrapper_is_refused on the same rung, whose shell and gh spellings behind the same wrappers are refused
        body = "body=$(cat <<'EOF'\n%s\nEOF\n)\n" % PHRASE
        for post in ('timeout 30 helm chat post -- "$body"',
                     'timeout 30s helm chat post -- "$body"',
                     'timeout -k 5 1.5m helm chat post -- "$body"',
                     'nice -n 5 helm chat post -- "$body"',
                     'nice -5 helm chat post -- "$body"',
                     'ionice -c3 helm chat post -- "$body"',
                     'ionice -c 2 -n7 helm chat post -- "$body"',
                     'timeout 30 nice ionice -c3 helm chat post -- "$body"'):
            for cmd in (body + post, post + " && echo '%s'" % PHRASE):
                with self.subTest(cmd=cmd[-60:]):
                    self.assertIsNone(refused(cmd))

    def test_a_wrapped_post_with_its_own_quoted_heredoc_passes(self):  # noqa: VACUOUS_ASSERTION — read beside test_a_runner_behind_a_wrapper_is_refused on the same rung
        for wrap in ("timeout 30", "nice", "ionice -c3"):
            cmd = "%s helm chat post --room x <<'EOF'\n%s\nEOF" % (wrap, PHRASE)
            with self.subTest(wrap=wrap):
                self.assertIsNone(refused(cmd))

    def test_a_body_read_from_a_quoted_heredoc_passes(self):  # noqa: VACUOUS_ASSERTION — read beside test_a_runner_behind_a_wrapper_is_refused, whose `sh -c "$body"` and `$body` spellings of the same body are refused
        """`read` assigns what it reads and runs none of it."""
        body = "IFS= read -r -d '' body <<'EOF' || true\n%s\nEOF\n" % PHRASE
        for post in ('helm chat post -- "$body"',
                     'timeout 30 helm chat post -- "$body"'):
            with self.subTest(post=post):
                self.assertIsNone(refused(body + post))


class ARunnerBehindAWrapperIsRefusedTest(unittest.TestCase):

    def test_a_runner_behind_a_wrapper_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        act = "$G %s %s ci.yml" % (NOUN, RUN)
        body = "IFS= read -r -d '' body <<'EOF' || true\n%s\nEOF\n" % act
        for cmd in (
                "timeout 5 sh -c '%s %s %s x'" % (GH, NOUN, RUN),
                "timeout 5 %s %s %s x" % (GH, NOUN, RUN),
                "timeout 5 sh -c '%s'" % act,
                "nice -n 5 bash -c '%s'" % act,
                "ionice -c3 sh -c '%s'" % act,
                "ionice -c3 python3 -c 'import os; os.system(\"%s %s %s x\")'"
                % (GH, NOUN, RUN),
                "nice python3 x.py '%s'" % PHRASE,
                "timeout 5 bash <<'EOF'\n%s\nEOF" % act,
                "timeout 5 nice xargs -I{} sh -c '{}' <<'EOF'\n%s\nEOF" % act,
                "timeout 5 eval '%s'" % act,
                # ionice on a process id takes no command: an option no row
                # names keeps the whole command read
                "ionice -p 1 sh -c '%s'" % act,
                # what `read` took is run by a shell or as a program word
                body + 'sh -c "$body"',
                body + "$body"):
            with self.subTest(cmd=cmd[-60:]):
                self.assertIsNotNone(refused(cmd))


class ARuntimeWrapperArgumentIsReadWholeTest(unittest.TestCase):
    """bash splits an unquoted runtime value into words, so a value where a
    wrapper reads its duration or adjustment can name a shell."""

    SHAPES = (
        "timeout $T helm chat post -- '$G %s %s ci.yml'",
        "nice -n $N helm chat post -- '$G %s %s ci.yml'",
        "timeout -k $K 5 helm chat post -- '$G %s %s ci.yml'",
        "ionice -c $C helm chat post -- '$G %s %s ci.yml'",
        "timeout 3* helm chat post -- '$G %s %s ci.yml'")

    def test_each_shape_is_refused(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a non-empty literal tuple, each asserted refused unconditionally
        for shape in self.SHAPES:
            cmd = shape % (NOUN, RUN)
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(refused(cmd))

    def test_bash_runs_the_head_through_a_split_duration(self):  # noqa: VACUOUS_ASSERTION — assertEqual on the stub's printed line proves bash ran the head, and the rung's refusal is asserted unconditionally after it
        if not (shutil.which("bash") and shutil.which("timeout")):
            self.skipTest("no bash or timeout")
        tmp = tempfile.mkdtemp(prefix="helm-4170-head-")
        self.addCleanup(shutil.rmtree, tmp, True)
        stub = _os.path.join(tmp, "head")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "STUB $*"\n')
        _os.chmod(stub, 0o755)
        helm = _os.path.join(tmp, "helm")
        with open(helm, "w") as f:
            f.write("#!/bin/sh\necho POSTED\n")
        _os.chmod(helm, 0o755)
        cmd = self.SHAPES[0] % (NOUN, RUN)
        env = {"PATH": tmp + ":/usr/bin:/bin", "G": stub,
               "T": "5 sh -c eval$IFS$5 x"}
        out = subprocess.run(["bash", "--noprofile", "--norc", "-c", cmd],
                             capture_output=True, text=True, env=env,
                             timeout=10).stdout
        self.assertEqual(out, "STUB %s %s ci.yml\n" % (NOUN, RUN))
        self.assertIsNotNone(refused(cmd))


if __name__ == "__main__":
    unittest.main()
