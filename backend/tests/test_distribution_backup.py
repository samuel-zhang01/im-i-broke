"""Host backup orchestration tests; real age/Compose roundtrip is in smoke_distribution."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'scripts' / 'backup_distribution.py'
SPEC = importlib.util.spec_from_file_location('distribution_backup', SOURCE)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class DistributionBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.deployment = self.root / 'household'; self.deployment.mkdir()
        self.output = self.root / 'backups'
        (self.deployment / '.env').write_text('SECRET=synthetic-private-setting\n')
        (self.deployment / 'compose.yml').write_text('synthetic compose configuration\n')
        self.effective = {'name': 'ledger-synthetic', 'volumes': {'data': {'name': 'ledger-synthetic_data'}},
                          'services': {name: {'image': 'synthetic-' + name + ':v1', 'volumes': [{'type': 'volume'}]}
                                       for name in ('backend', 'frontend', 'admin')}}
        self.calls = []
        self.configuration = None
        self.running = True
        self.fail_encrypt = False

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout = b''
        if 'config' in command:
            stdout = json.dumps(self.effective).encode()
        elif 'ps' in command:
            stdout = b'synthetic-container' if self.running else b''
        elif 'age' in command and self.fail_encrypt:
            return SimpleNamespace(returncode=1, stdout=b'private-output', stderr=b'synthetic-private-setting')
        elif 'cat' in command:
            # Ciphertext-shaped stand-in only; actual encryption is separately
            # exercised against the built image, not mocked here.
            kwargs['stdout'].write(b'age-encryption.org/v1\nsynthetic-encrypted-member\n')
        if kwargs.get('input'):
            self.configuration = kwargs['input']
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b'')

    def create(self):
        with patch.object(backup.subprocess, 'run', side_effect=self.fake_run):
            return backup.backup(self.deployment, self.output, 'age1syntheticpublicrecipient', keep=2)

    def test_complete_set_is_atomic_encrypted_and_uses_explicit_configuration(self):
        with patch.dict(backup.os.environ, {'FINANCE_INSTANCE_ID': 'unrelated-live-project', 'COMPOSE_PROJECT_NAME': 'another-live-project'}):
            result = self.create()
        target = Path(result['backup_set'])
        self.assertTrue(result['ok'])
        self.assertIsNotNone(backup.completed_manifest(target, 'ledger-synthetic'))
        self.assertEqual({path.name for path in target.iterdir()}, {*backup.FILES, 'manifest.json'})
        self.assertIn(b'synthetic-private-setting', self.configuration)
        for path in target.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'synthetic-private-setting', path.read_bytes())
        for command, options in self.calls:
            self.assertEqual(command[command.index('--env-file') + 1], str(self.deployment / '.env'))
            self.assertNotIn('FINANCE_INSTANCE_ID', options['env'])
            self.assertNotIn('COMPOSE_PROJECT_NAME', options['env'])
        phases = [command for command, _ in self.calls]
        self.assertTrue(any('stop' in command for command in phases))
        self.assertTrue(any('start' in command for command in phases))
        self.assertEqual(list(self.output.glob('.partial-*')), [])

    def test_failure_resumes_backend_removes_partial_set_and_never_echoes_secret(self):
        self.fail_encrypt = True
        with self.assertRaises(RuntimeError) as failure:
            self.create()
        self.assertNotIn('synthetic-private-setting', str(failure.exception))
        self.assertEqual(list(self.output.glob('ledger-set-*')), [])
        self.assertEqual(list(self.output.glob('.partial-*')), [])
        self.assertTrue(any('start' in command for command, _ in self.calls))

    def test_offline_household_stays_offline_and_lock_rejects_concurrent_backup(self):
        self.running = False
        self.create()
        self.assertFalse(any('start' in command or 'stop' in command for command, _ in self.calls))
        with backup.lock(self.deployment, 'ledger-synthetic'):
            with self.assertRaisesRegex(ValueError, 'already running'):
                with patch.object(backup.subprocess, 'run', side_effect=self.fake_run):
                    backup.backup(self.deployment, self.root / 'different-backups', 'age1syntheticpublicrecipient')

    def test_retention_only_deletes_verified_completed_sets_for_this_project(self):
        result = self.create()
        current = Path(result['backup_set'])
        def clone(name, *, project='ledger-synthetic', corrupt=False):
            target = self.output / ('ledger-set-' + name)
            backup.shutil.copytree(current, target)
            manifest = json.loads((target / 'manifest.json').read_text())
            manifest['project'] = project; manifest['completed_at'] = 0
            (target / 'manifest.json').write_text(json.dumps(manifest))
            if corrupt: (target / 'key.age').write_bytes(b'broken')
            return target
        old = clone('old')
        foreign = clone('foreign', project='another-household')
        damaged = clone('damaged', corrupt=True)
        unrelated = self.output / 'unrelated'; unrelated.mkdir(); (unrelated / 'keep.txt').write_text('keep')
        self.assertEqual(backup.retain(self.output, 'ledger-synthetic', keep=1, current=current), 1)
        self.assertFalse(old.exists())
        for path in (current, foreign, damaged, unrelated): self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
