"""Time banking requests without retaining URLs, identifiers or payloads."""
from time import perf_counter

from .banking_health import record_request


def operation_for(path):
    fixed = {
        '/api/banking': 'summary', '/api/banking/health': 'health',
        '/api/banking/sync': 'sync', '/api/analytics': 'analytics',
        '/api/spending-explorer': 'analytics', '/api/recurring': 'recurring',
        '/api/anomalies': 'anomalies', '/api/planning': 'planning',
        '/api/transfers': 'transfers', '/api/advice': 'advice',
    }
    if path in fixed:
        return fixed[path]
    if path.startswith('/api/banking/accounts/'):
        return 'mapping' if path.endswith('/mapping') else 'account_detail'
    if path.startswith('/api/banking/reviews/'):
        return 'review'
    for prefix, operation in (('/api/merchants', 'merchants'), ('/api/transactions', 'transactions')):
        if path == prefix or path.startswith(prefix + '/'):
            return operation
    return None


class BankingTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        operation = operation_for(scope.get('path', '')) if scope['type'] == 'http' else None
        if operation is None:
            return await self.app(scope, receive, send)
        started, status = perf_counter(), 500

        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)

        try:
            return await self.app(scope, receive, timed_send)
        finally:
            record_request(operation, status, (perf_counter() - started) * 1000)
