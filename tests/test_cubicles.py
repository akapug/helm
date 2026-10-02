#!/usr/bin/env python3
"""`helm/cubicles.py` — the owner's Orca floor: each fleet seat's tab moved to
the pane its state names (task/3900).

The arms are grouped by the property they defend:

  FLOOR      the floor is ONE named workspace, its panes counted in layout
             order, and a tab is named only through a seat's spawn register
  PLACEMENT  walled -> pane 1, working -> pane 2, local -> pane 3; the lead,
             hand-launched seats and unread states never move
  SHAPE      the floor keeps its three panes: a move that would close one is
             held, a missing pane is named and never made
  TICK       the switch, the two-pass wait, the effect check, and that no
             failure escapes into the sweep
  TITLES     a move never touches a title: orcatitle's byte-exact rule stands
  CLI        `helm seat cubicles` shows the plan and moves on --apply
  SURFACES   doctor names the switch; local-names declares both keys; the
             mood column reads seatmood when it exists

Hermetic: HELM_HOME is a temp tree, the Orca runtime is a fake holding its
own layouts, the spawn registers and liveness are patched, and the real
runtime RPC is switched off. No real pane, title or seat state is touched.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import cubicles, doctor, harness, localnames, orcaadopt, seat  # noqa: E402,F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WID = "repo-1::/floor"
OTHER_WID = "repo-2::/elsewhere"


def tab(tab_id, *handles):
    """One visual-layout tab: a single terminal, or a split of several."""
    leaves = [{"type": "terminal", "handle": h, "tabId": tab_id,
               "leafId": "leaf-%s" % h} for h in handles]
    node = leaves[0]
    for leaf in leaves[1:]:
        node = {"type": "split", "direction": "vertical", "first": node,
                "second": leaf}
    return {"tabId": tab_id, "title": "orca title", "panes": node}


def group(gid, *tabs):
    return {"type": "group", "groupId": gid,
            "activeTabId": tabs[0]["tabId"] if tabs else None,
            "tabs": list(tabs)}


def split(first, second, direction="horizontal"):
    return {"type": "split", "direction": direction, "first": first,
            "second": second}


def owners_floor():
    """The owner's layout: the lead and the walled seat in pane 1, the
    working seats in pane 2, the local seats in pane 3."""
    return split(group("g1", tab("t-lead", "h-lead"),
                       tab("t-opus46", "h-opus46")),
                 split(group("g2", tab("t-cursor", "h-cursor"),
                             tab("t-gemini", "h-gemini")),
                       group("g3", tab("t-qwen", "h-qwen"),
                             tab("t-bonsai", "h-bonsai"))))


REGISTERS = {
    "opus46": {"seat": "opus46", "handle": "h-opus46"},
    "cursor": {"seat": "cursor", "handle": "h-cursor"},
    "gemini": {"seat": "gemini", "handle": "h-gemini"},
    "qwenlocal": {"seat": "qwenlocal", "handle": "h-qwen"},
    "bonsai": {"seat": "bonsai", "handle": "h-bonsai"},
}

#: The states that put every seat of owners_floor() in its right pane.
SETTLED = {"opus46": "WALLED", "cursor": "IDLE", "gemini": "RUNNING"}


class FakeFloor(object):
    """An Orca runtime: workspaces as visual layouts, every RPC recorded.

    A move moves the tab inside the fake's own layout, as the desktop window
    would, unless `inert` (Orca took the move and the window never showed
    it). `refuse` answers every move with that error. `rename` fails the
    test: the mover must never write a title."""
    name = "orca"

    def __init__(self, root=None, path="/floor", others=(), refuse=None,
                 inert=False, list_error=None):
        self.layouts = [{"worktreeId": WID, "worktreePath": path,
                         "root": root if root is not None else owners_floor()}]
        self.layouts += list(others)
        self.refuse, self.inert, self.list_error = refuse, inert, list_error
        self.calls = []

    def rpc(self, method, params, timeout=5):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "terminal.list":
            if self.list_error:
                return None, self.list_error
            return {"terminals": [],
                    "visualLayouts": copy.deepcopy(self.layouts)}, None
        if method == "session.tabs.move":
            if self.refuse:
                return None, self.refuse
            if not self.inert:
                self._move(params)
            return {"moved": True}, None
        return None, "the fake runtime does not answer %s" % method

    def _groups(self, node):
        if not isinstance(node, dict):
            return []
        if node.get("type") == "group":
            return [node]
        return self._groups(node.get("first")) + self._groups(node.get("second"))

    def _move(self, params):
        layout = next(l for l in self.layouts
                      if "id:" + l["worktreeId"] == params["worktree"])
        groups = self._groups(layout["root"])
        moving = None
        for g in groups:
            for t in list(g["tabs"]):
                if t["tabId"] == params["tabId"]:
                    g["tabs"].remove(t)
                    moving = t
        target = next(g for g in groups if g["groupId"] == params["targetGroupId"])
        target["tabs"].append(moving)

    def moves(self):
        return [p for m, p in self.calls if m == "session.tabs.move"]

    def rename(self, *args, **kwargs):
        raise AssertionError("the cubicle mover renamed a tab: %r %r"
                             % (args, kwargs))


class Floor(unittest.TestCase):
    """A temp helm home whose local-names names a temp floor, registers and
    liveness patched, and the real runtime RPC switched off."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="helm-cubicles-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.home = os.path.join(tmp, "helm-home")
        self.floor = os.path.realpath(os.path.join(tmp, "dev"))
        os.makedirs(self.floor)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": self.home,
            "HELM_CHAT_DIR": os.path.join(tmp, "chat"),
            "HELM_ADOPTED_DIR": os.path.join(tmp, "adopted"),
            "HELM_ORCA_RPC": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.names({"cubicle-floor": self.floor})
        self.states = dict(SETTLED)
        self.liveness_calls = []
        self.registers = copy.deepcopy(REGISTERS)

        def liveness(name, upstream_sample=(), repair=True):
            self.liveness_calls.append((name, repair))
            state = self.states.get(name, "IDLE")
            if isinstance(state, Exception):
                raise state
            return {"seat": name, "state": state, "blocked_on": None}

        for patch in (
                mock.patch.object(seat, "seat_liveness", side_effect=liveness),
                mock.patch.object(orcaadopt, "helm_spawned",
                                  side_effect=lambda: copy.deepcopy(self.registers)),
                mock.patch.object(cubicles, "READBACK_PAUSE_S", 0)):
            patch.start()
            self.addCleanup(patch.stop)

    def names(self, table):
        d = os.path.join(self.home, "_global")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, localnames.CONFIG), "w") as f:
            json.dump(table, f)

    def fake(self, **kw):
        kw.setdefault("path", self.floor)
        return FakeFloor(**kw)

    def rows(self, ad):
        rows, why, _wid = cubicles.survey(ad, self.floor)
        self.assertIsNone(why)
        return {r.seat: r for r in rows}

    def state_file(self):
        return os.path.join(self.home, "_global", ".state", cubicles.STATE_NAME)


