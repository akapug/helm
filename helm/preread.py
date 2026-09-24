#!/usr/bin/env python3
"""helm preread — a council of cheap readers reads a review row's diff, a
DIFFERENT model judges their drafts, and what survives a citation check is
written down as a PRE-READ.

NOT `helm chat council`. `helm/council.py` is the embargoed N-of-M quorum
verb: its members answer under embargo and the quorum's answer carries
authority. A pre-read is the opposite on both axes. It is OPEN — the kept
findings, the judge's own refusals and the citation check all land in one
readable file — and it is NON-AUTHORITATIVE: what it produces is reading
material a strong reviewer or the integrator checks line by line.

A PRE-READ NEVER MINTS A VERDICT, and it never writes the dispatch ledger.
Its whole output is one file under the helm home plus one line in its own
ledger, and `tests.test_preread` pins the dispatch ledger's line count across
a run, because a reading that could close a row would be an approval by a
class of model the fleet's policy forbids from approving. Weak models review
only in councils, and a council is a pre-read.

WHY A COUNCIL RATHER THAN ONE CALL, measured over three real review rows whose
defects were already known: one call to a cheap model over a whole change
recovers none of them and degenerates into a repetition loop. The same model,
handed ONE FILE at a time with a checklist of the defect classes this
repository has actually been bitten by, and run under several seeds, raises the
true findings in its drafts. Scaffolding buys recall; it costs a false rate,
and both cures for that are here:

  * a JUDGE from a different model than the readers, which refuses the classes
    a weak reader over-reports (an except-pass whose comment says fail-open, a
    diagnostic that is meant to print its reason) and must write down what it
    dropped and why, so its output is auditable in one read;
  * a CITATION CHECK that keeps a finding only when the line it quotes really
    appears in the diff's added or context lines.

The judge is asked PER FILE and then once more to merge the judged lists. One
judge call over a whole change's drafts is the failure that costs the most: a
judge handed every file's drafts at once answers NONE FOUND in a few hundred
tokens and throws away true findings it never had room to read.

EVERY ENDPOINT, MODEL, HOST AND KEY LIVES IN THE CONFIG under the helm home,
never in this source — see `docs/preread-config.example.json`. Absent config is
a plain refusal naming the path, never a default endpoint. The judge's bearer
token is read from the file the config names by a reader that hands it to
exactly one request and to no surface: nothing here prints, logs, stores or
returns it through a rendering.

Import-safe, stdlib-only.
"""
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import home

CONFIG_NAME = "preread.json"
STORE = "prereads"
LEDGER = "ledger.jsonl"
EXAMPLE = "docs/preread-config.example.json"

# THE PER-FILE BOUND, named here because a reader's context window and the
# pre-read's wall time both scale with it and neither is visible to the caller.
# Roughly ten thousand tokens of diff per file: a local reader ingests prompt
# at a rate that makes this a handful of seconds, and a file past the bound is
# sent TRUNCATED with a line saying so rather than dropped in silence.
CAP_BYTES = 40000
TRUNK = "origin/main"
READER_MAX_TOKENS = 3000
# THE JUDGE'S BUDGET COVERS ITS THINKING. A reasoning judge spends output
# tokens on reasoning before the first answer token, and a 4000-token budget
# returned an EMPTY answer on every file whose drafts ran long (measured: the
# three largest files of a 21-file change, all not_judged). 12000 leaves room
# for the reasoning and the answer; the config can still lower it.
JUDGE_MAX_TOKENS = 12000
READER_TIMEOUT = 1800
JUDGE_TIMEOUT = 900

SYSTEM = ("You are a senior code reviewer. You are given a unified diff from a "
          "Python/JS repository. Report CONCRETE DEFECTS ONLY: each finding "
          "names the file, the line or function, what is wrong, and why it is "
          "wrong (a wrong value, a crash, a leaked secret, a broken invariant "
          "the code itself states). Do not report style, naming, or "
          "speculation. If you find nothing concrete, answer exactly NONE "
          "FOUND. Number the findings. Be brief.")

