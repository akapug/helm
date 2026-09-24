#!/usr/bin/env python3
"""The claim-evidence rung — a Stop-hook WARNER for outgoing claims the turn
did not earn.

Owner canon (the operator doing this audit by hand, asking for it
organized): a claim that SOUNDS settled feels finished, and summarising
someone else's summary feels like work rather than like guessing. Four
claim shapes, all measured live:

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

THE FIFTH SHAPE, AND THE SECOND PLACE TO LOOK — A CLAIM INDEXED TO *NOW*,
IN TEXT THE TURN MAKES DURABLE.

The four shapes above read `turn["text"]`: what a seat says to its own
operator. The durable text a seat publishes to OTHER seats — the chat and
dispatch send doors — never appears there. It rides inside a Bash tool_use
input, and a turn can publish a fleet-facing body while carrying no
assistant text record at all (only thinking and a tool_use), so a rung
reading only the private summary lets the fleet-facing artifact go out
unread. Running the four text shapes over published bodies fires on more
than a third of them — noise, not a mechanism — so publications get
exactly ONE shape, the one whose defect is specific to them:

  5. a claim the author INDEXED TO THE PRESENT INSTANT ("currently", "right
     now", "as of now") whose measurement instant is UNDISCLOSED, STALE, or
     IMPOSSIBLE.

WHY THIS SHAPE HAS NO TOOL ANCHOR, DELIBERATELY. Shapes 1-4 anchor a claim to
an exact token in a successful tool's output. A present-indexed claim has no
such token, and both candidate anchors launder: "any successful tool in
the turn" credits an unrelated command, and "the claim's words appear in
some tool" credits a present-indexed status claim that is simply false.
An anchor that credits a false claim is worse than none. What separates
honest publications from failing ones is purely syntactic: THE HONEST
ONES SAY WHEN — a claim naming its measurement instant, minutes old,
against the same claim shape carrying no instant at all, published hours
after the reading it rested on.

So the fifth shape never adjudicates whether a measurement happened. It asks
only whether the artifact discloses its own age, which the reader needs in
every case and which the author alone can state. The threat model is the
owner's: ACCIDENT, NOT ADVERSARY — nobody misstates when they measured; they
reuse an expired reading honestly, and a durable body carries it forward as
a standing fact long after it stopped being one.

Stdlib + the package's own punt transcript reader only.
"""
import datetime
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

# --- shape 5: the publication doors and the present-instant grammar ---------
# The verbs that make free prose DURABLE to other seats — the send doors of
# the chat/dispatch grammar (`chat reply` is a durable post funnel too, per
# chat's own help; its omission cost a real miss). `helm` may be reached as
# `helm`, `./bin/helm` or `python3 -m helm`, so the prefix is not anchored.
# This matches inside ONE masked shell segment, so a quoted or heredoc
# mention of a door is data by construction, never a command.
_PUB_VERB = re.compile(
    r"\bhelm\s+(chat\s+(?:post|dm|reply)|dispatch\s+(?:send|verdict))\b")
# A heredoc opener inside a masked segment. `<<` survives masking (it is an
# operator, never quoted data); the TAG is read from the RAW text at the
# same offset because a quoted delimiter's characters are masked away —
# which is also why the opener pattern must NOT consume trailing space:
# a masked `'EOF'` IS spaces, and an opener that eats them reads the tag
# from past its own delimiter — an opener that eats them makes every heredoc
# body fall back to quoted-argument extraction as the bare tag word.
_HD_OPEN = re.compile(r"<<-?")
_HD_TAG = re.compile(r"[ \t]*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))")


