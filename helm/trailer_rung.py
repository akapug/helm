#!/usr/bin/env python3
"""The commit-message rung: NO AI AUTHORING LINE REACHES A COMMIT.

THE RULE IS THE OWNER'S AND IT IS ABSOLUTE: no AI authoring line is added
anywhere, ever. Nothing reaching GitHub says which model wrote it, because
anything private can become public later and the note cannot be recalled once
it has. An operator reading git log finds no trace of model authorship.

WHY THE WHOLE MESSAGE AND NOT THE TRAILER BLOCK. A line can be verbatim
present and still not be a trailer, because prose after it demotes it to body
text -- so a rung that consulted git's trailer block would be blind to exactly
that placement. The bytes reach GitHub either way and the rule is about the
bytes, so every line of the message is judged,
and `git interpret-trailers` is no longer consulted at all.

AND WHY COLUMN ZERO AND A TRAILER SHAPE ANYWAY. Widening to "the message
contains this string" would refuse every commit that DISCUSSES the rule --
including the one that introduced it, and every arm whose message quotes what
it refuses. So a hit must be a line that IS one: column zero, trailer grammar
or a footer verb, and a name that is a model, an AI harness, a vendor AI
address or a fleet seat. Prose mentioning one, or quoting one indented, is not
an authoring line. That leaves a real hole -- an author
can indent a genuine trailer -- and the hole is ACCEPTED, because this rung
exists to stop the HARNESS DEFAULT, which is always emitted at column zero,
not to defeat somebody smuggling one past it.

IT REFUSES, WITH NO SHAKEOUT RELEASE, and that is a departure from its
predecessor rather than an oversight. That one shipped warning-first because
the compliant population was unknown and a refusal against an unenumerated
rule earns a skip variable in its first week. The reasoning does not transfer:
the compliant population here is "no such line", which needs no census, and
the rule came from the owner rather than from a measurement that might be
wrong. HELM_TRAILER_SKIP=1 still exists for a commit that must carry such a
line at column zero; nothing in helm needs it today.

THE MESSAGE IS ONLY HALF THE DISCLOSURE. A seat also announces itself on git's
IDENTITY plane: `helm/launch.py` exported the seat name as GIT_AUTHOR_NAME, so
commits rendered as `helm-claude-2` on GitHub however clean the message was.
That export is withdrawn in the same change. No message rung could ever have
seen it, which is the shape worth keeping: THE DISCLOSURE A GUARD IS BUILT TO
CATCH MAY ALSO TRAVEL ON A PLANE THE GUARD DOES NOT READ.
"""
import os
import re
import sys
import unicodedata

# WHAT COUNTS AS AN AI AUTHORING LINE. Two shapes, judged at column zero.
#
# `_ATTRIBUTION_KEYS` are trailer tokens that exist ONLY to name a machine
# author -- their presence is the disclosure whatever the value says.
# `Co-Authored-By` is NOT among them: it is a legitimate trailer for human
# collaborators and refusing it wholesale would break real co-authorship, so it
# is judged by its VALUE through `_value_credits_a_model`'s bounded grammar.
_ATTRIBUTION_KEYS = ("claude-session", "x-generated-by", "ai-generated-by")

# Substrings that make a Co-Authored-By value a MACHINE author. Comparison is
# normalized and case-folded below. `claude` is deliberately NOT a substring:
# Claude is also a human first name, so its model sense is judged by the same
# bounded family grammar as Kimi and Fable. The vendor address remains
# unconditional because it identifies the harness even when the display name
# is changed.
_MARK_SUBSTRINGS = ("noreply@anthropic.com", "gpt-", "chatgpt", "codex",
                    "copilot", "gemini", "openai", "anthropic")

# EVERY FAMILY THE FLEET RUNS IS NAMED, not only the vendor: a harness that
# credits its model by its own name ("Co-Authored-By: Fable <...>") never says
# "anthropic", so a vendor-only list lets that line through. Family names are
# matched only at the START of the display name and are a model when bare or
# followed/joined by a digit-bearing version. An ordinary successor remains a
# person: "Kimi Coder", "Kimi-Ai", and "Claude Martin" are not composed from a
# global family x machine-word Cartesian product.
_MODEL_WORDS = ("claude", "opus", "sonnet", "haiku", "fable", "kimi",
                "moonshot", "deepseek", "grok", "xai", "qwen", "qwenlocal",
                "cursor", "openrouter", "glm", "gpt", "gptoss", "llama",
                "mistral", "bonsai")

