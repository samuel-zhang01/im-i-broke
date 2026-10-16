"""Exercise built images using a random disposable Compose project and volumes.

Never reads the repository .env or uses an existing deployment name. No provider
credentials are passed and all provider refresh intervals are disabled. Cleanup
is restricted to this process's generated project, including its test volumes.
"""
from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tarfile
import tempfile
import time
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler
import uuid

import pyotp
from backup_distribution import backup, completed_manifest


def smoke(backend_image, frontend_image):
    repository = Path(__file__).resolve().parent.parent
    project = 'ledger-smoke-' + uuid.uuid4().hex[:16]
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    settings = {'FINANCE_INSTANCE_ID': project,
        'FINANCE_BACKEND_IMAGE': backend_image, 'FINANCE_FRONTEND_IMAGE': frontend_image,
        'FINANCE_BIND_ADDRESS': '127.0.0.1', 'FINANCE_PORT': str(port), 'FINANCE_DEPLOYMENT': 'local',
        'FINANCE_COOKIE_SECURE': 'false', 'FINANCE_PUBLIC_ORIGIN': '',
        'FINANCE_ALLOWED_HOSTS': '127.0.0.1,localhost,backend',
        'SNAPTRADE_ID': '', 'SNAPTRADE_KEY': '', 'LUNCH_FLOW_API': '',
        'LUNCH_FLOW_CLIENT_ID': '', 'LUNCH_FLOW_CLIENT_SECRET': '',
        'SNAPTRADE_COMMERCIAL_ID': '', 'SNAPTRADE_COMMERCIAL_KEY': '',
        'SNAPTRADE_SYNC_SECONDS': '0', 'LUNCH_FLOW_SYNC_SECONDS': '0',
        'INVESTMENT_PRICE_REFRESH_SECONDS': '0'}
    environment = {**os.environ, **settings}
    base = ['docker', 'compose', '--env-file', '/dev/null', '--profile', 'maintenance', '-p', project,
            '-f', str(repository / 'deploy' / 'compose.product.yml')]

    def command(*args):
        result = subprocess.run([*base, *args], cwd=repository, env=environment,
                                capture_output=True, text=True, timeout=180)
        if result.returncode:
            # Token retrieval output, callback credentials and passwords must
            # never be copied into failure logs.
            raise RuntimeError('Disposable distribution command failed: ' + args[0] + '\n' + result.stderr[-4000:])
        return result.stdout.strip()

    configured = json.loads(command('config', '--format', 'json'))
    if any(volume.get('external') or not volume.get('name', '').startswith(project + '_')
           for volume in configured['volumes'].values()):
        raise RuntimeError('Refusing a smoke configuration containing existing or external volumes.')
    if any(volume['type'] != 'volume' for service in configured['services'].values() for volume in service.get('volumes', [])):
        raise RuntimeError('Refusing a smoke configuration with host filesystem mounts.')
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    origin = 'http://127.0.0.1:' + str(port)

    def api(path, body=None, *, method=None):
        headers = {'X-Ledger-Request': '1', 'Origin': origin}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(origin + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    print(json.dumps({'project': project, 'private_port': port, 'phase': 'initializing synthetic storage'}), flush=True)
    try:
        command('run', '--rm', 'admin', 'python', '-m', 'scripts.installation', 'init')
        token = command('run', '--rm', 'admin', 'python', '-m', 'scripts.installation', 'token')
        command('up', '-d', '--wait', '--wait-timeout', '120', 'backend', 'frontend')
        assert api('/api/health')['ok']
        assert api('/api/setup/status') == {'available': True, 'configured': False}
        challenge = api('/api/setup/begin', {'token': token})
        remaining = 30 - time.time() % 30
        if remaining < 2:
            time.sleep(remaining + 0.1)
        result = api('/api/setup/enroll', {'token': token, 'challenge_id': challenge['challenge_id'],
            'password': 'Synthetic distribution smoke password ' + uuid.uuid4().hex,
            'code': pyotp.TOTP(challenge['secret']).now()})
        assert result['ok'] and len(result['recovery_codes']) == 10
        assert api('/api/auth/status')['authenticated']
        assert api('/api/setup/status') == {'available': False, 'configured': True}
        assert api('/api/workspace/preferences')['reporting_currency'] == 'GBP'
        assert api('/api/summary')['n_transactions'] == 0
        assert api('/api/connections/authorization/providers')['providers'] == []
        # Exercise the actual HTTP app with financial records before recovery.
        # Distinct source IDs retain two otherwise identical real-world purchases.
        account = api('/api/workspace/accounts', {'display_name': 'Synthetic joint cash', 'currency': 'GBP'})
        account_path = '/api/workspace/accounts/' + account['id']
        changes = {'ownership_share': 0.5}
        assert not api(account_path + '/preview', changes)['blockers']
        api(account_path, changes, method='PATCH')
        api(account_path + '/balances', {'amount': 100, 'currency': 'GBP', 'as_of': '2026-08-31'})
        csv = {'account_id': account['id'], 'source_name': 'Synthetic bank export', 'source_filename': 'august.csv',
            'csv_text': 'Date,Description,Amount,ID\n2026-08-10,Synthetic coffee,-4.50,one\n2026-08-10,Synthetic coffee,-4.50,two\n',
            'mapping': {'date_column': 'Date', 'description_column': 'Description', 'amount_column': 'Amount', 'id_column': 'ID'}}
        preview = api('/api/workspace/imports/preview', csv)
        assert preview['new_count'] == 2 and preview['error_count'] == 0
        assert api('/api/workspace/imports/commit', {**csv, 'preview_token': preview['preview_token']})['imported_count'] == 2
        repeated = api('/api/workspace/imports/preview', {**csv, 'source_filename': 'august-copy.csv'})
        assert repeated['duplicate_count'] == 2 and repeated['new_count'] == 0
        report = api('/api/workspace/reports/cashflow?currency=GBP&date_from=2026-08-01&date_to=2026-08-31')
        assert report['totals']['spending'] == 4.5 and not report['partial']
        assert api('/api/summary')['n_transactions'] == 2
        # Keep this saved but paused: encryption/persistence must work without
        # ever sending the synthetic credential to an external provider.
        saved = api('/api/connections', {'provider': 'lunchflow', 'label': 'Synthetic paused bank',
            'credentials': {'api_key': 'synthetic-container-credential'}})
        api('/api/connections/' + saved['id'], {'enabled': False}, method='PATCH')
        assert 'synthetic-container-credential' not in json.dumps(api('/api/connections'))
        doctor = json.loads(command('exec', '-T', 'backend', 'python', '-m', 'scripts.doctor'))
        assert doctor['ok'] and doctor['owner_configured']
        upgraded = json.loads(command('run', '--rm', 'admin', 'python', '-m', 'scripts.verify_upgrade', '/app/data/finance.db'))
        assert upgraded['ok'] and upgraded['backup_restore_verified']
        print(json.dumps({'project': project, 'phase': 'enrollment and upgrade checks passed; verifying encrypted recovery'}), flush=True)
        command('stop', 'backend')
        command('run', '--rm', 'admin', 'age-keygen', '-o', '/app/backups/smoke-identity.txt')
        recipient = command('run', '--rm', 'admin', 'age-keygen', '-y', '/app/backups/smoke-identity.txt')
        command('run', '--rm', 'admin', 'python', '-m', 'scripts.secure_backup', 'create',
                '--data', '/app/data', '--uploads', '/app/uploads', '--key', '/app/keys/auth.key',
                '--recipient', recipient, '--output', '/app/backups/smoke.age')
        command('run', '--rm', 'admin', 'python', '-m', 'scripts.secure_backup', 'restore',
                '--archive', '/app/backups/smoke.age', '--identity', '/app/backups/smoke-identity.txt',
                '--destination', '/app/backups/restored')
        command('run', '--rm', 'admin', 'python', '-c',
                "import sqlite3; c=sqlite3.connect('/app/backups/restored/auth.db'); "
                "assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0; c.close()")
        command('run', '--rm', 'admin', 'python', '-c',
                "import sqlite3,json; from cryptography.fernet import Fernet; from pathlib import Path; "
                "c=sqlite3.connect('/app/backups/restored/finance.db'); "
                "assert c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==2; "
                "assert c.execute('SELECT SUM(amount) FROM transactions').fetchone()[0]==-9; "
                "key=Fernet(Path('/app/keys/auth.key').read_bytes().strip()); "
                "assert json.loads(key.decrypt(c.execute('SELECT secrets FROM provider_connections').fetchone()[0]))['api_key']=='synthetic-container-credential'; c.close()")
        command('up', '-d', '--wait', '--wait-timeout', '120', 'backend', 'frontend')
        assert api('/api/auth/status')['authenticated']
        assert api('/api/summary')['n_transactions'] == 2
        persisted = api('/api/workspace/accounts')['accounts']
        assert next(row for row in persisted if row['id'] == account['id'])['ownership_share'] == 0.5
        print(json.dumps({'project': project, 'phase': 'verifying complete scheduled-backup helper'}), flush=True)
        # The host helper receives an explicit synthetic environment file. It
        # must never discover the repository's environment or another project.
        with tempfile.TemporaryDirectory(prefix=project + '-backup-') as temporary:
            directory = Path(temporary)
            (directory / 'compose.yml').write_bytes((repository / 'deploy' / 'compose.product.yml').read_bytes())
            private_environment = ''.join(key + '=' + value + '\n' for key, value in settings.items()).encode()
            (directory / '.env').write_bytes(private_environment)
            os.chmod(directory / '.env', 0o600)
            complete = backup(directory, directory / 'sets', recipient, keep=2)
            target = Path(complete['backup_set'])
            assert complete['backend_resumed'] and completed_manifest(target, project)

            def decrypt(member):
                result = subprocess.run([*base, 'run', '--rm', '-T', 'admin', 'age', '--decrypt',
                    '--identity', '/app/backups/smoke-identity.txt'], input=(target / member).read_bytes(),
                    cwd=repository, env=environment, capture_output=True, timeout=120)
                assert result.returncode == 0, 'A complete recovery-set member failed authenticated decryption.'
                return result.stdout

            assert decrypt('key.age').strip().decode() == command('run', '--rm', '-T', 'admin', 'cat', '/app/keys/auth.key')
            with tarfile.open(fileobj=io.BytesIO(decrypt('configuration.age'))) as configuration:
                assert configuration.extractfile('.env').read() == private_environment
                assert json.load(configuration.extractfile('effective-compose.json'))['name'] == project
            with tarfile.open(fileobj=io.BytesIO(decrypt('data.age'))) as data:
                assert {'finance.db', 'auth.db', 'manifest.json'} <= set(data.getnames())
        # The helper resumes the original container itself; no compose up is
        # permitted to conceal a failed restart here.
        for attempt in range(60):
            try:
                if api('/api/health')['ok']:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        assert api('/api/auth/status')['authenticated']
        return {'ok': True, 'fresh_install': True, 'browser_enrollment': True, 'doctor': True,
                'upgrade_rehearsal': True, 'encrypted_restore': True, 'complete_backup_set': True,
                'original_session_preserved': True,
                'csv_identity_and_repeat_import': True, 'joint_account_ownership': True,
                'financial_records_and_encrypted_connection_restored': True,
                'project': project, 'provider_requests': False}
    finally:
        command('down', '--volumes', '--remove-orphans')
        for resource in (['ps', '-a', '-q'], ['volume', 'ls', '-q']):
            remaining = subprocess.run(['docker', *resource, '--filter', 'label=com.docker.compose.project=' + project],
                                       capture_output=True, text=True, timeout=30, env=environment)
            if remaining.returncode or remaining.stdout.strip():
                raise RuntimeError('Disposable project cleanup left resources; inspect only project ' + project)
        print(json.dumps({'project': project, 'phase': 'disposable resources removed'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-image', required=True)
    parser.add_argument('--frontend-image', required=True)
    arguments = parser.parse_args()
    print(json.dumps(smoke(arguments.backend_image, arguments.frontend_image), indent=2))
