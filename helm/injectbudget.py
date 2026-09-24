#!/usr/bin/env python3
"""WHAT SHARE OF A SEAT'S CONTEXT DID OUR OWN HOOKS PUT THERE.

Every hook on the per-turn loop was locally reasonable and each added a few
hundred bytes for a good reason. Nothing anywhere reported the SUM, so the
composition was never a number anybody could be wrong about -- which is the
same composition hole as a fleet of individually-correct watchdogs that
together watch nothing. This module is the surface that reports the sum.

THE POPULATION IS DECIDED BY PROVENANCE, NEVER BY A TEXT MATCH, and that
distinction is the whole reason this instrument is trustworthy where reading
a transcript for hook markers is not. A record counts as hook-injected when
the harness itself typed it so: `type == "attachment"` and `attachment.type`
begins with `hook_`, carrying the harness's own `hookEvent`. A grep for a
marker would also match the marker quoted in a test, in a command typed while
investigating this, and in this file -- three different worlds sharing one
spelling, with the population decided by text rather than by who wrote it.

AND THE COST IS THE `rendered` BLOCK, NOT THE LINE. A hook record on disk
carries its `command` (kilobytes of wrapper shell), its `stdout`, its
`content` and, where the harness writes one, its `rendered` -- and the same
payload text is repeated across those fields while reaching the model ONCE.
`content` and `stdout` are byte-identical on every record, so the duplication
is at least twofold; where a `rendered` block is also present it is
threefold. WHICH IT IS VARIES BY HARNESS BUILD AND MUST NOT BE TAKEN AS A
CONSTANT -- a multiplier measured on one session and applied to the fleet is
the same error as adopting one seat's share as the fleet's. Nothing here
depends on the factor: the census reads the injected field and never scales a
line count. Sizing hook cost by line bytes overstates it by whatever that
record's own redundancy happens to be, and counting entries by a regex over
the raw line multiplies every count by the same unknown factor, which on a
single transcript is uniform enough to read as a stable, credible repeat
rate. `rendered` is the
harness's own statement of what it placed in the context window, so it is
the only field that answers the question this module asks. `unrendered`
below is the control on exactly that assumption: it counts hook records that
carried payload text but no rendered block, and a reader who sees it grow
knows this instrument has stopped being able to see what it claims to.

SEGMENTED AT COMPACTION, BECAUSE A CROSS-BOUNDARY COUNT IS THE WRONG NUMBER.
At a compaction the seat genuinely loses its premises and the injector
re-fires them ON PURPOSE -- `inject._ledger.forget_session` drops the
fire-record file at that boundary. Repeats counted across boundaries
therefore score the design working as intended as if it were waste, and the
error is large: a session spanning twenty boundaries reads near-total repeat
while its windows are individually clean.

WHAT IT COSTS TO RUN, AND WHY IT IS NOT A PER-TURN CHECK. The default census
reads BACKWARDS from the end of the transcript and stops at the first
compaction boundary, so it pays for one context window and not for the
session -- single-digit MB against a file that reaches hundreds. That is
cheap enough for a health report and far too expensive for a hook, so this
module is never wired to the per-turn loop; `helm doctor` and the CLI verb
are its readers. An auditor that becomes the cost it audits has refuted
itself.
"""
import json
import os
import re

# LEVELS: doctor's own three, so a finding crosses that boundary unchanged.
OK, WARN, FAIL = "OK", "WARN", "FAIL"

# THE BUDGET LIVES HERE AND THE SUITE IMPORTS IT BACK, so there is one budget
# with two readers rather than a constant in the check and a literal in a test
# that can drift apart without either one going red.
#
# THE NUMBERS ARE A POLICY, NOT A DERIVATION, and saying so is the point. What
# is measured is the population: across the complete windows of a heavy seat
# the share sits in single digits to the mid teens. WARN is set above every
# window such a seat has actually produced, so a breach means something
# CHANGED rather than that the fleet is busy; FAIL is set where roughly a
# third of the window was put there by hooks rather than by work. Neither
# number says what those bytes are WORTH or what they COST -- this module
# counts bytes, and injected bytes are cache-read on most turns, so a
# threshold phrased as billing would price something nobody has measured.
# Raising either without re-measuring the
# population turns the budget into a number that only ever agrees with
# whatever the fleet is currently doing.
WARN_SHARE = 0.20
FAIL_SHARE = 0.30

