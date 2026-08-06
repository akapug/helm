#!/usr/bin/env python3
"""The claim-evidence rung — a Stop-hook WARNER for outgoing claims the turn
did not earn.

Owner canon 2026-07-31 (the owner doing this by hand, asking for it
organized): a claim that SOUNDS settled feels finished, and summarising
someone else's summary feels like work rather than like guessing. The four
claim shapes, all measured on opus-integrator the same day:

  1. a bare COUNT or PERCENTAGE with no command behind it this turn
     ("42% land rate", "132.6h dwell" — both were real numbers bound to the
     wrong claim),
  2. a "verified"/"confirmed"/"proven" with no tool call this turn,
  3. a named SHA never resolved this turn (sha-guard covers fabrication;
     this covers ASSERTION-without-resolution),
  4. a "landed" that means LOCAL main rather than origin/main.

THE EVIDENCE IS THE TURN, not the library. A claim is earned when the turn
that makes it also ran the measurement: a tool_use between the previous
assistant message and this one. The transcript tail carries the records;
the rung reads them, never a model of them.

FOUR LAWS (the build brief's, verbatim): WARN never block; SILENT when the
message earns its claims; an unreadable turn is SKIPPED, never reported
clean (a blind read must not masquerade as a healthy message); no empty
refusal — the line names WHICH claim and WHY it is unearned.

Stdlib + the package's own punt transcript reader only.
"""
import hashlib
import json
import os
import re

# --- the claim shapes -------------------------------------------------------

_COUNT_CLAIM = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent|rows?|tests?|lanes?|seats?|commits?|"
    r"hours?|h dwell|dwell|rate)(?![A-Za-z])", re.I)
_PROOF_WORD = re.compile(
    r"\b(?:verified|confirmed|proven|measured|checked|green|all tests pass)\b",
    re.I)
_LANDED_WORD = re.compile(r"\b(?:landed|on main|merged|shipped)\b", re.I)

# A claim is earned by an EXACT ANCHOR to a successful tool, not by a tool
# NAME (meld R1): a claimed number must appear as an exact TOKEN in returned
# content — a command echo is not a measurement ('142 rows' never earned
# '42%'). A SHA may anchor in a resolution tool's input or content because
# successful predicates such as `git cat-file -e` return no content. A
# 'landed' claim needs a successful git command resolving the CANONICAL trunk
# (origin/main): a local branch named main is not evidence of a land. A claim
# about a measurement made with only chat posts — or an unrelated successful
# tool — is a summary of someone else's summary: the exact defect.
_SHA_TOOLS = {"Bash", "Shell", "Grep", "Read", "Glob"}
_LANDED_TOOLS = {"Bash", "Shell"}


# --- R2: the transcript's two envelopes --------------------------------------
# The wrapped envelope is {message:{role,content}, uuid, ...}; the flat one
# is {role, content} with no message key (older/other harnesses). Typed records
# with neither field are transcript METADATA (attachments, titles, mode, queue
# operations) and do not claim to be messages. A message-shaped record that
# fits NEITHER envelope is UNSUPPORTED: seeing one and reporting clean is a
# vacuous pass, so the turn reads as unreadable — never clean.

def _unwrap(d):
    """(msg, uuid) for a supported record, else None."""
    uuid = d.get("uuid") if isinstance(d.get("uuid"), str) else ""
    msg = d.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("role"), str):
        return msg, uuid
    if "message" not in d and isinstance(d.get("role"), str):
        return d, uuid
    return None


