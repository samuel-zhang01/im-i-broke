"""Record private broker snapshots every ten minutes while Am I Broke? is running.

This reads SnapTrade's available data through the normal bounded refresh path;
it never requests a paid, forced brokerage sync. Financial responses and error
details are neither logged nor retained by the scheduler.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import json
import os
import time
from collections.abc import Callable

from .snaptrade_client import configured
from .snaptrade_routes import refresh_portfolio
from .snaptrade_store import load_portfolio


DEFAULT_SYNC_SECONDS = 10 * 60
MIN_SYNC_SECONDS = 10 * 60
STARTUP_DELAY_SECONDS = 30
REFERENCE_REFRESH_SECONDS = 60 * 60
_last_reference_refresh = None


def sync_seconds():
    """Zero disables scheduling; positive intervals have a ten-minute floor."""
    try:
        value = int(os.environ.get('SNAPTRADE_SYNC_SECONDS', str(DEFAULT_SYNC_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_SYNC_SECONDS
    if value == 0:
        return 0
    if value < 0:
        return DEFAULT_SYNC_SECONDS
    return max(value, MIN_SYNC_SECONDS)


def seconds_until_due(cached, interval, now=None):
    """The latest persisted success or attempt also bounds retries on restart."""
    current = now or datetime.now(timezone.utc)
    dates = []
    for key in ('last_successful_refresh_at', 'last_attempt_at'):
        try:
            previous = datetime.fromisoformat(cached['cache'][key].replace('Z', '+00:00'))
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            dates.append(previous)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    if not dates:
        return 0.0
    elapsed = max(0.0, (current - max(dates)).total_seconds())
    return max(0.0, interval - elapsed)


def _refresh(after_refresh):
    global _last_reference_refresh
    response = refresh_portfolio()
    # Failed reads intentionally return a retained portfolio with HTTP 200.
    # Only a newly persisted broker snapshot is a successful refresh here.
    try:
        payload = json.loads(response.body)
        cache = payload['cache']
        successful = (response.status_code == 200 and payload.get('configured') is True
                      and (cache.get('environment_refresh_succeeded') is True
                           or (cache.get('is_cached') is False and not cache.get('refresh_error'))))
    except (AttributeError, KeyError, TypeError, ValueError):
        successful = False
    if not successful:
        return False
    # FX and full public price/history reads remain hourly even though broker
    # observations are more frequent. Failed callbacks may retry next cycle.
    if (after_refresh is not None and (_last_reference_refresh is None
            or time.monotonic() - _last_reference_refresh >= REFERENCE_REFRESH_SECONDS)):
        try:
            after_refresh()
        except Exception:
            pass
        else:
            _last_reference_refresh = time.monotonic()
    return True


async def _run(after_refresh=None):
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    next_attempt = 0.0
    while True:
        interval = sync_seconds()
        if not interval or not configured():
            return
        try:
            cached = await asyncio.to_thread(load_portfolio, connection_scope='environment:snaptrade')
            remaining = max(seconds_until_due(cached, interval), next_attempt - time.monotonic())
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            # Shared route lock and cooldown also protect against a manual
            # browser refresh coinciding with this scheduled read.
            next_attempt = time.monotonic() + interval
            await asyncio.to_thread(_refresh, after_refresh)
        except Exception:
            # A provider or disk failure must not become a retry storm or put
            # sensitive exception text in logs. The route saves safe failures.
            await asyncio.sleep(max(0.0, next_attempt - time.monotonic()) or interval)
        # Re-read the persisted receipt after both work and sleeps. This also
        # notices manual refreshes and subtracts callback time from the wait,
        # rather than appending a full interval after an expensive callback.


@asynccontextmanager
async def portfolio_scheduler(after_refresh: Callable[[], None] | None = None):
    """Attach to application lifespan without delaying startup or shutdown.

    Cancellation stops future scheduled work promptly. A bounded provider read
    already running in a worker may finish and save its snapshot normally.
    """
    task = None
    if configured() and sync_seconds():
        task = asyncio.create_task(_run(after_refresh), name='ledger-portfolio-snapshots')
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
