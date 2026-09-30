"""RULE 2 of the console walk as one check the page arms share: the console
never tells the owner to run a helm verb, because he does not use a terminal.

`owner_verbs(markup)` lists every `helm <verb>` a rendered fragment shows him,
attribute text included (a hover is text he reads). An HTML comment is not
shown, so it is not read. A verb is a key of the CLI's own table, so helm the
noun ("helm built it from today's seats") is not one, and a verb added to the
CLI later is caught without editing this file. `owner_commands(markup)` lists
the terminal steps that carry no helm verb: a login line, a shell link, a
"run it in a terminal"."""
import html
import re

from helm import cli

_HELM_WORD = re.compile(r"\bhelm\s+([a-z][a-z-]*)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def owner_verbs(markup):
    """[verb, ...] in the order the fragment shows them; [] when it is clean."""
    shown = html.unescape(_COMMENT.sub("", markup))
    return [word for word in _HELM_WORD.findall(shown) if word in cli.VERBS]


# A TERMINAL STEP WITH NO HELM VERB IN IT is the same instruction (task/3735,
# the Credit page): a vendor login line, the env prefix that picks its home,
# a shell link, or a sentence sending him to a terminal or a clipboard.
_COMMAND = re.compile(r"claude /login|codex login|CLAUDE_CONFIG_DIR=|CODEX_HOME="
                      r"|\bln -s|\bterminal\b|click to copy", re.I)


def owner_commands(markup):
    """[command text, ...] a rendered fragment shows him that no helm verb
    names, attribute text included; [] when it is clean."""
    shown = html.unescape(_COMMENT.sub("", markup))
    return [m.group(0) for m in _COMMAND.finditer(shown)]


_VIEW_END = re.compile(r'<div class="view[ "]|<script\b')


def view_markup(page, view):
    """The markup of one view (`id="view-<view>"`) of the assembled page, up
    to the next view or the first script: the static half of that page."""
    head = page.index('id="view-%s"' % view)
    tail = _VIEW_END.search(page, head)
    return page[head:tail.start() if tail else len(page)]


def shell_markup(page):
    """The page's markup outside every view, style and script: the nav, the
    banners and the footer every page shares."""
    body = page[page.index("</style>"):]
    return body[:_VIEW_END.search(body).start()]