JUDGE_ASK = (
    "Below are independent draft reviews of ONE FILE of a code change, one per "
    "reader and seed, written by a weaker model. You are the stronger judge. "
    "Produce ONE numbered list of the findings that are CONCRETE DEFECTS on a "
    "specific quoted line: drop duplicates; drop anything the draft itself "
    "hedges; drop any finding where the draft's own quote of the surrounding "
    "comment or docstring explains the behaviour (a fail-open except, a "
    "diagnostic that prints the reason, a documented mask). For each survivor "
    "give the file, the exact quoted line inside backticks, how many drafts "
    "raised it, and your own confidence 0-100. Then write a paragraph "
    "beginning 'Dropped:' naming what you refused and why. Answer NONE FOUND "
    "if nothing survives.\n\n")

MERGE_ASK = (
    "Below are your own per-file judgements of one code change. Merge them "
    "into ONE numbered list in the same shape — file, the exact quoted line "
    "inside backticks, drafts-raised count, confidence 0-100 — dropping "
    "nothing that is already judged and adding nothing new. Keep a final "
    "paragraph beginning 'Dropped:' that carries every reason from the "
    "per-file judgements. Answer NONE FOUND if every judgement was empty.\n\n")

# A LINE LONG ENOUGH TO IDENTIFY ITSELF. A backticked quote shorter than this
# ("if", "=", "x") matches somewhere in almost any diff, so a citation check
# built on it would keep everything and certify nothing.
QUOTE_FLOOR = 8

# The sentence the file leads with, and the end of the header block `facts()`
# reads: everything below it is model prose and may contain anything.
NOT_A_VERDICT = "THIS IS NOT A VERDICT."

_HEADER = re.compile(r"(?m)^(?=diff --git )")
_PATHS = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)")
_FINDING = re.compile(r"\n(?=\s*\d+[.)]\s)")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s")
_QUOTED = re.compile(r"`([^`\n]{%d,})`" % QUOTE_FLOOR)
_KEYLIKE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
_FACT = re.compile(r"(?m)^- ([a-z_]+): (.*)$")


# ---------------------------------------------------------------- paths


def config_path():
    return os.path.join(home.global_dir(), CONFIG_NAME)


def store_dir():
    return os.path.join(home.global_dir(), STORE)


def ledger_path():
    return os.path.join(store_dir(), LEDGER)


def report_path(name):
    """The pre-read file for a dispatch id or a bare tip. One file per subject,
    overwritten by a later run: a pre-read is a projection of a tip, and two
    readings of the same tip are the same artifact."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "unnamed"))
    return os.path.join(store_dir(), safe + ".md")


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- config


def load_config(path=None):
    """(config, None) or (None, one plain sentence naming what is missing).

    A pre-read has no endpoint, model or key of its own by construction, so an
    absent config is not a degraded run — there is nothing to degrade to.
    """
    p = path or config_path()
    if not os.path.exists(p):
        return None, ("no pre-read config at %s — this verb carries no "
                      "endpoint, model or key of its own; copy %s there and "
                      "edit it" % (p, EXAMPLE))
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.loads(f.read())
    except (OSError, ValueError) as exc:
        return None, "the pre-read config at %s did not read: %s" % (p, exc)
    if not isinstance(cfg, dict):
        return None, "the pre-read config at %s is not an object" % p
    readers = cfg.get("readers")
    if not isinstance(readers, list) or not readers:
        return None, "the pre-read config at %s declares no readers" % p
    for reader in readers:
        if not isinstance(reader, dict) or not reader.get("url") \
                or not reader.get("model"):
            return None, ("a reader in the pre-read config at %s is missing "
                          "its url or its model" % p)
    judge = cfg.get("judge")
    if not isinstance(judge, dict) or not judge.get("url") \
            or not judge.get("model"):
        return None, ("the pre-read config at %s declares no judge url and "
                      "model — a council judged by one of its own readers "
                      "keeps the false findings it made" % p)
    return cfg, None


def checklist_text(cfg):
    """(checklist, None) or (None, why). REFUSING beats running without it: an
    unscaffolded council is the variant that was measured to recover nothing,
    and it would cost the same wall time to prove it again."""
    path = cfg.get("checklist") or os.path.join(
        _repo_root(), "docs", "preread-checklist.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return None, "the pre-read checklist at %s did not read: %s" % (
            path, exc)
    _head, sep, body = text.partition("\n---\n")
    return (body if sep else text).strip(), None


def judge_key(path):
    """The judge's bearer token from the file the config names, or None.

    ONE NARROW READ and ONE CONSUMER: the value reaches the judge request's
    Authorization header and nothing else in this module. No diagnostic here is
    built from it — every line this module writes is made of names and counts —
    and `tests.test_preread` runs a whole pre-read under a fake key and asserts
    that string appears in neither stream, neither the written file nor the
    ledger line.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                head, sep, tail = line.partition("api-key:")
                if not sep or head.strip().lstrip("-").strip():
                    continue          # not a key line: a comment, a nested key
                value = tail.strip().strip('"').strip("'")
                if value and _KEYLIKE.match(value):
                    return value
    except OSError:
        return None
    return None


