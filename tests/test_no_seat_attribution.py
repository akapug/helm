"""THE SEAT-ATTRIBUTION GATE, IN THE SUITE (task/3296, the 0.3.5 leak bar).

No helm/ production line may name a seat as the one who found or reviewed something unless the private KEEP list
names that exact line, and no KEEP entry may match nothing. This runs the release tool's own predicate
(scripts/release/release.py: load_private, parse_keep, parse_patterns, attribution), so there is one definition,
not a copy. The seat patterns and the KEEP list are private and live outside the tree (~/.config/helm, mode 600).
Where they are absent the test says NOT RUN and skips; under HELM_GATE_REQUIRE_PRIVATE=1, which the land gate
sets on its hosts, their absence FAILS. A failure names path:line only, never the line or the pattern.
"""
import importlib.util
import os
import pathlib
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _release_tool():
    path = os.path.join(ROOT, "scripts", "release", "release.py")
    spec = importlib.util.spec_from_file_location("helm_release_tool", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


class NoSeatAttributionTest(unittest.TestCase):

    def test_helm_names_no_seat_as_finder_unless_kept(self):
        tool = _release_tool()
        private = os.path.expanduser(tool.PRIVATE)
        keep, keep_why = tool.load_private(os.path.join(private, tool.KEEP_FILE),
                                           "the KEEP list", tool.parse_keep, [ROOT])
        patterns, patterns_why = tool.load_private(
            os.path.join(private, tool.PATTERNS_FILE), "the private patterns",
            tool.parse_patterns, [ROOT])
        unusable = [why for why in (keep_why, patterns_why) if why]
        if unusable:
            reason = ("NOT RUN: the seat-attribution check needs its private files: %s"
                      % "; ".join(unusable))
            if os.environ.get("HELM_GATE_REQUIRE_PRIVATE") == "1":
                self.fail(reason)
            self.skipTest(reason)
        names = subprocess.check_output(["git", "ls-files", "helm"], cwd=ROOT, text=True).split()
        files = {name: pathlib.Path(ROOT, name).read_bytes() for name in names}
        _scanned, _named, found = tool.attribution(files, patterns["seat"])
        seen = {(path, text) for path, _n, text in found}
        unkept = ["%s:%d" % (path, n) for path, n, text in found if (path, text) not in keep]
        stale = sorted({path for path, text in keep if (path, text) not in seen})
        self.assertEqual(unkept, [], "these helm/ lines name a seat as the one who found or "
                         "reviewed something and are not on the KEEP list: reword them to the role "
                         "or family, or add a KEEP entry")
        self.assertEqual(stale, [], "KEEP entries in these files match no line (stale)")


if __name__ == "__main__":
    unittest.main()
