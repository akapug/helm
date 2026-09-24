"""Shared sharded evidence validation; observation is not landing authority.

The evidence constructor checks the complete root/supervisor/worker object.
Receipt readers add the versioned authority contract; neither caller maintains
its own weaker copy of the process-chain checks.
"""
import ast
import collections
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import string
import struct
import tarfile

from . import gateshard, gatetestrecord, vcs


RECEIPT_VERSION = 9
RUNNER_PATH = "helm/gateshard.py"
SUITE_ARGV = (RUNNER_PATH,)
# Frozen receipt-era grammar, independent of the active writer's command.
SERIAL_ARGV = ("-m", "unittest", "discover", "-s", "tests", "-t", ".")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_PLAN_KEYS = {"modules", "planned", "digest", "ids_digest"}
_AUTHORITY_KEYS = {
    "v", "kind", "bundle_digest", "artifact_census", "plan", "rediscovery",
    "assignments_digest", "process_chain_digest", "owner", "root",
    "workers", "peak_workers", "containment", "runner",
}
_RUNNER_V1_KEYS = {"path", "checkout_root", "blob", "sha256", "tree", "argv"}
_RUNNER_V2_KEYS = {
    "repo_id", "commit", "tree", "checkout_root", "path", "blob", "sha256",
    "argv", "content_digest", "files",
}
_RUNNER_FILE_KEYS = {"path", "blob", "sha256"}
_CONTENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _object(value, keys, name):
    if type(value) is not dict or set(value) != keys:
        raise ValueError("%s fields do not match the contract" % name)
    return value


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError("%s must be a positive integer" % name)
    return value


def _digest(value, name, pattern=_HEX64):
    if type(value) is not str or not pattern.fullmatch(value):
        raise ValueError("%s is not a canonical digest" % name)
    return value


def _identity(value, name):
    _object(value, {"pid", "start"}, name)
    for key in ("pid", "start"):
        _positive(value[key], "%s.%s" % (name, key))
    return value


