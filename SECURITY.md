# Security policy

helm is a coordination and knowledge substrate for a fleet of coding agents.
Part of what it does touches credentials directly: it manages per-account
credential **home** directories, snapshots credential files, and orchestrates
the login flow so a session that hits a limit can re-authenticate without
corrupting a pinned account. This document states what that machinery does and
does not do, and how to report a vulnerability.

## What helm does with credentials

- **Identity from content, never the name.** `helm cred` reads which account a
  credential belongs to from the credential file's own contents, never from the
  directory name — so asking for a home by name can never silently hand you a
  different account.
- **Snapshots are owner-only and local.** `helm cred backup` copies the stable
  credential bytes into a snapshot root (`~/.cred-backups` by default,
  overridable via `HELM_CRED_BACKUP_ROOT`). Snapshot directories are created
  `0700` and files `0600`, owner-only from creation. Provider keys baked into a
  seat config are written `0600` and never re-read from the environment
  afterward.
- **The human runs every login — always.** helm makes `/login` *safe*: it
  snapshots the current account first, then prints the exact command to run. It
  never performs authentication on the human's behalf, never drives the OAuth
  flow itself, and no code path logs a human in. This is an invariant, not a
  default.
- **Credential values never leave the disk.** helm does not transmit, upload,
  or log credential values (tokens/keys). The optional agent-to-agent transport
  carries signed *records*, not secrets. Snapshots stay on local disk — point
  `HELM_CRED_BACKUP_ROOT` at an encrypted volume if you want them encrypted at
  rest.

## Signing and attestation tiers

Signing/attestation is **optional**. helm is fully functional with no signer
configured: when `HELM_CELL_BIN` is unset the transport degrades cleanly and
rows are marked `unsigned`. helm *orchestrates* an external signer binary; it
does not implement the cryptography itself.

When a signer is configured, its runtime posture has two tiers:

- **devnet-marshal (lower assurance).** A development/devnet posture that may
  enable unaudited primitives (e.g. an unaudited post-quantum flag). Suitable
  for local bring-up and testing, never for protecting real trust decisions.
- **audited / Lean-verified (production).** The higher-assurance posture:
  production leaves the unaudited flags absent, so the signer stays audited. The
  signer's runtime posture is configured once per deployment and read fresh on
  every signed turn.

## Non-goals (what helm is *not*)

- helm is **not a secrets manager or vault.** Snapshots are plaintext-on-disk at
  rest unless you place them on an encrypted volume.
- helm guards against **accidental cross-account drift**, not a malicious local
  peer. A process running as the same user can already read any credential it
  has filesystem access to; helm's guarantees are about not *corrupting* or
  *mis-binding* accounts, not about defeating a hostile local actor.
- The mutation endpoints of the local web surface are bearer-gated, but the web
  surface is intended for a trusted operator on a trusted host, not public
  exposure.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
suspected vulnerability. Use the repository host's private security-advisory
channel ("Report a vulnerability") or contact the maintainers privately. Include
a description, affected version/commit, and a reproduction if you have one. We
aim to acknowledge reports promptly and will coordinate a fix and disclosure
timeline with you.
