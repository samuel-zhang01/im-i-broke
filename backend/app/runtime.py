"""Small request boundary and database readiness checks for trusted local use.

This is not authentication: a client on the allowed network can use the API.
The boundary prevents unrelated browser origins from submitting mutations and
rejects unconfigured Host headers, including DNS-rebinding hostnames.
"""
from __future__ import annotations

import os
import hashlib
import atexit
import secrets
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment):
            return None
        return (parsed.scheme, parsed.hostname.lower().rstrip('.'),
                parsed.port or (443 if parsed.scheme == 'https' else 80))
    except ValueError:
        return None


class RequestBoundaryMiddleware:
    def __init__(self, app, allowed_hosts=None):
        self.app = app
        configured = allowed_hosts if allowed_hosts is not None else os.environ.get(
            'FINANCE_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver').split(',')
        self.allowed_hosts = {host.strip().lower().strip('[]').rstrip('.') for host in configured if host.strip()}
        if not self.allowed_hosts or any('*' in host or '/' in host for host in self.allowed_hosts):
            raise ValueError('FINANCE_ALLOWED_HOSTS must list explicit hostnames/IP addresses, without wildcards.')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {name.decode('latin1').lower(): value.decode('latin1') for name, value in scope['headers']}
        destination = _origin(scope.get('scheme', 'http') + '://' + headers.get('host', ''))
        if not destination or destination[1] not in self.allowed_hosts:
            return await JSONResponse({'detail': 'Host is not configured for this local installation.'}, 400)(scope, receive, send)
        if scope.get('path', '').startswith('/api/'):
            origin = headers.get('origin')
            browser_site = headers.get('sec-fetch-site', '').lower()
            if browser_site == 'cross-site':
                return await JSONResponse({'detail': 'Cross-site browser API requests are not allowed.'}, 403)(scope, receive, send)
            if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'} and origin is not None:
                source = _origin(origin)
                # Vite's extra origin is for local development only. Hosted and
                # trusted-LAN instances must not inherit a localhost exception.
                # CLI requests without Origin remain supported.
                development = (os.environ.get('FINANCE_DEPLOYMENT', 'local') == 'local'
                               and source in {('http', 'localhost', 5173), ('http', '127.0.0.1', 5173)})
                public_origin = _origin(os.environ.get('FINANCE_PUBLIC_ORIGIN', ''))
                if not source or (source != destination and source != public_origin and not development):
                    return await JSONResponse({'detail': 'Cross-origin mutations are not allowed.'}, 403)(scope, receive, send)
        return await self.app(scope, receive, send)


def database_readiness(path: Path):
    if not path.is_file():
        return {'ok': False, 'db': False, 'status': 'database_missing'}, 503
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            if conn.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('Integrity check failed')
            # These queries detect an unrelated, empty or unmigrated SQLite
            # file without reading or exposing any personal row contents.
            conn.execute('SELECT id,txn_date,amount,classification FROM transactions LIMIT 1').fetchone()
            conn.execute('SELECT id FROM accounts LIMIT 1').fetchone()
            conn.execute('SELECT id FROM statements LIMIT 1').fetchone()
            conn.execute('SELECT version FROM data_migrations LIMIT 1').fetchone()
    except (sqlite3.Error, OSError):
        return {'ok': False, 'db': True, 'status': 'database_unavailable'}, 503
    return {'ok': True, 'db': True, 'status': 'ready'}, 200


_REVISION_OBSERVER_LIMIT = 8
_revision_lock = threading.Lock()
_revision_observers = OrderedDict()


def _close_revision_observers():
    """Release idle SQLite observers on process exit (also useful in tests)."""
    with _revision_lock:
        for observer in _revision_observers.values():
            observer['connection'].close()
        _revision_observers.clear()


atexit.register(_close_revision_observers)


def database_revision(path: Path):
    """Opaque logical-write hint, stable across read-only connection lifecycles.

    SQLite data_version changes when another connection commits. Keeping one
    read-only observer per database makes those values comparable across polls;
    it holds no read transaction and does not block checkpoints or writers.
    Ordinary reads and file timestamp touches do not invent data changes. An
    explicit maintenance checkpoint can conservatively invalidate this hint. File replacement and observer/process restarts
    get a new nonce so clients also refresh after a restore or server restart.
    """
    key = str(path.absolute())
    with _revision_lock:
        observer = _revision_observers.pop(key, None)
        try:
            stat = path.stat()
            if not path.is_file():
                raise OSError('Database is unavailable')
            identity = (stat.st_dev, stat.st_ino)
            if observer and (observer['identity'] != identity or observer['pid'] != os.getpid()):
                observer['connection'].close()
                observer = None
            if observer is None:
                connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                             timeout=1, check_same_thread=False)
                observer = {'connection':connection, 'identity':identity,
                            'pid':os.getpid(), 'nonce':secrets.token_hex(16)}
            version = observer['connection'].execute('PRAGMA data_version').fetchone()[0]
            # Do not retain an observer opened against a path replaced while
            # connecting. The next poll will open the current database safely.
            current = path.stat()
            if (current.st_dev,current.st_ino) != identity:
                raise OSError('Database changed while opening')
            _revision_observers[key] = observer
            while len(_revision_observers) > _REVISION_OBSERVER_LIMIT:
                _, expired = _revision_observers.popitem(last=False)
                expired['connection'].close()
            revision = hashlib.sha256(f"{observer['nonce']}:{version}".encode()).hexdigest()[:24]
            return {'revision':revision}, 200
        except (sqlite3.Error, OSError, ValueError):
            if observer:
                observer['connection'].close()
            return {'detail':'Database is unavailable.'}, 503