def _parse_records(raw):
    """The transcript's records as (role, kind, payload, uuid, end), in order.

    kind is 'text' | 'tool_use' | 'tool_result' for assistant records, and
    'prompt' | 'tool_result' for user records. The role/kind split is the
    meld's R2 foundation: a USER-ROLE record that carries a tool_result is
    NOT a turn boundary — only a genuine user PROMPT is. Both record
    envelopes (wrapped + flat) parse; `unsupported` counts observed records
    that fit neither envelope. `end` is the record's byte offset in the raw
    tail (the no-UUID fallback's stable identity, meld R3)."""
    records = []
    unsupported = 0
    off = 0
    for line in raw.splitlines(keepends=True):
        end = off + len(line.encode("utf-8", "replace"))
        off = end
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain"):
            continue
        uw = _unwrap(d)
        if uw is None:
            # Current Claude transcripts interleave role-bearing messages with
            # typed metadata records (attachment, title, mode, queue state,
            # file-history deltas). They make no message-envelope claim and
            # must not poison the whole tail. A record carrying `message` or
            # `role` DID claim that shape and stays fail-closed when malformed.
            if "message" not in d and "role" not in d \
                    and isinstance(d.get("type"), str):
                continue
            unsupported += 1
            continue
        msg, uuid = uw
        role = msg.get("role")
        content = msg.get("content")
        parts = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str)
            else [])
        if role == "user":
            # a user record is a TOOL_RESULT if it has tool_result content or
            # the harness's top-level toolUseResult field; a MIXED record
            # (tool_result + genuine text) is both — the text part is still a
            # turn boundary (meld R2 calibration)
            is_tr = "toolUseResult" in d or any(
                isinstance(i, dict) and i.get("type") == "tool_result"
                for i in parts)
            if not parts and "toolUseResult" not in d:
                # a user record with NO content parts at all (null, [], or a
                # shape the reader does not model) is content it cannot
                # account for — an unsupported read, never a clean prompt
                unsupported += 1
                continue
            if is_tr:
                for item in parts:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        records.append(("user", "tool_result", item, uuid, end))
                texts = [str(i.get("text")) for i in parts
                         if isinstance(i, dict) and i.get("type") == "text"
                         and str(i.get("text") or "").strip()]
                if texts:
                    records.append(("user", "prompt", "\n".join(texts),
                                    uuid, end))
            else:
                records.append(("user", "prompt", content, uuid, end))
        elif role == "assistant":
            for item in parts:
                if not isinstance(item, dict):
                    continue
                t = item.get("type")
                if t == "text" and str(item.get("text") or "").strip():
                    records.append(
                        ("assistant", "text", str(item["text"]), uuid, end))
                elif t == "tool_use":
                    records.append(("assistant", "tool_use", item, uuid, end))
        else:
            # a recognized record of a role the rung does not model (system,
            # tool, ...) — the transcript carries content the reader cannot
            # account for, which is an unsupported read, not a clean one
            unsupported += 1
    return records, unsupported


def _result_text(content):
    if isinstance(content, list):
        return " ".join(str(c.get("text") or "")
                        for c in content if isinstance(c, dict)).strip()
    return str(content or "").strip()


