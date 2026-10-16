"""Check tracked repository content and local Markdown links without reading live data.

Run from any directory: python3 scripts/check_repository.py
Secret detection is separate: run Gitleaks on the tree and Git history before
releasing. The optional manual validation workflow also runs those scans.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


PRIVATE_ROOTS = ('Data/', 'backend/data/', 'backend/uploads/', 'output/', 'secrets/',
                 'analysis/', 'docs/archive/', 'docs/reviews/', 'private-archive/')
PRIVATE_FILES = {'PRODUCTION_HANDOFF.md', 'deploy/compose.release.yml',
                 'deploy/compose.auto.yml', 'deploy/lan-access.conf'}
PRIVATE_SUFFIXES = ('.db', '.db-wal', '.db-shm', '.db-journal', '.sqlite', '.sqlite3',
                    '.sqlite-wal', '.sqlite-shm', '.sqlite-journal', '.sqlite3-wal',
                    '.sqlite3-shm', '.sqlite3-journal', '.key', '.pem', '.age',
                    '.identity', '.p12', '.pfx', '.jks', '.keystore', '.bundle',
                    '.tar', '.tar.gz', '.zip')
FINANCIAL_SOURCE_SUFFIXES = ('.pdf', '.csv', '.tsv', '.ofx', '.qfx', '.qif',
                             '.xlsx', '.xls', '.ods')
PUBLIC_DATA_EXAMPLES = {'examples/transactions.csv'}
PERSONAL_PATH = re.compile(r'(?:/home/|/Users/)[a-zA-Z0-9_.-]+/|[A-Za-z]:\\Users\\[^\\\s]+\\')
LINK = re.compile(r'!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+"[^"]*")?\)')
HTML_LINK = re.compile(r'(?:href|src)=["\']([^"\']+)["\']')


def private_path(name: str) -> bool:
    basename = PurePosixPath(name).name.lower()
    env = (basename == '.env' or basename.startswith('.env.') or basename.endswith('.env'))
    return (name.startswith(PRIVATE_ROOTS) or name in PRIVATE_FILES
            or basename.endswith(PRIVATE_SUFFIXES)
            or (basename.endswith(FINANCIAL_SOURCE_SUFFIXES) and name not in PUBLIC_DATA_EXAMPLES)
            or (env and basename != '.env.example'))


def anchors(markdown: str) -> set[str]:
    result = set(re.findall(r'\bid=["\']([^"\']+)["\']', markdown))
    counts: dict[str, int] = {}
    for heading in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', markdown, flags=re.M):
        slug = re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(f'{slug}-{count}' if count else slug)
    return result


def check_repository(root: Path) -> tuple[int, list[str]]:
    entries = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root).decode().split('\0')
    files = {}
    issues = []
    for entry in filter(None, entries):
        metadata, name = entry.split('\t', 1)
        mode, _, stage = metadata.split()
        if stage != '0':
            issues.append(f'{name}: unresolved merge')
        if mode != '100644' and mode != '100755':
            issues.append(f'{name}: review non-regular tracked file')
            continue
        if private_path(name):
            issues.append(f'{name}: private artifact must not be tracked')
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            issues.append(f'{name}: missing file or unexpected symbolic link')
            continue
        files[name] = path
    texts = {}
    for name, path in files.items():
        try:
            value = path.read_text(encoding='utf-8')
        except UnicodeError:
            continue
        texts[name] = value
        if PERSONAL_PATH.search(value):
            issues.append(f'{name}: personal home-directory path')
    for name, content in texts.items():
        if not name.endswith('.md'):
            continue
        # Code examples are literal text, not documentation navigation.
        prose = re.sub(r'^```[^\n]*\n.*?^```\s*$', '', content, flags=re.M | re.S)
        for target in LINK.findall(prose) + HTML_LINK.findall(prose):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            target_path = (files[name].parent / unquote(parsed.path)).resolve() if parsed.path else files[name]
            try:
                relative = target_path.relative_to(root.resolve()).as_posix()
            except ValueError:
                issues.append(f'{name}: local link escapes repository')
                continue
            # Directory links are fine when the directory contains tracked files.
            if relative not in files:
                if parsed.fragment or not any(p.startswith(relative.rstrip('/') + '/') for p in files):
                    issues.append(f'{name}: missing tracked link target {relative}')
                continue
            if parsed.fragment and relative.endswith('.md'):
                if unquote(parsed.fragment) not in anchors(texts.get(relative, '')):
                    issues.append(f'{name}: missing section in {relative}#{parsed.fragment}')
    return len(files), sorted(set(issues))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    count, issues = check_repository(root)
    for issue in issues:
        print(issue)
    print(f'Repository check: {count} tracked files; {len(issues)} issue(s).')
    return int(bool(issues))


if __name__ == '__main__':
    raise SystemExit(main())
