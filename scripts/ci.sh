#!/usr/bin/env bash
# Synthetic CI only. Never invoke deploy.sh, load .env, or discover an installation.
set -euo pipefail
umask 077

cd "$(git rev-parse --show-toplevel)"
python3 -c 'import sys; assert sys.version_info[:2] >= (3, 12), "CI host tools require Python 3.12 or newer"'
python3 scripts/check_repository.py
python3 scripts/sync_institution_logos.py --check

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ledger-ci.XXXXXXXX")"
suffix="$(date -u +%Y%m%d%H%M%S)-$$"
backend="ledger-ci-backend:$suffix"
backend_test="ledger-ci-backend-test:$suffix"
frontend="ledger-ci-frontend:$suffix"
frontend_build="ledger-ci-frontend-build:$suffix"
cleanup() {
  docker image rm "$backend_test" "$frontend_build" "$backend" "$frontend" >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Export committed source only: ignored storage/configuration cannot enter a test.
mkdir "$work/source" "$work/home"
git archive HEAD | tar -x -C "$work/source"
# Docker COPY preserves modes; the non-root test user must read exported code.
# The enclosing temporary directory stays private, and only tracked source is here.
chmod -R a+rX "$work/source"
curl --fail --silent --show-error --location --output "$work/gitleaks.tar.gz" \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
printf '%s  %s\n' '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb' \
  "$work/gitleaks.tar.gz" | sha256sum --check --status
tar -xzf "$work/gitleaks.tar.gz" -C "$work" gitleaks
"$work/gitleaks" git --redact=100 --config .gitleaks.toml --log-opts=--all .
"$work/gitleaks" dir --redact=100 --config .gitleaks.toml "$work/source"

python3 scripts/check_docker_contexts.py
docker build --progress=plain -t "$backend" "$work/source/backend"
docker build --progress=plain --build-arg "BACKEND_IMAGE=$backend" \
  -f "$work/source/.github/ci/backend.Dockerfile" -t "$backend_test" "$work/source/.github/ci"
docker build --progress=plain --target build -t "$frontend_build" "$work/source/frontend"
docker build --progress=plain -t "$frontend" "$work/source/frontend"

# No host environment, private volumes, Docker socket, or network enters units.
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus 2 --memory 2g --pids-limit 256 --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
  --mount "type=bind,source=$work/source,target=/source,readonly" \
  --mount "type=bind,source=$work/gitleaks,target=/opt/gitleaks,readonly" \
  --workdir /source/backend --entrypoint python \
  --env PYTHONPATH=/source/backend --env GITLEAKS_BINARY=/opt/gitleaks \
  --env FINANCE_DB_PATH=/tmp/finance-ci.db --env FINANCE_AUTH_DB_PATH=/tmp/auth-ci.db \
  --env FINANCE_AUTH_KEY_PATH=/tmp/auth-ci.key \
  --env LUNCH_FLOW_SYNC_SECONDS=0 --env SNAPTRADE_SYNC_SECONDS=0 \
  --env INVESTMENT_PRICE_REFRESH_SECONDS=0 \
  "$backend_test" -m unittest discover -s tests -q
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus 2 --memory 1g --pids-limit 128 --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  --mount "type=bind,source=$work/source/frontend/tests,target=/app/tests,readonly" \
  --entrypoint sh "$frontend_build" -c 'node --test tests/*.test.mjs'

# The existing distribution harness gets a source-only temporary Compose file.
# The backend is internal-only. A separate frontend bridge permits the harness's
# random loopback HTTP port without giving the backend provider/network access.
python3 - "$work/source/deploy/compose.product.yml" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = path.read_text()
assert source.count('  frontend:\n') == 1 and '\nnetworks:\n' not in source
source = source.replace('  frontend:\n', '  frontend:\n    networks: [default, ci_http]\n')
path.write_text(source + '\nnetworks:\n  default:\n    internal: true\n  ci_http:\n    driver: bridge\n')
PY
python3 -m venv "$work/python"
"$work/python/bin/python" -m pip install --disable-pip-version-check pyotp==2.9.0
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$work/home" LANG=C.UTF-8 \
  "$work/python/bin/python" "$work/source/scripts/smoke_distribution.py" \
  --backend-image "$backend" --frontend-image "$frontend"
printf 'Synthetic CI passed for %s; no household deployment was performed.\n' "$(git rev-parse HEAD)"
