"""Persisted six-hour bank polling. Reads provider cache, not forced bank refresh."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from .lunchflow_client import configured

DEFAULT_SYNC_SECONDS = 6 * 60 * 60
MIN_SYNC_SECONDS = 10 * 60
STARTUP_DELAY_SECONDS = 45


def sync_seconds():
    try:
        value = int(os.environ.get('LUNCH_FLOW_SYNC_SECONDS',str(DEFAULT_SYNC_SECONDS)))
    except (TypeError,ValueError):
        return DEFAULT_SYNC_SECONDS
    return 0 if value == 0 else (DEFAULT_SYNC_SECONDS if value < 0 else max(MIN_SYNC_SECONDS,value))


def retry_interval(state, interval):
    # Retry transient provider failures in 15 minutes, including after restart.
    # Credential/consent errors require owner action, not more requests.
    error = state.get('error') or ''
    transient = any(message in error for message in (
        'temporarily unavailable', 'could not be reached securely',
        'limiting requests', 'sync time budget was reached',
    ))
    return min(interval, 15 * 60) if transient else interval


def seconds_until_due(state,interval,current=None):
    interval = retry_interval(state, interval)
    # Attempts are persisted too, so a failing provider does not get hammered
    # every time the app/container restarts.
    try:
        previous = datetime.fromisoformat(state['last_attempt_at'].replace('Z','+00:00'))
        elapsed = max(0,((current or datetime.now(timezone.utc))-previous).total_seconds())
    except (ValueError,TypeError,KeyError,AttributeError):
        return 0
    return max(0,interval-elapsed)


def _state():
    from .lunchflow_store import database,sync_state
    conn = database()
    try:
        return sync_state(conn,provider_connection_id='environment:lunchflow')
    finally:
        conn.close()


async def _run(after_refresh=None):
    from .lunchflow_routes import run_sync
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while configured() and sync_seconds():
        interval = sync_seconds()
        try:
            remaining = seconds_until_due(await asyncio.to_thread(_state),interval)
            if remaining:
                # Re-read persisted state so a manual attempt can change the due time.
                await asyncio.sleep(min(remaining, 60))
                continue
            await asyncio.to_thread(run_sync)
            if after_refresh:
                await asyncio.to_thread(after_refresh)
        except Exception:
            pass
        await asyncio.sleep(min(interval, 60))


@asynccontextmanager
async def banking_scheduler(after_refresh=None):
    task = asyncio.create_task(_run(after_refresh),name='ledger-bank-sync') if configured() and sync_seconds() else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
