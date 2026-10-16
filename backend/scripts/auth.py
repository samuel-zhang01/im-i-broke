"""Run from backend: python -m scripts.auth setup (or reset). Secrets stay in terminal."""
import argparse
from getpass import getpass
import pyotp
from app.auth import database, enroll


def main():
    parser = argparse.ArgumentParser(description='Enroll/reset the single Am I Broke? owner locally.')
    parser.add_argument('action', choices=['setup', 'reset'])
    args = parser.parse_args()
    with database() as conn:
        exists = conn.execute('SELECT 1 FROM owner').fetchone() is not None
    if exists and args.action == 'setup':
        parser.error('Already configured. Use reset to replace credentials and revoke all sessions.')
    if args.action == 'reset' and input('Replace credentials and revoke all sessions? Type RESET: ') != 'RESET':
        return
    password = getpass('New password (15–256 characters): ')
    if not 15 <= len(password) <= 256:
        parser.error('Password must have 15–256 characters.')
    if password != getpass('Repeat password: '):
        parser.error('Passwords do not match.')
    secret = pyotp.random_base32()
    print('\nIn your authenticator, add a time-based account named Am I Broke? (6 digits, 30 seconds).')
    print('Enter this setup key manually:', secret)
    print('Keep this key private. Do not paste it into chat or a QR-code website.')
    code = getpass('Current authenticator code: ')
    try:
        codes = enroll(password, secret, code, reset=args.action == 'reset')
    except ValueError as error:
        parser.error(str(error))
    print('\nSave these single-use recovery codes in your password manager or offline. Shown only once:')
    print('\n'.join(codes))
    print('\nSetup complete. Login with your password and the NEXT authenticator code.')


if __name__ == '__main__':
    main()
