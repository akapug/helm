#!/usr/bin/env python3
"""pk — shared personal-knowledge store primitives (helm's copy of the pkstore
contract, kept format-compatible with the live store so existing hook consumers
and helm read/write the same files).

  - slug()                     filename-safe entry naming
  - parse_simple_frontmatter() the no-YAML-dep fenced key:value reader
  - atomic_write()             tmp+rename; a torn entry is never visible
  - now_ts()                   one timestamp format everywhere
  - cut_marked()               a bounded value that SAYS it was bounded
  - event()/read_events()      the ONE mutation-receipt chokepoint: every
                               store-adjacent WRITE appends one line to
                               _global/.state/events.jsonl (fire-ledger laws)

Import-safe, side-effect-free at import.
"""
import calendar
import os
import re
import threading
import time


def slug(s, cap=60):
    return re.sub(r"[^A-Za-z0-9_-]", "-", (s or "")).strip("-").lower()[:cap] or "default"


def now_ts():
    return epoch_ts(time.time())


def epoch_ts(epoch):
    """One UTC epoch -> the Helm timestamp spelling `now_ts` writes."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_ts_epoch(value):
    """One Helm timestamp -> UTC epoch seconds, or None when unknowable.

    Helm's authored rows carry three measured shapes: full seconds, omitted
    seconds, and date-only. The parser refuses rather than choosing a failure
    policy: a window caller may turn None into zero, while an age renderer must
    leave it unknown instead of inventing roughly 20,000 days. A future stamp
    parses normally; deciding that it cannot establish an age belongs to the
    age caller because this primitive owns syntax, not a clock.
    """
    if not isinstance(value, str):
        return None
    stamp = value.strip()
    if stamp.endswith("Z"):
        stamp = stamp[:-1]
    if not stamp:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(stamp, fmt))
        except ValueError:
            pass
    return None


class FrontmatterSyntaxError(ValueError):
    """Strict syntax failure with location metadata, never a source value."""

    REASONS = {
        "malformed-field": "malformed simple frontmatter field",
        "duplicate-field": "duplicate frontmatter field",
        "missing-fence": "frontmatter is absent or unterminated",
    }

    def __init__(self, reason, path, line, key=None):
        super().__init__(self.REASONS[reason])
        self.reason = reason
        self.path = os.fsdecode(path)
        self.line = line
        self.key = key


# THE FLAT FRONTMATTER'S FIELD GRAMMAR, IN ONE PLACE. The reader below and the
# representability question beneath it both go through these two, so a writer
# can ask what this grammar can carry WITHOUT transcribing its byte set beside
# it — a transcribed set is how a grammar and its guard drift apart.
_FIELD_LINE = re.compile(r"^\s*([A-Za-z_]+):\s*(.*)$")


def _field_value(raw):
    """A captured field value as the reader hands it on: surrounding whitespace
    and surrounding quotation marks removed."""
    return raw.strip().strip('"')


def field_value_is_representable(value):
    """Can this flat frontmatter carry `value` as ONE field value and hand it
    back UNCHANGED? Asked by DERIVATION, never by a list of bad bytes: the line
    a writer would render is rendered, split the way the reader splits, and read
    back through the grammar above.

    The refused set follows from that and is not a policy: anything that ends
    the line early (a newline, a carriage return, a vertical tab, a form feed, a
    Unicode line break) hands the remainder to the reader AS A FORGED FIELD, and
    a leading or trailing quotation mark comes back stripped. A value the
    grammar does carry — internal spaces, internal tabs, an internal colon — is
    untouched by this, which is what keeps the question separate from whether
    the value is a valid name in some OTHER contract."""
    text = str(value)
    rendered = "  field: " + text
    lines = rendered.splitlines()
    if len(lines) != 1:
        return False                     # the value ended the field's own line
    km = _FIELD_LINE.match(lines[0])
    return bool(km) and km.group(1) == "field" and _field_value(km.group(2)) == text


def parse_simple_frontmatter(path, defaults, list_keys=(), strict=False):
    """One entry file -> dict from `defaults` (copied), filled from the fenced
    frontmatter's flat OR metadata-nested `key: value` lines. Fail-open:
    unreadable -> None. `list_keys` values split on '||'. Authority callers use
    `strict=True`: read/decode failure propagates instead of claiming absence.
    """
    try:
        with open(path, encoding="utf-8", errors="strict" if strict else "replace") as f:
            raw = f.read()
    except Exception:
        if strict:
            raise
        return None
    e = dict(defaults)
    in_fm, closed = False, False
    seen = set()
    line_number = 0
    for line_number, line in enumerate(raw.splitlines(), 1):
        if line.strip() == "---":
            if in_fm:
                closed = True
                break
            in_fm = True
            continue
        if not in_fm:
            continue
        km = _FIELD_LINE.match(line)
        if not km:
            if strict and line.strip() and not line.lstrip().startswith("#"):
                raise FrontmatterSyntaxError("malformed-field", path, line_number)
            continue
        k, v = km.group(1).lower(), _field_value(km.group(2))
        if strict and k in e and k in seen:
            raise FrontmatterSyntaxError("duplicate-field", path, line_number, key=k)
        seen.add(k)
        if k in list_keys:
            e[k] = [x.strip() for x in v.split("||") if x.strip()] if v else []
        elif k in e:
            e[k] = v
    if strict and not closed:
        raise FrontmatterSyntaxError("missing-fence", path, line_number + 1)
    return e


def atomic_write(path, text, mode=None):
    """Replace `path` with `text` (str, or bytes written verbatim) in one
    rename. `mode`, when given, is the new file's permission bits from its
    creation on — a private store must never exist world-readable, even as a
    temporary."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # tmp name is unique PER WRITER (pid+thread): a shared path+'.tmp' loses
    # one of two concurrent same-path writers — the first os.replace steals
    # the other's tmp, whose own replace then dies FileNotFoundError and its
    # write silently vanishes
    tmp = "%s.%d.%x.tmp" % (path, os.getpid(), threading.get_ident())
    opener = None
    if mode is not None:
        def opener(name, flags):
            return os.open(name, flags, mode)
    if isinstance(text, bytes):
        # BYTES LAND AS BYTES: the dispatch fold checkpoint's payload is a
        # binary record (task/2770), and pushing it through a text encoding
        # would be a second format with its own way to be wrong.
        with open(tmp, "wb", opener=opener) as f:
            f.write(text)
    else:
        with open(tmp, "w", encoding="utf-8",       # never the locale's guess —
                  opener=opener) as f:              # readers open utf-8
            f.write(text)                           # explicitly
    os.replace(tmp, path)