# REPEAT IS A SECOND AXIS AND IT IS NOT THE SHARE. A small share made entirely
# of re-deliveries and a large share of first-deliveries are different worlds
# that one number cannot separate; WHY either holds is a question for
# `inject._whisper._cooled` and its fire records, never for this census, which
# sees only that an id arrived twice inside one window. Collapsing the two
# axes would hide whichever is not
# currently the larger one.
WARN_REPEAT = 0.35
FAIL_REPEAT = 0.60

# How far back the bounded census will look for a boundary before giving up.
# A cap that is too small does not return a wrong answer -- it returns
# INCOMPLETE, which is a different thing and is reported as such.
WINDOW_CAP = 64 * 1024 * 1024
_CHUNK = 1024 * 1024

# The boundary marker, matched on raw bytes before any parse so the scan can
# skip whole megabytes of records it will never need to decode.
_BOUNDARY = b'"compact_boundary"'

# A rendered store entry. DERIVED FROM THE RENDERER'S FORMAT STRINGS
# (`inject._entries._entry_line_full`), NEVER FROM SAMPLES OF ITS OUTPUT --
# reading samples produced a pattern that matched the four tags that happened
# to be in view and silently dropped every PRIOR, an eighteen percent
# undercount that no arm could see because the arms were built from the same
# samples. A prior carries its CONFIDENCE between tag and slug (`PRIOR 0.90
# some-slug:`), and any entry may carry a `[provisional]` prefix. The slug is
# BARE here rather than typed.
#
# CAP IS ABSENT ON PURPOSE: it renders as `CAP <slug>` with no colon, so a
# colon-terminated pattern can never match it, and naming it in the
# alternation would claim a coverage this does not have.
#
# Deliberately unanchored: the first entry of a block follows the harness's
# `hook success: ` prefix on the same line, so anchoring at a line start
# drops one entry per delivery -- about a quarter of them, a shortfall that
# looks exactly like a clean measurement.
_ENTRY = re.compile(
    r"(PREMISE|PRIOR\s+[\d.]+|MOVE|REF|TERM|WHO)\s+([A-Za-z0-9][\w.-]*):")

_HOOK = "hook_"


def hook_event(rec):
    """-> the harness's own hookEvent for a hook record, else None.

    THE TYPE IS THE DISCRIMINATOR. `attachment.type` is written by the
    harness when it files the record, so it separates a hook's output from a
    turn that merely talks about hooks -- which no examination of the text
    can do.
    """
    if not isinstance(rec, dict) or rec.get("type") != "attachment":
        return None
    att = rec.get("attachment")
    if not isinstance(att, dict):
        return None
    if not str(att.get("type") or "").startswith(_HOOK):
        return None
    return str(att.get("hookEvent") or "?")


def rendered_text(rec):
    """The text the harness recorded as placed in the context window, or "".

    ABSENT IS NOT AUTOMATICALLY ZERO. A hook that printed nothing injects
    nothing and files no rendered block -- but so does a transcript written
    by a harness build that had no such field at all, and the two are the
    same observable here. `injected` decides between them; this function
    only reports what the field says.
    """
    blocks = rec.get("rendered")
    if not isinstance(blocks, list):
        return ""
    return "".join(b.get("content", "") for b in blocks
                   if isinstance(b, dict) and isinstance(b.get("content"), str))


def injected(rec, att):
    """-> (text, exact). What this hook record put in the context window.

    TWO TRANSCRIPT GRAMMARS ARE LIVE ON ONE FLEET AND ONLY ONE OF THEM
    RECORDS THE ANSWER. Newer harness builds file a `rendered` block, which
    is the harness's own statement of what it placed in context and is
    therefore exact. Older ones file none, and a reader that treats a missing
    `rendered` as zero reports a silent, confident 0.0% for every seat on the
    older build -- the most dangerous possible output for an instrument whose
    entire job is noticing a number that grew.

    So the fallback is the hook's `stdout`, which is what the harness wraps
    into the injected reminder: measured against records carrying BOTH
    fields, `rendered` is `stdout` plus about sixty bytes of wrapper, and in
    aggregate runs eight percent larger. That is an ESTIMATE and it is
    returned flagged as one, never silently mixed into an exact total.
    """
    text = rendered_text(rec)
    if text:
        return text, True
    out = att.get("stdout")
    if isinstance(out, str) and out:
        return _from_stdout(out), False
    return "", True


