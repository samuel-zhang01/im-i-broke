"""Enable Banking metadata preflight. No bank consent or ledger writes.

The provider's bank list does not certify individual credit-card coverage.
That must be checked against accounts returned after the owner's consent.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class ProviderError(ValueError):
    """An error safe to show without printing credentials or provider bodies."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


class EnableBanking:
    def __init__(self, application_id: str, private_key_path: Path):
        try:
            self.application_id = str(UUID(application_id))
        except (ValueError, AttributeError):
            raise ProviderError('The application ID must be a UUID.') from None
        try:
            self.key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError):
            raise ProviderError('Cannot read an unencrypted PEM private key from the configured file.') from None
        if not isinstance(self.key, rsa.RSAPrivateKey) or self.key.key_size < 2048:
            raise ProviderError('Enable Banking requires an RSA private key of at least 2048 bits.')
        # A host proxy can observe signed metadata requests. Keep this
        # credential-bearing transport independent of inherited proxy settings.
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def _jwt(self) -> str:
        now = int(time.time())
        header = {'typ': 'JWT', 'alg': 'RS256', 'kid': self.application_id}
        claims = {'iss': 'enablebanking.com', 'aud': 'api.enablebanking.com',
                  'iat': now, 'exp': now + 300}
        message = '.'.join(_encode(json.dumps(part, separators=(',', ':')).encode())
                           for part in (header, claims)).encode('ascii')
        signature = self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return message.decode('ascii') + '.' + _encode(signature)

    def metadata(self, resource: str) -> dict:
        # Never send the signed token to a caller-supplied host or follow a redirect.
        if resource not in {'application', 'aspsps'}:
            raise ProviderError('Only application and bank metadata are available in this preflight.')
        request = Request('https://api.enablebanking.com/' + resource,
                          headers={'Authorization': 'Bearer ' + self._jwt(), 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ProviderError('Provider metadata exceeds the response size limit.')
                result = json.loads(raw)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProviderError(f'Enable Banking returned HTTP {status}; check application activation and credentials.') from None
        except (URLError, TimeoutError, OSError):
            raise ProviderError('Enable Banking could not be reached securely. Try again later.') from None
        except (ValueError, UnicodeError):
            raise ProviderError('Enable Banking returned invalid metadata.') from None
        if not isinstance(result, dict):
            raise ProviderError('Enable Banking returned invalid metadata.')
        return result

    def check_hsbc(self) -> dict:
        application = self.metadata('application')
        response = self.metadata('aspsps')
        banks = response.get('aspsps')
        if not isinstance(banks, list) or not all(isinstance(bank, dict) for bank in banks):
            raise ProviderError('Enable Banking returned an invalid bank list.')
        matches = [bank for bank in banks if bank.get('country') == 'GB'
                   and 'HSBC' in str(bank.get('name', '')).upper()
                   and 'personal' in (bank.get('psu_types') or [])]
        blockers = []
        if application.get('environment') != 'PRODUCTION':
            blockers.append('This is not a production application; real HSBC accounts cannot be connected yet.')
        if application.get('active') is not True:
            blockers.append('Activate restricted production access by linking your own accounts in the provider portal.')
        if 'AIS' not in (application.get('services') or []):
            blockers.append('Account information access (AIS) is not enabled for this application.')
        if 'GB' not in (application.get('countries') or []):
            blockers.append('UK access is not listed for this application.')
        if not matches:
            blockers.append('No HSBC UK personal integration was returned for this application.')
        return {
            'provider': 'Enable Banking',
            'environment': application.get('environment'),
            'application_active': application.get('active') is True,
            'metadata_checks_passed': not blockers,
            'blockers': blockers,
            'banks': [{'name': bank['name'], 'country': bank['country'],
                       'maximum_consent_validity_seconds': bank.get('maximum_consent_validity'),
                       'beta': bank.get('beta')} for bank in matches],
            'credit_card_coverage': 'Unverified; check the accounts returned after HSBC consent.',
            'connected': False,
        }
