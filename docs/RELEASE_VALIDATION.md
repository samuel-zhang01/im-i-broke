# Public snapshot validation

Prepared on 16 October 2026 using disposable synthetic storage and no real
provider credentials. These are this public candidate's own results; private
predecessor receipts are not carried forward as release acceptance.

| Check | Result |
| --- | --- |
| Repository layout / local links | 585 tracked files; zero issues |
| Institution assets | 53 local PNGs and 43 catalog integrations validated; bundled images inspected as artwork/logos |
| Backend | 1,238 tests passed in 54.118s on Python 3.13.9; zero skips or ResourceWarnings, with age and checksum-pinned Gitleaks integration |
| Frontend | 156 unit tests passed; production build passed on Node 22.22.2 |
| Fresh-owner browser setup | 8/8 passed, desktop/phone Chromium and WebKit, 33.4s |
| Authentication/recovery | 1/1 Chromium browser scenario passed, 8.5s |
| Focused UI | 40/40 desktop/phone Chromium checks passed, including Settings, dialogs, text enlargement and shared layout, 22.5s |
| Published sample CSV | Actual bundled file imported three rows with £2,000 income/£47 spending; repeated preview recognized all three duplicates |
| Docker inputs | Both scratch build-input probes passed; 34 private/unneeded fixtures excluded per context |
| Secrets | Checksum-pinned Gitleaks 8.30.1; final exported source and new Git history scanned before push |
| Local distribution build | Blocked by this host's AppArmor container-init error at the first Docker RUN; no image-install/restore pass claimed from this attempt |
| GitHub-hosted CI | [Passed on the initial public runtime](https://github.com/samuel-zhang01/im-i-broke/actions/runs/38930948149): 1,238 production-image backend tests in 43.490s, 156 frontend units, both production builds, fresh installation/enrollment, upgrade rehearsal and complete encrypted recovery |
| Repository protections | Public visibility and AGPL recognized; secret scanning/push protection, private vulnerability reporting and dependency alerts enabled; no private runners or repository secrets; main requires synthetic CI and blocks force pushes/deletion |

The hosted smoke gate used new named volumes and an internal-only backend
network. It verified CSV identity/reimport, joint ownership, restored financial
rows and encrypted connection credentials, data/key/configuration backups, and
preserved the original session after backup. Provider requests were disabled;
its random project and storage were removed. Subsequent documentation-only
commits do not change that runtime. See the latest
[main CI](https://github.com/samuel-zhang01/im-i-broke/actions/workflows/ci.yml)
for verification of later commits.

The source snapshot uses a fresh root commit and no predecessor Git objects,
branches or tags. Only committed source was exported. Private storage,
configuration, operational narratives and household-derived merchant examples
were excluded or replaced. Scanner passes are not proof that arbitrary future
examples contain no personal information.

The npm lockfile audit reported six high and two moderate development advisories;
production-only npm audit reported zero. Outstanding chains include
[braces pattern handling](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm),
[selector parsing](https://github.com/advisories/GHSA-rj75-hqrm-r3gf) and
[source maps](https://github.com/advisories/GHSA-68fv-2mgg-jv7q). These are not
marked repaired. A Tailwind major migration needs its own visual/browser gates.
The production build retains a large-chart-chunk warning; the backend test
client retains its upstream Starlette/httpx deprecation notice.

Full browser-engine suites on this public revision, live providers, external
DNS/TLS, physical devices and assistive technology are outside the local focused
verification above. See [known limitations](KNOWN_LIMITATIONS.md).
