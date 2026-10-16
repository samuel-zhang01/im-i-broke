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
| Docker inputs | Both scratch build-input probes passed; 34 private/unneeded fixtures excluded per context |
| Secrets | Checksum-pinned Gitleaks 8.30.1; final exported source and new Git history scanned before push |
| Local distribution build | Blocked by this host's AppArmor container-init error at the first Docker RUN; no image-install/restore pass claimed from this attempt |
| GitHub-hosted CI | Pending first public run; it will test production-image backend/frontend units, installation, enrollment, upgrade and complete encrypted recovery |

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