# ---------------------------------------------------------------- the diff


def chunks(diff):
    """One chunk per `diff --git` header, in the diff's own order."""
    return [c for c in _HEADER.split(diff or "") if c.strip()]


def chunk_path(chunk):
    m = _PATHS.match(chunk or "")
    return m.group("b") if m else "?"


def is_test_path(path):
    base = os.path.basename(path or "")
    return path.startswith("tests/") or base.startswith("test_") \
        or base.endswith("_test.py")


def ordered(cs, cap_bytes=CAP_BYTES):
    """[(path, text, truncated_from_or_None)] — code and docs FIRST, tests
    LAST, each file capped.

    The order is the budget's: a pre-read can be cut short by a wall, a dead
    endpoint or a caller, and what a reader most wants read is the code the
    change makes, not the arms that pin it. Truncation is DECLARED per file
    rather than silent — a reader that saw two thirds of a file and a reader
    that saw all of it make different claims, and the file says which.
    """
    out = []
    for chunk in cs:
        path = chunk_path(chunk)
        raw = chunk.encode("utf-8", "replace")
        if len(raw) <= cap_bytes:
            out.append((path, chunk, None))
            continue
        # A FILE PAST THE CAP IS SPLIT BY HUNK, NOT CUT. The largest file in a
        # change is the one the change is about, and a cut at the cap threw
        # away exactly the hunks a reader most needed (measured: a 50 KB
        # module cut at 40 KB lost every defect the change carried). Each
        # part repeats the file header so a reader knows the file, and is
        # labelled part i of n so the judge names it. Only a single hunk that
        # is itself past the cap is cut, and that cut is declared.
        parts = _hunk_parts(chunk, cap_bytes)
        for i, part in enumerate(parts, 1):
            praw = part.encode("utf-8", "replace")
            label = "%s (part %d of %d)" % (path, i, len(parts))
            if len(praw) > cap_bytes:
                text = praw[:cap_bytes].decode("utf-8", "ignore") + (
                    "\n[TRUNCATED by helm preread at %d bytes of %d]\n"
                    % (cap_bytes, len(praw)))
                out.append((label, text, len(praw)))
            else:
                out.append((label, part, None))
    out.sort(key=lambda row: (1 if is_test_path(row[0]) else 0, row[0]))
    return out


_HUNK = re.compile(r"(?m)^(?=@@ )")


def _hunk_parts(chunk, cap_bytes):
    """Split one file's chunk into parts of whole hunks, each under the cap
    when the hunks allow it; every part carries the file header."""
    pieces = [x for x in _HUNK.split(chunk) if x]
    if len(pieces) < 2:
        return [chunk]
    header, hunks = pieces[0], pieces[1:]
    # A SINGLE HUNK PAST THE CAP IS THE NEW-FILE SHAPE (one `@@ -0,0` hunk
    # holding the whole module), and it is the file the change is about more
    # often than not. Such a hunk is split by LINES into pieces under the cap,
    # each part carrying the header, so no part is ever cut.
    hb = len(header.encode("utf-8", "replace"))
    expanded = []
    for h in hunks:
        if hb + len(h.encode("utf-8", "replace")) <= cap_bytes:
            expanded.append(h)
            continue
        lines = h.splitlines(True)
        piece, size = [], hb
        for line in lines:
            n = len(line.encode("utf-8", "replace"))
            if piece and size + n > cap_bytes:
                expanded.append("".join(piece))
                piece, size = [], hb
            piece.append(line)
            size += n
        if piece:
            expanded.append("".join(piece))
    hunks = expanded
    hb = len(header.encode("utf-8", "replace"))
    parts, cur, size = [], [], hb
    for h in hunks:
        n = len(h.encode("utf-8", "replace"))
        if cur and size + n > cap_bytes:
            parts.append(header + "".join(cur))
            cur, size = [], hb
        cur.append(h)
        size += n
    if cur:
        parts.append(header + "".join(cur))
    return parts


