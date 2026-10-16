"""Household authentication; existing owner recovery remains terminal-only."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
import pyotp
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .db import DB_PATH

router = APIRouter(prefix='/api/auth', tags=['authentication'])
COOKIE = '__Host-ledger_session'

def cookie_name():
    name = COOKIE if secure_cookie() else 'ledger_session'
    # Cookies ignore ports. New isolated distributions already have a unique
    # project ID; namespace their sessions so localhost households can coexist.
    # Existing deployments without that ID retain their original cookie names.
    instance = os.environ.get('FINANCE_INSTANCE_ID', '')
    return name + '_' + hashlib.sha256(instance.encode()).hexdigest()[:16] if instance else name
ABSOLUTE_SECONDS = 8 * 3600
IDLE_SECONDS = 30 * 60
REMEMBER_ABSOLUTE_SECONDS = 30 * 24 * 3600
REMEMBER_IDLE_SECONDS = 7 * 24 * 3600
SCHEMA = '''
CREATE TABLE IF NOT EXISTS owner (
 id INTEGER PRIMARY KEY CHECK(id=1), password TEXT NOT NULL,
 secret BLOB NOT NULL, last_step INTEGER NOT NULL DEFAULT -1);
CREATE TABLE IF NOT EXISTS recovery (digest TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sessions (
 digest TEXT PRIMARY KEY, created REAL NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS remembered_sessions (
 digest TEXT PRIMARY KEY REFERENCES sessions(digest) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS throttle (
 id INTEGER PRIMARY KEY CHECK(id=1), attempts INTEGER NOT NULL, until REAL NOT NULL);
INSERT OR IGNORE INTO throttle VALUES(1,0,0);
CREATE TABLE IF NOT EXISTS security_events (
 id INTEGER PRIMARY KEY, occurred REAL NOT NULL, event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS installation_setup (
 id INTEGER PRIMARY KEY CHECK(id=1), token_digest TEXT NOT NULL,
 expires REAL NOT NULL, challenge_digest TEXT, secret BLOB, challenge_until REAL,
 attempts INTEGER NOT NULL DEFAULT 0, throttle_until REAL NOT NULL DEFAULT 0);
'''


def enabled():
    return os.environ.get('FINANCE_AUTH_ENABLED', 'true').lower() != 'false'


def secure_cookie():
    return os.environ.get('FINANCE_COOKIE_SECURE', 'true').lower() != 'false'


def auth_path():
    return Path(os.environ.get('FINANCE_AUTH_DB_PATH', str(DB_PATH.with_name('auth.db'))))


def key_path():
    return Path(os.environ.get('FINANCE_AUTH_KEY_PATH', str(auth_path().with_suffix('.key'))))


@contextmanager
def database():
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def cipher():
    # Never generate a replacement key when an existing installation loses it.
    return Fernet(key_path().read_bytes())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                            n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024)
    return salt + ':' + result.hex()


def verify_password(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def recovery_codes(conn):
    codes = [secrets.token_hex(12) for _ in range(10)]
    conn.execute('DELETE FROM recovery')
    conn.executemany('INSERT INTO recovery VALUES(?)', [(digest(code),) for code in codes])
    return codes


def audit(conn, event):
    # Fixed event labels only: never passwords, codes, cookies, IPs or financial data.
    conn.execute('INSERT INTO security_events(occurred,event) VALUES(?,?)', (time.time(), event))
    conn.execute('DELETE FROM security_events WHERE id NOT IN (SELECT id FROM security_events ORDER BY id DESC LIMIT 1000)')


def enroll(password, secret, code, *, reset=False):
    if not 15 <= len(password) <= 256:
        raise ValueError('Use a password between 15 and 256 characters.')
    step = int(time.time()) // 30
    if not pyotp.TOTP(secret).verify(code, for_time=step * 30, valid_window=0):
        raise ValueError('Authenticator code did not match. Check the clock and try again.')
    hashed = password_hash(password)
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing_owner = conn.execute('SELECT 1 FROM owner').fetchone() is not None
        if existing_owner and not reset:
            raise ValueError('Owner already enrolled. Use the explicit reset command.')
        key = key_path()
        if not key.exists():
            if existing_owner or conn.execute('SELECT 1 FROM installation_setup').fetchone():
                raise ValueError('Authentication encryption key is missing. Restore the matching key before enrolling or resetting the owner.')
            key.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(Fernet.generate_key())
        conn.execute('INSERT OR REPLACE INTO owner VALUES(1,?,?,?)',
                     (hashed, cipher().encrypt(secret.encode()), step))
        conn.execute('DELETE FROM sessions')
        conn.execute('DELETE FROM installation_setup')
        conn.execute('UPDATE throttle SET attempts=0, until=0')
        codes = recovery_codes(conn)
        audit(conn, 'owner_reset' if reset else 'owner_enrolled')
        conn.commit()
    return codes


def session_valid(token, *, touch=True):
    if not token or len(token) > 128:
        return False
    now = time.time()
    with database() as conn:
        row = conn.execute('SELECT s.*, r.digest IS NOT NULL AS remembered FROM sessions s '
                           'LEFT JOIN remembered_sessions r ON r.digest=s.digest WHERE s.digest=?',
                           (digest(token),)).fetchone()
        absolute = REMEMBER_ABSOLUTE_SECONDS if row and row['remembered'] else ABSOLUTE_SECONDS
        idle = REMEMBER_IDLE_SECONDS if row and row['remembered'] else IDLE_SECONDS
        if not row or row['created'] + absolute <= now or row['seen'] + idle <= now:
            return False
        if touch and now - row['seen'] >= 60:
            conn.execute('UPDATE sessions SET seen=? WHERE digest=?', (now, digest(token)))
            conn.commit()
        return True


class Credentials(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=64)


class LoginCredentials(Credentials):
    remember: bool = False


def create_session(conn, response, *, remember=False):
    """Keep devices independent, while bounding retained sessions to twenty."""
    now = time.time()
    conn.execute('DELETE FROM sessions WHERE created<=? OR seen<=?',
                 (now - REMEMBER_ABSOLUTE_SECONDS, now - REMEMBER_IDLE_SECONDS))
    conn.execute('DELETE FROM sessions WHERE digest IN '
                 '(SELECT digest FROM sessions ORDER BY seen DESC, created DESC LIMIT -1 OFFSET 19)')
    token = secrets.token_urlsafe(32)
    conn.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), now, now))
    if remember:
        conn.execute('INSERT INTO remembered_sessions VALUES(?)', (digest(token),))
    response.set_cookie(cookie_name(), token,
                        max_age=REMEMBER_ABSOLUTE_SECONDS if remember else ABSOLUTE_SECONDS,
                        httponly=True, secure=secure_cookie(), samesite='strict', path='/')
    return token


def authenticate(conn, credentials):
    """Must run inside BEGIN IMMEDIATE: serializes throttling and code consumption."""
    now = time.time()
    throttle = conn.execute('SELECT * FROM throttle WHERE id=1').fetchone()
    if throttle['until'] > now and throttle['attempts'] >= 5:
        raise HTTPException(429, 'Too many attempts. Wait 15 minutes before trying again.',
                            headers={'Retry-After': str(max(1, int(throttle['until'] - now)))})
    attempts = throttle['attempts'] if throttle['until'] > now else 0
    conn.execute('UPDATE throttle SET attempts=?, until=? WHERE id=1',
                 (attempts + 1, throttle['until'] if attempts else now + 900))
    owner = conn.execute('SELECT * FROM owner WHERE id=1').fetchone()
    valid = False
    if owner and verify_password(credentials.password, owner['password']):
        code = credentials.code.strip().replace('-', '').replace(' ', '')
        if len(code) == 6 and code.isascii() and code.isdigit():
            otp = pyotp.TOTP(cipher().decrypt(owner['secret']).decode())
            step = int(now) // 30
            for candidate in (step - 1, step, step + 1):
                if candidate > owner['last_step'] and hmac.compare_digest(otp.at(candidate * 30), code):
                    conn.execute('UPDATE owner SET last_step=? WHERE id=1', (candidate,))
                    valid = True
                    break
        else:
            valid = conn.execute('DELETE FROM recovery WHERE digest=?', (digest(code.lower()),)).rowcount == 1
    if not valid:
        audit(conn, 'login_rejected')
        conn.commit()  # Persist failed attempts even though the request fails.
        raise HTTPException(401, 'Password or code is incorrect, expired, or already used.')
    conn.execute('UPDATE throttle SET attempts=0, until=0 WHERE id=1')


@router.get('/status')
def status(request: Request):
    if not enabled():
        return {'enabled': False, 'configured': False, 'authenticated': True}
    with database() as conn:
        configured = conn.execute('SELECT 1 FROM owner').fetchone() is not None
    # Background status checks must not extend an idle session.
    return {'enabled': True, 'configured': configured,
            'authenticated': session_valid(request.cookies.get(cookie_name()), touch=False)}


@router.post('/login')
def login(credentials: LoginCredentials, response: Response):
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        authenticate(conn, credentials)
        create_session(conn, response, remember=credentials.remember)
        audit(conn, 'login_accepted')
        conn.commit()
    return {'ok': True}


@router.get('/sessions')
def list_sessions(request: Request):
    current = digest(request.cookies.get(cookie_name(), ''))
    now = time.time()
    with database() as conn:
        rows = conn.execute('SELECT s.*,r.digest IS NOT NULL AS remembered FROM sessions s '
                            'LEFT JOIN remembered_sessions r ON r.digest=s.digest ORDER BY s.seen DESC')
        return {'sessions': [{'id': row['digest'], 'created_at': row['created'],
                              'last_active_at': row['seen'], 'remembered': bool(row['remembered']),
                              'current': row['digest'] == current} for row in rows
                             if row['created'] + (REMEMBER_ABSOLUTE_SECONDS if row['remembered'] else ABSOLUTE_SECONDS) > now
                             and row['seen'] + (REMEMBER_IDLE_SECONDS if row['remembered'] else IDLE_SECONDS) > now]}


@router.post('/sessions/revoke-others')
def revoke_other_sessions(request: Request):
    with database() as conn:
        conn.execute('DELETE FROM sessions WHERE digest!=?', (digest(request.cookies.get(cookie_name(), '')),))
        audit(conn, 'other_sessions_revoked')
        conn.commit()
    return {'ok': True}


@router.post('/logout')
def logout(request: Request, response: Response):
    with database() as conn:
        conn.execute('DELETE FROM sessions WHERE digest=?', (digest(request.cookies.get(cookie_name(), '')),))
        audit(conn, 'signed_out')
        conn.commit()
    response.delete_cookie(cookie_name(), path='/', secure=secure_cookie(), httponly=True, samesite='strict')
    return {'ok': True}


@router.post('/recovery-codes')
def regenerate(credentials: Credentials):
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        authenticate(conn, credentials)
        codes = recovery_codes(conn)
        audit(conn, 'recovery_codes_replaced')
        conn.execute('DELETE FROM sessions')
        conn.commit()
    return {'codes': codes}


class AuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope['path']
        trusted_lan = os.environ.get('FINANCE_DEPLOYMENT') == 'trusted-lan'
        if enabled() or trusted_lan:
            # Keep CSRF protection in no-login LAN mode. The UI sends this header.
            if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('x-ledger-request') != '1':
                return await JSONResponse({'detail': 'Missing request protection header.'}, 403)(scope, receive, send)
        if trusted_lan and not enabled() and path.startswith('/api/auth/') and path != '/api/auth/status':
            return await JSONResponse({'detail': 'Account operations are disabled in trusted-LAN mode.'}, 404)(scope, receive, send)
        if enabled():
            public = {'/', '/index.html', '/favicon.ico', '/api/health', '/api/auth/status', '/api/auth/login',
                      '/api/setup/status', '/api/setup/begin', '/api/setup/enroll',
                      '/connect/callback/lunchflow', '/connect/callback/snaptrade'}
            if path not in public and not path.startswith('/assets/') and not await run_in_threadpool(session_valid, request.cookies.get(cookie_name()), touch=path != "/api/revision"):
                return await JSONResponse({'detail': 'Sign in to Am I Broke?'}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)
