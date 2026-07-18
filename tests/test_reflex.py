import os
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, reflex  # noqa: E402


class ReflexTest(unittest.TestCase):
    def setUp(self):
        home.scaffold_global()
        for n in os.listdir(os.path.join(home.global_dir(), "reflexes")):
            os.remove(os.path.join(home.global_dir(), "reflexes", n))

    def test_write_load_roundtrip(self):
        reflex.write({"id": "checkpoint-green", "steer": "checkpoint the green slice",
                      "signal": "prompt", "pattern": r"\bcommit\b"})
        es = reflex.load_all()
        self.assertEqual(len(es), 1)
        self.assertEqual(es[0]["id"], "checkpoint-green")

    def test_fire_prompt_signal(self):
        reflex.write({"id": "r1", "steer": "s1", "signal": "prompt", "pattern": r"\bdeploy\b"})
        self.assertEqual(len(reflex.fire("time to deploy this")), 1)
        self.assertEqual(reflex.fire("nothing relevant"), [])  # salience law

    def test_fire_marker_signal(self):
        marker = tempfile.mktemp()
        reflex.write({"id": "afk", "steer": "operator is away", "signal": "marker-file",
                      "marker": marker})
        self.assertEqual(reflex.fire("anything"), [])
        with open(marker, "w") as f:
            f.write("x")
        self.assertEqual(len(reflex.fire("anything")), 1)
        os.remove(marker)

    def test_every_turn_budget(self):
        for i in range(5):
            reflex.write({"id": "c%d" % i, "steer": "s", "signal": "every-turn"})
        self.assertEqual(len(reflex.fire("x")), reflex.EVERY_TURN_BUDGET)

    def test_bad_regex_fails_open(self):
        reflex.write({"id": "bad", "steer": "s", "signal": "prompt", "pattern": "("})
        self.assertEqual(reflex.fire("anything ("), [])

    def test_retired_silent(self):
        reflex.write({"id": "r2", "steer": "s2", "signal": "every-turn", "status": "retired"})
        self.assertEqual(reflex.fire("x"), [])


if __name__ == "__main__":
    unittest.main()