def _turn(path, tail_bytes=256 * 1024):
    """The outgoing turn as a dict: {text, tools, uuid, end, readable}.

    tools is a list of {name, input, succeeded, content} for every tool_use
    in the turn, joined to its tool_result (never DEFAULTED to success — a
    missing tool_result reads as NOT succeeded, meld R1's fail-closed law).

    The turn spans from the previous genuine USER PROMPT (ignoring user-role
    tool_result records, meld R2) to the final assistant text — so assistant
    commentary WITHIN a turn (tool -> commentary -> final) does not truncate
    the tool set. uuid + end identify the final assistant record (meld R3:
    uuid when the envelope carries one, else the record's byte offset in the
    tail — two byte-identical records are still two distinct messages).
    readable=False means SKIPPED, never clean — and an observed record in an
    UNSUPPORTED envelope makes the read unsupported too, never a vacuous
    clean bill (meld R2)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return {"text": "", "tools": [], "uuid": "", "end": 0,
                "readable": False}
    records, unsupported = _parse_records(raw)
    if unsupported:
        return {"text": "", "tools": [], "uuid": "", "end": 0,
                "readable": False}
    if not records:
        # an empty transcript has no outgoing message to judge — nothing to
        # warn about, but nothing EARNED either: not a clean bill (meld R2)
        return {"text": "", "tools": [], "uuid": "", "end": 0,
                "readable": False}
    # the final assistant text record
    final_idx = None
    for i in range(len(records) - 1, -1, -1):
        if records[i][0] == "assistant" and records[i][1] == "text":
            final_idx = i
            break
    if final_idx is None:
        return {"text": "", "tools": [], "uuid": "", "end": 0,
                "readable": True}
    final_text = records[final_idx][2]
    final_uuid = records[final_idx][3] or ""
    final_end = records[final_idx][4]
    # the turn boundary: the previous genuine USER PROMPT
    boundary = -1
    for i in range(final_idx - 1, -1, -1):
        if records[i][0] == "user" and records[i][1] == "prompt":
            boundary = i
            break
    # gather the turn's tools (after the boundary, up to and incl. final)
    uses = {}        # id -> {name, input}
    results = {}     # id -> (succeeded, content)  — only present if a result exists
    for role, kind, payload, _u, _e in records[boundary + 1:final_idx + 1]:
        if kind == "tool_use":
            inp = payload.get("input")
            uses[payload.get("id")] = {
                "name": str(payload.get("name") or ""),
                # the COMMAND, not its JSON envelope: the trunk anchor
                # matches against what the shell saw ('git ... origin/main'),
                # never the serialized wrapper around it (meld R1 calibration)
                "input": inp.get("command") if isinstance(inp, dict) and \
                    isinstance(inp.get("command"), str) else json.dumps(inp or {}),
            }
        elif kind == "tool_result":
            tid = payload.get("tool_use_id")
            results[tid] = (not payload.get("is_error"),
                            _result_text(payload.get("content")))
    tools = []
    for tid, u in uses.items():
        if tid in results:
            succeeded, content = results[tid]
        else:
            succeeded, content = False, ""     # fail-closed: no result != success
        tools.append({"name": u["name"], "input": u["input"],
                      "succeeded": succeeded, "content": content})
    return {"text": final_text, "tools": tools, "uuid": final_uuid,
            "end": final_end, "readable": True}


# --- R4: the whole-object message lexer --------------------------------------
# One lexical scan over the full message returns every candidate classified
# ASSERTED / DENIED / QUOTED_OR_CODE / UNKNOWN. Normalization first (Unicode
# quote/apostrophe forms -> ASCII), then a state machine over paired quotes
# and Markdown code, then clause tokenization with contrast boundaries, then
# structural negation scoped to its clause. Anything unclassifiable is
# UNKNOWN — never silently credited.

_QUOTE_XLAT = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
})

# The CLOSED short-SHA context set (meld R4): a 7-12 hex token is a commit
# claim only when the immediately preceding prose token is one of these, or
# the sentence's VERB names a commit act (landed/merged/shipped/rebased/
# gated/tagged at ...). 'at' alone is deliberately absent — 'token cached
# at <short-hex>' is not a SHA claim.
_SHA_CONTEXT_WORDS = frozenset({
    "sha", "commit", "tip", "head", "tree", "ref", "rev", "landed", "gate",
    "@",
})
# Verbs that announce a measured result; 'at' after one of these introduces
# the commit the result is about ('green at <short-sha>', 'landed at <tip>').
_SHA_VERBS = frozenset({
    "landed", "merged", "shipped", "rebased", "gated", "tagged", "approved",
    "green", "ok", "passing",
})
_SHA_VERB_RE = re.compile(r"\b(?:%s)\b" % "|".join(sorted(_SHA_VERBS)), re.I)

_NEG_WORDS = frozenset({"not", "never", "no", "without",
                        "unverified", "unproven"})


def _is_word_char(c):
    return c.isalnum() or c == "_"


def _lex(text):
    """The whole-object scan. Returns (prose, unknown):
    prose = the message with quoted/coded spans blanked to spaces (offsets
            preserved, so clause math still lines up with the original),
    unknown = True when a delimiter never closed or a code fence never
            terminated — the message could not be fully classified, and no
            candidate from it may be silently credited."""
    n = len(text)
    out = []
    i = 0
    unknown = False
    # fenced code first: ``` ... ``` swallows everything, quotes included
    while i < n:
        c = text[i]
        if text.startswith("```", i):
            j = text.find("```", i + 3)
            if j == -1:
                unknown = True
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j + 3 - i))
                i = j + 3
        elif c == "`":
            j = text.find("`", i + 1)
            nl = text.find("\n", i + 1)
            if j == -1 or (nl != -1 and nl < j):
                # an unbalanced inline-code tick: the tick is not a code
                # span, and a same-line tick is ambiguous -> UNKNOWN
                unknown = True
                out.append(c)
                i += 1
            else:
                out.append(" " * (j + 1 - i))
                i = j + 1
        elif c in "\"'":
            # an apostrophe BETWEEN letters is word-internal (wasn't), never
            # a quote delimiter
            if (c == "'" and i > 0 and i + 1 < n
                    and _is_word_char(text[i - 1]) and _is_word_char(text[i + 1])):
                out.append(c)
                i += 1
                continue
            j = i + 1
            closed = -1
            while j < n:
                if text[j] == "\n":
                    break
                if text[j] == c:
                    closed = j
                    break
                j += 1
            if closed == -1:
                # an unbalanced quote: the quote is speech of unknown extent
                unknown = True
                out.append(c)
                i += 1
            else:
                out.append(" " * (closed + 1 - i))
                i = closed + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), unknown


def _clauses(prose):
    """Prose split into clause spans [(start, end)], resetting at sentence
    punctuation and at MID-CLAUSE contrast words (but/however/now) — the
    negation of one clause must not bleed into the next ('not verified
    before; now verified' keeps the positive). A contrast word only splits
    when real prose precedes it in the current clause; a clause-OPENING
    'Now,' starts a fresh clause rather than cutting an empty one."""
    spans = []
    start = 0
    for m in re.finditer(r"[.;:!?\n]|\b(?:but|however|now)\b", prose, re.I):
        if m.group(0).isalpha() and not prose[start:m.start()].strip():
            continue               # leading contrast word: no clause to cut
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(prose):
        spans.append((start, len(prose)))
    return spans


def _negated(clause):
    """A clause is a denial when it carries structural negation: not/never/
    no/without, an auxiliary ending in n't, or an explicit un- prefix. The
    word list is CLOSED; the contraction shape is structural (any word
    ending n't after normalization)."""
    for tok in re.findall(r"[A-Za-z][A-Za-z']*|\S", clause.lower()):
        if tok in _NEG_WORDS or (len(tok) > 3 and tok.endswith("n't")):
            return True
    return False


_HEX_TOKEN = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,64})(?![0-9A-Za-z])")


