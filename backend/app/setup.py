"""One-time, token-authorized setup for explicitly initialized new installations.

The HTTP service never creates or replaces an authentication key, financial
database, or bootstrap authorization. An offline initializer supplies all three.
Existing installations without a bootstrap row can only enroll through the CLI.
"""
from __future__ import annotations

import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
import pyotp

from . import auth
from .db import DB_PATH
from .runtime import database_readiness

router = APIRouter(prefix='/api/setup', tags=['installation'])


class SetupToken(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class Enrollment(SetupToken):
    challenge_id: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=15, max_length=256)
    code: str = Field(min_length=6, max_length=6, pattern=r'^[0-9]{6}$')


def _ready():
    if not auth.enabled() or not auth.auth_path().is_file() or not auth.key_path().is_file():
        return False
    try:
        auth.cipher()
    except (OSError, ValueError):
        return False
    return database_readiness(DB_PATH)[1] == 200


@router.get('/status')
def status():
    # Do not create an auth DB as a side effect of probing a missing installation.
    if not auth.auth_path().is_file():
        return {'available': False, 'configured': False}
    with auth.database() as conn:
        configured = conn.execute('SELECT 1 FROM owner').fetchone() is not None
        setup = conn.execute('SELECT expires FROM installation_setup WHERE id=1').fetchone()
    return {'available': bool(not configured and setup and setup['expires'] > time.time() and _ready()),
            'configured': configured}


def _authorize(conn, token):
    """Called within a write transaction, including all failed-attempt updates."""
    now = time.time()
    setup = conn.execute('SELECT * FROM installation_setup WHERE id=1').fetchone()
    if conn.execute('SELECT 1 FROM owner').fetchone() or not setup or setup['expires'] <= now:
        raise HTTPException(404, 'Browser setup is unavailable. Use the installation administrator tools.')
    if setup['attempts'] >= 10 and setup['throttle_until'] > now:
        raise HTTPException(429, 'Too many setup attempts. Try again later.',
                            headers={'Retry-After': str(max(1, int(setup['throttle_until'] - now)))})
    if setup['throttle_until'] <= now:
        conn.execute('UPDATE installation_setup SET attempts=0, throttle_until=? WHERE id=1', (now + 900,))
    if not hmac.compare_digest(auth.digest(token), setup['token_digest']):
        _reject(conn)
    return setup


def _reject(conn):
    conn.execute('UPDATE installation_setup SET attempts=attempts+1 WHERE id=1')
    auth.audit(conn, 'setup_rejected')
    conn.commit()
    raise HTTPException(401, 'Setup credentials are invalid or expired.')


@router.post('/begin')
def begin(body: SetupToken):
    if not _ready():
        raise HTTPException(404, 'Browser setup is unavailable. Check installation readiness.')
    with auth.database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _authorize(conn, body.token)
        challenge = secrets.token_urlsafe(32)
        secret = pyotp.random_base32()
        until = time.time() + 600
        conn.execute('UPDATE installation_setup SET challenge_digest=?,secret=?,challenge_until=? WHERE id=1',
                     (auth.digest(challenge), auth.cipher().encrypt(secret.encode()), until))
        auth.audit(conn, 'setup_started')
        conn.commit()
    return {'challenge_id': challenge, 'secret': secret,
            'provisioning_uri': pyotp.TOTP(secret).provisioning_uri(name='Owner', issuer_name='Am I Broke?'),
            'expires_at': until}


@router.post('/enroll')
def enroll(body: Enrollment, response: Response):
    if not _ready():
        raise HTTPException(404, 'Browser setup is unavailable. Check installation readiness.')
    with auth.database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        setup = _authorize(conn, body.token)
        if (not setup['challenge_digest'] or not setup['secret'] or not setup['challenge_until']
                or setup['challenge_until'] <= time.time()
                or not hmac.compare_digest(auth.digest(body.challenge_id), setup['challenge_digest'])):
            _reject(conn)
        secret = auth.cipher().decrypt(setup['secret']).decode()
        step = int(time.time()) // 30
        if not pyotp.TOTP(secret).verify(body.code, for_time=step * 30, valid_window=0):
            _reject(conn)
        hashed = auth.password_hash(body.password)
        conn.execute('INSERT INTO owner VALUES(1,?,?,?)', (hashed, setup['secret'], step))
        codes = auth.recovery_codes(conn)
        conn.execute('DELETE FROM installation_setup')
        conn.execute('UPDATE throttle SET attempts=0,until=0 WHERE id=1')
        auth.create_session(conn, response)
        auth.audit(conn, 'owner_enrolled')
        conn.commit()
    return {'ok': True, 'recovery_codes': codes}