def _from_stdout(out):
    """What a hook's stdout actually injects, for transcripts with no
    `rendered` block.

    A HOOK'S STDOUT IS NOT ITS INJECTION WHEN IT IS A PROTOCOL ENVELOPE.
    Outside UserPromptSubmit a hook answers in JSON, and the harness injects
    only the envelope's `additionalContext` or `systemMessage` -- a bare `{}`
    is a hook declining to say anything, which is most of them. Charging the
    envelope's own punctuation to the context budget measures the protocol
    rather than the payload, and it is not a small error: on an old-format
    seat transcript the naive reading was twice the true one, all of the
    excess on hooks that injected nothing.

    Plain text is taken whole -- that is the UserPromptSubmit form, where the
    harness wraps stdout into the reminder verbatim.
    """
    head = out.lstrip()[:1]
    if head not in ("{", "["):
        return out
    try:
        doc = json.loads(out)
    except ValueError:
        # NOT PARSEABLE IS NOT EMPTY. A truncated or non-JSON body that merely
        # starts with a brace is still text the harness may have injected, so
        # it is counted rather than silently dropped to zero.
        return out
    if not isinstance(doc, dict):
        return out
    spec = doc.get("hookSpecificOutput")
    parts = [spec.get("additionalContext") if isinstance(spec, dict) else None,
             doc.get("systemMessage")]
    return "".join(p for p in parts if isinstance(p, str))


def _payload_len(att):
    """Bytes of hook payload the RECORD carries, whatever the harness did with
    it -- the control's numerator, never the context cost."""
    n = 0
    for key in ("content", "stdout"):
        v = att.get(key)
        if isinstance(v, str):
            n += len(v)
    return n


def utf8_len(text):
    """-> the UTF-8 BYTE length of `text`, which is what a field named bytes owes.

    len() on a str counts CHARACTERS, and for the fields this module publishes
    that is the wrong unit with the right magnitude: identical on ASCII, and
    silently short on anything else. The live case is not exotic — `_entry_line`
    appends U+2026 on truncation, one character and three bytes, so every
    truncated entry was undercounted by two.

    THE SHARE WAS NEVER WRONG and this does not fix it: `share` divides
    hook_bytes by context_bytes and both were measured with len(), so
    characters-over-characters is the same ratio as bytes-over-bytes. What was
    wrong is every ABSOLUTE figure and the name on it, and the comparison
    against `_utf8_sample` (helm/inject/_whisper.py), which prices the same
    injected stdout in real UTF-8 bytes and stamps `"encoding": "utf-8"` on
    its record. Encoding on both sides keeps the share sound and makes the
    label true. task/2691.
    """
    return len(text.encode("utf-8", "replace")) if isinstance(text, str) else 0


def context_bytes(rec):
    """This record's contribution to the model's context, in bytes.

    A PROXY IN ONE UNIT, WHICH IS WHAT MAKES THE SHARE MEAN ANYTHING. Bytes
    are not tokens, so no line here may be read as a token count; what the
    share needs is that numerator and denominator are measured the SAME way,
    and mixing a byte numerator with a token denominator is the arithmetic
    that makes a true reading answer a question nobody asked. `census`
    carries the harness's own token figure alongside, as an independent
    check on this proxy rather than as a second opinion inside it.
    """
    text = rendered_text(rec)
    if text:
        return utf8_len(text)
    kind = rec.get("type")
    if kind in ("user", "assistant"):
        try:
            return utf8_len(json.dumps(rec.get("message", ""),
                                       ensure_ascii=False))
        except (TypeError, ValueError):
            return 0
    if kind == "system":
        content = rec.get("content")
        return utf8_len(content)
    return 0


def _usage_tokens(rec):
    """The harness's OWN context size at an assistant turn, in tokens.

    Read, never derived, and used only to cross-check the byte proxy. A zero
    is not a measurement -- an assistant record without usage says nothing
    about the context, so it is reported as absent rather than as empty.
    """
    if rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens"):
        v = usage.get(key)
        if isinstance(v, int):
            total += v
    return total or None


