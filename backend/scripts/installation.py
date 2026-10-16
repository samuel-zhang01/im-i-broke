"""Explicit offline initialization. Never use init to repair a missing database.

Run `python -m scripts.installation init` against NEW, empty volumes only.
Existing households continue using their present deployment and terminal auth.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import uuid

from cryptography.fernet import Fernet

from app import auth
from app.db import DB_PATH, init_db, apply_startup_migrations
from app.lunchflow_store import ensure_schema
from app.runtime import database_readiness

TOKEN_LIFETIME = 24 * 3600


def _safe_path(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Installation paths must not contain symlinks.')
    return path


def initialize(db_path, uploads, auth_db, key):
    """Return a non-secret installation manifest; refuse any existing storage."""
    db_path, uploads, auth_db, key = map(_safe_path, (db_path, uploads, auth_db, key))
    if auth_db.parent != db_path.parent or key.parent == db_path.parent:
        raise ValueError('Use one data directory for both databases and a separate key directory.')
    roots = (db_path.parent, uploads, key.parent)
    if len(set(roots)) != 3 or any(a in b.parents for a in roots for b in roots if a != b):
        raise ValueError('Data, uploads and keys must be separate non-nested directories.')
    if db_path.name == auth_db.name or key.name in {'setup-token.txt', 'installation.json'}:
        raise ValueError('Installation filenames conflict.')
    for root in roots:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = db_path.parent / '.installation-lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('Initialization is already in progress. Inspect storage before removing a stale lock.') from None
    os.close(fd)
    try:
        for root in roots:
            if any(item != lock for item in root.iterdir()):
                raise ValueError('Initialization requires empty data, uploads and key directories. Restore existing installations; never initialize over them.')
        with tempfile.TemporaryDirectory(prefix='.new-installation-', dir=db_path.parent) as temporary:
            stage = Path(temporary)
            financial = stage / 'finance.db'
            conn = init_db(financial)
            try:
                ensure_schema(conn)
                conn.commit()
                apply_startup_migrations(conn)
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                conn.execute('PRAGMA journal_mode=DELETE')
            finally:
                conn.close()
            token = secrets.token_urlsafe(32)
            expires = time.time() + TOKEN_LIFETIME
            authentication = stage / 'auth.db'
            with closing(sqlite3.connect(authentication)) as conn:
                conn.executescript(auth.SCHEMA)
                conn.execute('INSERT INTO installation_setup(id,token_digest,expires) VALUES(1,?,?)',
                             (auth.digest(token), expires))
                conn.commit()
            manifest = {'format': 1, 'installation_id': str(uuid.uuid4()), 'created_at': time.time(),
                        'isolation': 'one-household-per-instance', 'setup_expires_at': expires}
            content = [(financial, db_path), (authentication, auth_db)]
            for filename, target, value in [
                ('auth.key', key, Fernet.generate_key()),
                ('setup-token.txt', key.parent / 'setup-token.txt', (token + '\n').encode()),
                ('installation.json', db_path.parent / 'installation.json', (json.dumps(manifest, indent=2) + '\n').encode()),
            ]:
                staged = stage / filename
                staged.write_bytes(value)
                content.append((staged, target))
            # Every destination is exclusive. Interrupted initialization is
            # deliberately left for inspection, never silently replaced on retry.
            for source, target in content:
                source.chmod(0o600)
                with source.open('rb') as output:
                    os.fsync(output.fileno())
                with target.open('xb') as output, source.open('rb') as input_file:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(input_file.read())
                    output.flush()
                    os.fsync(output.fileno())
            return manifest
    finally:
        lock.unlink(missing_ok=True)


def setup_token(*, renew=False):
    if not auth.auth_path().is_file() or not auth.key_path().is_file() or database_readiness(DB_PATH)[1] != 200:
        raise ValueError('New installation is incomplete. Inspect storage; do not recreate missing files.')
    with auth.database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        setup = conn.execute('SELECT * FROM installation_setup WHERE id=1').fetchone()
        if conn.execute('SELECT 1 FROM owner').fetchone() or not setup:
            raise ValueError('This installation is not awaiting browser setup. Use terminal owner recovery.')
        token_file = _safe_path(auth.key_path().parent / 'setup-token.txt')
        if renew:
            auth.cipher()  # Never rotate a token against an invalid/missing key.
            token = secrets.token_urlsafe(32)
            fd, temporary = tempfile.mkstemp(prefix='.setup-token-', dir=token_file.parent)
            try:
                with os.fdopen(fd, 'w') as output:
                    output.write(token + '\n')
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, token_file)
            finally:
                Path(temporary).unlink(missing_ok=True)
            conn.execute('UPDATE installation_setup SET token_digest=?,expires=?,challenge_digest=NULL,secret=NULL,'
                         'challenge_until=NULL,attempts=0,throttle_until=0 WHERE id=1',
                         (auth.digest(token), time.time() + TOKEN_LIFETIME))
            auth.audit(conn, 'setup_token_renewed')
            conn.commit()
        else:
            if setup['expires'] <= time.time():
                raise ValueError('Setup token expired. Run renew-token offline to issue a new token.')
            token = token_file.read_text().strip()
            if auth.digest(token) != setup['token_digest']:
                raise ValueError('Setup token file does not match. Run renew-token offline.')
    return token


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'token', 'renew-token'])
    args = parser.parse_args()
    try:
        if args.action == 'init':
            manifest = initialize(DB_PATH, Path(os.environ.get('FINANCE_UPLOADS_PATH', '/app/uploads')),
                                  auth.auth_path(), auth.key_path())
            print(json.dumps(manifest, indent=2))
            print('Ready for browser setup. Run the token command privately; the token expires after 24 hours.')
        else:
            print(setup_token(renew=args.action == 'renew-token'))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f'Installation operation failed: {error}\n')


if __name__ == '__main__':
    main()
