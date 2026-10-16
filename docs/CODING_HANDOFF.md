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