def canonical_digest(value):
    """Hash structured evidence without dropping order or duplicate IDs."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False).encode("utf-8", "surrogatepass")).hexdigest()


def plan_facts(counts, ids):
    """Compact facts from a complete module census, never a selected set."""
    if type(counts) is not dict or type(ids) is not dict or not counts \
            or set(counts) != set(ids):
        raise ValueError("plan census is missing or foreign")
    for name, count in counts.items():
        if type(name) is not str or not name:
            raise ValueError("plan module is not a name")
        _positive(count, "plan module count")
        if type(ids[name]) is not list or len(ids[name]) != count \
                or not all(type(test) is str and test for test in ids[name]):
            raise ValueError("plan IDs disagree with the module count")
    return {"modules": len(counts), "planned": sum(counts.values()),
            "digest": gateshard.plan_digest(counts, ids),
            "ids_digest": canonical_digest(ids)}


def _plan(value):
    _object(value, _PLAN_KEYS, "plan")
    for key in ("modules", "planned"):
        _positive(value[key], "plan." + key)
    if value["modules"] > value["planned"]:
        raise ValueError("plan has more modules than tests")
    for key in ("digest", "ids_digest"):
        _digest(value[key], "plan." + key)
    return value


def _runner_root(value):
    if type(value) is not str or not os.path.isabs(value) \
            or os.path.normpath(value) != value:
        raise ValueError("runner root is not a canonical absolute path")
    return value


def _runner_path(value):
    if type(value) is not str or not value or value.startswith("/") \
            or "\\" in value or posixpath.normpath(value) != value \
            or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("runner file path is not canonical")
    return value


def _runner_v1(row, runner, executable):
    _object(runner, _RUNNER_V1_KEYS, "runner")
    root = _runner_root(runner["checkout_root"])
    if runner["path"] != RUNNER_PATH:
        raise ValueError("runner is not the canonical checkout-owned path")
    _digest(runner["blob"], "runner blob", _HEX40)
    _digest(runner["sha256"], "runner sha256")
    _digest(runner["tree"], "runner tree", _HEX40)
    if runner["tree"] != row.get("tree"):
        raise ValueError("runner tree disagrees with the receipt")
    if runner["argv"] != [executable, RUNNER_PATH] \
            or row.get("argv") != runner["argv"]:
        raise ValueError("runner argv disagrees with the receipt interpreter")
    return root


def _runner_v2(row, runner, executable):
    _object(runner, _RUNNER_V2_KEYS, "runner")
    root = _runner_root(runner["checkout_root"])
    if type(runner["repo_id"]) is not str or not runner["repo_id"]:
        raise ValueError("runner repository provenance is incomplete")
    for key in ("commit", "tree"):
        _digest(runner[key], "runner " + key, _HEX40)
    if runner["path"] != RUNNER_PATH:
        raise ValueError("runner entrypoint is not canonical")
    _digest(runner["blob"], "runner blob", _HEX40)
    _digest(runner["sha256"], "runner sha256")
    content = runner["content_digest"]
    if content is not None and (type(content) is not str
                                or not _CONTENT_DIGEST.fullmatch(content)):
        raise ValueError("runner release content digest is not canonical")
    files = runner["files"]
    if type(files) is not list or not files:
        raise ValueError("runner executable bundle is empty")
    paths = []
    for item in files:
        _object(item, _RUNNER_FILE_KEYS, "runner file")
        paths.append(_runner_path(item["path"]))
        _digest(item["blob"], "runner file blob", _HEX40)
        _digest(item["sha256"], "runner file sha256")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("runner executable bundle paths are unordered or duplicated")
    entries = [item for item in files if item["path"] == RUNNER_PATH]
    if len(entries) != 1 or entries[0]["blob"] != runner["blob"] \
            or entries[0]["sha256"] != runner["sha256"]:
        raise ValueError("runner entrypoint disagrees with its executable bundle")
    argv = [executable, os.path.join(root, *RUNNER_PATH.split("/"))]
    if runner["argv"] != argv or row.get("argv") != argv:
        raise ValueError("runner argv disagrees with its concrete release root")
    return root


def validate_receipt(row):
    """Validate v9's derived attestation, not caller-asserted success flags.

    This reader does not re-run discovery or reconstruct deleted raw files.
    The minting boundary must first validate the complete bundle and contained
    rediscovery. Its compact facts then travel inside the receipt content ID
    and the existing repository/custody boundary. Shape alone grants no origin.
    Historical receipt versions retain their separate grammar. Every planned
    test must reach the runner: fixture-level nonexecution cannot qualify even
    when unittest reports OK. Skips remain bounded by the executed count.
    """
    if type(row) is not dict or type(row.get("v")) is not int \
            or row["v"] != RECEIPT_VERSION or row.get("suite") is not True:
        raise ValueError("sharded authority requires a whole-suite v9 receipt")
    authority = _object(row.get("sharded_authority"), _AUTHORITY_KEYS,
                        "sharded authority")
    version = authority["v"]
    if type(version) is not int or version not in (1, 2) \
            or authority["kind"] != "whole-suite-sharded":
        raise ValueError("sharded authority version or kind is unknown")
    for key in ("bundle_digest", "assignments_digest", "process_chain_digest"):
        _digest(authority[key], key)
    plan = _plan(authority["plan"])
    rediscovery = _object(authority["rediscovery"],
                         {"plan", "supervisor", "process", "sweep"},
                         "rediscovery")
    if _plan(rediscovery["plan"]) != plan:
        raise ValueError("rediscovery disagrees with the complete ordered plan")
    generations = [_identity(authority[key], key) for key in ("owner", "root")]
    generations.extend(_identity(rediscovery[key], "rediscovery." + key)
                       for key in ("supervisor", "process"))
    if len({(item["pid"], item["start"]) for item in generations}) != 4:
        raise ValueError("authority process generations are not distinct")
    if rediscovery["sweep"] != "empty-after-exit":
        raise ValueError("rediscovery has no post-exit containment proof")
    workers = _positive(authority["workers"], "workers")
    peak = _positive(authority["peak_workers"], "peak_workers")
    if workers != plan["modules"] or peak > workers:
        raise ValueError("worker census disagrees with the singleton plan")
    census = _object(authority["artifact_census"], {"count", "digest"},
                     "artifact census")
    if _positive(census["count"], "artifact count") != 1 + 2 * workers:
        raise ValueError("artifact census is incomplete")
    _digest(census["digest"], "artifact census digest")
    containment = _object(authority["containment"],
                          {"method", "supervisors", "swept"}, "containment")
    if containment["method"] != "subreaper" \
            or _positive(containment["supervisors"], "supervisors") != workers \
            or _positive(containment["swept"], "swept") != workers:
        raise ValueError("supervisor containment census is incomplete")
    runner = authority["runner"]
    interpreter = _object(row.get("interpreter"),
                          {"name", "version", "language", "executable"}, "interpreter")
    if not all(type(value) is str and value for value in interpreter.values()):
        raise ValueError("receipt interpreter identity is incomplete")
    executable = interpreter["executable"]
    if type(executable) is not str or not os.path.isabs(executable):
        raise ValueError("receipt interpreter executable is not absolute")
    if version == 1:
        _runner_v1(row, runner, executable)
    else:
        _runner_v2(row, runner, executable)
    if row.get("status") not in ("OK", "FAILED") \
            or type(row.get("rc")) is not int \
            or row["rc"] != (0 if row["status"] == "OK" else 1):
        raise ValueError("incomplete outcome cannot carry sharded authority")
    if row["status"] == "OK":
        if "failure_total" not in row or "failures" not in row:
            raise ValueError("OK authority failure record is incomplete")
        if row["failure_total"] != 0 or row["failures"] != []:
            raise ValueError("OK authority cannot carry failure diagnostics")
    ran, skipped = row.get("ran"), row.get("skipped")
    if type(ran) is not int or ran != plan["planned"] \
            or type(skipped) is not int or not 0 <= skipped <= ran:
        raise ValueError("receipt counts disagree with the complete plan")
    return authority


def receipt_refusal(row):
    """Total v9 reader; malformed evidence never becomes an exception escape."""
    try:
        validate_receipt(row)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return "sharded authority refused: %s" % exc
    return None


def _blob_id(raw):
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def loaded_runner_root():
    """Return the concrete root selected by the already-loaded runner module."""
    entry = os.path.realpath(os.path.abspath(gateshard.__file__))
    root = os.path.dirname(os.path.dirname(entry))
    expected = os.path.join(root, *RUNNER_PATH.split("/"))
    if entry != expected:
        raise ValueError("loaded gateshard module is outside its canonical path")
    return root


def runner_dependency_paths(raw):
    """Derive the executable file set from the runner's own declaration."""
    try:
        tree = ast.parse(raw.decode("utf-8"), filename=RUNNER_PATH)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError("runner dependency declaration is unreadable: %s" % exc)
    found = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) \
                and target.id == "STANDALONE_DEPENDENCIES":
            try:
                found.append(ast.literal_eval(node.value))
            except (ValueError, TypeError, SyntaxError) as exc:
                raise ValueError("runner dependency declaration is not literal") from exc
    if len(found) != 1 or type(found[0]) is not tuple or not found[0]:
        raise ValueError("runner must declare one nonempty dependency tuple")
    names = found[0]
    if not all(type(name) is str and name.endswith(".py")
               and "/" not in name and "\\" not in name
               and name not in (".", "..") for name in names) \
            or len(names) != len(set(names)):
        raise ValueError("runner dependency names are unsafe or duplicated")
    root = posixpath.dirname(RUNNER_PATH)
    return tuple(sorted([RUNNER_PATH] + [posixpath.join(root, name)
                                         for name in names]))