def _pub_entries(cmd):
    """([(verb, [bodies], seg_index)], total_segments) for one Bash input.

    LINE-STRUCTURED, LIKE THE CHAT ARGV-GUARD'S OWN READER — this reuses
    chat._shell_segments (quote-aware top-level operator split) and
    chat._mask_quoted_line rather than growing a rival shell grammar: the
    supported send doors keep one owner. Three properties the old
    whole-payload regexes lacked, each a measured miss:

      * a heredoc body belongs to the LINE that opened it — `cat >f <<'A'`
        followed by a later post no longer donates A's private file body to
        the publication, and a `# helm chat post ...` comment is truncated
        out of the masked text before the verb search, so comment text
        cannot mint a publication.
      * a no-heredoc door's body is the CONTENT of its quoted arguments —
        `chat post "The service is currently down."` hands the message
        itself downstream, where the whole raw command would be blanked as
        quotation by the R4 lexer and yield a silent zero.
      * the walk is LINEAR. The old pattern's unanchored `helm[^\\n]*?`
        restarted to end-of-line at every nonmatching `helm`; 250KB of
        repeated bare `helm` measured 115.8s. Segments are scanned once,
        and bodies are parsed only after a door is identified.
    """
    from .chat import (_shell_segments, _mask_quoted_line,
                       _shell_comment_start)
    entries = []
    pending = []       # (strip_tabs, tag, sink_or_None) in open order
    total = 0
    for line in cmd.splitlines():
        if pending:
            strip, tag, sink = pending[0]
            text = line.rstrip("\r\n")
            if (text.lstrip("\t") if strip else text) == tag:
                pending.pop(0)
            elif sink is not None:
                sink.append(line)
            continue
        # THE COMMENT IS CUT BEFORE THE SPLIT — an operator inside a
        # comment is not a statement boundary, and cutting after splitting
        # counted a commented semicolon as a second segment. Recognition
        # lives beside the splitter in chat, one owner for both questions.
        cut = _shell_comment_start(line)
        if cut != -1:
            line = line[:cut]
        for seg in _shell_segments(line):
            if not seg.strip():
                continue
            mseg = _mask_quoted_line(seg)
            if not mseg.strip():
                continue       # a pure comment is not a statement
            total += 1
            opens = []
            for op in _HD_OPEN.finditer(mseg):
                tm = _HD_TAG.match(seg[op.end():])
                if tm:
                    tag = next(g for g in tm.groups() if g is not None)
                    opens.append((seg[op.start():].startswith("<<-"), tag))
            vm = _PUB_VERB.search(mseg)
            if vm:
                verb = re.sub(r"\s+", " ", vm.group(1))
                sinks = [[] for _ in opens]
                for (strip, tag), sink in zip(opens, sinks):
                    pending.append((strip, tag, sink))
                entries.append((verb, sinks, seg, total - 1))
            else:
                for strip, tag in opens:
                    pending.append((strip, tag, None))
    out = []
    for verb, sinks, seg, idx in entries:
        if sinks:
            bodies = ["\n".join(s) for s in sinks]
        else:
            # quoted-argument CONTENT, in order; the raw segment only when
            # nothing is quoted, so an unquotable body is still scanned
            # whole rather than assumed empty.
            quoted = re.findall(r"'([^']*)'|\"([^\"]*)\"", seg)
            parts = [a or b for a, b in quoted if (a or b).strip()]
            bodies = [" ".join(parts)] if parts else [seg]
        out.append((verb, bodies, idx))
    return out, total

# DEICTIC PRESENT ONLY. A bare "now"/"today"/"still" was measured ambiguous —
# "the filter NOW binds row" describes code after a change, not the world at
# this instant — and including it tripled the fire rate with none of the
# night's real failures added. These forms mean one thing.
_NOW_CLAIM = re.compile(
    r"\b(?:currently|right now|presently|as of (?:now|this writing)|"
    r"at this (?:moment|instant)|as we speak|as things stand)\b", re.I)

