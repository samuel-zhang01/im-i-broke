"""Optional consumer consent flows, isolated from the read-only provider adapters.

Only explicitly configured operator applications appear in discovery. Consent
returns to the SPA, which removes URL credentials and completes via a same-origin
authenticated POST; Strict cookies and existing CSRF boundaries stay intact.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
import uuid

from fastapi import APIRouter, HTTPException, Request as WebRequest
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from snaptrade_client import SnapTrade, SnapTradeAuth
from urllib3 import Timeout

from . import auth, db, lunchflow_client, product_connections as connections, snaptrade_client, snaptrade_models
from .network import NoRedirects

router = APIRouter(prefix='/api/connections/authorization', tags=['Provider authorization'],
                   route_class=connections.PrivateValidationRoute)
callback_router = APIRouter(include_in_schema=False)
METHODS = {'lunchflow': 'lunchflow_oauth', 'snaptrade': 'snaptrade_commercial'}
ERROR = 'The provider authorization could not be completed. Saved history is preserved; retry from Settings.'
# The bare documentation domain issues a 308. Use the canonical host directly;
# secret-bearing requests must never depend on following a redirect.
LF_ORIGIN = 'https://www.lunchflow.app'
LF_TOKEN_PATH = '/api/platform/oauth/token'
LF_READ_PATH = re.compile(r'/api/platform/v1/accounts(?:/[A-Za-z0-9_-]{1,160}/(?:transactions|balance))?\Z')


@callback_router.get('/connect/callback/lunchflow')
@callback_router.get('/connect/callback/snaptrade')
def callback_document():
    document = Path(__file__).resolve().parents[2] / 'frontend' / 'dist' / 'index.html'
    # Source deployments keep frontend beside backend. Distributed deployments
    # serve this route through nginx's SPA fallback instead.
    if not document.is_file():
        raise HTTPException(503, 'The frontend must be built before completing authorization.')
    return FileResponse(document, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS provider_authorizations (
        state_digest TEXT PRIMARY KEY, session_digest TEXT NOT NULL,
        provider TEXT NOT NULL, connection_id TEXT NOT NULL REFERENCES provider_connections(id),
        expires_at REAL NOT NULL, expected_version TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS provider_connection_subjects (
        connection_id TEXT PRIMARY KEY REFERENCES provider_connections(id),
        subject_digest TEXT NOT NULL, client_id TEXT NOT NULL)''')


def _text(value, *, maximum=16384):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(ERROR)
    return value


def _config(provider):
    if provider not in METHODS or not auth.enabled() or not auth.secure_cookie():
        raise ValueError('This provider does not have consumer authorization configured.')
    origin = os.environ.get('FINANCE_PUBLIC_ORIGIN', '').rstrip('/')
    parsed = urlsplit(origin)
    allowed = {host.strip().lower() for host in os.environ.get('FINANCE_ALLOWED_HOSTS', '').split(',')}
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.hostname.lower() not in allowed
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or parsed.port not in (None, 443)):
        raise ValueError('Consumer authorization requires an explicitly allowed HTTPS origin.')
    names = ('LUNCH_FLOW_CLIENT_ID', 'LUNCH_FLOW_CLIENT_SECRET') if provider == 'lunchflow' else ('SNAPTRADE_COMMERCIAL_ID', 'SNAPTRADE_COMMERCIAL_KEY')
    client_id, client_secret = [_text(os.environ.get(name, '').strip(), maximum=4096) for name in names]
    auth.cipher()  # Fail closed before requesting or accepting provider credentials.
    return {'client_id': client_id, 'client_secret': client_secret, 'origin': origin,
            'redirect_uri': origin + '/connect/callback/' + provider}


def available_methods(provider):
    try:
        _config(provider)
        return [METHODS[provider]]
    except Exception:
        return []


@router.get('/providers')
def providers():
    return {'providers': [{'provider': key, 'label': connections.PROVIDERS[key].label,
                            'auth_method': METHODS[key]} for key in METHODS if available_methods(key)]}


def _lf_request(method, path, *, payload=None, access_token=None, query=None):
    if not ((method == 'POST' and path == LF_TOKEN_PATH and access_token is None)
            or (method == 'GET' and LF_READ_PATH.fullmatch(path) and access_token and payload is None)):
        raise ValueError('This provider operation is not permitted.')
    headers = {'Accept': 'application/json', 'User-Agent': 'Ledger/1.0'}
    if access_token:
        headers['Authorization'] = 'Bearer ' + _text(access_token)
    body = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(payload).encode()
    request = Request(LF_ORIGIN + path + ('?' + urlencode(query) if query else ''),
                      data=body, method=method, headers=headers)
    try:
        with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=20) as response:
            limit = lunchflow_client.MAX_BYTES if method == 'GET' else 65536
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ValueError(ERROR)
            result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError(ERROR)
        return result
    except HTTPError as error:
        error.close()
        raise ValueError(ERROR) from None
    except Exception:
        raise ValueError(ERROR) from None


def platform_read(resource, account_id=None, *, date_from=None, date_to=None, access_token):
    if resource == 'accounts' and account_id is None:
        path = '/api/platform/v1/accounts'
    elif resource in {'transactions', 'balance'} and account_id is not None:
        path = '/api/platform/v1/accounts/' + lunchflow_client.identifier(account_id) + '/' + resource
    else:
        raise ValueError('This provider operation is not permitted.')
    query = {}
    if resource == 'transactions':
        query['include_pending'] = 'true'
        for field, value in (('from', date_from), ('to', date_to)):
            if value is not None:
                query[field] = date.fromisoformat(str(value)).isoformat()
    return _lf_request('GET', path, access_token=access_token, query=query)


def restrict_control_transport(request, expected_path):
    """A separate, narrow control client cannot place orders or touch accounts."""
    def permitted(method, url, *args, **kwargs):
        parsed = urlsplit(url)
        if (method.upper() != 'POST' or parsed.scheme != 'https' or parsed.hostname != 'api.snaptrade.com'
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or parsed.path not in {expected_path, '/api/v1' + expected_path}
                or expected_path not in {'/snapTrade/registerUser', '/snapTrade/login'}):
            raise ValueError('This provider operation is not permitted.')
        kwargs.update(redirect=False, retries=False, timeout=Timeout(connect=3, read=12, total=15))
        response = request(method, url, *args, **kwargs)
        if hasattr(response, 'data') and len(response.data) > 65536:
            raise ValueError(ERROR)
        return response
    return permitted


def _snaptrade_control(operation, config, **kwargs):
    paths = {'register': '/snapTrade/registerUser', 'portal': '/snapTrade/login'}
    if operation not in paths:
        raise ValueError('This provider operation is not permitted.')
    client = SnapTrade(auth=SnapTradeAuth.commercial_api_key(
        client_id=config['client_id'], consumer_key=config['client_secret']))
    pool = client.authentication.api_client.rest_client.pool_manager
    pool.request = restrict_control_transport(pool.request, paths[operation])
    try:
        method = client.authentication.register_snap_trade_user if operation == 'register' else client.authentication.login_snap_trade_user
        result = method(**kwargs).body
        if not isinstance(result, dict):
            result = dict(result)
        return result
    except Exception:
        raise ValueError(ERROR) from None
    finally:
        pool.clear()


def _tokens(body, config, *, previous=None):
    if body.get('token_type', 'Bearer').lower() != 'bearer':
        raise ValueError(ERROR)
    seconds = body.get('expires_in', 3600)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 60 <= seconds <= 86400:
        raise ValueError(ERROR)
    result = {'access_token': _text(body.get('access_token')), 'refresh_token': _text(body.get('refresh_token')),
              'user_id': _text(body.get('user_id') or (previous or {}).get('user_id'), maximum=200),
              'expires_at': time.time() + seconds, 'client_id': config['client_id']}
    if previous and previous['user_id'] != result['user_id']:
        raise ValueError('Authorization returned a different provider user. The existing connection was preserved.')
    return result


def _decode(row):
    if not row['secrets']:
        return None
    try:
        value = json.loads(auth.cipher().decrypt(row['secrets']).decode())
        if not isinstance(value, dict):
            raise ValueError(ERROR)
        return value
    except Exception:
        raise ValueError('Saved credentials could not be decrypted. Restore the matching installation key before reconnecting.') from None


def _save(conn, connection_id, values, *, ready=False):
    encrypted = auth.cipher().encrypt(json.dumps(values).encode())
    conn.execute('UPDATE provider_connections SET secrets=?,updated_at=?' +
                 (",enabled=1,status='ready',last_error=NULL,last_checked_at=?" if ready else '') + ' WHERE id=?',
                 (encrypted, connections.now(), *((connections.now(),) if ready else ()), connection_id))


def resolve_hosted_credentials(conn, metadata, values):
    """Called only after the adapter's durable lease serializes token rotation."""
    provider = metadata['provider']
    try:
        config = _config(provider)
        if values.get('client_id') != config['client_id']:
            raise ValueError('The operator application changed. Restore the matching application configuration.')
        if provider == 'snaptrade' and metadata['auth_method'] == METHODS[provider]:
            return {'auth_method': METHODS[provider], 'client_id': config['client_id'],
                    'consumer_key': config['client_secret'], 'user_id': _text(values.get('user_id'), maximum=200),
                    'user_secret': _text(values.get('user_secret'), maximum=4096)}
        if provider != 'lunchflow' or metadata['auth_method'] != METHODS[provider]:
            raise ValueError(ERROR)
        if not isinstance(values.get('expires_at'), (float, int)) or values['expires_at'] <= time.time() + 60:
            result = _lf_request('POST', LF_TOKEN_PATH, payload={'grant_type': 'refresh_token',
                'refresh_token': _text(values.get('refresh_token')), 'client_id': config['client_id'],
                'client_secret': config['client_secret']})
            values = _tokens(result, config, previous=values)
            with conn:
                _save(conn, metadata['id'], values)
        return {'access_token': _text(values.get('access_token'))}
    except Exception:
        with conn:
            conn.execute("UPDATE provider_connections SET status='error',last_error=?,last_checked_at=? WHERE id=?",
                         ('Provider authorization needs attention. Reconnect or check operator configuration; saved history is preserved.',
                          connections.now(), metadata['id']))
        raise ValueError('Provider authorization needs attention. Reconnect from Settings.') from None