def runner_bundle_manifest(files):
    """Construct the exact declared executable manifest from raw file bytes."""
    if type(files) is not dict or RUNNER_PATH not in files \
            or not all(type(path) is str and type(raw) is bytes
                       for path, raw in files.items()):
        raise ValueError("runner bundle bytes are incomplete")
    paths = runner_dependency_paths(files[RUNNER_PATH])
    missing = [path for path in paths if path not in files]
    if missing:
        raise ValueError("runner bundle is missing declared files: %s"
                         % ", ".join(missing))
    return [{"path": path, "blob": _blob_id(files[path]),
             "sha256": hashlib.sha256(files[path]).hexdigest()}
            for path in paths]


def _bundle_refusal(runner, files):
    try:
        actual = runner_bundle_manifest(files)
    except ValueError as exc:
        return str(exc)
    if actual != runner["files"]:
        return "runner executable manifest disagrees with immutable bytes"
    return None


def _git_runner_refusal(repo, runner):
    try:
        backend = vcs.backend(repo)
        env = vcs._authority_env()
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        rc, tree, _err = backend.text(
            repo, "rev-parse", runner["commit"] + "^{tree}", env=env)
        if rc:
            return "release commit is unavailable in this Git object store"
        if tree.strip() != runner["tree"]:
            return "release commit names a different runner tree"
        files = {}
        for item in runner["files"]:
            path = item["path"]
            rc, oid, _err = backend.text(
                repo, "rev-parse", "%s:%s" % (runner["tree"], path), env=env)
            if rc or oid.strip() != item["blob"]:
                return "runner file %s differs from the release tree" % path
            rc, entry, _err = backend.run(
                repo, "ls-tree", "-z", runner["tree"], "--", path, env=env)
            expected = ("blob %s\t%s\0" % (item["blob"], path)).encode("utf-8")
            if rc or entry not in (b"100644 " + expected, b"100755 " + expected):
                return "runner file %s is not a regular tree blob" % path
            rc, raw, _err = backend.run(
                repo, "cat-file", "blob", item["blob"], env=env)
            if rc or type(raw) is not bytes:
                return "runner file %s bytes are unavailable" % path
            files[path] = raw
        refusal = _bundle_refusal(runner, files)
        if refusal:
            return refusal
        if runner["content_digest"] is None:
            return None
        rc, archive, _err = backend.run(
            repo, "archive", "--format=tar", runner["commit"], env=env)
        if rc or type(archive) is not bytes:
            return "release archive is unavailable for content verification"
        if _archive_digest(archive) != runner["content_digest"]:
            return "release content digest disagrees with the Git tree"
        return None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return "Git runner evidence is unavailable: %s" % exc


def _feed(digest, value):
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _archive_digest(raw):
    nodes = [(".", "dir", 0o555, None)]
    seen = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                name = member.name[:-1] if member.isdir() \
                    and member.name.endswith("/") else member.name
                if not name or name == "artifact-manifest.json" \
                        or name in seen or name.startswith("/") \
                        or any(part in ("", ".", "..")
                               for part in name.split("/")):
                    raise ValueError("unsafe or duplicate release archive path")
                seen.add(name)
                if member.isdir():
                    nodes.append((name, "dir", 0o555, None))
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("release archive file has no bytes")
                    with stream:
                        value = stream.read()
                    mode = 0o555 if member.mode & 0o111 else 0o444
                    nodes.append((name, "file", mode, value))
                elif member.issym():
                    target = member.linkname
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), target))
                    if not target or target.startswith("/") \
                            or resolved == ".." or resolved.startswith("../"):
                        raise ValueError("unsafe release archive symlink")
                    nodes.append((name, "link", 0o777, target))
                else:
                    raise ValueError("unsupported release archive member")
    except tarfile.TarError as exc:
        raise ValueError("release archive is unreadable: %s" % exc)
    digest = hashlib.sha256()
    digest.update(b"helm-artifact-content-v1\0")
    for name, kind, mode, value in sorted(
            nodes, key=lambda item: os.fsencode(item[0])):
        digest.update(kind.encode("ascii"))
        _feed(digest, os.fsencode(name))
        digest.update(struct.pack(">I", mode))
        if kind == "file":
            _feed(digest, value)
        elif kind == "link":
            _feed(digest, os.fsencode(value))
    return "sha256:" + digest.hexdigest()