def window_lines(path, cap=WINDOW_CAP, chunk=_CHUNK):
    """-> (lines, complete, read_bytes, why). The CURRENT compaction window.

    Read backwards in chunks and stop at the first boundary, so the cost is
    one context window rather than one session.

    THREE OUTCOMES, NOT TWO. A boundary found is a complete window. Reaching
    the start of the file is ALSO complete -- that session never compacted,
    and its whole transcript is its window. Exhausting the cap without either
    is INCOMPLETE: the records read are a suffix of a window whose start was
    never seen, and a share over a suffix is not the window's share. Rendering
    those three as two is how a bounded reader comes to publish a confident
    number about a region it could not finish reading.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return [], False, 0, "cannot size %s (%s)" % (path, type(exc).__name__)
    buf, pos, found = b"", size, False
    try:
        with open(path, "rb") as fh:
            while pos > 0:
                # THE CAP BOUNDS THE READ, NOT THE ITERATION THAT NOTICES IT.
                # Testing `(size - pos) < cap` and then reading a whole chunk
                # overshoots by up to a chunk, and for any cap below the chunk
                # size it reads the entire file while still reporting a
                # complete window -- a cost bound that does not bind and an
                # honest-looking verdict over a region the caller asked this
                # function not to read.
                budget = cap - (size - pos)
                if budget <= 0:
                    break
                step = min(chunk, pos, budget)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                if _BOUNDARY in buf:
                    found = True
                    break
    except OSError as exc:
        return [], False, 0, "cannot read %s (%s)" % (path, type(exc).__name__)
    lines = buf.split(b"\n")
    if pos > 0:
        # THE FIRST LINE IS HALF A RECORD. The seek landed mid-line, and a
        # prefix of a JSON object either raises or -- worse -- parses as
        # something that means something else.
        lines = lines[1:]
    if found:
        for i in range(len(lines) - 1, -1, -1):
            if _BOUNDARY in lines[i]:
                lines = lines[i + 1:]
                break
    complete = found or pos <= 0
    why = "" if complete else (
        "read %d bytes back without reaching a compaction boundary or the "
        "start of the file, so this is a SUFFIX of a window and its share is "
        "UNKNOWN" % (size - pos))
    return lines, complete, size - pos, why


def all_lines(path):
    """-> (lines, complete, read_bytes, why). The WHOLE session.

    The expensive census, kept beside the cheap one so a caller choosing it is
    choosing it. Its per-window numbers are the ones to read; a total over a
    session that compacted is a sum across windows that never coexisted.
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        return [], False, 0, "cannot read %s (%s)" % (path, type(exc).__name__)
    return blob.split(b"\n"), True, len(blob), ""


def _blank_window():
    return {"hook_bytes": 0, "context_bytes": 0, "by_event": {}, "by_hook": {},
            "injections": 0, "ids": {}, "tokens": None, "estimated_bytes": 0,
            "unrendered": 0, "unrendered_bytes": 0, "records": 0}


def _fold(window, rec):
    window["records"] += 1
    event = hook_event(rec)
    if event is None:
        window["context_bytes"] += context_bytes(rec)
        tokens = _usage_tokens(rec)
        if tokens is not None:
            window["tokens"] = tokens
        return
    att = rec.get("attachment")
    att = att if isinstance(att, dict) else {}
    text, exact = injected(rec, att)
    size = utf8_len(text)
    window["hook_bytes"] += size
    window["context_bytes"] += size
    window["by_event"][event] = window["by_event"].get(event, 0) + size
    if not exact:
        window["estimated_bytes"] += size
    if size:
        # WHICH HOOK, NOT ONLY WHICH EVENT. "PostToolUse is 60%" names no
        # actor and licenses no fix; the hook's own name does, and the
        # difference between two seats on the same event is exactly the thing
        # an event-level total cannot show.
        name = str(att.get("hookName") or event)
        window["by_hook"][name] = window["by_hook"].get(name, 0) + size
    else:
        # THE CONTROL ON THIS MODULE'S CORE ASSUMPTION. A hook record holding
        # payload text that neither field accounts for is either output the
        # harness discarded -- costing nothing, correctly counted as zero --
        # or context this instrument cannot see. It does not know which, so it
        # counts the population instead of choosing an interpretation.
        payload = _payload_len(att)
        if payload:
            window["unrendered"] += 1
            window["unrendered_bytes"] += payload
        return
    for _tag, ident in _ENTRY.findall(text):
        window["injections"] += 1
        window["ids"][ident] = window["ids"].get(ident, 0) + 1


def census(path, whole=False, cap=WINDOW_CAP):
    """-> a census dict, or {"why": <sentence>} when it could not be taken.

    `whole` reads the entire session and segments it at every boundary;
    the default reads the current window only.
    """
    read = all_lines(path) if whole else window_lines(path, cap=cap)
    lines, complete, read_bytes, why = read
    if why and not lines:
        return {"why": why}
    windows = [_blank_window()]
    unparsed = 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(rec, dict):
            unparsed += 1
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            windows.append(_blank_window())
            continue
        _fold(windows[-1], rec)
    return {"path": path, "windows": windows, "complete": complete,
            "read_bytes": read_bytes, "unparsed": unparsed,
            "whole": bool(whole), "why": why}


