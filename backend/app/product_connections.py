"""Credential-scoped provider adapters; environment connections remain intact.

Provider secrets use the owner's existing encryption key, are never returned,
and cannot be recovered by silently generating a replacement key.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import asynccontextmanager, suppress
import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
import json
import os
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import account_settings, auth, db, lunchflow_client, snaptrade_client

class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def private_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default validation response includes the rejected
                # input, which may itself be a credential or signed key.
                return JSONResponse({'detail': 'Check the provider, connection name and required credential fields.'}, 422,
                                    headers={'Cache-Control': 'no-store'})
        return private_handler


MAX_SAVED_CONNECTIONS = 50


router = APIRouter(prefix='/api/connections', tags=['Provider connections'], route_class=PrivateValidationRoute)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS provider_connections (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL, secrets BLOB,
 enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'unverified',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT,
 last_synced_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS provider_connection_jobs (
 connection_id TEXT PRIMARY KEY, owner TEXT, expires_at TEXT, last_attempt_at TEXT);
CREATE TABLE IF NOT EXISTS provider_connection_cache (
 connection_id TEXT PRIMARY KEY REFERENCES provider_connections(id), payload TEXT NOT NULL,
 saved_at TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
 last_successful_refresh_at TEXT NOT NULL, refresh_error TEXT);
'''


def ensure_schema(conn):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(provider_connections)')}
    if 'auth_method' not in columns:
        conn.execute("ALTER TABLE provider_connections ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'personal_api_key'")


def now():
    return datetime.now(timezone.utc).isoformat()


class ConnectionActionError(ValueError):
    """Fixed local messages safe to display; never provider exception text."""


class ConnectionBusyError(ValueError):
    """A lease or cooldown prevented an operation from starting."""


class ConnectionUnavailableError(ValueError):
    """A paused or disconnected connection must retain its saved state."""


class ConnectionCredentialError(ConnectionActionError):
    """An actionable local credential error, safe to persist and display."""


@dataclass(frozen=True)
class ProviderAdapter:
    id: str
    label: str
    credential_fields: tuple
    capabilities: tuple

    def public(self):
        try:
            from .provider_oauth import available_methods
            hosted_methods = available_methods(self.id)
        except ImportError:
            hosted_methods = []
        return {'id': self.id, 'label': self.label, 'auth_methods': ['personal_api_key', *hosted_methods],
                'capabilities': list(self.capabilities),
                'credential_fields': [{'name': key, 'label': label, 'secret': True} for key, label in self.credential_fields],
                'oauth_available': bool(hosted_methods)}

    def reader(self, credentials):
        if self.id == 'lunchflow':
            if 'access_token' in credentials:
                from .provider_oauth import platform_read
                return partial(platform_read, access_token=credentials['access_token'])
            return partial(lunchflow_client.read, api_key=credentials['api_key'])
        if self.id == 'snaptrade':
            return partial(snaptrade_client.call, credentials=credentials)
        raise ConnectionActionError('This provider does not have a configured read adapter.')

    def validate(self, credentials):
        read = self.reader(credentials)
        if self.id == 'lunchflow':
            return len(lunchflow_client.list_accounts(read('accounts')))
        from . import snaptrade_models
        rows = snaptrade_models.rows(read('accounts'))
        if len(rows) > snaptrade_client.MAX_ACCOUNTS:
            raise ValueError('Too many accounts.')
        ids = [snaptrade_models.account(row)['id'] for row in rows]
        if any(not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError('Invalid account identifiers.')
        return len(ids)

    def sync(self, credentials, connection_id, path):
        if self.id == 'lunchflow':
            from .lunchflow_routes import run_sync
            return run_sync(reader=self.reader(credentials), db_path=path, provider_connection_id=connection_id)
        if self.id != 'snaptrade':
            raise ConnectionActionError('This provider does not have a configured sync adapter.')
        from . import snaptrade_store
        payload = snaptrade_client.fetch_portfolio(read=self.reader(credentials), credentials_configured=True)
        if payload.get('error') and not payload.get('accounts'):
            raise ValueError('The portfolio could not be refreshed. Saved data is preserved.')
        if not connection_id.startswith('environment:'):
            previous = snaptrade_store.load_portfolio(path=path)
            occupied = set()
            for account in (previous or {}).get('accounts', []):
                scope = account.get('connection_scope') or 'environment:snaptrade'
                if scope == connection_id or account.get('connection_disconnected'):
                    continue
                external_id = account['id'].removeprefix(scope + ':') if scope != 'environment:snaptrade' else account['id']
                occupied.add((external_id, account.get('institution')))
            if any((account['id'], account.get('institution')) in occupied for account in payload['accounts']):
                raise ConnectionActionError('A brokerage account already belongs to another saved connection. Reconnect the original connection to preserve its history without counting it twice.')
            identifiers = [account['id'] for account in payload['accounts']]
            identifiers.extend(account['connection_id'] for account in payload['accounts'] if account.get('connection_id'))
            identifiers.extend(connection['id'] for connection in payload['connections']['data'])
            if any(len(connection_id) + 1 + len(identifier) > 240 for identifier in identifiers):
                raise ConnectionActionError('The provider returned an account identifier too long to store safely. Saved history is preserved.')
            for account in payload['accounts']:
                account['id'] = connection_id + ':' + account['id']
                if account.get('connection_id'):
                    account['connection_id'] = connection_id + ':' + account['connection_id']
            for connection in payload['connections']['data']:
                connection['id'] = connection_id + ':' + connection['id']
        return snaptrade_store.save_portfolio(payload, path=path,
            connection_scope=None if connection_id.startswith('environment:') else connection_id)


PROVIDERS = {
    'lunchflow': ProviderAdapter('lunchflow', 'Lunch Flow', (('api_key', 'API key'),), ('accounts', 'balances', 'transactions')),
    'snaptrade': ProviderAdapter('snaptrade', 'SnapTrade', (('client_id', 'Client ID'), ('consumer_key', 'Personal API key')),
                                ('accounts', 'balances', 'holdings', 'activities', 'history')),
}


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    provider: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=100)
    credentials: dict[str, str] = Field(min_length=1, max_length=8)


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    credentials: dict[str, str] | None = Field(default=None, min_length=1, max_length=8)


def _adapter(provider):
    if provider not in PROVIDERS:
        raise ValueError('Choose a supported provider.')
    return PROVIDERS[provider]


def _credentials(provider, values):
    expected = {key for key, _ in _adapter(provider).credential_fields}
    if not isinstance(values, dict) or set(values) != expected:
        raise ValueError('Provide exactly the credential fields required by this provider.')
    if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 4096
           or any(ord(character) < 32 or ord(character) == 127 for character in value) for value in values.values()):
        raise ValueError('Provider credentials must contain 1 to 4,096 characters without control characters.')
    return {key: value.strip() for key, value in values.items()}


def _encrypt(provider, values):
    credentials = _credentials(provider, values)
    try:
        return auth.cipher().encrypt(json.dumps(credentials).encode())
    except Exception:
        raise ValueError('The installation encryption key is unavailable. Restore the matching key before saving credentials.') from None


def environment_credentials(provider):
    if provider == 'lunchflow' and lunchflow_client.configured():
        return {'api_key': os.environ['LUNCH_FLOW_API'].strip()}
    if provider == 'snaptrade' and snaptrade_client.configured():
        return {'client_id': os.environ['SNAPTRADE_ID'].strip(), 'consumer_key': os.environ['SNAPTRADE_KEY'].strip()}
    return None


def public_connection(conn, connection_id):
    if connection_id.startswith('environment:'):
        provider = connection_id.removeprefix('environment:')
        adapter = _adapter(provider)
        if not environment_credentials(provider):
            raise LookupError('Provider connection not found.')
        return {'id': connection_id, 'provider': provider, 'label': adapter.label + ' (server configuration)',
                'source': 'environment', 'enabled': True, 'status': 'configured', 'created_at': None, 'updated_at': None,
                'last_checked_at': None, 'last_synced_at': None, 'last_error': None, 'capabilities': list(adapter.capabilities), 'auth_method': 'personal_api_key'}
    row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (connection_id,)).fetchone()
    if not row:
        raise LookupError('Provider connection not found.')
    fields = ('id', 'provider', 'label', 'status', 'created_at', 'updated_at', 'last_checked_at', 'last_synced_at', 'last_error', 'auth_method')
    return {**{key: row[key] for key in fields}, 'source': 'saved', 'enabled': bool(row['enabled']),
            'capabilities': list(_adapter(row['provider']).capabilities)}


def resolve_credentials(conn, connection_id):
    metadata = public_connection(conn, connection_id)
    if metadata['source'] == 'environment':
        return metadata, environment_credentials(metadata['provider'])
    row = conn.execute('SELECT secrets FROM provider_connections WHERE id=?', (connection_id,)).fetchone()
    if not metadata['enabled']:
        raise ConnectionUnavailableError('Reconnect or enable this connection before reading the provider.')
    if not row['secrets']:
        raise ConnectionCredentialError('Reconnect with provider credentials before reading this connection.')
    try:
        values = json.loads(auth.cipher().decrypt(row['secrets']).decode())
    except Exception:
        raise ConnectionCredentialError('Saved credentials could not be decrypted. Restore the matching installation key or reconnect with new credentials.') from None
    if metadata['auth_method'] != 'personal_api_key':
        from .provider_oauth import resolve_hosted_credentials
        try:
            return metadata, resolve_hosted_credentials(conn, metadata, values)
        except Exception:
            raise ConnectionCredentialError('Provider authorization needs attention. Reconnect from Settings or check the operator configuration. Saved history is preserved.') from None
    try:
        return metadata, _credentials(metadata['provider'], values)
    except ValueError:
        raise ConnectionCredentialError('Saved credentials are invalid. Reconnect from Settings with the required provider credentials. Saved history is preserved.') from None


def _assert_unique_credentials(conn, provider, values, exclude=None):
    if values == environment_credentials(provider):
        raise ValueError('These credentials are already configured on the server. Use the existing connection.')
    for row in conn.execute('SELECT id,secrets FROM provider_connections WHERE provider=? AND secrets IS NOT NULL', (provider,)):
        if row['id'] == exclude:
            continue
        try:
            if json.loads(auth.cipher().decrypt(row['secrets']).decode()) == values:
                raise ValueError('These credentials already have a saved connection. Reuse or reconnect it.')
        except ValueError:
            raise
        except Exception:
            raise ValueError('Existing credentials cannot be verified with this installation key. Restore the matching key first.') from None


def _admit_writer(conn):
    # Callers own commit/rollback. A no-row write upgrades a caller's deferred
    # transaction without committing earlier work or changing connection data.
    if conn.in_transaction:
        conn.execute('UPDATE provider_connections SET id=id WHERE 0')
    else:
        conn.execute('BEGIN IMMEDIATE')


def create_connection(conn, body):
    ensure_schema(conn)
    _admit_writer(conn)
    if conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0] >= MAX_SAVED_CONNECTIONS:
        raise ValueError('This workspace supports up to 50 saved provider connections.')
    values = _credentials(body.provider, body.credentials)
    _assert_unique_credentials(conn, body.provider, values)
    encrypted = _encrypt(body.provider, values)
    connection_id = 'connection:' + uuid.uuid4().hex
    stamp = now()
    conn.execute('INSERT INTO provider_connections(id,provider,label,secrets,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                 (connection_id, body.provider, body.label, encrypted, stamp, stamp))
    return public_connection(conn, connection_id)


def _assert_idle(conn, connection_id):
    active = conn.execute('SELECT 1 FROM provider_connection_jobs WHERE connection_id=? AND owner IS NOT NULL AND expires_at>?',
                          (connection_id, now())).fetchone()
    if active:
        raise ConnectionBusyError('A provider operation is running. Wait for it to finish before changing this connection.')


def update_connection(conn, connection_id, body):
    _admit_writer(conn)
    metadata = public_connection(conn, connection_id)
    if metadata['source'] == 'environment':
        raise ValueError('This connection is managed by server environment configuration.')
    _assert_idle(conn, connection_id)
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise ValueError('Connection settings cannot be null.')
    if 'credentials' in changes:
        if metadata['auth_method'] != 'personal_api_key':
            raise ValueError('Reconnect this connection through the provider sign-in flow to update its consent.')
        values = _credentials(metadata['provider'], changes.pop('credentials'))
        _assert_unique_credentials(conn, metadata['provider'], values, exclude=connection_id)
        encrypted = _encrypt(metadata['provider'], values)
        conn.execute("UPDATE provider_connections SET secrets=?,status='unverified',last_error=NULL,enabled=1 WHERE id=?", (encrypted, connection_id))
    if 'enabled' in changes and changes['enabled']:
        if not conn.execute('SELECT secrets FROM provider_connections WHERE id=?', (connection_id,)).fetchone()[0]:
            raise ValueError('Provide credentials to reconnect this connection.')
    for key, value in changes.items():
        conn.execute('UPDATE provider_connections SET ' + key + '=? WHERE id=?', (value, connection_id))
    conn.execute('UPDATE provider_connections SET updated_at=? WHERE id=?', (now(), connection_id))
    return public_connection(conn, connection_id)


def disconnect_connection(conn, connection_id):
    _admit_writer(conn)
    metadata = public_connection(conn, connection_id)
    if metadata['source'] == 'environment':
        raise ValueError('Remove the credentials from server configuration to disconnect this connection.')
    _assert_idle(conn, connection_id)
    conn.execute("UPDATE provider_connections SET secrets=NULL,enabled=0,status='disconnected',last_error=NULL,updated_at=? WHERE id=?", (now(), connection_id))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_authorizations'").fetchone():
        conn.execute('DELETE FROM provider_authorizations WHERE connection_id=?', (connection_id,))
    return public_connection(conn, connection_id)


def acquire_job(conn, connection_id):
    conn.execute('BEGIN IMMEDIATE')
    _assert_idle(conn, connection_id)
    row = conn.execute('SELECT last_attempt_at FROM provider_connection_jobs WHERE connection_id=?', (connection_id,)).fetchone()
    current = datetime.now(timezone.utc)
    if row and row[0] and (current - datetime.fromisoformat(row[0])).total_seconds() < 60:
        raise ConnectionBusyError('Please wait one minute between provider operations.')
    owner = uuid.uuid4().hex
    conn.execute('''INSERT INTO provider_connection_jobs(connection_id,owner,expires_at,last_attempt_at) VALUES(?,?,?,?)
        ON CONFLICT(connection_id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at,last_attempt_at=excluded.last_attempt_at''',
        (connection_id, owner, (current + timedelta(minutes=10)).isoformat(), current.isoformat()))
    conn.commit()
    return owner


def perform_operation(connection_id, operation, *, path=None):
    path = path or db.DB_PATH
    if not path.exists():
        raise ValueError('Set up or restore the database before syncing.')
    conn = db.connect(path)
    owner = None
    try:
        ensure_schema(conn)
        conn.commit()
        metadata = public_connection(conn, connection_id)  # Reject unknown IDs before allocating a job record.
        owner = acquire_job(conn, connection_id)
        error, result = None, None
        try:
            metadata, credentials = resolve_credentials(conn, connection_id)
            adapter = _adapter(metadata['provider'])
            result = adapter.validate(credentials) if operation == 'validate' else adapter.sync(credentials, connection_id, path)
            if metadata['provider'] == 'lunchflow' and isinstance(result, dict):
                error = result.get('result', {}).get('error')
        except ConnectionUnavailableError:
            # A disconnect/pause may win admission after the scheduler selects
            # IDs. Preserve that state; it is not a failed credential refresh.
            raise
        except ConnectionActionError as failure:
            error = str(failure)
        except Exception:
            # Provider bodies, SDK exceptions and arbitrary injected exception
            # text never enter the connection record or HTTP response.
            error = 'The provider could not be read. Check credentials and consent, then retry. Saved history is preserved.'
        stamp = now()
        with conn:
            conn.execute('''UPDATE provider_connections SET status=?,last_error=?,last_checked_at=?,
                last_synced_at=CASE WHEN ? THEN ? ELSE last_synced_at END WHERE id=?''',
                ('error' if error else 'ready', error, stamp, operation == 'sync' and not error, stamp, connection_id))
            if error and metadata['provider'] == 'snaptrade':
                conn.execute('UPDATE provider_connection_cache SET last_attempt_at=?,refresh_error=? WHERE connection_id=?', (stamp, error, connection_id))
            conn.execute('UPDATE provider_connection_jobs SET owner=NULL,expires_at=NULL WHERE connection_id=? AND owner=?', (connection_id, owner))
        owner = None
        metadata = public_connection(conn, connection_id)
        if metadata['source'] == 'environment':
            metadata.update(status='error' if error else 'ready', last_error=error, last_checked_at=stamp)
        return {'connection': metadata, 'result': result}
    finally:
        if owner:
            conn.rollback()
            with conn:
                conn.execute('UPDATE provider_connection_jobs SET owner=NULL,expires_at=NULL WHERE connection_id=? AND owner=?', (connection_id, owner))
        conn.close()


def saved_connection_ids(provider, path=None):
    path = path or db.DB_PATH
    if not path.exists():
        return []
    import sqlite3
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connections'").fetchone():
            return []
        return [row[0] for row in conn.execute('''SELECT id FROM provider_connections
            WHERE provider=? AND enabled=1 AND secrets IS NOT NULL ORDER BY created_at,id''', (provider,))]
    finally:
        conn.close()


def has_saved_connections(provider, path=None):
    return bool(saved_connection_ids(provider, path))


def sync_statuses(provider, *, conn=None, path=None):
    """Include failed first connections even when they have no accounts/cache.

    Read-only and credential-free; account discovery must not hide a failed
    connection simply because another connection produced a useful portfolio.
    """
    import sqlite3
    owned = conn is None
    if owned:
        path = Path(path or db.DB_PATH)
        if not path.exists():
            return []
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connections'").fetchone():
            return []
        return [{**dict(row), 'enabled': bool(row['enabled'])} for row in conn.execute(
            '''SELECT id,label,enabled,status,last_synced_at,last_error FROM provider_connections
               WHERE provider=? ORDER BY created_at,id''', (provider,))]
    finally:
        if owned:
            conn.close()


def refresh_saved_connections(provider, path=None):
    results = []
    for connection_id in saved_connection_ids(provider, path):
        try:
            results.append(perform_operation(connection_id, 'sync', path=path))
        except (ConnectionBusyError, ConnectionUnavailableError):
            # Another operation owns the lease, or the connection was paused
            # after this ID list was read. Preserve its cache and saved state.
            continue
    return results


def _scheduled_connections():
    from .lunchflow_scheduler import sync_seconds as bank_interval, retry_interval
    from .portfolio_scheduler import sync_seconds as portfolio_interval
    intervals = {'lunchflow': bank_interval(), 'snaptrade': portfolio_interval()}
    if not db.DB_PATH.exists():
        return []
    conn = db.connect(db.DB_PATH)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_connections'").fetchone():
            return []
        current = datetime.now(timezone.utc)
        due = []
        for row in conn.execute('SELECT id,provider,last_checked_at,last_error FROM provider_connections WHERE enabled=1 AND secrets IS NOT NULL'):
            interval = intervals.get(row['provider'], 0)
            if not interval:
                continue
            if row['provider'] == 'lunchflow':
                interval = retry_interval({'error': row['last_error']}, interval)
            try:
                elapsed = (current - datetime.fromisoformat(row['last_checked_at'])).total_seconds()
            except (TypeError, ValueError):
                elapsed = interval
            if elapsed >= interval:
                due.append(row['id'])
        return due
    finally:
        conn.close()


async def _scheduled_loop(after_refresh):
    await asyncio.sleep(45)
    while True:
        try:
            due = await asyncio.to_thread(_scheduled_connections)
            for connection_id in due:
                try:
                    result = await asyncio.to_thread(perform_operation, connection_id, 'sync')
                    if not result['connection']['last_error'] and after_refresh:
                        await asyncio.to_thread(after_refresh)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)


@asynccontextmanager
async def saved_connections_scheduler(after_refresh=None):
    task = asyncio.create_task(_scheduled_loop(after_refresh), name='ledger-saved-provider-sync')
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _response_error(error):
    if isinstance(error, LookupError):
        raise HTTPException(404, str(error)) from None
    if isinstance(error, ValueError):
        status = 429 if isinstance(error, ConnectionBusyError) else 409
        raise HTTPException(status, str(error)) from None
    raise HTTPException(503, 'Provider settings are temporarily unavailable.') from None


@router.get('')
def list_connections():
    with account_settings.database() as conn:
        ensure_schema(conn)
        connections = [public_connection(conn, 'environment:' + key) for key in PROVIDERS if environment_credentials(key)]
        connections.extend(public_connection(conn, row[0]) for row in conn.execute('SELECT id FROM provider_connections ORDER BY created_at,id'))
        return JSONResponse({'providers': [adapter.public() for adapter in PROVIDERS.values()], 'connections': connections}, headers=account_settings.HEADERS)


@router.post('', status_code=201)
def add_connection(body: ConnectionCreate):
    with account_settings.database() as conn:
        try:
            with conn:
                result = create_connection(conn, body)
            return JSONResponse(result, status_code=201, headers=account_settings.HEADERS)
        except Exception as error:
            _response_error(error)


@router.patch('/{connection_id}')
def patch_connection(connection_id: str, body: ConnectionPatch):
    with account_settings.database() as conn:
        ensure_schema(conn)
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                result = update_connection(conn, connection_id, body)
            return JSONResponse(result, headers=account_settings.HEADERS)
        except Exception as error:
            _response_error(error)


@router.post('/{connection_id}/disconnect')
def disconnect(connection_id: str):
    with account_settings.database() as conn:
        ensure_schema(conn)
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                result = disconnect_connection(conn, connection_id)
            return JSONResponse(result, headers=account_settings.HEADERS)
        except Exception as error:
            _response_error(error)


@router.post('/{connection_id}/validate')
def validate_connection(connection_id: str):
    try:
        return JSONResponse(perform_operation(connection_id, 'validate')['connection'], headers=account_settings.HEADERS)
    except Exception as error:
        _response_error(error)


@router.post('/{connection_id}/sync')
def sync_connection(connection_id: str):
    try:
        return JSONResponse(perform_operation(connection_id, 'sync'), headers=account_settings.HEADERS)
    except Exception as error:
        _response_error(error)


@router.get('/{connection_id}/data')
def connection_data(connection_id: str):
    with account_settings.database() as conn:
        ensure_schema(conn)
        try:
            metadata = public_connection(conn, connection_id)
            if metadata['provider'] == 'snaptrade':
                from .snaptrade_store import load_portfolio
                return JSONResponse(load_portfolio(connection_scope=connection_id), headers=account_settings.HEADERS)
            from . import lunchflow_store
            lunchflow_store.ensure_schema(conn)
            return JSONResponse({'accounts': [lunchflow_store.account_view(conn, row) for row in conn.execute(
                'SELECT * FROM bank_connections WHERE provider_connection_id=?', (connection_id,))]}, headers=account_settings.HEADERS)
        except Exception as error:
            _response_error(error)
