# Working instructions

This is the public, self-hosted Am I Broke? application. Start with
[README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md), and
[docs/CODING_HANDOFF.md](docs/CODING_HANDOFF.md).

- Use disposable synthetic databases, provider fixtures and browser profiles.
  Never read an existing household's storage or use real provider credentials in tests.
- One installation has one owner. Keep separate households isolated; do not add
  shared signup without designing tenant isolation across all storage and jobs.
- Preserve original financial evidence and account identities. Do not silently
  reinterpret currency, balance signs, missing prices or ambiguous imports.
- Follow [docs/DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md) when moving
  between devices. Never merge or push private predecessor history into this repo.
- CI uses GitHub-hosted runners with synthetic data. Do not introduce private
  runner labels, deployment credentials or automatic household deployment.
- Keep [the plan](docs/APP_IMPROVEMENT_PLAN.md) and
  [known limitations](docs/KNOWN_LIMITATIONS.md) current. Record actual check
  results; a source build does not prove installation, recovery or live-bank support.
- Respect AGPL-3.0 and third-party asset notices. Hosted modified versions need
  a corresponding source offer; update the Settings source link for such builds.

User instructions take precedence.
