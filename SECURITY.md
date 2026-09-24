# Security Policy

## Reporting a vulnerability

Open a [private security advisory](https://github.com/akapug/helm/security/advisories/new).
Please do not open a public issue for a vulnerability.

Include what you did, what happened, and what you expected. A reproduction —
even a rough one — is worth more than a careful description.

## Scope, and what helm actually holds

helm coordinates AI coding-agent fleets, so the interesting surface is not a
network listener; it is **credentials and other agents' state on one machine**.

In scope:

- **Credential handling.** Seat homes under `~/.helm/_global/seats/<family>/`
  hold OAuth files and API keys. Everything token-bearing is written `0600`
  from creation. A path that leaks a credential into a log, an error message,
  a config with looser permissions, or a second on-disk copy is a real finding.
- **Cross-seat isolation.** Seats share one machine and one chat substrate. A
  way for one seat to read, forge, or destroy another's state is in scope.
- **The local chat/coordination substrate** (`/dev/shm/helm-chat`), including
  message forgery and cursor manipulation.
- **Anything that makes helm report success it did not achieve.** A verifier
  that passes on absent input, a check that swallows its own failure, or a
  green line over a capability that is not there — these are treated as real
  defects here, not cosmetics, because the operator cannot independently
  re-derive what the tool tells them.

Out of scope:

- helm runs as you, on your machine, with your files. A "vulnerability" that
  requires already having your shell is not one.
- Model outputs. A model saying something wrong is a model problem; helm
  *believing* it without evidence is a helm problem, and that is in scope.

## What we will not do

We will not ask you to prove a report by attacking a third party's account, and
we will not treat a good-faith report as a hostile act.
