"""The price scheduler bounds provider polling and respects explicit disablement."""
import os
import unittest
from unittest.mock import patch

from app import market_price_scheduler as scheduler


class MarketPriceSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_default_and_invalid_intervals_use_fifteen_minute_refresh(self):
        with patch.dict(os.environ):
            os.environ.pop('INVESTMENT_PRICE_REFRESH_SECONDS',None)
            self.assertEqual(scheduler.refresh_seconds(),900)
            for value in ('invalid','-1'):
                with self.subTest(value=value):
                    os.environ['INVESTMENT_PRICE_REFRESH_SECONDS'] = value
                    self.assertEqual(scheduler.refresh_seconds(),900)

    def test_positive_intervals_cannot_poll_more_than_every_fifteen_minutes(self):
        for value,expected in (('1',900),('899',900),('900',900),('7200',7200)):
            with self.subTest(value=value), patch.dict(os.environ,{'INVESTMENT_PRICE_REFRESH_SECONDS':value}):
                self.assertEqual(scheduler.refresh_seconds(),expected)

    async def test_zero_disables_scheduling_without_starting_a_refresh_task(self):
        with patch.dict(os.environ,{'INVESTMENT_PRICE_REFRESH_SECONDS':'0'}), \
                patch.object(scheduler.asyncio,'create_task') as create_task, \
                patch('app.intraday_market.refresh_intraday') as refresh:
            self.assertEqual(scheduler.refresh_seconds(),0)
            async with scheduler.market_price_scheduler():
                create_task.assert_not_called()
        create_task.assert_not_called()
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
