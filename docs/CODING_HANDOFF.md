# Maintainer handoff

This public repository starts with a reviewed source snapshot and fresh Git
history. Private predecessor commits, household records, deployment receipts and
operational review narratives are not part of the public project.

The supported release model is an isolated self-hosted installation with one
owner and multiple signed-in devices. Begin with the [README](../README.md).
Use [Contributing](../CONTRIBUTING.md) for development and checks,
[the device workflow](DEVELOPMENT_WORKFLOW.md) for Git and deployment, and
[known limitations](KNOWN_LIMITATIONS.md) before making support claims.

## Public release preparation — 16 October 2026

- Export committed source into a separate directory without any old Git objects,
  ignored storage, credentials, uploads, logs, screenshots or backups.
- Replace private operational documentation with public setup and maintainer guides.
- Replace household-derived merchant starter rules and local-business fixtures
  with general defaults and invented spelling examples.
- License project code under AGPL-3.0-only; retain third-party attribution.
- Run synthetic CI on GitHub-hosted runners. Pull requests receive no household
  configuration, provider credentials or deployment authority.
- Keep user installations outside the source checkout. Updates preserve their
  storage and matching encryption key.
- Record verification of this public snapshot in [release validation](RELEASE_VALIDATION.md).

No household deployment is part of preparing this repository. Remaining product
work is tracked in the [improvement plan](APP_IMPROVEMENT_PLAN.md).

## Verified public baseline

The repository is public at
[samuel-zhang01/im-i-broke](https://github.com/samuel-zhang01/im-i-broke).
No predecessor commit parents, branches or tags were imported. Both source and
history passed checksum-pinned Gitleaks, and private reporting/push protection
are enabled. Public CI uses GitHub-hosted runners; there are no repository
secrets or private runners.

Local checks passed 1,238 backend tests, 156 frontend units/build, eight setup
browser scenarios, one authentication/recovery scenario and 40 focused UI checks.
The [initial public CI](https://github.com/samuel-zhang01/im-i-broke/actions/runs/38930948149)
also passed production-image tests and disposable installation, enrollment,
upgrade rehearsal and complete encrypted recovery. The local Docker build
attempt remained blocked by its AppArmor host; the hosted gate is separate
successful evidence. Exact counts and limitations are in
[release validation](RELEASE_VALIDATION.md).

Start future work from this repository's `main` and a feature branch. Do not
merge private predecessor history here or repoint an existing private
installation's remote to work around unrelated histories. Code publication and
household deployment remain separate actions.

A compatible source-map-js development-tool patch (1.2.1 → 1.2.2) also passed
156 frontend units/build with all 104 generated frontend files identical to the
browser-tested build. Remaining npm advisories are five high/two moderate in
the development graph; production-only npm audit remains zero. A Tailwind major
migration is tracked separately.