def share(window):
    """-> the hook share of this window's context, or None when UNKNOWN.

    A WINDOW WITH NO CONTEXT HAS NO SHARE. Zero over zero is not a clean
    bill; it is a window the census could not see into, and returning 0.0
    would let an empty reading render exactly like a healthy one.
    """
    total = window["context_bytes"]
    if total <= 0:
        return None
    return float(window["hook_bytes"]) / total


def repeat_rate(window):
    """-> the share of this window's injections that the seat already had,
    or None when nothing was injected.

    Within ONE window only. A re-delivery after a compaction is the injector
    restoring what the seat provably lost, so counting it as a repeat scores
    the cure as the disease.
    """
    total = window["injections"]
    if total <= 0:
        return None
    return float(total - len(window["ids"])) / total


def worst(cen, by=share):
    """-> the window a reader should be shown for ONE axis: the highest
    reading of `by`, and when no window has one, the last window.

    NOT THE MEAN. A mean over windows lets a quiet stretch pay for a breach,
    and the question a budget asks is whether ANY window went over.

    AND ONE AXIS PER CALL, WHICH IS WHY `by` IS A PARAMETER. The window with
    the worst share and the window with the worst repeat are routinely
    different windows, so reporting both from whichever one lost on share
    attaches a true reading to a claim it does not support: the repeat
    verdict would be silent about the window that actually breached it.
    """
    rated = [(by(w), w) for w in cen["windows"]]
    rated = [(v, w) for v, w in rated if v is not None]
    if not rated:
        return cen["windows"][-1] if cen["windows"] else _blank_window()
    return max(rated, key=lambda vw: vw[0])[1]


