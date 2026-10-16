"""Bounded process-local investment responses; browsers/proxies must not cache.

Every lookup observes SQLite commits (including WAL, FX and source mappings).
Responses are immutable JSON bytes, scoped to database, inputs and UTC day.
Failed reads and computations crossing a database revision are never retained.
"""
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

from fastapi.responses import JSONResponse, Response

from .runtime import database_revision


class PrivateReadCache:
    def __init__(self, *, ttl=30, max_entries=96, max_bytes=8 * 1024 * 1024,
                 max_entry_bytes=1024 * 1024, clock=time.monotonic):
        self.ttl, self.max_entries = ttl, max_entries
        self.max_bytes, self.max_entry_bytes = max_bytes, max_entry_bytes
        self.clock = clock
        self._entries = OrderedDict()
        self._bytes = 0
        self._pending = {}
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def _revision(self, path):
        value, status = database_revision(Path(path))
        return value.get('revision') if status == 200 else None

    def _prune(self):
        now = self.clock()
        for key, (expires, body) in list(self._entries.items()):
            if expires <= now:
                self._entries.pop(key)
                self._bytes -= len(body)

    def read(self, path, scope, filters, produce):
        revision = self._revision(path)
        if revision is None:
            return JSONResponse(produce()).body, 'BYPASS'
        key = (str(Path(path).resolve()), revision,
               datetime.now(timezone.utc).date().isoformat(), scope, filters)
        with self._lock:
            self._prune()
            entry = self._entries.get(key)
            if entry:
                self._entries.move_to_end(key)
                return entry[1], 'HIT'
            pending = self._pending.get(key)
            owner = pending is None
            if owner and len(self._pending) < 32:
                pending = Future()
                self._pending[key] = pending
            elif owner:
                pending = None
        if not owner:
            try:
                body = pending.result(timeout=5)
                if self._revision(path) == revision:
                    return body, 'COALESCED'
            except TimeoutError:
                return JSONResponse(produce()).body, 'BYPASS'
            # A writer committed while the other request ran. Start a fresh
            # lookup; never re-label that old result as a current cache hit.
            return self.read(path, scope, filters, produce)
        try:
            body = JSONResponse(produce()).body
            unchanged = self._revision(path) == revision
            retained = unchanged and len(body) <= min(self.max_entry_bytes, self.max_bytes)
            if retained:
                with self._lock:
                    self._prune()
                    previous = self._entries.pop(key, None)
                    if previous:
                        self._bytes -= len(previous[1])
                    self._entries[key] = (self.clock() + self.ttl, body)
                    self._bytes += len(body)
                    while self._entries and (len(self._entries) > self.max_entries or self._bytes > self.max_bytes):
                        _, (_, expired) = self._entries.popitem(last=False)
                        self._bytes -= len(expired)
            if pending is not None:
                pending.set_result(body)
            return body, 'MISS' if retained else 'BYPASS'
        except BaseException as error:
            if pending is not None:
                pending.set_exception(error)
            raise
        finally:
            if pending is not None:
                with self._lock:
                    self._pending.pop(key, None)


cache = PrivateReadCache()


def cached_json(path, scope, filters, produce):
    started = time.perf_counter()
    body, status = cache.read(path, scope, filters, produce)
    elapsed = (time.perf_counter() - started) * 1000
    return Response(body, media_type='application/json', headers={
        'Cache-Control': 'no-store', 'Pragma': 'no-cache',
        'X-Ledger-Investment-Cache': status,
        'Server-Timing': f'investment_read;dur={elapsed:.2f}, investment_cache;desc="{status}"',
    })
