#!/usr/bin/env python3
"""helm know-your-user — the WHO leg (memory=did, rules=believes, lexicon=means,
THIS=who). The know-your-user leg: what makes helm feel like a teammate instead of a
tool, and the reason it exists at all.

Store: ~/.helm/_global/know-your-user/
  profile.json   the one operator profile (schema v2). merge_scaffold() folds
                 in an optional external scaffold profile in place — content
                 wins over emptiness in both directions.
  notes/         dated superseding notes — guidance grows as markdown with
                 frontmatter (the store-evolution verdict), not bare JSON
                 strings. A note naming a predecessor in `supersedes` hides it;
                 history stays on disk.

The INTERVIEW is the first-run step that fills the leg: interactive on a tty,
copy-pasteable question sheet otherwise. A profile that already HAS content is
interviewed by CONFIRMATION — every line is a draft to keep/correct/drop, because
correcting is faster than composing. Off a tty, drafts are printed and NOTHING is
written: an agent never stands in for the owner (the certainty rail). Never nags —
done latches it off. Import-safe, side-effect-free.
"""
import os
import sys

from . import home, pk

SCHEMA_VERSION = 2

# Calibration anchors for technical_level (free-form tolerated), least -> most.
LEVELS = ("non-technical", "some-technical", "technical", "expert")

_STATUS_RANK = {"": 0, "offered": 1, "done": 2}

# The interview: each answer either fills profile.technical_level (that one
# key) or lands as a dated note with topic=key.
QUESTIONS = (
    {"key": "technical_level",
     "question": "How technical are you? (%s)" % " / ".join(LEVELS),
     "why": "Calibrates explanation depth — every agent pitches to this from session one."},
    {"key": "never_explain",
     "question": "What should an agent NEVER stop to explain to you?",
     "why": "Skipping what you already know is the fastest respect an agent can show."},
    {"key": "working_style",
     "question": "How much autonomy should agents take, and when should they interrupt you?",
     "why": "The autonomy/interrupt line is the biggest day-to-day feel difference."},
    {"key": "voice",
     "question": "How do you like to be spoken to — brevity vs narrative, formal vs warm?",
     "why": "Tone is remembered longer than content; get it right once, get it everywhere."},
    {"key": "corrections",
     "question": "What do agents keep getting wrong about working with you?",
     "why": "Standing corrections stop the same apology from happening twice."},
    {"key": "goals",
     "question": "What are you building toward this quarter?",
     "why": "Goals let an agent weigh what is worth your attention."},
    {"key": "warmth",
     "question": "What makes an agent feel like a great teammate rather than a tool?",
     "why": "The know-your-user leg IS the product — this answer defines it in your words."},
    {"key": "pet_peeves",
     "question": "What are your pet peeves in agent behavior or output?",
     "why": "Peeves are cheap to avoid and expensive to repeat."},
    {"key": "defaults",
     "question": "Preferred defaults — naming, formatting, review style?",
     "why": "Defaults answered once beat the same three questions in every session."},
)


def store_dir():
    return os.path.join(home.global_dir(), "know-your-user")


def profile_path():
    return os.path.join(store_dir(), "profile.json")


def notes_dir():
    return os.path.join(store_dir(), "notes")


def scaffold_profile_path():
    """The optional external scaffold this store merges from — the full path to
    a profile.json, named by env HELM_PROFILE_SCAFFOLD. Unset -> None (no
    scaffold; merge_scaffold() is then a no-op)."""
    raw = os.environ.get("HELM_PROFILE_SCAFFOLD")
    return os.path.expanduser(raw) if raw else None


def _empty_profile():
    return {
        "schema_version": SCHEMA_VERSION,
        "technical_level": "",
        "guidance": [],
        "interview_status": "",  # "" (never offered) | "offered" | "done"
        "updated_at": "",
        "source": "fresh",  # "fresh" | "merged-from-scaffold"
    }


def _norm_guidance(g):
    if isinstance(g, list):
        return [str(x).strip() for x in g if str(x).strip()]
    return [str(g).strip()] if str(g or "").strip() else []


