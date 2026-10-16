"""Reviewed instrument identities for holdings no longer in a live portfolio.

Listing evidence is independent of market prices and does not identify or link
brokerage accounts. Currency is the major unit; the market adapter converts GBp
to GBP once when accepting a verified London quote.
"""

LISTINGS = ({
    'isin': 'XS2399364582',
    'ticker': '5SPY',
    'raw_symbol': '5SPY',
    'symbol': '5SPY.L',
    'currency': 'GBP',
    'listing_currency': 'GBP',
    'exchange': 'XLON',
    'asset_type': 'etf',
    'instrument_id': 'historical:XS2399364582:XLON:GBP',
    'source_url': 'https://leverageshares.com/documents/factsheet/5x_spy_factsheet.pdf',
    'source_title': 'Leverage Shares 5x Long US 500 ETP issuer factsheet',
    'verified_at': '2026-09-18',
},)
