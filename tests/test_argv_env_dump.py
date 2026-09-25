#!/usr/bin/env python3
"""The env-dump rung (task/3037): a Bash or Monitor command never prints the
environment, the value of a secret-looking variable or a credentials file into
the transcript.

WHY. In another project a real production secret reached a model provider's
transcript. An agent ran a command that printed a box's whole environment, and
the transcript carries every byte a command prints. Nothing in the argv-guard
had a rung for that shape.

THE MATRIX IS THE TASK'S OWN. Each arm below is one row of the surface-by-state
matrix task/3037 set: refuse rows 1 to 6, pass rows 7 to 10. The pass rows are
the refusal's must-miss: a rung that refused every command would satisfy every
refusal arm forever. The hook arms drive the SHIPPED entry, `cmd_argv_guard`,
and read its exit code, and one arm runs the real entry script to prove the
rung adds no import to the hook path.

The probe strings are built from pieces (`E`, `P`, `X`) so that this file, read
by a person or grepped by a seat, never holds a dump a shell could run whole.
"""
import contextlib
import io
import json
import os as _os
import subprocess
import sys as _sys
import unittest
from unittest import mock

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-envdump-", var="HELM_HOME")

from helm import chat  # noqa: E402

E, P, X = "en" + "v", "print" + "env", "ex" + "port"
S, D, T = "se" + "t", "decl" + "are", "type" + "set"
KEY = "API" + "_KEY"
PROC = "/proc/%s/envir" + "on"


def refusal(command):
    return chat.env_dump_refusal(command)


