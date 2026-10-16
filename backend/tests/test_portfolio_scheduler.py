"""Background snapshots are bounded, restart-aware and private; no network."""
import asyncio
from datetime import datetime, timezone
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.responses import JSONResponse

from app import portfolio_scheduler as scheduler


def refresh_response(*, status=200, retained=False, error=None, stale=False):
    return JSONResponse({'configured': True, 'cache': {'is_cached': retained,
                        'refresh_error': error, 'stale': stale}}, status_code=status)


class PortfolioScheduleTests(unittest.TestCase):
    def test_default_disabled_minimum_and_invalid_intervals(self):
        for setting, expected in ((None, 600), ('0', 0), ('1', 600), ('600', 600),
                                  ('3600', 3600), ('-1', 600), ('invalid', 600)):
            with self.subTest(setting=setting), patch.dict(os.environ, {}, clear=True):
                if setting is not None:
                    os.environ['SNAPTRADE_SYNC_SECONDS'] = setting
                self.assertEqual(scheduler.sync_seconds(), expected)

    def test_persisted_success_controls_schedule_and_invalid_time_does_not_block(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        cached = lambda when: {'cache': {'last_successful_refresh_at': when}}
        self.assertEqual(scheduler.seconds_until_due(cached('2026-09-18T11:30:00Z'), 3600, now), 1800)
        self.assertEqual(scheduler.seconds_until_due(cached('2026-09-18T10:00:00Z'), 3600, now), 0)
        self.assertEqual(scheduler.seconds_until_due(cached('2026-09-19T11:30:00Z'), 3600, now), 3600)
        self.assertEqual(scheduler.seconds_until_due(cached('not a timestamp'), 3600, now), 0)
        self.assertEqual(scheduler.seconds_until_due(None, 3600, now), 0)

    def test_persisted_failed_attempt_bounds_retry_after_restart(self):
        now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        cached = {'cache': {'last_successful_refresh_at': '2026-09-18T11:00:00Z',
                            'last_attempt_at': '2026-09-18T11:58:00Z'}}
        self.assertEqual(scheduler.seconds_until_due(cached, 600, now), 480)
        cached['cache']['last_attempt_at'] = 'invalid'
        self.assertEqual(scheduler.seconds_until_due(cached, 600, now), 0)
        cached['cache'].update(last_successful_refresh_at='invalid', last_attempt_at='2026-09-18T11:58:00')
        self.assertEqual(scheduler.seconds_until_due(cached, 600, now), 480)

    def test_callback_only_runs_for_newly_saved_snapshots_including_partial_sections(self):
        cases = [(refresh_response(), True), (refresh_response(stale=True), True),
                 (refresh_response(status=429), False), (refresh_response(status=502), False),
                 (refresh_response(retained=True, error='Saved data retained'), False),
                 (refresh_response(retained=True), False),
                 (refresh_response(error='Saved data retained'), False),
                 (JSONResponse({'configured': False, 'cache': None}), False),
                 (SimpleNamespace(status_code=200, body=b'invalid'), False)]
        for response, expected in cases:
            callback = Mock()
            with self.subTest(response=response), patch.object(scheduler, '_last_reference_refresh', None), \
                    patch.object(scheduler, 'refresh_portfolio', return_value=response):
                self.assertEqual(scheduler._refresh(callback), expected)
            self.assertEqual(callback.call_count, int(expected))

    def test_public_reference_callback_is_hourly_and_failure_does_not_advance_clock(self):
        clock = {'now': 100.0}
        callback = Mock()
        with patch.object(scheduler, '_last_reference_refresh', None), \
                patch.object(scheduler.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(scheduler, 'refresh_portfolio', return_value=refresh_response()) as refresh:
            self.assertTrue(scheduler._refresh(callback))
            self.assertEqual(callback.call_count, 1)
            clock['now'] = 700
            self.assertTrue(scheduler._refresh(callback))
            self.assertEqual(callback.call_count, 1)
            clock['now'] = 3700
            callback.side_effect = RuntimeError('synthetic-private-detail')
            self.assertTrue(scheduler._refresh(callback))
            self.assertEqual(scheduler._last_reference_refresh, 100)
            clock['now'] = 4300
            callback.side_effect = None
            self.assertTrue(scheduler._refresh(callback))
            self.assertEqual(callback.call_count, 3)
            self.assertEqual(scheduler._last_reference_refresh, 4300)
            clock['now'] = 7900
            refresh.return_value = refresh_response(retained=True, error='Unavailable')
            self.assertFalse(scheduler._refresh(callback))
            self.assertEqual(callback.call_count, 3)
            self.assertEqual(scheduler._last_reference_refresh, 4300)


class AsyncPortfolioScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def run_single_cycle(self, *, cached=None, remaining=0, failure=False):
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        async def in_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        callback = Mock()
        with patch.object(scheduler, 'configured', return_value=True), \
             patch.object(scheduler, 'sync_seconds', return_value=600), \
             patch.object(scheduler, '_last_reference_refresh', None), \
             patch.object(scheduler.time, 'monotonic', return_value=1000), \
             patch.object(scheduler, 'load_portfolio', return_value=cached) as load, \
             patch.object(scheduler, 'seconds_until_due', return_value=remaining), \
             patch.object(scheduler, 'refresh_portfolio', return_value=refresh_response(),
                          side_effect=RuntimeError('synthetic-private-detail') if failure else None) as refresh, \
             patch.object(scheduler.asyncio, 'sleep', side_effect=sleep), \
             patch.object(scheduler.asyncio, 'to_thread', side_effect=in_thread):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._run(callback)
        for call in load.call_args_list:
            self.assertEqual(call.kwargs, {'connection_scope': 'environment:snaptrade'})
        return sleeps, load, refresh, callback

    async def test_due_snapshot_is_recorded_after_startup_delay(self):
        sleeps, load, refresh, callback = await self.run_single_cycle()
        self.assertEqual(sleeps, [30, 600])
        self.assertEqual(load.call_count, 2)  # Re-read saved state before waiting.
        refresh.assert_called_once_with()
        callback.assert_called_once_with()

    async def test_recent_snapshot_skips_provider_and_waits_until_due(self):
        sleeps, load, refresh, callback = await self.run_single_cycle(remaining=1800)
        self.assertEqual(sleeps, [30, 1800])
        refresh.assert_not_called()
        callback.assert_not_called()

    async def test_failure_waits_full_interval_instead_of_retrying_continuously(self):
        sleeps, load, refresh, callback = await self.run_single_cycle(failure=True)
        self.assertEqual(sleeps, [30, 600])
        refresh.assert_called_once_with()
        callback.assert_not_called()

    async def test_waking_reloads_a_manual_refresh_instead_of_reading_provider_again(self):
        sleeps = []
        before = {'cache': {'last_successful_refresh_at': '2026-09-18T12:00:00Z'}}
        manual = {'cache': {'last_successful_refresh_at': '2026-09-18T12:08:20Z'}}

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3: raise asyncio.CancelledError

        async def in_thread(function, *args, **kwargs): return function(*args, **kwargs)

        with patch.object(scheduler, 'configured', return_value=True), \
                patch.object(scheduler, 'sync_seconds', return_value=600), \
                patch.object(scheduler, 'load_portfolio', side_effect=[before, manual]) as load, \
                patch.object(scheduler, 'seconds_until_due', side_effect=[300, 500]) as due, \
                patch.object(scheduler, 'refresh_portfolio') as refresh, \
                patch.object(scheduler.asyncio, 'sleep', side_effect=sleep), \
                patch.object(scheduler.asyncio, 'to_thread', side_effect=in_thread):
            with self.assertRaises(asyncio.CancelledError): await scheduler._run()
        self.assertEqual(sleeps, [30, 300, 500])
        self.assertEqual(load.call_count, 2)
        self.assertEqual([call.args[0] for call in due.call_args_list], [before, manual])
        self.assertTrue(all(call.kwargs == {'connection_scope': 'environment:snaptrade'} for call in load.call_args_list))
        refresh.assert_not_called()

    async def test_expensive_callback_time_does_not_add_to_the_next_broker_interval(self):
        sleeps, clock = [], {'now': 1000.0}
        saved = {'cache': {'last_successful_refresh_at': '2026-09-18T12:00:00Z'}}

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2: raise asyncio.CancelledError

        async def in_thread(function, *args, **kwargs): return function(*args, **kwargs)

        def callback(): clock['now'] += 200

        with patch.object(scheduler, 'configured', return_value=True), \
                patch.object(scheduler, 'sync_seconds', return_value=600), \
                patch.object(scheduler, '_last_reference_refresh', None), \
                patch.object(scheduler.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(scheduler, 'load_portfolio', side_effect=[None, saved]) as load, \
                patch.object(scheduler, 'seconds_until_due', side_effect=[0, 400]), \
                patch.object(scheduler, 'refresh_portfolio', return_value=refresh_response()) as refresh, \
                patch.object(scheduler.asyncio, 'sleep', side_effect=sleep), \
                patch.object(scheduler.asyncio, 'to_thread', side_effect=in_thread):
            with self.assertRaises(asyncio.CancelledError): await scheduler._run(callback)
        self.assertEqual(sleeps, [30, 400])
        self.assertEqual(load.call_count, 2)
        self.assertTrue(all(call.kwargs == {'connection_scope': 'environment:snaptrade'} for call in load.call_args_list))
        refresh.assert_called_once_with()

    async def test_retained_error_or_missing_cache_never_causes_a_busy_retry_loop(self):
        sleeps, clock = [], {'now': 1000.0}
        callback = Mock()

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3: raise asyncio.CancelledError
            clock['now'] += seconds

        async def in_thread(function, *args, **kwargs): return function(*args, **kwargs)

        with patch.object(scheduler, 'configured', return_value=True), \
                patch.object(scheduler, 'sync_seconds', return_value=600), \
                patch.object(scheduler.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(scheduler, 'load_portfolio', return_value=None) as load, \
                patch.object(scheduler, 'refresh_portfolio',
                             return_value=refresh_response(retained=True, error='Unavailable')) as refresh, \
                patch.object(scheduler.asyncio, 'sleep', side_effect=sleep), \
                patch.object(scheduler.asyncio, 'to_thread', side_effect=in_thread):
            with self.assertRaises(asyncio.CancelledError): await scheduler._run(callback)
        self.assertEqual(sleeps, [30, 600, 600])
        self.assertEqual(refresh.call_count, 2)
        self.assertTrue(all(call.kwargs == {'connection_scope': 'environment:snaptrade'} for call in load.call_args_list))
        callback.assert_not_called()

    async def test_disabled_and_unconfigured_lifespan_never_starts_task(self):
        for configured, interval in ((False, 600), (True, 0)):
            with self.subTest(configured=configured, interval=interval), \
                 patch.object(scheduler, 'configured', return_value=configured), \
                 patch.object(scheduler, 'sync_seconds', return_value=interval), \
                 patch.object(scheduler.asyncio, 'create_task') as create:
                async with scheduler.portfolio_scheduler():
                    pass
                create.assert_not_called()

    async def test_shutdown_cancels_schedule_without_waiting_for_inflight_provider(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def refresh():
            started.set()
            try:
                release.wait(3)
                return refresh_response()
            finally:
                finished.set()

        with patch.object(scheduler, 'configured', return_value=True), \
             patch.object(scheduler, 'sync_seconds', return_value=600), \
             patch.object(scheduler, 'STARTUP_DELAY_SECONDS', 0), \
             patch.object(scheduler, 'load_portfolio', return_value=None), \
             patch.object(scheduler, 'refresh_portfolio', side_effect=refresh):
            context = scheduler.portfolio_scheduler()
            await context.__aenter__()
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                await asyncio.wait_for(context.__aexit__(None, None, None), timeout=0.2)
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 1)


if __name__ == '__main__':
    unittest.main()