class NotRegularFile(OSError, ValueError):
    """A store or config path that opens something other than a regular file.
    Both an OSError and a ValueError, so a reader that already answers an
    unreadable or an unparseable file answers this one the same way."""


def _nonblocking(path, flags):
    return os.open(path, flags | os.O_NONBLOCK)


def open_regular(path, *args, **kwargs):
    """`open(path, ...)` for READING a store or config file by path, refusing
    anything but a regular file before a read can block on it.

    `open()` on a FIFO with no writer never returns, so a FIFO where a store
    belongs hung every reader that opened it blocking (task/2530, 2536, 2523,
    2543). The call is still `open(path, *args)` with only an opener added,
    so the file checked is the file read, and a double of `open` still sees
    the path and the arguments the reader passed. O_NONBLOCK does nothing to
    a regular file. Raises NotRegularFile (closing what it opened) for a FIFO,
    socket or device; a missing path still raises FileNotFoundError and a
    directory IsADirectoryError, exactly as `open` does."""
    import stat
    f = open(path, *args, opener=_nonblocking, **kwargs)
    try:
        mode = os.fstat(f.fileno()).st_mode
        if stat.S_ISREG(mode):
            return f
        raise NotRegularFile("%s is not a regular file (%s)"
                             % (path, stat.filemode(mode)))
    except BaseException:
        f.close()
        raise


def read_json(path, default=None, strict=False):
    """Read JSON; strict authority reads distinguish missing from failed.

    Every read goes through `open_regular`: a strict read of a FIFO raises
    naming it (task/2530, which hung `helm doctor`); a lenient read answers
    its default, as for any file it cannot read (task/2536, which hung
    `helm seat composers`)."""
    import json
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field %s" % key)
            value[key] = item
        return value
    try:
        with open_regular(path, encoding="utf-8") as f:
            if not strict:
                return json.load(f)
            return json.load(f, object_pairs_hook=unique)
    except FileNotFoundError:
        if strict and os.path.islink(path):
            raise
        return default
    except Exception:
        if strict:
            raise
        return default


def write_json(path, obj):
    import json
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


# ---------------------------------------------------------------------------
# bounded values — a cut value says so
# ---------------------------------------------------------------------------


def cut_notice(kept, total):
    """The notice that turns a cut value into one which says it was cut.

    It names BOTH sizes, so a reader learns not only that the value is short
    but by how much, and can tell a value that lost three characters from one
    that lost three thousand. It is part of the VALUE, never a flag beside it:
    every reader that can render the field at all renders the loss, including
    readers that never learn the field has a bound."""
    return " … [cut: %d of %d chars]" % (kept, total)


def cut_marked(text, keep):
    """`text` bounded to `keep` characters, SAYING SO when anything is lost.

    A value that fits comes back UNCHANGED -- byte-identical to its input, no
    notice -- so the notice is evidence of a real loss and never decoration.
    Past the bound the result is the first `keep` characters plus
    `cut_notice`, which makes the RESULT longer than `keep`: the argument is a
    display width for the eye, not a storage limit for the medium. A caller
    whose bound is on the bytes it actually writes -- a pipe, a serialized row
    -- must size the notice IN rather than pass its medium's limit here; see
    `resumeturn._fit_reason`, which searches this function for the widest
    `keep` whose serialized payload still fits the pipe.

    None and non-strings are stringified first, because the callers persist
    whatever they were handed and a `None` that became the four characters
    "None" is not a cut."""
    text = "" if text is None else str(text)
    if len(text) <= keep:
        return text
    return text[:keep] + cut_notice(keep, len(text))


