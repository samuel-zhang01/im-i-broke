# Known limitations

Am I Broke? is an evolving self-hosted application. Review these limits before
using financial reports or offering installations to others.

- **One owner per installation.** Multiple devices can sign in as that owner.
  Public registration, invitations, separate household roles, billing and
  shared-database tenant isolation are not implemented.
- **Currency scope varies by view.** Native-currency accounts and Workspace
  reports support foreign amounts and dated conversion evidence. Established
  spending/planning views remain GBP-based. Missing FX or prices can leave totals
  partial; changing a display currency cannot supply missing evidence.
- **Provider coverage varies.** Availability depends on the provider, account,
  region and subscription. A successful sync does not prove complete history.
  Offline fixtures do not certify a live institution or every statement layout.
- **Source labels need refinement.** An empty manual account can inherit
  statement-oriented labels. An unknown balance remains unknown until you add
  a dated observation or source evidence.
- **Older saved precision is preserved.** Existing KWD valuations may have been
  stored at two decimal places. Current precision handling does not retroactively
  correct an immutable historical valuation; review against its original source.
- **Simultaneous edits can conflict.** Some same-field account, preference and
  card-rule edits use last-write-wins. Coordinate changes between devices until
  revision-aware editing is implemented.
- **Live storage needs host protection.** Databases and uploads are not encrypted
  at rest by the app. Saved secrets use a separate installation key. Keep a
  tested encrypted recovery set, including its matching key and configuration.
- **Platform evidence is bounded.** Automated Linux Chromium, Firefox and WebKit
  checks are not physical Windows/macOS/iOS/Android or assistive-technology
  certification. Windows setup uses WSL 2 and Linux containers.
- **Dependencies need continuing review.** Development tools and container bases
  can acquire advisories after a release. Consult the exact revision's CI and
  dependency alerts; older internal scans do not establish the current status.
  On 20 October 2026, the Tailwind 4 migration removed the previously reported
  pattern/parser dependency chains. Both the full locked npm graph and
  `npm audit --omit=dev` reported zero advisories. This does not establish the
  status of container base images or future dependency releases.
  See [validation](RELEASE_VALIDATION.md).
- **Modern browsers are required.** Tailwind 4 targets Safari 16.4+, Chrome 111+
  and Firefox 128+. Older browser engines are outside the supported styling
  baseline; see the [Tailwind upgrade guide](https://tailwindcss.com/docs/upgrade-guide).

See [release validation](RELEASE_VALIDATION.md) for checks actually run on this
public snapshot, [security](../SECURITY.md) for private vulnerability reporting,
and [the improvement plan](APP_IMPROVEMENT_PLAN.md) for follow-up work.
