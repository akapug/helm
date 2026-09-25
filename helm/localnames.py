#!/usr/bin/env python3
"""THIS HOST'S OWN NAMES, which the source must not carry.

A few of helm's behaviours depend on a name that only the operator's machine
knows: the tool helm replaced there, whose variables and directories it still
honours; the provider name a live proxy config was minted under; the directory
a deployed gate runs from. The source names none of them. Each is one key of
one JSON object under the helm home, `<helm home>/_global/local-names.json`
(the shape is in docs/local-names.example.json), and every key is optional:
without it, the behaviour it names is off or takes its neutral default. A
fresh clone therefore needs no file at all.

ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS (`home.global_json`), and here
they configure the same thing, nothing: a caller cannot act on half a file,
and every key's absent answer is its safe one. The difference is not lost:
`problems()` names an unreadable file, an unknown key and a value of the wrong
shape, and `helm doctor` reports them, so a broken file is never silent.

READ THROUGH A CACHE KEYED ON THE FILE'S STAT. A caller on a hot path (an
environment lookup) re-stats and never re-parses, and an edit needs no
restart. A module that binds a value at import (a table key, a module
constant) sees an edit on its next process start.

Import-safe and stdlib-only. It also imports by pathname, because
helm/catalog.py runs as a script and reads the predecessor through it.
"""
import os
import re

try:
    from . import home
except ImportError:                   # run by pathname: no package parent
    import home

CONFIG = "local-names.json"
EXAMPLE = "docs/local-names.example.json"

#: Every key this file may carry, the shape of its value, and what reads it.
#: A key that is not listed is never read, so `problems()` names it rather
#: than letting a misspelling configure nothing in silence.
KEYS = {
    "predecessor": ("name", "the tool helm replaced on this host: its "
                    "<NAME>_* variables, cache, config and archive directories "
                    "are still honoured (catalog, configs, corpus, homes, "
                    "keepalive, providers, web)"),
    "qwen27-provider": ("text", "the provider name the qwen27 family's local "
                        "pool row goes by; a live proxy config and its proofs "
                        "carry it (seat_catalog)"),
    "deploy-dir": ("text", "the directory a deployed gate's scripts run from, "
                   "compared with their committed source (doctor)"),
    "deploy-project": ("text", "the registered project whose checkout owns "
                       "those scripts (doctor)"),
    "local-review-script": ("text", "the findings pass's reading script, "
                            "unless HELM_LOCAL_REVIEW_SCRIPT names one "
                            "(findingspass)"),
    "fab-node": ("text", "the build node an operator's A/B eval rig runs on "
                 "when its --node is not given (the rig does not ship "
                 "with helm)"),
    "generic-keywords": ("words", "extra words the knowledge store never "
                         "treats as a topic (store)"),
}

#: A predecessor's name becomes an environment prefix and a path component,
#: so it is a bare lowercase identifier and nothing else.
_NAME = re.compile(r"\A[a-z][a-z0-9]{0,31}\Z")

_cache = {"stat": None, "table": None, "why": None, "path": None}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_cache": (
        "the local-names table keyed by the file's stat; a changed file "
        "misses"),
}


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _read():
    """(path, table or None, why or None), re-parsed only when the file's
    stat changes."""
    path = os.path.join(home.global_dir(), CONFIG)
    key = (path, _stat(path))
    if _cache["stat"] != key:
        _path, table, why = home.global_json(CONFIG)
        _cache.update(stat=key, table=table, why=why, path=path)
    return _cache["path"], _cache["table"], _cache["why"]


def _valid(key, value):
    shape = KEYS[key][0]
    if shape == "name":
        return isinstance(value, str) and bool(_NAME.match(value))
    if shape == "text":
        return isinstance(value, str) and bool(value.strip())
    return isinstance(value, list) and all(
        isinstance(v, str) and v.strip() for v in value)


def value(key):
    """The configured value of one listed key, or None: absent, unreadable
    and ill-shaped all configure nothing (see `problems`)."""
    if key not in KEYS:
        raise KeyError("local-names key %r is not declared in KEYS" % key)
    _path, table, why = _read()
    got = (table or {}).get(key) if not why else None
    if got is None or not _valid(key, got):
        return None
    return got.strip() if isinstance(got, str) else tuple(
        v.strip() for v in got)


def words(key):
    """A "words" key as a tuple, empty when unset."""
    return value(key) or ()


def problems():
    """[why] for everything in the file that configures nothing although it is
    written there: an unreadable file, an unknown key, a value of the wrong
    shape. Empty for an absent file and for a good one."""
    path, table, why = _read()
    if why:
        return [why]
    out = []
    for key, got in sorted((table or {}).items()):
        if key.startswith("_"):
            continue                  # "_comment" and friends document the file
        if key not in KEYS:
            out.append("%s: unknown key %r (the keys are listed in %s)"
                       % (path, key, EXAMPLE))
        elif not _valid(key, got):
            out.append("%s: %r is not a valid %s value" % (path, key,
                                                           KEYS[key][0]))
    return out


# ---------------------------------------------------------------- predecessor

def predecessor():
    """The tool helm replaced on this host, or None."""
    return value("predecessor")


def legacy_env_key(name):
    """The predecessor's spelling of one variable (`<NAME>_<name>`), or None
    when no predecessor is configured."""
    pred = predecessor()
    return "%s_%s" % (pred.upper(), name) if pred else None


def legacy_env(name, default=None):
    """The predecessor's spelling of one variable, read as a fallback only:
    callers ask for HELM_<name> first. None (or `default`) when no
    predecessor is configured or the variable is unset."""
    key = legacy_env_key(name)
    got = os.environ.get(key) if key else None
    return default if got is None else got


def legacy_path(*parts):
    """A predecessor path under the user's home. Each part may carry `{}`,
    which becomes the predecessor's name: `legacy_path(".cache", "{}")`.
    None when no predecessor is configured."""
    pred = predecessor()
    if not pred:
        return None
    return os.path.join(os.path.expanduser("~"),
                        *(p.replace("{}", pred) for p in parts))
