import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import enrichment
from app.db import init_db


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'lookup.db'
        self.conn = init_db(self.path)
        self.conn.execute("INSERT INTO merchants(canonical_name,kind) VALUES('Example Coffee','merchant')")
        self.conn.execute("INSERT INTO merchants(canonical_name,kind) VALUES('Private Friend','person')")
        self.conn.execute("INSERT INTO merchants(canonical_name,kind) VALUES('Private Owner','self')")
        self.conn.commit()
        app = FastAPI()
        app.include_router(enrichment.router)
        self.client = TestClient(app)
        self.path_patch = patch.object(enrichment, 'DB_PATH', self.path)
        self.path_patch.start()
        self.env_patch = patch.dict('os.environ', {'FINANCE_WIKIDATA_CONTACT': ''})
        self.env_patch.start()
        self.candidate = {'id': 'Q1234', 'label': 'Example Coffee', 'description': 'British coffee shop chain', 'instance_of': ['Q11707']}

    def tearDown(self):
        self.env_patch.stop()
        self.path_patch.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_no_contact_uses_browser_fallback_for_saved_business_without_network(self):
        with patch.object(enrichment, '_fetch_matches') as remote:
            response = self.client.get('/api/merchant-lookup', params={'query': 'example coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['query'], 'Example Coffee')
        self.assertEqual(response.json()['status'], 'browser_lookup_required')
        remote.assert_not_called()

    def test_private_unknown_and_raw_statement_queries_fail_before_network(self):
        with patch.object(enrichment, '_fetch_matches') as remote:
            for query in ('Private Friend', 'Private Owner', 'Unclassified Person',
                          'TRANSFER TO Example Coffee', 'Example Coffee 12345678', 'friend@example.com'):
                with self.subTest(query=query):
                    self.assertEqual(self.client.get('/api/merchant-lookup', params={'query': query}).status_code, 422)
                    self.assertEqual(self.client.post('/api/merchant-lookup/cache', json={'query': query, 'matches': []}).status_code, 422)
        remote.assert_not_called()

    def test_browser_candidates_cache_safe_fields_and_do_not_apply_classification(self):
        candidate = {**self.candidate, 'source_url': 'javascript:alert(1)', 'amount': 10000}
        response = self.client.post('/api/merchant-lookup/cache', json={'query': 'Example Coffee', 'matches': [candidate]})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['matches'][0]['source_url'], 'https://www.wikidata.org/wiki/Q1234')
        self.assertEqual(result['matches'][0]['suggested_category'], 'Eating out')
        self.assertNotIn('amount', result['matches'][0])
        self.assertIsNone(self.conn.execute("SELECT category FROM merchants WHERE canonical_name='Example Coffee'").fetchone()[0])
        cached = self.client.get('/api/merchant-lookup', params={'query': 'Example Coffee'}).json()
        self.assertTrue(cached['cached'])
        self.assertFalse(cached['stale'])
        self.assertEqual(cached['matches'], result['matches'])

    def test_humans_unrelated_entities_and_unsafe_fields_are_removed(self):
        candidates = [
            {**self.candidate, 'instance_of': ['Q5']},
            {**self.candidate, 'id': 'Q222', 'description': 'British businessman and restaurant founder'},
            {**self.candidate, 'id': 'Q333', 'description': 'mountain in Scotland'},
            {**self.candidate, 'id': 'Q444', 'label': '<script>alert(1)</script>'},
            {**self.candidate, 'id': 'Q0'},
        ]
        response = self.client.post('/api/merchant-lookup/cache', json={'query': 'Example Coffee', 'matches': candidates})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'empty')
        self.assertEqual(response.json()['matches'], [])

    def test_cache_is_inaccessible_if_business_becomes_person(self):
        enrichment.cache_matches(self.conn, 'Example Coffee', [self.candidate])
        self.conn.execute("UPDATE merchants SET kind='person' WHERE canonical_name='Example Coffee'")
        self.conn.commit()
        response = self.client.get('/api/merchant-lookup', params={'query': 'Example Coffee'})
        self.assertEqual(response.status_code, 422)

    def test_candidate_count_and_lengths_are_bounded(self):
        for candidates in ([self.candidate] * 6, [{**self.candidate, 'label': 'x' * 161}]):
            response = self.client.post('/api/merchant-lookup/cache', json={'query': 'Example Coffee', 'matches': candidates})
            self.assertEqual(response.status_code, 422)

    def test_server_cache_hit_avoids_network_and_failure_retains_stale_data(self):
        with patch.dict('os.environ', {'FINANCE_WIKIDATA_CONTACT': 'https://example.org/contact'}), \
             patch.object(enrichment, '_fetch_matches', return_value=[self.candidate]) as remote:
            first = self.client.get('/api/merchant-lookup', params={'query': 'Example Coffee'}).json()
            second = self.client.get('/api/merchant-lookup', params={'query': 'Example Coffee'}).json()
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(first['status'], 'ok')
        self.assertTrue(second['cached'])
        old = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        self.conn.execute('UPDATE enrichment_cache SET fetched_at=?', (old,))
        self.conn.commit()
        with patch.dict('os.environ', {'FINANCE_WIKIDATA_CONTACT': 'https://example.org/contact'}), \
             patch.object(enrichment, '_fetch_matches', side_effect=enrichment.LookupFailure()):
            failed = self.client.get('/api/merchant-lookup', params={'query': 'Example Coffee'}).json()
        self.assertEqual(failed['status'], 'unavailable')
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['matches'], first['matches'])
        self.assertEqual(failed['fetched_at'], old)

    def test_remote_claims_remove_human_with_business_like_label(self):
        replies = [{'search': [{'id': 'Q10', 'label': 'Example Coffee'}, {'id': 'Q20', 'label': 'Example Coffee'}]},
                   {'entities': {'Q10': {'descriptions': {'en': {'value': 'coffee shop chain'}},
                                         'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q5'}}}}]}},
                                  'Q20': {'descriptions': {'en': {'value': 'coffee shop chain'}}, 'claims': {}}}}]
        with patch.object(enrichment, '_request_json', side_effect=replies):
            matches = enrichment._fetch_matches('Example Coffee', 'https://example.org/contact')
        self.assertEqual([r['id'] for r in matches], ['Q20'])

    def test_retry_after_is_respected_without_repeating_network_call(self):
        error = HTTPError(enrichment.API_URL, 429, 'Too Many Requests', {'Retry-After': '90'}, io.BytesIO())
        with patch.object(enrichment, '_last_request', 0.0), patch.object(enrichment, '_retry_until', 0.0), \
             patch.object(enrichment.time, 'sleep'), patch.object(enrichment, 'urlopen', side_effect=error) as remote:
            for _ in range(2):
                with self.assertRaises(enrichment.LookupFailure) as caught:
                    enrichment._request_json({'action': 'wbsearchentities'}, 'TestBot/1.0 (https://example.org/contact)')
                self.assertEqual(caught.exception.status, 'rate_limited')
                self.assertGreaterEqual(caught.exception.retry_after, 89)
        self.assertEqual(remote.call_count, 1)

    def test_request_has_identifying_agent_timeout_and_lag_bound(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return json.dumps({'search': []}).encode()
        with patch.object(enrichment, '_last_request', 0.0), patch.object(enrichment, '_retry_until', 0.0), \
             patch.object(enrichment, 'urlopen', return_value=Response()) as remote:
            enrichment._request_json({'action': 'wbsearchentities', 'search': 'Example Coffee'}, 'TestBot/1.0 (https://example.org/contact)')
        request = remote.call_args.args[0]
        self.assertIn('TestBot/1.0', request.get_header('User-agent'))
        self.assertIn('maxlag=5', request.full_url)
        self.assertEqual(remote.call_args.kwargs['timeout'], 6)


if __name__ == '__main__':
    unittest.main()
