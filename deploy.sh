#!/usr/bin/env bash
set -euo pipefail

# Run on the deployment host after committing/pushing code from another machine.
deployment_checkout="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$deployment_checkout"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10+ is required. See docs/AUTOMATIC_DEPLOYMENT.md." >&2
  exit 1
fi
exec python3 "$deployment_checkout/scripts/deploy.py" "$@"