def _session(request):
    token = request.cookies.get(auth.cookie_name())
    if not auth.enabled() or not auth.session_valid(token, touch=False):
        raise HTTPException(401, 'Sign in before connecting a provider.')
    return auth.digest(token)


def _open():
    if not db.DB_PATH.is_file():
        raise HTTPException(503, 'The workspace database is unavailable.')
    conn = db.connect(db.DB_PATH)
    connections.ensure_schema(conn)
    ensure_schema(conn)
    conn.commit()
    return conn


class StartInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    provider: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=100)
    connection_id: str | None = Field(default=None, max_length=100)
    broker_connection_id: str | None = Field(default=None, pattern=r'^[a-fA-F0-9-]{36}$')


class CompleteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(min_length=1, max_length=40)
    state: str = Field(min_length=32, max_length=128)
    code: str | None = Field(default=None, min_length=1, max_length=4096)


def _new_connection(conn, body):
    if conn.execute('SELECT COUNT(*) FROM provider_connections').fetchone()[0] >= 50:
        raise ValueError('This workspace supports up to 50 saved provider connections.')
    ident, stamp = 'connection:' + uuid.uuid4().hex, connections.now()
    conn.execute('''INSERT INTO provider_connections(id,provider,label,enabled,status,created_at,updated_at,auth_method)
        VALUES(?,?,?,0,'awaiting_authorization',?,?,?)''', (ident, body.provider, body.label, stamp, stamp, METHODS[body.provider]))
    return conn.execute('SELECT * FROM provider_connections WHERE id=?', (ident,)).fetchone()


