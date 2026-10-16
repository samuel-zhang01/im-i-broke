# Backup and recovery

A recovery set consists of the encrypted finance/auth/uploads archive, the
matching separately encrypted `auth.key`, release/configuration metadata, any
environment credentials, and any original documents held outside uploads. Saved
connection credentials are encrypted inside `finance.db` with this same auth
key; replacing the key makes those credentials unreadable. Keep the age private
identity offline. The archive itself contains a versioned member manifest,
checksums, and optionally the auth-key fingerprint; it never contains that key.

For an existing root-Compose checkout, [`deploy.sh`](../deploy.sh) creates a
matched encrypted `recovery-*` set before activating an update. Its three
ciphertext files work with the recovery procedure below; see
[Automatic deployment](AUTOMATIC_DEPLOYMENT.md) for identity handling, manifests
and the distinct failure behaviour after candidate migrations begin.

For standard product deployments, the host-side helper creates the complete
matched set in one operation. Pass the exact private deployment directory and
your public age recipient; it does not discover or use the repository `.env`:

```sh
python3 /path/to/im-i-broke/scripts/backup_distribution.py /private/households/alex \
  --output /private/backups/alex --recipient age1YOUR_PUBLIC_RECIPIENT --keep 14
```

Each completed `ledger-set-*` directory contains `data.age`, `key.age`,
`configuration.age`, and a nonsecret checksum manifest. Configuration includes
the selected private environment file, Compose file, effective image/settings
and generated instance metadata when present. It is encrypted before leaving
memory. Export completed sets off the host; retaining only same-host copies
does not protect against loss of that host. Original documents stored outside
uploads must still be backed up separately.

The helper locks the explicit deployment directory, pauses the backend, and
resumes that same container in a `finally` block if it was previously running.
An already stopped backend stays stopped. Partial local sets are not published.
Retention removes only checksum-verified complete sets made by this helper for
the same project; unrelated, damaged and incomplete sets are preserved for
inspection. `--keep` counts complete sets rather than days. Custom bind mounts
or external volumes require an explicit backup procedure instead.

After testing a recovery set, an operator may add a schedule to the host user's
crontab, for example (absolute paths and actual public recipient required):

```cron
17 3 * * * /usr/bin/python3 /path/to/im-i-broke/scripts/backup_distribution.py /private/households/alex --output /private/backups/alex --recipient age1YOUR_PUBLIC_RECIPIENT --keep 14
```

No schedule is installed by this repository. Configure the host's job-failure
notifications and perform periodic restore rehearsals. The helper restarts the
backend on ordinary failures; a host power loss or forced process termination
still requires checking service health when the host returns.

To use a helper-generated set with the manual recovery commands below, select
that set's `data.age` and `key.age`. Decrypt `configuration.age` separately into
a new private directory using `age` and inspect the archived files before using
them. They contain credentials and the original project/port: preserve matching
image pins and secrets, but select a **new** project and private port for rehearsal.
Never extract configuration over an existing deployment.

For a manual matched data/key backup, use the commands below and separately
encrypt and preserve the deployment configuration and environment credentials.
Pause backend writes while taking a matched backup. Replace every example date
and recipient with a unique filename and your real **public** age recipient:

```sh
docker compose stop backend
docker compose run --rm admin python -m scripts.secure_backup create \
  --data /app/data --uploads /app/uploads --key /app/keys/auth.key \
  --recipient age1YOUR_PUBLIC_RECIPIENT \
  --output /app/backups/household-2026-09-21.age
docker compose run --rm admin age --encrypt --recipient age1YOUR_PUBLIC_RECIPIENT \
  --output /app/backups/owner-key-2026-09-21.age /app/keys/auth.key
docker compose up -d --wait backend frontend
```

Export the encrypted files off the host, using private local permissions:

```sh
umask 077
docker compose run --rm -T admin cat /app/backups/household-2026-09-21.age > household-2026-09-21.age
docker compose run --rm -T admin cat /app/backups/owner-key-2026-09-21.age > owner-key-2026-09-21.age
```

The backend image contains `age`; restoring on a separate host also requires
`age`. From `backend/`, restore to a **new** private destination on encrypted
storage (or tmpfs), never over the current installation:

```sh
mkdir -m 700 /private/recovery
python3 -m scripts.secure_backup restore --archive /private/household-2026-09-21.age \
  --identity /offline/age-identity.txt --destination /private/recovery/data
mkdir -m 700 /private/recovery/keys
age --decrypt --identity /offline/age-identity.txt \
  --output /private/recovery/keys/auth.key /private/owner-key-2026-09-21.age
chmod 600 /private/recovery/keys/auth.key
```

Restore verifies authenticated encryption, safe archive paths, checksums and
database integrity before the destination is usable. Legacy archives without a
manifest remain supported. Sessions and pending bootstrap authorization are
revoked. Compare `sha256sum /private/recovery/keys/auth.key` to
`authentication_key_sha256` in the restored `manifest.json` when present.

Use a **new** instance ID and private port, the matching release images, and
[`deploy/compose.recovery.example.yml`](../deploy/compose.recovery.example.yml) with `FINANCE_RECOVERY_ROOT` set to the new
recovery root. Ensure restored files are private and owned by container UID
10001 before starting; this concerns only the new recovery directory. Reset the
owner with the existing key using the offline admin service, verify the doctor
checks and inspect records/source documents before changing the public proxy.
Keep old volumes and backups intact until recovery is confirmed.

```sh
docker compose -f compose.yml -f compose.recovery.example.yml run --rm admin python -m scripts.auth reset
docker compose -f compose.yml -f compose.recovery.example.yml run --rm admin python -m scripts.doctor
docker compose -f compose.yml -f compose.recovery.example.yml up -d --wait backend frontend
```

Never use `down -v` for routine maintenance or recovery. Rotate provider
credentials separately if a compromise, rather than ordinary recovery, prompted
the restore.
