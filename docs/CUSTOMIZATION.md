# Account settings, provider connections and CSV imports

Existing accounts and server environment credentials keep their existing IDs,
links and financial history. Manage accounts, connections and imports in **Settings**.

## Workspace preferences

Choose a reporting currency, locale and time zone in **Settings → Workspace
preferences**. The currency is the default for Net assets, investments, workspace
reports and new manual accounts. Currency choices already made within a view stay
in place; original transaction currencies do not change.

The locale formats amounts and exchange rates in workspace reports. The interface
remains in English. Historical statement, spending and planning views keep their
labelled GBP amounts. The time zone sets calendar dates for manual balances,
history coverage, Net assets, planning and reports; provider timestamps stay
stored in UTC.

## Account settings

Accounts can have a display name, institution and institution aliases, report
and planning inclusion choices, ownership share, opening/closing dates, and
archive state. Existing accounts default to full ownership and inclusion.
Institution aliases make explicit matches possible when a provider and an
import call the same bank different names.

Settings changes retain an audit record. Changing an established account's
currency, or switching between cash, liability and investment classifications,
is blocked. Current/savings/cash/debit labels can be corrected without changing their
financial treatment. Opening and closing dates cannot exclude existing transactions,
including booked native bank records, or manual balance observations.
Transaction signs and linked ledger rows are never silently rewritten. Review
the preview and use a separate account when the denomination changes.

Manual accounts need no provider. A manual balance is an observation for its
specified date and currency; later observations preserve the earlier ones.
Balance observations do not create fabricated transactions or certify complete
transaction history. A newer manual card balance is displayed as a manual
observation, with its own date, rather than inheriting an older statement's status.

Planning inclusion applies to starting balances, investment holdings, recurring
activity and coverage. Excluding an account from planning leaves its report
inclusion unchanged. The GBP planning baseline withholds months containing
unsupported foreign-currency activity instead of treating that activity as zero.

## Provider connections

For step-by-step personal and hosted setup, see
[Lunch Flow and SnapTrade setup](PROVIDERS.md).

Personal API credentials can be saved for Lunch Flow and SnapTrade. Multiple
saved connections have separate credentials, identifiers, sync leases, status
and last-attempt information. Server variables `LUNCH_FLOW_API`, `SNAPTRADE_ID`
and `SNAPTRADE_KEY` remain available as read-only environment connections.

Use **Check connection** to validate access, **Refresh data** to retrieve saved
provider data, and **Edit connection** to rotate personal credentials on the
same connection. Reusing the same connection preserves source identity.
Connections can be paused, resumed or disconnected. Disconnect removes local
credentials and stops subsequent reads, while retaining financial evidence.
Manage any remaining provider-side consent in the provider's own account.

Secrets are encrypted with the installation's existing authentication key.
They are never returned by the settings API. Restore that same key with a
database backup; the service will not generate a substitute for a missing key.
A missing key must be restored before saved credentials can be read. A new
installation's initializer creates its key explicitly.

Bank discovery only updates accounts belonging to the connection being read.
An account already observed under other credentials is held for review rather
than automatically duplicating its ledger. Unsupported native bank currencies
remain in the provider cache and are shown by the native currency report.

Saved SnapTrade portfolios appear in the existing portfolio views. Their
cache and account identifiers are scoped to the saved connection; legacy
environment account IDs remain unchanged. Each successful refresh appends
observations for the connection actually refreshed. Combining display caches
does not create fresh observations for the other connections. Paused,
disconnected and failed connections keep their last saved data with stale
status. Disconnected brokerage connections are excluded from default net worth;
their saved observations remain selectable as historical evidence. Paused
connections remain included with stale status.

Saved connections use the same `LUNCH_FLOW_SYNC_SECONDS` and
`SNAPTRADE_SYNC_SECONDS` scheduling configuration as their environment-backed
counterparts. Setting a provider's interval to `0` disables its automatic
refreshes. Each connection has a persisted operation lease and a one-minute
cooldown, including manual checks.

## Generic CSV imports

Choose the target account and explicitly map the date, description and amount
columns, or separate positive debit/credit columns. Select the date format,
decimal separator, delimiter, currency and amount direction. The signed amount
stored in the ledger uses positive money in and negative money out. A
transaction currency must match its target account; use another account for a
different currency.

The importer accepts UTF-8 CSV text up to 512 KiB, 10,000 transactions and 100
unique columns. It rejects malformed rows, ambiguous unsupported date formats,
grouped amounts, invalid numbers and currency conflicts. Every validation error
must be fixed before any row is imported. Save a mapping template to reuse a
bank's column configuration.

The full JSON request, including escaping and mapping, must also fit the existing
1 MiB request limit; the browser checks this before sending it. Split larger
exports into separate files with the same import source name.

Review the preview before committing. The ordinary sample shows up to 200 rows,
alongside the complete row/error/duplicate/overlap counts. Every row needing an
overlap review is available in a separate paginated list, including rows beyond
the sample. Existing transactions with the
same date and amount, and transactions inside an imported statement period,
require an explicit overlap confirmation. A changed preview or relevant ledger
state requires a new preview.

When available, map a stable source transaction ID. Its import identity is
scoped to the target account and **Import source** name, so reuse that source
name for later exports from the same bank. The original filename is recorded
separately and does not change this identity. A source ID whose financial
contents change is rejected to preserve its previous ledger row.

Without source IDs, identity uses the normalized date, description, amount,
currency and occurrence within the CSV. This preserves identical repeated
transactions and makes repeat imports idempotent. Partial exports containing
indistinguishable identical transactions cannot establish which occurrence is
new; use stable bank IDs when possible and review ambiguous exports carefully.

Each committed batch retains its mapping, source name, optional filename,
content hash, original parsed records and transaction links. Import and derived
analytics update together in one transaction. A failed import does not leave a
partial batch or rewrite historical source evidence.
