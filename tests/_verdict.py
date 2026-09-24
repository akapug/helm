"""Exact runtime-author fixture for generated verdicts, never historical rows."""
import contextlib
import os
from unittest import mock

from helm import dispatches


@contextlib.contextmanager
def native_author(case, family="claude"):
    """Supply runtime evidence; leave tier capture and validation entirely real."""
    author_session = "verdict-author-session"

    def family_evidence(identity, session=None, require_exact_session=False):
        case.assertEqual(session, author_session)
        case.assertIs(require_exact_session, True)
        runtime = {"family": family, "agent_harness": "claude",
                   "backend": "native"}
        evidence = {"v": 5, "identity": identity,
                    "roster_identity": identity, "session": author_session,
                    "runtime": runtime, "runtime_verified": True}
        return ({family}, evidence,
                dispatches._subsumed_family_anchor(evidence), None)

    with mock.patch.dict(
            os.environ, {"CLAUDE_CODE_SESSION_ID": author_session}), \
            mock.patch.object(dispatches, "_approval_identity_family_evidence",
                              side_effect=family_evidence):
        yield
