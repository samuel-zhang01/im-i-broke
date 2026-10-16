# Improvement plan

This plan tracks public-project work. Earlier internal review rounds and scores
are not public release certifications. See [known limitations](KNOWN_LIMITATIONS.md)
and [release validation](RELEASE_VALIDATION.md) for the current evidence.

| Priority | Work | Acceptance |
| --- | --- | --- |
| Release baseline — complete | Public source, installation, enrollment, upgrade rehearsal and encrypted recovery | Fresh history/source scans, backend/frontend gates, focused setup/auth/UI browser checks and [passing matching-image CI](RELEASE_VALIDATION.md); broader platform/provider limits remain |
| Provenance | Distinguish new empty manual accounts from statement-backed accounts | Truthful empty, CSV, manual-balance and statement labels; keep unknown-balance warnings |
| Historical precision | Add an explicit correction path for old KWD valuations stored at two decimals | Preserve original evidence; require a reviewed correction, without silently rewriting saved valuations |
| Dependencies | Review current development and image advisories | Inspect complete dependency graph, compatible fixes and required build/browser checks |
| Conflicts | Add revision checks for simultaneous same-field edits | Preserve drafts and offer actionable conflict recovery |
| Platforms | Validate unfamiliar users, physical devices and assistive technology | Record setup/recovery outcomes and actual OS/device/browser coverage |
| Currency | Extend GBP-oriented spending/planning contracts | Explicit currency and historical FX evidence; no relabeling GBP totals |
| Imports | Improve ambiguous-date/sign/overlap guidance | Synthetic and representative reviewed exports; no invented balances or hidden duplicate choices |
| Maintenance | Reduce large reporting modules and clarify fixture/resource ownership | Preserve behavior, source provenance and complete regressions for financial/session changes |

The existing architecture supports one owner per isolated installation. A shared
signup service, household roles and multi-tenant storage need separate design
and security review.