@router.post('/start')
def start(body: StartInput, request: WebRequest):
    session = _session(request)
    try:
        config = _config(body.provider)
        with closing(_open()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (body.connection_id,)).fetchone() if body.connection_id else None
            if body.connection_id and (not row or row['provider'] != body.provider or row['auth_method'] != METHODS[body.provider]):
                raise ValueError('Choose an existing connection using this authorization method.')
            if row and body.provider == 'snaptrade' and not row['secrets'] and row['status'] == 'disconnected':
                # A removed SnapTrade user secret is unrecoverable. Never merge
                # a newly registered identity into an older connection's history.
                row = None
            if row is None:
                active = conn.execute('SELECT id FROM provider_connections WHERE provider=? AND auth_method=? AND status!=?',
                                      (body.provider, METHODS[body.provider], 'disconnected')).fetchone()
                if active:
                    raise ValueError('Use the existing consumer connection to connect or reconnect accounts.')
                row = _new_connection(conn, body)
            ident = row['id']
            conn.commit()
            owner = connections.acquire_job(conn, ident)
            try:
                current = conn.execute('SELECT * FROM provider_connections WHERE id=?', (ident,)).fetchone()
                if current['updated_at'] != row['updated_at']:
                    raise ValueError('This connection changed before authorization started.')
                row = current
                values = _decode(row)
                if values and values.get('client_id') != config['client_id']:
                    raise ValueError('The operator application changed. Restore its original configuration.')
                state, until = secrets.token_urlsafe(32), time.time() + 600
                if body.provider == 'lunchflow':
                    authorization_url = LF_ORIGIN + '/api/platform/oauth/authorize?' + urlencode({
                        'client_id': config['client_id'], 'redirect_uri': config['redirect_uri'], 'state': state})
                else:
                    if values is None:
                        user_id = 'ledger-' + uuid.uuid4().hex
                        registered = _snaptrade_control('register', config, user_id=user_id)
                        if registered.get('userId') != user_id:
                            raise ValueError(ERROR)
                        values = {'user_id': user_id, 'user_secret': _text(registered.get('userSecret'), maximum=4096),
                                  'client_id': config['client_id']}
                        with conn:
                            _save(conn, ident, values)
                    kwargs = {'user_id': values['user_id'], 'user_secret': values['user_secret'],
                              'custom_redirect': config['redirect_uri'] + '?' + urlencode({'state': state}),
                              'connection_type': 'read', 'immediate_redirect': True}
                    if body.broker_connection_id:
                        credentials = {'auth_method': METHODS['snaptrade'], 'client_id': config['client_id'],
                                       'consumer_key': config['client_secret'], 'user_id': values['user_id'], 'user_secret': values['user_secret']}
                        known = snaptrade_models.rows(snaptrade_client.call('connections', credentials=credentials))
                        if not any(item.get('id') == body.broker_connection_id and item.get('disabled') is True for item in known):
                            raise ValueError('The brokerage connection does not belong to this provider user.')
                        kwargs['reconnect'] = body.broker_connection_id
                    portal = _snaptrade_control('portal', config, **kwargs)
                    authorization_url = _text(portal.get('redirectURI'))
                    url = urlsplit(authorization_url)
                    if (url.scheme != 'https' or url.hostname != 'app.snaptrade.com' or url.port not in (None, 443)
                            or url.username or url.password or url.fragment or url.path != '/snapTrade/redeemToken'):
                        raise ValueError('The provider returned an unexpected authorization address.')
                    until = time.time() + 300
                with conn:
                    conn.execute('DELETE FROM provider_authorizations WHERE connection_id=? OR expires_at<=?', (ident, time.time()))
                    version = conn.execute('SELECT updated_at FROM provider_connections WHERE id=?', (ident,)).fetchone()[0]
                    conn.execute('INSERT INTO provider_authorizations VALUES(?,?,?,?,?,?)',
                                 (auth.digest(state), session, body.provider, ident, until, version))
                return {'authorization_url': authorization_url, 'expires_at': until, 'connection_id': ident}
            finally:
                with conn:
                    conn.execute('UPDATE provider_connection_jobs SET owner=NULL,expires_at=NULL WHERE connection_id=? AND owner=?', (ident, owner))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(409, ERROR) from None


