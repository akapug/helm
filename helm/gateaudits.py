"""THE TREE-WIDE AUDITS, PRINTED AS A COMMAND INSTEAD OF TYPED FROM MEMORY.

A lane's focused run is chosen by what the change IMPORTS, by a planner or by
hand. The audits below import nothing they judge: each enumerates the package
or the tree and asserts a property of every module it finds, so no consumer
sweep over a change's symbols ever selects one, and a lane that adds a module,
a verb or a docstring reference learns about them at the whole-suite gate, the
slowest place in the tree to learn anything.

The cure for that is not a cleverer selector. It is to run the whole list
before every gate, and the list was being typed from memory, which is how an
audit gets left off it. `helm gate audits` prints the list as one command a
seat can paste, with the lane's own modules appended.

THE LIST IS THE ONE docs/MODULE_REGISTRIES.md ENUMERATES, plus the two arms
that exercise the non-test rungs, and a test holds the two in step so neither
can grow without the other.
"""
import os
import sys

#: The test modules that enumerate the package (docs/MODULE_REGISTRIES.md,
#: "The twenty-two"), in that page's order.
ENUMERATORS = (
    "test_dispatch_honest", "test_display_launder_tripwire",
    "test_docstring_refs", "test_escape_hygiene", "test_foldcompose",
    "test_freetext", "test_handoff", "test_identity_layer",
    "test_instructions_are_runnable", "test_lease_recovery", "test_notify",
    "test_openflags", "test_orcaadopt", "test_readme_claims", "test_rearm",
    "test_reference_parity_pins", "test_roster_write_guard",
    "test_seat_facade_injection", "test_seats_split_contract",
    "test_session_start_closure", "test_surface_wiring", "test_vcs",
)
#: The arms on rungs whose enumeration lives outside tests/, the doc and
#: budget parities a new verb or a new hook line owes, and the scanner that
#: reads the PRODUCT tree for material that must never enter history: a lane
#: adding one fixture with an address-shaped string reddens it, and nothing
#: the lane imports would ever select it.
#: `test_env_hygiene` and `test_scratch` joined for task/3039: outside the
#: import closure and this list, they were the failing module in 5 and 4 of
#: the week's reds, and a focused plan for a change with no import graph (a
#: doc, a script) runs this whole list, so it must carry them.
RUNG_ARMS = ("test_wiring", "test_registry", "test_verb_sweep",
             "test_verbs_doc_parity", "test_hook_budgets",
             "test_seat_split_contract", "test_never_track",
             "test_env_hygiene", "test_scratch")
AUDITS = ENUMERATORS + RUNG_ARMS

USAGE = ("usage: helm gate audits [--repo PATH] [--json] [-- <test module> ...]\n"
         "  Prints ONE pasteable `fab test` command running every tree-wide "
         "audit, with any test modules named after `--` appended. Run it on "
         "the COMPOSED tree before a whole-suite gate. It runs nothing itself.")


def modules(extra=()):
    """The dotted module list: every audit, then `extra` in the caller's
    order, each named once. `extra` accepts `tests.test_x`, `test_x` or a
    path to the file."""
    out = ["tests." + name for name in AUDITS]
    for raw in extra:
        name = os.path.basename(str(raw))
        name = name[:-3] if name.endswith(".py") else name
        name = name.split(".")[-1] if name.startswith("tests.") else name
        dotted = "tests." + name
        if dotted not in out:
            out.append(dotted)
    return out


def missing(repo):
    """The listed audits with no file under `repo`/tests. A name this list
    carries that the tree does not is a list that has gone stale, and a
    command naming it would fail on import instead of auditing anything."""
    return [name for name in AUDITS
            if not os.path.isfile(os.path.join(repo, "tests", name + ".py"))]


def command(repo, extra=()):
    return "fab test --repo %s -- python3 -m unittest %s" % (
        repo, " ".join(modules(extra)))


def cmd(rest):
    from .cli import guard_tail
    rest = list(rest or ())
    extra = []
    if "--" in rest:
        cut = rest.index("--")
        rest, extra = rest[:cut], rest[cut + 1:]
    rc = guard_tail("helm gate audits", rest, flags=("--json",),
                    valued=("--repo",), usage=USAGE)
    if rc is not None:
        return rc
    repo = os.path.abspath(rest[rest.index("--repo") + 1]
                           if "--repo" in rest else os.getcwd())
    gone = missing(repo)
    if gone:
        print("helm gate audits: %s carries no tests/%s.py — this list names "
              "an audit the tree does not have; nothing printed"
              % (repo, ".py, tests/".join(gone)), file=sys.stderr)
        return 1
    if "--json" in rest:
        import json
        print(json.dumps({"repo": repo, "modules": modules(extra),
                          "command": command(repo, extra)}, indent=2))
        return 0
    print(command(repo, extra))
    return 0