def load_profile():
    """Fail-open: missing/garbled -> empty (a corrupt store degrades to
    re-interviewable, never breaks a turn). Unknown top-level keys from a newer
    schema carry through so a re-save never drops them (the forward-compat law)."""
    d = pk.read_json(profile_path())
    base = _empty_profile()
    if not isinstance(d, dict):
        return base
    for k, v in d.items():
        if k not in base:
            base[k] = v
    try:
        ver = int(d.get("schema_version", SCHEMA_VERSION))
    except (TypeError, ValueError):
        ver = SCHEMA_VERSION
    base["schema_version"] = max(SCHEMA_VERSION, ver)
    base["technical_level"] = str(d.get("technical_level") or "").strip()
    base["guidance"] = _norm_guidance(d.get("guidance"))
    base["interview_status"] = str(d.get("interview_status") or "").strip()
    base["updated_at"] = str(d.get("updated_at") or "").strip()
    base["source"] = str(d.get("source") or "fresh").strip()
    return base


def save_profile(p):
    p["updated_at"] = pk.now_ts()
    pk.write_json(profile_path(), p)
    pk.event("whoami.save", profile_path(), "operator profile saved")
    return p


def _content_key(p):
    return (p["schema_version"], p["technical_level"], tuple(p["guidance"]),
            p["interview_status"], p["source"])


def merge_scaffold(scaffold_path=None):
    """Import an external know-your-user scaffold (content AND bookkeeping) into
    the helm profile. Content wins over emptiness in both directions: a scaffold
    field never clobbers non-empty helm content, an empty scaffold field never
    erases anything, interview_status only ever ranks UP (offered never
    downgrades done). No scaffold configured -> a no-op. Idempotent — a
    no-change merge does not rewrite the store."""
    p = load_profile()
    before = _content_key(p)
    path = scaffold_path if scaffold_path is not None else scaffold_profile_path()
    ext = pk.read_json(path) if path else None
    if isinstance(ext, dict):
        lvl = str(ext.get("technical_level") or "").strip()
        if lvl and not p["technical_level"]:
            p["technical_level"] = lvl
        for g in _norm_guidance(ext.get("guidance")):
            if g not in p["guidance"]:
                p["guidance"].append(g)
        status = str(ext.get("interview_status") or "").strip()
        if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(p["interview_status"], 0):
            p["interview_status"] = status
        if _content_key(p) != before:
            # only an ACTUAL import stamps the source — an empty scaffold must
            # not clobber a richer provenance note (it did once: a derived
            # profile's source was overwritten by a no-op merge)
            p["source"] = "merged-from-scaffold"
    if not os.path.exists(profile_path()) or _content_key(p) != before:
        save_profile(p)
    return p


# ---- notes: dated superseding guidance -------------------------------------

_NOTE_DEFAULTS = {"name": "", "description": "", "type": "", "ts": "",
                  "topic": "", "supersedes": ""}


def _bare(name):
    """A supersedes/name reference normalized to the bare note name (path and
    .md tolerated, per the live store's bare-name convention)."""
    n = os.path.basename(str(name or "").strip())
    return n[:-3] if n.endswith(".md") else n


