"""Disposable, synthetic Ledger for browser tests. Never opens the user's database."""
import argparse
import json
from datetime import date, timedelta
import os
from pathlib import Path
import sys
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def isolate_environment():
    """Never inherit a developer's provider access or background refresh jobs."""
    for name in ('LUNCH_FLOW_API', 'SNAPTRADE_ID', 'SNAPTRADE_KEY',
                 'LUNCH_FLOW_CLIENT_ID', 'LUNCH_FLOW_CLIENT_SECRET',
                 'SNAPTRADE_COMMERCIAL_ID', 'SNAPTRADE_COMMERCIAL_KEY'):
        os.environ[name] = ''
    for name in ('LUNCH_FLOW_SYNC_SECONDS', 'SNAPTRADE_SYNC_SECONDS',
                 'INVESTMENT_PRICE_REFRESH_SECONDS'):
        os.environ[name] = '0'
    os.environ['FINANCE_DEPLOYMENT'] = 'local'
    os.environ['FINANCE_PUBLIC_ORIGIN'] = ''
    os.environ['FINANCE_ALLOWED_HOSTS'] = 'localhost,127.0.0.1,testserver'


def seed(path):
    from app.db import init_db
    from app.categorize import apply_categorization
    from app.analytics import recompute_all

    conn = init_db(path)
    conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('qa-current','QA Bank','current','QA Current')")
    conn.execute("INSERT INTO accounts(id,bank,account_type,display_name) VALUES('qa-credit','QA Bank','credit','QA Credit')")
    today = date.today()

    def transaction(name, amount, when=today, account='qa-current', code='VIS'):
        conn.execute('INSERT INTO transactions(account_id,txn_date,description,merchant,source_merchant,amount,type_code,dedupe_key,raw) VALUES(?,?,?,?,?,?,?,?,?)',
                     (account, when.isoformat(), name, name, name, amount, code,
                      f'qa-{conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]}', name))

    def merchant(name, category):
        ident = conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source) VALUES(?,'merchant',?,'normal','user')", (name, category)).lastrowid
        conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,'exact','spending')", (ident, name))

    for name, category in [('QA Stream', 'Subscriptions'), ('QA Annual', 'Subscriptions'),
                           ('QA Legal', 'Legal services'), ('QA Cafe', 'Eating out'),
                           ('QA Equipment', 'Shopping')]:
        merchant(name, category)
    for offset in range(4):
        month_index = today.year * 12 + today.month - 1 - offset
        transaction('QA Stream', -12, date(month_index // 12, month_index % 12 + 1, 1))
    transaction('QA Annual', -80)
    transaction('QA Legal', -140)
    transaction('QA Cafe', -30)
    transaction('QA Cafe', 5)
    transaction('QA Salary', 2500, code='CR')
    # A reviewable spike with a genuinely earlier, account-specific baseline.
    for offset in range(1, 6):
        transaction('QA Equipment', -10, today-timedelta(days=offset))
    transaction('QA Equipment', -600)
    for index in range(65):
        transaction(f'QA Unclassified {index:02}', -10-index, today-timedelta(days=index))
    for project in ('desktop', 'mobile'):
        transaction(f'QA Review {project}', -31)
        transaction(f'QA Match {project}', -21)
        transaction(f'QA Match {project}', -22, today-timedelta(days=7))
        transaction(f'QA Merge {project}', -21)
        transaction(f'QA Merge {project}', -22, today-timedelta(days=7))
        transaction(f'QA Own Transfer {project}', -300, code=None)
        transaction(f'QA Own Transfer {project}', -400, today-timedelta(days=7), code=None)
        transaction(f'QA Friend {project} Shared dinner', -100, code='bank_feed')
        transaction(f'QA Friend {project} Shared dinner', -20, today-timedelta(days=7), code='bank_feed')
    apply_categorization(conn)
    recompute_all(conn)
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8018)
    parser.add_argument('--auth', action='store_true', help='Enable synthetic authentication fixtures')
    parser.add_argument('--fresh', action='store_true', help='Empty isolated installation awaiting browser enrollment')
    parser.add_argument('--fixture-file', type=Path, help='Exclusive private file for the synthetic setup token; requires --fresh')
    args = parser.parse_args()
    if args.fresh != bool(args.fixture_file) or (args.fresh and args.auth):
        parser.error('--fresh requires --fixture-file and cannot be combined with --auth')
    isolate_environment()
    with tempfile.TemporaryDirectory(prefix='ledger-e2e-') as temporary:
        # Keep a browser run on one immutable build. A concurrent Vite build
        # replaces dist and otherwise causes intermittent missing pages/chunks.
        frontend = Path(temporary) / 'frontend'
        shutil.copytree(Path(os.environ.get('FINANCE_E2E_DIST', ROOT / 'frontend' / 'dist')), frontend)
        if not (frontend / 'index.html').is_file():
            raise RuntimeError('Build the frontend before starting browser tests.')
        path = Path(temporary) / 'data' / 'synthetic.db'
        os.environ['FINANCE_DB_PATH'] = str(path)
        os.environ['FINANCE_UPLOADS_PATH'] = str(Path(temporary) / 'uploads')
        os.environ['FINANCE_AUTH_ENABLED'] = 'true' if args.auth or args.fresh else 'false'
        os.environ['FINANCE_COOKIE_SECURE'] = 'false'
        os.environ['FINANCE_AUTH_DB_PATH'] = str(path.parent / 'auth.db')
        os.environ['FINANCE_AUTH_KEY_PATH'] = str(Path(temporary) / 'keys' / 'auth.key')
        if args.fresh:
            from scripts.installation import initialize, setup_token
            initialize(path, Path(os.environ['FINANCE_UPLOADS_PATH']), Path(os.environ['FINANCE_AUTH_DB_PATH']), Path(os.environ['FINANCE_AUTH_KEY_PATH']))
            descriptor = os.open(args.fixture_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as fixture:
                json.dump({'token': setup_token()}, fixture)
        else:
            path.parent.mkdir()
            seed(path)
        if args.auth:
            import pyotp
            from app.auth import enroll, database, digest
            secret = pyotp.random_base32()
            enroll('Synthetic browser test password!', secret, pyotp.TOTP(secret).now())
            with database() as conn:
                conn.execute('DELETE FROM recovery')
                conn.executemany('INSERT INTO recovery VALUES(?)',
                                 [(digest(f'{index:024x}'),) for index in range(10)])
                conn.commit()
        import uvicorn
        from app.main import app
        from fastapi.staticfiles import StaticFiles
        app.router.routes = [route for route in app.router.routes
                             if getattr(route, 'name', None) != 'frontend'
                             and not (getattr(route, 'name', None) == 'root' and getattr(route, 'path', None) == '/')]
        app.mount('/', StaticFiles(directory=str(frontend), html=True), name='frontend')
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
