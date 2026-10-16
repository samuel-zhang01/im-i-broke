# Financial behavior

This is an implementation reference. For normal setup and account changes, use
[customization](../CUSTOMIZATION.md) and the [provider guide](../PROVIDERS.md).

## Accounts and sources

Provider connections, saved account identities and source records are distinct.
Link discovered accounts to existing history deliberately; matching a bank name
alone does not establish that two accounts are the same. Connections can share
an institution without sharing a ledger. Separate Trading 212 wrappers and
brokerage accounts retain distinct identities.

Original financial records remain evidence. Display names, ownership shares,
report inclusion and merchant classifications affect derived views without
rewriting imported amounts. Provider balances are observations at a timestamp,
not proof of complete transaction history. Stale, partial and disconnected
sources retain their status.

Net assets use every selected account with a supported current balance. The
net-assets chart shows only dates for which every selected account has a
supported value, from the earliest such date in the selected range. Unsupported
days remain gaps, including gaps between older and newer supported dates.
Partial fixed-account history remains coverage evidence but is not plotted as
net assets. The final complete chart value uses the same calculation as the
current headline. A missing older balance is never filled using today's
amount. Reconciled GBP bank statements
can supply dated printed running balances when the full transaction chain agrees
with the statement closing balance.

The same chart can show a separately coloured investment-only series. Each of
its values requires a supported value for every selected investment account;
it excludes banks and cards and is never labelled full net assets. This series
may begin earlier than complete net-assets coverage. Gaps, reconstructed market
estimates and valuation-source changes retain their evidence labels. Saved PDFs
can add bank history only where their account identity, balances and dated
transaction chain reconcile; an absent bank balance is not extrapolated.

For a card whose bank feed reports available credit, a raw past feed value is
not dated debt. At a successful balance read, a rule already confirmed for that
account can save a separate effective card value tied to the raw observation,
fetch time, currency, limit and rule version. Historical net assets use that
saved value only on its observation date. Later rule or limit changes, and a
disconnected feed, do not recalculate or erase it. The bank feed has no bank
balance-as-of timestamp, so the date means the app's UTC fetch date rather than
a certified bank closing balance. A retrospective, date-scoped limit attestation
may support an explicit backfill from an existing raw observation; that value is
marked estimated and its attestation and source are retained.

Where no saved effective value exists, historical net assets may carry the most
recent reconciled card statement balance for up to 45 days. Dates beyond that
window remain unknown when the feed's historical interpretation is available
credit or unconfirmed. Today's card value uses the currently confirmed rule.
Carried statement debt is marked estimated, and a change of valuation basis
starts a new comparison segment.

Asset history in the investment workspace covers selected investment accounts
and brokerage cash. It excludes bank balances and card debt, which net assets
includes. The optional recent-price estimate applies historical prices to
today's holdings and cash, so it cannot recover earlier trades or cash flows.

For generic CSV imports, a stable transaction ID is scoped to the account and
import source. Without IDs, identical transactions are distinguished by their
occurrence within the file. Duplicate previews and overlap reviews are required;
partial exports with indistinguishable rows cannot prove which occurrence is new.

## Currency and signs

Cash ledger amounts use positive money in and negative money out. A credit
account's balance direction and transaction direction are separate settings;
check a known statement balance and a known purchase before changing either.
Account classification and currency changes that reinterpret existing evidence
are blocked, including changes requested while linking a bank feed. An empty
account can still be classified explicitly before it has history. Mapping and
transaction-review decisions acquire SQLite's writer slot before checking their
preconditions, so concurrent requests cannot claim the same account or transaction.

Native-currency reports retain the original denomination. Converted workspace
reports use dated FX evidence and surface withheld events or missing rates.
The established GBP spending and planning views do not reinterpret foreign
amounts as pounds. Months with unsupported foreign activity cannot establish a
verified zero-spending planning baseline. Planning exclusions apply to balances,
holdings, recurring activity and coverage. Ownership shares change derived
contributions only.

Report evidence is paginated without changing full-period totals. Pages belong
to one evidence revision; when underlying records change, return to the first
page to review the updated report. A reporting currency can be selected even
without an account in that denomination; missing FX stays explicitly unknown.