def diff_for(ref, repo, trunk=TRUNK, timeout=60):
    """(diff, None) or (None, why) — the tip against its merge base with trunk,
    THROUGH THE VCS SEAM. This module spawns nothing itself: `helm/vcs.py` owns
    every git process in this tree, and a second spawn door is the class the
    seam exists to close."""
    from . import vcs
    backend = vcs.backend(repo)
    rc, base, err = backend.text(repo, "merge-base", trunk, ref,
                                 timeout=timeout)
    if rc != 0 or not base:
        return None, ("no merge base for %s against %s in %s: %s"
                      % (ref, trunk, repo, err or "git exited %s" % rc))
    rc, out, err = backend.text(repo, "diff", "--no-color",
                                "%s..%s" % (base, ref), timeout=timeout)
    if rc != 0:
        return None, ("the diff %s..%s did not read in %s: %s"
                      % (base, ref, repo, err or "git exited %s" % rc))
    if not out.strip():
        return None, "%s..%s is an empty diff in %s" % (base, ref, repo)
    return out, None


# ---------------------------------------------------------------- the calls


def post(url, payload, headers=None, timeout=600):
    """THE ONE REMOTE DOOR, and the whole network surface of this module.

    Every reader and the judge go through it, so a test injects one fake and
    covers both. Every call carries max_tokens (built by its caller) and a
    timeout, because an unbounded call to a weak model is exactly how a
    pre-read becomes a hang.
    """
    head = {"content-type": "application/json"}
    head.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=head)
    # EXACTLY ONE REQUEST. The default opener follows a 3xx and copies the
    # authorization header onto the second request, wherever the first
    # response pointed it; the judge's key would then leave for a host the
    # config never named. A redirect is an HTTPError here and the leg fails.
    with _one_request_opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an answer this door refuses to follow, never a second request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            "redirect to %s refused: the pre-read door makes exactly one "
            "request and never re-sends its headers elsewhere" % newurl,
            headers, fp)


def _one_request_opener():
    return urllib.request.build_opener(_NoRedirect())


def _content(answer):
    """The assistant text, or "". A thinking model that spends its whole budget
    reasoning answers with content None and no error, which is why this cannot
    be a bare index: an empty answer is a fact to record, not a crash."""
    try:
        return (answer["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _cost(answer):
    try:
        value = (answer.get("usage") or {}).get("cost")
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def reader_payload(reader, checklist, path, text):
    body = {"model": reader["model"],
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user",
                 "content": ("Review this diff (ONE file, %s, of a larger "
                             "change).\n\n%s\n\n```diff\n%s\n```"
                             % (path, checklist, text))}],
            "max_tokens": int(reader.get("max_tokens") or READER_MAX_TOKENS),
            "temperature": float(reader.get("temperature", 0.3)),
            "repeat_penalty": float(reader.get("repeat_penalty", 1.1))}
    if not reader.get("think"):
        # A thinking model that exhausts max_tokens inside its reasoning
        # returns EMPTY content with no error, which reads downstream as a
        # clean file. Turning thinking off is what makes an empty answer mean
        # what it says.
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def judge_payload(judge, ask, drafts):
    return {"model": judge["model"],
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": ask + drafts}],
            "max_tokens": int(judge.get("max_tokens") or JUDGE_MAX_TOKENS),
            "temperature": float(judge.get("temperature", 0.1))}


# ---------------------------------------------------------------- grounding


def grounded(text, diff):
    """(kept, dropped, notes) — a finding survives only when a backticked
    quote of at least QUOTE_FLOOR characters appears in the diff's added or
    context lines.

    This is tool grounding without a tool. A model asked for the exact line
    will sometimes write a line it believes should be there, and a finding
    about a line that is not in the change is the one false class no amount of
    judging catches, because the judge only ever sees the drafts.

    ONLY A NUMBERED BLOCK IS A FINDING, and that third bucket is not tidiness.
    The judge is ASKED for a `Dropped:` paragraph and answers NONE FOUND when
    it refuses everything, so a two-bucket split counted the judge's own prose
    as a dropped finding: the first live run reported `dropped: 1` against an
    answer of exactly NONE FOUND. A count that moves when nothing was found
    means nothing to the reader it is printed for.
    """
    # HUNK BODIES ONLY. The `+++ b/<path>` header line starts with '+' too,
    # and a finding that backticks the file name (the shape the judge is
    # asked for) would match it and certify a line that is not in the diff.
    lines, paths, in_hunk = set(), set(), False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            paths.add(line[4:].strip())
            paths.add(line[4:].strip().split("/", 1)[-1])
            continue
        if in_hunk and line[:1] in "+ " and len(line.strip()) > QUOTE_FLOOR:
            lines.add(line[1:].strip())
    kept, dropped, notes = [], [], []
    for block in _FINDING.split(text or ""):
        block = block.strip()
        if not block:
            continue
        if not _NUMBERED.match(block):
            notes.append(block)
            continue
        quotes = [q.strip() for q in _QUOTED.findall(block)]
        # a quoted PATH names where, never what: it is not a cited line
        quotes = [q for q in quotes if q not in paths]
        hit = any(q in lines or any(q in line for line in lines)
                  for q in quotes)
        (kept if hit else dropped).append(block)
    return kept, dropped, notes


