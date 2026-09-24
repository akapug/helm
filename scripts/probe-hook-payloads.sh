#!/bin/sh
# Capture REAL Claude Code hook payloads (trigger design 7.1, CRITIC BLOCK 4).
#
# A field the harness schema lists is TRACED; only a live payload proves it.
# This builds a scratch git repo with a guaranteed merge conflict, runs one
# headless `claude -p` there with dump-only hooks passed by --settings (the
# user's own settings are NOT loaded: --setting-sources project in a directory
# with none), and prints each payload's SHAPE (helm.moments.payload_shape: keys,
# kinds, lengths — never content). Nothing in any settings file is changed.
#
# usage: scripts/probe-hook-payloads.sh [workdir]    (default: a mktemp dir)
# needs: claude on PATH, a logged-in account; costs one short headless run.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
w=${1:-$(mktemp -d "${TMPDIR:-/tmp}/helm-hookprobe.XXXXXX")}
mkdir -p "$w/repo" "$w/dump"
cd "$w/repo"
git init -q -b main
c() { git -c user.name=probe -c user.email=probe@example.invalid commit -q "$@"; }
printf 'a\nb\n' > f.txt; git add f.txt; c -m base
git checkout -q -b feature; printf 'a\nFEATURE\n' > f.txt; c -am feat
git checkout -q main; printf 'a\nMAIN\n' > f.txt; c -am main
cat > "$w/dump.sh" <<DUMP
#!/bin/sh
cat > "$w/dump/\$1.\$(date +%s%N).json"
exit 0
DUMP
chmod +x "$w/dump.sh"
python3 - "$w" <<'PY'
import json, sys
w = sys.argv[1]
ev = ("UserPromptSubmit", "PostToolBatch", "PostToolUse", "PostToolUseFailure",
      "Stop", "SubagentStart", "SubagentStop", "SessionStart")
hooks = {}
for e in ev:
    ent = {"hooks": [{"type": "command", "command": "%s/dump.sh %s" % (w, e),
                      "timeout": 5}]}
    if e not in ("UserPromptSubmit", "PostToolBatch", "Stop"):
        ent["matcher"] = "*"
    hooks[e] = [ent]
json.dump({"hooks": hooks}, open(w + "/settings.json", "w"))
PY
P='Hook-payload probe in a disposable scratch repo. Step 1: in ONE message make TWO parallel Bash calls, "git merge feature" and "rg zzz-no-such-token .", both expected to fail. Step 2: spawn one general-purpose subagent with the prompt: Reply OK and use no tools. Step 3: run "sleep 2; echo done" in the background and wait for its notification. Step 4: reply: probe done.'
env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_PID -u HELM_CHAT_NAME \
    claude -p "$P" --setting-sources project --strict-mcp-config \
    --settings "$w/settings.json" --allowedTools Bash Agent --max-turns 14 \
    --output-format json < /dev/null > "$w/child.out" 2> "$w/child.err" || true
PYTHONPATH="$here" python3 - "$w" <<'PY'
import glob, json, os, sys
from helm import moments
for p in sorted(glob.glob(sys.argv[1] + "/dump/*.json"),
                key=lambda p: p.rsplit(".", 2)[-2]):
    try:
        print(json.dumps(moments.payload_shape(json.load(open(p)))))
    except ValueError:
        print(json.dumps({"unreadable": os.path.basename(p)}))
PY
echo "payloads kept in $w/dump" >&2
