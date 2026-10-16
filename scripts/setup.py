"""Create and launch a new private household with Docker and Python's standard library.

Run from a reviewed source checkout: python3 scripts/setup.py ~/ledger-home --build
Existing directories, Compose projects and stored household data are never reused.
The generated manage.py remains usable without the source checkout.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit
import uuid


TOOL = 'ledger-private-setup'
ENV_PREFIXES = ('FINANCE_', 'LUNCH_FLOW_', 'SNAPTRADE_', 'INVESTMENT_PRICE_', 'COMPOSE_')


def private_path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Choose a destination without symbolic links.')
    return path


def clean_environment():
    """Only the selected private env file can configure this household."""
    return {key: value for key, value in os.environ.items() if not key.startswith(ENV_PREFIXES)}


def run(arguments, *, directory=None, timeout=600, visible=False):
    try:
        result = subprocess.run(arguments, cwd=directory, env=clean_environment(),
                                text=True, stdout=None if visible else subprocess.PIPE,
                                stderr=None if visible else subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        raise ValueError('Install Docker with the Compose plugin, then try again.') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('Docker timed out. The installation files and any new storage were preserved.') from None
    if result.returncode:
        # Compose diagnostics may contain private configuration. Do not echo it.
        raise RuntimeError('Docker command failed: ' + ' '.join(arguments[:3])
                           + '. Check Docker is running and the selected images are accessible.')
    return (result.stdout or '').strip()


def require_local_docker():
    """Loopback ports and private storage refer to this computer, not a remote daemon."""
    # Docker documents that DOCKER_CONTEXT takes precedence over DOCKER_HOST.
    context = os.environ.get('DOCKER_CONTEXT')
    endpoint = os.environ.get('DOCKER_HOST', '') if not context else ''
    if not endpoint:
        arguments = ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']
        if context:
            arguments.append(context)
        endpoint = run(arguments, timeout=30)
    try:
        parsed = urlsplit(endpoint)
        local = (not parsed.netloc and not parsed.query and not parsed.fragment
                 and ((parsed.scheme == 'unix' and parsed.path.startswith('/') and len(parsed.path) > 1)
                      or (parsed.scheme == 'npipe' and parsed.path.startswith('//./pipe/')
                          and len(parsed.path) > len('//./pipe/'))))
    except ValueError:
        local = False
    if not local:
        raise ValueError('This helper requires a local Docker Engine or Docker Desktop using Linux containers. '
                         'Select a local Unix socket or Windows named-pipe context and remove remote '
                         'DOCKER_HOST/DOCKER_CONTEXT overrides. For remote hosting, run setup on that host.')


def docker_ready():
    require_local_docker()
    run(['docker', 'compose', 'version'], timeout=30)
    if run(['docker', 'info', '--format', '{{.OSType}}'], timeout=30) != 'linux':
        raise ValueError('This app needs Linux containers. Switch Docker Desktop to Linux containers, then try again.')


def image_reference(value):
    if (not re.fullmatch(r'[a-z0-9][a-z0-9./_:-]*(?:@sha256:[a-f0-9]{64})?', value)
            or ('@sha256:' not in value and not re.fullmatch(r'.+:[a-z0-9_][a-z0-9_.-]{0,127}', value.rsplit('/', 1)[-1]))
            or value.endswith(':latest')):
        raise ValueError('Use matching versioned image tags or sha256 digests; latest is not a release pin.')
    return value


def require_matching_image_tags(backend_image, frontend_image):
    """A tagged pair must come from the same reviewed product release."""
    if '@sha256:' in backend_image or '@sha256:' in frontend_image:
        return
    if backend_image.rsplit(':', 1)[-1] != frontend_image.rsplit(':', 1)[-1]:
        raise ValueError('Backend and frontend image tags must name the same release.')


def choose_port(requested, parent):
    used = set()
    for path in parent.glob('*/instance.json'):
        try:
            used.add(json.loads(path.read_text()).get('private_port'))
        except (OSError, ValueError, AttributeError):
            continue
    choices = [requested] if requested is not None else range(8080, 8181)
    for port in choices:
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError('Choose a port between 1024 and 65535.')
        if port in used:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                continue
        return port
    raise ValueError('That port is in use or reserved by a sibling installation. Choose another with --port.')


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as file:
        file.write(content)


def compose(directory, *arguments, visible=False, timeout=600):
    return run(['docker', 'compose', '--env-file', str(directory / '.env'),
                '--profile', 'maintenance', '-f', str(directory / 'compose.yml'), *arguments],
               directory=directory, visible=visible, timeout=timeout)


def verify_configuration(directory, manifest):
    for name in ('.env', 'compose.yml', 'instance.json'):
        if not private_path(directory / name).is_file():
            raise ValueError('The installation is missing ' + name + '; restore its original configuration.')
    effective = json.loads(compose(directory, 'config', '--format', 'json'))
    project = manifest['instance_id']
    services = effective.get('services', {})
    if effective.get('name') != project or set(services) != {'backend', 'frontend', 'admin'}:
        raise ValueError('The configuration no longer matches this installation. Review it before continuing.')
    volumes = effective.get('volumes', {})
    if (set(volumes) != {'data', 'uploads', 'keys', 'backups'}
            or any(item.get('external') or item.get('driver_opts') or item.get('name') != project + '_' + name
                   for name, item in volumes.items())
            or any(mount['type'] != 'volume' for service in services.values() for mount in service.get('volumes', []))):
        raise ValueError('Refusing configuration that uses shared, external or host-mounted household storage.')
    return effective


def require_unused_project(manifest):
    project = manifest['instance_id']
    for arguments in (
        ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project],
        ['docker', 'network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project],
        ['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project],
    ):
        if run(arguments):
            raise ValueError('This project already has Docker resources. Initialization was refused; inspect them before recovery.')
    # A volume may predate or lack Compose labels. Never adopt one by name.
    names = set(run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines())
    if any(project + '_' + name in names for name in ('data', 'uploads', 'keys', 'backups')):
        raise ValueError('Existing storage uses this project name. Initialization was refused.')


def save_manifest(directory, manifest):
    temporary = directory / ('.instance-' + uuid.uuid4().hex + '.tmp')
    write_private(temporary, json.dumps(manifest, indent=2) + '\n')
    temporary.replace(directory / 'instance.json')


def launch(directory, manifest, *, images_built=False):
    if manifest.get('phase') != 'prepared':
        raise ValueError('This household has already been initialized. Use manage.py start to restart it.')
    docker_ready()
    verify_configuration(directory, manifest)
    require_unused_project(manifest)
    if not images_built:
        # --policy missing supports both explicit local tags and registry releases.
        print('Checking the selected application images…', flush=True)
        compose(directory, 'pull', '--policy', 'missing', 'backend', 'frontend', visible=True, timeout=1800)
    print('Creating your private household…', flush=True)
    compose(directory, 'run', '--rm', 'admin', 'python', '-m', 'scripts.installation', 'init')
    manifest['phase'] = 'initialized'
    save_manifest(directory, manifest)
    print('Starting the app. The first health check may take a minute…', flush=True)
    compose(directory, 'up', '-d', '--wait', '--wait-timeout', '180', 'backend', 'frontend', timeout=240)
    manifest['phase'] = 'running'
    save_manifest(directory, manifest)
    show_welcome(directory, manifest)


def show_welcome(directory, manifest):
    token = compose(directory, 'run', '--rm', 'admin', 'python', '-m', 'scripts.installation', 'token')
    print('\nYour app is ready: http://localhost:' + str(manifest['private_port']))
    print('\nOne-time setup token (valid for 24 hours):\n' + token)
    print('\nOpen the app, paste this token, choose a password, and add your authenticator.')
    print('Save the recovery codes when prompted. You can add banks later in Settings.')
    print('\nInstallation: ' + str(directory))
    print('Manage it: python3 ' + shlex.quote(str(directory / 'manage.py')) + ' --help')


def build_images(directory, manifest):
    if manifest.get('phase') != 'prepared':
        raise ValueError('This household is initialized. Use manage.py start; follow the upgrade guide for image changes.')
    if not manifest.get('source_checkout'):
        raise ValueError('This installation uses explicit images. Use manage.py launch to retry their download.')
    repository = private_path(manifest['source_checkout'])
    if not all((repository / service / 'Dockerfile').is_file() for service in ('backend', 'frontend')):
        raise ValueError('The original source checkout is unavailable at ' + str(repository) + '. Restore it before retrying the build.')
    for service in ('backend', 'frontend'):
        print('Building the ' + service + ' image from ' + str(repository) + '…', flush=True)
        run(['docker', 'build', '-t', manifest[service + '_image'], str(repository / service)],
            directory=repository, visible=True, timeout=3600)


def renew_token(directory, manifest):
    if manifest.get('phase') not in ('initialized', 'running'):
        raise ValueError('The household must finish initialization before its setup token can be renewed.')
    running = 'backend' in compose(directory, 'ps', '--status', 'running', '--services').splitlines()
    if running:
        compose(directory, 'stop', 'backend')
    try:
        token = compose(directory, 'run', '--rm', 'admin', 'python', '-m', 'scripts.installation', 'renew-token')
    except RuntimeError:
        raise ValueError('Token renewal failed. It is available only while this new household is awaiting setup. '
                         'An enrolled owner must use terminal owner recovery.') from None
    finally:
        if running:
            compose(directory, 'start', '--wait', '--wait-timeout', '180', 'backend', timeout=240)
    print('New one-time setup token (valid for 24 hours):\n' + token)


def prepare(destination, *, backend_image, frontend_image, port=None, repository=None):
    destination = private_path(destination)
    if destination.exists():
        raise ValueError('Choose a new directory. Existing installations and even empty target directories are never overwritten.')
    if not destination.parent.is_dir():
        raise ValueError('Create the parent directory first, then choose a new installation directory inside it.')
    backend_image, frontend_image = image_reference(backend_image), image_reference(frontend_image)
    require_matching_image_tags(backend_image, frontend_image)
    repository = repository or Path(__file__).resolve().parent.parent
    source = repository / 'deploy' / 'compose.product.yml'
    if not source.is_file():
        raise ValueError('Run setup.py from a complete reviewed source checkout.')
    port = choose_port(port, destination.parent)
    manifest = {'format': 1, 'tool': TOOL, 'instance_id': 'ledger-home-' + uuid.uuid4().hex[:16],
                'private_port': port, 'isolation': 'one-household-per-instance', 'phase': 'prepared',
                'backend_image': backend_image, 'frontend_image': frontend_image}
    destination.mkdir(mode=0o700)
    try:
        write_private(destination / 'compose.yml', source.read_text())
        # No personal or operator provider credentials are needed for setup.
        env = {'FINANCE_INSTANCE_ID': manifest['instance_id'], 'FINANCE_BACKEND_IMAGE': backend_image,
               'FINANCE_FRONTEND_IMAGE': frontend_image, 'FINANCE_PORT': str(port),
               'FINANCE_DEPLOYMENT': 'local', 'FINANCE_COOKIE_SECURE': 'false'}
        write_private(destination / '.env', ''.join(f'{key}={value}\n' for key, value in env.items()))
        write_private(destination / 'instance.json', json.dumps(manifest, indent=2) + '\n')
        write_private(destination / 'manage.py', Path(__file__).read_text())
        write_private(destination / 'README.txt',
            'Your private Am I Broke? installation\n\n'
            f'Browser: http://localhost:{port}\n'
            'Run these commands from this directory (Python 3 and Docker required):\n'
            '  python3 manage.py status   # Running containers\n'
            '  python3 manage.py start    # Restart an initialized household\n'
            '  python3 manage.py stop     # Stop the app, retaining all data\n'
            '  python3 manage.py token    # Show the unused setup token again\n'
            '  python3 manage.py renew-token  # Replace an expired unused setup token\n'
            '  python3 manage.py check    # Check installation readiness\n'
            '  python3 manage.py logs     # Recent application logs\n'
            '  python3 manage.py launch   # Initialize a prepared-only installation\n\n'
            'If a source build failed, python3 manage.py build retries it and launches the app.\n'
            'The app is reachable only from this computer. Add banks through Settings.\n'
            'Keep this private directory; it identifies your Docker storage.\n'
            'No banking credentials are required in .env. Do not use docker compose down -v.\n'
            'For backups, upgrades and HTTPS hosting, see docs/DEPLOYMENT.md in the source checkout.\n')
    except BaseException:
        # No Docker operations have run: removing only our new partial files is safe.
        shutil.rmtree(destination)
        raise
    return manifest


def install_main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('destination', type=Path, help='A new private installation directory; must not already exist')
    parser.add_argument('--build', action='store_true', help='Build both images from this reviewed checkout, then launch')
    parser.add_argument('--backend-image', help='Explicit matching versioned backend image (alternative to --build)')
    parser.add_argument('--frontend-image', help='Explicit matching versioned frontend image (alternative to --build)')
    parser.add_argument('--port', type=int, help='Optional loopback port; otherwise selects a free port from 8080–8180')
    parser.add_argument('--prepare-only', action='store_true', help='Write configuration only; needs both explicit image references')
    args = parser.parse_args()
    if args.build:
        if args.backend_image or args.frontend_image or args.prepare_only:
            parser.error('--build cannot be combined with image references or --prepare-only')
        suffix = uuid.uuid4().hex[:16]
        args.backend_image = 'ledger-setup-backend:' + suffix
        args.frontend_image = 'ledger-setup-frontend:' + suffix
    elif not args.backend_image or not args.frontend_image:
        parser.error('Choose --build, or provide both --backend-image and --frontend-image.')
    destination = None
    try:
        # Refuse existing targets before consulting or changing Docker.
        target = private_path(args.destination)
        if target.exists():
            raise ValueError('Choose a new directory; existing destinations are never overwritten.')
        if not args.prepare_only:
            docker_ready()
        manifest = prepare(target, backend_image=args.backend_image, frontend_image=args.frontend_image, port=args.port)
        destination = target
        if args.prepare_only:
            print('Prepared your private installation: ' + str(destination))
            print('Launch when ready: python3 ' + shlex.quote(str(destination / 'manage.py')) + ' launch')
            return
        if args.build:
            manifest['source_checkout'] = str(Path(__file__).resolve().parent.parent)
            save_manifest(destination, manifest)
            build_images(destination, manifest)
        launch(destination, manifest, images_built=args.build)
    except (ValueError, OSError, RuntimeError) as error:
        message = 'Setup stopped: ' + str(error)
        if destination is not None:
            message += '\nYour new installation files and any storage were preserved at ' + str(destination)
            message += '\nUse manage.py status to inspect it. If initialization completed, manage.py start can retry startup.'
            message += '\nIf a source build failed, manage.py build retries the build and launch from the original checkout.'
        parser.exit(1, message + '\n')


def manage_main():
    parser = argparse.ArgumentParser(description='Manage this private household. All commands retain financial data.')
    parser.add_argument('command', choices=('launch', 'build', 'start', 'stop', 'status', 'token', 'renew-token', 'check', 'logs'))
    args = parser.parse_args()
    try:
        directory = private_path(Path(__file__).absolute().parent)
        manifest = json.loads(private_path(directory / 'instance.json').read_text())
        if manifest.get('tool') != TOOL or manifest.get('format') != 1:
            raise ValueError('This is not an installation created by the private setup helper.')
        if args.command == 'launch':
            launch(directory, manifest)
            return
        docker_ready()
        verify_configuration(directory, manifest)
        if args.command == 'build':
            if manifest.get('phase') != 'prepared':
                raise ValueError('This household is initialized. Use manage.py start; follow the upgrade guide for image changes.')
            require_unused_project(manifest)
            build_images(directory, manifest)
            launch(directory, manifest, images_built=True)
        elif args.command == 'start':
            if manifest.get('phase') not in ('initialized', 'running'):
                raise ValueError('Setup has not completed. Use manage.py launch for an unused prepared installation.')
            compose(directory, 'up', '-d', '--wait', '--wait-timeout', '180', 'backend', 'frontend', timeout=240)
            print('App ready: http://localhost:' + str(manifest['private_port']))
        elif args.command == 'stop':
            compose(directory, 'stop', 'backend', 'frontend')
            print('App stopped. All household data has been retained.')
        elif args.command == 'status':
            print(compose(directory, 'ps'))
        elif args.command == 'token':
            print(compose(directory, 'run', '--rm', 'admin', 'python', '-m', 'scripts.installation', 'token'))
        elif args.command == 'renew-token':
            renew_token(directory, manifest)
        elif args.command == 'check':
            print(compose(directory, 'exec', '-T', 'backend', 'python', '-m', 'scripts.doctor'))
        elif args.command == 'logs':
            compose(directory, 'logs', '--tail', '100', 'backend', 'frontend', visible=True)
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        parser.exit(1, 'Command stopped: ' + str(error) + '\n')


if __name__ == '__main__':
    os.umask(0o077)
    if Path(__file__).name == 'manage.py':
        manage_main()
    else:
        install_main()