@router.post('/complete')
def complete(body: CompleteInput, request: WebRequest):
    session = _session(request)
    try:
        config = _config(body.provider)
        with closing(_open()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            pending = conn.execute('SELECT * FROM provider_authorizations WHERE state_digest=?', (auth.digest(body.state),)).fetchone()
            if (not pending or pending['expires_at'] <= time.time() or pending['provider'] != body.provider
                    or not hmac.compare_digest(pending['session_digest'], session)):
                raise ValueError('Authorization expired or belongs to another session.')
            row = conn.execute('SELECT * FROM provider_connections WHERE id=?', (pending['connection_id'],)).fetchone()
            if row['updated_at'] != pending['expected_version']:
                raise ValueError('This connection changed after authorization started.')
            ident = row['id']
            connections._assert_idle(conn, ident)
            owner = uuid.uuid4().hex
            conn.execute('UPDATE provider_connection_jobs SET owner=?,expires_at=? WHERE connection_id=?',
                         (owner, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), ident))
            conn.execute('DELETE FROM provider_authorizations WHERE state_digest=?', (auth.digest(body.state),))
            conn.commit()  # Consume before network: ambiguous exchanges cannot be replayed.
            try:
                previous = _decode(row)
                if body.provider == 'lunchflow':
                    values = _tokens(_lf_request('POST', LF_TOKEN_PATH, payload={
                        'grant_type': 'authorization_code', 'code': _text(body.code, maximum=4096),
                        'redirect_uri': config['redirect_uri'], 'client_id': config['client_id'],
                        'client_secret': config['client_secret']}), config, previous=previous)
                    subject = conn.execute('SELECT * FROM provider_connection_subjects WHERE connection_id=?', (ident,)).fetchone()
                    if subject and (subject['client_id'] != config['client_id'] or not hmac.compare_digest(subject['subject_digest'], auth.digest(values['user_id']))):
                        raise ValueError('The authorization belongs to a different provider user.')
                    with conn:
                        _save(conn, ident, values, ready=True)
                        conn.execute('INSERT OR IGNORE INTO provider_connection_subjects VALUES(?,?,?)',
                                     (ident, auth.digest(values['user_id']), config['client_id']))
                else:
                    if not previous or previous.get('client_id') != config['client_id']:
                        raise ValueError(ERROR)
                    credentials = {'auth_method': METHODS['snaptrade'], 'client_id': config['client_id'],
                                   'consumer_key': config['client_secret'], 'user_id': previous['user_id'], 'user_secret': previous['user_secret']}
                    # Callback flags are untrusted; prove server-side data access.
                    connections.PROVIDERS['snaptrade'].validate(credentials)
                    with conn:
                        _save(conn, ident, previous, ready=True)
                return {'connection': connections.public_connection(conn, ident)}
            finally:
                with conn:
                    conn.execute('UPDATE provider_connection_jobs SET owner=NULL,expires_at=NULL,last_attempt_at=NULL WHERE connection_id=? AND owner=?', (ident, owner))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(409, ERROR) from None
