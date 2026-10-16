"""Update this host's existing finance app without replacing household storage.

Default: fast-forward origin/main, run source checks, build a matching image pair,
back up the stopped household, rehearse migrations on a copy and verify activation.
Use --check for read-only inspection, or --local to deploy a clean local revision.
This updates root-Compose installations; fresh households use scripts/setup.py.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

import setup as installation

ROOT = Path(__file__).resolve().parents[1]
AUTO_PINS = Path('deploy/compose.auto.yml')
IMAGE_SERVICES = ('backend', 'frontend', 'backup', 'auth-admin', 'storage-init', 'import')
TOOL = 'ledger-host-deployment'


def run(arguments, *, directory=ROOT, input_data=None, output_file=None, visible=False, timeout=600, operation=None):
    """Keep private Compose configuration and diagnostics out of terminal logs."""
    operation = operation or arguments[0]
    try:
        result = subprocess.run(arguments, cwd=directory, env=installation.clean_environment(),
                                input=input_data, stdout=output_file if output_file is not None else None if visible else subprocess.PIPE,
                                stderr=None if visible else subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Command timed out during ' + operation + '; inspect the deployment privately.') from None
    if result.returncode:
        raise RuntimeError('Command failed during ' + operation + ' (exit ' + str(result.returncode)
                           + '); private command output was withheld.')
    return result.stdout or b''


def private_path(path):
    return installation.private_path(path)


@contextmanager
def lock(root, inherited=None):
    # The Git directory keeps the deployment lock out of the working tree.
    location = Path(run(['git', 'rev-parse', '--git-common-dir'], directory=root).decode().strip())
    if not location.is_absolute():
        location = root / location
    location = private_path(location)
    path = location / 'finance-deploy.lock'
    descriptor = inherited if inherited is not None else os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        selected, actual = os.fstat(descriptor), path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(selected.st_mode) or selected.st_uid != os.getuid()
                or selected.st_mode & 0o077 or (selected.st_dev, selected.st_ino) != (actual.st_dev, actual.st_ino)):
            raise ValueError('The deployment lock is not a private regular file for this checkout.')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another deployment is running from this checkout.') from None
        yield descriptor
    finally:
        os.close(descriptor)


def clean_checkout(root):
    top = Path(run(['git', 'rev-parse', '--show-toplevel'], directory=root).decode().strip())
    if top != root:
        raise ValueError('Run deploy.sh from the root of its Git checkout.')
    if run(['git', 'status', '--porcelain', '--untracked-files=normal'], directory=root).strip():
        raise ValueError('Commit or move local changes before deploying; the checkout was not overwritten.')
    revision = run(['git', 'rev-parse', 'HEAD'], directory=root).decode().strip()
    if not re.fullmatch(r'[a-f0-9]{40,64}', revision):
        raise ValueError('Could not determine the reviewed Git revision.')
    return revision


def inspect_container(ident):
    # Do not request full Docker inspection: Config.Env can contain bank keys.
    expression = '{{json .Config.Labels}}\n{{json .Mounts}}\n{{json .NetworkSettings.Ports}}\n{{json .Image}}\n{{json .State}}\n{{json .NetworkSettings.Networks}}\n{{json .Name}}\n{{json .Config.Hostname}}'
    lines = run(['docker', 'inspect', '--format', expression, ident]).decode().splitlines()
    if len(lines) != 8:
        raise ValueError('Unexpected Docker inspection response.')
    labels, mounts, ports, image, state, networks, name, hostname = map(json.loads, lines)
    return {'id': ident, 'labels': labels or {}, 'mounts': mounts, 'ports': ports,
            'image': image, 'state': state, 'networks': networks, 'name': name, 'hostname': hostname}


class Deployment:
    def __init__(self, root, project, files, backend, frontend):
        self.root, self.project, self.files = root, project, files
        self.backend, self.frontend = backend, frontend
        self.env_file = private_path(root / '.env')
        if not self.env_file.is_file():
            raise ValueError('The existing installation needs its private .env file; none was created.')

    def compose(self, *arguments, **options):
        command = ['docker', 'compose', '--env-file', str(self.env_file), '-p', self.project,
                   '--profile', 'maintenance']
        for file in self.files:
            command.extend(['-f', str(file)])
        return run([*command, *arguments], directory=self.root, **options)

    def configuration(self):
        return json.loads(self.compose('config', '--format', 'json'))


def discover(root, project=None):
    identifiers = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.service=backend']).decode().split()
    candidates = []
    for ident in identifiers:
        current = inspect_container(ident)
        labels = current['labels']
        if labels.get('com.docker.compose.project.working_dir') == str(root) and (
                project is None or labels.get('com.docker.compose.project') == project):
            candidates.append(current)
    if len(candidates) != 1:
        raise ValueError('Expected one existing finance backend for this checkout. '
                         'Use --project for an ambiguous deployment; fresh instances use scripts/setup.py.')
    backend = candidates[0]
    labels = backend['labels']
    project = labels.get('com.docker.compose.project', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,100}', project):
        raise ValueError('The existing Compose project name is invalid.')
    files = [private_path(path) for path in labels.get('com.docker.compose.project.config_files', '').split(',') if path]
    if not files or files[0] != root / 'docker-compose.yml':
        raise ValueError('This updater requires the existing root docker-compose.yml installation.')
    for file in files:
        if not file.is_relative_to(root) or not file.is_file():
            raise ValueError('Existing Compose files must be regular files in this checkout.')
    auto = root / AUTO_PINS
    if auto in files and files[-1] != auto:
        raise ValueError('The automatic image pin file must be the last Compose overlay.')
    frontend_ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project,
                        '--filter', 'label=com.docker.compose.service=frontend']).decode().split()
    if len(frontend_ids) != 1:
        raise ValueError('Expected exactly one existing frontend in this Compose project.')
    frontend = inspect_container(frontend_ids[0])
    for key in ('com.docker.compose.project.working_dir', 'com.docker.compose.project.config_files'):
        if frontend['labels'].get(key) != labels.get(key):
            raise ValueError('The backend and frontend use different deployment configuration; inspect them first.')
    deployment = Deployment(root, project, files, backend, frontend)
    validate_storage(deployment, deployment.configuration())
    return deployment


def validate_storage(deployment, config):
    services = config.get('services', {})
    if config.get('name') != deployment.project or not {'backend', 'frontend', 'backup', 'auth-admin'} <= services.keys():
        raise ValueError('The existing configuration is not a supported finance root Compose project.')
    volumes = {}
    for container in (deployment.backend, deployment.frontend):
        service = container['labels']['com.docker.compose.service']
        for item in services[service].get('volumes', []):
            target = item['target']
            actual = next((mount for mount in container['mounts'] if mount['Destination'] == target), None)
            if not actual or actual['Type'] != item['type']:
                raise ValueError('Configured mounts differ from the existing containers; no update was applied.')
            if actual.get('RW') != (not bool(item.get('read_only'))):
                raise ValueError('Configured mount permissions differ from the existing containers.')
            if item['type'] == 'volume':
                declared = config.get('volumes', {}).get(item['source'], {})
                name = declared.get('name')
                if declared.get('external') or not name or not name.startswith(deployment.project + '_') or actual.get('Name') != name:
                    raise ValueError('Refusing changed, external or shared household storage.')
                if service == 'backend':
                    volumes[target] = name
            else:
                if item['type'] != 'bind' or service != 'frontend' or not item.get('read_only'):
                    raise ValueError('Custom data mounts need an explicit deployment procedure.')
                source = private_path(item['source'])
                if str(source) != actual['Source'] or not source.is_relative_to(deployment.root) or not source.is_file():
                    raise ValueError('Frontend bind mounts must be matching regular files inside this checkout.')
        if {item['target'] for item in services[service].get('volumes', [])} != {item['Destination'] for item in container['mounts']}:
            raise ValueError('Existing container mounts differ from the requested configuration.')
        wanted_networks = {}
        for key, endpoint in services[service].get('networks', {'default': {}}).items():
            name = config.get('networks', {}).get(key, {}).get('name', deployment.project + '_' + key)
            wanted_networks[name] = (endpoint or {}).get('aliases', [])
        actual_networks = container.get('networks', {})
        number = container['labels'].get('com.docker.compose.container-number', '1')
        automatic_aliases = {service, container.get('name', '').lstrip('/'), container.get('hostname', ''),
                             container['id'], container['id'][:12],
                             deployment.project + '-' + service + '-' + number,
                             deployment.project + '_' + service + '_' + number}
        if set(wanted_networks) != set(actual_networks):
            raise ValueError('Configured networks or proxy aliases differ from the existing containers.')
        for name, aliases in wanted_networks.items():
            actual_aliases = set(actual_networks[name].get('Aliases') or [])
            if not set(aliases) <= actual_aliases or actual_aliases - automatic_aliases != set(aliases) - automatic_aliases:
                raise ValueError('Configured networks or proxy aliases differ from the existing containers.')
    if set(volumes) != {'/app/data', '/app/uploads', '/app/keys'}:
        raise ValueError('Expected separate financial, uploads and authentication-key volumes.')
    backup = config.get('volumes', {}).get('backups', {})
    if backup.get('external') or backup.get('name') != deployment.project + '_backups':
        raise ValueError('Expected this project’s own backup volume.')
    if len(set([*volumes.values(), backup['name']])) != 4:
        raise ValueError('Financial data, uploads, keys and backups must use four distinct volumes.')
    # Never change this installation's listener while refreshing application code.
    expected_ports = {}
    for port in services['frontend'].get('ports', []):
        key = str(port['target']) + '/' + port.get('protocol', 'tcp')
        expected_ports.setdefault(key, []).append({'HostIp': port.get('host_ip', '0.0.0.0'), 'HostPort': str(port['published'])})
    actual_ports = {key: value for key, value in deployment.frontend['ports'].items() if value}
    if expected_ports != actual_ports:
        raise ValueError('Configured frontend ports differ from the running installation; inspect the change first.')
    return {**volumes, '/app/backups': backup['name']}


def tool_container(image, volumes=None):
    command = ['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '--pids-limit', '96', '--memory', '1g',
               '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777']
    for target, volume in (volumes or {}).items():
        command.extend(['--mount', 'type=volume,source=' + volume + ',target=' + target
                        + ('' if target == '/app/backups' else ',readonly')])
    return [*command, image]


def verify_image_runtime(images):
    # Check the images as their configured non-root users without household
    # volumes, credentials or network access before pausing the existing app.
    run([*tool_container(images['backend']), 'python', '-c',
         'import app.main; import scripts.secure_backup; import scripts.verify_upgrade'],
        operation='backend image runtime check')
    frontend = tool_container(images['frontend'])
    # nginx validates the proxy hostname without contacting any real backend.
    frontend[-1:-1] = ['--add-host', 'backend:127.0.0.1']
    run([*frontend, 'sh', '-ec',
         'nginx -t; test -r /usr/share/nginx/html/index.html; '
         'find /usr/share/nginx/html -type f -exec cat {} + > /dev/null'],
        operation='frontend image runtime check')


def atomic_private(path, data):
    path = private_path(path)
    with tempfile.NamedTemporaryFile(prefix='.deploy-', dir=path.parent, delete=False) as temporary:
        candidate = Path(temporary.name)
        try:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(data); temporary.flush(); os.fsync(temporary.fileno())
            candidate.replace(path)
        finally:
            candidate.unlink(missing_ok=True)


def recipient_key(output, image, supplied=None):
    if supplied:
        if not re.fullmatch(r'age1[a-z0-9]{10,190}', supplied):
            raise ValueError('Use a public age recipient, never an identity/private key.')
        return supplied, None
    identity = private_path(output / 'recovery.identity')
    if not identity.exists():
        data = run([*tool_container(image), 'age-keygen'])
        descriptor = os.open(identity, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as destination:
            destination.write(data)
    if identity.stat().st_mode & 0o077:
        raise ValueError('The local recovery identity must have private 0600 permissions.')
    if not identity.is_file() or identity.stat().st_uid != os.getuid():
        raise ValueError('The local recovery identity must be a regular file owned by this user.')
    # Derive the public recipient using age rather than trusting editable comments.
    recipient = run([*tool_container(image), 'age-keygen', '-y'], input_data=identity.read_bytes()).decode().strip()
    if not re.fullmatch(r'age1[a-z0-9]{10,190}', recipient):
        raise ValueError('The local recovery identity is invalid.')
    return recipient, identity


def configuration_archive(deployment, config):
    contents = {'.env': deployment.env_file.read_bytes(),
                'effective-compose.json': json.dumps(config).encode(),
                'old-images.json': json.dumps({'backend': deployment.backend['image'], 'frontend': deployment.frontend['image']}).encode()}
    paths = list(deployment.files)
    paths.extend(private_path(item['source']) for item in config['services']['frontend'].get('volumes', [])
                 if item['type'] == 'bind')
    for path in paths:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Deployment configuration is unexpectedly large.')
        contents[path.relative_to(deployment.root).as_posix()] = path.read_bytes()
    if sum(map(len, contents.values())) > 8 * 1024 * 1024:
        raise ValueError('Deployment configuration is unexpectedly large.')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, value in contents.items():
            entry = tarfile.TarInfo(name); entry.mode = 0o600; entry.size = len(value)
            archive.addfile(entry, io.BytesIO(value))
    return stream.getvalue()


def backup(deployment, config, output, recipient, revision, *, image):
    ident = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    stage = output / ('.partial-' + ident)
    target = output / ('recovery-' + ident)
    stage.mkdir(mode=0o700)
    volumes = validate_storage(deployment, config)
    # Use the reviewed tools with current backup fixes while recording the old
    # running image pair for recovery. Household mounts remain read-only.
    prefix = tool_container(image, volumes)
    remote = '/app/backups/deploy-' + ident
    paths = [remote + '-' + name for name in ('data.age', 'key.age', 'configuration.age')]
    try:
        run([*prefix, 'python', '-m', 'scripts.secure_backup', 'create', '--data', '/app/data',
             '--uploads', '/app/uploads', '--offline-source', '--key', '/app/keys/auth.key', '--recipient', recipient, '--output', paths[0]],
            timeout=1800, operation='encrypted database and uploads backup')
        run([*prefix, 'age', '--encrypt', '--recipient', recipient, '--output', paths[1], '/app/keys/auth.key'],
            operation='separate authentication-key encryption')
        run([*prefix, 'age', '--encrypt', '--recipient', recipient, '--output', paths[2]],
            input_data=configuration_archive(deployment, config), operation='deployment-configuration encryption')
        digests = {}
        for source in paths:
            name = source.rsplit('-', 1)[-1]
            # configuration.age contains no '-' in its member name.
            file = stage / name
            descriptor = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as destination:
                run([*prefix, 'cat', source], output_file=destination, timeout=1800, operation='encrypted recovery export')
                destination.flush(); os.fsync(destination.fileno())
            digest = hashlib.sha256()
            with file.open('rb') as ciphertext:
                if ciphertext.read(22) != b'age-encryption.org/v1\n':
                    raise ValueError('Backup output is empty or not an age ciphertext.')
                ciphertext.seek(0)
                for block in iter(lambda: ciphertext.read(1024 * 1024), b''):
                    digest.update(block)
            digests[name] = {'bytes': file.stat().st_size, 'sha256': digest.hexdigest()}
        receipt = {'tool': TOOL, 'format': 1, 'complete': True, 'project': deployment.project,
                   'candidate_revision': revision, 'backend_image': deployment.backend['image'],
                   'frontend_image': deployment.frontend['image'], 'recipient': recipient, 'files': digests}
        atomic_private(stage / 'manifest.json', (json.dumps(receipt, indent=2) + '\n').encode())
        stage.rename(target)
        return target
    except BaseException:
        shutil.rmtree(stage)
        raise
    finally:
        # Remove only ciphertext scratch files allocated by this invocation.
        try:
            run([*prefix, 'python', '-c', 'from pathlib import Path; import sys; [Path(p).unlink(missing_ok=True) for p in sys.argv[1:]]', *paths],
                operation='encrypted scratch cleanup')
        except Exception:
            # Ciphertext leftovers must not hide the primary failure or discard
            # a complete recovery set that is already safely exported.
            print('Encrypted scratch cleanup failed; encrypted temporary files may remain in the backup volume.',
                  file=sys.stderr)


def image_id(reference):
    return run(['docker', 'image', 'inspect', '--format', '{{.Id}}', reference]).decode().strip()


def apply(deployment, revision, *, recipient=None, output=None, timeout=180):
    config = deployment.configuration()
    volumes = validate_storage(deployment, config)
    if not deployment.backend['state'].get('Running') or not deployment.frontend['state'].get('Running'):
        raise ValueError('The existing app must be running before an automatic update; use its normal start command first.')
    output = private_path(output or deployment.root / 'output' / 'deployment-backups')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077:
        raise ValueError('The recovery directory must have private 0700 permissions.')
    if not output.is_dir() or output.stat().st_uid != os.getuid():
        raise ValueError('The recovery directory must be owned by this user.')
    tag = revision[:16]
    images = {service: deployment.project + '-' + service + ':git-' + tag for service in ('backend', 'frontend')}
    print('Checking tracked source and institution assets…', flush=True)
    run([sys.executable, str(deployment.root / 'scripts/check_repository.py')])
    run([sys.executable, str(deployment.root / 'scripts/sync_institution_logos.py'), '--check'])
    for service, image in images.items():
        print('Building ' + service + ' for ' + tag + '…', flush=True)
        run(['docker', 'build', '--label', 'org.opencontainers.image.revision=' + revision,
             '-t', image, str(deployment.root / service)], visible=True, timeout=3600)
    expected = {service: image_id(image) for service, image in images.items()}
    print('Checking candidate images as their runtime users…', flush=True)
    verify_image_runtime(images)
    public_recipient, identity = recipient_key(output, deployment.backend['image'], recipient)
    if identity:
        print('Recovery identity: ' + str(identity) + ' — keep a private offline copy.', flush=True)
    auto = private_path(deployment.root / AUTO_PINS)
    previous = auto.read_bytes() if auto.exists() else None
    stopped, activated = False, False
    recovery = None
    try:
        print('Stopping the backend for a matched encrypted recovery set…', flush=True)
        # A Docker timeout can occur after the container has already stopped.
        stopped = True
        deployment.compose('stop', 'backend')
        recovery = backup(deployment, config, output, public_recipient, revision, image=images['backend'])
        print('Recovery set saved: ' + str(recovery), flush=True)
        print('Rehearsing migration on a private copy…', flush=True)
        run([*tool_container(images['backend'], {'/app/data': volumes['/app/data']}),
             'python', '-m', 'scripts.verify_upgrade', '/app/data/finance.db', '--offline-source'],
            timeout=600, operation='offline migration rehearsal')
        pins = {'services': {name: {'image': images['frontend' if name == 'frontend' else 'backend']}
                             for name in IMAGE_SERVICES}}
        atomic_private(auto, (json.dumps(pins, indent=2) + '\n').encode())
        if auto not in deployment.files:
            deployment.files.append(auto)
        updated = deployment.configuration()
        validate_storage(deployment, updated)
        for name in IMAGE_SERVICES:
            if name in updated['services'] and updated['services'][name]['image'] != pins['services'][name]['image']:
                raise ValueError('Compose did not apply the matching image pair.')
        print('Starting candidate images and waiting for health checks…', flush=True)
        # After this boundary a candidate may migrate the live database.
        activated = True
        deployment.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', str(timeout),
                           'backend', 'frontend', timeout=timeout + 60)
        active = {}
        for service in ('backend', 'frontend'):
            ids = deployment.compose('ps', '-q', service).decode().split()
            if len(ids) != 1:
                raise ValueError('Expected one running ' + service + ' container.')
            current = inspect_container(ids[0])
            if current['image'] != expected[service] or current['state'].get('Health', {}).get('Status') != 'healthy':
                raise ValueError('The ' + service + ' container is unhealthy or running a different image.')
            active[service] = current
        verified = Deployment(deployment.root, deployment.project, deployment.files,
                              active['backend'], active['frontend'])
        validate_storage(verified, updated)
        # Check the frontend's proxy, not just the backend's own health endpoint.
        deployment.compose('exec', '-T', 'frontend', 'wget', '-q', '-O', '/dev/null', 'http://127.0.0.1:8080/api/health')
        mode = updated['services']['backend'].get('environment', {}).get('FINANCE_DEPLOYMENT', 'local')
        if mode != 'trusted-lan':
            deployment.compose('exec', '-T', 'backend', 'python', '-m', 'scripts.doctor')
        receipt = {'tool': TOOL, 'format': 1, 'revision': revision, 'project': deployment.project,
                   'images': images, 'image_ids': expected, 'recovery_set': str(recovery), 'verified': True}
        atomic_private(output / 'last-deployment.json', (json.dumps(receipt, indent=2) + '\n').encode())
        print('Deployment verified: ' + deployment.project + ' at ' + tag, flush=True)
        print('Existing ports, proxy settings, provider configuration and household volumes retained.', flush=True)
        return receipt
    except BaseException:
        if activated:
            # Image rollback is unsafe after a schema migration. Preserve recovery.
            try:
                deployment.compose('stop', 'backend', 'frontend')
            except Exception:
                print('Could not stop the failed candidate; inspect this Compose project immediately.', file=sys.stderr)
            print('Candidate activation failed. Recovery set: ' + str(recovery)
                  + '. Restore into new private storage with the previous images; see docs/BACKUP.md.', file=sys.stderr)
        else:
            if previous is None:
                auto.unlink(missing_ok=True)
            else:
                atomic_private(auto, previous)
            if stopped:
                # Start the exact old container; never recreate it with candidate pins.
                run(['docker', 'start', deployment.backend['id']])
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--local', action='store_true', help='Deploy the current clean revision without fetching')
    parser.add_argument('--check', action='store_true', help='Inspect the existing project/storage; no fetch, build, stop or deployment')
    parser.add_argument('--project', help='Existing Compose project, only needed if discovery is ambiguous')
    parser.add_argument('--backup-recipient', default=os.environ.get('FINANCE_DEPLOY_BACKUP_RECIPIENT'), help='Public age recipient; default creates a private local recovery identity')
    parser.add_argument('--backup-dir', type=Path, help='Private recovery directory; default output/deployment-backups')
    parser.add_argument('--timeout', type=int, default=180, help='Health wait seconds (30–1800; default 180)')
    parser.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 10):
            raise ValueError('Python 3.10+ is required.')
        if not 30 <= args.timeout <= 1800:
            raise ValueError('Health timeout must be between 30 and 1800 seconds.')
        for command in ('git', 'docker'):
            if not shutil.which(command):
                raise ValueError(command + ' is required.')
        installation.docker_ready()
        root = private_path(ROOT)
        if args.check:
            deployment = discover(root, args.project)
            print('Existing project: ' + deployment.project)
            print('Compose files: ' + ', '.join(str(path.relative_to(root)) for path in deployment.files))
            print('Storage and listener match the existing containers. No deployment performed.')
            return
        with lock(root, args.lock_fd) as descriptor:
            revision = clean_checkout(root)
            # Validate the target before any remote checkout change.
            discover(root, args.project)
            if not args.local:
                branch = run(['git', 'symbolic-ref', '--short', '-q', 'HEAD']).decode().strip()
                if branch != 'main':
                    raise ValueError('Automatic synchronization requires main. Use --local for a reviewed branch.')
                print('Fetching origin/main…', flush=True)
                run(['git', 'fetch', 'origin', 'main'], timeout=180)
                try:
                    run(['git', 'merge-base', '--is-ancestor', 'HEAD', 'refs/remotes/origin/main'])
                except RuntimeError:
                    raise ValueError('Local main is ahead or diverged; no local commits were overwritten.') from None
                run(['git', 'merge', '--ff-only', 'refs/remotes/origin/main'])
                # Keep one exclusive lock across synchronization and re-exec.
                os.set_inheritable(descriptor, True)
                os.execv(sys.executable, [sys.executable, str(ROOT / 'scripts/deploy.py'), '--local',
                                         '--lock-fd', str(descriptor), *sys.argv[1:]])
            apply(discover(root, args.project), revision, recipient=args.backup_recipient,
                  output=args.backup_dir, timeout=args.timeout)
    except (ValueError, OSError, RuntimeError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, 'Deployment stopped: ' + str(error) + '\n')


if __name__ == '__main__':
    os.umask(0o077)
    main()
