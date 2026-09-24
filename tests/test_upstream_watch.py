#!/usr/bin/env python3
"""upstream-watch: the Claude Code release watcher and its schema-aware diff.

Every arm runs on fixtures: two tiny fake releases (a minimal Bun module graph
around a few lines of JavaScript), a fake CHANGELOG read through a file URL, and
a fake `claude` script that logs its argv and environment. Nothing here runs a
real model, touches the network, or reads the real helm home.
"""
import contextlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-upstream-watch-", var="HELM_HOME")

from helm import cli, tasks, upstream_surface as surface, upstream_watch as watch  # noqa: E402


def bun_graph(modules):
    """A minimal Bun standalone tail: fake runtime bytes, the module graph,
    its 32-byte header and the trailer. `modules` is [(name, encoding,
    loader, body bytes)]."""
    blob, records = bytearray(), []
    for name, encoding, loader, body in modules:
        raw = name.encode("utf-8")
        name_at = len(blob)
        blob += raw
        body_at = len(blob)
        blob += body
        records.append(struct.pack("<12I", name_at, len(raw), body_at, len(body),
                                   0, 0, 0, 0, 0, 0, 0, 0)
                       + bytes([encoding, loader, 0, 1]))
    table_at = len(blob)
    table = b"".join(records)
    blob += table
    header = struct.pack("<QIIIIII", len(blob), table_at, len(table), 0, 0, 0, 0)
    return b"\x7fELF fake runtime\n" + bytes(blob) + header + b"\n---- Bun! ----\n" + b"tail"


PROGRAM = """// @bun
var X={};function Qs(){let a=X.object({effortLevel:X.string().optional().describe("Effort level for the model")});return d({$schema:X.literal("u").optional().describe("JSON Schema reference for Claude Code settings"),model:X.string().optional().describe("%(model)s"),modelSettings:a.optional().describe("Per-model settings")%(extra)s})}
program.command("mcp").description("Configure MCP servers");
program.option("--fast","%(fast)s");
var e=process.env.%(env)s;
var H=[%(hooks)s];
var M=[%(models)s];
"""

V1 = PROGRAM % {"model": "Override the default model", "extra": "",
                "fast": "Run fast", "env": "CLAUDE_CODE_OLD_THING",
                "hooks": '"PreToolUse","PostToolUse","Stop","SubagentStop"',
                "models": '{id:"claude-opus-5",name:"Opus 5",min_claude_code_version:"2.1.200"}'}
V2 = PROGRAM % {"model": "Override the default model; aliases resolve to the newest release",
                "extra": ',ultracode:X.boolean().optional().describe("Ultra code mode")',
                "fast": "Run fast (the default since this release)",
                "env": "CLAUDE_CODE_NEW_THING",
                "hooks": '"PreToolUse","PostToolUse","Stop","SubagentStop","SessionStart"',
                "models": '{id:"claude-opus-5",name:"Opus 5",min_claude_code_version:"2.1.200"},'
                          '{id:"claude-opus-5-5",name:"Opus 5.5",min_claude_code_version:"2.1.280"}'}

SKILL_V1 = "---\nname: demo\ndescription: A demo skill\n---\nPut effort at the top level.\n"
SKILL_V2 = "---\nname: demo\ndescription: A demo skill\n---\nPut effort under modelSettings.\n"


def program_bytes(js, skill, extra=()):
    mods = [("/$bunfs/root/chunk-aaaa1111.js", 1, 1, js.encode("latin-1")),
            ("/$bunfs/root/SKILL-%s.md" % ("a1b2c3d4" if skill == SKILL_V1 else "e5f6a7b8"),
             1, 13, skill.encode("latin-1")),
            ("/$bunfs/root/logo.node", 0, 10, b"\x7fELF\x00binary")]
    return bun_graph(mods + list(extra))


CHANGELOG = """# Changelog

## 1.0.1

- Added the `ultracode` setting for long agentic runs
- Effort now reads from modelSettings for every model

## 1.0.0

- First release
"""

