"""Opt-in matched backups for an explicit standalone product deployment.

No schedules are installed. The caller supplies the private Compose directory,
age public recipient and output directory. Each completed set contains encrypted
financial/auth/uploads data, the separately encrypted installation key and
encrypted configuration. This helper never invokes installation or migration.
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
import subprocess
import tarfile
import time
import uuid

TOOL = 'ledger-distribution-backup'
FILES = ('data.age', 'key.age', 'configuration.age')


def safe_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Backup paths must not contain symlinks.')
    return path


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


@contextmanager
def lock(directory, project):
    path = directory / ('.' + TOOL + '-' + project + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A backup for this household is already running.') from None
        yield
    finally:
        os.close(fd)


def completed_manifest(directory, project):
    """Retention never selects arbitrary, incomplete, foreign or symlink trees."""
    if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith('ledger-set-'):
        return None
    try:
        members = list(directory.iterdir())
        if {item.name for item in members} != {*FILES, 'manifest.json'} or any(item.is_symlink() or not item.is_file() for item in members):
            return None
        manifest = json.loads((directory / 'manifest.json').read_text())
        if (not isinstance(manifest, dict) or manifest.get('tool') != TOOL or manifest.get('format') != 1
                or manifest.get('project') != project or manifest.get('complete') is not True
                or not isinstance(manifest.get('completed_at'), (int, float))
                or set(manifest.get('files', {})) != set(FILES)):
            return None
        for name in FILES:
            expected = manifest['files'][name]
            if expected['bytes'] != (directory / name).stat().st_size or expected['sha256'] != digest(directory / name):
                return None
        return manifest
    except (OSError, ValueError, TypeError, KeyError):
        return None


def retain(directory, project, *, keep, current):
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ValueError('Keep at least one completed backup set.')
    verified = [(path, manifest) for path in directory.iterdir()
                if path != current and (manifest := completed_manifest(path, project)) is not None]
    verified.sort(key=lambda item: (item[1]['completed_at'], item[0].name), reverse=True)
    removed = 0
    for path, _ in verified[max(0, keep - 1):]:
        shutil.rmtree(path)
        removed += 1
    return removed


def configuration_archive(directory, env_file, effective):
    members = {'compose.yml': (directory / 'compose.yml').read_bytes(),
               '.env': env_file.read_bytes(),
               'effective-compose.json': json.dumps(effective, indent=2).encode()}
    if (directory / 'instance.json').is_file():
        members['instance.json'] = safe_path(directory / 'instance.json').read_bytes()
    if sum(map(len, members.values())) > 4 * 1024 * 1024:
        raise ValueError('Configuration is unexpectedly large; inspect it before backing up.')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, value in members.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return stream.getvalue()


def backup(directory, output, recipient, *, keep=14, env_file=None):
    directory, output = safe_path(directory), safe_path(output)
    env_file = safe_path(env_file or directory / '.env')
    compose_file = safe_path(directory / 'compose.yml')
    if not compose_file.is_file() or not env_file.is_file():
        raise ValueError('Provide the explicit product Compose directory and its existing private environment file.')
    if not re.fullmatch(r'age1[a-z0-9]{10,190}', recipient):
        raise ValueError('Provide an age public recipient, never a private identity.')
    if not 1 <= keep <= 10000:
        raise ValueError('Retention must keep between 1 and 10,000 completed sets.')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The explicitly selected env file is authoritative; unrelated shell
    # household settings cannot redirect this operation to another deployment.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('FINANCE_', 'SNAPTRADE_', 'LUNCH_FLOW_', 'INVESTMENT_PRICE_', 'COMPOSE_'))}
    # Compose config omits profiled services unless enabled; administration is
    # still invoked only by explicit run commands below, never compose up.
    base = ['docker', 'compose', '--env-file', str(env_file), '--profile', 'maintenance', '-f', str(compose_file)]

    def command(*args, input_data=None, output_file=None):
        result = subprocess.run([*base, *args], cwd=directory, env=environment,
                                input=input_data, stdout=output_file if output_file else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600)
        if result.returncode:
            # Compose can echo rejected environment values: no subprocess
            # output is copied to logs when real household credentials exist.
            raise RuntimeError('Backup command failed during ' + args[0] + '; inspect the selected deployment privately.')
        return result.stdout or b''

    effective = json.loads(command('config', '--format', 'json'))
    project = effective.get('name', '')
    services = effective.get('services', {})
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,100}', project) or not {'backend', 'frontend', 'admin'} <= services.keys():
        raise ValueError('This helper requires a standalone product Compose deployment with an explicit project name.')
    if (any(item.get('external') or not item.get('name', '').startswith(project + '_') for item in effective.get('volumes', {}).values())
            or any(mount['type'] != 'volume' for service in services.values() for mount in service.get('volumes', []))):
        raise ValueError('This helper accepts only that product project\'s own named volumes. Back up custom recovery/bind mounts explicitly.')
    base.extend(['-p', project])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    ident = stamp + '-' + uuid.uuid4().hex[:16]
    target, stage = output / ('ledger-set-' + ident), output / ('.partial-ledger-set-' + ident)
    remote = '/app/backups/distribution-' + ident
    remote_files = [remote + '-' + name for name in FILES]
    # Lock the deployment itself: choosing another output directory must not
    # allow two schedules to stop/restart the same household concurrently.
    with lock(directory, project):
        stage.mkdir(mode=0o700)
        published = False
        was_running = False
        try:
            was_running = bool(command('ps', '--status', 'running', '-q', 'backend').strip())
            if was_running:
                command('stop', 'backend')
            command('run', '--rm', '-T', 'admin', 'python', '-m', 'scripts.secure_backup', 'create',
                    '--data', '/app/data', '--uploads', '/app/uploads', '--key', '/app/keys/auth.key',
                    '--recipient', recipient, '--output', remote_files[0])
            command('run', '--rm', '-T', 'admin', 'age', '--encrypt', '--recipient', recipient,
                    '--output', remote_files[1], '/app/keys/auth.key')
            command('run', '--rm', '-T', 'admin', 'age', '--encrypt', '--recipient', recipient,
                    '--output', remote_files[2], input_data=configuration_archive(directory, env_file, effective))
            for name, source in zip(FILES, remote_files):
                with (stage / name).open('xb') as destination:
                    os.fchmod(destination.fileno(), 0o600)
                    command('run', '--rm', '-T', 'admin', 'cat', source, output_file=destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                if not (stage / name).stat().st_size:
                    raise ValueError('An encrypted backup member was empty.')
            manifest = {'tool': TOOL, 'format': 1, 'project': project, 'complete': True,
                        'completed_at': time.time(), 'backend_image': services['backend']['image'],
                        'frontend_image': services['frontend']['image'],
                        'files': {name: {'bytes': (stage / name).stat().st_size, 'sha256': digest(stage / name)} for name in FILES}}
            with (stage / 'manifest.json').open('x') as destination:
                os.fchmod(destination.fileno(), 0o600)
                json.dump(manifest, destination, indent=2)
                destination.write('\n'); destination.flush(); os.fsync(destination.fileno())
            os.rename(stage, target)
            published = True
            directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                if was_running:
                    command('start', 'backend')
            finally:
                if not published:
                    shutil.rmtree(stage, ignore_errors=True)
                # These exact, generated temporary ciphertext paths belong only
                # to this backup; never enumerate or delete other remote files.
                command('run', '--rm', '-T', 'admin', 'python', '-c',
                        'from pathlib import Path; import sys; [Path(name).unlink(missing_ok=True) for name in sys.argv[1:]]', *remote_files)
        removed = retain(output, project, keep=keep, current=target)
    return {'ok': True, 'project': project, 'backup_set': str(target), 'retained_limit': keep,
            'expired_sets_removed': removed, 'backend_resumed': was_running}


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--keep', type=int, default=14)
    args = parser.parse_args()
    try:
        print(json.dumps(backup(args.directory, args.output, args.recipient, keep=args.keep, env_file=args.env_file), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Backup failed: {error}\n')
