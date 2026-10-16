"""Read-only installation checks. Never emits account data or credentials."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import stat

from app import auth
from app.db import DB_PATH, SCHEMA_VERSION
from app.runtime import database_readiness
from app.security import validate_deployment


def diagnose():
    checks = []
    def check(name, okay, remedy=''):
        checks.append({'check': name, 'ok': bool(okay), **({'remedy': remedy} if not okay else {})})
    check('financial_database', database_readiness(DB_PATH)[1] == 200,
          'For a new installation run the explicit initializer. For an existing installation restore its database.')
    try:
        with closing(sqlite3.connect(DB_PATH.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            check('database_schema_supported', conn.execute('PRAGMA user_version').fetchone()[0] <= SCHEMA_VERSION,
                  'Use the matching newer application image or restore a pre-upgrade backup into new storage.')
    except (sqlite3.Error, OSError):
        check('database_schema_supported', False, 'Restore the financial database or initialize a new installation explicitly.')
    try:
        validate_deployment()
        check('deployment_configuration', True)
    except (ValueError, RuntimeError):
        check('deployment_configuration', False, 'Check deployment mode, HTTPS origin, allowed hosts, secure cookies and separate key mount.')
    owner = pending = False
    try:
        with closing(sqlite3.connect(auth.auth_path().resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            check('authentication_integrity', conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
            row = conn.execute('SELECT secret FROM owner WHERE id=1').fetchone()
            owner = bool(row)
            if row:
                auth.cipher().decrypt(row[0])
            else:
                pending = bool(conn.execute('SELECT 1 FROM installation_setup WHERE expires>unixepoch()').fetchone())
            check('authentication_key', auth.key_path().is_file() and bool(auth.cipher()))
    except Exception:
        check('authentication_storage', False, 'Verify the matching authentication database and key without replacing either.')
    check('owner_or_bootstrap', owner or pending, 'Enroll locally or renew the setup token for an initialized new installation.')
    for name, path in [('auth_database_permissions', auth.auth_path()), ('auth_key_permissions', auth.key_path())]:
        check(name, path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
              'Use private 0600 files owned by the application user; initialize volume ownership offline if required.')
    return {'ok': all(item['ok'] for item in checks), 'checks': checks,
            'mode': os.environ.get('FINANCE_DEPLOYMENT', 'local'), 'owner_configured': owner,
            'setup_pending': pending, 'isolation': 'one-household-per-instance'}


if __name__ == '__main__':
    result = diagnose()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
