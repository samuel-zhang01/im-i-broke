"""Generated customer deployments must have disjoint identities and safe defaults."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.provision_instance import provision
from scripts.verify_upgrade import verify
from scripts import verify_upgrade
from app.db import init_db
from app.lunchflow_store import ensure_schema


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = Path(__file__).resolve().parents[2] / 'deploy' / 'compose.product.yml'

    def tearDown(self):
        self.temp.cleanup()

    def create(self, name='alpha', port=20001, **changes):
        values = {'household': name, 'hostname': name + '.example.com', 'port': port,
                  'backend_image': 'ghcr.io/example/ledger-backend:v1.2.3',
                  'frontend_image': 'ghcr.io/example/ledger-frontend:v1.2.3', 'compose_source': self.source}
        values.update(changes)
        return provision(self.root / name, **values)

    def test_household_projects_networks_volumes_and_ports_are_separate(self):
        a = self.create()
        b = self.create('beta', 20002)
        self.assertNotEqual(a['instance_id'], b['instance_id'])
        self.assertNotEqual(a['private_port'], b['private_port'])
        for name, result in [('alpha', a), ('beta', b)]:
            root = self.root / name
            env = (root / '.env').read_text()
            self.assertIn('FINANCE_DEPLOYMENT=public\n', env)
            self.assertIn('FINANCE_COOKIE_SECURE=true\n', env)
            self.assertIn('FINANCE_BIND_ADDRESS=127.0.0.1\n', env)
            self.assertIn('FINANCE_INSTANCE_ID=' + result['instance_id'], env)
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / 'compose.yml').read_bytes(), self.source.read_bytes())
            self.assertEqual(json.loads((root / 'instance.json').read_text()), result)

    def test_refuses_existing_destinations_duplicate_ports_and_unsafe_settings(self):
        self.create()
        original = (self.root / 'alpha' / '.env').read_bytes()
        with self.assertRaises(ValueError): self.create()
        with self.assertRaises(ValueError): self.create('beta', 20001)
        with self.assertRaises(ValueError): self.create('beta', 20002, hostname='alpha.example.com')
        for changes in [{'hostname': 'https://unsafe.example/path'}, {'hostname': 'a\nEVIL=true'},
                        {'port': 80}, {'backend_image': 'repo:latest'},
                        {'frontend_image': 'repo:v1\nEVIL=true'},
                        {'frontend_image': 'repo:v2.0.0'}, {'household': '../../alpha'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create('beta', **{'port': 20002, **changes})
        self.assertEqual((self.root / 'alpha' / '.env').read_bytes(), original)
        self.assertFalse((self.root / 'beta').exists())

    def test_upgrade_rehearsal_preserves_source_and_user_decisions(self):
        source = self.root / 'source.db'
        conn = init_db(source)
        conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('a','Synthetic bank','current')")
        conn.execute("INSERT INTO transactions(account_id,amount,txn_date,dedupe_key,raw) VALUES('a',-12,'2024-01-01','keep','original source')")
        conn.execute("INSERT INTO overrides(dedupe_key,category,note) VALUES('keep','Housing','Keep this decision')")
        conn.execute("INSERT INTO rules(pattern,category) VALUES('synthetic','Housing')")
        ensure_schema(conn)
        conn.execute("INSERT INTO bank_connections(id,name,institution_name,raw,first_seen_at,last_seen_at) VALUES('remote','Synthetic account','Synthetic institution','{}','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO bank_transactions(connection_id,provider_id,txn_date,amount,currency,status,raw,first_seen_at,last_seen_at) VALUES('remote','remote-transaction','2026-01-01',-14,'EUR','unlinked','{}','2026-01-01','2026-01-01')")
        conn.commit()
        conn.close()
        original = source.read_bytes()
        result = verify(source, self.root)
        self.assertTrue(result['ok'])
        self.assertTrue(result['backup_restore_verified'])
        self.assertEqual(result['protected_rows']['transactions'], 1)
        self.assertEqual(result['protected_rows']['overrides'], 1)
        self.assertEqual(result['protected_rows']['bank_transactions'], 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.root.glob('ledger-upgrade-check-*')), [])

    def test_upgrade_rehearsal_rejects_native_currency_relabelling_without_changing_source(self):
        source = self.root / 'source.db'
        conn = init_db(source)
        conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('a','Synthetic','current')")
        conn.execute("INSERT INTO transactions(account_id,amount,currency,txn_date,dedupe_key,raw) VALUES('a',-12,'EUR','2026-01-01','native','original source')")
        conn.commit(); conn.close()
        original = source.read_bytes()
        migrate = verify_upgrade.apply_startup_migrations
        def corrupt_currency(connection):
            result = migrate(connection)
            connection.execute("UPDATE transactions SET currency='GBP'")
            connection.commit()
            return result
        with patch.object(verify_upgrade, 'apply_startup_migrations', side_effect=corrupt_currency):
            with self.assertRaisesRegex(ValueError, 'protected source records.*transactions'):
                verify(source, self.root)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.root.glob('ledger-upgrade-check-*')), [])


if __name__ == '__main__':
    unittest.main()
