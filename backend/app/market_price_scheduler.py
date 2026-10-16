"""Hourly public price-cache maintenance, independent of brokerage syncing."""
import asyncio
from contextlib import asynccontextmanager, suppress
import os

from . import db


def refresh_seconds():
    try:
        value = int(os.environ.get('INVESTMENT_PRICE_REFRESH_SECONDS', '900'))
    except ValueError:
        value = 900
    return 0 if value == 0 else max(900, value) if value > 0 else 900


async def _run():
    from .intraday_market import refresh_intraday
    await asyncio.sleep(15)
    while refresh_seconds():
        if db.DB_PATH.exists():
            try:
                # Reads saved holdings; sends only public listing symbols.
                # The adapter bounds requests and skips fresh cached listings.
                await asyncio.to_thread(refresh_intraday, path=db.DB_PATH)
            except Exception:
                pass  # Preserve the last cache; retry on the normal interval.
        await asyncio.sleep(refresh_seconds())


@asynccontextmanager
async def market_price_scheduler():
    task = asyncio.create_task(_run(), name='ledger-hourly-market-prices') if refresh_seconds() else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