# ---------------------------------------------------------------------------
# FLOOR
# ---------------------------------------------------------------------------

class FloorReadTest(Floor):

    def test_panes_are_counted_first_before_second_at_every_depth(self):
        """The owner counts his panes left to right. A split tree can nest
        either side; the order must not depend on which side nests."""
        a, b, c = (group("ga", tab("ta", "h-a")), group("gb", tab("tb", "h-b")),
                   group("gc", tab("tc", "h-c")))
        right = split(a, split(b, c))
        left = split(split(copy.deepcopy(a), copy.deepcopy(b)),
                     copy.deepcopy(c))
        got = cubicles.panes(right)
        self.assertEqual([g for g, _tabs in got], ["ga", "gb", "gc"])
        self.assertEqual(got[0][1], [("ta", ["h-a"])])
        self.assertEqual(cubicles.panes(left), got)

    def test_a_split_tab_carries_every_terminal_handle(self):
        got = cubicles.panes(group("g", tab("t", "h1", "h2")))
        self.assertEqual(got, [("g", [("t", ["h1", "h2"])])])

    def test_only_the_named_workspace_is_the_floor(self):
        """A walled seat in ANOTHER workspace is never planned: Orca moves a
        tab only inside its own workspace, and the owner looks at the other
        cwds separately."""
        other = {"worktreeId": OTHER_WID, "worktreePath": "/elsewhere",
                 "root": split(group("x1", tab("t-x", "h-gemini")),
                               group("x2", tab("t-y", "h-cursor")))}
        ad = self.fake(others=[other])
        wid, got, why = cubicles.read_floor(ad, self.floor)
        self.assertIsNone(why)
        self.assertEqual(wid, WID)
        self.assertEqual([g for g, _t in got], ["g1", "g2", "g3"])

    def test_a_floor_with_no_open_workspace_says_so(self):
        ad = self.fake(path="/not-the-floor")
        wid, got, why = cubicles.read_floor(ad, self.floor)
        self.assertEqual((wid, got), (None, []))
        self.assertIn(self.floor, why)

    def test_a_tab_is_named_only_through_a_spawn_register(self):
        """The lead's tab has no register, so no seat: it is never planned.
        The positive control is the registered seat beside it."""
        rows = self.rows(self.fake())
        self.assertIn("opus46", rows)
        self.assertNotIn("t-lead", {r.tab for r in rows.values()})
        self.assertEqual(len(rows), len(REGISTERS))

    def test_a_handle_two_registers_claim_names_nobody(self):
        self.registers["kimi"] = {"seat": "kimi", "handle": "h-cursor"}
        rows = self.rows(self.fake())
        self.assertNotIn("cursor", rows)
        self.assertNotIn("kimi", rows)
        self.assertIn("gemini", rows)

    def test_a_renamed_seat_is_named_by_its_identity(self):
        self.registers["gemini"]["identity"] = "gemini-2"
        rows = self.rows(self.fake())
        self.assertIn("gemini-2", rows)
        self.assertNotIn("gemini", rows)