def _sha_candidate(clause):
    """First SHA in one clause under the CLOSED length/context grammar.

    The capped rung reports one candidate, so stop scanning once that owner
    value is known. Dash-adjacent tokens are UUID fragments, never SHAs."""
    for m in _HEX_TOKEN.finditer(clause):
        s = m.group(1)
        if clause[m.start() - 1:m.start()] == "-" or \
                clause[m.end():m.end() + 1] == "-":
            continue
        if len(s) in (40, 64):
            return s
        if len(s) <= 12:
            prefix = clause[:m.start()]
            prev = re.search(r"([A-Za-z@]+)\W*$", prefix)
            if prev:
                word = prev.group(1).lower()
                if word in _SHA_CONTEXT_WORDS or (
                        word == "at" and _SHA_VERB_RE.search(prefix)):
                    return s
        # 13..39: not a SHA, whatever the context
    return None


def _claims(text):
    """The claims a text actually MAKES, from the whole-object lexer (meld
    R4): quoted/coded speech is QUOTED_OR_CODE, a negated clause is DENIED,
    and an unclassifiable message (unbalanced quote/code) credits NOTHING.
    A claim inside reported speech or a denial is not the seat's own
    settled assertion — 'the report said "42% land rate"' and 'this wasn't
    verified' must not fire."""
    normalized = text.translate(_QUOTE_XLAT)
    prose, unknown = _lex(normalized)
    if unknown:
        return {}                      # refuse to credit the unclassifiable
    out = {}
    for start, end in _clauses(prose):
        clause = prose[start:end]
        if _negated(clause):
            continue                   # DENIED clause: no claims stand in it
        for name, rx in (("bare-number", _COUNT_CLAIM),
                         ("proof-word", _PROOF_WORD),
                         ("landed-claim", _LANDED_WORD)):
            if name not in out:
                m = rx.search(clause)
                if m:
                    out[name] = m.group(0)
        if "unresolved-sha" not in out:
            sha = _sha_candidate(clause)
            if sha:
                out["unresolved-sha"] = sha
    return out


# --- R1: exact-token anchoring -----------------------------------------------
# A claimed number must appear as an exact TOKEN in a successful tool's
# RESULT CONTENT — '142 rows' never earned '42'. A claimed SHA anchors in
# the command input (a resolution command names its object) or the result.
# A 'landed' claim needs a GIT command (git / git -C ...) resolving the
# canonical trunk origin/main — never bare local main.

def _norm_number(snippet):
    """The numeric literal in a count/percent claim, normalized for an exact
    token match (meld R1): '42%' -> '42', '5705 tests' -> '5705',
    '132.6h' -> '132.6'."""
    m = re.search(r"\d+(?:\.\d+)?", snippet)
    return m.group(0) if m else None


def _has_token(haystack, needle):
    """needle as an exact token of haystack — never a substring of a longer
    one ('142' contains '42' but is not the 42 that was claimed)."""
    if not needle:
        return False
    esc = re.escape(needle)
    return bool(re.search(r"(?<![\w.])%s(?![\w.])" % esc, haystack))


def _number_in_tools(value, tools):
    """Does a claimed number anchor in successful RESULT CONTENT? The result
    is the measurement; the command's echo of the claim is not."""
    return any(t["succeeded"] and value
               and _has_token(t["content"], value) for t in tools)


def _sha_in_tools(sha, tools):
    """Does a claimed SHA anchor in a successful resolution tool's input or
    content? Predicate commands legitimately return no content on success."""
    return any(t["succeeded"] and t["name"] in _SHA_TOOLS and sha
               and (_has_token(t["input"], sha)
                    or _has_token(t["content"], sha)) for t in tools)


