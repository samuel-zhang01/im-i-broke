import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.bank_provider import EnableBanking, NoRedirects, ProviderError


class BankProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.path = Path(self.tmp.name) / 'key.pem'
        self.path.write_bytes(self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        self.client = EnableBanking('550e8400-e29b-41d4-a716-446655440000', self.path)
        self.application = {'active': True, 'environment': 'PRODUCTION',
                            'countries': ['GB'], 'services': ['AIS']}
        self.bank = {'name': 'HSBC', 'country': 'GB', 'psu_types': ['personal']}

    def report(self, application=None, banks=None):
        self.client.metadata = Mock(side_effect=[
            self.application if application is None else application,
            {'aspsps': [self.bank] if banks is None else banks}])
        return self.client.check_hsbc()

    def test_signature_and_short_lived_provider_claims(self):
        with patch('app.bank_provider.time.time', return_value=1000):
            token = self.client._jwt()
        header, claims, signature = token.split('.')
        decode = lambda value: base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        self.key.public_key().verify(decode(signature), (header + '.' + claims).encode(),
                                     padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(json.loads(decode(header))['kid'], self.client.application_id)
        self.assertEqual(json.loads(decode(claims)), {
            'iss': 'enablebanking.com', 'aud': 'api.enablebanking.com', 'iat': 1000, 'exp': 1300})

    def test_ready_metadata_does_not_claim_connection_or_card_coverage(self):
        report = self.report()
        self.assertTrue(report['metadata_checks_passed'])
        self.assertFalse(report['connected'])
        self.assertIn('Unverified', report['credit_card_coverage'])
        self.assertEqual(report['banks'][0]['name'], 'HSBC')

    def test_sandbox_inactive_or_missing_permissions_are_blocked(self):
        for replacement in ({'environment': 'SANDBOX'}, {'active': False},
                            {'services': ['PIS']}, {'countries': ['FI']}):
            with self.subTest(replacement=replacement):
                report = self.report({**self.application, **replacement})
                self.assertFalse(report['metadata_checks_passed'])
                self.assertTrue(report['blockers'])

    def test_business_foreign_or_other_banks_do_not_establish_personal_coverage(self):
        report = self.report(banks=[{**self.bank, 'psu_types': ['business']},
                                   {**self.bank, 'country': 'FR'},
                                   {**self.bank, 'name': 'Other'}])
        self.assertFalse(report['metadata_checks_passed'])
        self.assertEqual(report['banks'], [])

    def test_invalid_metadata_does_not_establish_coverage(self):
        with self.assertRaises(ProviderError):
            self.report(banks=['bad payload'])

    def test_transport_never_sends_tokens_to_custom_url_or_follows_redirects(self):
        with patch('app.bank_provider.build_opener') as build:
            EnableBanking(self.client.application_id, self.path)
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[0], ProxyHandler)
        self.client.opener = Mock()
        with self.assertRaises(ProviderError):
            self.client.metadata('https://unrelated.example/')
        self.client.opener.open.assert_not_called()
        request = Request('https://api.enablebanking.com/application')
        self.assertIsNone(NoRedirects().redirect_request(
            request, None, 302, 'Moved', {}, 'https://unrelated.example/'))

    def test_provider_errors_do_not_echo_sensitive_bodies(self):
        self.client.opener = Mock()
        self.client.opener.open.side_effect = HTTPError(
            'https://api.enablebanking.com/application', 401, 'sensitive value', {},
            io.BytesIO(b'{"detail":"sensitive value"}'))
        with self.assertRaises(ProviderError) as error:
            self.client.metadata('application')
        self.assertIn('401', str(error.exception))
        self.assertNotIn('sensitive value', str(error.exception))

    def test_missing_credentials_fail_without_network(self):
        with self.assertRaises(ProviderError):
            EnableBanking('invalid', self.path)
        with self.assertRaises(ProviderError):
            EnableBanking(self.client.application_id, Path(self.tmp.name) / 'missing.pem')


if __name__ == '__main__':
    unittest.main()
