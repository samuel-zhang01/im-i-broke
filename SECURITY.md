# Security and privacy

Am I Broke? stores financial records on the host you choose. Connected providers
and market-data services receive requests for their enabled features. In-browser
performance diagnostics remain in memory and have no telemetry endpoint.

## Report a vulnerability

Do not include credentials, financial records or exploitable deployment details
in a public issue. Use [GitHub private vulnerability reporting](https://github.com/samuel-zhang01/im-i-broke/security/advisories/new). If it is unavailable, ask the maintainer to open a private
reporting channel without posting the sensitive details. Never post sensitive details in a public issue.

## Access protection

Owner enrollment requires a one-use, expiring setup token, a password and an
authenticator challenge. Existing installations never reopen browser enrollment
automatically. Recovery codes provide a second-factor fallback; keep them private.

Sessions use hashed random tokens and HttpOnly cookies. Public mode requires
HTTPS, Secure cookies, authentication and an explicit origin/host allowlist.
Normal sessions expire after 30 idle minutes or eight hours total; optional
remembered sessions expire after seven idle days or 30 days total. Settings can
revoke other devices. Background polling does not extend sessions.

Password hashes use scrypt. Authenticator seeds and saved provider credentials
are encrypted using the installation key stored separately from the database.
The web backend mounts that key read-only. Local disconnect removes saved
credentials; manage provider-side consent in the provider's own controls too.

## Hosting and storage

Use the [deployment guide](docs/DEPLOYMENT.md) for HTTPS and isolated households.
Trusted-LAN mode disables login and is only for a private, trusted network;
never expose it through a public proxy. Keep one backend process per household.

**Live databases, uploads and source documents are not transparently encrypted.**
Use encrypted host storage and restrict access to the installation directory.
An unlocked server with access to the data and key can read financial records.
Container hardening does not replace host security or a supported operating system.

A complete [recovery set](docs/BACKUP.md) includes encrypted data, the matching
separately encrypted key and private configuration. Keep an off-host copy and
rehearse restoration to fresh storage. Restoring a different key makes saved
provider credentials unreadable. Never commit configuration, keys, statements,
backups, real-account screenshots or diagnostic exports.

## Existing owner enrollment

For an older installation without an enrolled owner, first follow the storage
migration and offline enrollment steps in
[existing installations](docs/DEPLOYMENT.md#existing-installations). Preserve the
existing volumes and key; a missing database or owner is not a reason to run the
new-installation helper against saved data.

## Owner recovery

For a standalone installation, run from its private directory:

```sh
docker compose stop backend
docker compose run --rm admin python -m scripts.auth reset
docker compose up -d --wait backend frontend
```

This deliberately replaces the owner's credentials and revokes sessions. Preserve
the original key. Existing root-Compose deployments use their existing overlays
and `docker compose --profile maintenance run --rm auth-admin python -m scripts.auth reset`
instead of the standalone `admin` command.

## Repository checks

Before a release, run the repository checks and a secret scan of the source
tree and Git history. [Synthetic CI](docs/CONTINUOUS_INTEGRATION.md) runs these
checks on GitHub-hosted runners for pushes and pull requests. These checks cannot establish that
financial examples are anonymous or that a repository has never exposed
credentials. Review examples and history
before publication; deleting a file does not remove its earlier revisions.
If a credential was exposed, revoke or rotate it before removing the copies.
See [public-release preparation](CONTRIBUTING.md#preparing-a-public-repository).