# Joined/spaced aliases that are model names by declaration, not because either
# half happens to occur in a global machine-word list. An alias followed by a
# digit-bearing token is a model whatever trails it ("Claude Opus 5.5, working
# in helm"); no person is named "Claude Opus 5.5". `claude-fable` is the
# harness's own display name for the Fable model ("Claude Fable 5.1"); the
# `or-`, `grok-build` and `north-mini-code` rows are catalog model ids.
_MODEL_ALIASES = ("moonshot-ai", "qwen-coder", "kimi-k3", "xai-grok",
                  "claude-code", "claude-opus", "claude-sonnet",
                  "claude-haiku", "claude-fable", "grok-build", "gpt-oss",
                  "or-fast", "or-code", "or-dots", "or-free",
                  "north-mini-code")
# A leading family + version may carry only bounded model-variant tokens.
# Requiring the version first keeps `Grok 4 Heavy`, `GLM 5 Air`, and
# `Qwen 3 Coder` without restoring the global family x machine-word composition
# that misread `Kimi Coder` and `Kimi-Ai` as models. The tier names are here so
# the family-first spelling "Claude 3.5 Sonnet" reads like "Claude Sonnet 3.5".
_VERSION_VARIANTS = ("agent", "ai", "air", "assistant", "bot", "cli", "code",
                     "coder", "coding", "fast", "flash", "heavy", "high",
                     "labs", "llm", "max", "medium", "mini", "model",
                     "preview", "pro", "studio", "team", "thinking", "turbo",
                     "opus", "sonnet", "haiku", "fable", "astra", "sol")
# Ids no human name contains, matched only where a token STARTS: a bare
# substring refused `Ed <eds4@...>` and `Kids4Code` on `ds4`.
_SAFE_MODEL_ID = re.compile(r"(?<![a-z0-9])(?:ds4|gpt-oss|dots3)")
# A bracketed qualifier is read apart from the name it follows: the harness
# writes "Claude Opus 5 (1M context)" and a proxy seat "Claude (kimi-k3,
# Moonshot)", and "claude[bot]" is a bot account, while "Kimberly (Kimi)
# Nozawa" is a person with a nickname.
_BRACKETED = re.compile(r"[(\[{]([^)\]}]*)[)\]}]?")
# ONE SPLITTER FOR EVERY CREDITED NAME, after a trailer key or a footer verb,
# and for the seat names among them. A credit can list names ("Jane Doe &
# Claude Opus"), and a name ends where prose about it starts: a comma, a
# colon, a semicolon, a slash, an ampersand or a plus, a bracket, a spaced
# dash, a possessive, a sentence end, or a connective ("Claude Opus via
# Bedrock", "Fable in helm"). Two lists that disagree refuse a line on one
# surface and admit the same words on the other.
_NAME_LIST = re.compile(r"[,;:/&+()\[\]{}]|\s[—–-]+\s|['’]s\b|\.(?:\s|$)"
                        r"|\s(?:and|via|using|with|through|in|on|for|under"
                        r"|from|at|as|to|by|against|after|of)\s")
_NAME_TRIM = " \t.:!?'\"`*_@"
# A MODEL NAMED ONLY IN THE ADDRESS. A harness can write a plain display name
# over its vendor's address (an address at moonshot.ai or cursor.com),
# and the grammar reads only the name. An address at an AI vendor's domain, or
# a subdomain of one, names the model. The local part alone is NOT judged by
# the grammar: `kimi@` or `claude2@` at a human domain is a person, and only
# an exact roster seat (`_seat_names`) is refused there.
_AI_DOMAINS = ("anthropic.com", "claude.ai", "claude.com", "openai.com",
               "chatgpt.com", "moonshot.ai", "moonshot.cn", "kimi.ai",
               "kimi.com", "x.ai", "grok.com", "cursor.com", "cursor.sh",
               "anysphere.co", "deepseek.com", "mistral.ai", "openrouter.ai",
               "z.ai", "zhipuai.cn", "bigmodel.cn", "qwen.ai", "qwenlm.ai",
               "codeium.com", "windsurf.com", "cognition.ai", "devin.ai",
               "aider.chat", "ampcode.com", "factory.ai", "sisyphuslabs.ai")
