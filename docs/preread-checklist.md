# The pre-read checklist

`helm preread` sends this file to every reader with every file of the diff. It
is DATA, not code: widen it from the store's review classes and the next
pre-read is wider, with no edit to `helm/preread.py` and no release. The config
names the path (`checklist` in `_global/preread.json`); a pre-read whose
checklist will not read REFUSES rather than running an unscaffolded council,
because the checklist is the scaffolding — a weak reader with no defect classes
in front of it recovers nothing and loops.

Everything below the line is sent verbatim.

---

Before you answer, check the diff against this list of defect classes this repository has been bitten by; report a finding ONLY when you can point at the exact added or changed line that has it:
1. A promise in a docstring, a comment or a module header that the code beside it does not keep (a "fail-closed" rule, "every rendering masks", "never dropped", "only under the lock").
2. A secret or an identity rendered where the module says it is masked: an email, a token, an account name, an exception message printed to a page or a log.
3. A subprocess spawned directly (subprocess.run/Popen with "git" or any binary) in a module whose repository routes spawns through one seam module (helm/vcs.py); a new direct spawn is a defect.
4. A read outside a lock followed by a write inside it (time-of-check to time-of-use), or a check repeated per item that should be one transaction.
5. A rule stated in the same diff that is implemented in one place and not the sibling: a web handler that refuses a field while the library save() accepts it, a CLI that masks while the page does not.
6. A denominator, a unit or a percentage computed with the wrong operand; an off-by-one against a stated cap; bytes versus characters.
7. An exception handler that swallows the reason (except Exception: pass) or that leaks the raw exception text to a user surface.
8. A regular expression or a matcher that misses the case its own comment names, or that matches too much (a rule of "=" that matches a comment line).
9. A test that cannot fail: asserts only on absence, or seeds the state it then asserts is absent, or checks a weaker property than the docstring claims.
10. A new state file, verb or module without the registry row, the docs line or the wiring the repository requires (docs/MODULE_REGISTRIES.md, docs/VERBS.md).

TWO MORE RULES. (a) Before you report a line, read the comment or docstring beside it: if it states why the code does what it does (a fail-open rule, a diagnostic that is meant to print the reason, a mask that keeps a prefix by design), that is NOT a defect; report only a line that CONTRADICTS the nearest comment or docstring, or breaks a value. (b) Report at most FIVE findings, the ones you are surest of, and never repeat a finding.

For each finding: file, the exact line text you are pointing at (copy it inside backticks), the class number, what is wrong, why. Then say which classes you checked and found clean. If nothing concrete, answer NONE FOUND.