def findings(cen):
    """-> [(level, sentence)]. THE BUDGET'S ONE VERDICT, for every reader.

    The share is rendered on EVERY run and not only on a breach. A number
    that appears only when it is already bad teaches nobody what normal looks
    like, and the condition this module exists to prevent is precisely a
    quantity growing for months with no surface carrying it.
    """
    if cen.get("why") and not cen.get("windows"):
        return [(WARN, "injection budget: %s" % cen["why"])]
    win = worst(cen, share)
    pct = share(win)
    if pct is None:
        return [(WARN, "injection budget: read %d record(s) and measured NO "
                       "context bytes — UNMEASURED, not clean"
                 % win["records"])]
    # NAMED BY HOOK, because a fix needs an actor. The event roll-up stays as
    # the fallback for a record that carried no hook name.
    hooks = sorted(win["by_hook"].items(), key=lambda kv: -kv[1])
    if not hooks:
        hooks = sorted(win["by_event"].items(), key=lambda kv: -kv[1])
    top = ", ".join("%s %.0fKB" % (e, b / 1024.0) for e, b in hooks[:3] if b)
    scope = "session, worst of %d window(s)" % len(cen["windows"]) \
        if cen["whole"] else "current window"
    # THE QUALIFIER GOES IN THE HEADLINE OR IT IS NOT A QUALIFIER. A confident
    # "hooks are 0.0%" with a caveat two lines below is read as 0.0%, and the
    # clause the reader acts on is the one carrying the number.
    est = win["estimated_bytes"]
    if not cen["complete"]:
        # A SUFFIX IS NOT A WINDOW, AND THE HEADLINE IS WHERE THAT BELONGS.
        # The share of a fragment whose start was never reached is not the
        # window's share, and a reader acts on the clause carrying the number
        # rather than on a caveat printed below it.
        mark = "UNKNOWN — measured over a SUFFIX of a window whose start was " \
               "never reached; over that fragment alone, "
    elif est >= win["hook_bytes"] and est:
        mark = "ESTIMATED from hook stdout, this transcript records no " \
               "rendered block: "
    elif est:
        mark = "%.0fKB of it estimated from hook stdout: " % (est / 1024.0)
    else:
        mark = ""
    head = ("injection budget: hooks are %s%.1f%% of this seat's context (%s; "
            "%.0fKB of %.0fKB; %s)"
            % (mark, 100.0 * pct, scope, win["hook_bytes"] / 1024.0,
               win["context_bytes"] / 1024.0, top or "no hook bytes"))
    out = []
    if not cen["complete"]:
        # An incomplete read may not return a budget verdict at all: "under
        # budget" about a fragment is a claim the census did not earn.
        out.append((WARN, head))
    elif pct >= FAIL_SHARE:
        # WHAT THE BYTES ARE WORTH AND WHAT THEY COST ARE BOTH UNMEASURED
        # HERE. An earlier cut of this sentence called them "boilerplate the
        # seat is billed for on every turn" -- two claims this census cannot
        # make. It counts bytes, not value, so calling them boilerplate
        # asserts that the injected material is useless. And injected bytes
        # are cache-read on most turns, so "billed on every turn" names a
        # price nobody has measured. The ratio is the finding; a reader who
        # wants the cost has to go and measure it.
        # AND THE OVERAGE IS STATED AS A MARGIN, NOT AS A RATIO. An earlier
        # cut said "roughly one byte in N", which is readable at a small share
        # and degenerates at a large one -- at 90% it renders as "one byte in
        # 1", an arithmetic artifact a reader has to decode rather than a
        # fact. The margin is monotone across the whole range.
        out.append((FAIL, "%s — OVER the %.0f%% budget by %.1f points; these "
                          "bytes were put in the window by a hook rather than "
                          "by work"
                    % (head, 100.0 * FAIL_SHARE,
                       100.0 * (pct - FAIL_SHARE))))
    elif pct >= WARN_SHARE:
        out.append((WARN, "%s — past the %.0f%% warning band (fail at %.0f%%)"
                    % (head, 100.0 * WARN_SHARE, 100.0 * FAIL_SHARE)))
    else:
        out.append((OK, "%s — under the %.0f%% budget"
                    % (head, 100.0 * WARN_SHARE)))
    # THE REPEAT AXIS PICKS ITS OWN WINDOW. Reporting it from the share's
    # window would leave the window that actually breached repeat unnamed.
    rwin = worst(cen, repeat_rate)
    rep = repeat_rate(rwin)
    if rep is None:
        out.append((OK, "injection repeat: no store entries injected in this "
                        "window — nothing to repeat"))
    else:
        # THE RATE IS THE FINDING AND THE CAUSE IS NOT IN IT. An earlier cut
        # said a breach here meant "the per-session cooldown is NOT holding".
        # This census cannot see that. A repeat within one window is equally
        # consistent with a suppression that failed AND with the escape
        # working exactly as designed -- `inject._whisper._cooled` re-fires an
        # entry whose score clears COOLDOWN_ESCAPE precisely because a genuine
        # relevance spike is new information. Two different worlds, one
        # observable, and naming either one would be an accusation the number
        # does not carry. So the sentence reports the rate, says what a repeat
        # MEANS HERE, and points at the instrument to read.
        rhead = ("injection repeat: %.0f%% of %d entry deliveries (%d "
                 "distinct) repeated an id already delivered since the last "
                 "compaction boundary"
                 % (100.0 * rep, rwin["injections"], len(rwin["ids"])))
        level = FAIL if rep >= FAIL_REPEAT else (
            WARN if rep >= WARN_REPEAT else OK)
        if level == OK:
            out.append((OK, "%s — under the %.0f%% budget"
                        % (rhead, 100.0 * WARN_REPEAT)))
        else:
            out.append((level, "%s — past the %.0f%% budget. A repeat is a "
                               "re-delivery WITHIN one context window; it is "
                               "consistent BOTH with suppression failing and "
                               "with the score escape firing as designed, and "
                               "this census cannot tell which. Read "
                               "inject._whisper._cooled and its fire records "
                               "before changing anything"
                        % (rhead, 100.0 * WARN_REPEAT)))
    if not cen["complete"]:
        out.append((WARN, "injection budget: %s" % cen["why"]))
    # THE BLIND SPOT IS REPORTED AS A BOUND, AND ESCALATES ONLY WHEN IT COULD
    # MOVE THE VERDICT. A control that fires on every healthy run is noise a
    # reader learns to skip, and a control that is silent is not a control.
    # So the unseen payload is always converted into the most it could add to
    # the share, and only a bound big enough to carry the share across a
    # budget line becomes a warning of its own.
    if win["unrendered"]:
        bound = float(win["unrendered_bytes"]) / win["context_bytes"]
        sentence = ("injection budget: %d hook record(s) carried %.0fKB with "
                    "no rendered block; this census counts them as zero and "
                    "CANNOT TELL whether the harness discarded them or placed "
                    "them somewhere it does not record, so the share above is "
                    "understated by at most %.1f points"
                    % (win["unrendered"], win["unrendered_bytes"] / 1024.0,
                       100.0 * bound))
        crossed = pct < WARN_SHARE <= pct + bound
        out.append((WARN if crossed else OK, sentence))
    if cen["unparsed"]:
        out.append((WARN, "injection budget: %d transcript line(s) did not "
                          "parse and are absent from every number above"
                    % cen["unparsed"]))
    return out


