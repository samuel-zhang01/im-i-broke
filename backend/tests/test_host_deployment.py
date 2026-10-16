"""Exercise host-update failure boundaries without Docker or household data."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
SPEC = importlib.util.spec_from_file_location('host_deployment', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC.loader.exec_module(deploy)
finally:
    sys.path.remove(str(SCRIPTS))
ORIGINAL_RUN = deploy.run


class DockerOracle:
    """Only implements synthetic commands used by the deployment orchestration."""

    def __init__(self, root):
        self.root = root
        self.project = 'synthetic-household'
        self.revision = 'a' * 40
        self.calls = []
        self.failure = None
        self.cleanup_failure = False
        self.stale_service = None
        self.candidate_change = None
        self.activated = False
        self.backend_id = 'original-backend'
        self.frontend_id = 'original-frontend'
        self.recipient = 'age1' + 'q' * 58
        self.ciphertext = b'age-encryption.org/v1\nsynthetic-ciphertext\x00'
        self.files = [root / 'docker-compose.yml', root / 'deploy/compose.release.yml']
        labels = {
            'com.docker.compose.project': self.project,
            'com.docker.compose.project.working_dir': str(root),
            'com.docker.compose.project.config_files': ','.join(map(str, self.files)),
        }
        mounts = [
            {'Type': 'volume', 'Name': self.project + '_' + name, 'Destination': target, 'RW': writable}
            for name, target, writable in (
                ('dbdata', '/app/data', True), ('uploads', '/app/uploads', True), ('authkeys', '/app/keys', False))
        ]
        self.config = {
            'name': self.project,
            'services': {
                'backend': {
                    'image': 'synthetic-backend:old',
                    'environment': {'FINANCE_DEPLOYMENT': 'public', 'LUNCH_FLOW_API': 'synthetic-private-setting'},
                    'volumes': [{'type': 'volume', 'source': name, 'target': target, 'read_only': not writable}
                                for name, target, writable in (
                                    ('dbdata', '/app/data', True), ('uploads', '/app/uploads', True), ('authkeys', '/app/keys', False))],
                    'networks': {'default': None},
                },
                'frontend': {
                    'image': 'synthetic-frontend:old',
                    'volumes': [],
                    'ports': [{'target': 8080, 'published': '8808', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
                    'networks': {'default': None},
                },
                **{name: {'image': 'synthetic-backend:old'} for name in ('backup', 'auth-admin', 'storage-init', 'import')},
            },
            'volumes': {name: {'name': self.project + '_' + name} for name in ('dbdata', 'uploads', 'authkeys', 'backups')},
            'networks': {'default': {'name': self.project + '_default'}},
        }
        self.containers = {}
        for service, ident, service_mounts in (
                ('backend', self.backend_id, mounts), ('frontend', self.frontend_id, [])):
            self.containers[ident] = {
                'id': ident,
                'labels': {**labels, 'com.docker.compose.service': service},
                'mounts': service_mounts,
                'ports': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8808'}]} if service == 'frontend' else {},
                'image': 'sha256:original-' + service,
                'state': {'Running': True, 'Health': {'Status': 'healthy'}},
                'networks': {self.project + '_default': {'Aliases': [service, ident]}},
                'name': '/' + self.project + '-' + service + '-1',
                'hostname': ident,
            }

    def current_config(self, compose_arguments=()):
        config = deepcopy(self.config)
        auto = self.root / deploy.AUTO_PINS
        if auto.is_file() and str(auto) in compose_arguments:
            for name, values in json.loads(auto.read_text())['services'].items():
                config['services'][name].update(values)
        if (self.failure == 'updated-port' and
                config['services']['backend']['image'].endswith('git-' + self.revision[:16])):
            config['services']['frontend']['ports'][0]['published'] = '9999'
        return config

    def run(self, arguments, **options):
        command = list(map(str, arguments))
        self.calls.append((command, options))
        if command[0] == 'git':
            if command[1:3] == ['rev-parse', '--show-toplevel']:
                return str(self.root).encode()
            if command[1:3] == ['rev-parse', 'HEAD']:
                return self.revision.encode()
            if command[1:3] == ['rev-parse', '--git-common-dir']:
                return b'.git'
            if command[1] == 'status':
                return b' M tracked.py\n' if self.failure == 'dirty-checkout' else b''
            if command[1] == 'symbolic-ref':
                return b'main'
            if command[1] in ('fetch', 'merge-base', 'merge'):
                return b''
            raise AssertionError('Unexpected synthetic Git command: ' + repr(command))
        if command[:3] == ['docker', 'image', 'inspect']:
            service = 'frontend' if '-frontend:' in command[-1] else 'backend'
            return ('sha256:candidate-' + service).encode()
        if command[:2] == ['docker', 'build']:
            if self.failure == 'build-' + Path(command[-1]).name:
                raise RuntimeError('Synthetic build failure')
            return b''
        if command[:2] == ['docker', 'inspect']:
            container = self.containers[command[-1]]
            values = {
                '{{json .Config.Labels}}': container['labels'],
                '{{json .Mounts}}': container['mounts'],
                '{{json .NetworkSettings.Ports}}': container['ports'],
                '{{json .Image}}': container['image'],
                '{{json .State}}': container['state'],
                '{{json .NetworkSettings.Networks}}': container['networks'],
                '{{json .Name}}': container['name'],
                '{{json .Config.Hostname}}': container['hostname'],
            }
            expression = command[command.index('--format') + 1]
            return '\n'.join(json.dumps(values[line]) for line in expression.splitlines()).encode()
        if command[:2] == ['docker', 'ps']:
            if any('service=frontend' in item for item in command):
                return self.frontend_id.encode()
            if self.failure == 'ambiguous-project':
                return (self.backend_id + ' ' + self.backend_id).encode()
            return self.backend_id.encode()
        if command[:2] == ['docker', 'start']:
            return command[-1].encode()
        if command[:2] == ['docker', 'compose']:
            if 'config' in command:
                return json.dumps(self.current_config(command)).encode()
            if 'stop' in command and not self.activated and self.failure == 'stop':
                raise RuntimeError('Synthetic stop observation timeout')
            if 'up' in command:
                self.activated = True
                for service in ('backend', 'frontend'):
                    ident = 'candidate-' + service
                    current = deepcopy(self.containers[self.backend_id if service == 'backend' else self.frontend_id])
                    current['id'] = ident
                    current['image'] = 'sha256:candidate-' + service
                    current['hostname'] = ident
                    current['networks'][self.project + '_default']['Aliases'] = [service, ident]
                    if self.stale_service == service:
                        current['image'] = 'sha256:original-' + service
                    if service == 'backend' and self.candidate_change == 'writable-key':
                        next(mount for mount in current['mounts'] if mount['Destination'] == '/app/keys')['RW'] = True
                    if service == 'backend' and self.candidate_change == 'volume':
                        next(mount for mount in current['mounts'] if mount['Destination'] == '/app/data')['Name'] = 'foreign-household_dbdata'
                    if service == 'frontend' and self.candidate_change == 'network':
                        current['networks'] = {'foreign-proxy-network': {'Aliases': [service, ident]}}
                    current['labels']['com.docker.compose.project.config_files'] = ','.join(
                        map(str, [*self.files, self.root / deploy.AUTO_PINS]))
                    self.containers[ident] = current
                if self.failure == 'activation':
                    raise RuntimeError('Synthetic candidate health timeout')
                return b''
            if 'ps' in command:
                return ('candidate-' + command[-1]).encode()
            if 'exec' in command and 'frontend' in command and self.failure == 'proxy':
                raise RuntimeError('Synthetic frontend proxy failure')
            if 'exec' in command and 'scripts.doctor' in command and self.failure == 'doctor':
                raise RuntimeError('Synthetic installation doctor failure')
            return b''
        if command[:2] == ['docker', 'run']:
            if options.get('operation', '').endswith('image runtime check'):
                if self.failure == 'image-runtime':
                    raise RuntimeError('Synthetic image runtime failure')
                return b''
            if 'age-keygen' in command:
                if '-y' in command:
                    if '-i' not in command:
                        raise RuntimeError('Docker stdin is closed without -i')
                    self.assert_private_identity(options.get('input_data'))
                    return self.recipient.encode()
                return b'# synthetic age identity\nAGE-SECRET-KEY-SYNTHETIC\n'
            if 'scripts.secure_backup' in command:
                if self.failure == 'backup':
                    raise RuntimeError('Synthetic backup failure')
                return b'Encrypted backup created.'
            if 'age' in command and '--encrypt' in command:
                if options.get('input_data') is not None and '-i' not in command:
                    raise RuntimeError('Docker stdin is closed without -i')
                return b''
            if 'cat' in command:
                data = (b'invalid synthetic ciphertext' if self.failure == 'invalid-ciphertext'
                        else self.ciphertext + command[-1].encode())
                destination = options.get('output_file')
                if destination is None:
                    raise AssertionError('Recovery exports must stream into a private output file.')
                destination.write(data)
                if self.failure == 'backup-export':
                    raise RuntimeError('Synthetic ciphertext export failure')
                return b''
            if 'scripts.verify_upgrade' in command:
                if self.failure == 'rehearsal':
                    raise RuntimeError('Synthetic migration rehearsal failure')
                return b'{"ok":true,"source_modified":false}'
            if 'python' in command and '-c' in command:
                if self.cleanup_failure:
                    raise RuntimeError('Synthetic cleanup failure')
                return b''
            raise AssertionError('Unexpected synthetic tool command: ' + repr(command))
        if command[0] == sys.executable:
            return b''
        raise AssertionError('Unexpected synthetic command: ' + repr(command))

    @staticmethod
    def assert_private_identity(value):
        if value != b'# synthetic age identity\nAGE-SECRET-KEY-SYNTHETIC\n':
            raise AssertionError('Recipient derivation must read the private identity, not editable comments.')


class HostDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-host-deployment-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'deploy').mkdir()
        (self.root / '.env').write_bytes(b'FINANCE_PORT=8808\nLUNCH_FLOW_API=synthetic-private-setting\n')
        (self.root / '.env').chmod(0o600)
        (self.root / 'docker-compose.yml').write_text('# synthetic compose base\n')
        (self.root / 'deploy/compose.release.yml').write_text('# Preserve this original release overlay.\n')
        self.output = self.root / 'private-recovery'
        self.output.mkdir(mode=0o700)
        self.oracle = DockerOracle(self.root)
        run_patch = patch.object(deploy, 'run', side_effect=self.oracle.run)
        self.mock_run = run_patch.start()
        self.addCleanup(run_patch.stop)
        stdout_patch = patch('sys.stdout', new_callable=io.StringIO)
        self.stdout = stdout_patch.start()
        self.addCleanup(stdout_patch.stop)
        stderr_patch = patch('sys.stderr', new_callable=io.StringIO)
        self.stderr = stderr_patch.start()
        self.addCleanup(stderr_patch.stop)

    def deployment(self):
        return deploy.discover(self.root)

    def apply(self, **kwargs):
        return deploy.apply(self.deployment(), self.oracle.revision, output=self.output,
                            recipient=self.oracle.recipient, **kwargs)

    def selected_commands(self, *words):
        return [command for command, _ in self.oracle.calls if all(word in command for word in words)]

    def frontend_policy_mount(self, source):
        target = '/etc/nginx/access-policy.conf'
        self.oracle.config['services']['frontend']['volumes'] = [
            {'type': 'bind', 'source': str(source), 'target': target, 'read_only': True}]
        self.oracle.containers[self.oracle.frontend_id]['mounts'] = [
            {'Type': 'bind', 'Source': str(source), 'Destination': target, 'RW': False}]

    def assert_no_destructive_operations(self):
        for command, _ in self.oracle.calls:
            self.assertNotIn('down', command)
            self.assertNotIn('prune', command)
            self.assertNotIn('storage-init', command[command.index('run') + 1:] if 'run' in command else [])
            self.assertNotIn('scripts.installation', command)
            self.assertNotIn('scripts.initialize_storage', command)
            self.assertNotIn('scripts.auth', command)

    def test_verified_update_backs_up_and_rehearses_before_activation_preserving_private_configuration(self):
        env_before = (self.root / '.env').read_bytes()
        release_before = (self.root / 'deploy/compose.release.yml').read_bytes()
        receipt = self.apply()
        self.assertTrue(receipt['verified'])
        self.assertEqual((self.root / '.env').read_bytes(), env_before)
        self.assertEqual((self.root / 'deploy/compose.release.yml').read_bytes(), release_before)
        pins = json.loads((self.root / deploy.AUTO_PINS).read_text())
        self.assertEqual(set(pins['services']), set(deploy.IMAGE_SERVICES))
        self.assertEqual(pins['services']['backend']['image'], receipt['images']['backend'])
        self.assertEqual(pins['services']['frontend']['image'], receipt['images']['frontend'])
        commands = [command for command, _ in self.oracle.calls]
        stop = next(i for i, command in enumerate(commands) if 'compose' in command and 'stop' in command)
        backup = next(i for i, command in enumerate(commands) if 'scripts.secure_backup' in command)
        rehearsal = next(i for i, command in enumerate(commands) if 'scripts.verify_upgrade' in command)
        up = next(i for i, command in enumerate(commands) if 'compose' in command and 'up' in command)
        self.assertLess(stop, backup)
        self.assertLess(backup, rehearsal)
        self.assertIn(receipt['images']['backend'], commands[backup])
        self.assertIn('--offline-source', commands[backup])
        self.assertIn('--offline-source', commands[rehearsal])
        self.assertLess(rehearsal, up)
        recovery = Path(receipt['recovery_set'])
        self.assertEqual(set(path.name for path in recovery.iterdir()), {'data.age', 'key.age', 'configuration.age', 'manifest.json'})
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        for path in recovery.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        manifest = json.loads((recovery / 'manifest.json').read_text())
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['backend_image'], 'sha256:original-backend')
        self.assertEqual(manifest['frontend_image'], 'sha256:original-frontend')
        self.assertEqual(set(manifest['files']), {'data.age', 'key.age', 'configuration.age'})
        for name, expected in manifest['files'].items():
            exported = (recovery / name).read_bytes()
            self.assertEqual(expected['bytes'], len(exported))
            self.assertEqual(expected['sha256'], hashlib.sha256(exported).hexdigest())
        self.assertNotIn('synthetic-private-setting', json.dumps(receipt) + json.dumps(manifest))
        self.assertNotIn('synthetic-private-setting', self.stdout.getvalue() + self.stderr.getvalue())
        encrypted_config = next(options['input_data'] for command, options in self.oracle.calls
                                if 'age' in command and options.get('input_data'))
        with tarfile.open(fileobj=io.BytesIO(encrypted_config)) as archive:
            self.assertEqual(archive.extractfile('.env').read(), env_before)
            self.assertEqual(archive.extractfile('deploy/compose.release.yml').read(), release_before)
        self.assert_no_destructive_operations()

    def test_private_frontend_policy_is_preserved_in_the_encrypted_configuration_archive(self):
        policy = self.root / 'deploy/lan-access.conf'
        original = b'# Synthetic private network policy\nallow 127.0.0.1;\nallow 192.0.2.0/24;\ndeny all;\n'
        policy.write_bytes(original)
        policy.chmod(0o600)
        self.frontend_policy_mount(policy)
        receipt = self.apply()
        self.assertTrue(receipt['verified'])
        self.assertEqual(policy.read_bytes(), original)
        encrypted_config = next(options['input_data'] for command, options in self.oracle.calls
                                if 'age' in command and options.get('input_data'))
        with tarfile.open(fileobj=io.BytesIO(encrypted_config)) as archive:
            member = archive.getmember('deploy/lan-access.conf')
            self.assertEqual(member.mode, 0o600)
            self.assertEqual(archive.extractfile(member).read(), original)
            self.assertTrue(all(not Path(name).is_absolute() for name in archive.getnames()))
        self.assertNotIn('192.0.2.0/24', self.stdout.getvalue() + self.stderr.getvalue())

    def test_frontend_policy_outside_the_checkout_is_rejected_before_live_changes(self):
        external = tempfile.TemporaryDirectory(prefix='synthetic-external-policy-')
        self.addCleanup(external.cleanup)
        policy = Path(external.name) / 'access-policy.conf'
        policy.write_text('allow 127.0.0.1;\ndeny all;\n')
        self.frontend_policy_mount(policy)
        with self.assertRaisesRegex(ValueError, '[Bb]ind|checkout|[Ff]ile'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('docker', 'build'))

    def test_frontend_directory_bind_is_rejected_before_live_changes(self):
        directory = self.root / 'deploy/policy-directory'
        directory.mkdir()
        self.frontend_policy_mount(directory)
        with self.assertRaisesRegex(ValueError, '[Bb]ind|[Ff]ile|directory'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('docker', 'build'))

    def test_frontend_policy_symlink_is_rejected_before_live_changes(self):
        original = self.root / 'deploy/original-policy.conf'
        original.write_text('allow 127.0.0.1;\ndeny all;\n')
        policy = self.root / 'deploy/lan-access.conf'
        policy.symlink_to(original)
        self.frontend_policy_mount(policy)
        with self.assertRaisesRegex(ValueError, '[Ss]ymbolic|[Ss]ymlink'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('docker', 'build'))

    def test_backup_or_rehearsal_failure_restarts_exact_original_backend_and_never_activates(self):
        for stage in ('backup', 'backup-export', 'rehearsal'):
            with self.subTest(stage=stage):
                self.oracle.calls.clear()
                self.oracle.failure = stage
                with self.assertRaisesRegex(RuntimeError, 'Synthetic'):
                    self.apply()
                self.assertEqual(self.selected_commands('docker', 'start'), [['docker', 'start', self.oracle.backend_id]])
                self.assertFalse(self.selected_commands('compose', 'up'))
                self.assertFalse((self.root / deploy.AUTO_PINS).exists())
                self.assertFalse(list(self.output.glob('.partial-*')))
                self.assert_no_destructive_operations()

    def test_candidate_activation_or_proxy_failure_stops_candidates_and_never_restarts_old_backend(self):
        for stage in ('activation', 'proxy', 'doctor'):
            with self.subTest(stage=stage):
                self.oracle.calls.clear()
                self.oracle.failure = stage
                with self.assertRaisesRegex(RuntimeError, 'Synthetic'):
                    self.apply()
                self.assertFalse(self.selected_commands('docker', 'start'))
                stops = self.selected_commands('compose', 'stop')
                self.assertEqual(stops[-1][-3:], ['stop', 'backend', 'frontend'])
                self.assertTrue((self.root / deploy.AUTO_PINS).is_file())
                self.assertTrue(list(self.output.glob('recovery-*/manifest.json')))
                self.assertIn('Restore into new private storage', self.stderr.getvalue())
                self.assert_no_destructive_operations()

    def test_health_alone_cannot_accept_backend_or_frontend_running_an_old_image(self):
        for service in ('backend', 'frontend'):
            with self.subTest(service=service):
                self.oracle.calls.clear()
                self.oracle.stale_service = service
                with self.assertRaisesRegex(ValueError, 'different image'):
                    self.apply()
                self.assertFalse(self.selected_commands('docker', 'start'))
                self.assertEqual(self.selected_commands('compose', 'stop')[-1][-3:], ['stop', 'backend', 'frontend'])
                self.assertFalse((self.output / 'last-deployment.json').exists())

    def test_healthy_candidate_with_changed_live_storage_or_network_is_stopped(self):
        for change in ('writable-key', 'volume', 'network'):
            with self.subTest(change=change):
                self.oracle.calls.clear()
                self.oracle.candidate_change = change
                with self.assertRaisesRegex(ValueError, '[Mm]ount|[Ss]torage|[Nn]etwork'):
                    self.apply()
                self.assertFalse(self.selected_commands('docker', 'start'))
                self.assertEqual(self.selected_commands('compose', 'stop')[-1][-3:], ['stop', 'backend', 'frontend'])
                self.assertFalse((self.output / 'last-deployment.json').exists())

    def test_failed_build_leaves_live_containers_and_existing_auto_pins_unchanged(self):
        auto = self.root / deploy.AUTO_PINS
        previous = json.dumps({'services': {'backend': {'image': 'synthetic-backend:previous'},
                                           'frontend': {'image': 'synthetic-frontend:previous'}}}).encode()
        auto.write_bytes(previous)
        self.oracle.failure = 'build-frontend'
        with self.assertRaisesRegex(RuntimeError, 'build failure'):
            self.apply()
        self.assertEqual(auto.read_bytes(), previous)
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('compose', 'up'))
        self.assertFalse(self.selected_commands('docker', 'start'))

    def test_image_runtime_failure_leaves_original_services_and_pins_untouched(self):
        auto = self.root / deploy.AUTO_PINS
        auto.write_bytes(b'{"services":{}}')
        before = auto.read_bytes()
        self.oracle.failure = 'image-runtime'
        with self.assertRaisesRegex(RuntimeError, 'image runtime failure'):
            self.apply()
        self.assertEqual(auto.read_bytes(), before)
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('compose', 'up'))
        self.assertFalse(self.selected_commands('scripts.secure_backup'))

    def test_image_runtime_checks_have_no_household_mounts_or_network_before_backend_stop(self):
        self.apply()
        probes = [(i, command) for i, (command, options) in enumerate(self.oracle.calls)
                  if options.get('operation', '').endswith('image runtime check')]
        self.assertEqual(len(probes), 2)
        stop = next(i for i, (command, _) in enumerate(self.oracle.calls)
                    if 'compose' in command and 'stop' in command)
        for position, command in probes:
            self.assertLess(position, stop)
            self.assertEqual(command[command.index('--network') + 1], 'none')
            self.assertIn('--read-only', command)
            self.assertNotIn('--mount', command)
            self.assertNotIn('--user', command)
            self.assertNotIn('--env-file', command)
        self.assertIn('backend:127.0.0.1', probes[1][1])

    def test_rehearsal_has_read_only_financial_mount_and_no_key_provider_or_network_access(self):
        self.apply()
        command = self.selected_commands('scripts.verify_upgrade')[0]
        mounts = [command[index + 1] for index, word in enumerate(command) if word == '--mount']
        self.assertEqual(mounts, ['type=volume,source=synthetic-household_dbdata,target=/app/data,readonly'])
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertIn('--read-only', command)
        self.assertIn('--tmpfs', command)
        self.assertNotIn('--env', command)
        self.assertNotIn('/app/keys', ' '.join(command))

    def test_default_identity_is_private_and_recipient_derivation_uses_docker_stdin(self):
        recipient, identity = deploy.recipient_key(self.output, 'synthetic-backend:old')
        self.assertEqual(recipient, self.oracle.recipient)
        self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
        command, options = next((command, options) for command, options in self.oracle.calls
                                if 'age-keygen' in command and '-y' in command)
        self.assertIn('-i', command)
        self.assertEqual(options['input_data'], identity.read_bytes())
        self.assertNotIn('AGE-SECRET-KEY', self.stdout.getvalue() + self.stderr.getvalue())

    def test_dirty_checkout_and_ambiguous_project_are_rejected_before_mutating_commands(self):
        self.oracle.failure = 'dirty-checkout'
        with self.assertRaisesRegex(ValueError, 'Commit or move local changes'):
            deploy.clean_checkout(self.root)
        self.oracle.failure = 'ambiguous-project'
        with self.assertRaisesRegex(ValueError, 'Expected one existing finance backend'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('docker', 'build'))

    def test_changed_volume_or_port_configuration_is_rejected_before_backend_stop(self):
        self.oracle.config['volumes']['dbdata']['name'] = 'foreign-household_dbdata'
        with self.assertRaisesRegex(ValueError, 'shared household storage'):
            self.deployment()
        self.oracle.config['volumes']['dbdata']['name'] = self.oracle.project + '_dbdata'
        self.oracle.config['services']['frontend']['ports'][0]['published'] = '9999'
        with self.assertRaisesRegex(ValueError, 'ports differ'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))

    def test_pre_activation_validation_failure_restores_previous_auto_pins_and_original_container(self):
        auto = self.root / deploy.AUTO_PINS
        previous = json.dumps({'services': {'backend': {'image': 'synthetic-backend:previous'},
                                           'frontend': {'image': 'synthetic-frontend:previous'}}}).encode()
        auto.write_bytes(previous)
        self.oracle.files.append(auto)
        for container in self.oracle.containers.values():
            container['labels']['com.docker.compose.project.config_files'] = ','.join(map(str, self.oracle.files))
        self.oracle.failure = 'updated-port'
        with self.assertRaisesRegex(ValueError, 'ports differ'):
            self.apply()
        self.assertEqual(auto.read_bytes(), previous)
        self.assertEqual(self.selected_commands('docker', 'start'), [['docker', 'start', self.oracle.backend_id]])
        self.assertFalse(self.selected_commands('compose', 'up'))
        self.assertTrue(list(self.output.glob('recovery-*/manifest.json')))

    def test_remote_docker_preflight_stops_cli_before_discovery_or_source_changes(self):
        with patch.dict(os.environ, {'DOCKER_CONTEXT': '', 'DOCKER_HOST': 'ssh://synthetic-remote.example'}), \
                patch.object(deploy, 'ROOT', self.root), \
                patch.object(deploy.shutil, 'which', return_value='/synthetic/tool'), \
                patch.object(sys, 'argv', ['deploy.py', '--check']):
            with self.assertRaises(SystemExit) as error:
                deploy.main()
        self.assertEqual(error.exception.code, 1)
        self.assertIn('local Docker', self.stderr.getvalue())
        self.assertEqual(self.oracle.calls, [])

    def test_running_installation_requirement_preserves_a_stopped_household(self):
        self.oracle.containers[self.oracle.backend_id]['state']['Running'] = False
        with self.assertRaisesRegex(ValueError, 'must be running'):
            self.apply()
        self.assertFalse(self.selected_commands('docker', 'build'))
        self.assertFalse(self.selected_commands('compose', 'stop'))
        self.assertFalse(self.selected_commands('compose', 'up'))

    def test_subprocess_failure_withholds_private_configuration_and_scrubs_inherited_household_settings(self):
        with patch.dict(os.environ, {'LUNCH_FLOW_API': 'synthetic-bank-secret', 'FINANCE_DB_PATH': '/synthetic/private-db',
                                     'COMPOSE_PROJECT_NAME': 'foreign-project', 'DOCKER_CONTEXT': 'desktop-linux'}), \
                patch.object(deploy.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=1, stdout=b'synthetic-bank-secret', stderr=b'synthetic-private-configuration')) as executed:
            with self.assertRaises(RuntimeError) as failure:
                ORIGINAL_RUN(['docker', 'compose', 'config'], directory=self.root,
                             operation='deployment configuration check')
        self.assertIn('deployment configuration check (exit 1)', str(failure.exception))
        self.assertNotIn('synthetic-bank-secret', str(failure.exception))
        self.assertNotIn('synthetic-private-configuration', str(failure.exception))
        environment = executed.call_args.kwargs['env']
        self.assertNotIn('LUNCH_FLOW_API', environment)
        self.assertNotIn('FINANCE_DB_PATH', environment)
        self.assertNotIn('COMPOSE_PROJECT_NAME', environment)
        self.assertEqual(environment['DOCKER_CONTEXT'], 'desktop-linux')

    def test_second_deployer_cannot_acquire_the_same_checkout_lock(self):
        (self.root / '.git').mkdir()
        with deploy.lock(self.root):
            with self.assertRaisesRegex(ValueError, 'Another deployment'):
                with deploy.lock(self.root):
                    self.fail('A concurrent deployer acquired the same exclusive lock.')
        with deploy.lock(self.root):
            self.assertEqual((self.root / '.git/finance-deploy.lock').stat().st_mode & 0o777, 0o600)

    def test_proxy_network_changes_are_rejected_before_stopping_the_original_backend(self):
        self.oracle.config['networks']['default']['name'] = 'foreign-proxy-network'
        with self.assertRaisesRegex(ValueError, '[Nn]etwork'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))

    def test_removing_an_existing_proxy_alias_is_rejected_before_activation(self):
        self.oracle.config['networks']['proxy'] = {'name': 'synthetic-edge', 'external': True}
        self.oracle.config['services']['frontend']['networks']['proxy'] = {'aliases': ['ledger-web']}
        self.oracle.containers[self.oracle.frontend_id]['networks']['synthetic-edge'] = {
            'Aliases': ['frontend', self.oracle.frontend_id, 'ledger-web']}
        self.deployment()
        self.oracle.config['services']['frontend']['networks']['proxy']['aliases'] = []
        with self.assertRaisesRegex(ValueError, '[Nn]etwork|[Aa]lias'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))

    def test_key_mount_cannot_become_writable_during_a_source_update(self):
        key_mount = next(item for item in self.oracle.config['services']['backend']['volumes']
                         if item['target'] == '/app/keys')
        key_mount['read_only'] = False
        with self.assertRaisesRegex(ValueError, '[Mm]ount|[Ss]torage|read.only'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))

    def test_logically_different_storage_roots_cannot_share_one_named_volume(self):
        name = self.oracle.project + '_dbdata'
        self.oracle.config['volumes']['authkeys']['name'] = name
        actual = next(item for item in self.oracle.containers[self.oracle.backend_id]['mounts']
                      if item['Destination'] == '/app/keys')
        actual['Name'] = name
        with self.assertRaisesRegex(ValueError, '[Ss]torage|[Vv]olume'):
            self.deployment()
        self.assertFalse(self.selected_commands('compose', 'stop'))

    def test_stop_observation_timeout_attempts_to_resume_exact_original_container(self):
        self.oracle.failure = 'stop'
        with self.assertRaisesRegex(RuntimeError, 'stop observation timeout'):
            self.apply()
        self.assertEqual(self.selected_commands('docker', 'start'), [['docker', 'start', self.oracle.backend_id]])
        self.assertFalse(self.selected_commands('scripts.secure_backup'))
        self.assertFalse(self.selected_commands('compose', 'up'))

    def test_invalid_ciphertext_export_never_publishes_a_recovery_set_or_activates(self):
        self.oracle.failure = 'invalid-ciphertext'
        with self.assertRaisesRegex(ValueError, 'not an age ciphertext'):
            self.apply()
        self.assertFalse(list(self.output.glob('recovery-*')))
        self.assertFalse(list(self.output.glob('.partial-*')))
        self.assertFalse(self.selected_commands('compose', 'up'))
        self.assertEqual(self.selected_commands('docker', 'start'), [['docker', 'start', self.oracle.backend_id]])

    def test_ciphertext_cleanup_failure_keeps_complete_recovery_set_usable(self):
        self.oracle.cleanup_failure = True
        receipt = self.apply()
        self.assertTrue(receipt['verified'])
        self.assertTrue((Path(receipt['recovery_set']) / 'manifest.json').is_file())
        self.assertIn('Encrypted scratch cleanup failed', self.stderr.getvalue())

    def test_ciphertext_cleanup_failure_preserves_original_backup_error(self):
        self.oracle.failure = 'backup'
        self.oracle.cleanup_failure = True
        with self.assertRaisesRegex(RuntimeError, 'Synthetic backup failure'):
            self.apply()
        self.assertFalse(self.selected_commands('compose', 'up'))
        self.assertEqual(self.selected_commands('docker', 'start'), [['docker', 'start', self.oracle.backend_id]])
        self.assertIn('Encrypted scratch cleanup failed', self.stderr.getvalue())

    def test_export_subprocess_writes_directly_into_the_private_file_instead_of_a_pipe(self):
        target = self.output / 'synthetic.age'
        with target.open('wb') as destination, patch.object(deploy.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=None, stderr=b'')) as executed:
            result = ORIGINAL_RUN(['docker', 'run', 'synthetic-image', 'cat', '/synthetic.age'],
                                  directory=self.root, output_file=destination)
            self.assertIs(executed.call_args.kwargs['stdout'], destination)
        self.assertEqual(result, b'')

    def test_source_synchronization_keeps_exclusive_lock_through_reexec(self):
        (self.root / '.git').mkdir()
        observed = {}

        class Reexecuted(BaseException):
            pass

        def reexecute(executable, arguments):
            descriptor = int(arguments[arguments.index('--lock-fd') + 1])
            observed['descriptor'] = descriptor
            self.assertTrue(os.get_inheritable(descriptor))
            self.assertIn('--local', arguments)
            with self.assertRaisesRegex(ValueError, 'Another deployment'):
                with deploy.lock(self.root):
                    self.fail('The sync-to-exec boundary released the exclusive lock.')
            # An exec-inherited descriptor refers to the same open-file lock;
            # a duplicate simulates the new process retaining that ownership.
            duplicate = os.dup(descriptor)
            with deploy.lock(self.root, inherited=duplicate):
                with self.assertRaisesRegex(ValueError, 'Another deployment'):
                    with deploy.lock(self.root):
                        self.fail('The inherited lock permitted a competing updater.')
            raise Reexecuted()

        with patch.object(deploy, 'ROOT', self.root), \
                patch.object(deploy.installation, 'docker_ready'), \
                patch.object(deploy.shutil, 'which', return_value='/synthetic/tool'), \
                patch.object(sys, 'argv', ['deploy.py']), \
                patch.object(deploy.os, 'execv', side_effect=reexecute):
            with self.assertRaises(Reexecuted):
                deploy.main()
        self.assertIn('descriptor', observed)
        self.assertEqual(len(self.selected_commands('git', 'fetch')), 1)
        self.assertEqual(len(self.selected_commands('git', 'merge')), 1)
        self.assertFalse(self.selected_commands('docker', 'build'))
        with self.assertRaises(OSError):
            os.fstat(observed['descriptor'])
        with deploy.lock(self.root):
            pass

    def test_inherited_lock_from_a_different_file_is_refused(self):
        (self.root / '.git').mkdir()
        with deploy.lock(self.root):
            pass
        other = self.root / '.git/unrelated-private-file'
        descriptor = os.open(other, os.O_CREAT | os.O_RDWR, 0o600)
        with self.assertRaisesRegex(ValueError, 'private regular file'):
            with deploy.lock(self.root, inherited=descriptor):
                self.fail('An unrelated inherited descriptor was accepted as the deployment lock.')
        with self.assertRaises(OSError):
            os.fstat(descriptor)


if __name__ == '__main__':
    unittest.main()