def _artifact_paths(root):
    paths = []

    def visit(directory, prefix):
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda child: os.fsencode(child.name))
        for child in children:
            rel = child.name if not prefix else prefix + "/" + child.name
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                visit(child.path, rel)
            elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError("unsupported artifact node: %s" % rel)
            paths.append((child.path, rel, info.st_mode, info.st_size))

    visit(root, "")
    info = os.lstat(root)
    paths.append((root, ".", info.st_mode, info.st_size))
    return paths


def _artifact_digest(root, paths):
    digest = hashlib.sha256()
    digest.update(b"helm-artifact-content-v1\0")
    for path, name, mode, size in sorted(
            paths, key=lambda item: os.fsencode(item[1])):
        if name == "artifact-manifest.json":
            continue
        kind = "dir" if stat.S_ISDIR(mode) else \
            "file" if stat.S_ISREG(mode) else "link"
        digest.update(kind.encode("ascii"))
        _feed(digest, os.fsencode(name))
        digest.update(struct.pack(">I", stat.S_IMODE(mode)))
        if kind == "file":
            with open(path, "rb") as stream:
                raw = stream.read()
            _feed(digest, raw)
            if len(raw) != size or os.path.getsize(path) != size:
                raise ValueError("artifact file changed while hashing: %s" % name)
        elif kind == "link":
            target = os.readlink(path)
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(name), target))
            if not target or target.startswith("/") or resolved == ".." \
                    or resolved.startswith("../"):
                raise ValueError("artifact symlink escapes its release")
            _feed(digest, os.fsencode(target))
    return "sha256:" + digest.hexdigest()


def _artifact_file(root, rel):
    path = root
    parts = rel.split("/")
    for index, part in enumerate(parts):
        path = os.path.join(path, part)
        mode = os.lstat(path).st_mode
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError("artifact path crosses a non-directory: %s" % rel)
        if index == len(parts) - 1 and not stat.S_ISREG(mode):
            raise ValueError("artifact runner path is not regular: %s" % rel)
    with open(path, "rb") as stream:
        return stream.read()


def _artifact_runner_refusal(release, runner):
    try:
        if os.path.realpath(release) != os.path.abspath(release) \
                or not stat.S_ISDIR(os.lstat(release).st_mode) \
                or os.path.basename(release) != runner["commit"] \
                or os.path.basename(os.path.dirname(release)) != "releases":
            return "artifact is not the concrete recorded release"
        manifest_raw = _artifact_file(release, "artifact-manifest.json")
        manifest = json.loads(manifest_raw.decode("utf-8"))
        if type(manifest) is not dict:
            return "artifact manifest is not an object"
        expected = {"schema": 1, "commit": runner["commit"],
                    "tree": runner["tree"],
                    "package_version": manifest.get("package_version"),
                    "content_digest": runner["content_digest"]}
        canonical = (json.dumps(expected, sort_keys=True,
                                separators=(",", ":")) + "\n").encode("utf-8")
        if set(manifest) != set(expected) \
                or type(manifest.get("package_version")) is not str \
                or not manifest["package_version"] or manifest_raw != canonical:
            return "artifact manifest differs from the recorded release"
        if runner["content_digest"] is None:
            return "receipt carries no release content digest"
        paths = _artifact_paths(release)
        if _artifact_digest(release, paths) != runner["content_digest"]:
            return "artifact content digest differs from the recorded release"
        if any(not stat.S_ISLNK(mode) and mode & 0o222
               for _path, _name, mode, _size in paths):
            return "artifact release is writable rather than immutable"
        files = {item["path"]: _artifact_file(release, item["path"])
                 for item in runner["files"]}
        return _bundle_refusal(runner, files)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return "artifact runner evidence is unavailable: %s" % exc


def _source_candidates():
    # repo_id is remote provenance, never a local path selected by a receipt.
    # The reader may only discover the object store that owns its loaded Helm.
    out = []
    probe = os.path.abspath(__file__)
    while True:
        probe = os.path.dirname(probe)
        if os.path.lexists(os.path.join(probe, ".git")):
            out.append(probe)
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
    return list(dict.fromkeys(os.path.realpath(path) for path in out))


def _release_candidates(runner):
    # checkout_root is remote execution provenance, never a path to trust here.
    # Only this loaded release or the canonical local artifact store may anchor
    # artifact verification without an explicit test/integration candidate.
    out = []
    probe = os.path.abspath(__file__)
    while True:
        probe = os.path.dirname(probe)
        if os.path.isfile(os.path.join(probe, "artifact-manifest.json")):
            out.append(probe)
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
    base = os.environ.get("XDG_DATA_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    expected = os.path.join(base, "helm", "artifacts", "releases",
                            runner["commit"])
    if os.path.isdir(expected):
        out.append(expected)
    return list(dict.fromkeys(os.path.realpath(path) for path in out))


def _verification_channel(candidates, verifier):
    attempted, details = False, []
    for candidate in candidates:
        attempted = True
        refusal = verifier(candidate)
        if refusal is None:
            return {"attempted": True, "result": "VERIFIED",
                    "detail": os.path.realpath(candidate)}
        details.append("%s: %s" % (candidate, refusal))
    return {"attempted": attempted,
            "result": "REFUSED" if attempted else "NOT_RUN",
            "detail": "; ".join(details) if details else "no verifier input"}


