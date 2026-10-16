#!/usr/bin/env python3
"""Reproduce the investment EDA from cached-only API endpoints.

    python3 backend/scripts/investment_eda.py --base-url http://127.0.0.1:8080

Output is safe aggregate counts, percentages, dates and coverage; never raw
rows, account identifiers, names, credentials or private monetary values. The
pure checks are in backend/app/investment_profile.py. No provider refresh runs.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.investment_profile import profile, add_calendar_profile  # re-export for notebooks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    def get(path):
        with urlopen(args.base_url.rstrip('/') + path, timeout=30) as response:
            return json.load(response)
    try:
        review = get('/api/investments/analysis/insights')
        calendar = get('/api/investments/analysis/calendar')
    except HTTPError:
        raise SystemExit('The cached investment review API is unavailable; deploy the review service before running this report.') from None
    report = review.get('data_profile')
    if not isinstance(report, dict):
        raise SystemExit('This deployment does not expose the safe investment profile yet.')
    add_calendar_profile(report, calendar)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
