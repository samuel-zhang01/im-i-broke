"""Outbound public data requests use explicit HTTPS hosts and never redirect."""
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

ALLOWED_HOSTS = frozenset({'query1.finance.yahoo.com', 'api.frankfurter.dev', 'www.wikidata.org'})


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request, timeout=8):
    url = request.full_url if isinstance(request, Request) else request
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment):
        raise ValueError('Outbound URL is not an approved HTTPS data provider.')
    # Do not inherit an unexpected HTTP_PROXY from the host environment.
    return build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=timeout)
