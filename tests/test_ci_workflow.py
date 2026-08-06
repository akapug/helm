#!/usr/bin/env python3
"""The CI workflow's TRIGGERS are a claim about cost, and nothing tested them.

`helm` is stdlib-only, so this parses the `on:` block by indentation rather than
importing a YAML library — a test that needed provisioning would contradict the
thing it is guarding, which is the same argument ci.yml makes about itself.

WHY THIS FILE EXISTS. `on: push:` with no branch filter fires on EVERY branch,
and this repo runs ~40 live lane worktrees at once; the day Actions is enabled,
one slice of ordinary lane work launches hundreds of hosted jobs. The owner's
rule is about SPAM, not about hosted runners — commits stay fine-grained, pushes
batch at the end of a slice, a mid-slice run is waste — and `push: branches:
[main]` is exactly that batch point. The workflow's own concurrency comment
already said so ("batching CI at slice ends is the repo's convention") while its
triggers did the opposite, which is the shape a comment cannot catch and a test
can.
"""
import os
import unittest

WORKFLOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github", "workflows", "ci.yml")


def _top_level_block(text, key):
    """The lines under a top-level `key:` mapping, comments and blanks dropped.

    Top-level means column 0; the block ends at the next column-0 line. Good
    enough for a workflow file and honest about it — if this file ever grows
    anchors or flow mappings, the parse is wrong and should be replaced with a
    real reader rather than patched.
    """
    out, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if not inside:
            if line.startswith(key + ":"):
                inside = True
            continue
        if line and not line[0].isspace():        # next top-level key
            break
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


class TheWorkflowFileIsReadable(unittest.TestCase):
    """The control for every assertion below: an unreadable or renamed workflow
    must FAIL here rather than let the trigger tests pass over an empty parse."""

    def test_the_workflow_exists_and_the_parser_finds_its_blocks(self):
        self.assertTrue(os.path.exists(WORKFLOW), WORKFLOW)
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("name: ci", text)
        # the parser really extracts something — so an empty `on` block below is
        # a fact about the triggers, never about a parse that matched nothing
        self.assertTrue(_top_level_block(text, "on"))
        self.assertTrue(_top_level_block(text, "jobs"))


class CiTriggersBatchAtSliceEndsInsteadOfEveryPush(unittest.TestCase):

    def setUp(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            self.text = fh.read()
        self.on = _top_level_block(self.text, "on")

    def test_push_is_branch_filtered_never_bare(self):
        """THE DEFECT. A bare `push:` is every branch — with ~40 live lane
        worktrees that is hundreds of hosted jobs for one slice of work."""
        self.assertIn("push:", self.on)
        # the line after `push:` must be the filter, not another trigger
        idx = self.on.index("push:")
        self.assertLess(idx + 1, len(self.on),
                        "`push:` is the last key in `on:` — it is bare")
        self.assertTrue(self.on[idx + 1].startswith("branches:"),
                        "`push:` carries no branches filter: %r" % self.on)
        # THE FILTER MUST BE main AND NOTHING ELSE. A mutation found this:
        # `branches: [main, lane/**]` contains "main" and passes an `assertIn`
        # while restoring the exact spam this guard exists to prevent. Assert
        # the SET, never that the wanted value is somewhere in it.
        listed = self.on[idx + 1].split(":", 1)[1].strip().strip("[]")
        branches = [b.strip().strip('"\'') for b in listed.split(",") if b.strip()]
        self.assertEqual(branches, ["main"],
                         "push fires on more than main: %r" % branches)

    def test_a_manual_run_stays_available(self):
        """Narrowing must not remove the ability to verify BEFORE landing —
        otherwise the honest response to wanting a check is to widen the
        triggers again."""
        # STRUCTURAL, not membership: pin the WHOLE trigger set. A new
        # automatic trigger appearing beside workflow_dispatch is exactly the
        # regression this file guards, and `assertIn` would not notice it.
        # Parsed HERE rather than read off setUp so the assertion constrains a
        # producer this test owns.
        triggers = _top_level_block(self.text, "on")
        self.assertEqual(triggers,
                         ["push:", "branches: [main]", "workflow_dispatch:"])

    def test_no_trigger_fires_on_a_lane_branch(self):  # noqa: VACUOUS_ASSERTION — the contract IS an empty set (no unfiltered trigger may exist), so no positive control on `unfiltered` can exist; the `examined` assertion below is the strongest available evidence that the scan ran over real triggers, and test_a_manual_run_stays_available pins the whole set structurally.
        """The whole point, stated as the property rather than the spelling:
        nothing in `on:` may fire without a branch filter. `pull_request:` is
        absent today because lanes merge locally after a fab gate, so it could
        only duplicate the main run — but this arm passes if a future PR
        trigger arrives WITH a filter, because the rule is about spam."""
        unfiltered, examined = [], []
        for i, line in enumerate(self.on):
            if not line.endswith(":") or line.startswith("branches"):
                continue
            if line == "workflow_dispatch:":      # manual, never automatic
                continue
            examined.append(line)
            nxt = self.on[i + 1] if i + 1 < len(self.on) else ""
            if not nxt.startswith("branches:"):
                unfiltered.append(line)
        # POSITIVE CONTROL ON THE SAME SCAN: it really looked at an automatic
        # trigger. Without this, an `on:` block that parsed to nothing — a
        # renamed key, a reindented file — would satisfy the emptiness below
        # while proving no trigger safe.
        self.assertTrue(examined, "no automatic trigger examined: %r" % self.on)
        self.assertEqual(unfiltered, [],
                         "unfiltered trigger(s) in `on:` — these fire on every "
                         "lane branch: %r" % unfiltered)

    def test_the_matrix_survived_the_narrowing(self):
        """POSITIVE CONTROL ON THE SAME FILE, and the reason this lane did not
        take its reviewer's prescription: making CI manual-only or self-hosted
        would have deleted the matrix that converts helm's DECLARED 3.9 floor
        into a MEASURED one. The cost problem was the triggers, never the jobs."""
        # ...and assert the MATRIX, not the jobs block. A mutation found this
        # too: a second job carries `python-version: "3.9"` as the declared
        # floor, so "3.9 appears somewhere under jobs" stayed true with the
        # matrix gutted to a single column.
        jobs = _top_level_block(self.text, "jobs")
        matrix = [l for l in jobs if l.startswith("python:")]
        self.assertEqual(len(matrix), 1, "expected ONE matrix line: %r" % matrix)
        listed = matrix[0].split(":", 1)[1].strip().strip("[]")
        cols = [c.strip().strip('"\'') for c in listed.split(",") if c.strip()]
        self.assertEqual(cols, ["3.9", "3.10", "3.11", "3.12", "3.13"], cols)


if __name__ == "__main__":
    unittest.main()
