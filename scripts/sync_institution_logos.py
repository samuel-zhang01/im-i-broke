"""Maintain local institution artwork from SnapTrade's public integration catalog.

Run with --refresh to retrieve the current catalog and assets. --check is offline.
PNG conversion needs Pillow and CairoSVG (maintainer tools, not app dependencies).
The app never fetches logos from an account-supplied URL or contacts these hosts.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'frontend/src/institutionCatalog.json'
LOGOS = ROOT / 'frontend/public/logos'
SOURCE = 'https://snaptrade.com/brokerage-integrations'
ALIASES = {
    'aj-bell': ['AJBell', 'AJ Bell Youinvest'], 'alpaca': ['Alpaca Paper'],
    'bmo': ['BMO InvestorLine', 'Bank of Montreal'], 'bux': ['BUX Zero'],
    'chase': ['JPMorgan Chase', 'J.P. Morgan', 'JP Morgan'],
    'cibc': ["CIBC Investor’s Edge", "CIBC Investor's Edge", 'CIBC'],
    'citi': ['Citibank', 'Citi Self Invest'], 'commsec': ['Commonwealth Securities'],
    'etrade': ['E*TRADE', 'E Trade', 'ETrade Financial'],
    'fidelity': ['Fidelity Investments', 'Fidelity NetBenefits'],
    'hargreaves-lansdown': ['Hargreaves Lansdown', 'HL'],
    'ibkr': ['Interactive Brokers', 'IBKR', 'InteractiveBrokers'],
    'moomoo': ['Moomoo', 'Futu', 'Futu Securities'],
    'public': ['Public.com', 'Public Investing'],
    'schwab': ['Charles Schwab', 'Schwab'],
    'stake-aus': ['Stake', 'Stake AUS', 'Stake Australia'],
    'tastytrade': ['Tastyworks'], 'tddirectinvesting': ['TD Direct Investing', 'TD'],
    'trading212': ['Trading 212', 'Trading212', 'T212'],
    'us-bank': ['U.S. Bank', 'US Bank', 'U.S. Bancorp'],
    'vanguard': ['Vanguard', 'Vanguard US'], 'webull': ['Webull', 'Webull US'],
    'webull-ca': ['Webull Canada', 'Webull CA'], 'wellsfargo': ['Wells Fargo'],
}


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(); self.inside = False; self.values = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script': self.inside = True

    def handle_endtag(self, tag):
        if tag == 'script': self.inside = False

    def handle_data(self, data):
        if self.inside: self.values.append(data)


def read(url):
    host = urlsplit(url).hostname
    allowed = {
        'snaptrade.com', 'images.prismic.io', 'snaptrade-v3.cdn.prismic.io',
        'www.degiro.co.uk', 'www.degiro.com', 'is1-ssl.mzstatic.com',
    }
    if urlsplit(url).scheme != 'https' or host not in allowed:
        raise ValueError('Unreviewed logo source: ' + str(host))
    with urlopen(Request(url, headers={'User-Agent': 'InstitutionCatalog/1.0'}), timeout=25) as response:
        if urlsplit(response.url).scheme != 'https' or urlsplit(response.url).hostname not in allowed:
            raise ValueError('Review redirected artwork before using it')
        body = response.read(8_000_001)
        if len(body) > 8_000_000: raise ValueError('Oversized logo source')
        return body


def discover(html):
    """Resolve the complete published list, including pagination's hidden rows."""
    parser = Scripts(); parser.feed(html)
    flight = ''
    for script in parser.values:
        for match in re.finditer(r'self\.__next_f\.push\((\[1,.*?\])\)', script):
            flight += json.loads(match[1])[1]
    records = {}
    for line in flight.splitlines():
        key, separator, value = line.partition(':')
        if separator:
            try: records[key] = json.loads(value)
            except ValueError: pass

    def resolve(value, depth=0):
        if depth > 30: raise ValueError('Unexpected catalog reference cycle')
        if isinstance(value, str) and value.startswith('$') and value[1:] in records:
            return resolve(records[value[1:]], depth + 1)
        if isinstance(value, list): return [resolve(v, depth + 1) for v in value]
        if isinstance(value, dict): return {k: resolve(v, depth + 1) for k, v in value.items()}
        return value

    containers = [r for r in records.values() if isinstance(r, dict) and r.get('isIntegrationsPage') and 'brokerages' in r]
    if len(containers) != 1: raise ValueError('Catalog format changed; review the public source')
    rows = resolve(containers[0]['brokerages'])
    if not isinstance(rows, list) or len(rows) < 30: raise ValueError('Catalog appears incomplete')
    result = []
    for row in rows:
        primary = next(s['primary'] for s in row['data']['slices'] if s['slice_type'] == 'brokerages')
        name = ''.join(part['text'] for part in primary['title'])
        slug = row['uid'].removesuffix('-api')
        result.append({'slug': slug, 'name': name,
            'aliases': sorted(set([name, *ALIASES.get(slug, [])])), 'providers': ['snaptrade'],
            'source': 'https://snaptrade.com' + row['url'], 'image_source': primary['image'].get('url')})
    return result