def runner_verification(row, repos=None, releases=None):
    """Verify authority-v2 runner provenance through two independent routes."""
    refusal = receipt_refusal(row)
    if refusal:
        return {"verified": False,
                "git": {"attempted": False, "result": "NOT_RUN",
                        "detail": refusal},
                "artifact": {"attempted": False, "result": "NOT_RUN",
                             "detail": refusal}}
    authority = row["sharded_authority"]
    if authority["v"] != 2:
        raise ValueError("runner verification state exists only for authority-v2")
    runner = authority["runner"]
    sources = _source_candidates() if repos is None else repos
    artifacts = _release_candidates(runner) if releases is None else releases
    git = _verification_channel(
        sources, lambda repo: _git_runner_refusal(repo, runner))
    artifact = ({"attempted": False, "result": "NOT_RUN",
                 "detail": "Git verification already proved the runner"}
                if git["result"] == "VERIFIED" else
                _verification_channel(
                    artifacts,
                    lambda release: _artifact_runner_refusal(release, runner)))
    return {"verified": "VERIFIED" in (git["result"], artifact["result"]),
            "git": git, "artifact": artifact}


def runner_identity_refusal(row, repos=None, releases=None):
    refusal = receipt_refusal(row)
    if refusal:
        return refusal
    state = runner_verification(row, repos=repos, releases=releases)
    if state["verified"]:
        return None
    git, artifact = state["git"], state["artifact"]
    if not git["attempted"] and not artifact["attempted"]:
        return ("sharded runner identity UNKNOWN: neither Git nor artifact "
                "verifier ran")
    return ("sharded runner identity REFUSED: Git %s (%s); artifact %s (%s)"
            % (git["result"], git["detail"], artifact["result"],
               artifact["detail"]))


def runner_tree_refusal(row, repo):
    """Verify v1 in the tested tree; verify v2 against independent Helm proof."""
    refusal = receipt_refusal(row)
    if refusal:
        return refusal
    runner = row["sharded_authority"]["runner"]
    if row["sharded_authority"]["v"] == 2:
        return runner_identity_refusal(row)
    try:
        backend = vcs.backend(repo)
        env = vcs._authority_env()
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        rc, oid, _err = backend.text(
            repo, "rev-parse", "%s:%s" % (runner["tree"], RUNNER_PATH),
            env=env)
        if rc or oid.strip() != runner["blob"]:
            return "sharded runner blob differs from the immutable receipt tree"
        rc, entry, _err = backend.run(
            repo, "ls-tree", "-z", runner["tree"], "--", RUNNER_PATH,
            env=env)
        expected = ("blob %s\t%s\0" % (runner["blob"], RUNNER_PATH)).encode("utf-8")
        if rc or entry not in (b"100644 " + expected, b"100755 " + expected):
            return "sharded runner tree path is not a regular file blob"
        rc, raw, _err = backend.run(repo, "cat-file", "blob", runner["blob"],
                                    env=env)
        if rc or type(raw) is not bytes:
            return "sharded runner bytes are unavailable in the receipt tree"
        if _blob_id(raw) != runner["blob"] \
                or hashlib.sha256(raw).hexdigest() != runner["sha256"]:
            return "sharded runner blob and sha256 disagree with immutable bytes"
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return "sharded runner tree evidence is unavailable: %s" % exc
    return None


def validate_rediscovery(evidence, witness):
    """Compare a second contained, fresh-process census with the full bundle.

    The caller must own the supervisor's launch/exit/sweep evidence. This
    validator binds that generation to the gate owner, refuses missing proof,
    and compares complete ordered ID lists, including every duplicate. It does
    not launch discovery and cannot turn a selected module list into authority.
    """
    _object(witness, {"v", "token", "owner", "supervisor", "process",
                      "counts", "ids", "rc", "containment", "sweep"},
            "rediscovery witness")
    root = evidence["root"]
    if type(witness["v"]) is not int or witness["v"] != 1 \
            or witness["token"] != root["token"]:
        raise ValueError("rediscovery witness belongs to a foreign run")
    owner = _identity(witness["owner"], "rediscovery owner")
    if owner != {"pid": root["owner_pid"], "start": root["owner_start"]}:
        raise ValueError("rediscovery owner generation is foreign")
    supervisor = _identity(witness["supervisor"], "rediscovery supervisor")
    process = _identity(witness["process"], "rediscovery process")
    generations = [(row["pid"], row["start"]) for row in
                   [root] + evidence["workers"] + evidence["supervisors"]]
    generations.extend((row["pid"], row["start"])
                       for row in (owner, supervisor, process))
    if len(generations) != len(set(generations)):
        raise ValueError("rediscovery process generation is reused")
    if type(witness["rc"]) is not int or witness["rc"] != 0 \
            or witness["containment"] != "subreaper" \
            or witness["sweep"] != "empty-after-exit":
        raise ValueError("rediscovery lacks contained post-exit evidence")
    ids = {row["modules"][0]: list(row["planned"])
           for row in evidence["workers"]}
    counts = {name: len(tests) for name, tests in ids.items()}
    planned = plan_facts(counts, ids)
    confirmed = plan_facts(witness["counts"], witness["ids"])
    if witness["counts"] != counts or witness["ids"] != ids \
            or confirmed != planned:
        raise ValueError("rediscovery ordered IDs or full plan count drifted")
    return {"plan": confirmed, "supervisor": supervisor, "process": process,
            "sweep": witness["sweep"]}


