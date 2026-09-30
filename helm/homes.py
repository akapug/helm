#!/usr/bin/env python3
"""helm homes — credential-home lifecycle for claude AND codex (stdlib only).
helm-native
dissolve-into-helm law; rebrand only — new archives land in
~/.helm-home-archive/; where this host's local names declare a predecessor
(helm/localnames.py), its archive stays readable/restorable.

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
    folded to '-'  (ann@example.com -> ann-example-com). Aliases = symlinks.
  * claude homes: `projects` must SYMLINK to ~/.claude/projects (one shared
    session store) — a REAL projects dir silently strands sessions.
  * archive = MOVE into ~/.helm-home-archive/<name>-<date>/ (reversible),
    never delete; refused while a live agent sits on the home. A declared
    predecessor's archives (~/.<name>-home-archive/, marker .<name>-archive.json)
    are still listed and restorable — read both, write new.

Every public function returns a JSON-able dict (or list); errors are
{"error": "..."} — loud, attributed, never an exception across the API edge.
"""
import base64, collections, glob, hashlib, json, os, shlex, shutil, sys, time

from .home import env as _env
from . import localnames, pk

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
# the predecessor's archive root — recognized for list/restore, never written
# to. None on a host whose local names declare no predecessor.
LEGACY_ARCHIVE_ROOT = localnames.legacy_path(".{}-home-archive")
LEGACY_MARKER = (".%s-archive.json" % localnames.predecessor()
                 if localnames.predecessor() else None)

# the human runs these; helm only prints them (logins are human-only, per canon)
LOGIN_CMDS = {"claude": lambda h: f"CLAUDE_CONFIG_DIR={shlex.quote(h)} claude /login",
              "codex": lambda h: f"CODEX_HOME={shlex.quote(h)} codex login --device-auth"}


def canonical_name(email):
    """ann@example.com -> ann-example-com (same fold as the predecessor's _norm — keep them in step)."""
    return "".join(ch if ch.isalnum() else "-" for ch in (email or "").lower()).strip("-")


