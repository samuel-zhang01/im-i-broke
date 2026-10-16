"""Verify Docker excludes private artifacts using synthetic scratch images.

This copies only each working-tree Docker ignore policy into a temporary context.
It never builds app images, runs containers, reads household data or loads .env.
Requires a local Linux Docker engine. Scratch COPY builds do not execute code.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def check(context_name: str) -> int:
    tag = 'ledger-context-check:' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='ledger-context-check-') as temporary:
        directory = Path(temporary)
        context = directory / 'context'
        context.mkdir()
        (context / '.dockerignore').write_bytes((ROOT / context_name / '.dockerignore').read_bytes())
        (context / 'Dockerfile').write_text('FROM scratch\nCOPY . /probe/\n')
        prefix = 'public' if context_name == 'frontend' else 'app'
        required = {
            'frontend': ['package.json', 'package-lock.json', 'index.html', 'tsconfig.json',
                         'tsconfig.node.json', 'vite.config.ts', 'tailwind.config.js',
                         'postcss.config.js', 'nginx.conf', 'nginx-main.conf',
                         'security-headers.conf', 'api-proxy.conf', 'access-policy.conf',
                         'src/normal.ts', 'public/normal.svg', 'public/nested/normal.svg'],
            'backend': ['requirements.txt', 'entrypoint.sh', 'app/normal.py',
                        'app/nested/normal.py', 'scripts/normal.py'],
        }
        allowed = [*required[context_name], 'Dockerfile']
        forbidden = ['.env', 'output/diagnostic.log', 'tests/example.py']
        suffixes = ('.key', '.age', '.pem', '.identity', '.p12', '.pfx', '.jks', '.keystore', '.crt',
                    '.db', '.db-wal', '.sqlite', '.sqlite3-shm', '.csv', '.pdf', '.tar.gz', '.zip',
                    '.bundle', '.log', '.bak', '.tmp', '.local')
        forbidden += [prefix + '/nested/synthetic' + suffix for suffix in suffixes]
        forbidden += [prefix + '/nested/.env', prefix + '/nested/.env.production']
        forbidden += [prefix + '/nested/' + name + '/synthetic.txt'
                      for name in ('secrets', 'data', 'Data', 'uploads', 'backups', 'output', 'node_modules')]
        for name in [*allowed[:-1], *forbidden]:
            path = context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Disposable synthetic context fixture.\n')
        try:
            subprocess.run(['docker', 'build', '--progress=plain', '-t', tag, str(context)],
                           capture_output=True, check=True, timeout=90)
            archive = directory / 'image.tar'
            subprocess.run(['docker', 'image', 'save', '-o', str(archive), tag],
                           capture_output=True, check=True, timeout=90)
            observed = set()
            with tarfile.open(archive) as image:
                manifest = json.load(image.extractfile('manifest.json'))
                for name in manifest[0]['Layers']:
                    with tarfile.open(fileobj=image.extractfile(name)) as layer:
                        observed.update(member.name.removeprefix('probe/') for member in layer if member.isfile())
            leaked = observed.intersection(forbidden)
            missing = set(allowed).difference(observed)
            if leaked or missing:
                raise RuntimeError(f'{context_name}: {len(leaked)} synthetic private files admitted; '
                                   f'{len(missing)} required fixtures missing.')
            print(f'{context_name}: {len(forbidden)} private/unneeded fixtures excluded; '
                  f'{len(allowed)} production fixtures retained.')
            return len(forbidden)
        finally:
            # Remove only this invocation's randomly named, volume-free scratch image.
            subprocess.run(['docker', 'image', 'rm', tag], capture_output=True, timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    # Reuse installer endpoint/engine checks before any Docker mutation.
    from setup import docker_ready
    try:
        docker_ready()
        for name in ('frontend', 'backend'):
            check(name)
    except (ValueError, RuntimeError, subprocess.SubprocessError, OSError) as error:
        parser.exit(1, f'Docker context check failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