def png(body):
    from PIL import Image
    if body.lstrip().startswith((b'<svg', b'<?xml')):
        import cairosvg
        # Decode reviewed publisher artwork with XML entity expansion disabled.
        body = cairosvg.svg2png(bytestring=body, output_width=256)
    with Image.open(BytesIO(body)) as original:
        original.load()
        image = original.convert('RGBA'); image.thumbnail((256, 256))
        target = BytesIO(); image.save(target, format='PNG', optimize=True)
        return target.getvalue()


def check(catalog):
    slugs = [r['slug'] for r in catalog['institutions']]
    if len(slugs) != len(set(slugs)): raise ValueError('Duplicate logo slugs')
    for row in catalog['institutions']:
        if not re.fullmatch(r'[a-z0-9-]+', row['slug']): raise ValueError('Unsafe logo slug')
        if not row['aliases'] or not row['name']: raise ValueError('Institution identity missing')
        body = (LOGOS / (row['slug'] + '.png')).read_bytes()
        if not body.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('Invalid PNG for ' + row['name'])
    print(f"Validated {len(slugs)} local institution PNGs; {sum('snaptrade' in r['providers'] for r in catalog['institutions'])} published SnapTrade integrations.")


def refresh():
    previous = json.loads(CATALOG.read_text())
    discovered = discover(read(SOURCE).decode())
    old = {r['slug']: r for r in previous['institutions']}
    # Retain older artwork for saved accounts without advertising removed integrations.
    for row in old.values():
        row['providers'] = [p for p in row['providers'] if p != 'snaptrade']
    assets = {}
    for row in discovered:
        previous = old.get(row['slug'], {})
        # Keep reviewed replacements when the directory artwork is visibly degraded.
        if previous.get('curated_artwork'):
            row['image_source'] = previous['image_source']
            row['curated_artwork'] = True
        elif not row['image_source']:
            row['image_source'] = previous.get('image_source')
        if not row['image_source']: raise ValueError('Artwork needs review: ' + row['name'])
        row['retrieved_on'] = date.today().isoformat()
        old[row['slug']] = row
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, body in zip(discovered, pool.map(lambda row: png(read(row['image_source'])), discovered)):
            assets[row['slug']] = body
    catalog = {'snaptrade_catalog_source': SOURCE, 'checked_on': date.today().isoformat(),
               'institutions': sorted(old.values(), key=lambda r: r['slug'])}
    # Fetch and validate the entire update before replacing bundled assets.
    for slug, body in assets.items(): (LOGOS / (slug + '.png')).write_bytes(body)
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
    check(catalog)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.refresh: refresh()
    else: check(json.loads(CATALOG.read_text()))
