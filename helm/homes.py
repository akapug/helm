#!/usr/bin/env python3
"""helm homes — credential-home lifecycle for claude AND codex (stdlib only).
New archives land in ~/.helm-home-archive/; an older on-disk archive format
stays readable/restorable (read both, write new).

THE CANON (CRED_AUTH_CANON, distilled — violating these bricks accounts):
  * one home = one device login = one token family. Credentials are NEVER
    copied or moved between homes (revocation bomb): re-seating an identity is
    always a FRESH device login into the target home.
  * the HUMAN mints auth. These functions prepare homes and hand back the exact
    login command; they never run a login, never write token contents.
    Verification reads non-secret identity metadata only (.claude.json
    oauthAccount email; codex id_token email claim, decoded and discarded) —
    with ONE bounded exception: the shared-family audit hashes refresh-token
    bytes IN MEMORY (sha256) to detect byte-copies of a single token family
    across homes (the revocation bomb). Only the 10-hex digest prefix
    survives; token bytes are never printed, logged, or persisted.
  * canonical home name = the account email with every non-alphanumeric char
    folded to '-'  (owner@example.com -> owner-example-com). Aliases = symlinks.
  * claude homes: `projects` must SYMLINK to ~/.claude/projects (one shared
    session store) — a REAL projects dir silently strands sessions.
  * archive = MOVE into ~/.helm-home-archive/<name>-<date>/ (reversible),
    never delete; refused while a live agent sits on the home. An optional
    owner-configured legacy archive root (HELM_LEGACY_ARCHIVE_ROOT, unset by
    default) is also listed and restorable when set — read both, write new.

Every public function returns a JSON-able dict (or list); errors are
{"error": "..."} — loud, attributed, never an exception across the API edge.
"""
import base64, glob, hashlib, json, os, shlex, shutil, sys, time

from .home import env as _env

HOME = os.path.expanduser("~")
ROOTS = {"claude": os.path.join(HOME, ".claude-homes"),
         "codex": os.path.join(HOME, ".codex-homes")}
DEFAULTS = {"claude": os.path.join(HOME, ".claude"),
            "codex": os.path.join(HOME, ".codex")}
AUTH_FILE = {"claude": ".credentials.json", "codex": "auth.json"}
ENV_VAR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
SHARED_PROJECTS = os.path.join(HOME, ".claude", "projects")
ARCHIVE_ROOT = os.path.join(HOME, ".helm-home-archive")
MARKER = ".helm-archive.json"  # metadata only: name/provider/from/aliases — no token contents
# optional owner-configured legacy archive root — recognized for list/restore
# when set, never written to; empty (default) means no legacy archive exists
LEGACY_ARCHIVE_ROOT = os.environ.get("HELM_LEGACY_ARCHIVE_ROOT", "")
LEGACY_MARKER = os.environ.get("HELM_LEGACY_ARCHIVE_MARKER", ".legacy-archive.json")

# the human runs these; helm only prints them (logins are human-only, per canon)
LOGIN_CMDS = {"claude": lambda h: f"CLAUDE_CONFIG_DIR={shlex.quote(h)} claude /login",
              "codex": lambda h: f"CODEX_HOME={shlex.quote(h)} codex login --device-auth"}


def canonical_name(email):
    """owner@example.com -> owner-example-com (same fold as the predecessor's _norm — keep them in step)."""
    return "".join(ch if ch.isalnum() else "-" for ch in (email or "").lower()).strip("-")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _claude_identity(home):
    """oauthAccount email from a home's .claude.json — identity METADATA, never
    tokens. ONE content reader for the whole repo: cred.account_of (mtime-cached,
    fail-closed) so `helm cred`, this row, launch and doctor can never disagree
    about who a home holds."""
    from . import cred          # function-level: cred imports homes
    return cred.account_of(home)["email"]


def _codex_identity(home):
    """Best-effort email claim from auth.json's id_token payload (base64 decode only,
    NO verification — identity label, not authentication). Token discarded, never logged."""
    auth = _read_json(os.path.join(home, "auth.json")) or {}
    tok = (auth.get("tokens") or {}).get("id_token") or auth.get("id_token") or ""
    try:
        payload = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email")
        return email if isinstance(email, str) and email else None
    except Exception:
        return None


_IDENTITY = {"claude": _claude_identity, "codex": _codex_identity}


