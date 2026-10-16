# Portable installations and managed households

Start with the [README](../README.md#quick-start). This guide covers hosting,
management and upgrades. Existing installations keep their current environment,
Compose project, volumes and overlays; see [existing installations](#existing-installations).

## New self-hosted installation

Requirements: a running local Docker Engine with the Compose plugin, Python 3.10+
and an authenticator app. No Python packages, Node installation, bank credentials
or imported statements are required on the host.

The Docker engine must run **Linux containers**. On macOS, use a local Docker
Desktop engine. On Windows, the Unix-style commands below can run inside WSL 2
with Docker Desktop's WSL integration enabled; keep the installation directory in
that environment. The helper also accepts local Windows named-pipe Docker contexts.
It checks the engine before creating or starting containers. Remote SSH/TCP Docker
contexts are refused because its port checks and printed localhost address refer
to the computer running the helper. For a remote server, run setup on that server
and follow the HTTPS instructions below. Docker's
[context selection rules](https://docs.docker.com/reference/cli/docker/#environment-variables)
also apply: `DOCKER_CONTEXT` overrides `DOCKER_HOST` and the saved context.

From a reviewed source checkout, run one command:

```sh
python3 scripts/setup.py ~/ledger-home --build
```

Choose a directory that does **not** already exist. The helper builds both images
from this checkout, creates a private installation with its own storage and image
names, chooses a free localhost port, initializes the household and starts the
app. It prints the browser address and one-time setup token. The first build needs
internet access to download dependencies and may take several minutes.

There are no `.env` fields to fill in. The helper generates the six settings it
needs; inherited shell credentials and unrelated Compose settings are excluded.
Your installation directory contains its own `manage.py`, so routine management
does not depend on keeping the source checkout. It is private to this computer;
use the HTTPS section below when you are ready to host it publicly.

Open the printed address, paste the token, choose a password of at least 15
characters, and scan the QR code with your authenticator (or copy its setup key).
Save the recovery codes before continuing. A short welcome guide opens with your
reporting currency and choices for connecting or adding an account. Existing
households keep their usual landing page; this guide can be reopened in Settings.
Banks are optional: you can start with a manual account or CSV import,
then add Lunch Flow or SnapTrade in Settings. See the
[provider setup guide](PROVIDERS.md).

To use an existing matched image pair instead of building from source:

```sh
python3 scripts/setup.py ~/ledger-home \
  --backend-image REGISTRY/PROJECT-backend:VERSION \
  --frontend-image REGISTRY/PROJECT-frontend:VERSION
```

Replace these placeholders with reviewed lowercase version tags or immutable
digests. Local image tags also work; missing images are pulled from their registry.
Build from source with `--build` for this initial public release. Do not assume
a registry image exists: a release tag alone does not publish images. Image
publication is a separate maintainer workflow, and matching pins must be
listed in the relevant release before use. Use `--port 8088` to choose a specific free port, or add
`--prepare-only` with an explicit image pair to generate files without Docker
access or launching services.
The prepared installation prints its later launch command.

Manage the installation from any directory:

```sh
python3 ~/ledger-home/manage.py status
python3 ~/ledger-home/manage.py stop
python3 ~/ledger-home/manage.py start
python3 ~/ledger-home/manage.py token
python3 ~/ledger-home/manage.py check
```

Stopping the app retains all data. `token` displays the unused setup token again;
it does not reset an enrolled owner. `check` reports readiness without financial
data or secrets. The helper refuses existing directories, occupied/reserved ports,
and existing Docker resources under its generated project name. It never deletes
storage after a failed launch. If startup fails after initialization, inspect
`manage.py status` and `manage.py logs`, then retry `manage.py start`. An interrupted
initializer requires inspection before recovery; rerunning setup cannot overwrite
it. Keep the private installation directory alongside your backup records.

If the source image build fails before initialization, fix the reported build
problem and run `python3 ~/ledger-home/manage.py build`. This retries both builds
from the original checkout and finishes launching the same new installation.
It preserves the generated image names and configuration. Keep that checkout
until the first successful build; routine starts need only the installation folder.

For operators who need manual Compose control, the equivalent files remain
`deploy/compose.product.yml` and the minimal `deploy/product.env.example`. The
generated installation uses the same Compose format, so all commands below work
from its directory without a second configuration system.

The initializer accepts **empty data, uploads and key directories only**. It
creates the database, a separate encryption key, and a random token valid for 24
hours. The backend has a read-only key mount and cannot recreate these files.
The token is submitted in a request body, never placed in a URL. Enrollment is
atomic and permanently disables browser setup, including across restarts.
The unused token file becomes inert after enrollment; no live credential depends
on keeping it. Offline owner recovery continues to use `scripts.auth reset`.

An expired token for an initialized, unenrolled installation can be renewed:

```sh
python3 ~/ledger-home/manage.py renew-token
```

The helper briefly stops a running backend, renews the token offline and restores
its prior running state, including when renewal is refused. For a manually
provisioned installation, use the equivalent Compose commands: stop the backend,
run `docker compose run --rm admin python -m scripts.installation renew-token`, then
restart the backend only if it was running beforehand.

Renewal cannot enable browser setup for a pre-existing household. If initialization
was interrupted, inspect the new storage before changing anything; retries refuse
partially populated volumes. Missing databases on an existing household remain a
recovery error and are never silently replaced by an empty ledger.

Check installation readiness without printing financial data or secrets:

```sh
docker compose exec -T backend python -m scripts.doctor
```

An owner can remain signed in on multiple devices. Normal login retains the other
sessions; logout ends the current session. Revoking other sessions is available
in account settings. Owner reset and recovery-code replacement still revoke all
sessions. Up to 20 sessions are retained, with the existing idle and absolute
expiry limits.

## HTTPS and a managed service

For each public household, set:

```dotenv
FINANCE_DEPLOYMENT=public
FINANCE_COOKIE_SECURE=true
FINANCE_PUBLIC_ORIGIN=https://household.example.com
FINANCE_ALLOWED_HOSTS=household.example.com,localhost,127.0.0.1,backend
FINANCE_BIND_ADDRESS=127.0.0.1
```

Terminate TLS at your reverse proxy and preserve the original `Host` header.
Proxy only to that household's loopback port. Public mode refuses disabled
authentication, insecure cookies, missing/mismatched HTTPS origins and encryption
keys stored alongside the auth database. Each household keeps its own process,
network, financial/auth databases, credentials, uploads, key and backup volumes.
Do not mount any other household's volumes into it.

Prepare deployment files without launching services:

```sh
python3 backend/scripts/provision_instance.py /private/households/alex \
  --household alex --hostname alex.finance.example.com --port 21001 \
  --backend-image ghcr.io/your-org/your-repo-backend:v1.0.0 \
  --frontend-image ghcr.io/your-org/your-repo-frontend:v1.0.0
```

This creates an exclusive private directory, random unique Compose project name,
versioned image pins, HTTPS settings and an operator checklist. It rejects
overwrites and duplicate generated hostnames/ports among sibling deployments.
Your infrastructure must still reserve ports across other applications and
configure DNS/TLS. Give only the intended customer the one-time token through a
private channel; the command does not send messages or contact a bank.

This implements managed **isolated instances**, with one owner per household and
multiple devices. It does not implement shared-database multitenancy, public
registration, billing, invitation roles, or a support/control-plane service.
Never route different customers to one instance. Keep a single backend process
per instance: existing refresh coordination and SQLite storage are designed for
that deployment.

## Provider consent for customers

Configure your approved provider application using the [provider guide](PROVIDERS.md).
Personal credentials and operator credentials are separate. Provider approval,
subscriptions and consent are external setup steps.

## Release and upgrade

For an existing root-Compose app running from a Git checkout, root
[`deploy.sh`](../deploy.sh) fetches and fast-forwards `origin/main`, builds a
matched image pair, creates an encrypted recovery set, rehearses migration and
verifies activation. Use it on the deployment host after pushing from the coding
device. See [Automatic deployment](AUTOMATIC_DEPLOYMENT.md) for commands,
preserved private overlays, failure recovery and verification limits. It does not
update standalone installations generated by `scripts/setup.py`.

Pushes to `main` and pull requests run [synthetic CI](CONTINUOUS_INTEGRATION.md)
on GitHub-hosted runners with disposable data. A push does not deploy a household
or publish registry images. Maintainers can run **Validate application and
distribution** manually for the full browser and distribution gates. To publish
GHCR images, explicitly run **Publish versioned product images (manual)** on
`main` with an existing reviewed `vMAJOR.MINOR.PATCH` tag.
That publishing workflow validates the tagged revision before producing amd64 and
arm64 images with provenance/SBOM metadata and immutable digests in its summary.
Configure package visibility/access before distributing image references. It uses
the documented [Docker build action](https://github.com/docker/build-push-action)
and [GitHub package publishing permissions](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

Before an upgrade, record both old image digests and create a verified recovery
set. Rehearse the new code against a private snapshot; this command only migrates
a temporary copy and checks protected source records, saved decisions, repeat
startup and backup/restore:

```sh
docker compose run --rm admin python -m scripts.verify_upgrade /app/data/finance.db
```

For a candidate image, change only that one command's image using a separate
reviewed Compose environment, leaving running services on their current image.
The temporary copy uses `/tmp` (a bounded tmpfs in the product container); use a
private encrypted work directory with `--work-dir` for larger datasets. The check
does not replace a complete backup or visual comparison of a recovered copy.

After verification, update both image pins together. Pull only when the new pins
refer to registry images; locally built images already exist on the host. Then
apply and check readiness:

```sh
# Registry images only: docker compose pull backend frontend
docker compose up -d --no-build --wait backend frontend
docker compose exec -T backend python -m scripts.doctor
```

Keep the prior images and pre-upgrade backup. If a migration is incompatible with
the old code, rollback means restore to **new** private storage with the matching
older image; never assume switching images reverses database migrations.

## Backup and recovery

Follow the [backup guide](BACKUP.md) to create an encrypted recovery set and
rehearse restoration to new storage. Keep the matching installation key; a new
key cannot decrypt saved connections.

## Existing installations

Keep your existing private `.env`, Compose project name, volumes, image pins and
network overlays. Do not run the new-installation initializer over saved data.
The root Compose file and its public/proxy/LAN overlays remain supported. Local
release pins belong in your ignored `deploy/compose.release.yml`; retain that file
and your private `deploy/lan-access.conf` when updating a deployed checkout. Use the same `-f` files for every operation.

For older root-running installations, take a verified backup, stop the backend
and frontend, then run the permission/key migration with your existing overlays:

```sh
docker compose --profile maintenance run --rm storage-init
```

It changes storage ownership to UID/GID 10001 and moves a legacy authentication
key to the separate key volume; conflicting keys stop migration. If no owner is
enrolled, use `docker compose --profile maintenance run --rm auth-admin`. The
standalone distribution instead uses the `admin` service described above.
Configure HTTPS and verify health before restoring public access. Normal updates
do not require storage migration or owner enrollment. Never use `down -v` for an
update; it deletes saved volumes.

<details>
<summary><strong>Optional trusted-LAN development profile</strong></summary>

For a separate private development instance, the root Compose file can use
`deploy/compose.lan.yml`. This disables login. Before starting it, copy
[`lan-access.example.conf`](../deploy/lan-access.example.conf) to the ignored
`deploy/lan-access.conf` and add only your trusted network ranges. The example
allows loopback only. Configure the private bind address and allowed hosts in
that development instance's own environment. Existing private allowlists are
retained; do not replace them with the example during an upgrade.

Never combine the trusted-LAN overlay with a public overlay or route public
traffic to it. Public hosting uses the authenticated HTTPS configuration above.

</details>
