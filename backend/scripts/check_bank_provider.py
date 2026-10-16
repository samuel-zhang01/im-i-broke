"""Read-only provider setup check; never requests access to bank accounts."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.bank_provider import EnableBanking, ProviderError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application-id', required=True)
    parser.add_argument('--private-key', type=Path, required=True,
                        help='Path to the provider PEM private key. Never paste the key itself.')
    args = parser.parse_args()
    try:
        result = EnableBanking(args.application_id, args.private_key).check_hsbc()
    except ProviderError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result['metadata_checks_passed'] else 2


if __name__ == '__main__':
    sys.exit(main())
