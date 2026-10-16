# SnapTrade holdings retention

Keep all detailed holdings for at least 366 days (a full year including a leap day),
then retain the latest snapshot for each older UTC day. Also retain the last
complete, non-truncated holdings snapshot for each account on that day. Whole
snapshots remain intact; accounts are never combined into a fabricated snapshot.
This can retain several daily snapshots when accounts fail at different times.

The boundary date is retained in full. Malformed timestamps and the current
cache's snapshot are preserved. Maintenance runs independently of SnapTrade
credentials or polling: first after 60 seconds, then checks hourly, with one
committed compaction per UTC day. Failure rolls back and retries next hour.

Only redundant **position observations older than this window** are deleted. All account balances,
snapshot receipt headers, source timestamps, transactions, activity archives and
fetch receipts, statements, bank records and saved decisions remain intact.
Bank balance observations and their source-bound resolved card valuations have
no compaction or deletion path; both remain available beyond one year, including
after a credit-limit rule changes or a connection is removed.
This retains exact balance-history and source-authority behavior. A small
`holdings_retained` flag prevents compacted intraday receipts from appearing as
empty portfolios in the snapshot API/selector. Direct requests for a compacted
holdings snapshot return the existing not-found response.

The additive schema change adds `snaptrade_snapshots.holdings_retained` and
`snapshot_retention_runs`. The latter records each completed day's cutoff,
compacted snapshot count and removed position count. Collection does not depend
on maintenance succeeding. No provider requests are made by retention.

Most old position/index storage becomes reusable SQLite pages. No automatic
VACUUM runs, so filesystem size need not shrink immediately. Account receipts and
activity fetch logs still grow; this policy reduces the main growth source rather
than imposing a fixed database-size limit. Backups and source documents have
separate lifecycles and are not deleted.