def add_note(text, topic="", supersedes=""):
    """One dated note: note-<ts-slug>-<slug>.md with simple frontmatter + body.
    Returns the note name (bare, no .md), or None on empty text."""
    text = (text or "").strip()
    if not text:
        return None
    ts = pk.now_ts()
    stem = "note-%s-%s" % (pk.slug(ts), pk.slug(topic or text.split("\n")[0][:40]))
    path = os.path.join(notes_dir(), stem + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(notes_dir(), "%s-%d.md" % (stem, n))
        n += 1
    name = os.path.basename(path)[:-3]
    desc = text.split("\n")[0][:120].replace('"', "'")
    pk.atomic_write(path, (
        "---\n"
        "name: %s\n"
        'description: "%s"\n'
        "metadata:\n"
        "  type: know-your-user\n"
        "  ts: %s\n"
        "  topic: %s\n"
        "  supersedes: %s\n"
        "---\n\n%s\n" % (name, desc, ts, topic, _bare(supersedes), text)))
    return name


def _body(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    fences = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(fences) >= 2 and fences[0] == 0:
        lines = lines[fences[1] + 1:]
    return "\n".join(lines).strip()


def load_notes(include_superseded=False):
    """All notes newest-first (ts, then mtime within one second). A note named
    in another note's `supersedes` is hidden unless include_superseded — the
    newest statement of a topic wins, history stays on disk."""
    d = notes_dir()
    try:
        files = sorted(f for f in os.listdir(d)
                       if f.startswith("note-") and f.endswith(".md"))
    except OSError:
        return []
    notes = []
    for f in files:
        path = os.path.join(d, f)
        e = pk.parse_simple_frontmatter(path, _NOTE_DEFAULTS)
        if e is None:
            continue
        e["name"] = e["name"] or f[:-3]
        e["body"] = _body(path)
        e["_mtime"] = os.stat(path).st_mtime_ns
        notes.append(e)
    notes.sort(key=lambda e: (e["ts"], e["_mtime"]), reverse=True)
    if not include_superseded:
        gone = {_bare(e["supersedes"]) for e in notes if e["supersedes"]}
        notes = [e for e in notes if _bare(e["name"]) not in gone]
    for e in notes:
        e.pop("_mtime")
    return notes


# ---- CLI verbs (wired into cli.py by the integrator) -----------------------

def _take_flag(args, flag):
    if flag not in args:
        return ""
    i = args.index(flag)
    v = args[i + 1] if i + 1 < len(args) else ""
    del args[i:i + 2]
    return v


def _note_cmd(args):
    args = list(args)
    topic = _take_flag(args, "--topic")
    supersedes = _take_flag(args, "--supersedes")
    text = " ".join(args).strip()
    if not text:
        print("usage: helm whoami note <text...> [--topic T] [--supersedes <note-name>]",
              file=sys.stderr)
        return 2
    name = add_note(text, topic=topic, supersedes=supersedes)
    tail = " — supersedes %s" % _bare(supersedes) if supersedes else ""
    print("helm whoami: noted (%s)%s" % (name, tail))
    return 0


def cmd_whoami(args):
    """whoami [note <text...> [--topic T] [--supersedes N]] — what your agents
    know about you: profile + active notes."""
    if args and args[0] == "note":
        return _note_cmd(args[1:])
    if args:
        # `whoami <junk>` used to silently show the profile and exit 0 — the
        # one advertised subverb is `note`; anything else refuses.
        print("helm whoami: unknown subverb '%s' (whoami [note <text...>])"
              % args[0], file=sys.stderr)
        return 2
    p = merge_scaffold()
    notes = load_notes()
    if not (p["technical_level"] or p["guidance"] or notes):
        print("helm whoami: the know-your-user leg is empty — your agents don't know you yet.")
        print("  Run `helm interview` (a few minutes) and every future session starts warmer.")
        return 0
    print("helm whoami — what your agents know about you\n")
    if p["technical_level"]:
        print("  technical level: %s" % p["technical_level"])
    print("  interview: %s   source: %s   updated: %s" % (
        p["interview_status"] or "not offered", p["source"], p["updated_at"] or "-"))
    if p["guidance"]:
        print("\n  standing guidance:")
        for g in p["guidance"]:
            print("    - %s" % g)
    if notes:
        print("\n  active notes (newest first):")
        for e in notes:
            print("    [%s] %s — %s" % ((e["ts"] or "?")[:10], e["topic"] or "general",
                                        e["body"].split("\n")[0] if e["body"] else e["description"]))
    return 0


def _print_questions(p):
    print("helm interview — %d questions so your agents can actually know you" % len(QUESTIONS))
    print("(answer any subset; every answer makes every future session warmer)\n")
    for i, q in enumerate(QUESTIONS, 1):
        print("%2d. [%s] %s" % (i, q["key"], q["question"]))
        print("      why: %s" % q["why"])
    print("\nTo answer: run `helm interview` in a terminal (interactive), or submit any")
    print('single answer as a dated note:  helm whoami note "<answer>" --topic <key>')
    if not p["interview_status"]:
        p["interview_status"] = "offered"
        save_profile(p)
    return 0


def _run_interview(p):
    print("helm interview — %d quick questions. Blank answer skips; ctrl-d stops early." % len(QUESTIONS))
    print("This is the know-your-user leg: what you say here rides into every future session.\n")
    captured = 0
    for q in QUESTIONS:
        print(q["question"])
        print("  (%s)" % q["why"])
        try:
            a = input("> ").strip()
        except EOFError:
            print()
            break
        print()
        if not a:
            continue
        captured += 1
        if q["key"] == "technical_level":
            p["technical_level"] = a
        else:
            add_note(a, topic=q["key"])
    p["interview_status"] = "done"
    save_profile(p)
    print("Thank you — %d answer%s captured. Your agents know you now: `helm whoami`" % (
        captured, "s"[:captured != 1]))
    print("shows what they see, and `helm whoami note` grows it any time.")
    return 0


def _drafts(p):
    """The profile's content flattened to confirmable (key, line) rows."""
    lvl = [("technical_level", p["technical_level"])] if p["technical_level"] else []
    return lvl + [("guidance", g) for g in p["guidance"]]


def _print_drafts(p):
    """No tty: show the drafts a live interview would confirm and write NOTHING —
    an agent must never stand in for the owner (the certainty rail)."""
    rows = _drafts(p)
    print("helm interview — %d draft answer%s on file, awaiting owner confirmation." % (
        len(rows), "s"[:len(rows) != 1]))
    print("(source: %s)\n" % p["source"])
    for i, (key, val) in enumerate(rows, 1):
        print("%2d. [%s] %s" % (i, key, val))
    print("\nNot a tty — nothing was written; drafts stay drafts. Run `helm interview`")
    print("in a terminal to keep/correct/drop each line — only the owner confirms.")
    return 0


def _confirm_one(i, n, key, val):
    """One draft -> confirmed value, or None to drop. Blank/y keeps, e prompts a
    rewrite, d drops, any other text IS the correction (the fastest edit).
    EOFError propagates — the caller keeps the rest as drafted."""
    print("%d/%d [%s] %s" % (i, n, key, val))
    a = input("  y(keep) / e(dit) / d(rop) / corrected text > ").strip()
    if a.lower() in ("", "y", "yes", "k", "keep", "ok"):
        return val
    if a.lower() in ("d", "drop", "n", "no"):
        return None
    if a.lower() in ("e", "edit"):
        return input("  rewrite > ").strip() or val
    return a


def _confirm_interview(p):
    """Interview by confirmation: helm drafted the answers, the owner corrects.
    ctrl-d keeps the remaining drafts; completion marks the profile
    owner-confirmed and latches the interview done."""
    rows = _drafts(p)
    kept = [v for _, v in rows]  # ctrl-d = remaining drafts stand
    print("helm interview — %d draft answer%s already on file (%s)." % (
        len(rows), "s"[:len(rows) != 1], p["source"]))
    print("Blank/y keeps a line, e rewrites, d drops, or type the correction directly.")
    print("ctrl-d keeps the rest as drafted.\n")
    try:
        for i, (key, val) in enumerate(rows):
            kept[i] = _confirm_one(i + 1, len(rows), key, val)
            print()
        while True:
            a = input("add guidance (blank to finish) > ").strip()
            if not a:
                break
            rows.append(("guidance", a))
            kept.append(a)
    except EOFError:
        print()
    p["technical_level"] = ""
    p["guidance"] = []
    for (key, _), v in zip(rows, kept):
        if v is None:
            continue
        if key == "technical_level":
            p["technical_level"] = v
        else:
            p["guidance"].append(v)
    p["interview_status"] = "done"
    p["source"] = "owner-confirmed %s" % pk.now_ts()[:10]
    save_profile(p)
    confirmed = sum(v is not None for v in kept)
    print("Thank you — %d line%s confirmed, %d dropped. `helm whoami` shows what your" % (
        confirmed, "s"[:confirmed != 1], len(kept) - confirmed))
    print("agents see, and `helm whoami note` grows it any time.")
    return 0


def cmd_interview(args):
    """interview [--questions] [--redo] — the first-run step that fills the
    know-your-user leg. A populated profile is confirmed line-by-line (drafts to
    keep/correct/drop); an empty one gets the blank questions. Interactive on a
    tty; otherwise prints the sheet, or the drafts unwritten. Never nags: done
    latches it off."""
    from .cli import guard_tail
    rc = guard_tail("helm interview", args, flags=("--questions", "--redo"),
                    usage="interview [--questions] [--redo]")
    if rc is not None:
        return rc
    p = merge_scaffold()
    if p["interview_status"] == "done" and "--redo" not in args and "--questions" not in args:
        print("helm interview: already done (updated %s) — you're known." % (p["updated_at"] or "?"))
        print("  `helm whoami` shows the profile; `helm interview --redo` revisits it.")
        return 0
    if "--questions" in args:
        return _print_questions(p)
    if _drafts(p):
        return _confirm_interview(p) if sys.stdin.isatty() else _print_drafts(p)
    if not sys.stdin.isatty():
        return _print_questions(p)
    return _run_interview(p)