FAKE_CLAUDE = r'''import json, os, sys
prompt = sys.stdin.read()
with open(os.environ["UW_FAKE_SCENARIO"]) as f:
    scenario = json.load(f)
role = ("propose" if "ROLE: PROPOSE" in prompt else
        "refute" if "ROLE: REFUTE" in prompt else "other")
with open(os.environ["UW_FAKE_LOG"], "a") as f:
    f.write(json.dumps({"role": role, "argv": sys.argv, "env": sorted(os.environ),
                        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                        "cwd": os.getcwd(), "prompt_bytes": len(prompt),
                        "prompt": prompt if role == "refute" else ""}) + "\n")
if role in scenario.get("fail", []):
    sys.stderr.write("fixture failure\n")
    sys.exit(3)
if role == "propose":
    result = json.dumps({"tweaks": scenario.get("tweaks", [])})
else:
    verdict = "refuted"
    for title, value in scenario.get("verdicts", {}).items():
        if title in prompt:
            verdict = value
    result = json.dumps({"verdict": verdict, "reason": "fixture " + verdict,
                         "checked": ["fixture"]})
print(json.dumps({"type": "result", "is_error": False, "result": result,
                  "total_cost_usd": 0.5}))
'''

TWEAK = {"title": "Teach helm the ultracode setting",
         "why": "CC added a settings key helm writes nowhere",
         "evidence": ["Added the `ultracode` setting for long agentic runs"],
         "files": ["helm/hooks.py"], "proposed_diff": "--- a/helm/hooks.py\n+++ b/helm/hooks.py\n",
         "needs_owner": False, "owner_question": ""}
OTHER = dict(TWEAK, title="Move effort under modelSettings in seat configs",
             evidence=["Effort now reads from modelSettings for every model"],
             needs_owner=True, owner_question="Raise the fleet's default effort?")


