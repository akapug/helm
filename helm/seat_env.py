"""Child-session and proxy-environment guards for :mod:`helm.seat`.

This implementation module is loaded through ``helm.seat``. The facade seeds
its compatibility namespace after import so these moved functions retain the
monolith's module-global lookup and monkeypatch semantics.
"""


def scrub_env(env):
    """A copy of `env` with the proxy triple removed. Compose this into every
    subprocess env that launches claude for a CLAUDE-model seat."""
    return {k: v for k, v in dict(env).items() if k not in SCRUB_VARS}


def scrub_prefix():
    """The printed-command form of the guard: an `env -u ...` prefix for
    pasteable claude commands minted for Claude seats."""
    return "env " + " ".join("-u " + v for v in SCRUB_VARS) + " "


def child_stamp_unsets():
    """The `-u VAR ...` run that strips the child-session stamp — composed
    into every minted launch line (and, via launch_line, every launch.sh) so
    a launched seat starts as a top-level session with real persistence."""
    return " ".join("-u " + v for v in CHILD_STAMP_VARS)


def paste_unset_prefix():
    """The single `env -u ...` prefix every PASTEABLE claude/codex command
    carries: the child-session stamp AND the proxy triple.

    WHY ONE FUNCTION AND NOT TWO CALLS AT EACH SITE. `sessions.resume_exec`
    and `transcripts._native_cmd` each built this prefix from an identical
    inline literal — `"env -u " + " -u ".join(seat.CHILD_STAMP_VARS) + " "` —
    and THAT DUPLICATION IS WHY THE PROXY TRIPLE WAS MISSING FROM BOTH. With
    no shared seam the scrub had to be remembered twice, so it was remembered
    zero times: `scrub_prefix()` was written for exactly this job and had no
    production caller at all. A guard that must be re-added per site is a
    guard that will be absent from the next site.

    THE TWO REGISTERS ARE DIFFERENT HAZARDS AND BOTH BELONG HERE. The stamp
    makes a resumed session a subprocess child with transcript persistence
    silently off. The proxy triple is worse in kind: a command pasted into a
    PROXIED shell resumes a Claude session against ANTHROPIC_BASE_URL pointing
    at a local proxy fronting another vendor — it looks native, it is billed
    native, and it routes elsewhere. A human pasting a line helm printed has
    no way to see either.
    """
    return "env " + " ".join("-u " + v for v in PASTE_UNSET_VARS) + " "
