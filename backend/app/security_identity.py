"""Reviewed US listing identities for statement formats without printed ISINs.

This is enrichment, never a claim that the PDF printed an ISIN. The US market,
USD currency and exact ticker must agree. Unknown tickers remain unresolved.
"""

_SOFTWARE = 'https://www.ishares.com/us/products/239771/'
_CORE = 'https://www.ishares.com/uk/professional/en/products/251900/iusa'
_SEMIS = 'https://www.ishares.com/us/products/239705/ishares-robotics-and-artificial-intelligence-etf'
_BROKER = 'Corroborated exact US ticker and ISIN from imported Trading 212 security records'
_IDENTITIES = {
    'SPY': ('US78462F1030', 'https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy', 'State Street SPY listing'),
    'DIA': ('US78467X1090', 'https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-dow-jones-industrial-average-etf-trust-dia', 'State Street DIA listing'),
    'CRM': ('US79466L3024', _SOFTWARE, 'iShares IGV holdings'),
    'CRWD': ('US22788C1053', _SOFTWARE, 'iShares IGV holdings'),
    'PANW': ('US6974351057', _SOFTWARE, 'iShares IGV holdings'),
    'MSFT': ('US5949181045', _SOFTWARE, 'iShares IGV holdings'),
    'GOOGL': ('US02079K3059', _CORE, 'iShares IUSA holdings, Class A'),
    'META': ('US30303M1027', _CORE, 'iShares IUSA holdings'),
    'AMZN': ('US0231351067', _CORE, 'iShares IUSA holdings'),
    'NVDA': ('US67066G1040', _SEMIS, 'iShares SOXX holdings'),
    'AMD': ('US0079031078', _SEMIS, 'iShares SOXX holdings'),
    'INTC': ('US4581401001', _SEMIS, 'iShares SOXX holdings'),
    'V': ('US92826C8394', 'https://etp.morganstanley.com/fr/en/underlying-details/v_us_equity', 'Morgan Stanley Visa underlying identity'),
    'AAPL': ('US0378331005', None, _BROKER),
    'NFLX': ('US64110L1061', None, _BROKER),
}


def resolve_us_security(ticker, market='US', currency='USD'):
    if market != 'US' or currency != 'USD' or ticker not in _IDENTITIES:
        return None
    isin, source_url, source_title = _IDENTITIES[ticker]
    return {'isin': isin, 'source_url': source_url, 'source_title': source_title,
            'method': 'reviewed_us_ticker_catalog', 'verified_at': '2026-09-15',
            'printed_isin': False}