class RefuseMatrixTest(unittest.TestCase):
    """Rows 1 to 6: each command prints values into the transcript, and the
    rung names the kind of what it prints."""

    def refused(self, commands, kind):
        for command in commands:
            with self.subTest(command=command):
                hit = refusal(command)
                self.assertIsNotNone(hit, command)
                self.assertEqual(hit[0], kind, (command, hit))

    def test_row1_a_whole_environment_dump_alone_or_in_a_pipeline(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            E, P, X, S, D + " -p", D + " -x", T + " -p", X + " -p",
            E + " | sort", E + " | grep -i key", P + " | head",
            E + " | grep HELM", S + " | less", E + " -u X", E + " VAR=x",
            "sudo " + E, "sudo -u svc " + P + " | sort", "(" + E + ")",
            "{ " + E + "; } | sort", E + " >&2", E + " 2>/dev/null",
            "cd /tmp && " + E, "for v in $(compgen -v); do echo \"$v=${!v}\"; "
            "done"), "dump")

    def test_row2_export_of_a_substitution_or_a_possibly_empty_list(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """An empty substitution runs a bare export, which prints every
        exported value; so this row is refused wherever the output goes."""
        self.refused((
            X + " $(grep -v '^#' .env | xargs)", X + " $(cat vars)",
            X + " $VARS", X + " `cat vars`", X + " $(cat vars) >/dev/null"),
            "export")

    def test_row3_the_same_dumps_run_remotely_or_in_a_container(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            "ssh host " + E, "ssh host '" + P + "'",
            "ssh -p 2222 -o BatchMode=yes host '" + P + " | sort'",
            "ssh host <<'EOF'\n" + E + "\nEOF",
            "docker exec c " + E, "docker exec -it c sh -c '" + E + "'",
            "docker exec -e A=1 -u root c " + P,
            "docker compose exec web " + E, "docker run --rm img " + E,
            "kubectl exec pod -- " + E, "kubectl -n ns exec pod -- " + P,
            "kubectl exec -it pod -- sh -c '" + E + " | sort'",
            "podman exec c " + E), "dump")
        self.refused((
            "docker inspect c", "docker container inspect c",
            "podman inspect c",
            "docker inspect --format '{{json .Config}}' c",
            "docker inspect --format='{{json .}}' c",
            "docker inspect -f '{{range .Config." + "Env}}{{.}} {{end}}' c",
            "docker inspect c | jq '.[0].Config'"), "inspect")

    def test_row4_a_process_environment_read_by_any_reader(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            "cat " + PROC % "self", "strings " + PROC % "123",
            "tr '\\0' '\\n' < " + PROC % "$pid",
            "xargs -0 -n1 < " + PROC % "1", "xargs -0 -a " + PROC % "1",
            "cat " + PROC % "${PID}" + " | tr '\\0' '\\n'",
            "sudo cat " + PROC % "1", "ssh host cat " + PROC % "1"), "proc")

    def test_row5_a_credentials_file_printed(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """grep -c, grep -q, grep -l and wc -l print no content and pass
        (PassMatrixTest); every other read of the file prints it."""
        self.refused((
            "cat .env", "cat .env.local", "head -5 prod.env",
            "tail credentials.json", "less ~/.aws/credentials",
            "more .git-credentials", "bat secrets.yaml", "grep KEY .env",
            "grep -rn KEY .env.production", "rg KEY .env",
            "sed -n 1,5p .env", "awk -F= '{print $2}' .env",
            "strings id_rsa", "cat ~/.ssh/id_ed25519", "cat server.pem",
            "cat tls.key", "cat ~/.config/gh/token", "cat .netrc",
            "cat .env | grep KEY", "cut -d= -f2 .env", "jq . creds.json",
            "base64 .env", "diff .env .env.example",
            "while read l; do echo \"$l\"; done < .env",
            "cat \"$HOME/.aws/credentials\"", "ssh host 'cat /srv/app/.env'",
            "docker exec c cat /app/.env",
            "kubectl exec pod -- cat /var/run/secrets/token"), "file")

    def test_row6_a_secret_looking_variable_printed(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        self.refused((
            P + " " + KEY, "echo $" + KEY, "echo \"${" + KEY + "}\"",
            "echo ${" + KEY + ":0:8}", "echo \"${" + KEY + ":-none}\"",
            "printf '%s\\n' \"$GITHUB_TOKEN\"", "echo \"key=$" + KEY + "\"",
            "echo $DB_PASSWORD", "echo $PGPASSWORD", "echo $AWS_SECRET_"
            "ACCESS_KEY", "echo $CLAUDE_CODE_OAUTH_TOKEN",
            "echo $HTTP_AUTHORIZATION", "echo $MASTER_KEY",
            "echo $PRIVATE_KEY", "echo $DB_CREDENTIALS",
            "echo $DATABASE_URL", "echo $PG_DSN", "echo $REDIS_URL",
            "echo $" + KEY + " | head -c 8", "cat <<EOF\n$" + KEY + "\nEOF",
            "cat <<< \"$" + KEY + "\"", D + " -p " + KEY,
            "ssh host 'echo $" + KEY + "'"), "name")

    def test_the_rung_follows_what_a_printer_or_a_shell_runs(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """A dump inside a printer's substitution, a reader's process
        substitution, a shell's -c string or a body a shell reads prints
        exactly as the bare dump does."""
        cases = (("echo \"$(cat .env)\"", "file"),
                 ("echo `" + P + "`", "dump"),
                 ("diff <(" + E + ") <(ssh h " + E + ")", "dump"),
                 ("cat <(" + E + ")", "dump"),
                 ("bash <<'EOF'\n" + E + "\nEOF", "dump"),
                 ("bash -lc '" + E + " | sort'", "dump"),
                 ("sh -e -o pipefail -c '" + P + "'", "dump"),
                 ("su -c '" + E + "' svc", "dump"),
                 (E + " -S '" + P + "'", "dump"),
                 ("time -p " + E, "dump"))
        for command, kind in cases:
            with self.subTest(command=command):
                hit = refusal(command)
                self.assertIsNotNone(hit, command)
                self.assertEqual(hit[0], kind)

    def test_eval_runs_its_argument_in_this_shell(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """`eval` joins its words and runs them in the CURRENT shell, so a
        dump reaches this terminal exactly as `bash -c` does; the rung follows
        it like the other shell-eval forms (task/3037)."""
        cases = (("eval " + E, "dump"),
                 ("eval '" + P + "'", "dump"),
                 ("eval \"" + E + "\"", "dump"),
                 ("eval '" + E + " | sort'", "dump"),
                 ("eval -- " + E, "dump"),
                 ("true; eval " + P, "dump"),
                 ("eval 'cat .env'", "file"),
                 ("eval 'echo $" + KEY + "'", "name"))
        for command, kind in cases:
            with self.subTest(command=command):
                hit = refusal(command)
                self.assertIsNotNone(hit, command)
                self.assertEqual(hit[0], kind, (command, hit))

    def test_a_formatter_prints_a_file_operand_or_its_stdin(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """fmt/pr/expand/unexpand print a file operand exactly as cat does,
        and tee/fmt print what a `<` redirect hands their stdin; the sibling
        formatters column/fold/paste/rev/nl are caught the same way
        (task/3037)."""
        self.refused((
            "fmt .env", "pr .env", "expand .env", "unexpand .env",
            "pr credentials.json"), "file")
        self.refused((
            "tee < .env", "fmt < .env", "expand < .env"), "file")
        self.refused((
            "tee < " + PROC % "1", "fmt < " + PROC % "1",
            "fmt " + PROC % "1"), "proc")

    def test_a_process_substitution_carries_a_dump_both_ways(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """A reader prints what a command in `<(…)` prints, whether it takes
        it as an operand, through a redirect, a here-string or a heredoc
        body. The command in an output substitution `>(…)` writes to THIS
        shell's stdout, so a dump run in one, or written into one whose
        command passes its input on, reaches the transcript. The outsider
        QC's own three spellings open the list (task/3037)."""
        self.refused((
            "cat <(" + E + ")", "paste <(" + P + ")", "diff <(" + E + ") x",
            "cat < <(" + E + ")", "tee < <(" + E + ")",
            "while read l; do echo \"$l\"; done < <(" + P + ")",
            "cat <<< \"$(" + E + ")\"", "cat <<EOF\n$(" + P + ")\nEOF",
            "cat <<EOF\nvars: `" + E + "`\nEOF",
            E + " > >(cat)", P + " > >(sort | head)",
            E + " | tee >(cat) > /dev/null",
            E + " | tee /dev/stderr > /dev/null", E + " >&2 | wc -l",
            "true >(" + E + ")", "echo hi > >(" + P + ")",
            "comm <(" + E + ") x", "join <(" + E + ") x"), "dump")
        self.refused((
            "cat .env > >(cat)", "echo \"$(< .env)\"",
            "printf '%s' \"$(<token)\"", "echo `< .env`",
            "join .env other"), "file")
        self.refused(("echo $" + KEY + " > >(cat)",), "name")

    def test_jq_loads_a_credentials_file_into_a_variable_it_prints(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """--rawfile and --slurpfile READ their file into a variable the
        program can print, so a credentials file there is a read that
        prints (task/3037, outsider QC)."""
        self.refused((
            "jq -n --rawfile t .env '$t'",
            "jq -n --slurpfile c credentials.json '$c'",
            "jq --rawfile k server.pem '.key = $k' tmpl.json",
            "jq -n --rawfile t .env '$t' | head",
            "jq -n --arg a 1 --rawfile t ~/.config/gh/token '$t'"), "file")

    def test_an_output_to_the_terminal_is_still_a_print(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """An output option or operand that names stdout, stderr or a
        printing process substitution prints; and an option's VALUE is not
        the output operand, so `uniq -f 1 .env` and `xxd -l 16 .env` still
        read .env to the terminal (task/3037)."""
        self.refused((
            "sort -o /dev/stdout .env", "sort -o - .env",
            "sort -o /dev/stderr .env > /dev/null", "sort -o >(cat) .env",
            "cat .env | sort -o /dev/stderr", "sort -S 1M .env",
            "sort -t = -k2 .env", "uniq .env", "uniq .env -",
            "uniq -f 1 .env", "uniq -w 8 .env", "xxd .env", "xxd .env -",
            "xxd -l 16 .env", "xxd -c 8 .env", "xxd -ps .env",
            "base64 -i .env", "base64 -di .env", "base64 -w0 .env"), "file")

    def test_iconv_prints_a_file_operand_or_its_stdin(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        """iconv prints a file operand as cat does. It sat only in
        _ENV_PASSES because its -o would have been read as a read operand;
        with -o read as the file it writes, it is a reader (task/3037)."""
        self.refused((
            "iconv -f UTF-8 -t ASCII .env", "iconv -t UTF-8 < .env",
            "iconv -c -t ascii//TRANSLIT credentials.json",
            "iconv -o /dev/stdout -t ascii .env"), "file")


class PassMatrixTest(unittest.TestCase):
    """Rows 7 to 10, and the flows that print no value. Each arm opens with
    the refusal it is the must-miss of, on the same rung."""

    def passes(self, control, commands):
        self.assertIsNotNone(refusal(control), "control: " + control)
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(refusal(command), command)

    def test_row7_env_as_a_launcher(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        self.passes(E, (
            E + " VAR=value cmd", E + " -u VAR cmd", E + " -i cmd",
            E + " -i HOME=/h PATH=/bin bash -lc 'make'",
            E + " -u CLAUDECODE claude -p hi", E + " -i",
            E + " -i A=1", "/usr/bin/" + E + " LC_ALL=C sort file",
            "cmd | " + E + " LC_ALL=C sort"))

    def test_row8_a_presence_check_prints_no_value(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        self.passes("echo $" + KEY, (
            "[ -n \"$" + KEY + "\" ] && echo set",
            P + " " + KEY + " >/dev/null && echo set",
            P + " " + KEY + " > /dev/null 2>&1 && echo set",
            "test -n \"${" + KEY + ":-}\"", "echo ${#" + KEY + "}",
            "echo \"${" + KEY + ":+set}\"", "echo '$" + KEY + "'",
            E + " | grep -q '^" + KEY + "=' && echo set",
            E + " | grep -c " + KEY, "echo \"$" + KEY + "\" | wc -c"))

    def test_row9_the_words_as_data(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        self.passes(E + " | sort", (
            "echo \"run " + E + " to see\"",
            "git commit -m 'guard " + P + " and " + E + " | sort'",
            "git grep -n " + P, "git grep -n '" + E + " |'",
            "rg -n '" + E + " \\|' helm/",
            "grep -rn \"" + PROC % "self" + "\" helm/",
            "grep -rn token src/", "grep -rn credentials docs/",
            "cat > x.sh <<'EOF'\n" + E + "\n" + P + " | sort\nEOF",
            "git commit -F - <<'EOF'\n" + E + "\n" + X + "\nEOF",
            "helm chat post 'do not run " + E + " | sort'",
            "git commit -m \"$(cat <<'EOF'\n" + P + " | sort\nEOF\n)\"",
            "python3 -c 'import os'", "ls " + E + "/"))

    def test_row10_export_assignments_and_set_options(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        self.passes(S, (
            X + " NAME=value", X + " PATH=\"$HOME/bin:$PATH\"",
            X + " A=1 B=2", X + " NAME", X + " -n NAME", X + " -f fn",
            X + " \"$ONE\"", X + " GH_TOKEN=$(cat ~/.config/gh/token)",
            S + " -e", S + " -euo pipefail", S + " -x", S + " +x",
            S + " -- a b", S + " -o", D + " -A m=([a]=1)", D + " -F",
            D + " -p HOME", T + " -i n=1", "compgen -v | grep AWS"))

    def test_a_consumer_a_file_or_a_names_only_filter_stops_the_print(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        """A value handed to the command that needs it, written to a file or
        reduced to its name never reaches the transcript."""
        self.passes("cat .env", (
            "grep -c KEY .env", "grep -q '^K=' .env", "grep -l KEY .env",
            "rg -c KEY .env", "wc -l .env", "cat .env > backup",
            "cat .env | ssh h 'cat > .env'", "x=$(cat token)",
            P + " GITHUB_TOKEN | gh auth login --with-token",
            "echo \"$GITHUB_TOKEN\" | wrangler secret put X",
            "cut -d= -f1 .env", "cut -d '=' -f 1 .env",
            "sed 's/=.*//' .env", "awk -F= '{print $1}' .env",
            E + " | cut -d= -f1", E + " | cut -d= -f1 | sort",
            E + " | wc -l", E + " > /tmp/env-snapshot",
            "sed -i 's/^A=.*/A=b/' .env",
            "set -a; . ./.env 2>/dev/null; set +a",
            "source .env && make", "ls -la ~/.aws/credentials",
            "stat .env", "test -f .env && echo present",
            "ssh host 'cat > /tmp/x' < .env",
            "tr '\\0' '\\n' < " + PROC % "1" + " | grep -c '^HELM_CHAT_NAME='",
            "docker inspect -f '{{.State.Status}}' c",
            "docker inspect --format '{{json .Config.Labels}}' c",
            "docker inspect -f 'n={{len .Config." + "Env}}' c",
            "docker network inspect n", "docker volume inspect v",
            "docker exec c " + P + " HOME", "kubectl exec pod -- ls",
            "ssh host ls", "ssh host 'cat > /tmp/x'"))

    def test_names_and_files_that_are_not_secrets(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        self.passes("cat .env", (
            P + " HOME", "echo $PATH", "echo \"$HOME\"",
            "echo $SSH_AUTH_SOCK", "echo $GIT_AUTHOR_NAME",
            "echo $MAX_TOKENS", "echo $SSH_KEY_PATH", "echo $KEYBOARD",
            "for key in a b; do echo \"$key\"; done", "echo $token_count",
            "cat .env.example", "cat .env.sample", "cat helm/tokens.py",
            "cat ~/.ssh/id_rsa.pub", "cat tokenizer.json",
            "cat docs/credentials.md", "head src/secrets.ts",
            "cat secret_watch.example.yml", "tail creds-gate.log",
            "cat duetoken-notes.txt", "rg -n -g '*.py' '^def ident_token' .",
            "diff <(sort a) <(rg -v 'key|token|secret' b)"))

    def test_eval_that_prints_no_value_passes(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        """eval whose command consumes a value or prints none of it is a
        must-miss: the value never reaches the transcript (task/3037)."""
        self.passes("eval " + E, (
            "eval make", "eval 'ls -la'",
            "eval \"$(direnv hook bash)\"", "eval \"$(ssh-agent -s)\"",
            "eval \"$(cat .env)\""))

    def test_a_formatter_that_writes_or_counts_passes(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        """A formatter whose output goes to a file, or whose operand is a
        write target (tee) or a non-secret file, prints no secret."""
        self.passes("fmt .env", (
            "fmt .env > /tmp/wrapped", "cat notes.md | tee .env",
            "fmt README.md", "pr helm/chat.py", "expand Makefile"))

    def test_an_output_option_or_operand_names_a_file_written(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        """An output option (`sort -o`, BSD `base64 -o`) or output operand
        (`uniq IN OUT`, `xxd IN OUT`) names a file the reader WRITES in
        place of its stdout: it is never read, and the reader prints nothing
        (task/3037, outsider QC). `yq -i` writes in place as `sed -i`
        does, and a bare digit is an fd only after `>&`: `> 2` and `-o 1`
        name files."""
        self.passes("sort .env", (
            "sort -o out.txt .env", "sort -o .env.sorted data.txt",
            "sort -uo out .env", "sort --output=out .env",
            "sort --output out .env", "sort -k2 -t= -o out .env",
            "sort -o .env .env", E + " | sort -o /tmp/snap",
            "cat .env | sort -o out", "uniq .env out.txt",
            "uniq -c data .env.dedup", "uniq -f 1 .env out",
            "xxd .env out.hex", "xxd -r dump.hex .env",
            "xxd -c 8 .env out.hex", "base64 -i .env -o out.b64",
            "yq -i '.a = 1' secrets.yaml", "sort -o >(wc -l) .env",
            "sort -o 1 .env", E + " > 2",
            "iconv -f UTF-8 -t ASCII -o out.txt .env",
            "iconv -o token.txt src.txt", "iconv --output=out .env"))

    def test_jq_loads_a_file_for_what_needs_it(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        """A loaded file sent to a file or a consumer prints nothing, and
        --arg's NAME and VALUE are not files."""
        self.passes("jq -n --rawfile t .env '$t'", (
            "jq -n --rawfile t .env '$t' > payload.json",
            "jq -n --rawfile t .env '$t' | wc -c",
            "jq -n --rawfile k tls.key '{k: $k}' | curl -d @- https://x",
            "jq --rawfile t notes.md . x.json",
            "jq --arg token x . data.json"))

    def test_a_process_substitution_that_counts_or_writes_passes(self):  # noqa: VACUOUS_ASSERTION — `passes` asserts the refusal of its control on the same rung before any absence
        """A substitution whose command counts, stores or consumes what it
        reads, a quoted heredoc (which substitutes nothing) and `$(< FILE)`
        assigned to a variable print no value."""
        self.passes(E + " > >(cat)", (
            E + " > >(wc -l)", E + " > >(cat > /tmp/snap)",
            "cat .env > >(wc -c)", "wc -l < <(" + E + ")",
            "cat <(" + E + ") | wc -l", "x=$(< .env)",
            "x=$(true >(" + E + "))", "cat <<'EOF'\n$(" + E + ")\nEOF",
            "gh auth login --with-token < <(" + P + " GH_TOKEN)",
            "cat <<< \"$(date)\"", "echo \"$(< notes.md)\"",
            "cat <<EOF\nrun `date`\nEOF"))


class NarrowingTest(unittest.TestCase):
    """A grep that selects exact names which do not look secret prints no
    secret: the census of this fleet's own commands found most reads of a
    process environment were `tr '\\0' '\\n' < /proc/PID/environ | grep
    '^HELM_CHAT_NAME='`, a seat's identity. A prefix, an unbounded name, a
    secret name, -v, -a, -f, a context line or -o over a value prints more,
    and is refused."""

    TR = "tr '\\0' '\\n' < " + PROC % "1" + " | "

    def test_exact_names_narrow_a_dump(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the same source unnarrowed, refused on the same rung
        self.assertEqual(refusal(self.TR + "sort")[0], "proc")
        for command in (
                self.TR + "grep '^HELM_CHAT_NAME='",
                self.TR + "grep -m1 ^HELM_CHAT_NAME=",
                self.TR + "grep -E '^(HELM_CHAT_NAME|HELM_SEAT)='",
                E + " | grep -E '^HOME=.*'", E + " | grep -w HELM_CHAT_NAME",
                E + " | grep -o '^HELM_[A-Z_]*'",
                E + " | grep -oE '^(GH_TOKEN|GITHUB_TOKEN)='",
                P + " | grep -oE '^[A-Z_]+'", "grep '^HELM_HOME=' .env",
                "grep ^HELM_CHAT_NAME= " + PROC % "1"):
            with self.subTest(command=command):
                self.assertIsNone(refusal(command), command)

    def test_a_filter_that_can_print_a_secret_does_not_narrow(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        for command in (
                E + " | grep HELM", E + " | grep '^HELM_'",
                E + " | grep -E '^(" + KEY + ")='", E + " | grep -v X",
                E + " | grep -A1 '^HOME='", self.TR + "grep -a '^HOME='",
                self.TR + "grep 'HELM_CHAT_NAME|CLAUDE_SESSION'",
                self.TR + "grep -E '^(HELM_|CLAUDE_)'",
                E + " | grep -oE '^GH_TOKEN=.+'", E + " | grep -o 'TOKEN.*'",
                E + " | grep -f patterns", "grep '^DATABASE_URL=' .env"):
            with self.subTest(command=command):
                self.assertIsNotNone(refusal(command), command)


class SecretNameTest(unittest.TestCase):
    """`_secret_name`, both directions, on the matrix's own words."""

    def test_secret_looking_names(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        for name in ("API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                     "DB_PASSWORD", "PGPASSWORD", "DB_PASS", "OPENAI_APIKEY",
                     "CLAUDE_CODE_OAUTH_TOKEN", "HTTP_AUTHORIZATION",
                     "MASTER_KEY", "PRIVATE_KEY", "NPM_CREDS", "KEY",
                     "SMTP_PASSWD", "GPG_PASSPHRASE", "AUTH_TOKEN",
                     "DATABASE_URL", "PG_DSN", "REDIS_URL"):
            with self.subTest(name=name):
                self.assertTrue(chat._secret_name(name), name)

    def test_names_that_hold_no_secret(self):  # noqa: VACUOUS_ASSERTION — every finite tuple arm executes; its positive twin is test_secret_looking_names on the same predicate
        for name in ("HOME", "PATH", "HELM_CHAT_NAME", "SSH_AUTH_SOCK",
                     "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "XAUTHORITY",
                     "MAX_TOKENS", "KEYBOARD", "PASSTHROUGH", "SSH_KEY_PATH",
                     "PRIVATE_KEY_FILE", "SECRETS_DIR", "api_key", "key",
                     "_", "TOKENIZERS_PARALLELISM", "ORCA_PANE_KEY",
                     "SORT_KEY", "ANTHROPIC_BASE_URL", "API_URL"):
            with self.subTest(name=name):
                self.assertFalse(chat._secret_name(name), name)


class UnsettledTextTest(unittest.TestCase):
    """Where the shell reader cannot settle the text, a bare dump standing
    as a command of its own is still refused; and a defect in the reader
    reads the text that way instead of opening the rung."""

    CASE = "case $x in a) %s;; esac"

    def test_the_reader_really_cannot_settle_the_control(self):
        with self.assertRaises(chat._Unsettled):
            chat._ShellReader(self.CASE % E).read()

    def test_a_bare_dump_in_unsettled_text_is_refused(self):
        hit = refusal(self.CASE % E)
        self.assertEqual(hit, ("dump", E))
        self.assertEqual(refusal(self.CASE % (X + " -p")),
                         ("dump", X + " -p"))

    def test_unsettled_text_without_a_bare_dump_passes(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal on the same rung over the same shape
        self.assertIsNotNone(refusal(self.CASE % E))
        self.assertIsNone(refusal(self.CASE % (E + " A=1 make")))
        self.assertIsNone(refusal(self.CASE % (S + " -e")))

    def test_unsettled_text_reads_no_quote_and_no_heredoc_body(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the refusal on the same rung over the same case shape
        """The census: every refusal this reading made when it read quotes
        and bodies too was prose, a markdown table in a --body or a line of
        python."""
        self.assertIsNotNone(refusal(self.CASE % E))
        for command in (
                self.CASE % "echo \"| a | %s | b |\"" % S,
                self.CASE % "gh pr create --body '\n| %s |\n'" % E,
                "case $x in a) cat <<'EOF'\n%s\nEOF\n;; esac" % S,
                "case $x in a) python3 - <<EOF\n%s\nEOF\n;; esac" % E):
            with self.subTest(command=command):
                self.assertIsNone(refusal(command), command)

    def test_a_reader_defect_falls_back_to_the_bare_dump_reading(self):
        with mock.patch.object(chat, "_env_source",
                               side_effect=RuntimeError("defect")):
            self.assertEqual(refusal(E), ("dump", E))
            self.assertIsNone(refusal("ls -la"))

    def test_the_spelling_is_capped(self):
        name = "A" * 200 + "_KEY"
        hit = refusal("echo $" + name)
        self.assertEqual(hit[0], "name")
        self.assertEqual(len(hit[1]), chat._ENV_SPELL)
        self.assertTrue(hit[1].endswith("…"))


class HookTest(unittest.TestCase):
    """The shipped entry: exit 2 and the cure on a refusal, exit 0 on the
    must-miss, for Bash and Monitor alike."""

    def hook(self, tool="Bash", **tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "session_id": "sess-envdump", "cwd": "/tmp/repo",
                   "tool_use_id": "toolu_envdump", "tool_input": tool_input}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_sys, "stdin",
                               io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_argv_guard([])
        return rc, out.getvalue() + err.getvalue()

    def test_a_dump_exits_2_and_names_the_shape_and_the_cure(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        for tool in ("Bash", "Monitor"):
            with self.subTest(tool=tool):
                rc, out = self.hook(tool, command=E + " | sort")
                self.assertEqual(rc, 2, out)
                self.assertIn("[helm argv-guard] BLOCKED", out)
                self.assertIn("the whole environment (%s)" % E, out)
                self.assertIn("[ -n \"$NAME\" ] && echo set", out)
                self.assertIn(E + " | cut -d= -f1", out)

    def test_each_kind_names_its_own_cure(self):  # noqa: VACUOUS_ASSERTION — every arm of a finite tuple literal executes, and each asserts a non-empty refusal
        for command, cure in (
                ("echo $" + KEY, "pass it to the command that needs it"),
                ("cat .env", "set -a; . FILE 2>/dev/null; set +a"),
                ("cat " + PROC % "1", "grep -c '^NAME='"),
                (X + " $(cat vars)", "set -a; . ./.env 2>/dev/null; set +a"),
                ("docker inspect c", "{{.State.Status}}")):
            with self.subTest(command=command):
                rc, out = self.hook(command=command)
                self.assertEqual(rc, 2, out)
                self.assertIn(cure, out)

    def test_the_must_miss_passes_the_entry(self):  # noqa: VACUOUS_ASSERTION — the refusal arm above drives the same entry to rc 2, so rc 0 here is the rung declining
        rc, out = self.hook(command=E + " -u X make")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BLOCKED", out)

    def test_a_write_is_not_a_command(self):  # noqa: VACUOUS_ASSERTION — the refusal arm above drives the same entry to rc 2; a file's CONTENT is never run by this call
        rc, out = self.hook("Write", file_path="/tmp/x.sh",
                            content=E + " | sort\n")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BLOCKED", out)


class EntryImportTest(unittest.TestCase):
    """THE HOOK BUDGET, bound structurally as HookEntryBudgetTest binds it:
    the rung is a string predicate and imports nothing, so a refused dump
    loads exactly the modules a passing command loads."""

    ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def run_hook(self, command):
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': "
            "sorted(sys.modules)}) + '\\n')\n"
            % _os.path.join(self.ROOT, "bin", "helm"))
        payload = json.dumps({"tool_name": "Bash", "session_id": "env-arm",
                              "tool_input": {"command": command}})
        p = subprocess.run([_sys.executable, "-c", probe], input=payload,
                           capture_output=True, text=True,
                           env=dict(_os.environ, HELM_NO_TREE_WARNING="1"))
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stderr

    def test_a_refused_dump_imports_nothing_a_pass_does_not(self):
        rc, refused, err = self.run_hook(E + " | sort")
        self.assertEqual(rc, 2, err[-600:])
        self.assertIn("the whole environment", err)
        rc, passed, err = self.run_hook("ls -la")
        self.assertEqual(rc, 0, err[-600:])
        self.assertIn("helm.chat", passed)
        self.assertEqual(sorted(refused - passed), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
