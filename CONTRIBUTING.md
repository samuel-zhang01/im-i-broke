# Contributing

Start with the [README](README.md) for installation and product scope. Keep user
instructions there or in its linked guides; avoid duplicate setup walkthroughs.

## Local development

Use Python 3.12 and Node 22.12+ for the supported CI development setup. Check
`node --version` first: Node 18 cannot run the current Vite build. Work with
a disposable installation and synthetic data. Do not point test or development
commands at an existing household's databases, configuration or provider accounts.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.txt
npm --prefix frontend ci
```

Run an isolated app using the README setup command with a new directory. For
frontend hot reload, use its printed localhost address as `VITE_API_TARGET`:

```sh
cd frontend
VITE_API_TARGET=http://127.0.0.1:8080 npm run dev
```

Replace port 8080 with the actual setup port. Vite proxies API requests to the
app; keep the same-origin protections enabled.

## Checks

From the repository root, with no provider credentials in the shell:

```sh
python3 scripts/check_repository.py
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests
npm --prefix frontend test
npm --prefix frontend run build
```

Install `age` and `age-keygen` for encrypted-backup integration tests; those tests
report skips if the binaries are missing. Financial and
session changes require the complete regression suite.

Set `GITLEAKS_BINARY` to the checksum-pinned scanner installed by the
[validation workflow](.github/workflows/validate.yml) to execute its optional
integration case. A run with optional-tool skips is not the complete acceptance
result recorded in the audit.

SQLite's connection context manager commits or rolls back a transaction; it
does not close the connection. Fixtures that own a temporary handle should use
`contextlib.closing` around the connection, with a separate transaction context
when needed, or register an explicit cleanup. Keep Python 3.13 ResourceWarnings
visible while reviewing ownership; do not silence them to obtain a clean log.

Browser tests use disposable synthetic records and fresh browser profiles. They
do not need bank keys. To install the browsers and run the fresh-owner walkthrough:

```sh
cd frontend
npx playwright install --with-deps chromium webkit firefox
export PLAYWRIGHT_CHROMIUM_EXECUTABLE="$(node --input-type=module -e "import { chromium } from '@playwright/test'; process.stdout.write(chromium.executablePath())")"
npm run test:setup
```

`npm run test:e2e` covers application workflows; `npm run test:auth` covers sign-in
and recovery. `npm run test:webkit` covers desktop and iPhone-sized WebKit;
`npm run test:firefox` covers desktop and narrow Firefox layouts. The explicit
validation workflow runs both engine suites. Linux engine tests do not establish
physical iOS/Android or macOS/Windows support; check those devices before claiming
them in a release. Firefox's narrow project tests reflow without mobile emulation.
`FINANCE_TEST_PYTHON` can override the fixture's Python interpreter.
Generated screenshots and traces belong in ignored `output/`; inspect them before
sharing. Use mocked provider responses or a separate provider sandbox for tests.

Docker contexts accept only production inputs. The exclusion gate builds two
randomly named scratch images from synthetic fixtures and never starts a
container or reads household storage. On a local Linux Docker host run:

```sh
python3 scripts/check_docker_contexts.py
```

The distribution smoke test requires an explicit backend/frontend image pair
and creates its own Docker project and volumes. See
[`scripts/smoke_distribution.py`](scripts/smoke_distribution.py) and the optional
manual validation workflow for the build and test sequence. The
[synthetic CI workflow](docs/CONTINUOUS_INTEGRATION.md) runs backend/frontend
regressions, builds and disposable distribution checks on GitHub-hosted Linux
runners for pushes and pull requests. The broader validation workflow additionally
covers browser engines. A tag alone publishes no registry images.

## Review conventions

Use synthetic names, account identifiers and amounts in fixtures and screenshots.
Keep source evidence intact when changing financial calculations; ambiguous
imports and missing currency evidence should remain visible to users. Preserve
existing account IDs, environment connections and deployment configurations.

Useful implementation references:

- [Financial behavior](docs/reference/FINANCIAL_MODEL.md)
- [Snapshot retention](docs/reference/RETENTION.md)
- [UI typography and accessibility](docs/reference/UI.md)
- [Brand source assets](design/brand/README.md)
- [Institution logo attribution](frontend/public/logos/SOURCES.md)

## Preparing a public repository

1. Run the repository checks and secret scans from the validation workflow.
   Review screenshots, fixtures, financial exports and commit metadata manually.
2. Keep private operational notes, local release pins and audit receipts outside
   the repository. `.gitignore` cannot protect a file already tracked by Git.
3. Choose a license, enable private vulnerability reporting and review dependency
   and asset attribution before inviting public reuse.
4. If existing history contains private material, publish a reviewed **fresh
   source snapshot** into a new repository. Do not switch the old repository's
   visibility or push its branches, tags or history into the new public remote.

To export the reviewed commit without history or ignored local files, run from
the repository root (choose a new output filename outside the checkout):

```sh
git archive --format=tar --output=../ledger-public-source.tar HEAD
```

This exports committed files only. Scan the extracted tree and inspect it before
publication. The archive is not a backup of an installation; it contains no Git
history or ignored financial storage. If you opt to rewrite existing history,
coordinate separately with collaborators, remotes, forks and backups. Removing
private text from the current branch alone does not remove earlier copies.

## Contributing a change

Use a feature branch and a pull request. Follow the
[device and Git workflow](docs/DEVELOPMENT_WORKFLOW.md); never merge private
predecessor history into this repository. Keep changes focused and describe the
user-visible problem, resulting behavior and actual validation. Submit only
invented data in tests and reports. Use GitHub's private vulnerability reporting
for security issues, as described in [SECURITY.md](SECURITY.md).

New source contributions are provided under this project's
[AGPL-3.0 license](LICENSE). Retain third-party notices and update the app's
Settings source link when distributing a modified hosted version.
