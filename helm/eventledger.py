#!/usr/bin/env python3
"""Durable append-only JSONL event-ledger primitive.

Writers serialize on a stable sibling lock, repair only an incomplete tail,
append one bounded event with O_APPEND, fsync file + new directory entries, and
roll short writes back. Readers distinguish an absent ledger (known empty) from
an unsafe/unreadable ledger (obligations unknown), while domain callers choose
whether that becomes loud unavailable state or their legacy fail-open result.
"""
import contextlib
import io
import json
import os
import stat
import time

from . import fsops, openflags, projscope

MAX_EVENT_BYTES = 64 * 1024
CORRUPT_PREFIX = "corrupt ledger line "


def _flags(base):
    return openflags.flags(base, "O_NOFOLLOW", cloexec=True)


def _reader_flags():
    return openflags.flags(
        os.O_RDONLY, "O_NOFOLLOW", "O_NONBLOCK", cloexec=True)


def _fsync_dir(path):
    fd = os.open(path, openflags.flags(
        os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW", cloexec=True))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdirs(path):
    missing = []
    ancestor = path
    while not os.path.lexists(ancestor):
        missing.append(ancestor)
        older = os.path.dirname(ancestor)
        if older == ancestor:
            break
        ancestor = older
    if os.path.realpath(ancestor) != ancestor:
        raise OSError("ledger parent contains a symlink")
    for child in reversed(missing):
        parent = os.path.dirname(child)
        try:
            os.mkdir(child, 0o700)
        except FileExistsError:
            st = os.stat(child, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or os.path.realpath(child) != child:
                raise OSError("raced ledger parent is unsafe")
        _fsync_dir(parent)


def _prepare(path, create=False):
    p = os.path.abspath(path)
    parent = os.path.dirname(p)
    if create:
        _mkdirs(parent)
    if os.path.realpath(parent) != parent:
        raise OSError("ledger parent contains a symlink")
    st = os.stat(parent, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError("ledger parent is not a directory")
    try:
        mode = os.lstat(p).st_mode
        if stat.S_ISLNK(mode):
            raise OSError("ledger path is a symlink")
        if not stat.S_ISREG(mode):
            raise OSError("ledger path is not a regular file")
    except FileNotFoundError:
        pass
    return p


@contextlib.contextmanager
def locked(path, timeout=None):
    """Yield True under the stable sibling lock, False when setup fails.
    Mutations never proceed unlocked; local or ambient deadlines bound waits."""
    try:
        ambient = projscope.deadline()
        if ambient is not None:
            projscope.spend_or_raise("opening event ledger lock")
        p = _prepare(path, create=True)
        lock_path = p + ".lock"
        new_lock = not os.path.exists(lock_path)
        fd = os.open(lock_path, _flags(os.O_RDWR | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger lock is not a private regular file")
        os.fchmod(fd, 0o600)
        if new_lock:
            _fsync_dir(os.path.dirname(lock_path))
        import fcntl
        if timeout is None and ambient is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            local = (time.monotonic() + max(0, timeout)
                     if timeout is not None else None)
            end = min(x for x in (local, ambient) if x is not None)
            ambient_limited = ambient is not None and (
                local is None or ambient <= local)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    left = end - time.monotonic()
                    if left <= 0:
                        os.close(fd)
                        if ambient_limited:
                            raise projscope.Expired(
                                "waiting for event ledger lock")
                        yield False
                        return
                    time.sleep(min(0.01, left))
    except (OSError, ValueError):
        if "fd" in locals():
            os.close(fd)
        yield False
        return
    try:
        projscope.spend_or_raise("acquiring event ledger lock")
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


_CAP_REASON = "exceeds %d bytes" % MAX_EVENT_BYTES


def _exceeds_cap(line):
    """Is this line, WITH its terminator, over `MAX_EVENT_BYTES`? ONE OWNER.

    `line` excludes the b"\n" — that is the framer's unit and the grammar's —
    so the cap on the terminated line is `>=` on these bytes. The framer needs
    this answer BEFORE the blank exemption and `_row_verdict` needs it as part
    of what a valid row IS, and writing `>=` twice is how two bounds end up one
    byte apart: a line exactly at the cap would pass one door and be refused by
    the other, which is the same class of defect as two framings.
    """
    return len(line) >= MAX_EVENT_BYTES


def _row_verdict(raw):
    """(row, reason) for ONE complete line, WITHOUT ITS TERMINATOR. THE ONLY
    definition of a valid row.

    A shared row definition is necessary and NOT sufficient: two readers can
    agree perfectly about what a ROW is and still disagree about FRAMING.
    That is why `checked_rows` is a thin adapter onto `_rows_from_fd` and
    this is the grammar of ONE machine rather than a definition two machines
    promise to share.

    Framing belongs with the grammar, so it is stated here: a line ends at
    b"\n" and NOTHING ELSE. `splitlines` also breaks on a bare b"\r", which
    binary file iteration does not — so a row carrying a bare CR would
    silently become two, the first half dropped as an unterminated tail and
    the second parsing as a whole row, turning one COMPLETE corrupt row into
    a clean ledger. Under `strict` that is the difference between poisoning
    the read and folding the corruption away.

    `raw` EXCLUDES the terminator, and the cap does not: `MAX_EVENT_BYTES`
    bounds the LINE, newline included, which is what a reader counting bytes
    off a disk sees. So the bound here is `>=` on the terminator-less line,
    and it agrees to the byte with the framer's. A `>` would let a line one
    byte over the cap through THIS door while the framer refused it — two
    bounds one byte apart is the same class of defect as two framings.
    """
    if _exceeds_cap(raw):
        return None, _CAP_REASON
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "is not valid UTF-8 JSON"
    if not (isinstance(row, dict) and row.get("id")):
        return None, "is not an object with a non-empty id"
    return row, None


_READ_CHUNK = 65536


def _effective_deadline(explicit):
    ambient = projscope.deadline()
    if ambient is None or (explicit is not None and explicit <= ambient):
        return explicit
    return ambient


def _rows_from_fd(f, strict, skip_blank, deadline=None):
    """(rows, reason) read INCREMENTALLY from an open descriptor.

    A buffer that grows past `MAX_EVENT_BYTES` with no terminator in it is an
    over-long row detected WITHOUT waiting for its newline, which a
    whole-buffer reader cannot do: it has already read the whole line before
    it can measure it.

    ONLY A NEWLINE-TERMINATED LINE IS A ROW. A trailing fragment at EOF is
    outside the durability boundary and is dropped, byte-for-byte as the
    buffered path does it.

    THE DEADLINE REACHES INSIDE THE READ, and reading in chunks is what makes
    that possible: a single `f.read()` cannot be interrupted and neither can a
    whole-buffer split, so a budget placed around them bounds nothing. The
    checks sit at every boundary where work is about to start or a verdict is
    about to be returned, and NOT on a fixed row count — `MAX_EVENT_BYTES`
    caps one line, so checking every 64 rows permits 64 times that much work
    between checks.

    AN EXPIRED READ RETURNS NO ROWS AND A REASON, never a short list. A
    partial list is an authority answer built from a check that stopped early,
    and UNKNOWN outranks corruption at every one of these boundaries or at
    none of them.
    """
    deadline = _effective_deadline(deadline)
    # EVERY PER-ROW GLOBAL IS BOUND ONCE, HERE. A dense ledger is 116,508
    # minimal rows per MiB, and at that density a global lookup is not a
    # detail: each one is a dict probe repeated six figures of times inside
    # the loop below, and hoisting them is worth ~13% of this function on
    # its own, measured.
    verdict = _row_verdict
    exceeds_cap = _exceeds_cap
    # `deadline is not None` is answered ONCE. `_past` is correct and it is a
    # CALL, and an unbudgeted read pays it twice per row for an answer that
    # cannot change inside the loop; at 116,508 rows per MiB that is not a
    # detail.
    budgeted = deadline is not None
    now = time.monotonic
    max_bytes = MAX_EVENT_BYTES
    chunk_size = _READ_CHUNK
    out = []
    append = out.append
    buf = b""
    line = 0
    # DISCARD-UNTIL-NEWLINE, and it is not tidiness — it is the difference
    # between skipping an over-long line and FORGING a row out of its tail.
    # On an over-long line whose later chunk holds valid JSON before its
    # newline, clearing the buffer at MAX_EVENT_BYTES makes the SUFFIX parse
    # as a fresh row: `_rows_from_fd` returns id=forged AND id=good where
    # `checked_rows` returns only id=good. An authority reader that can
    # invent a row the canonical reader does not see is worse than any
    # overrun, and it defeats the shared-grammar claim `_row_verdict` is
    # extracted for. A line ends at its NEWLINE, never at the length a
    # reader gave up at.
    skipping = False
    while True:
        if budgeted and now() >= deadline:
            raise projscope.Expired(
                "ledger read deadline exceeded after %d rows" % line)
        chunk = f.read(chunk_size)
        if not chunk:
            # THE LAST READ IS A SPEND LIKE ANY OTHER, and it is the only one
            # whose expiry no later check can see: the loop head bounds when a
            # read STARTS, and this read ENDS the loop. Without it a read whose
            # final `f.read` crossed the deadline falls out to `return out,
            # None` and answers CLEAN — on an empty ledger that is `([],
            # None)`, indistinguishable from a ledger that really has no rows.
            if budgeted and now() >= deadline:
                raise projscope.Expired(
                    "ledger read deadline exceeded after %d rows" % line)
            break
        buf += chunk
        # ONE SPLIT PER CHUNK, IN C, NOT ONE SCAN PER ROW IN PYTHON. This is
        # the whole performance argument and it is measured, not reasoned: a
        # per-row `find`+slice cursor runs 1.25-1.30x the whole-buffer reader
        # on 116,508 minimal rows per MiB, and one `bytes.split` per 64KiB
        # read brings it to parity (0.99x) with IDENTICAL rows across 384
        # strict x skip_blank x chunk-size comparisons. The last element is
        # the UNCONSUMED REMAINDER by construction — `split` puts the bytes
        # after the final newline there, and they are not a line until a
        # later read supplies one.
        parts = buf.split(b"\n")
        buf = parts.pop()
        # AND THE SPLIT IS ALSO THE NO-COMPLETE-LINE TEST, so there is no
        # separate `b"\n" in buf` pre-scan. That pre-scan read every
        # newline-bearing chunk TWICE — once to answer a question `split`
        # answers on its way past, and once to split it. An empty `parts` is
        # exactly "no terminator arrived in this read".
        if skipping and not parts:
            buf = b""                  # still inside the over-long line
            continue
        for part in parts:
            line += 1
            if skipping:
                # THIS LINE'S TERMINATOR, NOT A ROW. It is consumed and
                # counted, and nothing is parsed out of it — but consuming it
                # IS a row boundary, and the budget can expire on it. The
                # deadline is consulted before the strict poison below, or a
                # read that merely RAN OUT OF TIME returns a confident
                # `corrupt` verdict: a skipped case answering as a decided one.
                skipping = False
                if budgeted and now() >= deadline:
                    raise projscope.Expired(
                        "ledger read deadline exceeded at line %d" % line)
                # AND ONLY NOW MAY STRICT POISON. Length alone does not make
                # a row: an over-long run that never terminates is an
                # UNTERMINATED TAIL, outside the durability boundary and
                # skipped at any length. Refusing as soon as the buffer
                # passes the cap poisons a read on a truncated final write
                # that a whole-buffer reader answers cleanly, and the
                # disagreement is visible only at small chunk sizes.
                if strict:
                    return [], CORRUPT_PREFIX + "%d %s" % (line, _CAP_REASON)
                continue
            if budgeted and now() >= deadline:
                raise projscope.Expired(
                    "ledger read deadline exceeded at line %d" % line)
            # THE GRAMMAR ORDER IS FIXED AND THIS IS WHERE IT IS ENFORCED:
            #   complete line -> SIZE BOUND -> blank exemption -> JSON.
            # `skip_blank` exempts BOUNDED SEPARATORS — the empty lines a
            # normal append produces — never an arbitrarily large malformed
            # write. Running the exemption first makes a 65KB run of spaces
            # "a blank line", which is how a torn or padded record gets
            # folded away silently; it also makes the answer depend on the
            # CHUNK SIZE, because an over-long blank arriving whole in one
            # read takes this path while one split across reads takes the
            # discard path above. Chunk size must never change authority.
            #
            # `part` CARRIES NO TERMINATOR, so the cap — which bounds the
            # LINE, terminator included — is `>=` here and in `_row_verdict`.
            if exceeds_cap(part):
                if strict:
                    return [], CORRUPT_PREFIX + "%d %s" % (line, _CAP_REASON)
                continue
            if skip_blank and not part.strip():
                continue
            row, reason = verdict(part)
            # AND AFTER THE PARSE, BEFORE ANY VERDICT. The pre-row check bounds
            # when the parse STARTS; the decode and the JSON load happen inside
            # `_row_verdict`, and a strict return one line below would report
            # CORRUPTION for a read that merely ran out of time. This is the
            # boundary a check placed only ahead of the work cannot see.
            if budgeted and now() >= deadline:
                raise projscope.Expired(
                    "ledger read deadline exceeded at line %d" % line)
            if reason is not None:
                if strict:
                    return [], CORRUPT_PREFIX + "%d %s" % (line, reason)
                continue
            append(row)
        # THE REMAINDER CAN ITSELF BE OVER-LONG. Checked after the split for
        # the same reason as before it: nothing that cannot become a row is
        # allowed to accumulate, and its eventual terminator must be
        # recognised as the END of a skipped line rather than the start of a
        # fresh one.
        if len(buf) > max_bytes:
            skipping = True
            buf = b""
    return out, None


def checked_rows(data, strict=False, skip_blank=False, deadline=None):
    """(complete dict events, corruption reason) for ONE READ'S BYTES.

    THE PARSE, SPLIT OUT FROM THE OPEN, and the split is the point rather than
    tidiness. A caller that must be able to say WHICH FILE DESCRIPTION answered
    it — the land projection's read-set, which puts the path, the inode and the
    content identity into one witness term — cannot hand a PATH to a reader that
    opens it again: the resolver can move between the two, and then the record
    names one file while the body consumed another. Such a caller opens once,
    fstats THAT handle, reads THOSE bytes, and parses them here.

    `checked_events` is this function with the open in front of it, so the two
    can never disagree about what a complete row is.

    ONE FRAMING STATE MACHINE, NOT TWO THAT AGREE. A second line splitter
    here would share `_row_verdict`'s definition of a ROW and still be free
    to disagree about FRAMING — which is how an over-long line's tail becomes
    a forged row in one reader and not the other, and how an over-long run of
    whitespace is a blank line at one chunk size and corruption at another.
    Sharing a grammar does not make two machines one machine, so there is
    only one.

    So there is now one machine and this is a thin adapter onto it: the
    bytes become a descriptor and the incremental reader frames them. Parity
    holds BY CONSTRUCTION rather than by an equivalence matrix, which demotes
    that matrix from safety net to regression guard — the right job for it.

    What the machine guarantees, in this order: a row is a NEWLINE-TERMINATED
    line (a trailing fragment at EOF is outside the durability boundary and
    is dropped); then the SIZE BOUND; then the optional blank exemption; then
    JSON. A read that cannot complete returns NO ROWS and a reason — never a
    short list, which is an authority answer built from a check that stopped
    early.
    """
    deadline = _effective_deadline(deadline)
    return _rows_from_fd(io.BytesIO(data), strict, skip_blank,
                         deadline)


def _past(deadline):
    """True when `deadline` has passed. A None deadline never expires."""
    return deadline is not None and time.monotonic() >= deadline


def ledger_identity(path):
    """(dev, ino, size, mtime_ns, ctime_ns, mode, nlink) of the ledger, or
    None.

    THE CHEAP FRESHNESS PROBE: one open and one fstat, no read and no parse.
    Its whole reason to exist is that a caller memoising a validated read must
    be able to ask "is this still the file that was read?" WITHOUT paying for
    read again — a probe that parses to answer that question is not a cache.

    It applies the SAME O_NOFOLLOW and private-regular-file discipline as the
    validated read, because a key taken through a weaker check than the read
    it short-circuits is a fail-open dressed as an optimisation.
    """
    fd = None
    try:
        p = _prepare(path)
        fd = os.open(p, _reader_flags())
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                st.st_ctime_ns, st.st_mode, st.st_nlink)
    except Exception:                          # noqa: BLE001
        return None
    finally:
        if fd is not None:
            os.close(fd)


def checked_events_with_identity(path, strict=False, skip_blank=False,
                                 deadline=None):
    """(rows, unavailable, identity) — all three from ONE descriptor.

    A CALLER THAT MEMOISES AUTHORITY NEEDS AN IDENTITY THAT PROVABLY DESCRIBES
    THE BYTES IT PARSED, and it cannot build one by stat-ing around a separate
    read. `checked_events` plus two stats is THREE descriptors and therefore
    three chances to be looking at a different file: a path replaced A -> B ->
    A between them validates B's rows and files them under A's identity, and
    no later append dislodges that entry because the key is legitimately A's.
    That shape is measured, and this is the door that removes it.

    `identity` is (dev, ino, size, mtime_ns, ctime_ns, mode, nlink) of the
    descriptor the rows were read from.

    CTIME IS IN THE KEY BECAUSE MTIME CAN BE RESTORED AND CTIME CANNOT. A
    rewrite that puts back the same number of bytes and then calls `utime` to
    restore the old mtime produces an IDENTICAL (dev, ino, size, mtime, mode,
    nlink) for DIFFERENT CONTENT — and a memo keyed on that serves the old
    rows forever. `st_ctime` is the inode-change time: the kernel sets it on
    every write and no userspace call restores it, so it is the field that
    makes a same-size same-mtime rewrite visible. The private-regular-file check and O_NOFOLLOW are
    the SAME ones `checked_events` applies, because a cache key must never be
    a weaker check than the read it short-circuits.

    `identity is None` HAS TWO MEANINGS AND A CALLER MUST TREAT THEM ALIKE:
      * NOTHING TO DESCRIBE — the ledger is absent, so there is no file to
        take a key from.
      * THE KEY CANNOT BE TRUSTED — the descriptor's fstat MOVED between the
        pre-read snapshot and EOF, so the file changed under the read. The
        rows are still an honest read; it is the identity that would be a
        lie, and it is withheld rather than published.
    Both say the same operational thing — DO NOT CACHE THIS PASS — which is
    why one value serves for both, and a caller that distinguishes them is
    almost certainly about to cache one of them. What a caller must never do
    is read None as "the ledger is empty": that is the absent case only, and
    `rows` already answers it.

    ONE EXIT DOOR, AND EVERY RETURN PASSES THROUGH IT. The deadline checks
    inside the read are early exits that save work; they are not the
    guarantee, because they can only fire where someone thought to put one.
    The paths that skip them are exactly the paths that produce a CONFIDENT
    ANSWER out of a failure: an absent ledger returns `([], None, None)`,
    which says "read successfully, no events", and an unreadable one returns
    a reason. Reached after the budget is gone, both are answers to a
    question this call was no longer entitled to answer, and a caller cannot
    tell them from the same answer produced in time.

    So the work happens in `_read_with_identity` and this function is the
    door: whatever the read produced -- rows, absence, an unreadable reason,
    an unexpected exception folded to a string -- expiry is checked ONCE,
    LAST, and outranks all of them. A timeout is not a fact about the
    ledger, so it can never be reported as one.
    """
    deadline = _effective_deadline(deadline)
    try:
        result = _read_with_identity(path, strict, skip_blank, deadline)
    except projscope.Expired:
        # A TIMEOUT IS NOT AN UNREADABLE LEDGER, AND THE BROAD HANDLER BELOW
        # WOULD MAKE IT ONE. `Expired` is an ordinary exception, so
        # `except Exception: return None, str(exc), None` catches the raise
        # the read's own deadline checks perform and converts it back into a
        # STRING -- which is exactly the shape the type replaced, only now
        # invisible, because the reader looks like it raises and does not.
        # A fail-open written for I/O errors silently swallows a control
        # signal that was added later.
        raise
    except FileNotFoundError:
        result = [], None, None
    except Exception as exc:                   # noqa: BLE001
        result = None, str(exc), None
    if _past(deadline):
        # THE CHECK THAT DOES NOT DEPEND ON ANYONE REMEMBERING IT. Above,
        # `result` is already a complete, plausible answer -- and if the
        # budget ran out while it was being produced, it is an answer this
        # call had no standing to give. Discarding it here is the whole
        # point of a single door: a new return added to the read below
        # inherits this precedence without its author having to know.
        raise projscope.Expired("ledger read deadline exceeded")
    return result


def _read_with_identity(path, strict, skip_blank, deadline):
    """The read itself. Its ONLY exit contract is the triple or an exception.

    Every failure translation lives at the door in
    `checked_events_with_identity`, so nothing here converts an exception
    into a confident answer, and no return here is final.
    """
    fd = None
    try:
        if _past(deadline):
            raise projscope.Expired("ledger read deadline exceeded before open")
        p = _prepare(path)
        fd = os.open(p, _reader_flags())
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger is not a private regular file")
        identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                    st.st_ctime_ns, st.st_mode, st.st_nlink)
        if _past(deadline):
            raise projscope.Expired("ledger read deadline exceeded before read")
        with os.fdopen(fd, "rb") as f:
            fd = None
            rows, reason = _rows_from_fd(f, strict, skip_blank, deadline)
            # THE IDENTITY IS TAKEN BEFORE THE READ AND MUST BE CONFIRMED
            # AFTER IT. One descriptor removes the file-swap race and does
            # NOT remove this one: an append that lands between the fstat and
            # the read gives rows that INCLUDE the new event under an identity
            # whose size and mtime say it is not there yet. Measured
            # exactly that. A caller memoising on this key then holds rows
            # from a moment the key does not describe, and every later reader
            # whose fstat matches that key is served them — a cached answer
            # from the future, which no append dislodges because the key is
            # legitimately the older file's.
            #
            # So the SAME descriptor is fstat'd again after EOF. If anything
            # moved, the rows are still an honest read of the file and the
            # IDENTITY is the part that cannot be trusted, so identity is
            # withheld: the caller gets its answer and simply cannot cache it
            # this pass. Withholding a cache key costs one re-read; publishing
            # a wrong one is permanent.
            try:
                after = os.fstat(f.fileno())
            except OSError:
                after = None
        if reason is not None:
            return None, reason, None
        if _past(deadline):
            raise projscope.Expired("ledger read deadline exceeded during parse")
        moved = after is None or (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode, after.st_nlink) != identity
        return rows, None, (None if moved else identity)
    finally:
        if fd is not None:
            os.close(fd)


def checked_events(path, strict=False, skip_blank=False):
    """Return (LIST of complete event dicts, unavailable reason).

    THE LIST IS THE CONTRACT, and describing it as anything id-keyed costs a
    caller a crash. Read as a map, the result is TOTAL on an empty ledger
    (``[]`` is falsy, so a fallback dict takes over) and raises
    AttributeError on the first real event — a defect that appears only once
    the feature starts working.
    Every return here is a list; there is no id-keyed shape to index.

    A missing file or parent is a known empty ledger. Unsafe paths, permission
    failures, and other I/O errors are UNKNOWN, not zero obligations. The default
    reader preserves the historical projection contract and skips malformed rows.
    ``strict=True`` instead poisons the whole read on any malformed COMPLETE row;
    an unterminated final tail remains outside the durability boundary and is
    ignored in both modes. ``skip_blank`` preserves ledgers whose historical
    grammar explicitly admitted blank separator lines; it is opt-in so blank
    rows remain corruption everywhere else.
    """
    deadline = _effective_deadline(None)
    fd = None
    try:
        if _past(deadline):
            raise projscope.Expired(
                "ledger read deadline exceeded before open")
        p = _prepare(path)
        fd = os.open(p, _reader_flags())
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger is not a private regular file")
        with os.fdopen(fd, "rb") as f:
            fd = None
            result = _rows_from_fd(f, strict, skip_blank, deadline)
    except projscope.Expired:
        raise
    except FileNotFoundError:
        result = [], None
    except OSError as exc:
        result = [], "%s: %s" % (type(exc).__name__, exc)
    finally:
        if fd is not None:
            os.close(fd)
    if _past(deadline):
        raise projscope.Expired("ledger read deadline exceeded")
    return result


def read_bytes(path):
    """(the ledger's BYTES, unavailable) through ONE descriptor, unparsed.

    THE CHECKPOINTED FOLD NEEDS THE BYTES, NOT ONLY THE ROWS. It proves that a
    stored prefix is still the file's prefix by hashing exactly the bytes it
    then parses, and it parses only the tail past that prefix with
    `checked_rows` — the one framing machine, so the tail's rows are the rows
    `checked_events` produces for the same bytes. A path read twice (once to
    hash, once to parse) is two descriptors and a window for the file to change
    between them; this is one.

    THE SAME OPEN DISCIPLINE AS `checked_events`: O_NOFOLLOW, a private regular
    file with one link, and a missing file is a KNOWN-EMPTY ledger (b"").
    Every other failure is UNKNOWN and returns (None, reason), in the SAME
    words `checked_events` uses for the same failure, so a reader that moved
    from that door to this one reports the same sentence.
    """
    deadline = _effective_deadline(None)
    fd = None
    try:
        if _past(deadline):
            raise projscope.Expired(
                "ledger read deadline exceeded before open")
        p = _prepare(path)
        fd = os.open(p, _reader_flags())
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger is not a private regular file")
        with os.fdopen(fd, "rb") as f:
            fd = None
            result = f.read(), None
    except projscope.Expired:
        raise
    except FileNotFoundError:
        result = b"", None
    except OSError as exc:
        result = None, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if fd is not None:
            os.close(fd)
    if _past(deadline):
        raise projscope.Expired("ledger read deadline exceeded")
    return result


def events(path):
    return checked_events(path)[0]


def latest_checked(path, accept=None, strict=False):
    # `strict` rides through to checked_events unchanged: a projection caller
    # keeps the tolerant historical read, a DECISION caller (one that will
    # file, refuse, or classify on the answer) opts into poisoning the whole
    # read on a corrupt complete row rather than folding it as known-empty.
    events_, unavailable = checked_events(path, strict=strict)
    if unavailable:
        return {}, unavailable
    out = {}
    for row in events_:
        rid = str(row.get("id"))
        prior = out.get(rid)
        if accept is None or accept(row, prior):
            out[rid] = row
    return out, None


def latest(path, accept=None):
    return latest_checked(path, accept)[0]


def _trim_torn_tail(fd, size):
    """Return the last complete-line boundary under the ledger lock."""
    if not size or fsops.pread(fd, 1, size - 1) == b"\n":
        return size
    end = size
    while end:
        start = max(0, end - 8192)
        chunk = fsops.pread(fd, end - start, start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def append_unlocked(path, row):
    """Append one event while the caller holds ``locked(path)``.

    Repairs a pre-existing torn tail, writes once, fsyncs, and rolls a partial
    write back. A newly-created ledger is not acknowledged until its containing
    directory entry has also been fsynced.
    """
    fd, before = None, None
    try:
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            return False
        p = _prepare(path, create=True)
        new_file = not os.path.exists(p)
        fd = os.open(p, _flags(os.O_RDWR | os.O_APPEND | os.O_CREAT), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("ledger is not a private regular file")
        os.fchmod(fd, 0o600)
        before = _trim_torn_tail(fd, st.st_size)
        if before != st.st_size:
            os.ftruncate(fd, before)
            os.fsync(fd)
        if os.write(fd, payload) != len(payload):
            os.ftruncate(fd, before)
            os.fsync(fd)
            return False
        try:
            os.fsync(fd)
            if new_file:
                _fsync_dir(os.path.dirname(p))
        except OSError:
            os.ftruncate(fd, before)
            os.fsync(fd)
            raise
        return True
    except (OSError, TypeError, ValueError):
        if fd is not None and before is not None:
            try:
                os.ftruncate(fd, before)
            except OSError:
                pass
        return False
    finally:
        if fd is not None:
            os.close(fd)


def append(path, row):
    with locked(path) as held:
        return append_unlocked(path, row) if held else False