# ---------------------------------------------------------------- the run


def run(ref, repo, subject, cfg, transport=None, trunk=None, now=None):
    """Read one tip and return the whole result as plain data.

    A READER OR JUDGE FAILURE NEVER ABORTS THE PRE-READ. A dead endpoint, a
    timeout or a malformed answer is recorded against the file it happened on
    and the remaining files are still read and still judged; a file that no
    reader answered is listed by name under `not_read`, because a file nobody
    read and a file nobody found anything in are different facts and only one
    of them means the reader can stop looking.
    """
    transport = transport or post
    trunk = trunk or cfg.get("trunk") or TRUNK
    started = time.time()
    checklist, why = checklist_text(cfg)
    if why:
        return None, why
    diff, why = diff_for(ref, repo, trunk=trunk)
    if why:
        return None, why
    files = ordered(chunks(diff), int(cfg.get("cap_bytes") or CAP_BYTES))
    readers = cfg["readers"]
    judge = cfg["judge"]
    key = judge_key(judge.get("key_from") or "") if judge.get("key_from") \
        else None
    jobs = []
    for index, (path, text, _cut) in enumerate(files):
        for reader in readers:
            for seed in (reader.get("seeds") or [1]):
                jobs.append((index, reader, seed, path, text))
    workers = max(1, min(int(cfg.get("workers") or 6), 12))
    drafts = [[] for _ in files]
    failures = []

    def one(job):
        index, reader, seed, path, text = job
        payload = reader_payload(reader, checklist, path, text)
        payload["seed"] = seed
        answer = transport(reader["url"], payload, None,
                           int(reader.get("timeout") or READER_TIMEOUT))
        return index, reader, seed, _content(answer)

    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for job, future in [(j, ex.submit(one, j)) for j in jobs]:
                index, reader, seed = job[0], job[1], job[2]
                try:
                    index, reader, seed, text = future.result()
                except Exception as exc:              # noqa: BLE001
                    failures.append({"file": job[3], "leg": "reader",
                                     "model": reader.get("model"),
                                     "seed": seed,
                                     "why": "%s: %s" % (type(exc).__name__,
                                                        exc)})
                    continue
                if text:
                    drafts[index].append({"model": reader.get("model"),
                                          "seed": seed, "text": text})
                else:
                    failures.append({"file": job[3], "leg": "reader",
                                     "model": reader.get("model"),
                                     "seed": seed, "why": "empty answer"})

    headers = {"authorization": "Bearer " + key} if key else None
    judged, cost, not_read, not_judged = [], 0.0, [], []
    cost_known = False
    for index, (path, _text, _cut) in enumerate(files):
        if not drafts[index]:
            not_read.append(path)
            continue
        # THE JUDGE IS TOLD WHICH FILE IT IS JUDGING. The ask requires each
        # survivor to name its file, and the drafts carry only a model and a
        # seed — so without this line the judge was asked for a field nothing
        # had given it, and every finding's file was a guess from the diff
        # text. Found by a fixture that keyed on the path being in the judge's
        # payload and never fired.
        merged = "FILE: %s\n\n" % path + "\n\n".join(
            "[%s seed %s]\n%s" % (d["model"], d["seed"], d["text"])
            for d in drafts[index])
        try:
            answer = transport(judge["url"],
                               judge_payload(judge, JUDGE_ASK, merged),
                               headers,
                               int(judge.get("timeout") or JUDGE_TIMEOUT))
        except Exception as exc:                      # noqa: BLE001
            failures.append({"file": path, "leg": "judge",
                             "model": judge.get("model"),
                             "why": "%s: %s" % (type(exc).__name__, exc)})
            # READ BUT NOT JUDGED IS ITS OWN STATE. Filing it under `not_read`
            # would accuse the readers of a judge's failure, and the two want
            # opposite repairs: one says look at this file yourself, the other
            # says the drafts exist and the judge is down.
            not_judged.append(path)
            continue
        spent = _cost(answer)
        if spent is not None:
            cost, cost_known = cost + spent, True
        text = _content(answer)
        if text:
            judged.append("[%s]\n%s" % (path, text))
        else:
            # AN EMPTY JUDGE ANSWER IS NOT A JUDGED FILE. A thinking judge that
            # spends its whole budget reasoning returns no content; counting
            # that file as judged made the header say every file was judged
            # while two were missing from the per-file answers.
            failures.append({"file": path, "leg": "judge",
                             "model": judge.get("model"), "why": "empty answer"})
            not_judged.append(path)

    verdict_text = ""
    if len(judged) == 1:
        verdict_text = judged[0].split("\n", 1)[-1]
    elif judged:
        try:
            answer = transport(judge["url"],
                               judge_payload(judge, MERGE_ASK,
                                             "\n\n".join(judged)),
                               headers,
                               int(judge.get("timeout") or JUDGE_TIMEOUT))
            spent = _cost(answer)
            if spent is not None:
                cost, cost_known = cost + spent, True
            verdict_text = _content(answer)
        except Exception as exc:                      # noqa: BLE001
            failures.append({"file": "(merge)", "leg": "judge",
                             "model": judge.get("model"),
                             "why": "%s: %s" % (type(exc).__name__, exc)})
            verdict_text = "\n\n".join(judged)
    # THE MERGE IS A RENDERING, NEVER THE EVIDENCE. Measured on the first
    # live row: eight per-file judgements went into one small merge call and
    # it answered NONE FOUND, which would have thrown away every finding the
    # judge had already kept — the same capacity failure that forced judging
    # per file, one level up. So when the merge carries NO numbered finding
    # and the per-file judgements do, the per-file union IS the answer and the
    # file says the merge was empty. The per-file text is written down either
    # way, so nothing the judge said is ever lost to a summariser.
    kept, dropped, notes = grounded(verdict_text, diff)
    per_file_text = "\n\n".join(judged)
    merge_empty = False
    if not kept and not dropped and judged:
        from_files = grounded(per_file_text, diff)
        if from_files[0] or from_files[1]:
            merge_empty = True
            kept, dropped, notes = from_files
    return {"subject": subject, "ref": ref, "repo": repo, "trunk": trunk,
            "ts": now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "readers": ["%s x%d" % (r.get("model"),
                                    len(r.get("seeds") or [1]))
                        for r in readers],
            "seeds": sorted({s for r in readers
                             for s in (r.get("seeds") or [1])}),
            "judge": judge.get("model"),
            "files": [path for path, _t, _c in files],
            "truncated": [path for path, _t, cut in files if cut],
            "not_read": not_read, "not_judged": not_judged,
            "failures": failures,
            "drafts": sum(len(d) for d in drafts),
            "kept": kept, "dropped": dropped, "notes": notes,
            "judged_files": len(judged), "merge_empty": merge_empty,
            "judge_text": verdict_text, "per_file_text": per_file_text,
            "cost": round(cost, 6) if cost_known else None,
            "wall_s": round(time.time() - started, 1)}, None


