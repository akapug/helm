import contextlib
import io
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import chat, home, reflex  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR")


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


class SeedBase(unittest.TestCase):
    """Fresh HELM_HOME per test — the default pack must never land in (or read
    from) the module-level home the ReflexTest wipes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seed-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def by_id(self, **kw):
        return {e["id"]: e for e in reflex.load_all(**kw)}


class SeedDefaultsTest(SeedBase):
    def test_seed_installs_four_marked_defaults(self):
        wrote = reflex.seed_defaults()
        self.assertEqual(len(wrote), 4)
        es = self.by_id()
        self.assertEqual(sorted(es), ["compaction-continuity", "correction-language",
                                      "owner-chat-unread", "punt-tell"])
        for e in es.values():
            self.assertEqual(e["source"], "helm-default")  # shipped, legibly
        for rid in ("compaction-continuity", "correction-language", "punt-tell"):
            self.assertEqual(es[rid]["signal"], "prompt")
            self.assertTrue(es[rid]["pattern"])
        # the chat notify reflex: marker-file on the room's owner-unread flag,
        # path resolved at SEED time from HELM_CHAT_DIR (env-respecting)
        e = es["owner-chat-unread"]
        self.assertEqual(e["signal"], "marker-file")
        self.assertEqual(e["marker"], chat.marker_path("main"))
        self.assertTrue(e["marker"].startswith(os.environ["HELM_CHAT_DIR"]))

    def test_reseed_is_a_byte_identical_noop(self):
        reflex.seed_defaults()
        def snap():
            out = {}
            for e in reflex.load_all():
                with open(e["path"]) as f:
                    out[e["id"]] = f.read()
            return out
        before = snap()
        self.assertEqual(reflex.seed_defaults(), [])
        self.assertEqual(snap(), before)

    def test_operator_edit_survives_reseed(self):
        reflex.seed_defaults()
        reflex.write({"id": "punt-tell", "steer": "my own wording",
                      "signal": "prompt", "pattern": r"\blater\b"})
        self.assertEqual(reflex.seed_defaults(), [])
        e = self.by_id()["punt-tell"]
        self.assertEqual(e["steer"], "my own wording")
        self.assertNotEqual(e["source"], "helm-default")  # authored now

    def test_retired_default_stays_retired(self):
        reflex.seed_defaults()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reflex.cmd_reflex(["retire", "punt-tell"]), 0)
        self.assertEqual(reflex.seed_defaults(), [])
        self.assertNotIn("punt-tell", self.by_id())
        e = self.by_id(include_retired=True)["punt-tell"]
        self.assertEqual(e["status"], "retired")
        self.assertEqual(reflex.fire("do it later"), [])

    def test_scaffold_global_seeds_and_stays_idempotent(self):
        home.scaffold_global()
        self.assertEqual(len(reflex.load_all()), 4)
        home.scaffold_global()
        self.assertEqual(len(reflex.load_all()), 4)


class DefaultPackFiringTest(SeedBase):
    def setUp(self):
        super().setUp()
        reflex.seed_defaults()

    def fired(self, text):
        return sorted(e["id"] for e in reflex.fire(text))

    def test_correction_language_fires(self):
        for t in ("no, actually the store is HOME-anchored",
                  "that's wrong, re-read the spec",
                  "i said use the typed store",
                  "stop doing per-commit pushes"):
            self.assertEqual(self.fired(t), ["correction-language"], t)

    def test_punt_tell_fires(self):
        for t in ("let's wire the guard later",
                  "leave a TODO by the cache path",
                  "good enough for now",
                  "park it until next session"):
            self.assertEqual(self.fired(t), ["punt-tell"], t)

    def test_owner_chat_unread_fires_on_marker_only(self):
        # the chat notify loop: marker present -> fires on ANY turn text;
        # consumed (cleared) -> silent again
        chat.post("agents, status?", who="david")
        chat.mark_owner_unread()
        self.assertEqual(self.fired("totally unrelated turn"), ["owner-chat-unread"])
        chat.consume(total=chat.read()[1])
        self.assertEqual(reflex.fire("totally unrelated turn"), [])

    def test_compaction_continuity_fires(self):
        for t in ("This session is being continued from a previous conversation.",
                  "the conversation was summarized to fit the window",
                  "context was compacted; resuming the build"):
            self.assertEqual(self.fired(t), ["compaction-continuity"], t)

    def test_generic_prompts_fire_none(self):
        for t in ("please refactor the auth module and add coverage",
                  "what does the registry sync actually write?",
                  "run the suite and show the failures",
                  # narrative "later"/"todo" is not a punt — the tightened
                  # pattern demands punt-SHAPED phrasing
                  "3 days later the bug reappeared in the todo list view",
                  "the changelog was updated two hours later",
                  ""):
            self.assertEqual(reflex.fire(t), [], t)  # specificity guard

    def test_nudge_lines_short_and_single(self):
        for e in reflex.load_all():
            line = "REFLEX: " + e["steer"]  # exactly what the lane emits
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), 160, e["id"])


class LiveLaneTest(SeedBase):
    """The seeded pack must reach the per-turn surface through the same public
    read path inject uses (reflex.fire -> the reflex lane) — no inject edits."""

    def test_lane_carries_the_nudge_through_gather(self):
        home.scaffold_global()  # first-use scaffolding seeds the pack
        from helm import inject
        steer = next(d["steer"] for d in reflex.DEFAULT_PACK
                     if d["id"] == "correction-language")
        sections = inject.gather("no, actually keep the store HOME-anchored")
        self.assertEqual(sections["reflex"], ["REFLEX: " + steer])
        self.assertEqual(inject.gather("refactor the parser")["reflex"], [])


if __name__ == "__main__":
    unittest.main()