# ---------------------------------------------------------------------------
# PLACEMENT
# ---------------------------------------------------------------------------

class PlacementTest(Floor):

    def test_the_owners_floor_in_its_right_shape_moves_nothing(self):  # noqa: VACUOUS_ASSERTION — the STAYS action set and the three placements are the positive controls on the same plan; the empty move list is the idempotence contract
        """IDEMPOTENCE. Every seat already sits in its pane, so two live
        passes send no move. MUTATION: plan a move when now == want."""
        ad = self.fake()
        for _ in range(3):
            cubicles.tick(ad, apply=True)
        self.assertEqual(ad.moves(), [])
        rows = self.rows(ad)
        self.assertEqual({r.action for r in rows.values()}, {cubicles.STAYS})
        self.assertEqual(rows["opus46"].place, cubicles.WALLED)
        self.assertEqual(rows["gemini"].place, cubicles.WORKING)
        self.assertEqual(rows["qwenlocal"].place, cubicles.LOCAL)

    def test_a_walled_seat_wants_pane_one(self):
        self.states["gemini"] = "BLOCKED_ON_QUOTA"
        row = self.rows(self.fake())["gemini"]
        self.assertEqual((row.now, row.want, row.action),
                         (1, 0, cubicles.WOULD))
        self.assertEqual(row.group, "g1")

    def test_a_recovered_seat_wants_pane_two(self):
        self.states["opus46"] = "IDLE"
        row = self.rows(self.fake())["opus46"]
        self.assertEqual((row.now, row.want, row.action),
                         (0, 1, cubicles.WOULD))
        self.assertEqual(row.group, "g2")

    def test_a_local_seat_wants_pane_three_and_its_liveness_is_never_read(self):
        root = split(group("g1", tab("t-lead", "h-lead")),
                     split(group("g2", tab("t-cursor", "h-cursor"),
                                 tab("t-qwen", "h-qwen")),
                           group("g3", tab("t-bonsai", "h-bonsai"))))
        row = self.rows(self.fake(root=root))["qwenlocal"]
        self.assertEqual((row.now, row.want, row.action),
                         (1, 2, cubicles.WOULD))
        read = {name for name, _repair in self.liveness_calls}
        self.assertNotIn("qwenlocal", read)
        self.assertIn("cursor", read)

    def test_liveness_is_read_without_repair(self):
        """A plan is an observation: the registered pane resolver REPAIRS a
        stale handle by rewriting the spawn register unless told not to."""
        self.rows(self.fake())
        self.assertTrue(self.liveness_calls)
        self.assertEqual({repair for _n, repair in self.liveness_calls}, {False})

    def test_a_registered_seat_outside_the_catalog_never_moves(self):  # noqa: VACUOUS_ASSERTION — the STAYS action and the 'not a catalog seat' reason are the positive controls on the same row
        """A hand-launched lead that does hold a register is still not a
        fleet seat: it stays wherever the owner put it."""
        self.registers["acme-lead"] = {"seat": "acme-lead", "handle": "h-lead"}
        self.states["acme-lead"] = "IDLE"
        row = self.rows(self.fake())["acme-lead"]
        self.assertEqual(row.action, cubicles.STAYS)
        self.assertIsNone(row.want)
        self.assertIn("not a catalog seat", row.reason)
        self.assertNotIn("acme-lead", {n for n, _r in self.liveness_calls})

    def test_an_unread_liveness_moves_nothing(self):
        self.states["opus46"] = "UNKNOWN"
        self.states["gemini"] = RuntimeError("pane read failed")
        rows = self.rows(self.fake())
        for name in ("opus46", "gemini"):
            self.assertEqual(rows[name].action, cubicles.STAYS, name)
            self.assertIsNone(rows[name].want, name)
        self.assertIn("pane read failed", rows["gemini"].reason)