def description_line(text, width):
    """The `description:` value of a front-matter entry: ONE quoted line.

    A handoff, a reflex and a whoami note each render a lead line into this
    field and each cut it at its own width, so a reader of the shelf could not
    tell a glance from a whole thought. The BOUND is right — the value is a
    glance at an entry whose whole text sits in the body directly below it —
    and only the silence was wrong, so this marks the cut and keeps the width.

    Whitespace collapses and a double quote becomes a single one because the
    result is written INSIDE a `"..."` field: a newline or a quote in the
    value tears the front-matter of the entry carrying it, and
    `parse_simple_frontmatter` then reads back an entry nobody wrote.
    """
    one = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    return cut_marked(one.replace('"', "'"), width)


# ---------------------------------------------------------------------------
# events journal — mutation receipts through ONE chokepoint
# ---------------------------------------------------------------------------

EVENTS_MAX = 5 * 1024 * 1024  # journal rotates here (one .1 generation)

#: The summary's DISPLAY WIDTH. `helm store events`, the native ledger page
#: and the scratch reaper's line all render this field in one row of a table,
#: so it stays narrow for the eye.
SUMMARY_WIDTH = 200

#: The summary's STORAGE BOUND, on the second key that carries the whole
#: value. MEASURED over the live journal: 644 of 1886 rows sat at exactly
#: SUMMARY_WIDTH — a third of the trail was cut, and the widest producers are
#: dispatch briefs and handoff summaries, which run to thousands of
#: characters. Keeping them costs one key on a third of the rows; a
#: by-reference file would cost a second write and a GC obligation per event,
#: against a seam whose HARD LAW is one stat plus one append. The bound is
#: what stops a single pathological row from eating a rotation generation: at
#: this width one row is under a thousandth of EVENTS_MAX, and a value past it
#: says so in the same breath.
SUMMARY_MAX = 4000


def events_path():
    from . import home
    return os.path.join(home.global_dir(), ".state", "events.jsonl")


def event(verb, target, summary, actor=None):
    """The mutation-receipt seam: a writer calls this AFTER its write lands and
    ONE {v, ts, actor, verb, target, summary} line lands on events.jsonl,
    carrying a seventh key `summary_full` when the summary is wider than the
    table column `summary` renders.
    Receipts, never truth — the files stay source and nothing may read the
    journal over them (a hand-edit is legal; it just has no receipt). The
    .state/ home is DELIBERATE (host-local telemetry, never ships — the
    AUTHORED/DERIVED split): a rotating lossy trail is not a durable record;
    durable provenance rides IN the artifacts (evidence_log/attest_*/
    tombstones), and shipping the journal would invite reading it as a third
    store. The fire-ledger's HARD LAWS apply: O(1) (one stat + one append, never a read),
    5MB one-generation rotation (-> .1), FAIL-OPEN — journal trouble must never
    block or fail the write it describes. actor: explicit arg, else
    $HELM_ACTOR, else the harness session id (home.session_id()), else 'cli'."""
    import json
    from . import home            # local: pk stays free of a home import cycle
    try:
        path = events_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > EVENTS_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass  # no journal yet
        summary = "" if summary is None else str(summary)
        row = {"v": 1, "ts": now_ts(),
               "actor": actor or os.environ.get("HELM_ACTOR")
               or home.session_id() or "cli",
               "verb": str(verb), "target": str(target),
               "summary": cut_marked(summary, SUMMARY_WIDTH)}
        if len(summary) > SUMMARY_WIDTH:
            # THE WHOLE VALUE, BESIDE THE NARROW ONE. `summary` is a display
            # width and every reader renders it in a table row; the receipt
            # would be worthless as evidence if the width were also the
            # record. The key is ADDITIVE, so a row written before it exists
            # still reads, and a reader that never learns of it still sees
            # from the mark in `summary` that there is more.
            row["summary_full"] = cut_marked(summary, SUMMARY_MAX)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_events(limit=20):
    """The recent trail, oldest-first within the last `limit` rows; spans the
    rotated generation when the current file runs short. Fail-open: garbled
    lines are skipped, any file trouble reads as fewer rows, never a raise."""
    import json
    try:
        n = max(int(limit), 0)
    except (TypeError, ValueError):
        n = 20
    path = events_path()
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        pass
    if len(lines) < n:
        try:
            with open(path + ".1", encoding="utf-8") as f:
                lines = f.read().splitlines() + lines
        except Exception:
            pass
    out = []
    for ln in lines[-n:] if n else []:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out