def _token_family(provider, home):
    """10-hex sha256 prefix of the home's refresh token — the FAMILY
    fingerprint. Content-equality grouping only: the bytes are read solely to
    hash IN MEMORY; nothing but the digest prefix leaves this function."""
    if provider == "claude":
        from . import cred
        auth = cred._read_json(os.path.join(home, AUTH_FILE[provider])) or {}
        tok = (auth.get("claudeAiOauth") or {}).get("refreshToken")
    else:
        auth = _read_json(os.path.join(home, AUTH_FILE[provider])) or {}
        tokens = auth.get("tokens") or {}
        tok = tokens.get("refresh_token") or tokens.get("refreshToken")
    if not isinstance(tok, str) or not tok:
        return None
    return hashlib.sha256(tok.encode()).hexdigest()[:10]


def _agent_procs():
    """[(pid, provider, home_realpath|None)] for every live claude/codex AGENT process
    (comm must match — children like MCP servers inherit the env but carry other
    comms). home None = the process runs on the provider DEFAULT home."""
    out = []
    for envf in glob.glob("/proc/[0-9]*/environ"):
        pid = envf.split("/")[2]
        try:
            with open(f"/proc/{pid}/comm") as fh:
                comm = fh.read().strip()
            if comm not in ("claude", "codex"):
                continue
            with open(envf, "rb") as fh:
                env = fh.read()
        except OSError:
            continue
        key = (ENV_VAR[comm] + "=").encode()
        val = None
        for var in env.split(b"\0"):
            if var.startswith(key):
                val = var.split(b"=", 1)[1].decode("utf-8", "replace")
                break
        out.append((int(pid), comm,
                    os.path.realpath(os.path.expanduser(val)) if val else None))
    return out


def _detect_provider(path):
    """Provider of an on-disk home by its own artifacts (for marker-less archives)."""
    if any(os.path.lexists(os.path.join(path, f))
           for f in (".claude.json", ".credentials.json", "projects", "statsig")):
        return "claude"
    if any(os.path.lexists(os.path.join(path, f))
           for f in ("auth.json", "config.toml", "sessions")):
        return "codex"
    return None


def _home_row(provider, path, aliases, procs, default=False):
    real = os.path.realpath(path)
    authed = os.path.exists(os.path.join(real, AUTH_FILE[provider]))
    identity = _IDENTITY[provider](real)
    canonical = None
    if identity and not default:  # the default home has no canonical-name rule
        canonical = canonical_name(identity) == os.path.basename(real)
    projects_link_ok = None
    if provider == "claude":
        if default:
            projects_link_ok = True  # ~/.claude/projects IS the shared store
        else:
            pl = os.path.join(real, "projects")
            projects_link_ok = (os.path.islink(pl)
                                and os.path.realpath(pl) == os.path.realpath(SHARED_PROJECTS))
    live = sorted(pid for pid, prov, h in procs
                  if prov == provider and (h == real or (h is None and default)))
    return {"name": f"(default-{provider})" if default else os.path.basename(path),
            "provider": provider, "path": path, "default": default, "aliases": aliases,
            "authed": authed, "identity": identity, "canonical": canonical,
            "family": _token_family(provider, real) if authed else None,
            "projects_link_ok": projects_link_ok, "live_pids": live, "archived": False}


def _archive_marker(path):
    """(meta, marker_file) — helm marker wins, legacy marker still honored."""
    for marker in (MARKER, LEGACY_MARKER):
        meta = _read_json(os.path.join(path, marker))
        if meta is not None:
            return meta, marker
    return {}, None


def _archive_dirs():
    """Every archived-home dir across BOTH archive roots (helm first, then legacy)."""
    out = []
    for root in (ARCHIVE_ROOT, LEGACY_ARCHIVE_ROOT):
        if not root:
            continue
        for p in sorted(glob.glob(os.path.join(root, "*"))):
            if os.path.isdir(p) and not os.path.islink(p):
                out.append(p)
    return out


def _archived_rows():
    rows = []
    for p in _archive_dirs():
        meta, _ = _archive_marker(p)
        prov = meta.get("provider") or _detect_provider(p)
        rows.append({"name": meta.get("name") or os.path.basename(p),
                     "provider": prov, "path": p, "default": False, "aliases": [],
                     "authed": bool(prov) and os.path.exists(os.path.join(p, AUTH_FILE.get(prov, "\0"))),
                     "identity": _IDENTITY[prov](p) if prov in _IDENTITY else None,
                     "canonical": None, "projects_link_ok": None, "live_pids": [],
                     "archived": True, "archived_at": meta.get("archived_at")})
    return rows