# ---------------------------------------------------------------- output


def render(result):
    """The pre-read file. The `- name: value` header block is the ONE place
    these counts are written, and `facts()` reads them back out of it, so the
    line `helm dispatch triage` prints cannot disagree with the file it points
    at."""
    out = ["# pre-read: %s" % result["subject"], "",
           "- ref: %s" % result["ref"],
           "- repo: %s" % result["repo"],
           "- trunk: %s" % result["trunk"],
           "- ts: %s" % result["ts"],
           "- readers: %s" % ", ".join(result["readers"]),
           "- seeds: %s" % ", ".join(str(s) for s in result["seeds"]),
           "- judge: %s" % result["judge"],
           "- drafts: %d" % result["drafts"],
           "- kept: %d" % len(result["kept"]),
           "- dropped: %d" % len(result["dropped"]),
           "- cost: %s" % ("%.6f" % result["cost"]
                           if result["cost"] is not None
                           else "UNKNOWN (the judge reported no usage cost)"),
           "- wall_s: %s" % result["wall_s"],
           "- files: %d" % len(result["files"]),
           "- not_read: %s" % (", ".join(result["not_read"]) or "none"),
           "- not_judged: %s" % (", ".join(result["not_judged"]) or "none"),
           "- judged_files: %d" % result["judged_files"],
           "- merge_empty: %s" % ("yes" if result["merge_empty"] else "no"),
           "",
           NOT_A_VERDICT + " A pre-read is an open, non-authoritative "
           "reading by models that may not approve anything; every finding "
           "below is a candidate a reviewer checks against the line itself.",
           ""]
    if result["truncated"]:
        out += ["Sent truncated (only the first bytes of the file were read): "
                + ", ".join(result["truncated"]), ""]
    out += ["## kept (the quoted line is in the diff)", ""]
    out += [f + "\n" for f in result["kept"]] or ["none\n"]
    out += ["## dropped by the citation check (the quoted line is NOT in the "
            "diff)", ""]
    out += [f + "\n" for f in result["dropped"]] or ["none\n"]
    if result["failures"]:
        out += ["## legs that failed", ""]
        out += ["- %s %s (%s): %s" % (f["leg"], f["file"],
                                      f.get("model") or "?", f["why"])
                for f in result["failures"]] + [""]
    if result["merge_empty"]:
        out += ["THE MERGE CALL CARRIED NOTHING — it answered with no numbered "
                "finding while the per-file judgements did, so the findings "
                "above are the per-file union and the merge below is only "
                "evidence about the merge.", ""]
    if result["notes"]:
        out += ["## what the judge said that was not a numbered finding", ""]
        out += [n + "\n" for n in result["notes"]]
    out += ["## the judge's per-file answers, verbatim", "",
            result["per_file_text"] or "(no file was judged)", "",
            "## the judge's merged answer, verbatim", "", result["judge_text"]
            or "(the judge answered nothing)", ""]
    return "\n".join(out)


