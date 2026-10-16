# Deploy updates from another coding device

[Synthetic CI](CONTINUOUS_INTEGRATION.md) checks disposable data and images on
GitHub-hosted runners. Running this helper is a separate operator action for an
existing root-Compose deployment. Installations created by `scripts/setup.py`
use the [standalone upgrade guide](DEPLOYMENT.md#release-and-upgrade) instead.

Use the root [`deploy.sh`](../deploy.sh) **on the machine that already runs this
checkout's app**. Code and test on the other device, commit there, and push to
`origin/main`. Then run this on the deployment host:

```sh
cd /path/to/im-i-broke
./deploy.sh
```

The default includes the Git pull steps: `git fetch origin main`, validation that
the update is a fast-forward, and `git merge --ff-only origin/main`. It then runs
the fetched deployment implementation while retaining its deployment lock.
Local changes, untracked files, local commits ahead of the remote and divergent
history are refused; the script does not discard coding work. Commit and push
the handoff on the current machine before relying on remote synchronization.

To invoke it from the coding device, use your existing SSH access:

```sh
ssh DEPLOYMENT_USER@DEPLOYMENT_HOST 'cd /path/to/im-i-broke && ./deploy.sh'
```

Replace the user, host and checkout path with this deployment machine's values.
Git authentication must already work on the deployment host. If a host does not
have this script yet,
pull this commit once with `git pull --ff-only`; subsequent updates need only
`./deploy.sh`. Nothing installs a timer or deploys automatically on a Git push.

## Useful commands

```sh
./deploy.sh --help
./deploy.sh --check
./deploy.sh --local
```

`--check` inspects Docker metadata and the effective Compose configuration; it
does not fetch, build, stop, migrate or deploy. It also works before local edits
are committed. `--local` deploys the current **clean, committed** revision without
fetching, including a reviewed branch other than `main`. The default requires
`main`. `--project PROJECT` resolves an ambiguous existing deployment;
`--timeout 300` changes the candidate health wait from its default 180 seconds.

Requirements: Bash, Python 3.10+, Git, and a running local Docker Engine with the
Compose plugin and Linux containers. The helper uses POSIX file locking; on
Windows, run it inside WSL with local Docker integration. Remote Docker contexts
are refused. Python uses only the standard library; host Node/npm and host `age`
are unnecessary because dependencies and backup tools run inside the images.
Both Docker builds need access to their pinned dependencies. This helper updates
an **existing root-Compose installation**, not installations generated elsewhere
by `scripts/setup.py`. For a fresh household, follow [Deployment](DEPLOYMENT.md).

## What one deployment does

1. Lock this checkout, inspect its existing backend/frontend and identify the
   original Compose project and ordered overlays. Validate storage, mount
   permissions, listeners, networks and proxy aliases against the containers.
2. Fetch and fast-forward `origin/main`, then run the fetched script. Repeat
   validation with the updated source before touching running services.
3. Run repository and institution-asset checks. Build backend and frontend
   images from the same commit, with commit-specific tags and revision labels.
   The frontend Docker build installs locked packages and compiles the app.
   Verify both images as their non-root runtime users without household mounts
   or network access before stopping the existing backend.
4. Prepare a private backup recipient, stop backend writes, and create a matched
   encrypted recovery set: financial/auth databases and uploads, the matching
   encryption key separately, and private deployment configuration separately
   (including supported frontend policy bind files within this checkout).
   Use the newly built backup tools with read-only household mounts. Stage the
   stopped database and any WAL/rollback journal in private writable tmpfs before
   SQLite reads them, preserving committed journal records. Export ciphertext
   with checksums into a private host directory.
5. Rehearse the candidate migration on a temporary copy of the financial
   database. The candidate rehearsal has read-only data, no live auth key, no
   uploads mount and no network. Backup and rehearsal temporary filesystems are
   each bounded to 256 MiB, including staged source copies and snapshots; a larger
   household needs a separately reviewed backup/migration procedure.
6. Write ignored image pins, start only backend/frontend, wait for health, verify
   exact image IDs and live mounts/networks/listeners, and probe `/api/health`
   through the frontend proxy. Run the installation doctor outside trusted-LAN
   mode. Save a nonsecret deployment receipt after these checks pass.

The existing `.env`, Compose project, ordered overlays, port bindings, provider
settings and four separate household volumes are retained. External/shared
volumes and custom backend bind mounts are refused. Frontend binds must be
read-only regular files inside the checkout; directories and external bind paths
need a separate deployment procedure. The script does not run storage
initialization, owner reset, imports, provider refresh or volume removal.
It retains old images and recovery sets; there is no automatic pruning.

The new `deploy/compose.auto.yml` is private and ignored. It is appended **last**
after the original overlays, so it can update an older private release image
pin without editing that file. It pins backend, frontend and maintenance services
to the matching pair. Subsequent script runs discover that overlay from Docker.
If you use manual Compose commands afterwards, include it last with the same
project and original overlays, or those commands may select the older images:

```sh
docker compose --env-file .env -p PROJECT \
  -f docker-compose.yml -f YOUR_EXISTING_OVERLAY.yml \
  -f deploy/compose.auto.yml ps
```

Replace the project and overlay list with those printed by `--check`. Include
every original overlay in its original order.

## Keep the recovery set usable

By default the helper creates `output/deployment-backups/` with mode `0700`, a
local `recovery.identity` with mode `0600`, and timestamped `recovery-*` sets.
The identity is a private decryption key: keep a separate private offline copy.
Copy completed encrypted recovery sets off the host. These ignored files are
not part of the Git handoff; losing the only identity makes the sets unreadable.
Documents outside the app's uploads volume need a separate backup.

If you already manage an age identity, supply its **public** recipient instead:

```sh
./deploy.sh --backup-recipient age1YOUR_PUBLIC_RECIPIENT \
  --backup-dir /private/finance-recovery
```

The recipient can also be set in `FINANCE_DEPLOY_BACKUP_RECIPIENT`. The recovery
directory must belong to the invoking user, be private, and have no symlink
components. No plaintext environment archive or key is printed to the terminal.
Each complete set contains `data.age`, `key.age`, `configuration.age` and a
checksum `manifest.json` recording the previous image IDs and candidate commit.
`last-deployment.json` records the most recent verified activation.

Read [Backup and recovery](BACKUP.md) for decryption and restore into **new**
private storage. `configuration.age` contains the old environment, original
Compose overlays, effective configuration and old image IDs. Inspect it privately;
do not extract it over a live deployment. Use the recorded previous images and a
new project/private port for a recovery rehearsal.

## Failure behaviour and verification limits

Failures identify the operation and exit status without printing private command
output. A build or runtime preflight failure leaves the running app alone. A
failure after pausing writes but before candidate activation restores the previous automatic pins and starts the
exact old backend container. If candidate activation or verification fails, the
script stops the candidate services and reports the matched recovery set. It
does **not** restart old code against a potentially migrated live database. Recover
into new storage using the previous images and matching backup/key; keep the
existing volumes intact. Power loss, forced termination or a failing Docker
daemon still require inspecting service health manually.

Run the development checks in [Contributing](../CONTRIBUTING.md) before pushing.
Automatic deployment does not run the complete backend/unit/browser suites, a
full encrypted restore rehearsal, or external DNS/TLS/public-origin probes. Its
health checks establish container and frontend-to-backend readiness; they do not
establish every application journey or physical-device compatibility.

## Moving between source repositories

This public project has fresh Git history. Do not repoint an existing private
checkout's remote here and force a pull or merge unrelated histories. Keep that
installation's current deployment process until a deliberate migration has been
rehearsed with a matched recovery set. See [the development workflow](DEVELOPMENT_WORKFLOW.md).