def homes_list():
    """EVERY home: real dirs under ~/.claude-homes + ~/.codex-homes (aliases folded
    onto their target row), the default ~/.claude / ~/.codex, and both archives.
    No-auth homes included — they never appear in quota providers, but they exist."""
    procs = _agent_procs()
    rows, broken = [], []
    for provider, root in ROOTS.items():
        aliases, reals = {}, []
        for p in sorted(glob.glob(os.path.join(root, "*"))):
            if os.path.islink(p):
                if os.path.isdir(p):
                    aliases.setdefault(os.path.realpath(p), []).append(os.path.basename(p))
                else:
                    broken.append({"name": os.path.basename(p), "provider": provider,
                                   "path": p, "broken_alias": True,
                                   "target": os.readlink(p), "archived": False})
            elif os.path.isdir(p):
                reals.append(p)
        seen_inodes = set()
        for p in reals:
            st = os.stat(p)
            seen_inodes.add((st.st_dev, st.st_ino))
            rows.append(_home_row(provider, p, aliases.get(os.path.realpath(p), []), procs))
        d = DEFAULTS[provider]
        if os.path.isdir(d):
            st = os.stat(os.path.realpath(d))
            if (st.st_dev, st.st_ino) not in seen_inodes:  # default may symlink onto a home
                rows.append(_home_row(provider, d, [], procs, default=True))
    # duplicate-identity scan: one identity should hold ONE home per provider
    by_ident = {}
    for r in rows:
        if r["authed"] and r["identity"]:
            by_ident.setdefault((r["provider"], r["identity"]), []).append(r)
    for group in by_ident.values():
        if len(group) > 1:
            for r in group:
                r["duplicate_identity"] = [o["name"] for o in group if o is not r]
    # shared-family scan: byte-identical refresh tokens across DISTINCT homes =
    # copies of ONE token family — the revocation bomb (reuse detection revokes
    # the whole family at once). Keyed on CONTENT hashes; dir names are labels.
    by_family = {}
    for r in rows:
        if r["authed"] and r.get("family"):
            by_family.setdefault((r["provider"], r["family"]), []).append(r)
    for group in by_family.values():
        if len(group) > 1:
            for r in group:
                r["shared_family"] = [o["name"] for o in group if o is not r]
    return rows + broken + _archived_rows()


def _resolve(name, provider=None):
    """Home row by name, alias name, or path. Ambiguity (same name in both provider
    roots) is an ERROR demanding a provider — a mutation must never guess."""
    name = (name or "").strip().rstrip("/")
    if not name:
        return None, {"error": "need a home name (see `helm homes`)"}
    if provider is not None and provider not in ROOTS:
        return None, {"error": f"unknown provider {provider!r} (claude | codex)"}
    rows = [r for r in homes_list() if not r["archived"] and not r.get("broken_alias")]
    want = os.path.realpath(os.path.expanduser(name)) if os.path.sep in name else None
    hits = [r for r in rows if provider in (None, r["provider"])
            and (r["name"] == name or name in r["aliases"]
                 or (want and os.path.realpath(r["path"]) == want))]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, {"error": f"ambiguous: {name} matches "
                               + ", ".join(f"{r['provider']}:{r['name']}" for r in hits)
                               + " — pass a provider"}
    return None, {"error": f"unknown home {name!r} (see `helm homes`)"}