def _sample_reason(row, workers=False):
    required = {
        "load", "affinity_cores", "host_cores", "memory", "competing_suites",
    } | ({"phase", "workers"} if workers else set())
    if type(row) is not dict or set(row) != required:
        return "resource sample schema is incomplete"
    if type(row["load"]) is not list or len(row["load"]) != 3 \
            or not all(isinstance(value, (int, float)) for value in row["load"]):
        return "resource sample load is unavailable"
    if type(row["affinity_cores"]) is not int or row["affinity_cores"] <= 0 \
            or type(row["host_cores"]) is not int or row["host_cores"] <= 0:
        return "resource sample core census is unavailable"
    memory = row["memory"]
    if type(memory) is not dict or set(memory) != {"MemAvailable", "MemFree"} \
            or not all(type(value) is int and value >= 0
                       for value in memory.values()):
        return "resource sample memory census is unavailable"
    competing = row["competing_suites"]
    if type(competing) is not list or not all(
            type(item) is dict and set(item) == {"pid", "start", "argv"}
            and type(item["pid"]) is int and item["pid"] > 1
            and (item["start"] is None or type(item["start"]) is int)
            and type(item["argv"]) is list
            and all(isinstance(arg, str) for arg in item["argv"])
            for item in competing):
        return "resource sample competing-suite census is unavailable"
    if workers and (type(row["workers"]) is not list
                    or not all(type(pid) is int and pid > 1
                               for pid in row["workers"])):
        return "resource sample worker census is unavailable"
    return None


def _root_artifact(row, token):
    """Validate a sharded-root artifact. -> row (raises ValueError)

    TWO VERSIONS, VALIDATED SEPARATELY, NEITHER OPTIONAL WITHIN ITSELF.
    v1 is the legacy shape and carries NO planning evidence. v2 carries
    `plan` and MUST carry it, fully shaped.

    Making `plan` optional inside v1 was the wrong repair and it is worth
    recording why, because it looked like the conservative choice: a version
    int that admits two shapes is not a version, and an optional authority
    field means a newly minted canonical root could omit its own evidence --
    or carry arbitrary content under that key -- and still pass this reader.
    Updating fixtures costs less than evidence that may or may not be there.
    """
    required = {
        "v", "type", "token", "pid", "start", "owner_pid", "owner_start",
        "bins", "assignments", "samples", "counts", "rc",
    }
    version = row.get("v") if isinstance(row, dict) else None
    if version == 2:
        required = required | {"plan"}
    if type(row) is not dict or set(row) != required \
            or type(version) is not int or version not in (1, 2) \
            or row.get("type") != "sharded-root" or row.get("token") != token:
        raise ValueError("sharded root artifact schema is invalid")
    if version == 2:
        plan = row["plan"]
        digest = plan.get("digest") if isinstance(plan, dict) else None
        modules = plan.get("modules") if isinstance(plan, dict) else None
        planned = plan.get("planned") if isinstance(plan, dict) else None
        if not isinstance(plan, dict) or set(plan) != {
                "digest", "modules", "planned"}:
            raise ValueError("sharded root plan schema is invalid")
        if not isinstance(digest, str) or len(digest) != 64 \
                or digest != digest.lower() \
                or any(ch not in string.hexdigits for ch in digest):
            raise ValueError("sharded root plan digest is invalid")
        if not isinstance(modules, list) \
                or not all(isinstance(name, str) and name for name in modules) \
                or modules != sorted(modules):
            raise ValueError("sharded root plan modules are invalid")
        if type(planned) is not int or planned < 0:
            raise ValueError("sharded root plan count is invalid")
    for name in ("pid", "start", "owner_pid", "owner_start"):
        if type(row[name]) is not int or row[name] <= 0:
            raise ValueError("sharded root process identity is invalid")
    if type(row["bins"]) is not list or not all(
            type(names) is list and all(isinstance(name, str) and name
                                        for name in names)
            for names in row["bins"]) \
            or type(row["assignments"]) is not list \
            or type(row["samples"]) is not list \
            or not all(isinstance(sample, dict) for sample in row["samples"]) \
            or [sample.get("phase") for sample in row["samples"]] != [
                "launch", "steady", "peak"]:
        raise ValueError("sharded root assignment schema is invalid")
    counts = row["counts"]
    if type(counts) is not dict \
            or set(counts) != set(gatetestrecord.COUNT_FIELDS) | {"ok"}:
        raise ValueError("sharded root counts are invalid")
    for name in gatetestrecord.COUNT_FIELDS:
        if type(counts[name]) is not int or counts[name] < 0:
            raise ValueError("sharded root count is invalid")
    if type(counts["ok"]) is not bool or type(row["rc"]) is not int \
            or row["rc"] not in (0, 1) \
            or (row["rc"] == 0) != counts["ok"]:
        raise ValueError("sharded root status is invalid")
    return row