Card limits, effective debt and native totals use the supported currency's
smallest unit: three decimal places for BHD, IQD, JOD, KWD, LYD, OMR and TND;
zero for BIF, CLP, DJF, GNF, ISK, JPY, KMF, KRW, PYG, RWF, UGX, VND, VUV, XAF,
XOF and XPF; two for other supported currencies. The frontend uses the same
explicit policy rather than the browser's currency defaults. Native components
are summed before totals are rounded. For example, a KWD limit of 1,000.120 and
available credit of 998.456 produce debt of -1.664.

Previously saved card valuations remain immutable evidence even if an earlier
calculation rounded them to two decimal places. Subsequent receipts use the
correct precision; a historical correction needs a separate reviewed procedure.
The current backfill skips already recorded valuation IDs and cannot repair
those records. No historical correction command was added by this audit.
Previously confirmed fractional limits in a zero-decimal currency retain their
available-credit interpretation and display reconfirmation guidance. New or
reconfirmed rules must use whole units; reading an old rule does not rewrite it.

## Calendar dates and source timestamps

The workspace IANA time zone defines today's date for manual observations,
confirmed history coverage, planning and workspace reports. The manual balance
form reads `/api/workspace/calendar` rather than guessing from the browser or
UTC clock. It waits for that response and keeps an intentionally chosen date
during subsequent refreshes. The preferences response remains preferences only.

Provider observations, fetch timestamps and saved card valuation dates retain
their UTC evidence clock. A provider receipt from the current UTC day remains
available to the current net-assets view even when the workspace calendar is
still on the previous day. Historical replay continues to select evidence by
its saved date. Neither changing the workspace zone nor crossing its midnight
rewrites provider evidence; the net-assets cache includes the workspace date so
user-entered observations become visible at the correct calendar boundary.

## Spending and merchants

Merchant aliases can use exact or prefix matches in a relevant transaction
context. Merchant identity and spending treatment are separate: transfers, card
repayments, refunds and ordinary purchases must not be combined solely because
their descriptions resemble one another. Review suggestions before saving them.

Person-name and business-name similarities are review clues, not automatic proof
of identity. Exact evidence and saved decisions take precedence. Generic rules
must not encode an individual household's account numbers or private payees.
Public merchant defaults are maintained separately from user-created aliases.

## Investments and market estimates

Broker-reported values, imported statements and price-based estimates carry
different evidence and timestamps. A successful refresh saves observations for
the connection actually refreshed; combining cached portfolios must not invent
freshness for the others. Disconnected portfolios are excluded from default net
worth to avoid double-counting a later connection while retaining history.

Market estimates depend on instrument identity, quotation units, exchange and
currency. They are estimates rather than broker valuations. Corporate actions
need explicit, auditable transformations; stock splits must preserve economic
value rather than appear as investment performance. Never fabricate transactions
to fill a gap in historical coverage.

Historical source reconciliation is a deliberate operator workflow. Rehearse
changes against private copies using `scripts.verify_upgrade`; ordinary testing
must not rebuild or consolidate a live ledger. Detailed position observations
follow the separate [retention policy](RETENTION.md).

## Retained statement adapters

Legacy statement ingestion is an operator workflow; browser PDF uploads are
retired. The generic CSV import in Settings is separate. Rehearse ingestion on
a copy and preserve original documents and saved source assignments.

A reconciled statement supplies the ledger transaction date when its purchase
matches an API record, regardless of import order. Printed posting dates and
original API dates remain source evidence; exact posting dates can establish
overlap beyond the usual seven-day matching window. Ambiguous matches still
require review. Reprocessing an unchanged, reconciled HSBC source can correct
old parsed dates only when the complete financial/raw sequence and available
source hashes agree. IDs, reviews and old date-correction evidence are retained,
including through later overlapping statements. This is not an automatic
household-data migration.

Trading 212 CSVs may omit wrapper/account identity. Use an explicit account
choice or the `Data/Trading212/Stocks/` and `Data/Trading212/ISA/` folders. Known
files retain their saved assignments; unfamiliar ambiguous exports are rejected
when multiple accounts exist. Costs replay independently per account before
positions are combined. A wrapper label does not calculate tax treatment.

The Moomoo adapter reads monthly securities PDFs from Moomoo Financial Singapore.
It preserves native cash separately from the statement reporting currency,
allocates order fees across fills, and retains gifts, income and split evidence.
Cash and share reconciliation must agree with independently printed snapshots.
Gift shares with no acquisition valuation retain unknown cost. Missing opening
history uses dated statement holdings without inventing purchases or historical
profit. Matching reimports are idempotent; conflicting same-period replacements
require review. See [`parsers/moomoo.py`](../../backend/app/parsers/moomoo.py).