_ADDRESS = re.compile(r"([a-z0-9._%+-]+)@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_ANGLED = re.compile(r"<([^<>]*)>?")
_CONFUSABLES = {"а": "a", "α": "a", "с": "c"}
_MIXED_CONFUSABLE = re.compile(r"[a-z0-9.+\-аαс]+")


def _model_text(value):
    """NFKC/casefold plus a bounded mixed-script confusable skeleton.

    Only Cyrillic/Greek lookalikes measured around `Claude` are folded, and
    only inside a token that also carries an ASCII letter. That catches
    `Clаude-3` without transliterating non-Latin names or widening the model
    grammar to arbitrary Unicode lookalikes.
    """
    text = unicodedata.normalize("NFKC", value).casefold()

    def fold(match):
        word = match.group(0)
        return "".join(_CONFUSABLES.get(c, c) for c in word) \
            if any("a" <= c <= "z" for c in word) else word

    return _MIXED_CONFUSABLE.sub(fold, text)


def _digits(word):
    return any(c.isdigit() for c in word)


def _version_with_variants(words):
    """A version run (digit-bearing tokens: `5.5`, `3.6 27b`), optionally
    followed by declared variant tokens."""
    if not words or not _digits(words[0]):
        return False
    variants = False
    for word in words[1:]:
        if not variants and _digits(word):  # 5 5 after alias canonicalization
            continue
        if word not in _VERSION_VARIANTS:
            return False
        variants = True
    return True


def _leads_with_model(name, bare):
    """Whether NAME opens with a model: an alias, a family with a version,
    a joined `<seat>-claude-2` id, or -- when BARE -- the family alone."""
    canonical = "-".join(re.findall(r"[a-z0-9]+", name))
    for alias in _MODEL_ALIASES:
        if canonical == alias:
            return True
        if canonical.startswith(alias + "-"):
            # A tier may stand before the version: "Claude Code Opus 5.5".
            rest = canonical[len(alias) + 1:].split("-")
            if _digits(rest[0]) or all(w in _VERSION_VARIANTS or _digits(w)
                                       for w in rest):
                return True
    words = re.findall(r"[a-z0-9.+-]+", name)
    if not words:
        return False
    first = words[0]
    parts = [p for p in re.split(r"[-+]", first) if p]
    if len(parts) > 1 and _digits(parts[-1]) and \
            any(p in _MODEL_WORDS for p in parts[:-1]):
        return True                     # helm-claude-2; Jean-Claude stays
    for family in _MODEL_WORDS:
        if first == family:
            if (bare and len(words) == 1) or \
                    _version_with_variants(words[1:]):
                return True
            continue
        if first.startswith(family):
            joined = first[len(family):].lstrip("-.+")
            if _version_with_variants(
                    [part for part in re.split(r"[-+]", joined) if part] +
                    words[1:]):
                return True
    return False


def _vendor_address(low):
    """Whether an address in LOW is at an AI vendor's domain or under one."""
    return any(domain == d or domain.endswith("." + d)
               for _local, domain in _ADDRESS.findall(low)
               for d in _AI_DOMAINS)


# A FLEET SEAT NAME IS A MACHINE AUTHOR TOO, and the grammar above cannot
# read one. `meta-claude` and `Jean-Claude` have the same shape, so no rule
# about shape can refuse the first and pass the second. The seats are read
# from the roster helm keeps instead, and a name must match one EXACTLY.
#
# THE ROSTER IS THE FILE seatname_guard READS, resolved the same way:
# HELM_SEAT_NAMES, then MELD_SEAT_NAMES, then ~/.helm/_global/seat-names.txt.
# Every roster write keeps that file current, and it is plain text, so a
# standalone snapshot can read it without importing helm. A `!` line is held
# back from the tests/ guard only, and it is still a seat. A roster name with
# no separator and no digit (`cj`) is left out, because it also spells a
# person's initials, and a bare family word is the grammar's to judge.
#
# NO ROSTER MEANS NO SEAT NAMES, NOT A BUILT-IN LIST. The seats are instance
# data (helm/hardcode.py flags a seat name in helm logic for that reason),
# and a box with no roster runs no seats whose names could leak. The model
# grammar still applies there. Tests plant a fixture roster through
# HELM_SEAT_NAMES, which the suite already isolates.
_SEAT_SHAPE = re.compile(r"[-_.0-9]")


def _roster_path():
    """The seat-name authority path, or None for a relative override.

    This repeats seatname_guard.authority_target because this module cannot
    import it; tests/test_trailer_rung.py pins the two to one file."""
    path = os.path.expanduser(os.environ.get("HELM_SEAT_NAMES")
                              or os.environ.get("MELD_SEAT_NAMES")
                              or os.path.join("~", ".helm", "_global",
                                              "seat-names.txt"))
    return os.path.normpath(path) if os.path.isabs(path) else None


def _seat_names():
    """The roster's seat names, casefolded; empty when there is no roster."""
    path = _roster_path()
    if path is None:
        return frozenset()
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return frozenset()
    names = (line.strip().lstrip("!").strip().casefold()
             for line in text.splitlines())
    return frozenset(n for n in names
                     if n and not n.startswith("#") and _SEAT_SHAPE.search(n))


# Trailer tokens that are ordinary for humans and a disclosure only when the
# VALUE names a model. A `Model:` key is deliberately absent — a commit that
# describes model routing says `Model: codex` in prose, and this rung reads
# bytes, not intent. ASCII spaces/underscores/hyphens are equivalent separators
# in a key, so the nearby `Co-authored by:` spelling reaches the same lookup.
#
# THE CREDIT KEYS ARE HERE TOO. Reviewed-by and its siblings were never read,
# and trunk carries 23 Reviewed-by lines, every one naming a model, a seat or
# a model's subagent ("Reviewed-by: kimi (cross-family)"). They credit the
# author's helpers by name, so they are the same disclosure. A human value
# passes as it does for Co-Authored-By.
_VALUE_KEYS = ("co-authored-by", "assisted-by", "generated-by", "written-by",
               "authored-by", "reviewed-by", "signed-off-by", "helped-by",
               "acked-by", "tested-by")
_KEY_SEPARATORS = re.compile(r"[ \t\f\v_-]+", re.ASCII)


def _key_name(key):
    return _KEY_SEPARATORS.sub("-", key.strip().lower())

# Body-text footers that are not trailers at all but say the same thing.
# A FOOTER OPENS ITS LINE: only leading non-alphanumerics (the harness's robot
# emoji, a dash, a bullet) may stand before it. A substring test refused prose
# ABOUT the rule — "the hook now refuses the Generated with Claude Code
# footer", "nothing here was generated with Claude." — which is how a guard
# earns a skip variable.
#
# THE NAMES RIGHT AFTER THE VERB ARE JUDGED, EACH FROM ITS FIRST WORD. The
# footer once matched `claude` alone, so "Generated by Fable 5.1" and
# "Generated with Codex" passed. A wrapped line of prose can open with the
# same verb ("written by the shipped `codex_pool` writer" is on trunk), so the
# line is not searched for a model word anywhere: the words after the verb are
# split into names by `_NAME_LIST`, and a name must itself lead with a model.
# That is the grammar a trailer value's names share (`_credits_a_model`), with
# one difference. The vendor marks that are a substring anywhere in a trailer
# value must LEAD a name here, because a substring of a prose line is prose.
_FOOTER = re.compile(r"^\W*(?:generated (?:with|by)|written by"
                     r"|(?:co-?)?authored[ -](?:with|by))\s+(.*)",
                     re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARK_LEADS = re.compile(r"(?:gpt-|(?:chatgpt|codex|copilot|gemini|openai"
                         r"|anthropic)(?![a-z_]))")


def _name_pieces(name):
    """Every name that NAME lists, in order, the bracketed ones last.

    A BRACKET ENDS A NAME and its words are listed after the rest: in
    "Claude Opus (bot) working in helm" the name is "Claude Opus", and
    "Kimberly (Kimi) Nozawa" lists "Kimberly", "Nozawa", then "Kimi".
    When nothing outside a bracket is a name, the first bracketed one is:
    "(Claude)" credits Claude.

    A piece with no letter is no name and is dropped before the first name
    is chosen, so ", Claude", "1, Claude" and "—, Claude" all list
    "Claude" first. A digit is not a letter: a version or a numbering left
    before a comma is never the name the credit opens with."""
    pieces = (piece.strip(_NAME_TRIM)
              for chunk in [_BRACKETED.sub(",", name)] +
              _BRACKETED.findall(name)
              for piece in _NAME_LIST.split(chunk))
    return [piece for piece in pieces if any(c.isalpha() for c in piece)]


def _leads_with_a_model(piece, bare):
    """Whether one listed name leads with a vendor mark, a model id, or the
    model grammar; a family word alone counts only when BARE."""
    return bool(_MARK_LEADS.match(piece) or _SAFE_MODEL_ID.match(piece)) or \
        _leads_with_model(piece, bare)


def _credits_a_model(text):
    """Whether the names in one credit name a model or a fleet seat.

    TEXT is a trailer's value or the words after a footer verb, folded by
    `_model_text`. Both are read by this one grammar, so a line refused on
    one surface is refused on the other.

    THE FIRST NAME IS JUDGED ALONE, and a family word alone is the model:
    "Claude Opus, working in helm" and "Kimi via Bedrock" credit models. A
    LATER NAME counts only when it names a model unambiguously, so a
    surname-first person ("Nozawa, Kimi") or a person listed second ("Jane
    Doe, Claude Martin") stays a person. A bracketed name is later too:
    "Kimberly (Kimi) Nozawa" carries a nickname, "(kimi-k3, Moonshot)" names
    the model. Any listed name that is exactly a roster seat is refused.

    THE NAME ENDS AT ITS ADDRESS, AND THE LIST GOES ON PAST IT. What stands
    in angle brackets is an address, judged by its domain and its local part
    and never as words of the name, and every angle-bracketed address is
    judged. An address ends one name as a comma does, so the names after it
    are listed too: "Jane Doe <jane@example.com> and Claude Opus 5.5" credits
    the model, as it does with no address in it. Cut at the first address,
    the list ended at Jane. A credit that opens with a bracketed name and no
    address ("<Claude>", "<Claude> working in helm") is judged by that name
    first."""
    angled = [inner.strip() for inner in _ANGLED.findall(text)]
    lead = angled[:1] if angled and "@" not in angled[0] and \
        not _name_pieces(text.partition("<")[0]) else []
    names = lead + _name_pieces(_ANGLED.sub(",", text)) or [""]
    addresses = " ".join(angled)
    seats = _seat_names()
    return _leads_with_a_model(names[0], bare=True) or \
        any(_leads_with_a_model(n, bare=False) for n in names[1:]) or \
        any(n in seats for n in names) or _vendor_address(addresses) or \
        any(local in seats for local, _domain in _ADDRESS.findall(addresses))


def _value_credits_a_model(value):
    """Whether a credit trailer's VALUE names a model or a fleet seat.

    A trailer value is a credit from end to end, so a vendor mark, a model
    id, or an AI vendor or seat address anywhere in it names the machine.
    Its names are judged by `_credits_a_model`, as a footer's are."""
    low = _model_text(value)
    seats = _seat_names()
    return any(mark in low for mark in _MARK_SUBSTRINGS) or \
        bool(_SAFE_MODEL_ID.search(low)) or _vendor_address(low) or \
        any(local in seats for local, _domain in _ADDRESS.findall(low)) or \
        _credits_a_model(low)


def _footer_credits_a_model(rest):
    """Whether the words right after a footer verb name a model or a seat.

    REST is the line after "Generated with" or "Written by". A markdown link
    is read as its text, and the names are judged by `_credits_a_model`.

    THE NAME ALSO ENDS AT ITS ADDRESS, and the address is judged as a
    trailer's is. Read as words of the name, `<noreply@anthropic.com>` turned
    "Written by Claude <noreply@anthropic.com>" into a person, a line the
    claude-only footer before this one refused."""
    return _credits_a_model(_model_text(_MARKDOWN_LINK.sub(r"\1", rest)))

TOKEN = "Co-Authored-By"

# THIS MODULE SPAWNS NOTHING, which is why it carries no direct-git-spawn debt.
# Finding git's trailer BLOCK requires git's own parser — which trailing lines
# form the block is a real parse, and a paraphrase disagrees with git exactly
# where nobody tests. This rung does not need that question answered: every
# line of the message is judged, so the file is pure string work on the bytes
# it was handed.
#
# It still SHIPS AS A STANDALONE HOOK ASSET, copied into
# `.git/hooks/.helm-scanners/` and run as a plain script in repositories where
# the helm package is not importable, so it stays stdlib-only and imports
# nothing from helm.

OK = "OK"
FOUND = "FOUND"
UNKNOWN = "UNKNOWN"

def offending(message):
    """[(lineno, line)] — every AI AUTHORING LINE in this message.

    NO ``git interpret-trailers`` HERE, DELIBERATELY, and its removal is the
    inversion's real substance. The predecessor asked whether the line sat in
    git's trailer BLOCK, which was the right question while the line was
    REQUIRED: a demoted line had failed to do its job. Now the line's mere
    presence is the harm, and a demoted one reaches GitHub in the message body
    exactly like a promoted one. Asking git where the block ends would make
    body placement invisible — the one direction this rung must not fail in.

    COLUMN ZERO AND TRAILER GRAMMAR, so that PROSE ABOUT the rule survives.
    This module's own docstring, this function's, and every arm whose fixture
    quotes what it refuses would otherwise be refused by it. An indented line,
    or one mentioning the token mid-sentence, is not an authoring line.

    THE HOLE IS KNOWN AND ACCEPTED: an author who indents a real trailer
    passes. This rung exists to stop the HARNESS DEFAULT, which is always
    emitted at column zero, not to defeat somebody smuggling one past it.
    """
    hits = []
    for number, raw in enumerate(message.splitlines(), 1):
        if raw[:1].isspace():
            continue
        line = raw.strip()
        footer = _FOOTER.match(line)
        if footer and _footer_credits_a_model(footer.group(1)):
            hits.append((number, line))
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = _key_name(key)
        if key in _ATTRIBUTION_KEYS:
            hits.append((number, line))
        elif key in _VALUE_KEYS and _value_credits_a_model(value):
            hits.append((number, line))
    return hits


def verdict(message):
    """(state, why) for one commit message. The whole rule lives here.

    Separated from the hook entry point ON PURPOSE, and the reason outlived the
    rule it was written for: a population control over real history arrives as
    strings, not as file paths in argv. A rule exercisable only through its
    hook can only be tested against fixtures its author invented — which is how
    the predecessor's last-line bug survived review.

    NOTHING RETURNS UNKNOWN FROM HERE ANY MORE. The predecessor shelled out to
    git, so it had to treat "I could not read the message" as its own answer
    and fail open, or a broken git would turn every commit into a refusal. This
    reads the string it was handed. ``UNKNOWN`` survives for ``main``, which
    can still fail to DECODE the file.
    """
    hits = offending(message)
    if not hits:
        return OK, None
    return FOUND, "; ".join("line %d: %s" % (n, ln) for n, ln in hits)


def report(state, why, refuse):
    """The operator-facing lines. Returns the process exit code.

    IT PRINTS THE OFFENDING LINES WITH THEIR NUMBERS, because the seat reading
    this was told to ADD one of them by its own harness and may not believe it
    is there. A guard that says "remove the attribution line" has handed its
    reader a search; one that prints line 34 and the text has handed them an
    edit.
    """
    if state == OK:
        return 0
    if state == UNKNOWN:
        sys.stderr.write(
            "[helm trailer] WARNING: could not read the commit message (%s) —\n"
            "[helm trailer] attribution NOT checked for this commit.\n" % why)
        return 0
    verb = "REFUSED" if refuse else "WARNING"
    sys.stderr.write(
        "[helm trailer] %s: this commit message carries an AI AUTHORING LINE.\n"
        "[helm trailer]   %s\n"
        "[helm trailer] Owner ruling 2026-09-21: no AI authoring line is added\n"
        "[helm trailer] anywhere, ever. Nothing reaching GitHub should say which\n"
        "[helm trailer] model wrote it — anything private can become public\n"
        "[helm trailer] later, and the note cannot be recalled once it has.\n"
        "[helm trailer] DELETE the line(s) above and add nothing in their place.\n"
        "[helm trailer] Your harness may still be asking for one; THE OWNER\n"
        "[helm trailer] OUTRANKS IT. Other trailers are fine and untouched.\n"
        % (verb, why))
    if not refuse:
        sys.stderr.write(
            "[helm trailer] Not refusing: HELM_TRAILER_REFUSE=0 is set.\n")
        return 0
    sys.stderr.write(
        "[helm trailer] Skip this one commit: HELM_TRAILER_SKIP=1\n")
    return 1


def main(argv):
    """commit-msg entry: argv[1] is the path git wrote the message to."""
    if os.environ.get("HELM_TRAILER_SKIP") == "1":
        return 0
    if len(argv) < 2:
        sys.stderr.write("[helm trailer] WARNING: no message path given — "
                         "attribution NOT checked.\n")
        return 0
    # BYTES, NOT TEXT, AND THE CATCH IS WIDER THAN OSError. A commit message is
    # bytes — git has `i18n.commitEncoding` precisely because non-UTF-8 messages
    # are legitimate — so `open(path, "r")` decodes with the LOCALE default and
    # `read()` raises UnicodeDecodeError on the first bad byte. That is a
    # ValueError, not an OSError, so it escaped the catch below, propagated out
    # of main, and killed the commit-msg hook with a traceback and a non-zero
    # exit. A WARNING-FIRST RUNG THEN HARD-REFUSES THE COMMIT — the one posture
    # this whole file argues it must not have before its flip criterion is met,
    # and the only door here that failed OPPOSITE to every other unreadable path.
    # Measured end to end by a reviewer through the GENERATED hook on a build
    # node — not read out of this file — with rc=1 propagating while
    # HELM_TRAILER_REFUSE was unset. The two InstalledHookTest arms below are
    # that measurement, kept.
    #
    # `errors="replace"` RATHER THAN GIVING UP ON THE MESSAGE. The canonical
    # trailer is pure ASCII, so replacement cannot damage the thing being
    # searched for; only non-ASCII elsewhere becomes U+FFFD, which this rung has
    # no opinion about. The commonest real shape is a perfectly good ASCII
    # trailer beside one odd byte in the body, and treating the whole message as
    # unreadable would go blind on exactly that. `surrogateescape` was rejected:
    # it round-trips, but `trailers()` hands the string to subprocess with
    # text=True and lone surrogates raise on ENCODE, which MOVES the crash
    # instead of removing it.
    #
    # The widened catch still stands behind it, so anything that fails anyway
    # becomes the honest NOT-checked warning at exit 0 — an unreadable message
    # and a non-compliant one keep DIFFERENT values, which is the rule this
    # module is built on.
    try:
        with open(argv[1], "rb") as fh:
            message = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        sys.stderr.write("[helm trailer] WARNING: could not read %s (%s) — "
                         "attribution NOT checked.\n" % (argv[1], exc))
        return 0
    state, why = verdict(message)
    # REFUSES BY DEFAULT, AND THE VARIABLE INVERTED WITH THE RULE. Its
    # predecessor was opt-IN to refusal (`== "1"`) because it shipped against a
    # rule whose compliant forms nobody had enumerated, and a refusal like that
    # earns a skip variable in its first week. Neither half holds now: the
    # compliant population is "no such line", which needs no census, and the
    # rule is an owner ruling rather than a measurement that might be wrong.
    # `HELM_TRAILER_REFUSE=0` remains as the operator's off switch, so the
    # posture is still one variable away in BOTH directions.
    return report(state, why,
                  os.environ.get("HELM_TRAILER_REFUSE") != "0")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
