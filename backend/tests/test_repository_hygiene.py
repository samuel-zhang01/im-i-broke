"""Publication checks operate on disposable Git repositories, never household data."""
import importlib.util
import json
import os
import secrets
import string
from pathlib import Path
import subprocess
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    'repository_hygiene', Path(__file__).resolve().parents[2] / 'scripts/check_repository.py')
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)


class RepositoryHygieneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)

    def add(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        subprocess.run(['git', 'add', '-f', '--', name], cwd=self.root, check=True)

    def test_examples_and_nested_navigation_pass_with_no_credentials_required(self):
        self.add('.env.example', 'EXAMPLE_KEY=\n')
        self.add('deploy/product.env.example', 'INSTANCE=example\n')
        self.add('README.md', '# Start\n[Guide](docs/guide.md#an-example)\n<img src="icon.svg">\n')
        self.add('icon.svg', '<svg/>')
        self.add('docs/guide.md', '# An example\n[Home](../README.md#start)\n')
        self.assertEqual(hygiene.check_repository(self.root)[1], [])

    def test_force_added_private_files_are_rejected_without_content_in_output(self):
        paths = ['.env', '.env.production', 'deploy/customer.env', 'backend/data/finance.db',
                 'auth.key', 'Data/statement.csv', 'docs/archive/handoff.md',
                 'deploy/compose.release.yml', 'deploy/lan-access.conf', 'history.bundle', 'backup.tar.gz']
        paths += ['exports/transactions.csv', 'docs/ANNUAL.PDF', 'bank.qfx', 'records.xlsx']
        for name in paths:
            self.add(name, 'DO-NOT-PRINT-PRIVATE-PAYLOAD')
        _, issues = hygiene.check_repository(self.root)
        self.assertEqual(len(issues), len(paths))
        self.assertNotIn('DO-NOT-PRINT-PRIVATE-PAYLOAD', '\n'.join(issues))

    def test_ignored_untracked_private_files_are_never_inspected(self):
        self.add('README.md', '# Hello\n')
        (self.root / '.env').write_text('DO-NOT-READ')
        count, issues = hygiene.check_repository(self.root)
        self.assertEqual((count, issues), (1, []))

    def test_only_the_reviewed_synthetic_csv_can_be_tracked(self):
        self.add('examples/transactions.csv', 'Date,Description,Amount,ID\n')
        self.add('examples/another.csv', 'DO-NOT-PRINT-PRIVATE-PAYLOAD')
        count, issues = hygiene.check_repository(self.root)
        self.assertEqual(count, 1)
        self.assertEqual(issues, ['examples/another.csv: private artifact must not be tracked'])

    def test_deleted_guides_bad_anchors_and_escaping_links_fail(self):
        self.add('README.md', '[Old](old.md) [Bad](guide.md#gone) [Escape](../private.md)\n')
        self.add('guide.md', '# Present\n')
        self.assertEqual(len(hygiene.check_repository(self.root)[1]), 3)

    def test_symbols_and_local_machine_paths_fail(self):
        self.add('README.md', '# Example\n')
        # Build the sample path at runtime so the fixture itself stays generic.
        self.add('guide.md', '/home/' + 'synthetic-owner/private/file\n')
        (self.root / 'link').symlink_to('/dev/null')
        subprocess.run(['git', 'add', 'link'], cwd=self.root, check=True)
        self.assertEqual(len(hygiene.check_repository(self.root)[1]), 2)

    @unittest.skipUnless(os.environ.get('GITLEAKS_BINARY'), 'Set GITLEAKS_BINARY for the scanner integration check')
    def test_scanner_exception_does_not_hide_credentials_in_documentation(self):
        scanner = os.environ['GITLEAKS_BINARY']
        config = Path(__file__).resolve().parents[2] / '.gitleaks.toml'
        filename = '-'.join(('owner', 'key', '2026', '09', '21')) + '.age'
        self.add('docs/BACKUP.md', f'docker compose run --rm -T admin cat /app/backups/{filename} > {filename}\n')
        document = self.root / 'docs/BACKUP.md'
        def scan():
            report = self.root / 'redacted-report.json'
            result = subprocess.run([scanner, 'dir', '--redact=100', '--config', str(config),
                                     '--report-format', 'json', '--report-path', str(report),
                                     str(document.parent)], capture_output=True)
            return result.returncode, json.loads(report.read_text())
        code, findings = scan()
        self.assertEqual((code, findings), (0, []))
        # A generated, unissued token with a recognizable format, never printed.
        fake = 'ghp_' + ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
        document.write_text(document.read_text() + 'token = "' + fake + '"\n')
        code, findings = scan()
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1)
        self.assertNotIn(fake, json.dumps(findings))


if __name__ == '__main__':
    unittest.main()
