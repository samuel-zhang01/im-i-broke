"""Prepare an isolated household's deployment files without starting services.

Each generated project has separate application/auth storage, keys, uploads,
backup volumes, container network and private port. No shared financial database.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import uuid


def provision(destination, *, household, hostname, port, backend_image, frontend_image, compose_source):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', household):
        raise ValueError('Household identifier must be 1–40 lowercase letters, digits or hyphens, starting with a letter.')
    if (not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', hostname)
            or '.' not in hostname or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in hostname.split('.'))):
        raise ValueError('Use a public DNS hostname without a scheme, path, port or wildcard.')
    if not 1024 <= port <= 65535:
        raise ValueError('Use a private upstream port between 1024 and 65535.')
    for value in (backend_image, frontend_image):
        if (not re.fullmatch(r'[a-z0-9][a-z0-9./_:-]*(?:@sha256:[a-f0-9]{64})?', value)
                or ('@sha256:' not in value and not re.fullmatch(r'.+:[a-z0-9_][a-z0-9_.-]{0,127}', value.rsplit('/', 1)[-1]))
                or value.endswith(':latest')):
            raise ValueError('Use versioned lowercase image references or immutable digests; latest is not a release pin.')
    if ('@sha256:' not in backend_image and '@sha256:' not in frontend_image
            and backend_image.rsplit(':', 1)[-1] != frontend_image.rsplit(':', 1)[-1]):
        raise ValueError('Backend and frontend image tags must name the same release.')
    destination = Path(destination).absolute()
    if any(part.is_symlink() for part in (destination, *destination.parents)):
        raise ValueError('Deployment destinations must not contain symlinks.')
    if destination.exists():
        raise ValueError('Choose a new destination; existing deployment files are never overwritten.')
    for manifest_path in destination.parent.glob('*/instance.json'):
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('hostname') == hostname or manifest.get('private_port') == port:
            raise ValueError('Another generated instance in this directory already uses that hostname or port.')
    instance_id = f'ledger-{household}-{uuid.uuid4().hex[:10]}'
    manifest = {'format': 1, 'instance_id': instance_id, 'hostname': hostname, 'private_port': port,
                'isolation': 'one-household-per-instance', 'backend_image': backend_image, 'frontend_image': frontend_image}
    destination.mkdir(mode=0o700, parents=True)
    try:
        shutil.copyfile(compose_source, destination / 'compose.yml')
        env = {'FINANCE_INSTANCE_ID': instance_id, 'FINANCE_BACKEND_IMAGE': backend_image,
               'FINANCE_FRONTEND_IMAGE': frontend_image, 'FINANCE_BIND_ADDRESS': '127.0.0.1',
               'FINANCE_PORT': str(port), 'FINANCE_DEPLOYMENT': 'public', 'FINANCE_COOKIE_SECURE': 'true',
               'FINANCE_PUBLIC_ORIGIN': f'https://{hostname}',
               'FINANCE_ALLOWED_HOSTS': f'{hostname},localhost,127.0.0.1,backend'}
        (destination / '.env').write_text(''.join(f'{name}={value}\n' for name, value in env.items()))
        (destination / '.env').chmod(0o600)
        (destination / 'instance.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (destination / 'README.txt').write_text(
            'Run all commands from this directory. No services have been started.\n'
            '1. docker compose pull\n'
            '2. docker compose run --rm admin python -m scripts.installation init\n'
            '3. docker compose run --rm admin python -m scripts.installation token\n'
            f'4. Configure the HTTPS proxy for {hostname} to 127.0.0.1:{port}, preserving Host.\n'
            '5. docker compose up -d --wait backend frontend\n'
            '6. Complete browser enrollment using the private setup token.\n'
            '7. docker compose exec -T backend python -m scripts.doctor\n'
            'Back up each household and its separate keys independently. Never use down -v.\n'
            'See docs/DEPLOYMENT.md for backup, upgrade and restore instructions.\n')
    except BaseException:
        shutil.rmtree(destination)
        raise
    return manifest


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--household', required=True)
    parser.add_argument('--hostname', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--backend-image', required=True)
    parser.add_argument('--frontend-image', required=True)
    parser.add_argument('--compose-source', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'deploy' / 'compose.product.yml')
    args = vars(parser.parse_args())
    destination = args.pop('destination')
    try:
        print(json.dumps(provision(destination, **args), indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, f'Provisioning failed: {error}\n')


if __name__ == '__main__':
    main()