# A wall-clock instant as this fleet writes them: 09:41Z, 01:54Z, 05:37:23Z,
# 2026-01-15T04:43:23Z. A bare date carries no instant and does not count.
_INSTANT = re.compile(
    r"(?<![\d:])(?:(\d{4})-(\d{2})-(\d{2})[T ])?"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*Z\b")

# A STATE PREDICATION, without which a present marker is not a claim about
# the world. MEASURED: "COMMIT IN YOUR WORKTREE. RIGHT NOW, BEFORE YOUR NEXT
# DEEP READ" and "WHY THIS MATTERS RIGHT NOW" are urgency on an imperative —
# the marker times the READER'S action, not a fact. A copula (or `cannot`,
# which predicates a state just as squarely: "an approve cannot authorize
# anything right now") is what makes it an assertion. The CONTRACTED forms
# are deliberately absent rather than forgotten: any word ending n't already
# makes its clause a denial upstream, so isn't/aren't/can't can never reach
# this test and listing them would be dead alternation.
_STATE_PRED = re.compile(r"\b(?:is|are|cannot)\b", re.I)

# The honest citations in the measured corpus clustered at 0, 1, 5 and 9
# minutes; the one stale citation was 22. Twenty sits in the measured gap.
NOW_CLAIM_FRESH_S = 20 * 60
# A cited instant may legitimately run a little ahead of the record clock
# (the author reads `date -u`, then writes). Ninety seconds is slack, not a
# tolerance for a fabricated citation — the measured real case was +12m.
_FUTURE_SLACK_S = 90
# A BARE HH:MMZ carries no date, and the overwhelmingly common reason one
# reads as "ahead" is that it belongs to YESTERDAY: a run log quoting
# "2026-01-14T19:29Z, 23:27Z, 00:52Z, 05:06Z" dates only the first. MEASURED
# before this rule existed, 5 of 5 live `future-instant` findings were that
# case and every one was a false accusation of fabrication. Beyond this
# window a bare instant is read as the previous day; inside it, a citation
# ahead of the clock is what it looks like (the measured real one was +12m).
_DAY_ROLLBACK_S = 2 * 60 * 60


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


# The record's own stamp, as this harness writes it on every transcript line:
# 2026-07-25T07:13:05.036Z (measured on a live transcript). The record clock
# is UTC; a record that says nothing about when it happened parses as None,
# never as "now" — substituting the reader's clock for the writer's is the
# exact defect this module warns about.
_RECORD_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d+)?(?:Z|\+00:00)?$")


