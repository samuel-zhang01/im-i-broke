"""New-installation orchestration must never adopt or overwrite another household."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / 'scripts' / 'setup.py'
SPEC = importlib.util.spec_from_file_location('easy_setup', SOURCE)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class EasySetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / 'household'
        self.images = {'backend_image': 'ledger-synthetic-backend:verify',
                       'frontend_image': 'ledger-synthetic-frontend:verify'}
        self.calls = []
        self.failure = None
        self.existing = None
        self.running = False
        self.endpoint = 'unix:///var/run/docker.sock'
        self.engine_type = 'linux'
        self.environment = patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **changes):
        self.manifest = setup.prepare(self.target, **{**self.images, **changes})
        project = self.manifest['instance_id']
        self.effective = {'name': project,
                          'services': {name: {'volumes': [{'type': 'volume'}]}
                                       for name in ('backend', 'frontend', 'admin')},
                          'volumes': {name: {'name': project + '_' + name}
                                      for name in ('data', 'uploads', 'keys', 'backups')}}
        return self.manifest

    def docker(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.failure and self.failure in command:
            return SimpleNamespace(returncode=1, stdout='synthetic-secret-output', stderr='synthetic-private-setting')
        output = ''
        if command[1:3] == ['context', 'inspect']:
            output = self.endpoint
        elif command[1] == 'info':
            output = self.engine_type
        elif 'config' in command:
            output = json.dumps(self.effective)
        elif 'token' in command or 'renew-token' in command:
            output = 'synthetic-enrollment-token'
        elif 'ps' in command and '--services' in command:
            output = 'backend\nfrontend' if self.running else ''
        elif self.existing == 'label' and 'ps' in command:
            output = 'existing-container'
        elif self.existing == 'unlabelled-volume' and '--format' in command and 'volume' in command:
            output = self.manifest['instance_id'] + '_data'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def launch(self):
        with patch.object(setup.subprocess, 'run', side_effect=self.docker), patch('sys.stdout', new_callable=io.StringIO) as printed:
            setup.launch(self.target, self.manifest)
        return printed.getvalue()

    def test_private_portable_installations_have_unique_storage_and_six_settings(self):
        first = self.prepare()
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        for path in self.target.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        env = (self.target / '.env').read_text()
        self.assertEqual(len(env.splitlines()), 6)
        self.assertNotIn('LUNCH_FLOW', env)
        self.assertNotIn('SNAPTRADE', env)
        self.assertIn('FINANCE_COOKIE_SECURE=false\n', env)
        self.assertEqual((self.target / 'manage.py').read_bytes(), SOURCE.read_bytes())
        self.target = self.root / 'another-household'
        second = self.prepare()
        self.assertNotEqual(first['instance_id'], second['instance_id'])
        self.assertNotEqual(first['private_port'], second['private_port'])

    def test_existing_even_empty_destinations_and_symlinks_are_refused(self):
        self.target.mkdir()
        with self.assertRaisesRegex(ValueError, 'new directory'):
            self.prepare()
        sentinel = self.target / '.env'
        sentinel.write_text('Keep existing configuration')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(sentinel.read_text(), 'Keep existing configuration')
        linked = self.root / 'linked'
        linked.symlink_to(self.target, target_is_directory=True)
        self.target = linked / 'nested'
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.prepare()
        self.assertFalse((sentinel.parent / 'nested').exists())

    def test_bad_images_and_unavailable_ports_create_no_directory(self):
        for value in ('image:latest', 'image', 'image:v1\nFINANCE_AUTH_ENABLED=false', 'image:$(id)', 'IMAGE:v1', 'image:v2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare(backend_image=value)
            self.assertFalse(self.target.exists())
        for port in (80, 65536):
            with self.assertRaises(ValueError):
                self.prepare(port=port)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            with self.assertRaisesRegex(ValueError, 'port is in use'):
                self.prepare(port=listener.getsockname()[1])
        self.assertFalse(self.target.exists())

    def test_partial_file_creation_only_removes_its_new_directory(self):
        sibling = self.root / 'live'; sibling.mkdir()
        (sibling / 'keep').write_text('existing household')
        original = setup.write_private
        def fail_on_manifest(path, value):
            if path.name == 'instance.json':
                raise OSError('synthetic disk failure')
            original(path, value)
        with patch.object(setup, 'write_private', side_effect=fail_on_manifest), self.assertRaises(OSError):
            self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual((sibling / 'keep').read_text(), 'existing household')

    def test_successful_launch_ignores_shell_households_credentials_and_compose_overrides(self):
        self.prepare()
        inherited = {'FINANCE_INSTANCE_ID': 'live-household', 'COMPOSE_FILE': '/private/live.yml',
                     'COMPOSE_PROJECT_NAME': 'other-household', 'LUNCH_FLOW_API': 'synthetic-private-setting',
                     'SNAPTRADE_COMMERCIAL_KEY': 'synthetic-private-setting', 'INVESTMENT_PRICE_REFRESH_SECONDS': '7'}
        with patch.dict(os.environ, inherited):
            output = self.launch()
        self.assertIn('http://localhost:' + str(self.manifest['private_port']), output)
        self.assertIn('synthetic-enrollment-token', output)
        self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'running')
        calls = [command for command, _ in self.calls]
        self.assertLess(next(i for i, command in enumerate(calls) if 'init' in command),
                        next(i for i, command in enumerate(calls) if 'up' in command))
        self.assertFalse(any('down' in command for command in calls))
        for command, options in self.calls:
            self.assertFalse(set(inherited) & set(options['env']))
            if 'compose' in command and 'version' not in command:
                self.assertEqual(command[command.index('--env-file') + 1], str(self.target / '.env'))

    def test_local_unix_and_windows_pipe_engines_pass_preflight(self):
        for endpoint in ('unix:///var/run/docker.sock', 'unix:///tmp/docker-desktop/docker.sock',
                         'npipe:////./pipe/docker_engine', 'npipe:////./pipe/dockerDesktopLinuxEngine'):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {'DOCKER_HOST': endpoint}), \
                    patch.object(setup.subprocess, 'run', side_effect=self.docker):
                setup.docker_ready()

    def test_remote_or_unknown_engine_refused_before_docker_mutations(self):
        self.prepare()
        for endpoint in ('ssh://operator@remote.example', 'tcp://remote.example:2376',
                         'tcp://127.0.0.1:2375', 'npipe:////remote/pipe/docker_engine', '',
                         'unix://remote.example/socket'):
            self.endpoint = endpoint
            self.calls = []
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, 'local Docker'):
                self.launch()
            self.assertTrue(all(command[1:3] == ['context', 'inspect'] for command, _ in self.calls))
            self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'prepared')

    def test_docker_environment_precedence_matches_cli(self):
        with patch.object(setup.subprocess, 'run', side_effect=self.docker):
            # An explicit context overrides an otherwise remote DOCKER_HOST.
            with patch.dict(os.environ, {'DOCKER_CONTEXT': 'desktop-linux', 'DOCKER_HOST': 'ssh://remote.example'}):
                setup.require_local_docker()
            self.assertEqual(self.calls[-1][0][-1], 'desktop-linux')
            self.calls = []
            # Without an explicit context, DOCKER_HOST overrides the saved context.
            with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://remote.example'}):
                with self.assertRaisesRegex(ValueError, 'local Docker'):
                    setup.require_local_docker()
            self.assertEqual(self.calls, [])

    def test_windows_container_engine_is_rejected_before_initialization(self):
        self.prepare()
        self.engine_type = 'windows'
        with self.assertRaisesRegex(ValueError, 'Linux containers'):
            self.launch()
        self.assertFalse(any('init' in command or 'up' in command for command, _ in self.calls))

    def test_generated_manager_rechecks_engine_before_start_stop_or_token(self):
        self.prepare()
        self.manifest['phase'] = 'running'
        setup.save_manifest(self.target, self.manifest)
        self.endpoint = 'ssh://operator@remote.example'
        for command in ('start', 'stop', 'token'):
            self.calls = []
            with self.subTest(command=command), patch.object(setup, '__file__', str(self.target / 'manage.py')), \
                    patch.object(sys, 'argv', ['manage.py', command]), \
                    patch.object(setup.subprocess, 'run', side_effect=self.docker), \
                    patch('sys.stderr', new_callable=io.StringIO) as errors:
                with self.assertRaises(SystemExit):
                    setup.manage_main()
            self.assertIn('local Docker', errors.getvalue())
            self.assertTrue(all(arguments[1:3] == ['context', 'inspect'] for arguments, _ in self.calls))

    def test_refuses_existing_labeled_and_unlabeled_storage_before_initializing(self):
        self.prepare()
        for kind in ('label', 'unlabelled-volume'):
            self.calls = []
            self.existing = kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.launch()
            self.assertFalse(any('init' in command or 'up' in command for command, _ in self.calls))
            self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'prepared')

    def test_rejects_foreign_or_host_storage_before_any_mutation(self):
        self.prepare()
        for key, value in (('external', True), ('name', 'existing-household_data'),
                           ('driver_opts', {'device': '/private/live', 'o': 'bind', 'type': 'none'})):
            original = dict(self.effective['volumes']['data'])
            self.effective['volumes']['data'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.launch()
            self.effective['volumes']['data'] = original
        self.effective['services']['backend']['volumes'] = [{'type': 'bind'}]
        with self.assertRaises(ValueError):
            self.launch()
        self.assertFalse(any('init' in command for command, _ in self.calls))

    def test_failure_retains_configuration_and_never_deletes_initialized_storage(self):
        self.prepare()
        self.failure = 'up'
        with self.assertRaises(RuntimeError) as failure:
            self.launch()
        self.assertNotIn('synthetic-private-setting', str(failure.exception))
        self.assertNotIn('synthetic-secret-output', str(failure.exception))
        self.assertTrue((self.target / '.env').is_file())
        self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'initialized')
        self.assertFalse(any('down' in command or 'rm' in command for command, _ in self.calls))
        with self.assertRaisesRegex(ValueError, 'already been initialized'):
            self.launch()

    def test_interrupted_initializer_does_not_mark_household_as_initialized(self):
        self.prepare()
        self.failure = 'init'
        with self.assertRaises(RuntimeError):
            self.launch()
        self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'prepared')
        self.assertFalse(any('up' in command or 'down' in command for command, _ in self.calls))

    def test_prepare_only_cli_and_generated_manager_work_without_source_imports(self):
        result = subprocess.run([sys.executable, str(SOURCE), str(self.target), '--prepare-only',
                                 '--backend-image', self.images['backend_image'],
                                 '--frontend-image', self.images['frontend_image']], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('manage.py launch', result.stdout)
        result = subprocess.run([sys.executable, str(self.target / 'manage.py'), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('status', result.stdout)
        original = (self.target / '.env').read_bytes()
        result = subprocess.run([sys.executable, str(SOURCE), str(self.target), '--build'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('never overwritten', result.stderr)
        self.assertEqual((self.target / '.env').read_bytes(), original)

    def test_build_command_uses_unique_images_and_launches_only_the_new_installation(self):
        real_prepare = setup.prepare
        def record_preparation(*args, **kwargs):
            manifest = real_prepare(*args, **kwargs)
            self.manifest = manifest
            project = manifest['instance_id']
            self.effective = {'name': project,
                              'services': {name: {'volumes': [{'type': 'volume'}]}
                                           for name in ('backend', 'frontend', 'admin')},
                              'volumes': {name: {'name': project + '_' + name}
                                          for name in ('data', 'uploads', 'keys', 'backups')}}
            return manifest
        with patch.object(sys, 'argv', [str(SOURCE), str(self.target), '--build']), \
                patch.object(setup, 'prepare', side_effect=record_preparation), \
                patch.object(setup.subprocess, 'run', side_effect=self.docker), \
                patch('sys.stdout', new_callable=io.StringIO):
            setup.install_main()
        commands = [command for command, _ in self.calls]
        builds = [command for command in commands if command[1] == 'build']
        self.assertEqual(len(builds), 2)
        for service, command in zip(('backend', 'frontend'), builds):
            self.assertEqual(command[command.index('-t') + 1], self.manifest[service + '_image'])
            self.assertRegex(self.manifest[service + '_image'], '^ledger-setup-' + service + ':[a-f0-9]{16}$')
            self.assertEqual(command[-1], str(SOURCE.parent.parent / service))
        self.assertFalse(any('pull' in command for command in commands))
        self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'running')

    def test_token_renewal_restores_running_state_even_when_enrolled_owner_refuses_it(self):
        self.prepare()
        self.manifest['phase'] = 'initialized'
        for running, failure in ((True, None), (False, None), (True, 'renew-token')):
            self.running, self.failure, self.calls = running, failure, []
            with self.subTest(running=running, failure=failure), \
                    patch.object(setup.subprocess, 'run', side_effect=self.docker), \
                    patch('sys.stdout', new_callable=io.StringIO) as printed:
                if failure:
                    with self.assertRaisesRegex(ValueError, 'enrolled owner'):
                        setup.renew_token(self.target, self.manifest)
                    self.assertNotIn('synthetic-enrollment-token', printed.getvalue())
                else:
                    setup.renew_token(self.target, self.manifest)
                    self.assertIn('synthetic-enrollment-token', printed.getvalue())
            commands = [command for command, _ in self.calls]
            self.assertEqual(any('stop' in command for command in commands), running)
            self.assertEqual(any('start' in command for command in commands), running)
            self.assertFalse(any('init' in command or 'up' in command for command in commands))
        self.manifest['phase'] = 'prepared'
        with self.assertRaisesRegex(ValueError, 'finish initialization'):
            setup.renew_token(self.target, self.manifest)

    def test_build_retry_resumes_only_uninitialized_source_installations(self):
        self.prepare()
        self.manifest['source_checkout'] = str(SOURCE.parent.parent)
        setup.save_manifest(self.target, self.manifest)
        self.failure = 'build'
        with patch.object(setup.subprocess, 'run', side_effect=self.docker), \
                patch('sys.stdout', new_callable=io.StringIO), self.assertRaises(RuntimeError):
            setup.build_images(self.target, self.manifest)
        self.assertEqual(json.loads((self.target / 'instance.json').read_text())['phase'], 'prepared')
        self.failure = None
        with patch.object(setup, '__file__', str(self.target / 'manage.py')), \
                patch.object(sys, 'argv', ['manage.py', 'build']), \
                patch.object(setup.subprocess, 'run', side_effect=self.docker), \
                patch('sys.stdout', new_callable=io.StringIO):
            setup.manage_main()
        restored = json.loads((self.target / 'instance.json').read_text())
        self.assertEqual(restored['phase'], 'running')
        self.assertEqual(restored['instance_id'], self.manifest['instance_id'])
        self.assertEqual(restored['backend_image'], self.manifest['backend_image'])
        with self.assertRaisesRegex(ValueError, 'initialized'):
            setup.build_images(self.target, restored)
        self.manifest.pop('source_checkout')
        with self.assertRaisesRegex(ValueError, 'explicit images'):
            setup.build_images(self.target, self.manifest)


if __name__ == '__main__':
    unittest.main()
