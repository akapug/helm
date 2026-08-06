#!/usr/bin/env python3
"""helm toolwhisper — the per-TOOLCALL steer: a nudge at the moment of the act,
with the act's own context.

WHY THIS LEVEL AND NOT THE TURN. helm already injects premises every turn, and
that was not enough. Owner, 2026-07-26: "injections at the turn level are
helpful, but injections on toolcalls with action and active context was the
actual goal of the whisper system since the prior harness. there is tons of planning for
it to operate at all levels: session start, post compact, each turn, each
toolcall within turns."

The failures that motivated it all happened MID-TURN, at one specific tool
call, after the turn's premises had already been delivered and read:
  * five durable lessons Written into a private memory dir while `/learn` — the
    loaded, correct skill — says "This routes to HELM's store, never to Mission
    Control". The rule arrived. The write happened anyway.
Injecting text at turn start is a briefing. This is a tap on the shoulder while
your hand is on the wrong drawer.

LAWS, all of them borrowed from the stop-whisper because they are what keep a
nudge from becoming noise:
  * RULES ARE EARNED ONE AT A TIME. A classifier framework nobody has watched
    fire is the thing this repo keeps building and regretting; ship one rule
    proven end to end, then earn the next. Two are earned today:
      1. memory-dir writes (the original — a durable lesson written where no
         other seat can read it);
      2. helmese register edits (#216) — the register is VERSIONED and DIGESTED
         because a decoder meeting an unknown version REFUSES rather than
         guessing with a newer table (draft-2 amendment 3). An entry changed
         without a VERSION bump ships a register that READS as the old version
         and is not, and nothing at read time can tell. The bump instruction
         lives in a comment above VERSION, which is exactly the place nobody
         looks while editing the table below it.
  * LATCHED per (rule, session): a repeat write to the same class is silent. An
    agent mid-refactor in one directory must never be nagged per file.
  * FAIL-OPEN, ALWAYS: any trouble returns None and the tool boundary passes
    untouched. A steer that can hold a turn is worse than no steer.
  * POSITIVE AND ACTIONABLE: name the RIGHT move with its exact command, never
    just the wrong one. A negation is re-readable as permission
    (failure-modes-as-active-instructions).
"""
import os
import re

from . import pk, record

# A durable-looking write into a per-agent PRIVATE memory directory — the
# "memoryhole" the owner named: files that do not resurface reliably and that
# no other seat can ever read. Matched on the DIRECTORY, which is exactly the
# field record.py used to discard before edit-paths.log existed.
_MEMORYHOLE_RE = re.compile(r"(^|/)\.claude(-homes)?/.*/memory(/|$)")

# The shipped L2 seed register. Matched on the module path, not on content:
# toolwhisper sees only the destination of the edit that landed.
_REGISTER_RE = re.compile(r"(^|/)helm/helmese\.py$")

RULES = (
    {
        "id": "memoryhole-write",
        "match": lambda p: bool(_MEMORYHOLE_RE.search(p)),
        "say": ("that path is a PRIVATE memory file — it does not reliably "
                "resurface and no other seat can read it. A durable lesson "
                "belongs in helm's store, which injects fleet-wide every turn: "
                "load the `learn` skill, choose one type, then `helm store add "
                "premise '<id> | <statement> | <keywords>'` (or replace "
                "`premise` with `heuristic`) and prove it with `helm store "
                "resolve \"<a sentence someone would type>\"`. "
                "Keep the memory file only if it is genuinely private to this "
                "session."),
    },
    {
        "id": "helmese-register-edit",
        "match": lambda p: bool(_REGISTER_RE.search(p)),
        "say": ("that is the helmese SEED REGISTER. If you changed what a "
                "seeded entry MEANS, added one, or retired one, bump "
                "`helmese.VERSION` in the same commit: every rendered slice "
                "carries the version and digest it was written against, and a "
                "decoder that meets an unknown version REFUSES to interpret "
                "rather than guessing with a newer table. A changed entry "
                "under a stale version ships a register that reads as the old "
                "one and is not. Check the move with `python3 -c \"from helm "
                "import helmese; print(helmese.stamp())\"` \u2014 the digest "
                "moves even when the version does not."),
    },
)


def _latch_path(session):
    return os.path.join(record.session_dir(session), "toolwhisper-latch.json")


def classify(path):
    """The first rule matching this destination, or None. Pure — no I/O, no
    latch — so it is trivially testable and can never be the thing that fails."""
    for rule in RULES:
        try:
            if rule["match"](path):
                return rule
        except Exception:
            continue
    return None


def whisper_for(session, path):
    """(text, rule_id) for a write worth steering, else (None, None).

    LATCHES on success: the same rule stays silent for the rest of the session,
    so a refactor across twenty files in one directory costs ONE line. Every
    failure path returns (None, None) — an unreadable latch must never turn a
    nudge into an exception at a tool boundary."""
    try:
        if not session or not path:
            return None, None
        rule = classify(path)
        if not rule:
            return None, None
        lp = _latch_path(session)
        latch = pk.read_json(lp, {}) or {}
        if latch.get(rule["id"]):
            return None, None
        latch[rule["id"]] = pk.now_ts()
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        pk.write_json(lp, latch)
        return "[helm toolwhisper] %s" % rule["say"], rule["id"]
    except Exception:
        return None, None


def last_edit_path(session):
    """The destination of the most recent landed edit, or None.

    Reads edit-paths.log, which `helm record` writes on the SAME PostToolUse
    boundary and EARLIER in the hook order (record is listed before deliver, so
    the line is on disk before this runs). Failed edits are never appended
    there, so a write that landed nowhere can never trigger a steer."""
    try:
        fp = os.path.join(record.session_dir(session), "edit-paths.log")
        with open(fp, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
        return lines[-1] if lines else None
    except Exception:
        return None


def for_hook(session):
    """The whisper line for THIS tool boundary, or None. The one entry point a
    hook calls; every leg fail-open."""
    return whisper_for(session, last_edit_path(session))[0]
