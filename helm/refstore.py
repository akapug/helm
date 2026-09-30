#!/usr/bin/env python3
"""refstore — THE content-addressed text store under the helm home.

A ledger row is bounded (`eventledger.MAX_EVENT_BYTES`, and tighter budgets
each ledger keeps on top of it); the text a row stands for is not. A row that
must carry more than it can afford stores the text WHOLE here and carries a
REFERENCE instead: the blake2b-128 digest of the text's UTF-8 bytes, plus the
byte length.

ONE STORE, SEVERAL TENANTS, and this module is where the store lives so that
no tenant writes a second one:
  - dispatch briefs (`dispatches.write_brief_file`), whose row keeps a bounded
    copy beside the reference;
  - findings-pass outputs (`findingspass`), through that same door;
  - task comment archives and oversized comment texts (`tasks`), which move
    old comments off a task row that would otherwise outgrow the event cap.

It was first written inside helm/dispatches.py for briefs and lifted here so
the task ledger could reuse it without importing the whole dispatch module
(measured: importing that module costs about six times what importing the
task module does, on every `helm task` call). Its laws came with it; the lift
added one — the write is now fsynced — and files are now created private
(0600), like the ledgers whose text they hold.

THE LAWS, stated once:

CONTENT-ADDRESSED, NOT ROW-ADDRESSED. The digest is known the instant the text
is, so the file can be written BEFORE any row names it; the write is
idempotent (the same text is the same file); and the reference is
SELF-PROVING — a reader recomputes the digest over the bytes it read and knows
whether the file is the text the row was written with, without trusting the
ledger.

FILE FIRST, ROW SECOND, ALWAYS. A kill between the two leaves an ORPHAN FILE,
which costs a few kilobytes and is invisible to every reader. The opposite
order leaves a DANGLING REFERENCE: a row promising text that does not exist.

DURABLE BEFORE THE ROW. Every ledger append is fsynced, so a text file that is
merely renamed into place could be lost by a crash that keeps the row naming
it — "file first" is an ORDER only if the file reaches the disk first. `write`
fsyncs the file and its directory entry before it returns.

THE DIGEST IS RECOMPUTED ON EVERY READ, NEVER TRUSTED, and every failure is
NAMED. `read` returns a (kind, detail) fault rather than a bare None, and each
tenant words that fault for its own reader: a dispatch reader has a bounded
copy to fall back to, a task reader of an archive has nothing, and one
sentence cannot say both truthfully.

A REFERENCE READ OFF A LEDGER ROW IS UNTRUSTED INPUT and is about to become a
path, so only exactly 32 lowercase hex characters — the spelling `digest`
produces — ever name a file. No value on any row, hand-edited or corrupt, can
escape the directory.
"""
import hashlib
import os
import re

from . import home, openflags, pk

# THE DIRECTORY KEEPS THE NAME IT WAS BORN WITH. It predates the other
# tenants, and renaming it would strand every reference already written.
# Declared in helm/registry.py `projections()`.
DIR = "dispatch-briefs"

REF = re.compile(r"[0-9a-f]{32}")

# The kinds of read fault, named once because tenants branch on them.
MALFORMED = "malformed"
MISSING = "missing"
MISMATCH = "digest-mismatch"
LENGTH = "length-mismatch"


def directory():
    return os.path.join(home.global_dir(), DIR)


def digest(text):
    """blake2b-128 over the text's UTF-8 bytes, as 32 hex characters."""
    return hashlib.blake2b(str(text).encode("utf-8"),
                           digest_size=16).hexdigest()


def path_of(ref):
    """The file one reference names, or None if the reference is not one.

    None rather than an exception on a malformed ref, because every caller is
    a READER of a possibly-corrupt row and has its own fallback to take."""
    ref = str(ref or "")
    if not REF.fullmatch(ref):
        return None
    return os.path.join(directory(), ref + ".txt")


def _fsync_path(path, directory_=False):
    base = openflags.verified_directory(os.O_RDONLY) if directory_ \
        else os.O_RDONLY
    fd = os.open(path, openflags.flags(base, "O_NOFOLLOW", cloexec=True))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(text):
    """(ref, nbytes, err) — store a text WHOLE, durably, before a row names it.

    `err` is the reason as a sentence fragment, or None. It is RETURNED, never
    raised: every caller is a write door, and the honest answer to "the text
    could not be stored" is a refusal naming why, not a traceback and not a
    row that quietly points nowhere."""
    text = "" if text is None else str(text)
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return None, 0, "the text is not encodable as UTF-8 (%s)" % exc
    ref = hashlib.blake2b(raw, digest_size=16).hexdigest()
    path = path_of(ref)
    try:
        pk.atomic_write(path, raw, mode=0o600)
        _fsync_path(path)
        _fsync_path(os.path.dirname(path), directory_=True)
    except OSError as exc:
        return None, len(raw), str(exc)
    return ref, len(raw), None


def read(ref, declared_bytes=None):
    """(text, fault) — the whole text a reference names, PROVEN.

    `fault` is None, or (kind, detail) with kind one of MALFORMED, MISSING,
    MISMATCH, LENGTH:
      MALFORMED  detail = the reference as found, cut to 64 characters
      MISSING    detail = why the file could not be read
      MISMATCH   detail = the digest the file's bytes actually have
      LENGTH     detail = the file's actual byte length

    newline="" — NO universal-newline translation. The digest was taken over
    the writer's bytes, and a text with an internal CR or CRLF hashed
    differently once the default reader folded it, so an INTACT file reported
    a mismatch. The reader returns the bytes the writer wrote."""
    path = path_of(ref)
    if not path:
        return None, (MALFORMED, str(ref)[:64])
    try:
        with pk.open_regular(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, (MISSING, str(exc))
    actual = digest(text)
    if actual != str(ref):
        return None, (MISMATCH, actual)
    size = len(text.encode("utf-8"))
    if isinstance(declared_bytes, int) and size != declared_bytes:
        # UNREACHABLE WHILE THE DIGEST HOLDS, and kept anyway: the row states
        # two independent facts about the same file, and a reader that checks
        # only one cannot report the other going wrong. It costs one len().
        return None, (LENGTH, size)
    return text, None
