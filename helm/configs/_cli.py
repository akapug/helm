import os
import json

from ._common import HOME
from ._resolve import homes_configs, tree, resolve
from ._io import read_file, write_file, list_backups, restore


# ── CLI (read-only surface over the same model) ───────────────────────────────

def _print_files(files, indent="    "):
    import sys
    for f in files:
        extra = ""
        if f.get("entries") is not None:
            extra = "  (%d entries)" % len(f["entries"])
        elif not f["editable"]:
            extra = "  [read-only: %s]" % f["reason"]
        print("%s%-32s %s%s" % (indent, f["rel"], f["kind"], extra))


def cmd_configs(args):
    """configs [list|show <path>|cascade <cwd> [--harness claude|codex|pi]
    [--home DIR]|injection [--seat SEAT] [--session SID]|edit <path>|backups|
    restore <backup>] — the config-estate
    surface. list = every discovered config file grouped by scope; show = one
    recognized file's content; cascade = what a seat at <cwd> loads (via
    physics). edit is the one write: new content on stdin, backup -> validate
    -> atomic; backups lists the snapshots that makes, restore returns one."""
    import sys
    args = list(args or [])
    verb = args.pop(0) if args else "list"

    if verb == "list":
        homes = homes_configs()
        print("helm configs — home/user scope:")
        if not homes:
            print("  (none)")
        for h in homes:
            # provider is None for a root helm cannot place — say so, rather
            # than rendering the word "None" as if it were a harness name.
            print("  %s  [%s]" % (h["path"], h["provider"] or "unknown"))
            _print_files(h["files"])
        t = tree()
        print("project scope (roots: %s):" % ", ".join(t["config_roots"]))
        def _walk(node):
            if node["files"]:
                print("  " + node["path"])
                _print_files(node["files"])
            for c in node["children"]:
                _walk(c)
        for r in t["roots"]:
            _walk(r)
        return 0

    if verb == "show":
        if not args:
            print("usage: helm configs show <path>", file=sys.stderr)
            return 2
        r = read_file(args[0])
        if r.get("error"):
            print("helm configs: %s" % r["error"], file=sys.stderr)
            return 1
        print("# %s  [%s%s]" % (r["path"], r["type"],
                                "" if r["editable"] else "; read-only: " + r["reason"]),
              file=sys.stderr)
        sys.stdout.write(r["content"])
        return 0

    if verb == "cascade":
        cwd, harness, home_p = None, "claude", None
        while args:
            a = args.pop(0)
            if a == "--harness" and args:
                harness = args.pop(0)
            elif a == "--home" and args:
                home_p = args.pop(0)
            elif not a.startswith("-") and cwd is None:
                cwd = a
            else:
                print("usage: helm configs cascade <cwd> [--harness claude|codex|pi] "
                      "[--home DIR]", file=sys.stderr)
                return 2
        if not cwd or harness not in ("claude", "codex", "pi"):
            print("usage: helm configs cascade <cwd> [--harness claude|codex|pi] "
                  "[--home DIR]", file=sys.stderr)
            return 2
        default_home = os.path.join(HOME, ".pi/agent" if harness == "pi" else ".codex" if harness == "codex" else ".claude")
        home_p = home_p or default_home
        res = resolve(home_p, cwd, harness)
        if res.get("error"):
            print("helm configs cascade: %s" % res["error"], file=sys.stderr)
            return 1
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if verb == "injection":
        from .. import injection_config
        from ..cli import guard_tail
        rc = guard_tail("helm configs injection", args,
                        valued=("--seat", "--session"),
                        usage=injection_config.INJECTION_USAGE)
        if rc is not None:
            return rc
        seat = args[args.index("--seat") + 1] if "--seat" in args else None
        session = args[args.index("--session") + 1] \
            if "--session" in args else None
        if not seat and not session:
            print("usage: " + injection_config.INJECTION_USAGE, file=sys.stderr)
            return 2
        print(json.dumps(injection_config.view(seat, session), indent=2,
                         ensure_ascii=False))
        return 0

    if verb == "edit":
        # edit <path>  (new content on stdin) — backup -> validate -> atomic
        if not args:
            print("usage: helm configs edit <path>   (new content on stdin)", file=sys.stderr)
            return 2
        if sys.stdin.isatty():
            print("helm configs edit: pipe the new content on stdin "
                  "(refusing an interactive empty write)", file=sys.stderr)
            return 2
        r = write_file(args[0], sys.stdin.read())
        if r.get("error"):
            print("helm configs edit: " + r["error"], file=sys.stderr)
            return 1
        print("helm configs: wrote %s (backup: %s)" % (r["path"], r.get("backup") or "none — new file"))
        return 0

    if verb == "backups":
        bs = list_backups()
        if not bs:
            print("helm configs: no backups yet.")
            return 0
        print("helm configs backups (%d, newest first):" % len(bs))
        for b in bs[:30]:
            print("  %s  <- %s" % (b.get("backup", "?"), b.get("orig") or "?"))
        return 0

    if verb == "restore":
        if not args:
            print("usage: helm configs restore <backup-path>", file=sys.stderr)
            return 2
        r = restore(args[0])
        if r.get("error"):
            print("helm configs restore: " + r["error"], file=sys.stderr)
            return 1
        print("helm configs: restored %s (pre-restore backup: %s)"
              % (r["path"], r.get("backup") or "none"))
        return 0

    print("usage: helm configs [list|show <path>|cascade <cwd>|"
          "injection --seat SEAT [--session SID]|edit <path>|backups|"
          "restore <backup>]", file=sys.stderr)
    return 2