# ---------------------------------------------------------------------------
# SHAPE
# ---------------------------------------------------------------------------

class ShapeTest(Floor):

    def test_the_last_tab_in_a_pane_is_held(self):
        """Orca closes a pane when its last tab leaves, which renumbers the
        floor. MUTATION: drop the remaining-count check."""
        root = split(group("g1", tab("t-lead", "h-lead")),
                     split(group("g2", tab("t-gemini", "h-gemini")),
                           group("g3", tab("t-qwen", "h-qwen"))))
        self.states["gemini"] = "WALLED"
        row = self.rows(self.fake(root=root))["gemini"]
        self.assertEqual(row.action, cubicles.HELD)
        self.assertIn("close", row.reason)

    def test_two_seats_leaving_one_pane_keep_one_behind(self):  # noqa: VACUOUS_ASSERTION — the sorted pair must hold one WOULD and one HELD, a positive control on both rows
        self.states.update(cursor="WALLED", gemini="WALLED")
        rows = self.rows(self.fake())
        got = sorted((rows["cursor"].action, rows["gemini"].action))
        self.assertEqual(got, sorted((cubicles.WOULD, cubicles.HELD)))

    def test_a_missing_pane_is_named_and_never_made(self):  # noqa: VACUOUS_ASSERTION — the HELD action and the 'pane 3' reason are the positive controls; no move is the contract
        root = split(group("g1", tab("t-lead", "h-lead")),
                     group("g2", tab("t-cursor", "h-cursor"),
                           tab("t-qwen", "h-qwen")))
        ad = self.fake(root=root)
        for _ in range(2):
            cubicles.tick(ad, apply=True)
        row = self.rows(ad)["qwenlocal"]
        self.assertEqual(row.action, cubicles.HELD)
        self.assertIn("pane 3", row.reason)
        self.assertEqual(ad.moves(), [])

    def test_a_seat_in_a_fourth_pane_stays(self):  # noqa: VACUOUS_ASSERTION — the (now, action) pair and the 'pane 4' reason are the positive controls on the same row
        root = split(owners_floor(), group("g4", tab("t-kimi", "h-kimi")))
        self.registers["kimi"] = {"seat": "kimi", "handle": "h-kimi"}
        self.states["kimi"] = "WALLED"
        row = self.rows(self.fake(root=root))["kimi"]
        self.assertEqual((row.now, row.action), (3, cubicles.STAYS))
        self.assertIn("pane 4", row.reason)
        self.assertNotIn("kimi", {n for n, _r in self.liveness_calls})


# ---------------------------------------------------------------------------
# TICK
# ---------------------------------------------------------------------------

