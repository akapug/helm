import contextlib
import os
import stat
import threading

from ._common import (
    HOME, CWD_ROOTS, HOME_ROOTS, MANAGED_DIRS, _HELM_HOME,
    _DENY_FILES, _PROJECT_FILES, _HOME_FILES, _HOME_SUBDIRS,
    _RULE_RE, _HOME_SUBDIR_RE, _MAX_CONFIG_BYTES,
)


# THE MEMO IS OPT-IN, PER THREAD, AND PER BLOCK. Nothing outside a
# `resolving()` block is memoized at all.
_resolving = threading.local()


def _real(p):
    """realpath+expanduser — memoized inside a `resolving()` block.

    Building one configs payload asks for the same handful of paths thousands
    of times over. Every recognized file runs `classify_path`, which resolves
    its own parent, then every allowlisted root until one matches, then every
    home root's plugins/ dir; a payload of a few hundred directories on the
    owner's dev tree costs about half a million lstats, nearly all of them
    re-answering a question already answered.

    A memo held for one enumeration is exactly as fresh as the enumeration it
    serves — the walk it feeds is a snapshot of the tree either way.
    """
    memo = getattr(_resolving, "memo", None)
    if memo is None:
        return os.path.realpath(os.path.expanduser(p))
    hit = memo.get(p)
    if hit is None:
        hit = memo[p] = os.path.realpath(os.path.expanduser(p))
    return hit


@contextlib.contextmanager
def resolving():
    """Memoize `_real` for the duration of ONE read-only enumeration.

    READ PATHS ONLY, AND DELIBERATELY SO. helm's config WRITERS never open a
    block: a writer that resolved a path for its gate and reused that answer
    after the gate would re-open the alias window `_candidate` exists to close.
    The enumeration has no such window — it decides nothing about a path except
    whether to show it.

    Re-entrant (the outermost block owns the memo, so a nested one neither
    replaces nor discards it) and thread-local (a background refresh and a
    request thread never share one).
    """
    if getattr(_resolving, "memo", None) is not None:
        yield
        return
    _resolving.memo = {}
    try:
        yield
    finally:
        _resolving.memo = None


def _error(code, message):
    """A class-only failure safe to return through HTTP/CLI.

    OSError strings and refused caller paths are intentionally not reflected:
    either can disclose an unrelated path from outside the owner config surface.
    """
    return {"error": message, "code": code}


def _absolute(path):
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    return os.path.abspath(os.path.expanduser(path))


def _candidate(path):
    """Resolve the parent, never the leaf.

    realpath(path) silently turns a symlink file into its target and loses the
    fact that the owner selected an alias. Keeping the leaf unresolved lets
    lstat/open(O_NOFOLLOW)/fstat reject aliases and devices explicitly.
    """
    ap = _absolute(path)
    if ap is None:
        return None
    return os.path.join(_real(os.path.dirname(ap)), os.path.basename(ap))