def _read_json(path):
    try:
        with pk.open_regular(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _claude_identity(home):
    """oauthAccount email from a home's .claude.json — identity METADATA, never
    tokens. ONE content reader for the whole repo: cred.account_of (mtime-cached,
    fail-closed) so `helm cred`, this row, launch and doctor can never disagree
    about which account a home's metadata names."""
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
    """(meta, marker_file) — helm marker wins, a predecessor's marker still honored."""
    for marker in (MARKER, LEGACY_MARKER):
        if not marker:
            continue
        meta = _read_json(os.path.join(path, marker))
        if meta is not None:
            return meta, marker
    return {}, None


def _archive_roots():
    """helm's archive root, then a declared predecessor's."""
    return tuple(r for r in (ARCHIVE_ROOT, LEGACY_ARCHIVE_ROOT) if r)


def _archive_dirs():
    """Every archived-home dir across BOTH archive roots (helm first, then legacy)."""
    out = []
    for root in _archive_roots():
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
    # the homes card shows this when a row went away under it (archived from
    # elsewhere); it says so in his words, never the verb (task/3735)
    return None, {"error": f"unknown home {name!r} (see `helm homes`)",
                  "owner_error": f"no home named {name} is here now; reopen "
                                 "this card to read the homes again"}


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


def _mcp_source():
    """The mcpServers the provider default home runs with, from its STATE
    FILE as physics resolves it. The default home is the one place the trap
    lives: run without CLAUDE_CONFIG_DIR it keeps state in the SIBLING
    ~/.claude.json, not in ~/.claude/.claude.json. Reading only the inside
    file found zero servers on a host whose sibling declared six, so a home
    prepared there would get none while the note said the source was empty.
    Raises ValueError when a state file exists and none could be read."""
    from . import physics
    state, path, warn = physics._claude_state_file(DEFAULTS["claude"])
    if path is None and warn:
        raise ValueError(warn)
    servers = state.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _mcp_provision(home):
    """Give a home the SAME MCP server set the provider default carries.

    WHY THIS IS AN ENTRY IN BENEFITS AND NOT SOMEWHERE ELSE: `home_create`
    states the rule — *every home loads the same best setup, whatever cred it
    carries* — and provisions the shared session store, the skills hub and the
    skill deck under it. The MCP set was simply never a member of that list, so a credhome seat launched with a pinned token came up
    without its MCP servers (cv, exa, fetch and the rest) while the default home had
    all of them. Nothing errors: the tools are absent, the seat does not know
    what it is missing, and the only symptom is work it cannot do.

    COUNT THE HOMES BY INODE, NOT BY NAME, when auditing this: names under the
    root include symlinks onto other homes, so a per-NAME tally overcounts the
    population and reports drift for a home that is another home.

    THE SOURCE IS THE PROVIDER DEFAULT HOME, deliberately, and not a new config
    key: it is the set the owner actually sees and edits, so there is exactly one
    place to change and no second list to drift. ADDITIVE ONLY — a name the home
    already defines is left exactly as it is, because a home may legitimately
    pin a different command or env for the same server and this function is
    provisioning, never reconciliation. An EMPTY source writes nothing at all:
    propagating emptiness would silently strip a home that has its own entries.
    """
    try:
        want = _mcp_source()
    except ValueError as e:
        return "source-unreadable", str(e)
    if not want:
        return "source-empty", ""
    from . import projectmcp
    path = os.path.join(home, ".claude.json")
    if os.path.islink(path):
        return "refused", "%s is a symlink (a rewrite would cut it)" % path
    missing = []

    def change(cur):
        # recomputed on every compare-and-swap try, on the file as it is then
        have = cur.get("mcpServers")
        have = have if isinstance(have, dict) else {}
        missing[:] = [k for k in want if k not in have]
        if not missing:
            return None
        merged = dict(have)
        for k in missing:
            merged[k] = want[k]
        cur["mcpServers"] = merged
        return True

    def backup(p):
        # THE BACKUP SITS BESIDE THE FILE, named the way the hand cure of the
        # live fleet named it, so a rewrite is always one `mv` from undone. A
        # new file has nothing to keep.
        if os.path.exists(p):
            shutil.copy2(p, _beside(p, "bak-mcp"))

    # the ONE state writer (projectmcp.update_state), a compare-and-swap: a
    # running session's own write between our read and our replace is kept,
    # and a file that keeps changing is refused with nothing written
    try:
        _r, wrote = projectmcp.update_state(path, change, before_write=backup)
    except ValueError:
        return "unreadable", path
    except projectmcp.HomeHeld as e:
        return "held", str(e)
    except OSError as e:
        return "error", str(e)
    if not wrote:
        return "ok", str(len(want))
    return "linked", ", ".join(sorted(missing))


_MCP_NOTE = {
    "linked": "mcp servers added: %s",
    "ok": "mcp servers already complete (%s)",
    "source-unreadable": "mcp servers NOT provisioned: the default home's state "
                         "is unreadable (%s) — nothing copied",
    "source-empty": "mcp servers NOT provisioned: the default home declares none "
                    "— nothing to copy, and helm never invents a server list",
    "unreadable": "mcp servers NOT provisioned: %s is unreadable or not an object "
                  "— left untouched rather than overwritten",
    "refused": "mcp servers NOT provisioned: %s — left untouched",
    "held": "mcp servers NOT provisioned: %s",
    "error": "mcp servers NOT provisioned (%s) — sessions here see no MCP tools",
}


_SKILLS_NOTE = {
    "linked": "skills -> %s",
    "ok": "skills already -> %s",
    "indirect": "skills -> %s resolves to the hub through another path — left "
                "untouched; `helm skills sync --apply` makes it direct",
    "foreign": "skills -> %s is NOT the skills hub — left untouched; "
               "`helm skills sync --apply` normalizes it",
    "unavailable": "skills hub UNAVAILABLE (%s) — this home has NO skills; "
                   "restore the hub, then `helm skills sync --apply`",
    "real": "%s is a REAL dir — left untouched; `helm skills sync --apply` "
            "folds it into the hub",
    "error": "skills NOT linked (%s) — sessions here see no helm skills until "
             "`helm skills sync --apply`",
}



def _beside(path, tag):
    """A free sibling name `<path>.<tag>-<UTC stamp>` for a backup."""
    base = "%s.%s-%s" % (path, tag, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    out, n = base, 1
    while os.path.lexists(out):
        out, n = "%s.%d" % (base, n), n + 1
    return out


def _is_default(home):
    return os.path.realpath(home) == os.path.realpath(DEFAULTS["claude"])


# --------------------------------------------------------- the benefit list
#
# WHAT A CLAUDE CREDENTIAL HOME CARRIES, AS ONE LIST. Before this list the
# answer lived in the order of statements inside home_create: each benefit
# was there because one person added it, and nothing said what the whole set
# was. Two readers walk this list and no other: `provision` (what home_create
# runs) and `benefit_drift` (what `helm doctor` prints for every existing
# home). A benefit added here reaches every new home and is reported missing
# on every old home, with no other code change.
#
# Each entry:
#   name       the word the notes and the drift report use.
#   source     where the benefit comes from: the thing a home is compared to.
#   provision  fn(home) -> (notes, error). ADDITIVE ONLY: a value the home
#              already holds is never replaced. `error` stops the pass (a
#              home that would strand sessions must not be handed out).
#              None means this pass does not write it; see `remedy`.
#   missing    fn(home) -> None when the home carries it, else one phrase
#              saying what is absent. It RAISES when it cannot tell, and the
#              drift report keeps "cannot tell" apart from "missing".
#   default    True when the provider default home (~/.claude) must carry it
#              too. False for benefits the default IS the source of.
#   remedy     the command that closes the gap on an existing home.
#   launch     True when `helm launch` also runs it on every home it wires:
#              only an entry that writes settings.json alone, is additive and
#              is a pure read once the home carries it. False by default.
#
# Codex homes carry none of these today: each entry is a claude-home fact.

def _prov_projects(home):
    """One shared session store: a real projects dir here would strand
    sessions. A link elsewhere is re-pointed (the one non-additive write
    here, and the one home_create always made: a link is not content)."""
    os.makedirs(SHARED_PROJECTS, exist_ok=True)
    pl = os.path.join(home, "projects")
    if os.path.islink(pl):
        if os.path.realpath(pl) != os.path.realpath(SHARED_PROJECTS):
            os.remove(pl)
            os.symlink(SHARED_PROJECTS, pl)
            return ["re-pointed the projects symlink at the shared store"], None
        return [], None
    if os.path.isdir(pl):
        return [], (f"{pl} is a REAL directory — sessions born there are stranded; "
                    f"merge its contents into {SHARED_PROJECTS} and replace it with "
                    "a symlink before using this home")
    if os.path.lexists(pl):
        return [], f"{pl} exists and is neither a directory nor a symlink"
    os.symlink(SHARED_PROJECTS, pl)
    return [], None


def _miss_projects(home):
    pl = os.path.join(home, "projects")
    if os.path.islink(pl) and \
            os.path.realpath(pl) == os.path.realpath(SHARED_PROJECTS):
        return None
    what = ("links %s" % os.path.realpath(pl) if os.path.islink(pl) else
            "is a REAL dir" if os.path.isdir(pl) else "is missing")
    return "projects %s, not the shared session store %s" % (what, SHARED_PROJECTS)


def _prov_skills_hub(home):
    """Canonical first: the skills hub (skillsync.canonical) is the one set
    every config dir on this host shares, and a home born without `skills ->
    hub` loses every skill silently. The link is minted by the same primitive
    seat mint uses. An existing home is never rewired here (a link elsewhere
    is named, not replaced; a REAL dir is left whole) — `helm skills sync
    --apply` is that repair."""
    from . import skillsync
    res = skillsync.link_canonical(home, relink=False)
    notes = []
    if res.action in _SKILLS_NOTE:
        notes.append(_SKILLS_NOTE[res.action] % res.detail)
    notes += [line for line in (skillsync.failure_line(res),
                                skillsync.degraded_line(res)) if line]
    return notes, None


def _miss_skills_hub(home):
    """No hub configured is nothing to carry. An unreadable authored layer
    raises (skillsync.canonical refuses), so it reads as cannot-tell."""
    from . import skillsync
    canon = skillsync.canonical()
    if not canon:
        return None
    sdir = os.path.join(home, "skills")
    if os.path.realpath(sdir) == os.path.realpath(canon):
        return None
    what = ("links %s" % os.readlink(sdir) if os.path.islink(sdir) else
            "is a REAL dir" if os.path.isdir(sdir) else "is missing")
    return "skills %s, not the hub %s" % (what, canon)


def _deck():
    deck = _env("SKILL_DECK")
    deck = os.path.realpath(os.path.expanduser(deck)) if deck else None
    return deck if deck and os.path.isdir(deck) else None


def _prov_skill_deck(home):
    """No hub on this host: symlink the canonical skill deck
    (HELM_SKILL_DECK, a dir of skill dirs) so a new home is never born with a
    stale subset. No deck configured = nothing to provision; helm never
    guesses a local path. A home whose skills/ is ANY symlink takes no deck —
    the hub, an indirect hub link, or a foreign target alike — since a deck
    entry symlinked through that link is written INTO whatever the link
    names, never into this home."""
    deck = _deck()
    if os.path.islink(os.path.join(home, "skills")) or not deck:
        return [], None
    linked, skipped = _link_deck(deck, os.path.join(home, "skills"))
    if not (linked or skipped):
        return [], None
    return [f"skill deck: linked {linked}"
            + (f", left {skipped} existing" if skipped else "")], None


def _miss_skill_deck(home):
    """What the pass would farm: nothing through a skills link, and nothing
    into an ABSENT skills/ that the hub step will link first."""
    from . import skillsync
    deck = _deck()
    sdir = os.path.join(home, "skills")
    if not deck or os.path.islink(sdir):
        return None
    if not os.path.lexists(sdir):
        canon = skillsync.canonical()
        if canon and os.path.isdir(canon):
            return None
    want = sorted(n for n in os.listdir(deck)
                  if os.path.isfile(os.path.join(deck, n, "SKILL.md")))
    gone = [n for n in want if not os.path.lexists(os.path.join(sdir, n))]
    return ("deck skills absent: %s" % ", ".join(gone)) if gone else None


def _prov_mcp(home):
    action, detail = _mcp_provision(home)
    note = _MCP_NOTE.get(action)
    return ([note % detail if "%s" in note else note] if note else []), None


def _miss_mcp(home):
    """Server NAMES the default home declares and this home does not. A home
    before its first login has no .claude.json and so lacks all of them; a
    file that will not parse raises."""
    want = _mcp_source()
    path = os.path.join(home, ".claude.json")
    have = {}
    if os.path.lexists(path):
        body = _read_json(path)
        if not isinstance(body, dict):
            raise ValueError("%s is unreadable or not an object" % path)
        have = body.get("mcpServers")
        have = have if isinstance(have, dict) else {}
    gone = sorted(k for k in want if k not in have)
    return ("servers absent: %s" % ", ".join(gone)) if gone else None


def _approval_gaps(home):
    """[(project key, [names])] the home's TRUSTED projects read from their
    `.mcp.json` and the home has not approved (projectmcp.recorded_plans).
    Raises ValueError when the state file cannot be read or extended."""
    from . import projectmcp
    gaps = []
    for p in projectmcp.recorded_plans(home):
        if p["error"]:
            raise ValueError(p["error"])
        gaps += p["adds"]
    return gaps


def _prov_project_mcp(home):
    """Approve, in the home's `.claude.json`, the project `.mcp.json` servers
    of every project the home already trusts (task/2698, task/2692): the
    backfill of what a launch approves for its own cwd. Additive; a name the
    home rejected stays rejected; an unreadable file is named, never
    rewritten."""
    from . import projectmcp
    try:
        plans = projectmcp.recorded_plans(home)
    except ValueError as e:
        return ["project mcp approvals NOT written: %s — left untouched" % e], None
    notes = []
    for p in plans:
        notes += p["warnings"]
        verdict, detail = projectmcp.apply(p)
        if verdict == "applied":
            notes.append("project mcp servers approved: %s" % detail)
        elif verdict == "FAIL":
            notes.append("project mcp approvals NOT written: %s" % detail)
    return notes, None


def _miss_project_mcp(home):
    gaps = _approval_gaps(home)
    return ("unapproved project servers: %s" % "; ".join(
        "%s (%s)" % (k, ", ".join(g)) for k, g in gaps)) if gaps else None


def _instructions_carried(home):
    """True when the home's OWN CLAUDE.md already is the global instructions
    (the same file, or the same bytes): the rules link would load them twice."""
    from . import skillsync
    try:
        src, _named = skillsync.instructions_canonical()
        own = os.path.join(home, "CLAUDE.md")
        if not src or not os.path.isfile(own) or not os.path.isfile(src):
            return False
        if os.path.realpath(own) == os.path.realpath(src):
            return True
        with open(own, "rb") as a, open(src, "rb") as b:
            return a.read() == b.read()
    except Exception:
        return False


def _prov_instructions(home):
    """A credhome session reads its OWN config dir's user memory, never
    ~/.claude/CLAUDE.md, so without this link a seat on a credhome runs with
    none of the host's global instructions (task/3089). The link is
    skillsync.link_instructions', the primitive seat mint and `helm tidy`
    use; the home's own CLAUDE.md is never touched."""
    from . import skillsync
    if _instructions_carried(home):
        return [], None
    res = skillsync.link_instructions(home)
    if res.action in ("linked", "relinked"):
        return ["global instructions linked: %s -> %s"
                % (res.link, os.readlink(res.link))], None
    if res.action in ("ok", "none"):
        return [], None
    return ["global instructions NOT linked (%s)" % res.detail], None


def _miss_instructions(home):
    from . import skillsync
    if _instructions_carried(home):
        return None
    res = skillsync.link_instructions(home, apply=False)
    if res.action in ("ok", "none"):
        return None
    if res.action in ("unavailable", "error"):
        raise ValueError(res.detail)
    if res.action == "would-link":
        return "%s absent (source %s)" % (skillsync.INSTRUCTIONS_LINK, res.detail)
    if res.action == "would-relink":
        return "%s %s" % (skillsync.INSTRUCTIONS_LINK, res.detail)
    return res.detail


def reconcile_seat_home(home, cwd=None):
    """THE SEAT-HOME RECONCILE a launch runs on the credhome it execs on: the
    same BENEFITS writers `helm homes provision --apply` runs (the default
    home's user-scope MCP servers, the global instructions link), plus the
    approval of the launch cwd's project `.mcp.json` servers. -> [lines]: only
    what changed or could not be done; a home already carrying all of it is
    silent. Never raises — a launch is never stopped by this."""
    from . import projectmcp
    lines = []
    try:
        if not _proxy_home(home):
            action, detail = _mcp_provision(home)
            if action not in ("ok", "source-empty"):
                note = _MCP_NOTE[action]
                lines.append(note % detail if "%s" in note else note)
            lines += _prov_instructions(home)[0]
    except Exception as e:
        lines.append("seat home not reconciled (%s: %s)"
                     % (e.__class__.__name__, e))
    if cwd:
        lines += projectmcp.reconcile(home, cwd)
    return lines


# OWNER RULING (premise opus-agents-xhigh-ultracode-subagents-for-same-
# model): every Opus agent runs at xhigh effort with ultracode on. Two
# settings.json keys carry it (measured on Claude Code 2.1.280): "ultracode"
# true, and the per-model effort under modelSettings. Each row is (key path,
# value).
#
# NOT hooks.ESTATE_DEFAULTS, for two reasons. That table is AUTHORITATIVE (it
# rewrites a different value to its own), and these keys are ADDITIVE: a home
# that sets another effort keeps it. And that table reaches the proxy seat
# config dirs too, which must get neither key: automatic fan-out on a limited
# family's credentials is the thing the ruling excludes.
SETTINGS_DEFAULTS = (
    (("ultracode",), True),
    (("modelSettings", "claude-opus-5-5", "effortLevel"), "xhigh"),
)


def _proxy_home(home):
    """A proxy family's seat config dir (<helm>/_global/seats/<family>/claude,
    or .../instances/<seat>/claude): the test the config write gate already
    uses to admit those dirs, reused so the two can never disagree."""
    from .configs import _is_seat_home
    return _is_seat_home(home)


def _settings_gaps(body):
    """-> (absent, blocked). `absent` is [(key path, value)] for each
    SETTINGS_DEFAULTS row whose key the body does not set. `blocked` is [key
    path] for a row whose parent is present but is not an object, so adding
    the key would replace a value the home set."""
    absent, blocked = [], []
    for keys, value in SETTINGS_DEFAULTS:
        node = body
        for k in keys[:-1]:
            node = node.get(k, {})
            if not isinstance(node, dict):
                blocked.append(keys)
                break
        else:
            if keys[-1] not in node:
                absent.append((keys, value))
    return absent, blocked


def _settings_body(home):
    """(path, body) of a home's settings.json; body {} when there is no file
    yet, None when the file is unreadable or not an object."""
    path = os.path.join(home, "settings.json")
    if not os.path.lexists(path):
        return path, {}
    body = _read_json(path)
    return path, (body if isinstance(body, dict) else None)


def _prov_settings_defaults(home):
    """Add each SETTINGS_DEFAULTS key the home's settings.json lacks. A key the
    home sets, to any value, is kept, and every other key survives. A proxy
    seat's config dir is left alone. An unreadable file and a symlinked one
    are left untouched and named (a replace would cut the link). A rewrite
    leaves its backup beside the file, named like the hand cure's backups."""
    if _proxy_home(home):
        return [], None
    path, body = _settings_body(home)
    if body is None:
        return ["settings defaults NOT written: %s is unreadable or not an "
                "object — left untouched" % path], None
    absent, blocked = _settings_gaps(body)
    notes = ["settings defaults: %s is under a value that is not an object "
             "— left untouched" % ".".join(k) for k in blocked]
    if not absent:
        return notes, None
    if os.path.islink(path):
        return notes + ["settings defaults NOT written: %s is a symlink — "
                        "left untouched" % path], None
    for keys, value in absent:
        node = body
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    try:
        mode = None
        if os.path.lexists(path):
            mode = os.stat(path).st_mode & 0o777
            shutil.copy2(path, _beside(path, "bak-ultracode"))
        pk.atomic_write(path, json.dumps(body, indent=2, ensure_ascii=False)
                        + "\n", mode=mode)
    except OSError as e:
        return notes + ["settings defaults NOT written (%s)" % e], None
    return notes + ["settings defaults set: %s" % ", ".join(
        ".".join(k) for k, _v in absent)], None


def _miss_settings_defaults(home):
    """The SETTINGS_DEFAULTS keys the home does not set. A key set to another
    value is present (the pass never changes it, so it is no gap). Nothing
    for a proxy seat's config dir. An unreadable file raises (cannot tell)."""
    if _proxy_home(home):
        return None
    path, body = _settings_body(home)
    if body is None:
        raise ValueError("%s is unreadable or not an object" % path)
    absent, blocked = _settings_gaps(body)
    gone = [".".join(k) for k, _v in absent] + [
        "%s (its parent is not an object)" % ".".join(k) for k in blocked]
    return ("settings.json lacks %s" % ", ".join(gone)) if gone else None


def _hook_row(home):
    from . import hooks
    return hooks._gap_row(os.path.basename(home), home, hooks.SPECS)


def _miss_hook_contract(home):
    """The inject hook, every required guard lane, the beacon permits and the
    estate defaults (hooks.ESTATE_DEFAULTS, the no-AI-attribution block among
    them) — the same `_gap_row` `helm hooks status` and doctor's guard rungs
    read, and the post-write check `hooks.install_home` runs."""
    from . import hooks
    row = _hook_row(home)
    parts = []
    if row["missing"]:
        parts.append("lanes not live: " + ", ".join(sorted(row["missing"])))
    if not row["permits"]:
        parts.append("beacon permits absent")
    path, body = _settings_body(home)
    if body is None:
        raise ValueError("%s is unreadable or not an object" % path)
    stale = [k for k, v in hooks.ESTATE_DEFAULTS.items()
             if not hooks._default_live(body.get(k), v)]
    if stale:
        parts.append("estate defaults absent or drifted: " + ", ".join(stale))
    return "; ".join(parts) or None


def _prov_hook_contract(home):
    """Write the hook contract through its one writer, `hooks.install_home`,
    at prepare (task/3591): a credential home is used by `claude /login` and
    by orca before any `helm hooks install`, and the estate defaults it
    carries (hooks.ESTATE_DEFAULTS) include the owner's no-AI-attribution
    block. A proxy seat's config dir is its launch's to write (seat
    `_write_launch_assets` runs the same writer). A failed write is named,
    never silent, and does not stop the rest of the pass."""
    if _proxy_home(home):
        return [], None
    from . import hooks
    action, detail = hooks.install_home(home)
    if action == "fail":
        return ["hook contract not written (%s) — `helm hooks install` "
                "writes it" % detail], None
    return [], None


def _miss_memory_base(home):
    """The auto-memory base (hooks.MEMORY_BASE_ENV) a home whose projects is a
    symlink needs, or it stops every memory write on a permission prompt."""
    from . import hooks
    row = _hook_row(home)
    if row["memory"]:
        return None
    want = row["memory_base"]
    return ("settings env lacks %s=%s" % (hooks.MEMORY_BASE_ENV, want) if want
            else "settings env carries %s it no longer needs" % hooks.MEMORY_BASE_ENV)


Benefit = collections.namedtuple(
    "Benefit", "name source provision missing default remedy launch",
    defaults=(False,))

# ORDER IS LOAD-BEARING: projects before the memory base (the base is the
# parent of the projects link), and the hub before the deck (the deck is the
# fallback when skills/ is not a link).
#
# THE HOOK CONTRACT IS WRITTEN HERE through its one writer,
# `hooks.install_home` (task/3591): its config write gate admits a direct child
# of the homes root created after import, so a home minted in this process is
# written in the same pass, estate defaults (the no-AI-attribution block)
# included, before its first session. The memory base rides the same write,
# so its entry only checks. `helm launch` still installs at launch.
BENEFITS = (
    Benefit("shared session store", "SHARED_PROJECTS (~/.claude/projects)",
            _prov_projects, _miss_projects, False,
            "merge any real dir into %s, then ln -sfn %s <home>/projects"
            % (SHARED_PROJECTS, SHARED_PROJECTS)),
    Benefit("skills hub", "skillsync.canonical()",
            _prov_skills_hub, _miss_skills_hub, True,
            "`helm skills sync --apply`"),
    Benefit("skill deck", "HELM_SKILL_DECK (only when skills/ is no link)",
            _prov_skill_deck, _miss_skill_deck, False,
            "`helm homes provision {name} --apply`"),
    Benefit("mcp servers", "the default home's state-file mcpServers",
            _prov_mcp, _miss_mcp, False,
            "`helm homes provision {name} --apply`"),
    Benefit("project mcp approvals",
            "each trusted project's .mcp.json (projectmcp)",
            _prov_project_mcp, _miss_project_mcp, False,
            "`helm homes provision {name} --apply`"),
    Benefit("global instructions",
            "skillsync.instructions_canonical() (the default home's CLAUDE.md)",
            _prov_instructions, _miss_instructions, False,
            "`helm homes provision {name} --apply`"),
    Benefit("opus xhigh + ultracode",
            "SETTINGS_DEFAULTS (the owner's Opus effort ruling)",
            _prov_settings_defaults, _miss_settings_defaults, True,
            "`helm homes provision {name} --apply` (the default home: a "
            "`helm launch` with neither --home nor --no-install)", True),
    Benefit("hook contract",
            "hooks.SPECS + beacon permits + hooks.ESTATE_DEFAULTS",
            _prov_hook_contract, _miss_hook_contract, True,
            "`helm hooks install`"),
    Benefit("auto-memory base", "hooks.memory_base(home)",
            None, _miss_memory_base, True, "`helm hooks install`"),
)


def provision(home, at_launch=False, apply=True):
    """THE ONE PROVISIONING PASS: walk BENEFITS over one claude home, in
    order. -> (notes, error); an error stops the pass. An entry this pass does
    not write is checked instead, and its absence becomes a note naming the
    command that writes it. Reads the module-level list at call time.
    `at_launch` walks only the entries marked `launch` (what `helm launch`
    runs on each home it wires). `apply=False` is the DRY RUN: every entry is
    checked, never written, and each gap becomes a `would provision` note."""
    notes = []
    for b in BENEFITS:
        if at_launch and not b.launch:
            continue
        if not apply:
            try:
                gap = b.missing(home)
            except Exception as e:
                notes.append("%s: cannot tell whether it is present (%s: %s)"
                             % (b.name, e.__class__.__name__, e))
                continue
            if gap and b.provision is not None:
                notes.append("%s: would provision (%s)" % (b.name, gap))
            elif gap:
                notes.append("%s not written here (%s) — %s writes it"
                             % (b.name, gap, b.remedy.format(name=os.path.basename(home))))
            continue
        if b.provision is not None:
            more, err = b.provision(home)
            notes += more
            if err:
                return notes, err
            continue
        try:
            gap = b.missing(home)
        except Exception as e:
            notes.append("%s: cannot tell whether it is present (%s: %s)"
                         % (b.name, e.__class__.__name__, e))
            continue
        if gap:
            notes.append("%s not written here (%s) — %s writes it"
                         % (b.name, gap, b.remedy.format(name=os.path.basename(home))))
    return notes, None


def benefit_drift(dirs=None):
    """THE ONE DRIFT REPORT: walk BENEFITS over every claude home (each real
    dir under the homes root, aliases folded, plus the default) and name what
    each lacks. -> [{label, path, missing: [(benefit, gap, remedy)], unknown:
    [(benefit, why)]}]. A check that cannot tell lands in `unknown`, never in
    `missing` and never as present. Read-only. `dirs` is a test seam."""
    if dirs is None:
        from . import hooks
        dirs = hooks.claude_homes()
    out = []
    for label, path in dirs:
        miss, unknown = [], []
        for b in BENEFITS:
            if not b.default and _is_default(path):
                continue
            try:
                gap = b.missing(path)
            except Exception as e:
                unknown.append((b.name, "%s: %s" % (e.__class__.__name__, e)))
                continue
            if gap:
                miss.append((b.name, gap, b.remedy.format(name=label)))
        out.append({"label": label, "path": path, "missing": miss,
                    "unknown": unknown})
    return out


def home_create(provider, account_email):
    """Prepare a home for a fresh device login. mkdir + (claude) the one
    provisioning pass over BENEFITS, then hand the human the exact login
    command. NEVER seats credentials itself. Every home loads the same best
    setup, whatever cred it carries: the list says what that setup is."""
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
            # THE CARD'S WORDS BESIDE THE CLI'S (task/3735): the same fact,
            # and the migrate button on that row in place of the verb
            return {"error": f"{email} is already seated in {r['path']} (home {r['name']})"
                             f" — one home = one login; use that home{hint}",
                    "owner_error": f"{email} already has a home, {r['name']}: one "
                                   "account keeps one home, so use that one"
                                   + ("" if r["canonical"] in (True, None) else
                                      "; its folder is not named for that account "
                                      "yet, and the migrate button on its row "
                                      "renames it")}
    home = os.path.join(ROOTS[provider], name)
    existing = os.path.isdir(home) and not os.path.islink(home)
    if not existing and os.path.lexists(home):
        return {"error": f"{home} exists but is not a plain directory — inspect it by hand"}
    if existing:
        held = _IDENTITY[provider](home)
        if held and held != email:
            # Claude's reader is oauthAccount metadata. Codex's is the token's
            # id_token email, and that claim stays a hold.
            if provider == "claude":
                from . import cred
                label = cred.metadata_says(held, home)
            else:
                label = "already holds %s" % held
            return {"error": f"{home} {label} — one home = one login; "
                             "archive it first or pick the right email"}
    os.makedirs(home, mode=0o700, exist_ok=True)
    notes = []
    if provider == "claude":
        notes, err = provision(home)
        if err:
            return {"error": err}
    if existing:
        notes.append("home already existed (idempotent — nothing was overwritten)")
    return {"home": home, "name": name, "provider": provider, "existing": existing,
            "login_cmd": LOGIN_CMDS[provider](home),
            "next": "run the login command in YOUR terminal, approve in the browser, "
                    f"then: helm homes verify {name} --provider {provider}",
            # THE SAME NEXT STEP IN THE OWNER'S WORDS, for the console's homes
            # card (task/3735): he does not use a terminal, so it names no
            # command. The login stays his to approve; an agent can start it.
            "owner_next": f"home {name} is ready and needs signing in: an agent "
                          "can start the login for you, and you approve it in "
                          "the browser. Then press verify on its row.",
            "note": "; ".join(notes) or None}


def home_provision(name, apply=True):
    """Run the one provisioning pass over an EXISTING claude home — the door
    the drift report names for a benefit only this pass writes. Additive as
    at creation; the default home is the source of the set and is refused.
    `apply=False` plans it and writes nothing (the verb's default)."""
    row, err = _resolve(name, "claude")
    if err:
        return err
    if row["default"]:
        return {"error": "REFUSED: the default home is the SOURCE of the set "
                         "every other home is provisioned from"}
    real = os.path.realpath(row["path"])
    notes, err = provision(real, apply=apply)
    if err:
        return {"error": err, "notes": notes}
    return {"home": row["name"], "path": real, "notes": notes, "apply": apply}


def home_verify(name, provider=None):
    """Post-login check: authed, identity, canonical name, projects link, duplicate
    identities. Verdict + concrete fixes — helm flags, the human (or a verb) fixes.

    `fixes` are the terminal's lines and name the verb or command that fixes
    each. `owner_fixes` are the same lines in the OWNER'S words, one for one,
    for the console's homes card (task/3735): the fact and who fixes it, never
    a command, because he does not use a terminal — as `ready._row` carries
    `owner_repair` beside `repair`."""
    row, err = _resolve(name, provider)
    if err:
        return err
    prov, real = row["provider"], os.path.realpath(row["path"])
    fixes, owner_fixes = [], []

    def fix(line, owner):
        fixes.append(line)
        owner_fixes.append(owner)
    if not row["authed"]:
        fix("not logged in — run (human-only): " + LOGIN_CMDS[prov](real),
            "not signed in yet: an agent can start the login for you, and you "
            "approve it in the browser")
    elif not row["identity"]:
        fix("authed but identity unreadable — "
            + (".claude.json has no oauthAccount yet; open the agent once"
               if prov == "claude" else "auth.json id_token carries no email claim"),
            "signed in, but which account it holds cannot be read yet: "
            + ("it can be once an agent has been opened in this home"
               if prov == "claude" else "its login names no email address"))
    if row["canonical"] is False:
        fix(f"dir name lies: identity {row['identity']} wants "
            f"{canonical_name(row['identity'])} — `helm homes migrate {row['name']}`",
            f"its folder is not named for the account it holds: it holds "
            f"{row['identity']}, so it should be named "
            f"{canonical_name(row['identity'])}; the migrate button on this row "
            "renames it")
    if prov == "claude" and row["projects_link_ok"] is False:
        pl = os.path.join(real, "projects")
        if os.path.isdir(pl) and not os.path.islink(pl):
            fix(f"projects is a REAL dir — sessions born here are STRANDED; merge "
                f"{pl}/* into {SHARED_PROJECTS}, then: ln -sfn {SHARED_PROJECTS} {pl}",
                "its sessions folder is a folder of its own, so sessions started "
                "here are stranded outside the shared store; an agent moves them "
                "in and links the folder")
        elif os.path.islink(pl):
            fix(f"projects symlink points at {os.path.realpath(pl)}, not the shared "
                f"store — re-point: ln -sfn {SHARED_PROJECTS} {pl}",
                "its sessions folder points somewhere other than the shared "
                "store; an agent points it back")
        else:
            fix(f"projects link missing — create: ln -s {SHARED_PROJECTS} {pl}",
                "its sessions folder is not linked to the shared store yet; an "
                "agent links it")
    dups = row.get("duplicate_identity") or []
    # default-home sharing is the orchestrator pattern (see _hygiene_flags) —
    # only named-home <-> named-home duplication demands a survivor
    named_dups = [d for d in dups if not d.startswith("(default-")] \
        if not row["default"] else []
    if named_dups:
        fix(f"identity {row['identity']} also lives in: {', '.join(named_dups)} — one "
            "identity should hold ONE named home; the human picks a survivor and archives "
            "the rest (`helm homes archive`) — NEVER copy credentials between homes",
            f"the same account also lives in {', '.join(named_dups)}: one account "
            "keeps one home, so pick the one to keep and archive the others with "
            "their archive buttons; a login is never copied between homes")
    fam = row.get("shared_family") or []
    if fam:
        fix(f"BYTE-COPIES of one refresh-token family with: {', '.join(fam)} — "
            "reuse detection revokes the WHOLE family at once; a fresh login per "
            "home is the only fix (one home = one login = one token family)",
            f"it holds a copy of the same login as {', '.join(fam)}, and the "
            "vendor cancels every copy at once when it sees one reused: each "
            "home needs its own fresh sign-in, which an agent can start for you")
    verdict = "pending-login" if not row["authed"] else ("issues" if fixes else "ok")
    return {"home": row["name"], "provider": prov, "path": row["path"],
            "checks": {"authed": row["authed"], "identity": row["identity"],
                       "canonical": row["canonical"],
                       "projects_link_ok": row["projects_link_ok"],
                       "duplicate_identity": dups, "shared_family": fam,
                       "live_pids": row["live_pids"]},
            "verdict": verdict, "fixes": fixes, "owner_fixes": owner_fixes}


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
    Searches BOTH archive roots — helm's and a declared predecessor's."""
    name = (name or "").strip()
    if not name:
        return {"error": "need a home name (see `helm homes archives`)"}
    cands = []
    for p in _archive_dirs():
        meta, marker = _archive_marker(p)
        if os.path.basename(p) == name or meta.get("name") == name:
            cands.append((p, meta, marker))
    if not cands:
        return {"error": f"nothing archived under {name!r} in "
                         f"{' or '.join(_archive_roots())}"}
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
                         "(`helm homes archive`); credentials are never merged or copied",
                "owner_error": f"a home named {want} already exists, so this one "
                               "cannot be renamed to it: pick the one to keep and "
                               "archive the other with its archive button; a login "
                               "is never copied between homes"}
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
        print("helm homes: no archives (%s)" % ", ".join(_archive_roots()))
        return 0
    print("helm homes: %d archived (restore: helm homes restore <name>)" % len(rows))
    for r in rows:
        print("  %-7s %-28s %-30s %s" % (
            r["provider"] or "?", r["name"], r["identity"] or "-", r["path"]))
    return 0


def cmd_homes(args):
    """homes [prepare <provider> <email> | provision [<name>] [--apply] | verify [<name>] |
    archive <name> | restore <name> | migrate <name> | archives]
    [--provider claude|codex]"""
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
    if verb == "provision":
        # a mutation door: a junk flag refuses (rc 2) before any home is read
        from .cli import guard_tail
        rc = guard_tail("helm homes provision",
                        [a for a in rest if a.startswith("-")],
                        flags=("--apply",),
                        usage="homes provision [<name>] [--apply]")
        if rc is not None:
            return rc
        apply = "--apply" in rest
        rest = [a for a in rest if not a.startswith("-")]
        names = rest or [r["name"] for r in homes_list()
                         if r["provider"] == "claude" and not r["archived"]
                         and not r.get("broken_alias") and not r["default"]]
        worst = 0
        for n in names:
            res = home_provision(n, apply=apply)
            for line in res.get("notes") or ():
                print("  %s: %s" % (res.get("home", n), line))
            if "error" in res:
                worst = max(worst, _fail(res))
                continue
            if apply:
                print("helm homes: provisioned %s (%s)" % (res["home"], res["path"]))
            else:
                print("helm homes: %s (%s) — dry run, %s; `--apply` writes it"
                      % (res["home"], res["path"],
                         "nothing to provision" if not res["notes"]
                         else "nothing written"))
        return worst
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
          "provision [<name>] [--apply]|verify [<name>]|archive <name>|restore <name>|"
          "migrate <name>|archives)" % verb, file=sys.stderr)
    return 2