def _record_instant(d):
    """The record's own wall-clock instant (aware UTC), or None."""
    m = _RECORD_TS.match(str(d.get("timestamp") or ""))
    if not m:
        return None
    try:
        return datetime.datetime(*map(int, m.groups()),
                                 tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _parse_records(raw):
    """The transcript's records as (role, kind, payload, uuid, end, ts), in
    order.

    kind is 'text' | 'tool_use' | 'tool_result' for assistant records, and
    'prompt' | 'tool_result' for user records. The role/kind split is the
    meld's R2 foundation: a USER-ROLE record that carries a tool_result is
    NOT a turn boundary — only a genuine user PROMPT is. Both record
    envelopes (wrapped + flat) parse; `unsupported` counts observed records
    that fit neither envelope. `end` is the record's byte offset in the raw
    tail (the no-UUID fallback's stable identity, meld R3). `ts` is the
    record's OWN instant from its envelope stamp, or None when the record
    does not say — never a substituted clock."""
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
        ts = _record_instant(d)
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
                        records.append(("user", "tool_result", item, uuid,
                                        end, ts))
                texts = [str(i.get("text")) for i in parts
                         if isinstance(i, dict) and i.get("type") == "text"
                         and str(i.get("text") or "").strip()]
                if texts:
                    records.append(("user", "prompt", "\n".join(texts),
                                    uuid, end, ts))
            else:
                records.append(("user", "prompt", content, uuid, end, ts))
        elif role == "assistant":
            for item in parts:
                if not isinstance(item, dict):
                    continue
                t = item.get("type")
                if t == "text" and str(item.get("text") or "").strip():
                    records.append(
                        ("assistant", "text", str(item["text"]), uuid, end, ts))
                elif t == "tool_use":
                    records.append(("assistant", "tool_use", item, uuid,
                                    end, ts))
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
    # THE FINAL RECORD IS THE LAST ASSISTANT RECORD OF ANY KIND. The first
    # cut looked for the last assistant TEXT first and fell back to any-kind
    # only when no text existed — so a turn shaped [text ... tool-publication]
    # bound its identity to the OLDER text and its tool span STOPPED there,
    # and the newest publication in the turn was exactly the record the rung
    # never read (the latest tool-only publication otherwise loses
    # to older assistant text). The latch identity must be the record whose
    # newness a re-read detects, which is the last record, whatever its kind;
    # the TEXT under assessment is still the turn's last prose.
    final_idx = None
    for i in range(len(records) - 1, -1, -1):
        if records[i][0] == "assistant":
            final_idx = i
            break
    if final_idx is None:
        return {"text": "", "tools": [], "uuid": "", "end": 0,
                "readable": True}
    final_uuid = records[final_idx][3] or ""
    final_end = records[final_idx][4]
    # the turn boundary: the previous genuine USER PROMPT
    boundary = -1
    for i in range(final_idx - 1, -1, -1):
        if records[i][0] == "user" and records[i][1] == "prompt":
            boundary = i
            break
    # the turn's outgoing PROSE: its last assistant text within the span. A
    # turn may have none — a fleet-facing turn's final record can BE a tool_use —
    # and an empty text with a live tool span is the shape this rung exists
    # for, never a reason to discard the turn.
    final_text = ""
    for i in range(final_idx, boundary, -1):
        if records[i][0] == "assistant" and records[i][1] == "text":
            final_text = records[i][2]
            break
    # the turn's tools: USES from the span, RESULTS from the whole tail.
    # Identity selection and result joining are separate questions — the
    # final record bounds which tool_uses are this turn's, but a result is a
    # user-role record that lands AFTER the tool_use it answers, so the
    # topology [tool_use -> tool_result -> no later prose] puts the result
    # past the final record. Joining only inside the span reads a successful
    # tool-only post as unresolved.
    tools = _tools_of(records[boundary + 1:final_idx + 1],
                      results_from=records)
    return {"text": final_text, "tools": tools, "uuid": final_uuid,
            "end": final_end, "readable": True}


def _tools_of(records, results_from=None):
    """The span's tool_use records joined to their results, as a list of
    {name, input, succeeded, content, resulted, at}. `results_from` widens
    the RESULT scan beyond the use span (results bind by tool_use_id, so a
    wider scan cannot mis-join; omitting it keeps both scans on `records`).

    `succeeded` stays fail-closed for the ANCHOR shapes: a missing
    tool_result reads as NOT succeeded (meld R1's law — an unresolved tool
    must never EARN a claim). `resulted` carries the third state those
    shapes deliberately collapse: whether a result record EXISTS at all,
    which `publications` needs because a recorded failure and a result that
    has not landed yet mean opposite things about whether text went out.

    `at` is the PUBLICATION'S OWN instant: the result record's stamp when
    one landed (the row was durable by then), else the tool_use record's
    stamp (the instant the command went out). Both are the writer's clock;
    None means the transcript did not say, and stays None — the reader's
    clock never substitutes."""
    uses = {}        # id -> {name, input, at}
    results = {}     # id -> (succeeded, content, at)  — only if a result exists
    for role, kind, payload, _u, _e, ts in records:
        if kind == "tool_use":
            inp = payload.get("input")
            uses[payload.get("id")] = {
                "name": str(payload.get("name") or ""),
                # the COMMAND, not its JSON envelope: the trunk anchor
                # matches against what the shell saw ('git ... origin/main'),
                # never the serialized wrapper around it (meld R1 calibration)
                "input": inp.get("command") if isinstance(inp, dict) and \
                    isinstance(inp.get("command"), str) else json.dumps(inp or {}),
                "at": ts,
            }
    # RESULTS FROM THE WIDER SCAN when the caller passed one — the second
    # loop, not a second name on the first: a widened parameter that only
    # the signature carries is the wired-partway class, a join that reads
    # narrower than it declares.
    for role, kind, payload, _u, _e, ts in (results_from or records):
        if kind == "tool_result":
            tid = payload.get("tool_use_id")
            results[tid] = (not payload.get("is_error"),
                            _result_text(payload.get("content")), ts)
    tools = []
    for tid, u in uses.items():
        if tid in results:
            succeeded, content, rts = results[tid]
            resulted = True
        else:
            succeeded, content, rts = False, "", None
            resulted = False   # fail-closed for anchors: no result != success
        tools.append({"name": u["name"], "input": u["input"],
                      "succeeded": succeeded, "content": content,
                      "resulted": resulted, "at": rts or u["at"]})
    return tools


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


# --- shape 5: what the turn PUBLISHES ---------------------------------------

def publications(tools):
    """[(verb, body, at, durable)] for every body this turn sent through a
    publication door, in order. `at` is the publication's own instant (see
    `_tools_of`), or None when its transcript record did not say. `durable`
    is True only when a result record SAYS the text landed — the advisory
    words itself on it, because "you just made durable" about an unresolved
    attempt is a claim the record never made.

    A publication is a send door of the chat/dispatch grammar, found by
    _pub_entries' segment walk. The body is each heredoc THAT SEGMENT
    opened; a no-heredoc door contributes its quoted arguments' content,
    or the whole segment when nothing is quoted — a body the extractor
    cannot isolate is scanned whole, never read as empty. Identical bodies
    published twice in one turn are reported once — the same text is one
    claim however many doors it went through.

    WHAT COUNTS AS PUBLISHED — attribution is POSITIONAL and symmetric,
    because the tool-level exit code is the LAST segment's and speaks for
    the door exactly when the door IS the last segment. Both failure
    directions of the alternative are live: a door-mid failure hides a
    successful post, and `post ... || true` returns success over a
    refused one:

      * door-last, result recorded, success — text went durable; assess it.
      * door-last, result recorded, FAILURE — the door's own refusal;
        excluded on the record's word. Warning about text that never
        published is the false-positive class the exclusion exists for.
      * door anywhere else, or NO result record — an ATTEMPT: assessed,
        never excluded, never called durable. The aggregate exit proves
        nothing about a door it does not sit on, and a fleet-facing turn's
        final record can BE the tool_use, so demanding a landed result would
        blind the rung to exactly its subject. The cost of assessing an
        attempt that then fails is one advisory line; the cost of silence
        on one that lands is the rung's whole point. This is not the
        anchor law's fail-closed default — `succeeded` collapses this
        state to False on purpose there; the distinction rides in
        `resulted` and the segment index."""
    out = []
    seen = set()
    for t in tools:
        cmd = t.get("input") or ""
        if "helm" not in cmd:
            continue           # linear prescan; the parser never runs cold
        entries, total = _pub_entries(cmd)
        for verb, bodies, idx in entries:
            # ATTRIBUTION IS POSITIONAL, symmetric in both directions. The
            # tool-level exit code is the LAST segment's, so it speaks for
            # the door exactly when the door IS the last segment: a
            # door-last failure is the door's own refusal (excluded on the
            # record's word), a door-last success went durable. Anywhere
            # else the aggregate proves NOTHING either way — `post ... ||
            # true` returns 0 over a refused post — so the publication is
            # assessed as an ATTEMPT: never excluded, never called durable.
            door_owns_rc = t.get("resulted") and idx == total - 1
            if door_owns_rc and not t.get("succeeded"):
                continue       # the door itself refused; nothing went out
            durable = bool(door_owns_rc and t.get("succeeded"))
            for b in bodies:
                if not b.strip():
                    continue
                key = (verb, b)
                if key in seen:
                    continue
                seen.add(key)
                out.append((verb, b, t.get("at"), durable))
    return out


def _now_clauses(prose):
    """Clause spans for the present-instant shape.

    Deliberately NOT `_clauses`: that splitter cuts at the word `now` so a
    denial cannot bleed past a correction, which would sever the very phrase
    this shape reads ("right now" would end one clause and open the next).
    Splitting only at sentence punctuation and at contrast keeps the phrase
    whole, and keeps any negation in the same clause as the claim it denies —
    which errs toward SILENCE, the safe direction for a warner."""
    spans = []
    start = 0
    for m in re.finditer(r"[.;:!?\n]|\b(?:but|however)\b", prose, re.I):
        if m.group(0).isalpha() and not prose[start:m.start()].strip():
            continue
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(prose):
        spans.append((start, len(prose)))
    return spans


def _instants(text, at):
    """[(literal, seconds_before `at`)] for every wall-clock instant cited.
    When `at` is None (the publication's own instant is unknown) each valid
    citation still counts as PRESENT but its age is None — unknowable, not
    zero and not computed against some other clock.

    A bare HH:MMZ carries no date. It is read on `at`'s own UTC day, and
    rolled back one day when that reading lands more than _DAY_ROLLBACK_S
    ahead of the clock — a body quoting a run log dates its first stamp and
    leaves the rest bare, so "23:27Z" published at 05:39Z is last night, not
    a claim about the future. What survives as still-ahead after that rollback
    is ahead by MINUTES, which is the shape of a constructed citation rather
    than of a day boundary."""
    out = []
    for m in _INSTANT.finditer(text):
        y, mo, d, hh, mm, ss = m.groups()
        try:
            if y:
                dt = datetime.datetime(int(y), int(mo), int(d), int(hh),
                                       int(mm), int(ss or 0),
                                       tzinfo=datetime.timezone.utc)
            elif at is not None:
                dt = at.replace(hour=int(hh), minute=int(mm),
                                second=int(ss or 0), microsecond=0)
                if (dt - at).total_seconds() > _DAY_ROLLBACK_S:
                    dt -= datetime.timedelta(days=1)
            else:
                # validity only: a bare stamp with no publication instant
                # cannot be placed on a day, but 25:99Z is still not a time
                datetime.time(int(hh), int(mm), int(ss or 0))
                out.append((m.group(0), None))
                continue
        except ValueError:
            continue            # 25:99Z is not an instant
        out.append((m.group(0),
                    None if at is None else (at - dt).total_seconds()))
    return out


def now_findings(body, at=None):
    """[(shape, snippet, why)] for present-indexed claims in ONE published body.

    `at` is the PUBLICATION'S OWN instant — the stamp its transcript record
    carries — and None means the record did not say. None NEVER becomes the
    caller's clock: a substituted clock reads one body silent at one firing
    and stale at the next purely because the hook fired later — the verdict a
    function of hook timing rather than of the claim. An instrument that
    silently substitutes a different clock is the exact class this rung
    exists to catch. Four outcomes, all syntactic — the rung never decides
    whether a measurement happened, only whether the artifact says when:

      * an instant AFTER `at` — the citation names a time that has not
        happened, so it cannot be a reading. This is the only one stated as
        a fact about the world rather than a request.
      * NO instant anywhere in the body — the claim reads as a standing fact
        with nothing to date it.
      * every cited instant older than NOW_CLAIM_FRESH_S — the body says
        when, and when was a while ago.
      * instants cited but `at` is None — the body's age is UNKNOWN, and the
        finding says exactly that instead of borrowing a clock or going
        silent.

    THE FRESHEST citation is the one that counts, and it is taken over the
    WHOLE body rather than the claim's own clause: an author who dates any
    part of the artifact has disclosed a reading instant to their reader, and
    crediting that errs toward silence. A warner earns its keep by being
    quiet when the author did the right thing."""
    if not str(body or "").strip():
        return []
    prose, unknown = _lex(str(body).translate(_QUOTE_XLAT))
    if unknown:
        return []              # unclassifiable message credits nothing, and
                               # accuses nothing either
    claim = None
    for start, end in _now_clauses(prose):
        clause = prose[start:end]
        if not _NOW_CLAIM.search(clause):
            continue
        if _negated(clause) or not _STATE_PRED.search(clause):
            continue
        claim = clause.strip()
        break
    if claim is None:
        return []
    # instants are read from the RAW body: a reading instant is routinely
    # quoted back from the tool that produced it, and `_lex` blanks quotes
    stamps = _instants(str(body), at)
    if not stamps:
        # at-independent: no instant in the body is no instant whatever the
        # publication clock says (or fails to say)
        return [("undated-now", claim[:120],
                 "a claim indexed to the present instant, with no "
                 "measurement instant anywhere in the body")]
    if at is None:
        # the body says when — the TRANSCRIPT does not say when it published,
        # so the citation cannot be aged. UNKNOWN is the whole verdict: not
        # stale (that would accuse), not silent (that would vouch), and
        # never a substituted clock (that is the defect this rung catches).
        return [("unknown-instant", claim[:120],
                 "a claim indexed to the present instant, citing %s — but "
                 "this publication's own record carries no timestamp, so the "
                 "citation's age is UNKNOWN, not measured" % stamps[0][0])]
    ahead = [(lit, age) for lit, age in stamps if age < -_FUTURE_SLACK_S]
    if ahead:
        lit, age = min(ahead, key=lambda p: p[1])
        return [("future-instant", lit,
                 "a claim about NOW citing %s — %d minutes from now, an "
                 "instant that has not happened" % (lit, round(-age / 60.0)))]
    fresh = min(age for _lit, age in stamps)
    if fresh > NOW_CLAIM_FRESH_S:
        lit = min(stamps, key=lambda p: p[1])[0]
        return [("stale-now", claim[:120],
                 "a claim indexed to the present instant, in a body whose "
                 "freshest instant, %s, is %d minutes old"
                 % (lit, round(fresh / 60.0)))]
    return []


def _message_id(turn):
    """Stable id for an already-read final assistant record (meld R3).

    The final record may be a TOOL_USE — a turn that only published, the
    fifth shape's defining case — and it still needs a real identity: with
    the empty id every tool-only turn shares one latch fingerprint, so one
    latched advisory silently suppresses the next turn's different one."""
    if not turn["readable"]:
        return ""
    if turn["uuid"]:
        return hashlib.blake2b(turn["uuid"].encode("utf-8"),
                               digest_size=8).hexdigest()
    if not turn["end"]:
        return ""      # no assistant record at all — nothing to name
    return hashlib.blake2b(
        ("%d:" % turn["end"]).encode("utf-8") + turn["text"].strip().encode("utf-8"),
        digest_size=8).hexdigest()


# Blocker-3 cure: one closing line PER STATE FOUND, each describing the state
# it closes. The old single trailer said "nothing to date it"/"no instant"
# under EVERY state — but a stale finding HAS an instant (an old one) and a
# future finding HAS an instant (an impossible one); calling either "no
# instant" described a third state that was not what was found.
_NOW_TAILS = (
    ("undated-now",
     "Say when you measured it. A present tense with no instant keeps "
     "reading TRUE long after it stopped being."),
    ("stale-now",
     "The reading it cites had already expired when you published. "
     "Re-measure, or index the claim to its instant instead of to NOW."),
    ("future-instant",
     "It cites an instant that had not happened at publication; name the "
     "clock that actually produced the reading."),
    ("unknown-instant",
     "Your body says when — this transcript's record does not say when it "
     "published, so the age is UNKNOWN. This line reports the instrument's "
     "blindness, not a defect in your text."),
)


def assessment(transcript_path):
    """(WARN lines, readable, message id) from ONE transcript snapshot.

    The latch identity must describe the same final record whose claims were
    assessed; reading a growing transcript twice can bind message A's finding
    to message B's identity.

    THERE IS DELIBERATELY NO CLOCK PARAMETER. Each publication is judged at
    ITS OWN recorded instant (`publications` carries it), so the verdict is a
    function of the claim and its publication — never of when the Stop hook
    happened to fire. The borrowed-clock default read one body silent at
    one firing and stale at the next, for no reason but hook timing."""
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
    durable_pubs, attempt_pubs = [], []
    shapes = set()
    for verb, body, pub_at, durable in publications(turn["tools"]):
        for shape, snippet, why in now_findings(body, pub_at):
            # the door is named WITHOUT its `helm` prefix on purpose: a
            # backticked "helm <token>" is command-shaped text, and this one
            # interpolates, so the literal advertises a verb spelled `%s`
            # that no parser dispatches (caught by the advertised-verb guard)
            shapes.add(shape)
            dest = durable_pubs if durable else attempt_pubs
            dest.append("  [%s] via the `%s` door — %s" % (shape, verb, why))
            dest.append("      %r" % (snippet[:90],))
    # TWO HEADERS, because "you just made durable" was said about attempts
    # whose result never landed — a claim the record never made. A
    # resolved success earns the durable sentence; anything
    # unresolved is an ATTEMPT and the advisory says so.
    if durable_pubs:
        out = out + [
            "A CLAIM ABOUT *NOW*, IN TEXT YOU JUST MADE DURABLE — the "
            "reader receives it as a standing fact:"] + durable_pubs
    if attempt_pubs:
        out = out + [
            "A CLAIM ABOUT *NOW*, IN TEXT THIS TURN SENT TO A PUBLICATION "
            "DOOR WITHOUT A RESOLVED SUCCESS — if the row landed, the "
            "reader receives it as a standing fact:"] + attempt_pubs
    if durable_pubs or attempt_pubs:
        out = out + [tail for shape, tail in _NOW_TAILS if shape in shapes]
    return out, True, _message_id(turn)


def gate_lines(transcript_path):
    """The Stop-hook WARN lines plus readable tri-state."""
    lines, readable, _mid = assessment(transcript_path)
    return lines, readable
