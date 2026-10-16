# Synthetic continuous integration

[Synthetic application CI](../.github/workflows/ci.yml) runs on pushes to `main`,
pull requests and manual dispatch. All public workflows use disposable
GitHub-hosted Ubuntu runners. They have read-only repository permissions and
remove checkout credentials before tests. There are no private runner labels,
provider secrets, deployment keys or production storage mounts.

The reproducible Linux entry point is `bash scripts/ci.sh` with Python 3.12 or newer for host tools,
local Docker, Git, curl, tar and SHA-256 tools. It exports committed source,
checks local documentation and institution assets, runs checksum-pinned Gitleaks
against Git history and source, and verifies Docker build-input exclusions.
It builds both production images, runs all backend and frontend units in
network-disabled containers, and performs image-only installation, enrollment,
CSV import, upgrade rehearsal and encrypted data/key/configuration recovery.
Only its random Compose project, volumes and unique image tags are removed.

The smoke backend has an internal-only network, absent or synthetic provider
credentials, and disabled schedulers. A separate frontend bridge exposes only a
random localhost HTTP port for the harness. The job never discovers an existing
household or invokes `deploy.sh`.

The manually dispatched [validation workflow](../.github/workflows/validate.yml)
also runs full Chromium, configured WebKit and Firefox, first-owner setup and
authentication/recovery browser suites. Use it on an intended release revision.
The [manual image release](../.github/workflows/release.yml) accepts a reviewed
version tag from `main`, validates that revision, and publishes matching amd64
and arm64 images only after validation. No registry images are promised until
those workflows complete and their immutable pins are recorded in a release.

A push, pull request or version tag does not deploy any installation or publish
images. Do not attach private self-hosted runners to this public repository.
Contribution workflows use `pull_request`, never `pull_request_target`, so fork
code receives no write token or private secrets.

Passing CI applies to the checked revision and synthetic Linux environment.
Live providers, external TLS, physical devices and assistive technology require
separate evidence. See [release validation](RELEASE_VALIDATION.md) and
[known limitations](KNOWN_LIMITATIONS.md).