class Fixture(unittest.TestCase):
    """A temp helm home, a versions dir holding releases 1.0.0 and 1.0.1, a
    CHANGELOG file, and the fake claude, all wired through the watcher's
    knobs. Tier-2 sources are emptied; the docs arm sets its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-uw-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vdir = os.path.join(self.tmp, "versions")
        os.makedirs(self.vdir)
        self.release("1.0.0", V1, SKILL_V1)
        self.release("1.0.1", V2, SKILL_V2)
        self.changelog = os.path.join(self.tmp, "CHANGELOG.md")
        self.write(self.changelog, CHANGELOG)
        self.fake = os.path.join(self.tmp, "fake-cc")
        self.write(self.fake, "#!%s\n%s" % (sys.executable, FAKE_CLAUDE))
        os.chmod(self.fake, 0o755)
        self.log = os.path.join(self.tmp, "claude.log")
        self.scenario_path = os.path.join(self.tmp, "scenario.json")
        self.scenario({"tweaks": [TWEAK], "verdicts": {TWEAK["title"]: "survives"}})
        self.home = os.path.join(self.tmp, "helm-home")
        self.claude_home = os.path.join(self.tmp, "claude-home")
        env = {"HELM_HOME": self.home,
               "HELM_UPSTREAM_WATCH": "1", "HELM_UPSTREAM_WATCH_DRY_RUN": "0",
               "HELM_UPSTREAM_WATCH_VERSIONS_DIR": self.vdir,
               "HELM_UPSTREAM_WATCH_CHANGELOG_URL": "file://" + self.changelog,
               "HELM_UPSTREAM_WATCH_CLAUDE": self.fake,
               "HELM_UPSTREAM_WATCH_CLAUDE_HOME": self.claude_home,
               "HELM_UPSTREAM_WATCH_MODEL": "opus", "HELM_UPSTREAM_WATCH_EFFORT": "high",
               "HELM_UPSTREAM_WATCH_TIMEOUT_S": "120",
               "UW_FAKE_LOG": self.log, "UW_FAKE_SCENARIO": self.scenario_path}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch.object(watch, "SOURCES", ()),
                        mock.patch.object(watch, "_checkout", return_value=self.tmp),
                        mock.patch.object(watch, "_owner", return_value=("seat-a", None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        posting = mock.patch("helm.chat.post", return_value={"id": "row-1"})
        self.post = posting.start()
        self.addCleanup(posting.stop)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def release(self, version, js, skill, extra=()):
        with open(os.path.join(self.vdir, version), "wb") as f:
            f.write(program_bytes(js, skill, extra))

    def scenario(self, value):
        self.write(self.scenario_path, json.dumps(value))

    def runs(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def state(self):
        path = watch.state_path()
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def filed(self):
        return sorted((r for r in tasks.rows().values()), key=lambda r: r["id"])


class ExtractorTest(Fixture):

    def test_the_module_graph_is_read_in_its_real_encodings(self):
        utf16 = "Überblick — a note stored as UTF-16\n"
        path = os.path.join(self.tmp, "u16")
        with open(path, "wb") as f:
            f.write(program_bytes(V1, SKILL_V1, [
                ("/$bunfs/root/notes-c0ffee12.md", 2, 13, utf16.encode("utf-16-le"))]))
        mods, why = surface.bun_modules(path)
        self.assertIsNone(why)
        self.assertEqual([m[0].rsplit("/", 1)[1] for m in mods],
                         ["chunk-aaaa1111.js", "SKILL-a1b2c3d4.md", "logo.node",
                          "notes-c0ffee12.md"])
        found, unreadable = surface.assets(mods)
        self.assertEqual(found["notes.md#Überblick — a note stored as UTF-16"]["text"], utf16)
        self.assertEqual(found["SKILL.md#demo"]["text"], SKILL_V1)
        self.assertEqual(unreadable, [])
        self.assertIn("function Qs()", surface.program_text(mods))
        plain = os.path.join(self.tmp, "plain")
        self.write(plain, "not a Bun executable at all, just some text " * 4)
        mods, why = surface.bun_modules(plain)
        self.assertIsNone(mods)
        self.assertIn("no Bun module-graph trailer", why)

    def test_the_schema_aware_diff_pairs_changes_and_names_new_surface(self):
        d, why = surface.diff_versions(os.path.join(self.vdir, "1.0.0"),
                                       os.path.join(self.vdir, "1.0.1"))
        self.assertIsNone(why)
        s = d["surface"]
        self.assertEqual(s["unavailable"], [])
        self.assertEqual(s["settings_keys"], [{"op": "added", "key": "ultracode"}])
        changed = [r for r in s["settings_describes"] if r["op"] == "changed"]
        self.assertEqual(changed, [{"op": "changed", "key": "model",
                                    "old": "Override the default model",
                                    "new": "Override the default model; aliases "
                                           "resolve to the newest release"}])
        self.assertIn({"op": "added", "key": "ultracode", "new": "Ultra code mode"},
                      s["settings_describes"])
        self.assertEqual(s["envs"], [{"op": "added", "key": "CLAUDE_CODE_NEW_THING"},
                                     {"op": "removed", "key": "CLAUDE_CODE_OLD_THING"}])
        self.assertEqual([(r["op"], r["key"]) for r in s["flags"]], [("changed", "mcp --fast")])
        self.assertEqual([r["op"] for r in s["hook_arrays"]], ["added", "removed"])
        self.assertEqual([(r["op"], r["key"]) for r in s["models"]],
                         [("added", "claude-opus-5-5")])
        self.assertIn('min_claude_code_version:"2.1.280"', s["models"][0]["new"])
        self.assertEqual(s["commands"], [])
        self.assertEqual([(r["op"], r["key"]) for r in d["assets"]],
                         [("changed", "SKILL.md#demo")])
        self.assertIn("+Put effort under modelSettings.", d["assets"][0]["diff"])
        self.assertIn("modelSettings.effortLevel",
                      surface.read_version(os.path.join(self.vdir, "1.0.1"))[0]
                      ["surface"]["settings_keys"])

    def test_a_rebuild_that_only_renames_minified_names_diffs_empty(self):
        a = surface.extract('x.describe(ab.c);var M=[{id:"claude-z",f:ab,g:"keep"}];')
        b = surface.extract('x.describe(qz.c);var M=[{id:"claude-z",f:qz,g:"keep"}];')
        d = surface.diff_surface(a, b)
        self.assertEqual(d["all_describes"], [])
        self.assertEqual(d["models"], [])
        c = surface.extract('x.describe(qz.c);var M=[{id:"claude-z",f:qz,g:"changed"}];')
        self.assertEqual([r["op"] for r in surface.diff_surface(a, c)["models"]], ["changed"])
        self.assertIn("settings: the settings schema anchor", a["unavailable"][0])

    def test_a_long_section_is_cut_and_the_cut_is_counted(self):
        kept, dropped = surface.bounded({"envs": [{"op": "added", "key": str(i)}
                                                  for i in range(7)],
                                         "flags": [], "unavailable": ["x"]}, keep=3)
        self.assertEqual(len(kept["envs"]), 3)
        self.assertEqual(dropped, {"envs": 4})
        self.assertEqual(kept["unavailable"], ["x"])


class PassTest(Fixture):

    def test_a_new_release_is_read_once_and_the_state_advances_after_success(self):
        self.assertIsNone(self.state())
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "done", result.get("why"))
        self.assertEqual([r["role"] for r in self.runs()], ["propose", "refute"])
        rows = self.filed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner"], "seat-a")
        self.assertEqual(rows[0]["project"], "helm")
        self.assertEqual(rows[0]["source"], "upstream-watch")
        self.assertIn("Teach helm the ultracode setting", rows[0]["title"])
        self.assertIn("Added the `ultracode` setting", rows[0]["note"])
        self.assertIn("PROPOSED CHANGE:", rows[0]["note"])
        self.assertEqual(self.post.call_count, 1)
        text = self.post.call_args[0][0]
        self.assertIn("FILED %s" % rows[0]["id"], text)
        self.assertIn("@seat-a", text)
        self.assertEqual(self.post.call_args[1]["room"], "helm")
        self.assertEqual(self.state()["last_seen"], "1.0.1")
        again = watch.run_pass()
        self.assertEqual(again["outcome"], "idle")
        self.assertIn("1.0.1 was already read", again["idle"])
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(len(self.filed()), 1)
        self.assertEqual(self.post.call_count, 1)

    def test_a_failed_pass_leaves_the_state_and_the_release_is_read_again(self):
        self.scenario({"fail": ["propose"]})
        failed = watch.run_pass()
        self.assertEqual(failed["outcome"], "failed")
        self.assertIn("propose: claude -p exited 3", failed["why"])
        self.assertNotIn("last_seen", self.state())
        self.assertEqual(self.state()["last_run"]["outcome"], "failed")
        self.assertEqual(self.filed(), [])
        self.scenario({"tweaks": [TWEAK], "verdicts": {TWEAK["title"]: "survives"},
                       "fail": ["refute"]})
        self.assertEqual(watch.run_pass()["outcome"], "failed")
        self.assertEqual(self.filed(), [])
        self.assertNotIn("last_seen", self.state())
        self.scenario({"tweaks": [TWEAK], "verdicts": {TWEAK["title"]: "survives"}})
        retried = watch.run_pass()
        self.assertEqual(retried["outcome"], "done")
        self.assertEqual(self.state()["last_seen"], "1.0.1")
        self.assertEqual(len(self.filed()), 1)

    def test_an_unreachable_changelog_or_an_unposted_digest_fails_the_pass(self):  # noqa: VACUOUS_ASSERTION — no model run and no advance is the contract; the last pass here advances
        with mock.patch.dict(os.environ, {"HELM_UPSTREAM_WATCH_CHANGELOG_URL":
                                          "file://" + os.path.join(self.tmp, "absent.md")}):
            result = watch.run_pass()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("CHANGELOG:", result["why"])
        self.assertEqual(self.runs(), [])
        self.post.return_value = None
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("did not post", result["why"])
        self.assertNotIn("last_seen", self.state())
        filed = self.filed()
        self.assertEqual(len(filed), 1)
        self.assertEqual(self.state()["unannounced"],
                         [{"task": filed[0]["id"], "title": TWEAK["title"], "owner": "seat-a"}])
        self.post.return_value = {"id": "row-2"}
        retried = watch.run_pass()
        self.assertEqual(retried["outcome"], "done")
        self.assertEqual(retried["tweaks"][0]["filing"]["state"], "duplicate")
        self.assertEqual(self.state()["last_seen"], "1.0.1")
        self.assertNotIn("unannounced", self.state())
        self.assertIn("FILED %s in an earlier pass whose digest did not post"
                      % filed[0]["id"], self.post.call_args[0][0])
        self.assertEqual(len(self.filed()), 1)

    def test_a_refuted_tweak_files_nothing(self):
        self.scenario({"tweaks": [TWEAK, OTHER],
                       "verdicts": {TWEAK["title"]: "refuted", OTHER["title"]: "survives"}})
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "done")
        self.assertEqual([r["role"] for r in self.runs()], ["propose", "refute", "refute"])
        titles = [r["title"] for r in self.filed()]
        self.assertEqual(len(titles), 1)
        self.assertIn(OTHER["title"], titles[0])
        self.assertNotIn(TWEAK["title"], "\n".join(titles))
        text = self.post.call_args[0][0]
        self.assertIn("2 proposed, 1 survived the refute", text)
        self.assertIn("NEEDS OWNER DECISION", text)
        self.assertIn("Raise the fleet's default effort?", text)
        self.assertNotIn(TWEAK["title"], text)
        self.assertIn("NEEDS OWNER DECISION", self.filed()[0]["note"])

    def test_nothing_relevant_posts_nothing(self):
        self.scenario({"tweaks": []})
        quiet = watch.run_pass()
        self.assertEqual(quiet["outcome"], "done")
        self.assertIsNone(quiet["digest"])
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state()["last_seen"], "1.0.1")
        self.scenario({"tweaks": [TWEAK], "verdicts": {TWEAK["title"]: "refuted"}})
        self.release("1.0.2", V2, SKILL_V2)
        self.write(self.changelog, CHANGELOG.replace("## 1.0.1", "## 1.0.2\n\n- Added the "
                                                     "`ultracode` setting for long agentic "
                                                     "runs\n\n## 1.0.1"))
        refuted = watch.run_pass()
        self.assertEqual(refuted["outcome"], "done")
        self.assertEqual(self.post.call_count, 0)
        self.scenario({"tweaks": [TWEAK], "verdicts": {TWEAK["title"]: "survives"}})
        self.release("1.0.3", V2, SKILL_V2)
        self.write(self.changelog, CHANGELOG.replace("## 1.0.1", "## 1.0.3\n\n- Added the "
                                                     "`ultracode` setting for long agentic "
                                                     "runs, again\n\n## 1.0.1"))
        self.assertEqual(watch.run_pass()["outcome"], "done")
        self.assertEqual(self.post.call_count, 1)
        self.assertFalse(watch.relevant({"claude_code": {"changelog": {"releases": []},
                                                         "program": {"counts": {"envs": 0}}},
                                         "vendor_pages": []}))
        self.assertTrue(watch.relevant({"claude_code": {"changelog": {"releases": []},
                                                        "program": {"counts": {"envs": 1}}},
                                        "vendor_pages": []}))

    def test_a_dry_run_files_nothing_posts_nothing_and_writes_no_state(self):  # noqa: VACUOUS_ASSERTION — nothing filed, posted or written is the contract; the last pass here files a row
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["upstream-watch", "--dry-run"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("--- would post to #helm ---", text)
        self.assertIn("WOULD FILE: Teach helm the ultracode setting", text)
        self.assertIn("--- would file (owner @seat-a) ---", text)
        self.assertIn("DRY RUN: nothing filed", text)
        self.assertEqual([r["role"] for r in self.runs()], ["propose", "refute"])
        self.assertEqual(self.filed(), [])
        self.assertEqual(self.post.call_count, 0)
        self.assertIsNone(self.state())
        with mock.patch.dict(os.environ, {"HELM_UPSTREAM_WATCH_DRY_RUN": "1"}):
            forced = watch.run_pass(dry_run=watch.dry_run_forced())
        self.assertTrue(forced["dry_run"])
        self.assertEqual(self.filed(), [])
        self.assertIsNone(self.state())
        self.assertEqual(watch.run_pass()["outcome"], "done")
        self.assertEqual(len(self.filed()), 1)

    def test_evidence_the_bundle_does_not_hold_is_dropped_before_any_refute(self):
        invented = dict(TWEAK, title="Chase an invented change",
                        evidence=["this sentence is in no upstream artifact"])
        self.scenario({"tweaks": [invented, TWEAK],
                       "verdicts": {TWEAK["title"]: "survives", invented["title"]: "survives"}})
        result = watch.run_pass()
        self.assertEqual(result["dropped"], 1)
        self.assertEqual([t["title"] for t in result["tweaks"]], [TWEAK["title"]])
        self.assertEqual([r["role"] for r in self.runs()], ["propose", "refute"])
        self.assertEqual(len(self.filed()), 1)
        bundle = {"claude_code": {"notes": ['"first-wins" (the default) is kept']}}
        kept, dropped, why = watch.tweaks_from(
            {"tweaks": [dict(TWEAK, evidence=['\\"first-wins\\" (the default) is kept']),
                        dict(TWEAK, evidence=['"first-wins" (the default) is kept']),
                        dict(TWEAK, evidence=["first-wins (the default)"])]}, bundle)
        self.assertIsNone(why)
        self.assertEqual((len(kept), dropped), (2, 1))

    def test_a_duplicate_of_an_open_task_is_a_decision_not_a_failure(self):  # noqa: VACUOUS_ASSERTION — no post is the contract; the row list is asserted non-empty in the same arm
        row, err = tasks.add("upstream-watch, CC 1.0.0 -> 1.0.1: Teach helm the ultracode setting",
                             "seat-b", project="helm")
        self.assertIsNone(err)
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["tweaks"][0]["filing"], {"state": "duplicate", "task": row["id"]})
        self.assertEqual([r["id"] for r in self.filed()], [row["id"]])
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state()["last_seen"], "1.0.1")

    def test_a_posture_refusal_is_reported_and_the_state_advances(self):
        pin = dict(TWEAK, title="Pin Claude Code to the last good release")
        self.scenario({"tweaks": [pin], "verdicts": {pin["title"]: "survives"}})
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["tweaks"][0]["filing"]["state"], "refused")
        self.assertIn("strategy on a dependency seam", result["tweaks"][0]["filing"]["why"])
        text = self.post.call_args[0][0]
        self.assertIn("NOT FILED (helm task add: this names a strategy", text)
        self.assertIn(pin["title"], text)
        self.assertEqual(self.state()["last_seen"], "1.0.1")
        self.assertEqual(len(tasks.rows()), 0)

    def test_the_refuter_sees_the_open_tasks_a_proposal_may_repeat(self):
        row, err = tasks.add("seat configs never learn the new ultracode key", "seat-b",
                             project="helm")
        self.assertIsNone(err)
        self.scenario({"tweaks": [TWEAK], "verdicts": {
            TWEAK["title"]: "survives", "seat configs never learn the new ultracode key": "refuted"}})
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "done")
        refutes = [r for r in self.runs() if r["role"] == "refute"]
        self.assertEqual(len(refutes), 1)
        self.assertIn("%s: seat configs never learn the new ultracode key" % row["id"],
                      refutes[0]["prompt"])
        self.assertEqual(result["tweaks"][0]["refute"]["verdict"], "refuted")
        self.assertEqual([r["id"] for r in self.filed()], [row["id"]])

    def test_the_off_switch_runs_nothing(self):  # noqa: VACUOUS_ASSERTION — no run and no state is the contract; the second pass here runs twice
        with mock.patch.dict(os.environ, {"HELM_UPSTREAM_WATCH": "0"}):
            off = watch.run_pass()
        self.assertEqual(off["outcome"], "disabled")
        self.assertEqual(self.runs(), [])
        self.assertIsNone(self.state())
        self.assertEqual(watch.run_pass()["outcome"], "done")
        self.assertEqual(len(self.runs()), 2)

    def test_a_release_waits_for_its_changelog_entry_then_reads_without_it(self):
        self.write(self.changelog, "# Changelog\n\n## 1.0.0\n\n- First release\n")
        waiting = watch.run_pass(now=1000.0)
        self.assertEqual(waiting["outcome"], "deferred")
        self.assertEqual(self.state()["awaiting_changelog"], {"version": "1.0.1", "since": 1000.0})
        self.assertNotIn("last_seen", self.state())
        self.assertEqual(self.runs(), [])
        late = watch.run_pass(now=1000.0 + watch.CHANGELOG_GRACE_S + 1)
        self.assertEqual(late["outcome"], "done")
        self.assertEqual(self.state()["last_seen"], "1.0.1")
        self.assertNotIn("awaiting_changelog", self.state())
        self.assertEqual([r["role"] for r in self.runs()], ["propose"])

    def test_an_unreadable_state_fails_rather_than_rereading(self):  # noqa: VACUOUS_ASSERTION — no model run is the contract; the repaired pass here completes
        os.makedirs(os.path.dirname(watch.state_path()), exist_ok=True)
        self.write(watch.state_path(), "{not json")
        result = watch.run_pass()
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("is unreadable", result["why"])
        self.assertEqual(self.runs(), [])
        self.write(watch.state_path(), "{}")
        self.assertEqual(watch.run_pass()["outcome"], "done")

    def test_a_removed_baseline_diffs_from_the_newest_older_release(self):
        os.makedirs(os.path.dirname(watch.state_path()), exist_ok=True)
        self.write(watch.state_path(), json.dumps({"last_seen": "0.9.0"}))
        pl, why = watch.plan({"last_seen": "0.9.0"}, ["1.0.0", "1.0.1"])
        self.assertIsNone(why)
        self.assertEqual((pl["old"], pl["new"], pl["since"]), ("1.0.0", "1.0.1", "0.9.0"))
        self.assertIn("no longer installed", pl["notes"][0])
        self.assertEqual(watch.plan({"last_seen": "1.0.2"}, ["1.0.0", "1.0.1"])[0], None)
        self.assertIn("rollback", watch.plan({"last_seen": "1.0.2"}, ["1.0.0", "1.0.1"])[1])
        bundle, why = watch.bundle_only()
        self.assertIsNone(why)
        self.assertEqual(bundle["claude_code"]["changelog"]["releases"], ["1.0.1", "1.0.0"])


class ClaudeRunTest(Fixture):

    def test_model_runs_use_the_claude_cli_and_never_an_api_key(self):  # noqa: VACUOUS_ASSERTION — per-run env contract; the run list is asserted to hold two runs first
        leaked = {"ANTHROPIC_API_KEY": "sk-test-not-a-key", "ANTHROPIC_AUTH_TOKEN": "t",
                  "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "CLAUDECODE": "1",
                  "CLAUDE_CODE_SESSION_ID": "s-1", "CLAUDE_CONFIG_DIR": "/elsewhere",
                  "HELM_CHAT_ROOM": "main", "UW_MARKER": "kept"}
        with mock.patch.dict(os.environ, leaked):
            self.assertEqual(watch.run_pass()["outcome"], "done")
        runs = self.runs()
        self.assertEqual(len(runs), 2)
        for r in runs:
            argv = r["argv"]
            self.assertEqual(argv[0], self.fake)
            self.assertEqual(argv[1], "-p")
            self.assertNotIn("--bare", argv)
            self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
            self.assertIn("--no-session-persistence", argv)
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual([k for k in r["env"] if k.startswith("ANTHROPIC")], [])
            self.assertNotIn("CLAUDECODE", r["env"])
            self.assertNotIn("CLAUDE_CODE_SESSION_ID", r["env"])
            self.assertNotIn("HELM_CHAT_ROOM", r["env"])
            self.assertIn("UW_MARKER", r["env"])
            self.assertEqual(r["config_dir"], self.claude_home)
            self.assertEqual(r["cwd"], os.path.realpath(self.tmp))
        env = watch.claude_env({"CLAUDE_CONFIG_DIR": "/own/home", "ANTHROPIC_API_KEY": "k"})
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/own/home"})

    def test_a_reply_is_read_from_its_json_whatever_wraps_it(self):
        self.assertEqual(watch._json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(watch._json_object('Here you go: {"a": 2} done'), {"a": 2})
        self.assertIsNone(watch._json_object("no object here"))


class DocsTest(Fixture):

    def test_a_changed_vendor_page_joins_the_bundle_and_first_sight_is_a_baseline(self):  # noqa: VACUOUS_ASSERTION — first sight runs no model; the changed page files a row in this arm
        page = os.path.join(self.tmp, "models.html")
        self.write(page, "<html><script>var t=1;</script><h1>Models</h1>"
                         "<p>Opus 5.5 needs Claude Code 2.1.280</p></html>")
        src = ({"name": "anthropic-models", "enabled": True, "url": "file://" + page,
                "why": "model ids"},
               {"name": "codex-pricing", "enabled": False, "url": "file:///absent",
                "why": "rate card"})
        with mock.patch.object(watch, "SOURCES", src):
            os.makedirs(os.path.dirname(watch.state_path()), exist_ok=True)
            self.write(watch.state_path(), json.dumps({"last_seen": "1.0.1"}))
            first = watch.run_pass()
            self.assertEqual(first["outcome"], "idle")
            self.assertEqual(self.runs(), [])
            self.assertIn("anthropic-models", self.state()["docs"])
            self.assertNotIn("codex-pricing", self.state()["docs"])
            with open(os.path.join(watch.docs_dir(), "anthropic-models.txt")) as f:
                self.assertEqual(f.read(), "Models\nOpus 5.5 needs Claude Code 2.1.280")
            self.write(page, "<h1>Models</h1><p>Opus 5.5 needs Claude Code 2.1.281</p>")
            self.scenario({"tweaks": [dict(TWEAK, evidence=["+Opus 5.5 needs Claude Code 2.1.281"])],
                           "verdicts": {TWEAK["title"]: "survives"}})
            bundle, why = watch.bundle_only()
            self.assertIsNone(why)
            self.assertIsNone(bundle["claude_code"])
            self.assertEqual(bundle["vendor_pages"][0]["name"], "anthropic-models")
            self.assertIn("-Opus 5.5 needs Claude Code 2.1.280\n+Opus 5.5 needs Claude Code 2.1.281",
                          bundle["vendor_pages"][0]["diff"])
            result = watch.run_pass()
            self.assertEqual(result["outcome"], "done")
            self.assertEqual(result["label"], "tier-2 pages")
            self.assertIn("upstream-watch, tier-2 pages: Teach helm", self.filed()[0]["title"])
            self.assertEqual(len(self.filed()), 1)
            self.assertEqual(watch.run_pass()["outcome"], "idle")


class TimerAndCliTest(Fixture):

    def test_the_daily_units_run_a_real_pass_with_absolute_programs(self):
        spath, service, tpath, timer = watch.timer_units(env={
            "HELM_UPSTREAM_WATCH_CLAUDE": "/opt/claude", "HELM_UPSTREAM_WATCH_CLAUDE_HOME": "/h/c"})
        self.assertTrue(spath.endswith("/.config/systemd/user/helm-upstream-watch.service"))
        self.assertTrue(tpath.endswith("/.config/systemd/user/helm-upstream-watch.timer"))
        self.assertIn("ExecStart=%h/.local/bin/helm upstream-watch\n", service)
        self.assertIn("Environment=HELM_UPSTREAM_WATCH_CLAUDE=/opt/claude\n", service)
        self.assertIn("Environment=HELM_UPSTREAM_WATCH_CLAUDE_HOME=/h/c\n", service)
        self.assertIn("UnsetEnvironment=CLAUDE_CODE_SESSION_ID", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("helm.upstream_watch.shutil.which", return_value="/usr/bin/systemctl"), \
                mock.patch("helm.upstream_watch.pk.atomic_write") as write, \
                mock.patch("helm.upstream_watch.subprocess.run", return_value=done) as run:
            ok, detail = watch.install_timer()
        self.assertTrue(ok, detail)
        self.assertEqual(len(write.call_args_list), 2)
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["/usr/bin/systemctl", "--user", "daemon-reload"],
                          ["/usr/bin/systemctl", "--user", "enable", "--now",
                           "helm-upstream-watch.timer"]])

    def test_the_verb_refuses_an_unknown_flag_and_prints_the_bundle(self):  # noqa: VACUOUS_ASSERTION — --bundle runs nothing and writes nothing; its printed bundle is asserted
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["upstream-watch", "--dryrun"]), 2)
        self.assertIn("unknown arg '--dryrun'", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["upstream-watch", "--bundle"]), 0)
        bundle = json.loads(out.getvalue())
        self.assertEqual(bundle["claude_code"]["release"], "1.0.1")
        self.assertEqual(self.runs(), [])
        self.assertIsNone(self.state())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["upstream-watch", "--help"]), 0)
        self.assertIn("--install-timer", out.getvalue())


if __name__ == "__main__":
    unittest.main()
