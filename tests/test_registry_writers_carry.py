"""No registry writer loses an authored key it does not own.

The authored layer (registry-authored.json) is written by more than one
version of helm at once, so a key one version authors is a key another has
never heard of. A writer that rebuilt an entry from the fields it knew erased
such a key on its next ordinary save. The field this was measured on is
`residency`: an older helm's `helm sync` would have erased two projects'
`may-leave-lan`, and outside scoring would have stopped with nothing saying
why. It fails closed, but the writer was lossy.

Two arms, each over EVERY writer of the registry files:
  * a project authors `residency` and a key no helm owns yet; every writer
    runs, and both survive where the project's authority now lives;
  * the OLD CODE SHAPE: the same writers run as a helm that does not know
    `residency` (the field tables without it), and it survives there too.

Each writer is asked for its own effect first, so a survival can never be a
writer that did nothing. Hermetic: a tmp HELM_HOME, no scan, no real registry.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import automap, home, pk, registry  # noqa: E402

OPEN = {"value": "may-leave-lan", "reason": "non-client", "by": "owner", "ts": 1}
FUTURE = {"from": "a newer helm", "n": 3}
STATE = {"colour": "yellow", "reason": "normal work", "by": "owner", "ts": 1}
CARRIED = {"residency": OPEN, "zz_future_field": FUTURE}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-registry-carry-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME", "HELM_SCAN_ROOTS")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_SCAN_ROOTS"] = self.dir("scan-root")
        os.environ.pop("MELD_HOME", None)

        def restore():
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.assertTrue(home.helm_home().startswith(self.tmp))
        noise = mock.patch.object(automap, "_is_noise", lambda cwd: False)
        noise.start()
        self.addCleanup(noise.stop)

    def dir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def plant(self):
        """alpha (a live dir, authored state + notes, like a real project),
        gone (a vanished path, for forget/restore), mover (a vanished source
        and a live target, for repoint) and beta. Every one but beta carries
        the residency field and the unowned key."""
        self.paths = {"alpha": self.dir("dev", "alpha"), "beta": self.dir("dev", "beta"),
                      "gone": os.path.join(self.tmp, "vanished", "gone"),
                      "mover": os.path.join(self.tmp, "vanished", "mover")}
        self.target = self.dir("dev", "mover-now")
        projects, entries = {}, {}
        for name, path in self.paths.items():
            projects[name] = {"name": name, "path": path, "kind": "git",
                              "status": "active", "sessions": {}}
            entries[name] = {"path": path}
            if name != "beta":
                entries[name].update(CARRIED, state=STATE, notes="keep this")
        pk.write_json(home.registry_path(), {"version": 1, "projects": projects})
        pk.write_json(home.authored_path(), {"version": 1, "projects": entries})

    def auth(self):
        return pk.read_json(home.authored_path())

    def entry(self, name, path=None):
        """The authored record the RESOLVER finds for (name, path) now."""
        _key, entry = registry.authority_record(self.auth(), name, path or self.paths[name])
        return entry or {}

    def assert_carried(self, name, path=None, fields=("residency", "zz_future_field")):
        got = self.entry(name, path)
        for field in fields:
            self.assertEqual(got.get(field), CARRIED[field],
                             "%s lost %r on %s" % (self.writer, field, name))

    # ---------------------------------------------------------- the writers
    # Each runs one writer and asserts that writer's OWN effect, so a
    # surviving field is never the survival of a writer that wrote nothing.

    def w_save(self):
        registry.save(registry.load())
        self.assertIn("generated_ts", pk.read_json(home.registry_path()))
        return ["alpha", "gone", "mover"]

    def w_sync(self):
        reg, _report = registry.sync(observations=[])
        self.assertIn("alpha", reg["projects"])
        self.assertTrue(os.path.exists(os.path.join(home.project_dir("alpha"), "registry.json")))
        return ["alpha", "gone", "mover"]

    def w_add_edge(self):
        e, err = registry.add_edge("alpha", "forked-from", "beta")
        self.assertIsNone(err)
        self.assertEqual(self.entry("alpha")["edges"][0]["to"], "beta")
        return ["alpha", "gone", "mover"]

    def w_add_external(self):
        registry.add_external("vendored", self.dir("ext", "vendored"), "a reference clone")
        self.assertTrue(self.auth()["projects"]["vendored"]["external"])
        return ["alpha", "gone", "mover"]

    def w_state(self):
        row, err = registry.state("alpha", "red", reason="frozen", by="owner", apply=True)
        self.assertIsNone(err)
        self.assertEqual(self.entry("alpha")["state"]["colour"], "red")
        row, err = registry.state("alpha", "clear", apply=True)
        self.assertIsNone(err)
        self.assertNotIn("state", self.entry("alpha"))
        return ["alpha"]

    def w_set_residency(self):
        row, err = registry.set_residency("beta", "lan-only", reason="client", by="owner",
                                          apply=True)
        self.assertIsNone(err)
        self.assertEqual(self.entry("beta")["residency"]["value"], "lan-only")
        return ["alpha", "gone", "mover"]

    def w_author_host(self):
        registry.author_host("dial", {"value": 2})
        self.assertEqual(self.auth()["host"]["dial"], {"value": 2})
        return ["alpha", "gone", "mover"]

    def w_load_migration(self):
        reg = pk.read_json(home.registry_path())
        reg["projects"]["alpha"]["aliases"] = ["al"]       # a mixed-era inline field
        pk.write_json(home.registry_path(), reg)
        registry.load()
        self.assertEqual(self.entry("alpha")["aliases"], ["al"])   # it migrated
        return ["alpha"]

    def w_forget_restore(self):
        row, err = registry.forget("gone", apply=True)
        self.assertIsNone(err)
        self.assertIn("gone", self.auth()["forgotten_projects"])
        self.assert_carried("gone")
        row, err = registry.restore("gone", apply=True)
        self.assertIsNone(err)
        self.assertNotIn("gone", self.auth().get("forgotten_projects", {}))
        return ["gone"]

    def w_repoint(self):
        row, err = registry.repoint("mover", self.paths["mover"], self.target, apply=True)
        self.assertIsNone(err)
        self.assertEqual(self.auth()["project_bindings"]["mover"]["path"], self.target)
        self.assert_carried("mover", self.target)
        # and home again: the undo is the same writer the other way
        row, err = registry.repoint("mover", self.target, undo=True, apply=True)
        self.assertIsNone(err)
        self.assertEqual(self.auth()["project_bindings"]["mover"]["path"], self.paths["mover"])
        return ["mover"]

    WRITERS = ("w_save", "w_sync", "w_add_edge", "w_add_external", "w_state",
               "w_set_residency", "w_author_host", "w_load_migration",
               "w_forget_restore", "w_repoint")

    def sweep(self, fields=("residency", "zz_future_field")):
        for name in self.WRITERS:
            with self.subTest(writer=name):
                self.plant()
                self.writer = name
                survivors = getattr(self, name)()
                for project in survivors:
                    path = self.paths[project]
                    self.assert_carried(project, path, fields)


class EveryWriterCarriesKeysItDoesNotOwn(Base):
    def test_residency_and_an_unowned_key_survive_every_writer(self):  # noqa: VACUOUS_ASSERTION — every assertion is an equality on a planted value, and each writer asserts its own effect first
        self.sweep()

    def test_the_fixture_resolves_both_keys_before_any_writer_runs(self):  # noqa: VACUOUS_ASSERTION — assert_carried is an equality on each planted value, three projects, unconditionally
        """The premise, or every survival above is about nothing."""
        self.plant()
        self.writer = "no writer"
        for name in ("alpha", "gone", "mover"):
            self.assert_carried(name)


class TheOldCodeShapeCarriesTheFieldItDoesNotKnow(Base):
    """THE CASE THAT BIT US: a helm whose field tables do not name `residency`
    builds its save from the fields it knows. It must still carry the field."""

    def test_a_writer_that_does_not_know_residency_keeps_it(self):  # noqa: VACUOUS_ASSERTION — every assertion is an equality on a planted value, and each writer asserts its own effect first
        old = tuple(f for f in registry.AUTHORED_FIELDS if f != "residency")
        old_never = tuple(f for f in registry.NEVER_MIGRATED_FIELDS if f != "residency")
        with mock.patch.object(registry, "AUTHORED_FIELDS", old), \
                mock.patch.object(registry, "NEVER_MIGRATED_FIELDS", old_never):
            self.assertNotIn("residency", registry.AUTHORED_FIELDS)     # the premise
            self.WRITERS = tuple(w for w in Base.WRITERS if w != "w_set_residency")
            self.sweep()


class AKeyClearedAtTheCurrentLocationStaysClearedAcrossAnUndo(Base):
    """A repoint carries the DEPARTED entry's unowned keys and never the
    destination's. The destination of an undo is the project's own stale
    entry from before the move, so taking its keys would bring back a key
    that was cleared at the current location: for a helm that does not know
    `residency`, a cleared may-leave-lan would come back open."""

    def move_clear_undo(self, field, clear):
        self.plant()
        self.writer = "repoint"
        row, err = registry.repoint("mover", self.paths["mover"], self.target, apply=True)
        self.assertIsNone(err)
        self.assert_carried("mover", self.target, (field,))           # it moved
        clear()
        self.assertNotIn(field, self.entry("mover", self.target))     # cleared here
        # THE PREMISE: the stale entry at the old location still holds it.
        self.assertEqual(self.auth()["projects"]["mover"].get(field), CARRIED[field])
        row, err = registry.repoint("mover", self.target, undo=True, apply=True)
        self.assertIsNone(err)
        self.assertEqual(self.auth()["project_bindings"]["mover"]["path"], self.paths["mover"])
        return self.entry("mover")

    def test_S4_a_cleared_unowned_key_is_not_resurrected_by_an_undo(self):  # noqa: VACUOUS_ASSERTION — move_clear_undo asserts the key moved, was cleared, and still sits in the stale entry before the undo, and the other carried field is asserted back
        def clear():                      # a newer helm that owns the key removes it
            auth = self.auth()
            _key, entry = registry.authority_record(auth, "mover", self.target)
            del auth["projects"][_key]["zz_future_field"]
            pk.write_json(home.authored_path(), auth)
        back = self.move_clear_undo("zz_future_field", clear)
        self.assertNotIn("zz_future_field", back)
        self.assertEqual(back.get("residency"), OPEN)                  # the rest moved back

    def test_S5_an_older_helm_never_reopens_a_cleared_may_leave_lan(self):  # noqa: VACUOUS_ASSERTION — move_clear_undo asserts the field moved and still sits in the stale entry before the undo; residency() is asserted to read lan-only and the other carried key is asserted back
        old = tuple(f for f in registry.AUTHORED_FIELDS if f != "residency")
        old_never = tuple(f for f in registry.NEVER_MIGRATED_FIELDS if f != "residency")

        def clear():                      # the owner closes the door at the new location
            row, err = registry.set_residency("mover", "clear", apply=True)
            self.assertIsNone(err)
        with mock.patch.object(registry, "AUTHORED_FIELDS", old), \
                mock.patch.object(registry, "NEVER_MIGRATED_FIELDS", old_never):
            back = self.move_clear_undo("residency", clear)
        self.assertNotIn("residency", back)
        self.assertEqual(registry.residency("mover", self.paths["mover"])["value"], "lan-only")
        self.assertEqual(back.get("zz_future_field"), FUTURE)          # the rest moved back


class DuplicateSpellingsFoldOnlyWhenTheySayTheSameThing(Base):
    """A save folds two spellings of one (project, location) into one key. A
    key only the folded spelling held would be deleted with it, so two
    spellings that differ in ANY key are two records, and the resolver
    refuses them rather than choosing."""

    def test_an_unowned_difference_is_a_different_record(self):
        path = self.dir("dev", "alpha")
        stamped = registry._qualified("alpha", path)
        same = {"path": path, "notes": "n"}
        self.assertTrue(registry._same_authority_record(same, dict(same)))       # the control
        self.assertTrue(registry._same_authority_record(
            dict(same, zz_future_field=FUTURE), dict(same, zz_future_field=FUTURE)))
        self.assertFalse(registry._same_authority_record(
            same, dict(same, zz_future_field=FUTURE)))
        pk.write_json(home.registry_path(), {"version": 1, "projects": {"alpha": {
            "name": "alpha", "path": path, "kind": "git", "status": "active", "sessions": {}}}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            "alpha": same, stamped: dict(same, zz_future_field=FUTURE)}})
        with self.assertRaises(ValueError):
            registry.save(registry.load())
        self.assertEqual(self.auth()["projects"][stamped]["zz_future_field"], FUTURE)


if __name__ == "__main__":
    unittest.main()