def _link_deck(deck, skills_dir):
    """Symlink each SKILL.md-bearing dir of the deck into skills_dir. Additive
    only: an existing entry (link or real dir) is never touched — replacing a
    diverged copy is the deck's own deploy tool's job, not home creation's.
    -> (linked, skipped_existing)."""
    os.makedirs(skills_dir, exist_ok=True)
    linked = skipped = 0
    for name in sorted(os.listdir(deck)):
        src = os.path.join(deck, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        dst = os.path.join(skills_dir, name)
        if os.path.lexists(dst):
            skipped += 1
            continue
        os.symlink(src, dst)
        linked += 1
    return linked, skipped


def home_create(provider, account_email):
    """Prepare a home for a fresh device login. mkdir + (claude) the projects symlink
    + (claude) the HELM_SKILL_DECK symlink farm, then hand the human the exact
    login command. NEVER seats credentials itself."""
    provider = (provider or "").strip().lower()
    if provider not in ROOTS:
        return {"error": f"provider must be claude or codex (got {provider!r})"}
    email = (account_email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return {"error": f"need the full account email (got {account_email!r}) — "
                         "the canonical home name derives from it"}
    name = canonical_name(email)
    for r in homes_list():
        if r["archived"] or r.get("broken_alias") or r["provider"] != provider:
            continue
        if r["default"]:
            # an identity seated in the provider DEFAULT may also get a named
            # home — that IS the orchestrator compromise (default = managed
            # cred for env-less launches; named home = pinned processes)
            continue
        if r["authed"] and r["identity"] == email:
            hint = "" if r["canonical"] in (True, None) else \
                f" (its name is non-canonical — `helm homes migrate {r['name']}` fixes that)"
            return {"error": f"{email} is already seated in {r['path']} (home {r['name']})"
                             f" — one home = one login; use that home{hint}"}
    home = os.path.join(ROOTS[provider], name)
    existing = os.path.isdir(home) and not os.path.islink(home)
    if not existing and os.path.lexists(home):
        return {"error": f"{home} exists but is not a plain directory — inspect it by hand"}
    if existing:
        held = _IDENTITY[provider](home)
        if held and held != email:
            return {"error": f"{home} already holds {held} — one home = one login; "
                             "archive it first or pick the right email"}
    os.makedirs(home, mode=0o700, exist_ok=True)
    notes = []
    if provider == "claude":
        # one shared session store: a real projects dir here would strand sessions
        os.makedirs(SHARED_PROJECTS, exist_ok=True)
        pl = os.path.join(home, "projects")
        if os.path.islink(pl):
            if os.path.realpath(pl) != os.path.realpath(SHARED_PROJECTS):
                os.remove(pl)
                os.symlink(SHARED_PROJECTS, pl)
                notes.append("re-pointed the projects symlink at the shared store")
        elif os.path.isdir(pl):
            return {"error": f"{pl} is a REAL directory — sessions born there are stranded; "
                             f"merge its contents into {SHARED_PROJECTS} and replace it with "
                             "a symlink before using this home"}
        elif os.path.lexists(pl):
            return {"error": f"{pl} exists and is neither a directory nor a symlink"}
        else:
            os.symlink(SHARED_PROJECTS, pl)
        # every home loads the same best setup, whatever cred it carries:
        # symlink the canonical skill deck (HELM_SKILL_DECK, a dir of skill
        # dirs) so a new home is never born with a stale subset. No deck
        # configured = nothing to provision; helm never guesses a local path.
        deck = _env("SKILL_DECK")
        deck = os.path.realpath(os.path.expanduser(deck)) if deck else None
        if deck and os.path.isdir(deck):
            linked, skipped = _link_deck(deck, os.path.join(home, "skills"))
            if linked or skipped:
                notes.append(f"skill deck: linked {linked}"
                             + (f", left {skipped} existing" if skipped else ""))
    if existing:
        notes.append("home already existed (idempotent — nothing was overwritten)")
    return {"home": home, "name": name, "provider": provider, "existing": existing,
            "login_cmd": LOGIN_CMDS[provider](home),
            "next": "run the login command in YOUR terminal, approve in the browser, "
                    f"then: helm homes verify {name} --provider {provider}",
            "note": "; ".join(notes) or None}


def home_verify(name, provider=None):
    """Post-login check: authed, identity, canonical name, projects link, duplicate
    identities. Verdict + concrete fixes — helm flags, the human (or a verb) fixes."""
    row, err = _resolve(name, provider)
    if err:
        return err
    prov, real = row["provider"], os.path.realpath(row["path"])
    fixes = []
    if not row["authed"]:
        fixes.append("not logged in — run (human-only): " + LOGIN_CMDS[prov](real))
    elif not row["identity"]:
        fixes.append("authed but identity unreadable — "
                     + (".claude.json has no oauthAccount yet; open the agent once"
                        if prov == "claude" else "auth.json id_token carries no email claim"))
    if row["canonical"] is False:
        fixes.append(f"dir name lies: identity {row['identity']} wants "
                     f"{canonical_name(row['identity'])} — `helm homes migrate {row['name']}`")
    if prov == "claude" and row["projects_link_ok"] is False:
        pl = os.path.join(real, "projects")
        if os.path.isdir(pl) and not os.path.islink(pl):
            fixes.append(f"projects is a REAL dir — sessions born here are STRANDED; merge "
                         f"{pl}/* into {SHARED_PROJECTS}, then: ln -sfn {SHARED_PROJECTS} {pl}")
        elif os.path.islink(pl):
            fixes.append(f"projects symlink points at {os.path.realpath(pl)}, not the shared "
                         f"store — re-point: ln -sfn {SHARED_PROJECTS} {pl}")
        else:
            fixes.append(f"projects link missing — create: ln -s {SHARED_PROJECTS} {pl}")
    dups = row.get("duplicate_identity") or []
    # default-home sharing is the orchestrator pattern (see _hygiene_flags) —
    # only named-home <-> named-home duplication demands a survivor
    named_dups = [d for d in dups if not d.startswith("(default-")] \
        if not row["default"] else []
    if named_dups:
        fixes.append(f"identity {row['identity']} also lives in: {', '.join(named_dups)} — one "
                     "identity should hold ONE named home; the human picks a survivor and archives "
                     "the rest (`helm homes archive`) — NEVER copy credentials between homes")
    fam = row.get("shared_family") or []
    if fam:
        fixes.append(f"BYTE-COPIES of one refresh-token family with: {', '.join(fam)} — "
                     "reuse detection revokes the WHOLE family at once; a fresh login per "
                     "home is the only fix (one home = one login = one token family)")
    verdict = "pending-login" if not row["authed"] else ("issues" if fixes else "ok")
    return {"home": row["name"], "provider": prov, "path": row["path"],
            "checks": {"authed": row["authed"], "identity": row["identity"],
                       "canonical": row["canonical"],
                       "projects_link_ok": row["projects_link_ok"],
                       "duplicate_identity": dups, "shared_family": fam,
                       "live_pids": row["live_pids"]},
            "verdict": verdict, "fixes": fixes}


def home_archive(name, provider=None):
    """Move a home into ~/.helm-home-archive/<name>-<YYYYMMDD>/ — reversible, never a
    delete. Refuses defaults and any home with a live agent (the /proc live-scan)."""
    row, err = _resolve(name, provider)
    if err:
        return err
    if row["default"]:
        return {"error": "REFUSED: the default home is the harness's fallback"
                         + (" and holds the shared session store" if row["provider"] == "claude" else "")
                         + " — it is not archivable"}
    if row["live_pids"]:
        return {"error": f"REFUSED: {len(row['live_pids'])} live {row['provider']} agent(s) "
                         f"on {row['name']} (pids {', '.join(map(str, row['live_pids']))}) — "
                         "end or park those panes first, then archive"}
    real = os.path.realpath(row["path"])
    os.makedirs(ARCHIVE_ROOT, mode=0o700, exist_ok=True)
    dest = os.path.join(ARCHIVE_ROOT, f"{row['name']}-{time.strftime('%Y%m%d')}")
    if os.path.lexists(dest):
        dest += time.strftime("-%H%M%S")
    shutil.move(real, dest)
    removed = []  # alias symlinks now dangle — remove them, remember them for restore
    for a in row["aliases"]:
        ap = os.path.join(ROOTS[row["provider"]], a)
        if os.path.islink(ap):
            os.remove(ap)
            removed.append(a)
    with open(os.path.join(dest, MARKER), "w") as f:
        json.dump({"name": row["name"], "provider": row["provider"], "from": real,
                   "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "aliases": removed}, f, indent=1)
    return {"ok": True, "archived_to": dest, "aliases_removed": removed,
            "note": f"reversible: helm homes restore {row['name']}"}


def home_unarchive(name):
    """Restore an archived home to where it came from (newest archive wins).
    Searches BOTH archive roots — helm and the legacy one."""
    name = (name or "").strip()
    if not name:
        return {"error": "need a home name (see `helm homes archives`)"}
    cands = []
    for p in _archive_dirs():
        meta, marker = _archive_marker(p)
        if os.path.basename(p) == name or meta.get("name") == name:
            cands.append((p, meta, marker))
    if not cands:
        where = str(ARCHIVE_ROOT)
        if LEGACY_ARCHIVE_ROOT:
            where += f" or {LEGACY_ARCHIVE_ROOT}"
        return {"error": f"nothing archived under {name!r} in {where}"}
    p, meta, marker = cands[-1]  # date-suffixed names sort oldest-first per root
    prov = meta.get("provider") or _detect_provider(p)
    dest = meta.get("from") or (os.path.join(ROOTS[prov], meta.get("name", name))
                                if prov in ROOTS else None)
    if not dest:
        return {"error": f"cannot determine the restore path for {p} "
                         "(no marker and provider undetectable) — restore by hand"}
    if os.path.lexists(dest):
        return {"error": f"restore target {dest} already exists — resolve that first"}
    shutil.move(p, dest)
    for m in (marker,) if marker else ():
        try:
            os.remove(os.path.join(dest, m))
        except OSError:
            pass
    restored = []
    for a in meta.get("aliases", []):
        ap = os.path.join(ROOTS[prov], a)
        if not os.path.lexists(ap):
            os.symlink(dest, ap)
            restored.append(a)
    return {"ok": True, "restored_to": dest, "aliases_restored": restored}


def home_migrate(name, provider=None):
    """Fix a name-lies home: rename the dir to the canonical name for its identity and
    leave an alias symlink at the old path. Never touches credentials; refuses live."""
    row, err = _resolve(name, provider)
    if err:
        return err
    if row["default"]:
        return {"error": "the default home has no canonical-name rule — nothing to migrate"}
    if not row["identity"]:
        return {"error": f"{row['name']} has no readable identity — migrate derives the "
                         "canonical name from it (log in first, or archive the home)"}
    want = canonical_name(row["identity"])
    real = os.path.realpath(row["path"])
    if os.path.basename(real) == want:
        return {"ok": True, "path": real,
                "note": f"{row['name']} is already canonical for {row['identity']}"}
    if row["live_pids"]:
        return {"error": f"REFUSED: {len(row['live_pids'])} live {row['provider']} agent(s) "
                         f"on {row['name']} (pids {', '.join(map(str, row['live_pids']))}) — "
                         "their env points at the old path; end or park them first"}
    target = os.path.join(ROOTS[row["provider"]], want)
    if os.path.lexists(target):
        return {"error": f"{target} already exists — that is a duplicate-identity situation, "
                         "not a rename: the human picks a survivor and archives the other "
                         "(`helm homes archive`); credentials are never merged or copied"}
    os.rename(real, target)
    os.symlink(target, real)  # alias at the old path so nothing referencing it breaks
    return {"ok": True, "from": real, "to": target, "alias_left": real,
            "note": f"renamed to canonical {want}; the old name remains as an alias symlink"}


# ---------------------------------------------------------------- CLI leg

def _hygiene_flags(r):
    """Terse per-row audit column — the name-vs-login mismatch and friends."""
    flags = []
    if not r["authed"]:
        flags.append("no-auth")
    elif not r["identity"]:
        flags.append("identity?")
    if r["canonical"] is False:
        flags.append(f"name-lies(want {canonical_name(r['identity'])})")
    if r["projects_link_ok"] is False:
        flags.append("projects!")
    dups = r.get("duplicate_identity") or []
    # named-home + provider DEFAULT sharing an identity is the recognized
    # orchestrator pattern (the default carries orchestrator-managed cred for
    # processes launched without a home env; the named home pins the rest) —
    # describe it, don't alarm. Named-home <-> named-home stays a violation.
    named = [d for d in dups if not d.startswith("(default-")]
    if r["default"]:
        named = []  # the default's mirror flag is informational by the same rule
        if dups:
            flags.append("shares:" + ",".join(dups) + " (by design)")
    elif len(named) < len(dups):
        flags.append("shared-with-default (by design)")
    if named:
        flags.append("dup:" + ",".join(named))
    fam = r.get("shared_family") or []
    if fam:  # the revocation bomb — always the loudest flag
        flags.append("SHARED-FAMILY:" + ",".join(fam))
    if r["live_pids"]:
        flags.append("live:" + ",".join(map(str, r["live_pids"])))
    return flags


def _take_flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            return None
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def _fail(res):
    print("helm homes: " + res["error"], file=sys.stderr)
    return 1


def _print_list():
    rows = homes_list()
    live_rows = [r for r in rows if not r["archived"] and not r.get("broken_alias")]
    broken = [r for r in rows if r.get("broken_alias")]
    archived = [r for r in rows if r["archived"]]
    if not live_rows:
        print("helm homes: none — `helm homes prepare <claude|codex> <email>` starts one")
        return 0
    counts = {}
    for r in live_rows:
        counts[r["provider"]] = counts.get(r["provider"], 0) + 1
    print("helm homes: %d home%s (%s)" % (
        len(live_rows), "s"[:len(live_rows) != 1],
        ", ".join(f"{p} {n}" for p, n in sorted(counts.items()))))
    for r in sorted(live_rows, key=lambda r: (r["provider"], r["default"], r["name"])):
        flags = _hygiene_flags(r)
        alias = " [alias: %s]" % ",".join(r["aliases"]) if r["aliases"] else ""
        print("  %-7s %-28s %-30s %s%s" % (
            r["provider"], r["name"], r["identity"] or "-",
            "; ".join(flags) or "ok", alias))
    for r in broken:
        print("  %-7s %-28s broken alias -> %s" % (r["provider"], r["name"], r["target"]))
    if archived:
        print("  (+%d archived — `helm homes archives`)" % len(archived))
    return 0


def _print_archives():
    rows = _archived_rows()
    if not rows:
        print("helm homes: no archives (%s, %s)" % (ARCHIVE_ROOT, LEGACY_ARCHIVE_ROOT))
        return 0
    print("helm homes: %d archived (restore: helm homes restore <name>)" % len(rows))
    for r in rows:
        print("  %-7s %-28s %-30s %s" % (
            r["provider"] or "?", r["name"], r["identity"] or "-", r["path"]))
    return 0


def cmd_homes(args):
    """homes [prepare <provider> <email> | verify [<name>] | archive <name> |
    restore <name> | migrate <name> | archives] [--provider claude|codex]"""
    args = list(args)
    provider = _take_flag(args, "--provider")
    if not args:
        return _print_list()
    verb, rest = args[0], args[1:]
    if verb == "archives":
        return _print_archives()
    if verb == "prepare":
        if len(rest) < 2:
            print("usage: helm homes prepare <claude|codex> <email>", file=sys.stderr)
            return 2
        res = home_create(rest[0], rest[1])
        if "error" in res:
            return _fail(res)
        print("helm homes: prepared %s home %s%s" % (
            res["provider"], res["home"], " (already existed)" if res["existing"] else ""))
        if res.get("note"):
            print("  note: " + res["note"])
        print("  login (YOU run this): " + res["login_cmd"])
        print("  then: " + res["next"].split("then: ")[-1])
        return 0
    if verb == "verify":
        names = rest or [r["name"] for r in homes_list()
                         if not r["archived"] and not r.get("broken_alias") and not r["default"]]
        if not names:
            print("helm homes: nothing to verify")
            return 0
        worst = 0
        for n in names:
            res = home_verify(n, provider)
            if "error" in res:
                worst = max(worst, _fail(res))
                continue
            print("helm homes: %s/%s — %s (%s)" % (
                res["provider"], res["home"], res["verdict"],
                res["checks"]["identity"] or "no identity"))
            for f in res["fixes"]:
                print("  fix: " + f)
            worst = max(worst, 0 if res["verdict"] == "ok" else 1)
        return worst
    if verb == "archive":
        if not rest:
            print("usage: helm homes archive <name>", file=sys.stderr)
            return 2
        res = home_archive(rest[0], provider)
        if "error" in res:
            return _fail(res)
        print("helm homes: archived to %s (%s)" % (res["archived_to"], res["note"]))
        return 0
    if verb == "restore":
        if not rest:
            print("usage: helm homes restore <name>", file=sys.stderr)
            return 2
        res = home_unarchive(rest[0])
        if "error" in res:
            return _fail(res)
        print("helm homes: restored to %s" % res["restored_to"])
        return 0
    if verb == "migrate":
        if not rest:
            print("usage: helm homes migrate <name>", file=sys.stderr)
            return 2
        res = home_migrate(rest[0], provider)
        if "error" in res:
            return _fail(res)
        print("helm homes: " + (res.get("note") or "ok"))
        return 0
    print("helm homes: unknown subverb '%s' (prepare <provider> <email>|"
          "verify [<name>]|archive <name>|restore <name>|migrate <name>|"
          "archives)" % verb, file=sys.stderr)
    return 2