def live_path(session=None, root=None):
    """-> (path, why). This seat's own transcript.

    BOUND BY SESSION ID, NEVER BY MTIME. On a box running twenty seats the
    newest transcript belongs to whichever seat wrote last, which is a
    different seat's context measured under this seat's name.
    """
    from . import home, turnresponse
    sid = session or home.session_id()
    if not sid:
        return None, ("no session id in the environment — this seat cannot "
                      "identify its own transcript")
    path, err = turnresponse.transcript_path(sid, root=root)
    if err:
        return None, err
    if not path:
        return None, "no transcript on disk for session %s" % sid
    return path, ""


# How recently a transcript must have been written to count as a live seat.
FLEET_DAYS = 3

# THE SMALLEST CONTEXT A SHARE MAY BE A VERDICT ABOUT. A seat's SessionStart
# injection is a fixed cost paid once and amortised over the whole window, so
# a session four kilobytes old reads as 95% hooks and is not a finding about
# anything -- it is a session that has not started working yet. Ranked as a
# breach it takes the headline away from the seat that is genuinely over, which
# is the one failure this instrument cannot afford. Below the floor a share is
# reported and never ranked, never breached.
MIN_CONTEXT = 256 * 1024


def fleet_paths(days=FLEET_DAYS, roots=None, now=None):
    """-> [(mtime, path)] for every seat transcript written recently, newest
    first. The roots are the seat config homes plus the owner's own."""
    import glob as _glob
    import time as _time
    from . import home
    if roots is None:
        base = home.helm_home()
        roots = [os.path.join(base, "_global", "seats", "*", "claude",
                              "projects", "*"),
                 os.path.expanduser("~/.claude/projects/*")]
    cut = (now if now is not None else _time.time()) - days * 86400
    seen, out = set(), []
    for root in roots:
        for path in _glob.glob(os.path.join(root, "*.jsonl")):
            try:
                st = os.stat(path)
            except OSError:
                continue
            real = (st.st_dev, st.st_ino)
            if real in seen or st.st_mtime < cut:
                continue
            seen.add(real)
            out.append((st.st_mtime, path))
    return sorted(out, reverse=True)


def _seat_of(path):
    """The seat a transcript belongs to, from its config home."""
    parts = os.path.abspath(path).split(os.sep)
    if "seats" in parts:
        i = parts.index("seats")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "owner"


def fleet(days=FLEET_DAYS, cap=WINDOW_CAP, paths=None):
    """-> rows, worst share first.

    NEVER A MEAN, AND THE WORST SEAT IS THE HEADLINE. Averaging the fleet is
    how an outlier disappears: a seat at three times the budget and four quiet
    seats average to something comfortable, and the outlier is the entire
    reason to look. A fleet number that can be satisfied by its own
    population's calm is not a budget.
    """
    rows = []
    for mtime, path in (paths if paths is not None else fleet_paths(days)):
        cen = census(path, cap=cap)
        win = worst(cen, share)
        sid = os.path.basename(path).split(".")[0][:8]
        rows.append({"seat": "%s/%s" % (_seat_of(path), sid), "path": path,
                     "mtime": mtime,
                     "share": share(win), "repeat": repeat_rate(win),
                     "hook_bytes": win["hook_bytes"],
                     "context_bytes": win["context_bytes"],
                     "ranked": win["context_bytes"] >= MIN_CONTEXT,
                     "estimated": bool(win["estimated_bytes"]),
                     "top": sorted(win["by_hook"].items(),
                                   key=lambda kv: -kv[1])[:2],
                     "complete": cen["complete"]})
    def order(row):
        # Group 0: a real reading, ranked by share. Group 1: UNKNOWN, which
        # sits beside a breach rather than at the bottom -- a seat whose share
        # could not be measured is not a seat that is fine. Group 2: below the
        # floor, which is not a reading yet.
        if row["share"] is None:
            return (1, 0.0)
        if not row["ranked"]:
            return (2, -row["share"])
        return (0, -row["share"])
    return sorted(rows, key=order)