# A PURE git command — the whole command line is one git invocation, never
# a chain by ANY shell mechanism: ; | & `&&`, a NEWLINE, a $(...) or
# backtick substitution, or a trailing # comment (each names the trunk
# without resolving it, so each earns nothing, meld R1 calibration).
_GIT_CMD = re.compile(r"^\s*git(?:\s+-C\s+\S+)?\s[^;&|\n$`#]*$")
_GIT_TRUNK = re.compile(r"\borigin/main\b|\brefs/remotes/origin/main\b")


def _trunk_check(tools, sha):
    """A successful PURE git command naming the canonical trunk (origin/main,
    never bare local main — a checkout of local main says nothing about the
    land). The trunk must be named in the COMMAND; a result that merely
    prints 'origin/main' is output, not a resolution. The claimed SHA
    anchors when present (meld R1)."""
    for t in tools:
        if not t["succeeded"] or t["name"] not in _LANDED_TOOLS:
            continue
        if not _GIT_CMD.match(t["input"]):
            continue
        if _GIT_TRUNK.search(t["input"]):
            if not sha or _has_token(t["content"], sha) \
                    or _has_token(t["input"], sha):
                return True
    return False


def findings(text, tools):
    """[(shape, claim-snippet, why)] for claims the turn did not earn.

    tools is the turn's [{name, input, succeeded, content}]. A claim is earned
    by an EXACT ANCHOR to a successful tool (meld R1): a count/percent needs
    its normalized number in a successful tool's content; a SHA needs the
    exact sha in a successful tool's input or content; 'landed' needs a
    successful git command naming origin/main (and the claimed sha when
    present). A proof-word inherits ONLY from an earned concrete claim in the
    same message — standalone 'verified/green' with no anchored subject still
    warns. An unrelated successful tool earns NOTHING."""
    if not str(text or "").strip():
        return []
    claims = _claims(text)
    if not claims:
        return []
    out = []
    earned_any_concrete = False
    if "bare-number" in claims:
        num = _norm_number(claims["bare-number"])
        if _number_in_tools(num, tools):
            earned_any_concrete = True
        else:
            out.append(("bare-number", claims["bare-number"],
                        "a count whose value no successful tool produced this turn"))
    if "unresolved-sha" in claims:
        sha = claims["unresolved-sha"]
        if _sha_in_tools(sha, tools):
            earned_any_concrete = True
        else:
            out.append(("unresolved-sha", sha[:12],
                        "a named sha no successful tool resolved this turn"))
    if "landed-claim" in claims:
        if _trunk_check(tools, claims.get("unresolved-sha")):
            earned_any_concrete = True
        else:
            out.append(("landed-claim", claims["landed-claim"],
                        "'landed' with no successful origin/main check this turn"))
    if "proof-word" in claims and not earned_any_concrete:
        out.append(("proof-word", claims["proof-word"],
                    "a proof word with no earned concrete claim behind it"))
    return out


def _message_id(turn):
    """Stable id for an already-read final assistant record (meld R3)."""
    if not turn["readable"] or not turn["text"]:
        return ""
    if turn["uuid"]:
        return hashlib.blake2b(turn["uuid"].encode("utf-8"),
                               digest_size=8).hexdigest()
    return hashlib.blake2b(
        ("%d:" % turn["end"]).encode("utf-8") + turn["text"].strip().encode("utf-8"),
        digest_size=8).hexdigest()


def assessment(transcript_path):
    """(WARN lines, readable, message id) from ONE transcript snapshot.

    The latch identity must describe the same final record whose claims were
    assessed; reading a growing transcript twice can bind message A's finding
    to message B's identity."""
    turn = _turn(transcript_path)
    if not turn["readable"]:
        return [], False, ""
    hits = findings(turn["text"], turn["tools"])
    out = []
    if hits:
        out = ["CLAIMS WITHOUT THIS TURN'S EVIDENCE — these read as settled "
               "and the turn made no measurement that earns them:"]
        for shape, snippet, why in hits:
            out.append("  [%s] %r — %s" % (shape, snippet[:60], why))
        out.append("Run the measurement and quote it, or say who measured it "
                   "and when. A claim summarising a summary is a guess "
                   "wearing a report.")
    return out, True, _message_id(turn)


def gate_lines(transcript_path):
    """The Stop-hook WARN lines plus readable tri-state."""
    lines, readable, _mid = assessment(transcript_path)
    return lines, readable
