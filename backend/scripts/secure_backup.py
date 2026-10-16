"""Encrypted Ledger archives using age. No private backup key is needed to create one.

Use a tmpfs/encrypted work directory. Pause the backend during backup so database
snapshots and uploaded documents describe the same point in time. Auth encryption
keys and the original Data/ archive are deliberately backed up separately.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time

from scripts.database import snapshot, inspect, snapshot_source


def private_file(path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb')


def auth_snapshot(source, target, *, offline_source=False):
    deadline = time.monotonic() + 60
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('Auth snapshot timed out.')
    with snapshot_source(source, offline_source=offline_source, directory=target.parent) as uri, \
            closing(sqlite3.connect(uri, uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            dst.execute('PRAGMA foreign_keys=ON')
            src.backup(dst, pages=256, progress=progress)
            dst.execute('PRAGMA journal_mode=DELETE')
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Authentication database failed integrity check.')
            dst.execute('DELETE FROM sessions')
            if dst.execute("SELECT 1 FROM sqlite_master WHERE name='installation_setup'").fetchone():
                dst.execute('DELETE FROM installation_setup')
            dst.commit()
    target.chmod(0o600)


def add_file(archive, source, name):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Backup refuses symlinks and special files.')
    info = archive.gettarinfo(str(source), arcname=name)
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    info.mode = 0o600
    with source.open('rb') as data:
        archive.addfile(info, data)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def create(data, uploads, output, recipient, work_dir=None, key=None, *, offline_source=False):
    if not recipient.startswith('age1') or len(recipient) > 200:
        raise ValueError('Provide an age public recipient key, not a private key.')
    if uploads.is_symlink() or not uploads.is_dir():
        raise ValueError('Uploads must be an existing directory (empty is allowed).')
    # Validate source trees and destination before starting age.
    files = sorted(uploads.rglob('*'))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in files):
        raise ValueError('Backup refuses symlinks and special files.')
    if output.exists() or output.is_symlink():
        raise ValueError('Backup destination exists; choose a new filename.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ledger-backup-', dir=work_dir) as temporary:
        stage = Path(temporary)
        snapshot(data / 'finance.db', stage / 'finance.db', offline_source=offline_source)
        if (data / 'auth.db').exists():
            auth_snapshot(data / 'auth.db', stage / 'auth.db', offline_source=offline_source)
        members = [(path, path.name) for path in sorted(stage.iterdir())]
        members.extend((path, 'uploads/' + path.relative_to(uploads).as_posix()) for path in files if path.is_file())
        manifest = {'format': 1, 'created_at': time.time(),
                    'files': {name: {'bytes': path.stat().st_size, 'sha256': file_digest(path)} for path, name in members},
                    'excluded': ['authentication encryption key', 'environment credentials', 'external original document archives'],
                    'restore_sessions': 'revoked'}
        with closing(sqlite3.connect((stage / 'finance.db').resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            manifest['database_schema_version'] = conn.execute('PRAGMA user_version').fetchone()[0]
        if key is not None:
            if key.is_symlink() or not key.is_file():
                raise ValueError('The separate authentication key is missing or unsafe.')
            manifest['authentication_key_sha256'] = file_digest(key)
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        members.append((stage / 'manifest.json', 'manifest.json'))
        fd, name = tempfile.mkstemp(prefix='.encrypted-', dir=output.parent)
        partial = Path(name)
        process = None
        try:
            with os.fdopen(fd, 'wb') as ciphertext:
                process = subprocess.Popen(['age', '--encrypt', '--recipient', recipient],
                                           stdin=subprocess.PIPE, stdout=ciphertext, stderr=subprocess.PIPE)
                try:
                    with tarfile.open(fileobj=process.stdin, mode='w|') as archive:
                        for source, name in members:
                            add_file(archive, source, name)
                finally:
                    process.stdin.close()
                process.stderr.read()
                process.stderr.close()
                if process.wait() != 0:
                    raise ValueError('age encryption failed; check the recipient key.')
                ciphertext.flush()
                os.fsync(ciphertext.fileno())
            os.link(partial, output)  # Refuse overwrite, including a racing destination.
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            if process:
                process.stdin.close()
                process.stderr.close()
            partial.unlink(missing_ok=True)


def restore(archive_path, identity, destination, work_dir=None):
    if destination.exists() or destination.is_symlink():
        raise ValueError('Restore requires a new destination directory.')
    with tempfile.TemporaryDirectory(prefix='ledger-restore-', dir=work_dir) as temporary:
        # Verify the entire authenticated ciphertext before extracting any contents.
        plain = Path(temporary) / 'verified.tar'
        with private_file(plain) as output:
            result = subprocess.run(['age', '--decrypt', '--identity', str(identity), str(archive_path)],
                                    stdout=output, stderr=subprocess.PIPE)
        if result.returncode:
            raise ValueError('Decryption failed: wrong identity, truncated or damaged archive.')
        with tarfile.open(plain, 'r:') as archive:
            members = archive.getmembers()
            names = set()
            for member in members:
                path = PurePosixPath(member.name)
                if (not member.isfile() or path.is_absolute() or '..' in path.parts
                        or member.name in names or len(path.parts) > 12
                        or (member.name not in {'finance.db', 'auth.db', 'manifest.json'} and
                            not (path.parts and path.parts[0] == 'uploads' and len(path.parts) > 1))):
                    raise ValueError('Archive contains an unsafe or unexpected member.')
                names.add(member.name)
            if 'finance.db' not in names:
                raise ValueError('Archive has no financial database.')
            destination.mkdir(mode=0o700)  # Exclusive creation; never overwrite live storage.
            try:
                for member in members:
                    target = destination / member.name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.extractfile(member) as source, private_file(target) as output:
                        shutil.copyfileobj(source, output)
                if 'manifest.json' in names:
                    manifest = json.loads((destination / 'manifest.json').read_text())
                    if (not isinstance(manifest, dict) or manifest.get('format') != 1
                            or not isinstance(manifest.get('files'), dict)
                            or set(manifest['files']) != names - {'manifest.json'}):
                        raise ValueError('Backup manifest does not match archive members.')
                    for name, expected in manifest['files'].items():
                        target = destination / name
                        if (not isinstance(expected, dict) or expected.get('bytes') != target.stat().st_size
                                or expected.get('sha256') != file_digest(target)):
                            raise ValueError('Backup manifest checksum verification failed.')
                inspect(destination / 'finance.db')
                (destination / 'uploads').mkdir(exist_ok=True, mode=0o700)
                if (destination / 'auth.db').exists():
                    with closing(sqlite3.connect(destination / 'auth.db')) as conn:
                        conn.execute('PRAGMA foreign_keys=ON')
                        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('Authentication database failed integrity check.')
                        conn.execute('DELETE FROM sessions')
                        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='installation_setup'").fetchone():
                            conn.execute('DELETE FROM installation_setup')
                        conn.commit()
            except BaseException:
                shutil.rmtree(destination)
                raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    backup = commands.add_parser('create')
    backup.add_argument('--data', type=Path, required=True)
    backup.add_argument('--uploads', type=Path, required=True)
    backup.add_argument('--recipient', required=True)
    backup.add_argument('--output', type=Path, required=True)
    backup.add_argument('--key', type=Path, help='Record a fingerprint of the separate auth key; never include the key itself.')
    backup.add_argument('--offline-source', action='store_true',
                        help='All database writers are stopped: stage database/WAL copies before reading read-only storage.')
    decrypt = commands.add_parser('restore')
    decrypt.add_argument('--archive', type=Path, required=True)
    decrypt.add_argument('--identity', type=Path, required=True)
    decrypt.add_argument('--destination', type=Path, required=True)
    for command in (backup, decrypt):
        command.add_argument('--work-dir', type=Path, default=Path('/tmp'))
    args = parser.parse_args()
    try:
        if args.action == 'create':
            create(args.data, args.uploads, args.output, args.recipient, args.work_dir, args.key,
                   offline_source=args.offline_source)
            print('Encrypted backup created. Keep the private recovery key offline.')
        else:
            restore(args.archive, args.identity, args.destination, args.work_dir)
            print('Archive verified and restored to new directory. Reset owner credentials before serving restored auth state.')
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        parser.exit(1, f'Backup operation failed: {error}\n')


if __name__ == '__main__':
    main()
