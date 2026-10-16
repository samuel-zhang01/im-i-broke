"""Build the finance DB end-to-end: parse every statement PDF (the folder-routed
Data/ tree + any auto-detected PDFs in the uploads folder) then compute and
persist the local intelligence. Idempotent — safe to re-run on each container
start (ingest uses INSERT OR IGNORE + dedupe; recompute fully rebuilds).

Usage: python -m app.build_db [DATA_ROOT] [UPLOADS_ROOT]
       defaults: DATA_ROOT=../Data  UPLOADS_ROOT=/app/uploads
"""
from __future__ import annotations
import sys
from pathlib import Path

from . import analytics
from .db import DB_PATH, connect
from .categorize import apply_categorization
from .ingest import ingest_dir, ingest_uploads


def main(data_root: str = "../Data", uploads_root: str = "/app/uploads") -> None:
    print(f"[build_db] ingesting from {data_root} -> {DB_PATH}", flush=True)
    stats = ingest_dir(data_root, db_path=DB_PATH)
    print(f"[build_db] ingested: {stats}", flush=True)
    if Path(uploads_root).exists():
        up = ingest_uploads(uploads_root, db_path=DB_PATH)
        print(f"[build_db] uploads: {up}", flush=True)
    conn = connect(DB_PATH)
    try:
        apply_categorization(conn)
        intel = analytics.recompute_all(conn)
        print(f"[build_db] intelligence: {intel}", flush=True)
        # Best-effort: warm live market prices so net worth includes holdings
        # immediately. Never fail the build if the price API is unreachable.
        try:
            val = analytics.valuation(conn, refresh=True)
            if val:
                print(f"[build_db] prices warmed: {val['totals']['n_priced']} priced,"
                      f" {val['totals']['n_unpriced']} unpriced", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[build_db] price warm skipped: {e}", flush=True)
    finally:
        conn.close()
    print("[build_db] done.", flush=True)


if __name__ == "__main__":
    data = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[2] / "Data")
    uploads = sys.argv[2] if len(sys.argv) > 2 else "/app/uploads"
    main(data, uploads)
