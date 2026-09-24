#!/usr/bin/env python3
"""HOW OFTEN A WORD REACHES A TURN — the prompt side of commonness (task/2978).

The store asks "is this word common?" in two places: the mint gate, before it
auto-adds a 1-2 word stem of an author's phrase, and the resolver, before a
lone single-word match may fire an entry. The mint gate measured it on the
STATEMENT corpus (write.corpus_profile), but the probes fire on PROMPTS: chat
wakes, task notices, subagent hand-backs, typed turns. The two languages
differ where it costs. MEASURED on the E2 audit (2,015 turns with substance,
live store): `summary` sat in 1 verified statement and in 16.5% of turns,
`ok` in 1 and 16.7%, `background` in 1 and 14.0%, so the statement gate called
them rare and minted them, and each one then fired its entry on every turn
that said it. This module is the prompt-side measurement.

THE UNIT IS A TURN. A word a turn repeats ten times is in one turn. A fraction
this reports is "the share of recent turns this probe would have matched",
which is the question both callers ask.

THE WORD FORMS ARE THE RESOLVER'S OWN. `forms(text)` returns every single-word
probe that would match `text` under store.resolve._probe_re: the text's runs
of [a-z0-9] (the probe boundary), its hyphen and apostrophe compounds (a
probe like `stop-guard` matches whole), and the base of every inflected form
the resolver tolerates (`events` -> `event`, `processed` -> `process`; a base
shorter than resolve._INFLECT_MIN is exact-only there, so it is not added
here). A census on a different tokenizer would measure a second law.

WHAT IS RECORDED, AND WHERE. inject records each admitted turn's SUBSTANCE
(promptshape.substance: the notice envelope already set aside) after the
turn's ledger row lands. A turn with no substance is not a turn here: nothing
matched on it. Only word forms and counts are kept, never text. The file is
`<global>/.state/prompt-census.json`, beside coinages.json, and it is bounded
twice: WINDOW halves every count when the turn count reaches it (so the census
describes roughly the last WINDOW turns and forgets older ones), and CAP sheds
the rarest forms when the table grows past it (a shed form reads as rare,
which errs toward admitting a stem, never toward refusing one).

CONCURRENCY. Every seat's hook writes the same file. The write is an atomic
replace, so a reader never sees a torn table, but two turns racing can lose
one turn's increments. That is accepted: the census is a frequency estimate
over thousands of turns, and a lock on the per-turn hot path would cost every
seat to protect a count nobody needs exactly.

UNMEASURED IS A STATE. Below MIN_TURNS the census answers `measured = False`
and callers do nothing with it: the mint gate refuses no stem as
prompt-common and the resolver holds no lone word. A fresh deployment, or
the hours after this lands, behaves exactly as before until the census has
seen enough turns to mean something.

Stdlib only, fail-open everywhere: a census that cannot be read reads empty,
and a record that cannot be written is skipped.
"""
import os
import re

from . import home, pk

# The turn count at which every count halves. Measured fleet traffic in the E2
# window: ~100 admitted turns an hour, so the census spans about 20-40 hours.
WINDOW = 4000
# Distinct forms kept. A form at the resolver's 4% bar has ~100+ turns of a
# 4000-turn window; the forms shed at this size have single-digit counts.
CAP = 6000
# Turns before the census counts as measured. At 400 turns a word at the 4%
# bar has an expected count of 16, enough that a fraction near the bar is not
# one unlucky afternoon.
MIN_TURNS = 400

_RUN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_PART = re.compile(r"[a-z0-9]+")
# store.resolve._INFLECT, longest suffix first. Kept in step by
# tests/test_promptcensus.py, which checks forms() against _probe_re itself.
_SUFFIXES = ("ing", "es", "ed", "s")
_INFLECT_MIN = 4


def path():
    return os.path.join(home.global_dir(), ".state", "prompt-census.json")


def forms(text):
    """Every single-word probe that would match `text` (see the module note)."""
    low = (text or "").lower()
    out = set()
    for m in _RUN.finditer(low):
        token = m.group(0)
        out.add(token)
        for part in _PART.findall(token):
            out.add(part)
            if not part.isalpha():
                continue
            for suffix in _SUFFIXES:
                if part.endswith(suffix) and \
                        len(part) - len(suffix) >= _INFLECT_MIN:
                    out.add(part[:-len(suffix)])
    return out


class Census(object):
    """One read of the census: `turns` and `df` (form -> turns it reached)."""

    __slots__ = ("turns", "df")

    def __init__(self, turns=0, df=None):
        self.turns = turns
        self.df = df or {}

    @property
    def measured(self):
        return self.turns >= MIN_TURNS

    def frac(self, word):
        """The share of recorded turns `word` reached (0.0 when unrecorded)."""
        if not self.turns:
            return 0.0
        return self.df.get(str(word or "").lower(), 0) / float(self.turns)

    def common(self, bar):
        """The forms reaching at least `bar` of turns; empty when unmeasured."""
        if not self.measured:
            return frozenset()
        cut = bar * self.turns
        return frozenset(w for w, c in self.df.items() if c >= cut)


def _read():
    """(turns, df) from the file, or (0, {}) for anything not the census's
    own shape: missing, torn, or written by something else."""
    d = pk.read_json(path())
    if not isinstance(d, dict):
        return 0, {}
    turns, df = d.get("turns"), d.get("df")
    if type(turns) is not int or turns < 0 or not isinstance(df, dict):
        return 0, {}
    if any(type(c) is not int for c in df.values()):
        return 0, {}
    return turns, df


def load():
    """The census as it stands. Never raises."""
    try:
        turns, df = _read()
    except Exception:  # noqa: BLE001 — a census is never worth a turn
        return Census()
    return Census(turns, df)


def record(text):
    """Count one turn's word forms. True when a turn was recorded; an empty
    text records nothing. Never raises."""
    fs = forms(text)
    if not fs:
        return False
    try:
        turns, df = _read()
        turns += 1
        for f in fs:
            df[f] = df.get(f, 0) + 1
        if turns >= WINDOW:
            turns //= 2
            df = {w: c // 2 for w, c in df.items() if c // 2}
        if len(df) > CAP:
            keep = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))
            df = dict(keep[:CAP * 3 // 4])
        pk.write_json(path(), {"v": 1, "turns": turns, "df": df})
    except Exception:  # noqa: BLE001 — fail open, the turn goes on
        return False
    return True
