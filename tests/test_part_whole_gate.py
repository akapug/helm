#!/usr/bin/env python3
"""The ask's path door (task/3938, gate 4) — a claim or send that names a task
picks EXACTLY one of `--whole` / `--part`. Neither (the path is meant but
unspoken) and both (two paths to one ask) are refused, the refusal naming the
two flags and the exact retry; exactly one proceeds. This door is met at
`helm work claim` and `helm dispatch send` alike.

These arms are the RED contract for the gate: on main (without the gate) a bare
`--task` claim and send proceed, so every "refuses" arm here fails first, and
`--part` is an unknown flag. The green code (claim/send gates plus the `--part`
flag) is what turns red into green. Callers elsewhere are fixed in their own
modules; this file only names the door."""
import unittest

from helm import dispatches, tasks  # noqa: E402
from tests.test_dispatch_chain import ChainBase, run  # noqa: E402
from tests.test_work import WorkBase  # noqa: E402


class ClaimPathTest(WorkBase):
    """`helm work claim --task task/N` refuses unless exactly one of
    `--whole` / `--part` is given (claim door)."""

    def file(self, title="the thing"):
        row, err = tasks.add(title, "s1", force_new=True)
        self.assertIsNone(err, err)
        return row["id"]

    def test_neither_flag_refuses_and_names_both_flags(self):
        tid = self.file()
        rc, out, err = self.work("claim", "demo", "--seat", "s1",
                                 "--task", tid)
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--whole", err)
        self.assertIn("--part", err)
        self.assertIn("exactly one of --whole or --part", err)
        self.assertNotIn("lane/demo\t", out)

    def test_both_flags_refuse_and_name_both(self):
        tid = self.file()
        rc, out, err = self.work("claim", "demo", "--seat", "s1",
                                 "--task", tid, "--whole", "--part")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--whole", err)
        self.assertIn("--part", err)
        self.assertIn("both --whole and --part were given", err)
        self.assertNotIn("lane/demo\t", out)

    def test_part_with_no_task_refuses(self):
        # (task/3938 round 3) an orphan --part, with no --task, is refused:
        # --part (like --whole) names a path to the task that --task names.
        rc, out, err = self.work("claim", "demo", "--seat", "s1", "--part")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--part", err)
        self.assertIn("--task", err)
        self.assertNotIn("lane/demo\t", out)

    def test_part_proceeds_and_records_no_whole_ask(self):
        tid = self.file()
        rc, out, err = self.work("claim", "demo", "--seat", "s1",
                                 "--task", tid, "--part")
        self.assertEqual(rc, 0, err)
        # the machine line: path<TAB>branch<TAB>lease<TAB>ttl, branch is
        # lane/<lane>
        self.assertEqual(out.strip().split("\t")[1], "lane/demo", out)
        from helm import taskkey
        self.assertEqual(taskkey.lane_records(self.root)[0]["demo"],
                         frozenset({tid}))
        self.assertEqual(taskkey.lane_wholes(self.root)[0], {})


class SendPathTest(ChainBase):
    """`helm dispatch send --task task/N` refuses unless exactly one of
    `--whole` / `--part` is given (send door)."""

    def setup_task(self):
        row, err = tasks.add("the thing", "seat-b", force_new=True)
        self.assertIsNone(err, err)
        self.tid = row["id"]

    def _send(self, *extra):
        self.setup_task()
        return run(dispatches.cmd_dispatch,
                   ["send", "seat-b", "lane-a", "take the piece",
                    "--ref", self.a, "--kind", "build",
                    "--repo", self.repo, "--new-work", "--task",
                    self.tid] + list(extra))

    def _send_no_task(self, *extra):
        """A send without --task: the orphan --whole/--part arms."""
        self.setup_task()
        return run(dispatches.cmd_dispatch,
                   ["send", "seat-b", "lane-a", "take a piece",
                    "--ref", self.a, "--kind", "build",
                    "--repo", self.repo, "--new-work"] + list(extra))

    def test_neither_flag_refuses_and_names_both(self):
        rc, out, err = self._send()
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--whole", err)
        self.assertIn("--part", err)
        self.assertIn("exactly one of --whole or --part", err)

    def test_part_with_no_task_refuses(self):
        # (task/3938 round 3) an orphan --part, with no --task, is refused at
        # the send door too: --part (like --whole) names the path to the task
        # that --task names.
        rc, out, err = self._send_no_task("--part")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--part", err)
        self.assertIn("--task", err)

    def test_whole_with_no_task_is_judged_by_the_row_writer(self):
        # --whole with no --task is NOT an orphan at the send door: the row
        # writer resolves the task from the lane's record, and only it knows
        # whether one exists. With no record here, its own sentence refuses.
        rc, out, err = self._send_no_task("--whole")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("WHOLE ask", err)
        self.assertNotIn("name the path to the task that --task names", err)

    def test_both_flags_refuse(self):
        rc, out, err = self._send("--whole", "--part")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--whole", err)
        self.assertIn("--part", err)
        self.assertIn("both --whole and --part were given", err)

    def test_part_proceeds(self):
        rc, out, err = self._send("--part")
        self.assertEqual(rc, 0, err)
        (row,) = dispatches.rows().values()
        self.assertEqual(row["task"], self.tid)


if __name__ == "__main__":
    unittest.main()
