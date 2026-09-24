"""What ships names no private network address and no real person's address.

Two shape arms over every file that ships: `helm/`, `bin/`, `scripts/`,
`docs/`, `agents/` and the root-level Markdown. Tests are outside the scope
on purpose: fixtures plant addresses and hosts, and a fixture is not a claim
about the world.

THE ARMS CARRY NO PRIVATE VALUE. An arm that named the host or the domain it
guards against would itself put that value into the tree it protects. So each
arm refuses a SHAPE, and anything outside the shape's documented exemptions:

  * a private IPv4 literal (RFC 1918, and the RFC 6598 shared space an overlay
    network hands out) names one operator's LAN. An endpoint on such a host is
    configured on that host, never written into the code. The documentation
    ranges (RFC 5737) and loopback are not private in this sense.
  * an e-mail address at a registrable domain can reach a person. Examples use
    the reserved names (RFC 2606, RFC 6761). The two public service identities
    a parser has to recognise are listed with their reason.

Each arm scans the tree AND one planted file in a single call, then asserts
the result is exactly the planted hit: the scanner is proven able to see the
shape in the same observation that finds the tree clean.
"""
import ipaddress
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The directories whose files ship, plus the Markdown files at the root.
SHIPPING_DIRS = ("helm/", "bin/", "scripts/", "docs/", "agents/")

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918
    "100.64.0.0/10"))                                   # RFC 6598 shared space

# The domain ends at the first label boundary that nothing continues, so a
# pool file named `codex-<address>-team.json` yields the address's own domain.
_ADDRESS = re.compile(
    r"([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+?[A-Za-z]{2,})"
    r"(?![A-Za-z0-9]|\.[A-Za-z0-9])")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_TLDS = ("example", "test", "invalid", "localhost")
#: Public service identities a parser must name to recognise them.
PUBLIC_IDENTITIES = {
    "git@github.com": "the SSH remote syntax helm/hostpath_guard.py normalises",
    "noreply@anthropic.com": "the vendor's published co-author address the "
                             "trailer rung detects",
}


def _shipping_files():
    """[(path, text)] for every tracked text file that ships."""
    out = subprocess.run(("git", "ls-files", "-z"), cwd=ROOT,
                         capture_output=True, check=True).stdout
    files = []
    for rel in out.decode("utf-8", "replace").split("\0"):
        if not (rel.startswith(SHIPPING_DIRS)
                or ("/" not in rel and rel.endswith(".md"))):
            continue
        try:
            with open(os.path.join(ROOT, rel), "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        files.append((rel, data.decode("utf-8", "replace")))
    return files


def private_hosts(files):
    """[(path, line, address)] for each private IPv4 literal."""
    hits = []
    for rel, text in files:
        for n, line in enumerate(text.splitlines(), 1):
            for m in _IPV4.finditer(line):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                if any(ip in net for net in _PRIVATE_NETS):
                    hits.append((rel, n, m.group(1)))
    return hits


def _reserved(domain):
    d = domain.lower()
    return d in _RESERVED_DOMAINS \
        or any(d.endswith("." + r) for r in _RESERVED_DOMAINS) \
        or d.rsplit(".", 1)[-1] in _RESERVED_TLDS


def registrable_addresses(files):
    """[(path, line, address)] for each address outside the reserved names
    and the public identities."""
    hits = []
    for rel, text in files:
        for n, line in enumerate(text.splitlines(), 1):
            for m in _ADDRESS.finditer(line):
                address = "%s@%s" % (m.group(1), m.group(2))
                if _reserved(m.group(2)) \
                        or address.lower() in PUBLIC_IDENTITIES:
                    continue
                hits.append((rel, n, address))
    return hits


class WhatShipsTest(unittest.TestCase):
    PLANTED = "<planted>"

    def test_no_private_network_address_ships(self):
        planted = (self.PLANTED, "base = 'http://172.20.3.4:8080/v1'\n")
        hits = private_hosts(_shipping_files() + [planted])
        self.assertEqual(hits, [(self.PLANTED, 1, "172.20.3.4")],
                         "a private network address ships; configure the "
                         "endpoint on its host instead")

    def test_every_shipped_address_is_at_a_reserved_name(self):
        planted = (self.PLANTED, "owner = 'someone@registrable-name.org'\n")
        hits = registrable_addresses(_shipping_files() + [planted])
        self.assertEqual(hits, [(self.PLANTED, 1,
                                 "someone@registrable-name.org")],
                         "an address at a registrable domain ships; use "
                         "example.com or another reserved name")

    def test_the_documentation_shapes_are_not_hits(self):
        """The must-miss half: every exemption is exercised, and one hit of
        each shape rides beside them so an empty answer cannot pass."""
        text = "\n".join((
            "http://192.0.2.10:8083/v1 and 127.0.0.1:8345 and 0.0.0.0",
            "version 10.0.0.1.2 and 1.2.3.4",
            "a@example.com, b@mail.example.org, c@host.test, d@x.invalid",
            "codex-a@example.com-team.json",
            "git@github.com:owner/repo and noreply@anthropic.com",
            "pkg@v1.4.143 and stash@{0}",
            "http://10.1.2.3/ and e@registrable-name.net-team.json",
        ))
        files = [(self.PLANTED, text)]
        self.assertEqual(private_hosts(files),
                         [(self.PLANTED, 7, "10.1.2.3")])
        self.assertEqual(registrable_addresses(files),
                         [(self.PLANTED, 7, "e@registrable-name.net")])


if __name__ == "__main__":
    unittest.main()