def fleet_findings(rows, condensed=False):
    """-> [(level, sentence)]. The worst seat, then every other breach.

    `condensed` is the health-report form: the headline, every seat actually
    over the budget, and a COUNT of the quiet ones. The count is not
    decoration -- a rung that printed only breaches would render an unwired
    census and a clean fleet identically, and the population is what separates
    them.
    """
    if not rows:
        return [(WARN, "injection budget: no seat transcript written in the "
                       "last %d day(s) — UNMEASURED, not clean" % FLEET_DAYS)]
    out = []
    for row in rows:
        pct = row["share"]
        if pct is None:
            out.append((WARN, "injection budget: %s — share UNKNOWN (%s)"
                        % (row["seat"], "incomplete window" if not row["complete"]
                           else "no context bytes read")))
            continue
        top = ", ".join("%s %.0fKB" % (n, b / 1024.0) for n, b in row["top"])
        body = ("injection budget: %-20s %5.1f%%%s of context "
                "(%.0fKB of %.0fKB%s)"
                % (row["seat"], 100.0 * pct, "~" if row["estimated"] else "",
                   row["hook_bytes"] / 1024.0, row["context_bytes"] / 1024.0,
                   "; " + top if top else ""))
        if not row["ranked"]:
            if not condensed:
                out.append((OK, "%s — context under %dKB, too small for the "
                                "share to mean anything yet; NOT ranked"
                            % (body, MIN_CONTEXT / 1024)))
            continue
        level = FAIL if pct >= FAIL_SHARE else (
            WARN if pct >= WARN_SHARE else OK)
        if condensed and level == OK:
            continue
        out.append((level, body + ("" if level == OK else "  <-- OVER")))
    rated = [r for r in rows if r["share"] is not None and r["ranked"]]
    if not rated:
        out.insert(0, (WARN, "injection budget: no seat carried enough context "
                             "to rank — share UNMEASURED, not clean"))
        return out
    head = max(rated, key=lambda r: r["share"])
    quiet = sum(1 for r in rated if r["share"] < WARN_SHARE)
    out.insert(0, (FAIL if head["share"] >= FAIL_SHARE else (
        WARN if head["share"] >= WARN_SHARE else OK),
        "injection budget: WORST SEAT is %s at %.1f%%%s — %d of %d ranked "
        "seat(s) under the %.0f%% budget. Read the worst, never the mean: an "
        "average over a quiet fleet hides exactly the outlier this exists to "
        "find"
        % (head["seat"], 100.0 * head["share"],
           "~" if head["estimated"] else "", quiet, len(rated),
           100.0 * WARN_SHARE)))
    return out


def _report(cen, verbose=False):
    lines = []
    for level, msg in findings(cen):
        lines.append("  %-4s %s" % (level, msg))
    if verbose:
        for i, w in enumerate(cen["windows"]):
            pct, rep = share(w), repeat_rate(w)
            lines.append(
                "  w%-3d share=%s repeat=%s hook=%.0fKB ctx=%.0fKB inj=%d "
                "distinct=%d tokens=%s"
                % (i, "%.1f%%" % (100.0 * pct) if pct is not None else "UNKNOWN",
                   "%.0f%%" % (100.0 * rep) if rep is not None else "n/a",
                   w["hook_bytes"] / 1024.0, w["context_bytes"] / 1024.0,
                   w["injections"], len(w["ids"]),
                   w["tokens"] if w["tokens"] is not None else "UNKNOWN"))
    return lines


def cmd_injectbudget(args):
    """injectbudget [--fleet] [--whole] [--verbose] [--json] [--session ID]
    [--path P] — what share of a seat's context its own hooks injected."""
    from .cli import guard_tail

    rc = guard_tail("helm injectbudget", args,
                    flags=("--fleet", "--whole", "--verbose", "--json"),
                    valued=("--session", "--path"),
                    usage=cmd_injectbudget.__doc__)
    if rc is not None:
        return rc
    if "--fleet" in args:
        rows = fleet()
        found = fleet_findings(rows)
        if "--json" in args:
            print(json.dumps(rows, indent=2, sort_keys=True, default=str))
            return 0
        for level, msg in found:
            print("  %-4s %s" % (level, msg))
        return 1 if any(l == FAIL for l, _ in found) else 0
    path = _value(args, "--path")
    if not path:
        path, why = live_path(_value(args, "--session"))
        if not path:
            print("helm injectbudget: %s" % why)
            return 1
    cen = census(path, whole="--whole" in args)
    if "--json" in args:
        print(json.dumps(_jsonable(cen), indent=2, sort_keys=True))
        return 0
    print("helm injectbudget: %s" % path)
    for line in _report(cen, verbose="--verbose" in args):
        print(line)
    return 1 if any(l == FAIL for l, _ in findings(cen)) else 0


def _jsonable(cen):
    """The census with each window's derived figures resolved, so a consumer
    reads the same share this module's own verdict used rather than
    recomputing it from the parts and diverging."""
    out = dict(cen)
    out["windows"] = [dict(w, share=share(w), repeat=repeat_rate(w),
                           distinct=len(w["ids"]),
                           ids=dict(sorted(w["ids"].items(),
                                           key=lambda kv: -kv[1])[:10]))
                      for w in cen.get("windows", [])]
    return out


def _value(args, flag):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None