def _path_below(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _root_match(path):
    """Return the canonical allowlisted root matching the caller's lexical path.

    Both the lexical and resolved-parent paths must remain in the same root.
    This denies /tmp/alias -> ~/.claude escapes while still supporting a root
    which is itself configured as a symlink. Symlink components below a root
    are denied; discovery emits canonical paths, so aliases never become a
    second identity for one file.
    """
    ap, rp = _absolute(path), _candidate(path)
    if ap is None or rp is None:
        return None
    roots = list(CWD_ROOTS) + list(HOME_ROOTS) + [_HELM_HOME]
    for root in roots:
        raw, real = _absolute(root), _real(root)
        if raw is None:
            continue
        base = raw if _path_below(ap, raw) else real if _path_below(ap, real) else None
        if base is None or not _path_below(rp, real):
            continue
        rel = os.path.relpath(ap, base)
        cur = base
        for part in rel.split(os.sep)[:-1]:
            if part in ("", "."):
                continue
            cur = os.path.join(cur, part)
            try:
                if stat.S_ISLNK(os.lstat(cur).st_mode):
                    return None
            except FileNotFoundError:
                break
            except OSError:
                return None
        return real
    holder = _home_holder(rp)
    if _is_seat_home(holder):
        real = _real(holder)
        if _path_below(ap, real) and _path_below(rp, real):
            cur = real
            for part in os.path.relpath(ap, real).split(os.sep)[:-1]:
                if part in ("", "."):
                    continue
                cur = os.path.join(cur, part)
                try:
                    if stat.S_ISLNK(os.lstat(cur).st_mode):
                        return None
                except FileNotFoundError:
                    break
                except OSError:
                    return None
            return real
    return None


def _lstat_regular(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return st if stat.S_ISREG(st.st_mode) else False


def _home_holder(path):
    parent = os.path.dirname(path)
    if os.path.basename(parent) in _HOME_SUBDIRS:
        return os.path.dirname(parent)
    return parent


def _is_seat_home(parent):
    """True iff `parent` is a seat's isolated claude config dir —
    <helm_home>/_global/seats/<family>/claude, OR (slice 6) an instance's
    <helm_home>/_global/seats/<family>/instances/<seat>/claude. Computed per
    call (env-honoring, NOT the import-time glob) so a seat minted after
    import (`helm seat add` wires its delivery hooks in the same process) is
    recognized by the write gate. Same trust surface as the HOME_ROOTS glob
    that catches pre-existing seats; creds beside it stay denied by name."""
    from .. import home
    rp = _real(parent)
    if os.path.basename(rp) != "claude":
        return False
    sroot = _real(os.path.join(home.global_dir(), "seats"))
    up1 = os.path.dirname(rp)                             # <family> | <seat>
    up2 = os.path.dirname(up1)                            # seats | instances
    if up2 == sroot:
        return True                                       # seats/<family>/claude
    # instance: seats/<family>/instances/<seat>/claude
    return (os.path.basename(up2) == "instances"
            and os.path.dirname(os.path.dirname(up2)) == sroot)


def _is_recognized_config(rp):
    """True iff rp is a RECOGNIZED config file — the single gate for both reading
    content and editing. Extension alone is NOT enough (a package.json, a random
    settings.json, or a credential store must never qualify): a project file must
    match a known rel-path pattern, a home file must sit directly in a home root.
    This is the fix for the review's arbitrary-read (#1) + arbitrary-write (#2)."""
    base = os.path.basename(rp)
    if base in _DENY_FILES:
        return False
    # project-scoped: the path ends with a known rel pattern (at any cwd depth)
    if any(rp.endswith("/" + rel) for rel in _PROJECT_FILES):
        return True
    if _RULE_RE.search(rp):
        return True
    # home-scope DECLARATIVE config one level down (commands/, rules/): the
    # subdir must sit directly in a recognized home root, so a stray
    # commands/foo.md anywhere else on disk still fails the gate.
    m = _HOME_SUBDIR_RE.search(rp)
    if m:
        holder = _real(os.path.dirname(os.path.dirname(rp)))
        if any(holder == _real(h) for h in HOME_ROOTS) or _is_seat_home(holder):
            return True
    # home/user-scope: the recognized basename sits DIRECTLY in a home root
    if base in _HOME_FILES:
        parent = _real(os.path.dirname(rp))
        if any(parent == _real(h) for h in HOME_ROOTS) or _is_seat_home(parent):
            return True
        if base == ".claude.json" and parent == _real(HOME):  # the ~/.claude.json sibling
            return True
    return False


def _ext_type(path):
    """Validation type for a path by its recognized shape, else 'other'."""
    base = os.path.basename(path)
    if base.endswith((".mcp.json",)) or base in (".claude.json", "settings.json",
                                                 "settings.local.json", "hooks.json"):
        return "json"
    if base.endswith(".json"):
        return "json"
    if base.endswith(".toml"):
        return "toml"
    if base.endswith(".md"):
        return "md"
    if base.endswith(".rules"):
        # No parser to validate against, so it rides the text path — but it
        # IS recognized, which is what makes it editable at all.
        return "text"
    return "other"


def _under(path, roots):
    rp = _real(path)
    return any(rp == _real(r) or rp.startswith(_real(r).rstrip("/") + "/") for r in roots)


def _is_plugin_or_managed(path):
    rp = _real(path)
    if any(_path_below(rp, _real(m)) for m in MANAGED_DIRS):
        return True
    # plugin-provided config lives under a home's plugins/ tree — read-only here.
    # Match the actual plugins install dir, not any directory merely NAMED 'plugins'
    # (a project's own plugins/ dir is a legit editable location).
    return any(rp.startswith(_real(os.path.join(h, "plugins")) + os.sep) for h in HOME_ROOTS)


def classify_path(path):
    """(type, editable, reason) for one leaf without following that leaf.

    Existing entries must be regular, owner-writable files. Missing recognized
    leaves may be created only below a real allowlisted parent (or one direct
    config subdirectory below it). All callers share this classification gate.
    """
    rp = _candidate(path)
    typ = _ext_type(rp or "")
    if rp is None:
        return typ, False, "invalid path"
    if os.path.basename(rp) in _DENY_FILES:
        return typ, False, "credential/token store — never editable"
    if not _is_recognized_config(rp):
        return typ, False, "not a recognized config file"
    if _root_match(path) is None:
        return typ, False, "outside the allowlisted config roots"
    holder = _home_holder(rp)
    if not (_under(rp, CWD_ROOTS + HOME_ROOTS) or _is_seat_home(holder)):
        return typ, False, "outside the allowlisted config roots"
    if _is_plugin_or_managed(rp):
        return typ, False, "plugin/managed-provided — read-only"
    st = _lstat_regular(rp)
    if st is False:
        return typ, False, "not a regular file"
    if st is not None:
        if st.st_size > _MAX_CONFIG_BYTES:
            return typ, False, "file exceeds the editor size limit"
        if not (st.st_mode & stat.S_IWUSR):
            return typ, False, "owner read-only"
        return typ, True, "ok"
    parent = os.path.dirname(rp)
    if not os.path.isdir(parent) and not os.path.isdir(os.path.dirname(parent)):
        return typ, False, "parent directory does not exist"
    return typ, True, "ok"