def _validated_sharded_evidence(arm):
    """One complete evidence object; malformed input always raises ValueError."""
    try:
        return _validate_sharded_evidence(arm)
    except (TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ValueError("sharded evidence is malformed: %s" % exc) from exc


def _validate_sharded_evidence(arm):
    """One normalized evidence object, or ValueError. -> dict

    ONE CONSTRUCTOR, NO FIELD-BY-FIELD FALLBACK. Grown a check at a time,
    each addition is individually correct while the ORDER quietly decides
    what the checks can see.

    THE ORDER IS THE DESIGN. Filename-derived identity comes FIRST, before
    anything is classified: a row's file is derived from the row itself and
    must be the key it was stored under, so filename, bytes and content are
    one object. Classifying first and verifying names afterwards leaves the
    census downstream of the thing being checked -- a swapped filename still
    chooses which typed row enters by_shard, and every later arrow then binds
    correctly against the wrong row.

    WHAT THIS PROVES AND WHAT IT DOES NOT. It proves the run is internally
    consistent with its own plan: every hop witnessed, every generation exact,
    the plan digest recomputing from the workers' own planned ids. IT IS NOT
    AN INDEPENDENT ORACLE. The plan and the evidence come from one execution,
    so a discovery that under-enumerated agrees with itself perfectly here.
    Landing authority additionally requires a fresh CONTAINED rediscovery,
    which does not exist yet.
    """
    files = arm.get("artifact_files")
    if not isinstance(files, dict) or not files:
        raise ValueError("measurement collection preserved no byte evidence")

    # A: envelopes, and identity derived FROM THE ROW
    rows = []
    for name, envelope in sorted(files.items()):
        if not isinstance(envelope, dict) \
                or set(envelope) != {"basename", "raw_sha256", "row"}:
            raise ValueError("artifact envelope schema is invalid")
        if envelope["basename"] != name:
            raise ValueError("artifact envelope key is not its basename")
        digest = envelope["raw_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 \
                or digest != digest.lower() \
                or any(ch not in string.hexdigits for ch in digest):
            raise ValueError("artifact envelope digest is invalid")
        row = envelope["row"]
        if not isinstance(row, dict):
            raise ValueError("artifact envelope row is not an object")
        try:
            derived = gatetestrecord.artifact_basename(row)
        except (KeyError, TypeError):
            raise ValueError("artifact row cannot name its own file")
        if derived != name:
            raise ValueError("artifact filename does not match its row")
        rows.append(row)

    # B: typed census, from the validated identities only
    roots = [row for row in rows if row.get("type") == "sharded-root"]
    supervisors = [row for row in rows
                   if row.get("type") == "worker-supervisor"]
    workers = [row for row in rows if row.get("role") == "worker"]
    if len(roots) != 1 or not workers \
            or len(workers) != len(supervisors) \
            or len(roots) + len(workers) + len(supervisors) != len(rows):
        raise ValueError("sharded arm contains missing or foreign artifacts")
    root = _root_artifact(roots[0], arm["token"])
    if root["v"] != 2:
        raise ValueError("canonical authority requires a plan-bearing root")
    workers = [gatetestrecord.validate_artifact(row, arm["token"])
               for row in workers]
    supervisors = [gatetestrecord.validate_supervisor_artifact(
        row, arm["token"]) for row in supervisors]
    if root["owner_pid"] != arm["owner"]["pid"] \
            or root["owner_start"] != arm["owner"]["start"]:
        raise ValueError("sharded root owner is foreign")

    # THE ASSIGNMENT SCHEMA IS EXACT. Accepting unknown keys lets a root
    # carry a field the binder never examines -- an assignment claiming an
    # authority of its own reads as equivalent, because nothing refuses what
    # nothing reads.
    for row in root["assignments"]:
        if not isinstance(row, dict) \
                or set(row) != {"index", "modules", "pid", "start"}:
            raise ValueError("sharded assignment schema is invalid")
        for key in ("index", "pid", "start"):
            _positive(row[key], "sharded assignment " + key)
    assignments = {row["index"]: row for row in root["assignments"]}
    count = len(root["bins"])
    if not count or len(assignments) != len(root["assignments"]) \
            or set(assignments) != set(range(1, count + 1)) \
            or len(workers) != count or len(supervisors) != count:
        raise ValueError("sharded assignment census is incomplete")
    by_shard = {row["shard"]: row for row in workers}
    chains = {row["shard"]: row for row in supervisors}
    if len(by_shard) != count or set(by_shard) != set(assignments) \
            or len(chains) != count or set(chains) != set(assignments):
        raise ValueError("shard ownership is duplicated or missing")

    # THE ROWS AND THE BYTES ARE ONE COLLECTION, checked by IDENTITY rather
    # than equality: two rows that merely compare equal are not the same row,
    # and the binder later asserts `is` against these objects.
    parsed = [envelope["row"] for _n, envelope in sorted(files.items())]
    if collections.Counter(map(id, parsed)) != collections.Counter(
            map(id, arm["artifacts"])):
        raise ValueError("artifact rows disagree with their byte evidence")

    identities = [(row["pid"], row["start"])
                  for row in [root] + workers + supervisors]
    identities.append((root["owner_pid"], root["owner_start"]))
    if len(identities) != len(set(identities)):
        raise ValueError("sharded process generation is assigned more than once")

    seen = set()
    for one in root["bins"]:
        if len(one) != 1 or not one[0] or one[0] in seen:
            raise ValueError("canonical authority requires one module per "
                             "process")
        seen.add(one[0])

    # C-E LIVE HERE, NOT IN THE CALLER. This function's contract is that a
    # returned object is fully validated evidence; leaving throughput, the
    # three-hop binder and the plan binding outside it made "one
    # constructor" true of the SHAPE and false of the VALIDATION, so a
    # second caller could obtain an object that had passed A-B only.
    envelopes = files

    assigned_pids = {assignment.get("pid")
                     for assignment in assignments.values()}
    for position, measurement in enumerate(root["samples"]):
        reason = _sample_reason(measurement, workers=True)
        if reason:
            raise ValueError(reason)
        sampled = set(measurement["workers"])
        if not sampled <= assigned_pids \
                or position == 0 and sampled != assigned_pids:
            raise ValueError("worker throughput sample is missing or foreign")
    supervisors_by_shard = chains

    for index, assignment in assignments.items():
        worker = by_shard[index]
        chain = supervisors_by_shard[index]
        # THE CHAIN IS THREE HOPS AND EVERY ARROW IS EXACT IMMEDIATE-PARENT
        # IDENTITY. The pool launched the SUPERVISOR, so the assignment names
        # it; the supervisor launched the INNER, so it names that; and the
        # worker names the supervisor as its root. A reused pid cannot
        # impersonate a generation at any link because each carries start.
        # CANONICAL MEANS ONE MODULE PER PROCESS, AND THE BINDER MUST SAY SO.
        # Checking only that the three hops AGREE on a module list accepts a
        # PACKED bin -- all three coherently naming two modules -- which is
        # exactly the co-location that lets a producer and its consumer share
        # an interpreter and hide. Agreement is not isolation.
        if len(root["bins"][index - 1]) != 1:
            raise ValueError("canonical authority requires one module per "
                             "process")
        if assignment.get("modules") != root["bins"][index - 1] \
                or chain["modules"] != assignment.get("modules") \
                or chain["pid"] != assignment.get("pid") \
                or chain["start"] != assignment.get("start"):
            raise ValueError("supervisor process or module assignment is "
                             "foreign")
        if chain["root_pid"] != root["pid"] \
                or chain["root_start"] != root["start"]:
            raise ValueError("supervisor owner is foreign")
        if worker["modules"] != chain["modules"] \
                or worker["shard"] != chain["shard"] \
                or worker["pid"] != chain["inner_pid"] \
                or worker["start"] != chain["inner_start"] \
                or worker["root_pid"] != chain["pid"] \
                or worker["root_start"] != chain["start"]:
            raise ValueError("worker process or module assignment is foreign")
        # THE BYTES, NOT A RE-SERIALIZATION. The supervisor claims a SHA-256
        # of the worker file; the envelope preserved that digest at the only
        # moment the bytes existed.
        basename = "%s-worker-%s-%s.json" % (
            arm["token"], chain["shard"], chain["inner_pid"])
        if basename not in envelopes:
            raise ValueError("worker artifact named by the chain is missing")
        if envelopes[basename]["raw_sha256"] \
                != chain["inner_artifact_digest"]:
            raise ValueError("worker artifact digest does not match the "
                             "supervisor claim")
        # The row this basename names IS this worker, by construction rather
        # than by a check here: the constructor derives every envelope's
        # filename FROM ITS OWN ROW and refuses a mismatch, and by_shard is
        # keyed from those validated rows. An identity comparison at this
        # point is tautological -- no input reaches it.
        gatetestrecord.certifying_supervisor_artifact(chain, arm["token"])
        if chain["inner_rc"] not in (0, 1) \
                or (chain["inner_rc"] == 0) != bool(worker["counts"]["ok"]):
            raise ValueError("supervisor inner result disagrees with the "
                             "worker outcome")
    # THE PLAN IS BOUND TO THE EVIDENCE, NOT MERELY SHAPED. A v2 root with a
    # syntactically perfect but FALSE plan -- a zero digest, the wrong module
    # list, an invented count -- passed the shape gate and authorized. A shape
    # check wearing a verification's name is the same defect as a digest that
    # only proves it is 64 characters long.
    # CANONICAL AUTHORITY IS v2 ONLY. A v1 root carries no planning evidence
    # at all, so there is nothing to bind and nothing to compare against an
    # independent re-derivation; accepting one here would authorize on the
    # absence of the evidence rather than on the evidence.
    if root["v"] != 2:
        raise ValueError("canonical authority requires a plan-bearing root")
    plan = root["plan"]
    census = sorted(name for one in root["bins"] for name in one)
    if plan["modules"] != census:
        raise ValueError("root plan module census does not match its bins")
    planned_total = sum(len(row["planned"]) for row in workers)
    if plan["planned"] != planned_total:
        raise ValueError("root plan count does not match the worker census")
    counts = {name: len(by_shard[index]["planned"])
              for index, one in enumerate(root["bins"], 1)
              for name in one}
    ids = {name: list(by_shard[index]["planned"])
           for index, one in enumerate(root["bins"], 1)
           for name in one}
    if plan["digest"] != gateshard.plan_digest(counts, ids):
        raise ValueError("root plan digest does not bind the worker evidence")
    sums = {name: sum(row["counts"][name] for row in workers)
            for name in gatetestrecord.COUNT_FIELDS}
    sums["ok"] = all(row["counts"]["ok"] for row in workers)
    if sums != root["counts"]:
        raise ValueError("worker outcomes disagree with the root aggregate")
    receipt = arm["receipt"]
    if receipt["ran"] != sums["ran"] \
            or receipt["skipped"] != sums["skipped"] \
            or (receipt["status"] == "OK") != sums["ok"]:
        raise ValueError("sharded aggregate disagrees with its receipt")

    # THE OBJECT IS RETURNED ONLY AFTER EVERY STAGE HAS PASSED.
    return {
        "root": root, "workers": workers, "supervisors": supervisors,
        "assignments": assignments, "by_shard": by_shard, "chains": chains,
        "envelopes": files, "count": count,
    }


