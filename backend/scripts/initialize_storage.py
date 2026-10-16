"""Offline Docker volume permission/key migration. No symlinks are followed."""
import os
from pathlib import Path
import stat


def initialize(roots=(Path('/app/data'), Path('/app/uploads'), Path('/app/keys')), uid=10001, gid=10001):
    entries = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        entries.extend([root, *root.rglob('*')])
    # Validate the entire tree before changing ownership.
    if any(not (stat.S_ISREG(p.lstat().st_mode) or stat.S_ISDIR(p.lstat().st_mode)) for p in entries):
        raise ValueError('Refusing special files or symlinks in Ledger storage.')
    legacy, new = roots[0] / 'auth.key', roots[2] / 'auth.key'
    if legacy.exists():
        if new.exists() and legacy.read_bytes() != new.read_bytes():
            raise ValueError('Conflicting auth keys. Resolve manually before migration.')
        if not new.exists():
            with new.open('xb') as output:
                output.write(legacy.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            entries.append(new)
        legacy.unlink()
        entries.remove(legacy)
    for entry in entries:
        os.chown(entry, uid, gid, follow_symlinks=False)
        entry.chmod(0o700 if entry.is_dir() else 0o600)


if __name__ == '__main__':
    os.umask(0o077)
    initialize()
    print('Ledger storage is private and owned by UID 10001. Authentication keys are in the separate key volume.')