class TickTest(Floor):

    def test_two_agreeing_passes_move_one_tab(self):
        """The first live pass waits; the second sends exactly one move with
        Orca's own parameters, and the re-read layout confirms it."""
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        first = cubicles.tick(ad, apply=True)
        self.assertEqual(ad.moves(), [])
        self.assertTrue(any("gemini" in l and cubicles.WAITS in l
                            for l in first), first)
        second = cubicles.tick(ad, apply=True)
        self.assertEqual(ad.moves(), [{"worktree": "id:" + WID,
                                       "tabId": "t-gemini",
                                       "targetGroupId": "g1",
                                       "kind": "move-to-group"}])
        self.assertTrue(any("gemini" in l and cubicles.MOVED in l
                            for l in second), second)
        self.assertEqual(self.rows(ad)["gemini"].action, cubicles.STAYS)
        cubicles.tick(ad, apply=True)
        self.assertEqual(len(ad.moves()), 1)

    def test_a_reading_that_changes_starts_the_wait_over(self):
        ad = self.fake()
        self.states["gemini"] = "WALLED"
        cubicles.tick(ad, apply=True)
        self.states["gemini"] = "IDLE"
        cubicles.tick(ad, apply=True)
        self.states["gemini"] = "WALLED"
        cubicles.tick(ad, apply=True)
        self.assertEqual(ad.moves(), [])
        cubicles.tick(ad, apply=True)
        self.assertEqual(len(ad.moves()), 1)

    def test_a_dry_sweep_plans_and_never_moves_or_waits(self):  # noqa: VACUOUS_ASSERTION — a WOULD line in the tick's output is the positive control; no move and no waiting record are the contract
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        for _ in range(3):
            lines = cubicles.tick(ad, apply=False)
        self.assertEqual(ad.moves(), [])
        self.assertTrue(any(cubicles.WOULD in l for l in lines), lines)
        self.assertFalse(os.path.exists(self.state_file()))

    def test_the_switch_off_reads_nothing_and_says_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence IS the contract; test_two_agreeing_passes_move_one_tab drives the same fixture and records calls
        self.names({"cubicle-floor": self.floor, "cubicle-mover": "off"})
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        for _ in range(2):
            self.assertEqual(cubicles.tick(ad, apply=True), [])
        self.assertEqual(ad.calls, [])
        self.assertEqual(self.liveness_calls, [])

    def test_the_switch_dry_run_plans_and_never_moves(self):  # noqa: VACUOUS_ASSERTION — a WOULD line in the tick's output is the positive control on the same pass
        self.names({"cubicle-floor": self.floor, "cubicle-mover": "dry-run"})
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        for _ in range(2):
            lines = cubicles.tick(ad, apply=True)
        self.assertEqual(ad.moves(), [])
        self.assertTrue(any(cubicles.WOULD in l for l in lines), lines)

    def test_no_floor_is_silent_and_reads_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence IS the contract; the floor-named arms drive the same fake and record calls
        self.names({})
        ad = self.fake()
        self.assertEqual(cubicles.tick(ad, apply=True), [])
        self.assertEqual(ad.calls, [])

    def test_a_refused_move_is_a_row_never_an_exception(self):
        self.states["gemini"] = "WALLED"
        ad = self.fake(refuse="orca runtime rpc session.tabs.move: "
                              "target_group_not_found")
        cubicles.tick(ad, apply=True)
        lines = cubicles.tick(ad, apply=True)
        self.assertEqual(len(ad.moves()), 1)
        hit = [l for l in lines if "gemini" in l]
        self.assertEqual(len(hit), 1, lines)
        self.assertIn(cubicles.FAILED, hit[0])
        self.assertIn("target_group_not_found", hit[0])

    def test_a_move_the_layout_never_shows_is_sent_not_moved(self):
        """THE EFFECT, NOT THE REPLY. Orca answers moved:true once it has told
        the window. MUTATION: mark a row MOVED on the reply."""
        self.states["gemini"] = "WALLED"
        ad = self.fake(inert=True)
        cubicles.tick(ad, apply=True)
        lines = cubicles.tick(ad, apply=True)
        hit = [l for l in lines if "gemini" in l]
        self.assertEqual(len(hit), 1, lines)
        self.assertIn(cubicles.SENT, hit[0])
        self.assertNotIn(" %s" % cubicles.MOVED, hit[0])

    def test_no_runtime_rpc_degrades_to_one_line(self):
        class NoRpc(object):
            name = "orca"
        lines = cubicles.tick(NoRpc(), apply=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("RPC", lines[0])
        lines = cubicles.tick(self.fake(list_error="orca runtime rpc "
                                        "terminal.list: deadline exceeded"),
                              apply=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("deadline exceeded", lines[0])

    def test_nothing_escapes_into_the_sweep(self):
        with mock.patch.object(orcaadopt, "helm_spawned",
                               side_effect=OSError("register tree unreadable")):
            lines = cubicles.tick(self.fake(), apply=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("register tree unreadable", lines[0])


# ---------------------------------------------------------------------------
# TITLES
# ---------------------------------------------------------------------------

class TitlesTest(Floor):

    def test_the_mover_never_titles_a_tab_and_speaks_two_methods(self):
        """orcatitle.py's byte-exact title is owner-ruled and the reaper reads
        it. The fake's rename raises; the mover's whole RPC vocabulary is
        the layout read and the move. MUTATION: paint a mood on the title or
        the tab colour."""
        self.states.update(gemini="WALLED", opus46="IDLE")
        ad = self.fake()
        for _ in range(2):
            cubicles.tick(ad, apply=True)
        self.assertTrue(ad.moves())
        self.assertEqual({m for m, _p in ad.calls},
                         {"terminal.list", "session.tabs.move"})

    def test_the_source_calls_no_title_or_colour_writer(self):  # noqa: VACUOUS_ASSERTION — the method set must equal the two methods, an unconditional positive control on the same scan
        """Every `.rpc(<literal>)` in the module is one of the two methods,
        and nothing in it calls a `rename`. The must-hit control is that the
        scan finds both methods."""
        import ast
        with open(os.path.join(ROOT, "helm", "cubicles.py")) as f:
            tree = ast.parse(f.read())
        methods, renames = set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and \
                    isinstance(node.func, ast.Attribute):
                if node.func.attr == "rpc" and node.args and \
                        isinstance(node.args[0], ast.Constant):
                    methods.add(node.args[0].value)
                if node.func.attr in ("rename", "setTabProps"):
                    renames.append(node.lineno)
        self.assertEqual(methods, {"terminal.list", "session.tabs.move"})
        self.assertEqual(renames, [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliTest(Floor):

    def run_cli(self, args, ad):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness, "detect", return_value=ad), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["cubicles"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_bare_verb_is_a_dry_run_table(self):  # noqa: VACUOUS_ASSERTION — rc 0, the header, DRY RUN and the WOULD row are the positive controls
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        rc, out, err = self.run_cli([], ad)
        self.assertEqual(rc, 0, err)
        self.assertIn("SEAT", out)
        self.assertIn("DRY RUN", out)
        line = [l for l in out.splitlines() if l.startswith("gemini ")]
        self.assertEqual(len(line), 1, out)
        self.assertIn(cubicles.WOULD, line[0])
        self.assertEqual(ad.moves(), [])

    def test_apply_moves_on_one_reading(self):  # noqa: VACUOUS_ASSERTION — exactly one recorded move is the positive control
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        rc, out, err = self.run_cli(["--apply"], ad)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(ad.moves()), 1)
        self.assertNotIn("DRY RUN", out)
        self.assertFalse(os.path.exists(self.state_file()))

    def test_dry_run_wins_over_apply(self):  # noqa: VACUOUS_ASSERTION — rc 0 and DRY RUN in the output are the positive controls
        self.states["gemini"] = "WALLED"
        ad = self.fake()
        rc, out, _err = self.run_cli(["--apply", "--dry-run"], ad)
        self.assertEqual(rc, 0)
        self.assertEqual(ad.moves(), [])
        self.assertIn("DRY RUN", out)

    def test_json_carries_the_plan(self):  # noqa: VACUOUS_ASSERTION — the floor path and the (now, want, action) triple are the positive controls
        self.states["gemini"] = "WALLED"
        rc, out, err = self.run_cli(["--json"], self.fake())
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["floor"], self.floor)
        self.assertFalse(got["apply"])
        row = {r["seat"]: r for r in got["rows"]}["gemini"]
        self.assertEqual((row["now"], row["want"], row["action"]),
                         (2, 1, cubicles.WOULD))

    def test_a_refused_move_exits_one(self):
        self.states["gemini"] = "WALLED"
        rc, out, _err = self.run_cli(["--apply"], self.fake(refuse="nope"))
        self.assertEqual(rc, 1)
        self.assertIn(cubicles.FAILED, out)

    def test_no_floor_is_refused_naming_the_key(self):
        self.names({})
        rc, _out, err = self.run_cli([], self.fake())
        self.assertEqual(rc, 1)
        self.assertIn("cubicle-floor", err)

    def test_no_orca_is_refused(self):
        rc, _out, err = self.run_cli([], None)
        self.assertEqual(rc, 1)
        self.assertIn("orca", err)

    def test_an_unknown_flag_is_refused(self):
        rc, _out, _err = self.run_cli(["--now"], self.fake())
        self.assertEqual(rc, 2)


# ---------------------------------------------------------------------------
# SURFACES
# ---------------------------------------------------------------------------

class SwitchTest(Floor):

    def test_unset_is_on_and_doctor_names_the_floor(self):  # noqa: VACUOUS_ASSERTION — the ON word, the CHECKS membership, the OK level and the floor path are the positive controls
        self.assertEqual(cubicles.switch(), cubicles.ON)
        self.assertIn("check_cubicle_mover", doctor.CHECKS)
        rows = doctor.check_cubicle_mover()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], doctor.OK)
        self.assertIn(self.floor, rows[0][1])

    def test_off_is_a_doctor_warning(self):  # noqa: VACUOUS_ASSERTION — the OFF word, the WARN level and the text are the positive controls
        self.names({"cubicle-floor": self.floor, "cubicle-mover": "off"})
        self.assertEqual(cubicles.switch(), cubicles.OFF)
        rows = doctor.check_cubicle_mover()
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("OFF", rows[0][1])

    def test_a_word_the_switch_does_not_know_plans_only(self):
        self.names({"cubicle-floor": self.floor, "cubicle-mover": "yes"})
        self.assertEqual(cubicles.switch(), cubicles.DRY)
        ok, text = cubicles.switch_state()
        self.assertFalse(ok)
        self.assertIn("'yes'", text)

    def test_no_floor_is_named_by_doctor_as_the_neutral_state(self):
        self.names({})
        ok, text = cubicles.switch_state()
        self.assertTrue(ok)
        self.assertIn("cubicle-floor", text)

    def test_local_names_declares_both_keys_and_the_example_shows_them(self):
        keys = (cubicles.FLOOR_KEY, cubicles.SWITCH_KEY)
        self.assertEqual(keys, ("cubicle-floor", "cubicle-mover"))
        self.assertLessEqual(set(keys), set(localnames.KEYS))
        with open(os.path.join(ROOT, localnames.EXAMPLE)) as f:
            example = json.load(f)
        self.assertLessEqual(set(keys), set(example))
        comment = "\n".join(example["_comment"])
        self.assertEqual([k for k in keys if k in comment], list(keys))


class MoodTest(Floor):

    def test_the_mood_column_reads_seatmood_when_it_exists(self):
        mod = types.ModuleType("helm.seatmood")
        mod.mood = lambda name: {"mood": "stuck"} if name == "gemini" else "flowing"
        with mock.patch.dict(sys.modules, {"helm.seatmood": mod}):
            rows = self.rows(self.fake())
        self.assertEqual(rows["gemini"].mood, "stuck")
        self.assertEqual(rows["cursor"].mood, "flowing")
        self.assertIn("stuck", rows["gemini"].line())

    def test_an_absent_or_broken_seatmood_leaves_the_column_blank(self):  # noqa: VACUOUS_ASSERTION — the WORKING placement on the same row is the positive control: the row is planned while its mood is blank
        with mock.patch.dict(sys.modules, {"helm.seatmood": None}):
            self.assertIsNone(self.rows(self.fake())["gemini"].mood)
        mod = types.ModuleType("helm.seatmood")

        def broken(name):
            raise ValueError("ledger unreadable")
        mod.mood = broken
        with mock.patch.dict(sys.modules, {"helm.seatmood": mod}):
            row = self.rows(self.fake())["gemini"]
        self.assertIsNone(row.mood)
        self.assertEqual(row.place, cubicles.WORKING)


if __name__ == "__main__":
    unittest.main()