def facts(path):
    """The header block of a written pre-read, as {name: value}, or {}.

    BOUNDED AT THE HEADER, not at a byte count. A finding's own prose can
    carry a `- name: value` line, and a reader that swallowed the whole file
    would let a quoted line rewrite the counts the pointer prints.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(8192)
    except OSError:
        return {}
    return {k: v.strip()
            for k, v in _FACT.findall(text.split(NOT_A_VERDICT)[0])}


def write(result, subject=None):
    """(path, ledger_line) — the file, then one line in the PRE-READ's own
    ledger. Nothing here touches the dispatch ledger."""
    from . import pk
    path = report_path(subject or result["subject"])
    os.makedirs(store_dir(), exist_ok=True)
    pk.atomic_write(path, render(result))
    line = {"ts": result["ts"], "row": result["subject"], "ref": result["ref"],
            "readers": result["readers"], "judge": result["judge"],
            "drafts": result["drafts"], "kept": len(result["kept"]),
            "dropped": len(result["dropped"]), "cost": result["cost"],
            "wall_s": result["wall_s"]}
    with open(ledger_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return path, line


def triage_line(dispatch_id):
    """The one line `helm dispatch triage <id>` prints, or "" when this row has
    no pre-read. Read out of the file's own header so the count in the line and
    the findings in the file are the same measurement."""
    path = report_path(dispatch_id)
    if not os.path.exists(path):
        return ""
    got = facts(path)
    return "pre-read: %s (%s findings, %s dropped, %s, judge %s)" % (
        path, got.get("kept", "?"), got.get("dropped", "?"),
        coverage_phrase(got), got.get("judge", "?"))


def coverage_phrase(got):
    """"read N of M files" from a written header, and NOTHING WAS READ when
    N is zero. A file nobody read and a file nobody found anything in are
    different facts, and "0 findings" alone reads as the second; the reader-
    facing line says which."""
    try:
        files = int(got.get("files", ""))
    except (TypeError, ValueError):
        return "coverage unknown"
    not_read = [x for x in str(got.get("not_read", "")).split(", ")
                if x and x != "none"]
    read = files - len(not_read)
    if files and read <= 0:
        return "NOTHING WAS READ (0 of %d files)" % files
    try:
        judged = int(got.get("judged_files", ""))
    except (TypeError, ValueError):
        judged = None
    if judged is not None and judged < read:
        return "read %d of %d files, judged %d" % (read, files, judged)
    return "read %d of %d files" % (read, files)


# ---------------------------------------------------------------- the verb


def usage():
    """The synopsis, read from the ONE place that holds it. `cli._VERB_HELP`
    is what `helm preread --help` and the root listing print, so a second copy
    here is a second thing to keep true; this reads that one."""
    from .cli import _VERB_HELP
    return "helm " + _VERB_HELP["preread"]


def _row_for(token):
    """(row, None) or (None, why) — one open ledger row by id or unique
    prefix. The pre-read READS the dispatch ledger and writes nothing to it."""
    from . import dispatches
    snap = dispatches.rows() or {}
    hits = [row for rid, row in sorted(snap.items())
            if rid == token or rid.startswith(token)]
    if not hits:
        return None, "no dispatch row matches %s" % token
    if len(hits) > 1:
        return None, "%s matches %d rows — name more of the id" % (
            token, len(hits))
    row = hits[0]
    if not row.get("ref"):
        return None, "row %s carries no ref to read" % token
    if not row.get("repo_root"):
        return None, "row %s carries no repo_root to read it in" % token
    return row, None


def _announce(row, path, kept, coverage="coverage unknown"):
    """DM the row's recipient one line with the path, through the same door
    every other dispatch-side wake uses. A pre-read nobody is told about is a
    file nobody opens, and one that read nothing must say so in the DM."""
    from . import seats
    to = str(row.get("recipient") or "")
    if not to:
        return "the row names no recipient"
    text = ("pre-read ready for your review row %s: %s (%d candidate "
            "finding%s, %s, NOT a verdict — check each against the line)"
            % (str(row.get("id") or "")[:12], path, kept,
               "" if kept == 1 else "s", coverage))
    try:
        sent, why = seats.dm(to, text, who="preread")
    except Exception as exc:                          # noqa: BLE001
        return "the DM raised %s: %s" % (type(exc).__name__, exc)
    return None if (sent and not why) else (why or "the DM door said nothing")


def cmd_preread(args):
    """preread <dispatch-id> — a council of cheap readers pre-reads a row."""
    from .cli import guard_tail
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(usage(), file=sys.stderr)
        return 2
    token, rest = (None, args) if args[0].startswith("-") \
        else (args[0], args[1:])
    rc = guard_tail("helm preread", rest, flags=("--no-dm", "--json"),
                    valued=("--ref", "--repo"), usage=usage())
    if rc is not None:
        return rc
    cfg, why = load_config()
    if why:
        print("helm preread: " + why, file=sys.stderr)
        return 2
    row = None
    if token:
        row, why = _row_for(token)
        if why:
            print("helm preread: " + why, file=sys.stderr)
            return 2
        ref, repo, subject = row["ref"], row["repo_root"], str(row["id"])
    else:
        if "--ref" not in rest or "--repo" not in rest:
            print("helm preread: name a dispatch id, or both --ref and --repo",
                  file=sys.stderr)
            return 2
        ref = rest[rest.index("--ref") + 1]
        repo = os.path.abspath(os.path.expanduser(
            rest[rest.index("--repo") + 1]))
        subject = ref
    result, why = run(ref, repo, subject, cfg)
    if why:
        print("helm preread: " + why, file=sys.stderr)
        return 2
    path, line = write(result)
    if "--json" in rest:
        print(json.dumps(line, ensure_ascii=False, sort_keys=True))
    else:
        print("helm preread: %s — %d kept, %d dropped, %d drafts over %d "
              "file(s), judge %s, %ss%s"
              % (path, len(result["kept"]), len(result["dropped"]),
                 result["drafts"], len(result["files"]), result["judge"],
                 result["wall_s"],
                 ", %d not read" % len(result["not_read"])
                 if result["not_read"] else ""))
        for name in result["not_read"]:
            print("  NOT READ: " + name)
        for name in result["not_judged"]:
            print("  READ BUT NOT JUDGED (the judge leg failed): " + name)
    if row is not None and "--no-dm" not in rest:
        why = _announce(row, path, len(result["kept"]),
                        coverage_phrase(facts(path)))
        if why:
            print("helm preread: the recipient was not told (%s)" % why,
                  file=sys.stderr)
    return 0
