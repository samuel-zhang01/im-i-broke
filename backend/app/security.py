"""Request limits, deployment invariants, and browser response protections."""
import os
import ipaddress
import tempfile
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self' https://www.wikidata.org; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def validate_deployment():
    """Public mode cannot silently fall back to HTTP cookies or disabled auth."""
    from .auth import enabled, secure_cookie, auth_path, key_path
    deployment = os.environ.get('FINANCE_DEPLOYMENT', 'local')
    if deployment not in {'local', 'trusted-lan', 'public'}:
        raise RuntimeError('FINANCE_DEPLOYMENT must be local, trusted-lan, or public.')
    if deployment == 'trusted-lan':
        bind = ipaddress.ip_address(os.environ.get('FINANCE_BIND_ADDRESS', '127.0.0.1'))
        allowed = (ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128'),
                   ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                   ipaddress.ip_network('192.168.0.0/16'), ipaddress.ip_network('fc00::/7'))
        if not any(bind in network for network in allowed) or os.environ.get('FINANCE_PUBLIC_ORIGIN'):
            raise RuntimeError('Trusted-LAN mode requires a private/loopback bind address and no public origin.')
        return
    if deployment != 'public':
        return
    origin = urlsplit(os.environ.get('FINANCE_PUBLIC_ORIGIN', ''))
    hosts = os.environ.get('FINANCE_ALLOWED_HOSTS', '').split(',')
    if (not enabled() or not secure_cookie() or origin.scheme != 'https'
            or not origin.hostname or origin.hostname not in hosts
            or origin.username or origin.password or origin.path not in ('', '/')
            or origin.query or origin.fragment):
        raise RuntimeError('Public deployment requires authentication, Secure cookies, and an explicit allowed HTTPS origin.')
    if not os.environ.get('FINANCE_AUTH_KEY_PATH') or key_path().resolve().is_relative_to(auth_path().resolve().parent):
        raise RuntimeError('Public deployment requires a separate authentication key mount.')


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def protected_send(message):
            if message['type'] == 'http.response.start':
                values = {
                    b'cache-control': b'no-store',
                    b'x-content-type-options': b'nosniff', b'x-frame-options': b'DENY',
                    b'referrer-policy': b'no-referrer',
                    b'permissions-policy': b'camera=(), microphone=(), geolocation=()',
                    b'content-security-policy': CSP.encode(),
                }
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() not in values] + list(values.items())
            await send(message)
        await self.app(scope, receive, protected_send)


class RequestSizeMiddleware:
    """Bound streamed bodies before parsing, including no Content-Length.

    Placed inside authentication so anonymous request bodies are never buffered. The
    bounded spool moves to the container's size-limited /tmp tmpfs after 256 KiB.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope['path']
        maximum = 1024 * 1024
        if path.startswith(('/api/auth/', '/api/setup/')):
            maximum = 4096
        elif path.startswith('/api/connections/authorization/'):
            maximum = 8192
        headers = {k.lower(): v for k, v in scope['headers']}
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = -1
        if length < 0:
            return await JSONResponse({'detail': 'Invalid request length.'}, 400)(scope, receive, send)
        if length > maximum:
            return await JSONResponse({'detail': 'Request body exceeds the size limit.'}, 413)(scope, receive, send)
        if headers.get(b'content-encoding', b'identity').lower() != b'identity':
            return await JSONResponse({'detail': 'Compressed request bodies are not supported.'}, 415)(scope, receive, send)
        with tempfile.SpooledTemporaryFile(max_size=256 * 1024) as body:
            size = 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > maximum:
                    return await JSONResponse({'detail': 'Request body exceeds the size limit.'}, 413)(scope, receive, send)
                await run_in_threadpool(body.write, chunk)
                if not message.get('more_body', False):
                    break
            body.seek(0)
            delivered = 0
            finished = False
            async def replay():
                nonlocal delivered, finished
                if finished:
                    return await receive()
                chunk = await run_in_threadpool(body.read, 65536)
                delivered += len(chunk)
                finished = delivered >= size
                return {'type': 'http.request', 'body': chunk, 'more_body': delivered < size}
            await self.app(scope, replay, send)
