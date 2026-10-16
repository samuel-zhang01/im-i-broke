"""Moomoo Singapore monthly and daily securities statements.

Read execution rows, never turn a monthly holdings snapshot into a purchase.
Native cash, broker statement snapshots, printed share movements and source
coordinates remain inspectable. The statement does not supply historical GBP
costs or a timezone; neither is inferred from its SGD reporting currency.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pdfplumber

from .base import ParsedStatement, RawTxn

NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
CURRENCY = r'[A-Z]{3}'
DATE = r'\d{4}/\d{2}/\d{2}'
TIME = r'\d{2}:\d{2}:\d{2}'
ZERO = Decimal(0)
SGX_SECURITIES = {
    'D05': ('SG1L01001701', 'https://links.sgx.com/1.0.0/corporate-announcements/H4CAPIJKFH4UKN0Y'),
    'O39': ('SG1S04926220', 'https://links.sgx.com/1.0.0/corporate-announcements/6FQ0XQXN50GGQQHH'),
}
FEE_PATTERN = r'((?:Platform|Settlement|Consolidated Audit Trail|SEC|Trading Activity|Trading|Clearing) Fees|Commission|Consumption Tax):\s*('+NUMBER+r')'
FEE_STARTS = {'Platform', 'Settlement', 'Consolidated', 'SEC', 'Trading', 'Clearing', 'Commission:', 'Consumption'}


def _d(value: str) -> Decimal:
    value = value.replace(',', '').strip()
    if not re.fullmatch(NUMBER, value):
        raise ValueError(f'Moomoo statement has an invalid number: {value!r}')
    return Decimal(value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def _text(words, x0=0, x1=842, y0=0, y1=595):
    chosen = [w for w in words if x0 <= (w['x0']+w['x1'])/2 < x1 and y0 <= (w['top']+w['bottom'])/2 < y1]
    lines = []
    for w in sorted(chosen, key=lambda w: (w['top'], w['x0'])):
        if not lines or abs(lines[-1][0]['top']-w['top']) > 2:
            lines.append([w])
        else:
            lines[-1].append(w)
    return '\n'.join(' '.join(w['text'] for w in sorted(line,key=lambda w:w['x0'])) for line in lines)


def _flat(text):
    return re.sub(r'\s+', ' ', text).strip()


def _daily_opening_nav(text, base_currency):
    """Read the separate printed opening NAV, never derive it from the close."""
    block = re.search(r'Starting Net Asset Value \d{8}\s+(.*?)\s+Ending Net Asset Value', text, re.S)
    if not block:
        raise ValueError('Moomoo daily opening NAV could not be read')
    lines = [_flat(line) for line in block[1].splitlines() if line.strip()]
    if len(lines) != 5 or lines[1] != f'Equal to({base_currency})':
        raise ValueError('Moomoo daily opening NAV columns could not be aligned')
    currencies, amounts = lines[0].split(), lines[2].split()
    rates = re.findall(r'exchange rate : ('+NUMBER+r'|-)(?=\s|$)', lines[4])
    if (len(currencies) != 6 or len(set(currencies)) != 6 or
            not all(re.fullmatch(CURRENCY, c) for c in currencies) or
            len(amounts) != len(currencies) or len(rates) != len(currencies)):
        raise ValueError('Moomoo daily opening NAV currency breakdown is incomplete')
    nav = _d(lines[3])
    calculated = ZERO
    for amount, rate in zip(amounts, rates):
        amount = _d(amount)
        if rate == '-':
            if amount:
                raise ValueError('Moomoo daily opening NAV has an unvalued currency')
        else:
            calculated += amount * _d(rate)
    if abs(nav-calculated) > Decimal('.02'):
        raise ValueError('Moomoo daily opening NAV disagrees with its printed currency values')
    return float(nav)


def _identity(ticker, exchange):
    if exchange == 'SGX' and ticker in SGX_SECURITIES:
        isin, url = SGX_SECURITIES[ticker]
        return {'isin': isin, 'security_identity': {
            'isin': isin, 'source_url': url, 'source_title': 'Issuer announcement on Singapore Exchange',
            'method': 'reviewed_sgx_ticker_catalog', 'verified_at': '2026-09-19', 'printed_isin': False}}
    if exchange != 'US':
        raise ValueError(f'Moomoo exchange {exchange!r} needs a verified security mapping before import')
    from ..security_identity import resolve_us_security
    evidence = resolve_us_security(ticker)
    if not evidence:
        raise ValueError(f'Moomoo security {ticker!r} needs a verified identity before import')
    return {'isin': evidence['isin'], 'security_identity': evidence}


def _meta(action, time, currency, native, ticker=None, name=None, exchange=None, listing_exchange=None, **extra):
    result = {
        'broker': 'Moomoo', 'parser': 'moomoo', 'action': action, 'time': time,
        'source_time': time, 'time_timezone': 'unspecified',
        'source_time_column': 'Date/Time (statement timezone unspecified)',
        'native_currency': currency, 'native_amount': float(native),
        'cash_movements': {currency: float(native)}, 'gbp_value_known': currency == 'GBP',
        'ticker': ticker, 'security_name': name, 'exchange': exchange,
        'shares': None, 'cost_basis_gbp': None, 'proceeds_gbp': None,
        'provider_id': None,
    }
    if ticker:
        result.update(_identity(ticker, listing_exchange or exchange or 'US'))
    if listing_exchange is not None:
        result['listing_exchange'] = listing_exchange
    result.update(extra)
    return result


def _txn(meta, raw):
    action, ticker = meta['action'], meta.get('ticker')
    desc = ' '.join(p for p in (action, ticker, meta.get('security_name')) if p)
    category = ('Investment income' if action.startswith('Dividend') or action == 'Lending interest'
                else 'Transfers & payments' if action in ('Deposit','Withdrawal')
                else 'Fees & interest' if action == 'Margin interest' else 'Investments')
    return RawTxn(txn_date=datetime.fromisoformat(meta['time']).date(), description=desc,
                  amount=meta['cash_movements'].get('GBP',0), category=category,
                  merchant=ticker or 'Moomoo', type_code=action, raw=raw, meta=meta)


def _row_security(words, bbox, mode):
    """Read a ruled position row with its multiline name kept in its column."""
    x0,y0,x1,y1 = bbox
    text = _text(words,x0,x1,y0,y1)
    row_words = [w for w in words if y0 <= (w['top']+w['bottom'])/2 < y1 and x0 <= (w['x0']+w['x1'])/2 < x1]
    markets = {w['text'] for w in row_words} & {'US', 'SGX'}
    if not markets:
        return None
    # These documents use independently ruled rows, so the symbol is always
    # the final token in the name column, including names wrapping to 6 lines.
    exchange_words = [w for w in row_words if w['text'] in markets]
    if not exchange_words:
        return None
    cut = min(w['x0'] for w in exchange_words)-1
    name_lines = _text(words,x0,cut,y0,y1).splitlines()
    ticker = name_lines[-1].strip()
    name = ' '.join(name_lines[:-1])
    if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}',ticker):
        raise ValueError('Moomoo position row has an unreadable security symbol')
    tokens = [w['text'] for w in sorted(row_words,key=lambda w:w['x0']) if w['x0']>=cut]
    if tokens[:2] not in (['US','USD'], ['SGX','SGD']):
        raise ValueError('Moomoo position columns could not be aligned')
    vals = tokens[2:]
    required = 8 if mode == 'ending' else 11
    if len(vals) != required:
        raise ValueError(f'Moomoo {mode} position {ticker} has {len(vals)} numeric fields, expected {required}')
    nums = [None if v == '-' else _d(v) for v in vals]
    exchange, currency = tokens[:2]
    base = {'ticker':ticker,'name':name,'exchange':exchange,'currency':currency, **_identity(ticker,exchange)}
    if mode == 'ending':
        settled,unsettled,shares,multiplier,price,value,fx,base_value = nums
        if abs(settled+unsettled-shares) > Decimal('0.00000001') or multiplier != 1:
            raise ValueError(f'Moomoo unsupported multiplier or inconsistent units for {ticker}')
        if price is None or abs(_money(shares*price)-value)>Decimal('.01'):
            raise ValueError(f'Moomoo position value disagrees with shares and price for {ticker}')
        base.update(shares=float(shares),settled_shares=float(settled),unsettled_shares=float(unsettled),
                    price=float(price),market_value=float(value),base_exchange_rate=float(fx),base_market_value=float(base_value))
    else:
        starting,start_price,start_value,ending,end_price,end_value,change,buy,sell,into,out = nums
        base.update(starting_shares=float(starting),shares=float(ending),starting_price=float(start_price) if start_price is not None else None,
                    price=float(end_price) if end_price is not None else None,starting_market_value=float(start_value),market_value=float(end_value),
                    bought_shares=float(buy),sold_shares=float(sell),transferred_in=float(into),transferred_out=float(out))
    return base


def _extract_pages(path: str):
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page = page.dedupe_chars()
            if abs(page.width-842)>3 or abs(page.height-595)>3:
                raise ValueError('Unsupported Moomoo statement page size; no rows imported')
            pages.append({'words':page.extract_words(), 'text':page.extract_text() or '',
                          'tables':[(t, t.extract()) for t in page.find_tables()]})
    return pages


def parse(path: str) -> ParsedStatement:
    pages = _extract_pages(path)
    full = '\n'.join(p['text'] for p in pages)
    daily = 'Daily Statement' in full
    if (('Monthly Statement' not in full and not daily) or 'Moomoo Financial Singapore' not in full
            or (daily and 'Monthly Statement' in full)):
        raise ValueError('This is not a supported Moomoo Singapore securities statement')
    account_numbers = set(re.findall(r'Account Number\s+(\d{10,20})',full)) | set(re.findall(r'Account Number:\s*(\d{10,20})',full))
    if len(account_numbers)!=1:
        raise ValueError('Moomoo statement has missing or inconsistent account identity')
    account_number = account_numbers.pop()
    start_match = re.search(r'Starting Net Asset Value (\d{8})',full)
    end_match = re.search(r'Ending Net Asset Value (\d{8})',full)
    if not start_match or not end_match:
        raise ValueError('Moomoo statement has no independent reporting interval')
    previous_end = datetime.strptime(start_match[1],'%Y%m%d').date()
    end = datetime.strptime(end_match[1],'%Y%m%d').date()
    start = previous_end+timedelta(days=1)
    if not (0 < (end-previous_end).days <= (7 if daily else 35)):
        raise ValueError('Moomoo statement interval is not a supported reporting interval')
    nav = re.search(r'Net Asset Value:\s*('+NUMBER+r')',pages[0]['text'])
    totals = re.search(r'Base Currency:\s*([A-Z]{3})\s+('+NUMBER+r')\s+('+NUMBER+r')\s+('+NUMBER+r')',pages[0]['text'])
    if not nav or not totals:
        raise ValueError('Moomoo printed portfolio/cash totals could not be read')
    base_currency=totals[1]
    txns,trade_events,movement_legs,exchange_legs = [],[],[],[]
    snapshots,changes,cash = [],[],{}
    for page_no,page in enumerate(pages,1):
        words = page['words']
        # Position tables are ruled even when extraction reports merged cells.
        for table,rows in page['tables']:
            table_text=' '.join(' '.join(v or '' for v in row) for row in rows)
            if 'Settled Quantity' in table_text:
                table_mode='ending'
            elif 'Starting' in table_text and 'Buy Quantity' in table_text:
                table_mode='changes'
            else:
                table_mode=None
            if table_mode:
                for row in table.rows:
                    security = _row_security(words,row.bbox,table_mode)
                    if security:
                        security['source_page']=page_no
                        (snapshots if table_mode=='ending' else changes).append(security)
            # Stock gifts / source split legs have a distinct eight-column grid.
            if 'Direticon' in table_text or ('Movement - Securities' in table_text and 'Quantity' in table_text):
                for row in table.rows:
                    x0,y0,x1,y1=row.bbox
                    typ=_flat(_text(words,138,230,y0,y1))
                    if typ not in ('Stock Gift','Corporate Action'):
                        continue
                    dt=_flat(_text(words,45,138,y0,y1))
                    name_lines=_text(words,330,420,y0,y1).splitlines()
                    currency=_flat(_text(words,423,514,y0,y1))
                    exchange=_flat(_text(words,233,330,y0,y1))
                    direction=_flat(_text(words,515,610,y0,y1))
                    quantity=_d(_flat(_text(words,610,704,y0,y1)))
                    comment=_flat(_text(words,704,800,y0,y1))
                    if not re.fullmatch(DATE+' '+TIME,dt) or not name_lines or direction not in ('In','Out'):
                        raise ValueError('Moomoo security movement columns could not be read')
                    movement_legs.append(dict(type=typ,time=dt.replace('/','-'),ticker=name_lines[-1],
                        name=' '.join(name_lines[:-1]),currency=currency,exchange=exchange,shares=float(quantity),
                        direction=direction,comment=comment,source_page=page_no,source_row=_text(words,x0,x1,y0,y1)))
        # Read each fill at its Direction anchor. Order summaries can continue
        # on the next page, so attach them after sorting the full event stream.
        direction_words=[w for w in words if 48<w['x0']<52 and w['text'] in ('Buy','Sell')]
        for anchor in direction_words:
            y=anchor['top']; y0,y1=y-10,y+18
            direction=_flat(_text(words,45,123,y0,y1))
            if direction in ('Buy Amount','Buy Fee','Sell Amount','Sell Fee'):
                continue
            if direction not in ('Buy to Open','Sell to Close'):
                raise ValueError(f'Moomoo unsupported trade direction {direction!r}')
            # SGX's verified eight-column layout omits Order Capacity. The
            # explicit SGX exchange cell distinguishes it from the US layout.
            sgx = _flat(_text(words,230,324,y0,y1)) == 'SGX'
            name_end, exchange_end, currency_start, date_start = (230,324,324,418) if sgx else (207,290,375,457)
            names=_text(words,123,name_end,y0,y1).splitlines()
            dt=_flat(_text(words,date_start,550,y0,y1))
            exchange=_flat(_text(words,name_end,exchange_end,y0,y1))
            currency=_flat(_text(words,currency_start,date_start,y0,y1))
            price=_d(_flat(_text(words,550,636,y0,y1)))
            qty=_d(_flat(_text(words,636,720,y0,y1)))
            gross=_d(_flat(_text(words,720,800,y0,y1)))
            if not names or not re.fullmatch(DATE+' '+TIME,dt) or qty<=0 or price<=0:
                raise ValueError('Moomoo trade has invalid execution time, price or quantity')
            if abs(_money(price*qty)-gross)>Decimal('.01'):
                raise ValueError('Moomoo trade amount disagrees with its quantity and price')
            trade_events.append((page_no,y,'fill',dict(direction=direction,ticker=names[-1],name=' '.join(names[:-1]),
                time=dt.replace('/','-'),exchange=exchange,currency=currency,price=price,qty=qty,gross=gross,
                source_page=page_no,source_row=_text(words,45,800,y0,y1))))
        for anchor in [w for w in words if w['text']=='Subtotal:']:
            y=anchor['top']
            # The independently printed Net Amount can wrap within its cell.
            following=[w['top'] for w in words if w['top']>y+1 and w['text']=='Subtotal:']
            y1=min(y+18,*following) if following else y+18
            row=_text(words,45,800,y-1,y1)
            fee=_d(re.search(r'Subtotal:\s*('+NUMBER+r')',row)[1])
            count=int(re.search(r'Number of Transactions:\s*(\d+)',row)[1])
            anchors=[w for w in words if y-1<=w['top']<y+2 and w['text'] in {'Subtotal:','Number','Transaction','Net'} | FEE_STARTS]
            # Find the explicit Net label, then crop to the next fee label.
            net_word=next(w for w in anchors if w['text']=='Net')
            next_x=min((w['x0'] for w in anchors if w['x0']>net_word['x0']+2 and w['text'] in FEE_STARTS),default=800)
            net_text=_flat(_text(words,net_word['x0']-1,next_x-1,y-1,y1))
            net_match=re.search(r'Net Transaction Amount:\s*([+-]?)\s*('+NUMBER+r')',net_text)
            if not net_match:
                raise ValueError('Moomoo order net cash could not be read')
            net=_d((net_match[1] or '')+net_match[2])
            # A wrapped CAT fee can share its second visual line with the net
            # cash amount in another column. Crop each fee cell independently.
            fee_matches=[]
            labels=sorted((w for w in words if y-1<=w['top']<y1 and w['text'] in FEE_STARTS), key=lambda w:(w['top'],w['x0']))
            for label in labels:
                right=min((w['x0'] for w in labels if abs(w['top']-label['top'])<2 and w['x0']>label['x0']+2),default=800)
                cell=_flat(_text(words,label['x0']-1,right-1,label['top']-1,min(label['top']+12,y1)))
                match=re.fullmatch(FEE_PATTERN,cell)
                if not match:
                    raise ValueError('Moomoo order fee cell could not be read')
                fee_matches.append((match[1],match[2]))
            fees=[{'name':name,'amount':float(_d(value))} for name,value in fee_matches]
            if abs(sum((_d(str(f['amount'])) for f in fees),ZERO)-fee)>Decimal('.001'):
                raise ValueError('Moomoo order fees do not reconcile')
            trade_events.append((page_no,y,'summary',dict(fee=fee,count=count,net=net,fees=fees,source_page=page_no,source_row=row)))
        # Cash blocks can span pages. Header anchors identify their currency;
        # only dated records in the cash time column are imported, never the
        # printed buy/sell totals, financing accruals or income-detail repeats.
        cash_headers=[]
        for w in words:
            if w['text']=='Date/Time' and 280<w['x0']<300:
                cur=_flat(_text(words,44,270,w['top']-2,w['top']+10))
                if not re.fullmatch(CURRENCY,cur):
                    raise ValueError('Moomoo cash block has no currency')
                cash_headers.append((w['top'],cur))
        for idx,(header_y,ccy) in enumerate(cash_headers):
            end_y=cash_headers[idx+1][0]-2 if idx+1<len(cash_headers) else 565
            for w in words:
                if w['top']>header_y+10 and w['text'] in ('Ending','Financing','Movement') and w['x0']<55:
                    if w['text']=='Ending' and 'Ending Positions' not in _flat(_text(words,44,180,w['top']-1,w['top']+10)):
                        continue
                    end_y=min(end_y,w['top']-1)
            left=_text(words,44,270,header_y+10,end_y)
            opening=re.search(r'Starting Cash\s+('+NUMBER+r')',left)
            closing=re.search(r'Ending Cash\s+('+NUMBER+r')',left)
            if not opening or not closing:
                raise ValueError('Moomoo native cash balance could not be read')
            if ccy in cash:
                raise ValueError('Moomoo cash currency appears more than once in a statement')
            cash[ccy]={'opening':float(_d(opening[1])),'closing':float(_d(closing[1])),'source_page':page_no}
            alltext=_text(words,270,800,header_y+10,end_y)
            for side in ('Buy Amount','Buy Fee','Sell Amount','Sell Fee'):
                match=re.search(r'- '+side+r'\s+('+NUMBER+r')',alltext)
                if match:
                    cash[ccy][side.lower().replace(' ','_')]=float(_d(match[1]))
            dates=[w for w in words if 280<w['x0']<300 and re.fullmatch(DATE,w['text']) and header_y<w['top']<end_y]
            for i,anchor in enumerate(dates):
                y=anchor['top']
                lower=(dates[i-1]['top']+y)/2+3 if i else y-14
                upper=(dates[i+1]['top']+y)/2+3 if i+1<len(dates) else min(end_y,y+21)
                dt=_flat(_text(words,280,414,y-1,y+10))
                typ=_flat(_text(words,414,545,y-1,y+10))
                native=_d(_flat(_text(words,545,675,y-1,y+10)))
                comment=_flat(_text(words,675,800,lower,upper))
                ticker=None
                if typ=='Corporate Action':
                    m=re.search(r'^([A-Z][A-Z0-9.-]*)\s+[-+\d.,]+\s+SHARES',comment)
                    if not m:
                        raise ValueError('Moomoo cash corporate action has no security identity')
                    ticker=m[1]
                    action='Dividend withholding tax' if 'WITHHOLDING TAX' in comment else 'Dividend' if 'DIVIDENDS' in comment else None
                    if action is None:
                        raise ValueError('Unsupported Moomoo cash corporate action')
                elif typ=='Cash In Out': action='Deposit' if native>0 else 'Withdrawal'
                elif typ=='Monthly Interest': action='Margin interest'
                elif typ=='Stock Yield Program': action='Lending interest'
                elif typ in ('Cash Dividend', 'NRA Withholding Tax'):
                    match=re.search(r'\(([A-Z][A-Z0-9.-]*)\)\s+dividend,',comment)
                    if not match or ccy != 'USD':
                        raise ValueError('Moomoo dividend has no verified security identity')
                    ticker=match[1]
                    action='Dividend' if typ=='Cash Dividend' else 'Dividend withholding tax'
                    if (action=='Dividend' and native<=0) or (action!='Dividend' and native>=0):
                        raise ValueError('Moomoo dividend or withholding has an unsupported cash direction')
                elif typ=='Currency Exchange':
                    match=re.fullmatch(r'TRANSFER (FROM|TO) UNIVERSAL SECURITIES ACCOUNT \(([A-Z]{3}) -> ([A-Z]{3}) ('+NUMBER+r')\)',comment)
                    if not match:
                        raise ValueError('Moomoo currency exchange has no printed direction and rate')
                    exchange_legs.append(dict(time=dt.replace('/','-'),currency=ccy,native=native,
                        direction=match[1],from_currency=match[2],to_currency=match[3],rate=_d(match[4]),
                        source_page=page_no,source_row=_text(words,280,800,lower,upper),source_comment=comment))
                    continue
                else: raise ValueError(f'Unsupported Moomoo cash event {typ!r}')
                meta=_meta(action,dt.replace('/','-'),ccy,native,ticker=ticker,exchange='US' if ticker else None,
                           source_action=typ,source_comment=comment,source_page=page_no,
                           source_row=_text(words,280,800,lower,upper))
                txns.append(_txn(meta,meta['source_row']))
    pending=[]
    for page_no,y,kind,event in sorted(trade_events,key=lambda v:(v[0],v[1])):
        if kind=='fill':
            pending.append(event); continue
        if len(pending)!=event['count']:
            raise ValueError('Moomoo printed number of fills disagrees with the extracted order')
        if len({(f['direction'],f['ticker'],f['currency']) for f in pending})!=1:
            raise ValueError('Moomoo order mixes unsupported directions or securities')
        gross=sum((f['gross'] for f in pending),ZERO)
        sign=-1 if pending[0]['direction']=='Buy to Open' else 1
        if abs(event['net']-(sign*gross-event['fee']))>Decimal('.01'):
            raise ValueError('Moomoo order cash does not reconcile with fills and fees')
        # Preserve fill grain. Allocate only order-level fees pro-rata; the last
        # fill gets the rounding remainder so the order cash remains exact.
        fee_remaining=event['fee']; cash_remaining=event['net']
        for i,fill in enumerate(pending):
            fee=fee_remaining if i==len(pending)-1 else _money(event['fee']*fill['gross']/gross)
            native=cash_remaining if i==len(pending)-1 else sign*fill['gross']-fee
            fee_remaining-=fee; cash_remaining-=native
            action='Market buy' if sign<0 else 'Market sell'
            listing_exchange=None
            if fill['exchange'] in {'JNST','XNAS'}:
                matching=[p for p in snapshots+changes if p['ticker']==fill['ticker']]
                if not matching or any((p['exchange'],p['currency'])!=('US','USD') for p in matching) or fill['currency']!='USD':
                    raise ValueError('Moomoo execution venue has no matching US/USD statement position')
                listing_exchange='US'
            meta=_meta(action,fill['time'],fill['currency'],native,fill['ticker'],fill['name'],fill['exchange'],
                       listing_exchange=listing_exchange,
                       shares=float(fill['qty']*(-sign)),execution_price=float(fill['price']),
                       execution_price_major=float(fill['price']),execution_price_currency=fill['currency'],
                       execution_currency_major=fill['currency'],exchange_rate=None,
                       fees=[{'name':'Allocated order fees','amount':float(fee),'currency':fill['currency']}],
                       order_fees=[dict(f,currency=fill['currency']) for f in event['fees']],
                       fee_allocation='Printed order fees allocated by gross fill value; final fill receives rounding remainder',
                       source_page=fill['source_page'],source_row=fill['source_row'],
                       order_summary=event['source_row'],source_fill_gross=float(fill['gross']),source_action=fill['direction'],
                       trade_value_check={'currency':fill['currency'],'reported_total':float(abs(native)),
                                          'calculated_total':float(fill['gross']+fee*(-sign)),
                                          'residual':float(abs(native)-(fill['gross']+fee*(-sign))),
                                          'formula':'Printed fill amount plus buy fees or minus sell fees; fees allocated from order'})
            txns.append(_txn(meta,meta['source_row']))
        pending=[]
    if pending:
        raise ValueError('Moomoo trade fills have no independently printed order cash summary')
    paired=defaultdict(list)
    for leg in exchange_legs:
        paired[(leg['time'],leg['from_currency'],leg['to_currency'],leg['rate'])].append(leg)
    for (time,source,target,rate),legs in paired.items():
        debit=[leg for leg in legs if leg['currency']==source and leg['direction']=='FROM' and leg['native']<0]
        credit=[leg for leg in legs if leg['currency']==target and leg['direction']=='TO' and leg['native']>0]
        if len(legs)!=2 or len(debit)!=1 or len(credit)!=1 or source==target or rate<=0:
            raise ValueError('Moomoo currency exchange requires matching debit and credit evidence')
        debit,credit=debit[0],credit[0]
        if _money(-debit['native']*rate)!=credit['native']:
            raise ValueError('Moomoo currency exchange cash disagrees with its printed rate')
        rows=[{**leg,'native':float(leg['native']),'rate':float(leg['rate'])} for leg in legs]
        meta=_meta('Currency exchange',time,source,debit['native'],
                   cash_movements={source:float(debit['native']),target:float(credit['native'])},
                   exchange_rate=float(rate),exchange_from=source,exchange_to=target,source_legs=rows,
                   source_action='Currency Exchange',source_page=debit['source_page'],
                   source_row='\n'.join(leg['source_row'] for leg in legs))
        txns.append(_txn(meta,meta['source_row']))
    movements=defaultdict(list)
    for leg in movement_legs:
        if leg['type']=='Stock Gift':
            if leg['shares']<=0:
                raise ValueError('Unsupported outgoing Moomoo stock gift')
            meta=_meta('Stock gift',leg['time'],leg['currency'],ZERO,leg['ticker'],leg['name'],leg['exchange'],
                       shares=leg['shares'],gbp_value_known=False,cost_basis_native=None,
                       cost_basis_reason='Statement records free gift shares but no acquisition valuation',
                       source_page=leg['source_page'],source_row=leg['source_row'],source_action=leg['type'])
            txns.append(_txn(meta,meta['source_row']))
        else:
            movements[(leg['time'],leg['ticker'],leg['currency'])].append(leg)
    for (time,ticker,ccy),legs in movements.items():
        if len(legs)!=2 or sorted(l['direction'] for l in legs)!=['In','Out']:
            raise ValueError('Moomoo corporate action needs matching in/out evidence')
        before=-_d(str(next(l['shares'] for l in legs if l['direction']=='Out')))
        after=_d(str(next(l['shares'] for l in legs if l['direction']=='In')))
        ratios=[re.search(r'FORWARD SPLIT (\d+) For (\d+)',l['comment']) for l in legs]
        if before<=0 or after<=0 or not all(ratios) or len({(m[1],m[2]) for m in ratios})!=1:
            raise ValueError('Unsupported Moomoo share movement; no guessed corporate action imported')
        ratio=Decimal(ratios[0][1])/Decimal(ratios[0][2])
        if abs(before*ratio-after)>Decimal('0.00000001'):
            raise ValueError('Moomoo split ratio disagrees with its printed share legs')
        meta=_meta('Stock split',time,ccy,ZERO,ticker,legs[0]['name'],legs[0]['exchange'],
                   shares=float(after-before),split_ratio=float(ratio),split_before_shares=float(before),
                   split_after_shares=float(after),source_legs=legs,source_page=legs[0]['source_page'],
                   source_row='\n'.join(l['source_row'] for l in legs),gbp_value_known=False)
        txns.append(_txn(meta,meta['source_row']))
    if not cash or not snapshots or len({p['ticker'] for p in snapshots})!=len(snapshots):
        raise ValueError('Moomoo cash/position snapshot is missing or duplicated')
    reconciliation=[]
    for ccy,values in cash.items():
        native=sum((_d(str(t.meta['cash_movements'].get(ccy,0))) for t in txns),ZERO)
        printed=_d(str(values['closing']))-_d(str(values['opening']))
        residual=printed-native
        # The order's printed net cash is authoritative, including fractional
        # execution rounding. Any remaining cash difference is unexplained;
        # do not manufacture a balancing transaction or silently forgive it.
        if residual:
            raise ValueError(f'Moomoo {ccy} cash does not reconcile: difference {residual}')
        reconciliation.append({'currency':ccy,'opening':values['opening'],'closing':values['closing'],
                               'event_cash_change':float(printed),'rounding_adjustment':float(residual),'passed':True})
    by_ticker={p['ticker']:p for p in changes}
    for p in snapshots:
        change=by_ticker.get(p['ticker'])
        if not change or abs(change['shares']-p['shares'])>1e-8:
            raise ValueError(f'Moomoo independent position tables disagree for {p["ticker"]}')
        delta=sum((_d(str(t.meta.get('shares') or 0)) for t in txns if t.meta.get('ticker')==p['ticker']),ZERO)
        if abs(_d(str(change['starting_shares']))+delta-_d(str(p['shares'])))>Decimal('0.00000001'):
            raise ValueError(f'Moomoo {p["ticker"]} monthly share movement does not reconcile')
    for change in changes:
        delta=sum((_d(str(t.meta.get('shares') or 0)) for t in txns if t.meta.get('ticker')==change['ticker']),ZERO)
        if abs(_d(str(change['starting_shares']))+delta-_d(str(change['shares'])))>Decimal('0.00000001'):
            raise ValueError(f'Moomoo {change["ticker"]} monthly share movement does not reconcile')
    portfolio_sum=sum((_d(str(p['base_market_value'])) for p in snapshots),ZERO)
    if abs(portfolio_sum-_d(totals[3]))>Decimal('.005')*len(snapshots)+Decimal('.01'):
        raise ValueError('Moomoo printed portfolio total disagrees with position values')
    if abs(_d(nav[1])-sum((_d(totals[n]) for n in (2,3,4)),ZERO))>Decimal('.02'):
        raise ValueError('Moomoo printed net assets disagree with cash and securities')
    printed_counts=re.findall(r'Total Number:\s*(\d+)',full)
    if not printed_counts or sum(map(int,printed_counts))!=len(snapshots):
        raise ValueError('Moomoo printed position count does not match parsed positions')
    # Content-derived identities retain repeated fills and distinguish cash
    # currencies when native amounts have no GBP ledger equivalent.
    counts=Counter()
    for t in sorted(txns,key=lambda t:(t.meta['time'],t.type_code,t.meta.get('ticker') or '')):
        evidence={key:t.meta.get(key) for key in ('time','action','ticker','native_currency','shares','execution_price','native_amount')}
        identity=json.dumps(evidence,sort_keys=True); occurrence=counts[identity]; counts[identity]+=1
        t.ext_id='moomoo-event:'+hashlib.sha256((identity+'|'+str(occurrence)).encode()).hexdigest()
        t.meta['event_identity_method']='Exact statement event facts plus occurrence; provider does not print execution IDs'
        t.meta['source_statement_end']=end.isoformat()
        if daily:
            t.meta['source_statement_start']=start.isoformat()
    meta={'parser':'moomoo','base_currency':base_currency,'account_label':'Moomoo Margin · '+account_number[-4:],
          'snapshot_date':end.isoformat(),'previous_snapshot_date':previous_end.isoformat(),
          'positions':snapshots,'position_changes':changes,'cash_balances':{k:v['closing'] for k,v in cash.items()},
          'opening_cash_balances':{k:v['opening'] for k,v in cash.items()},'cash_reconciliation':reconciliation,
          'net_asset_value':float(_d(nav[1])),'assets_in_transit':float(_d(totals[2])),
          'portfolio_value':float(_d(totals[3])),'cash_balance_base':float(_d(totals[4])),
          'native_reconciled':True,'snapshot_is_current':False,
          'source_timezone':'unspecified','first_event_date':min((t.txn_date.isoformat() for t in txns),default=None),
          'last_event_date':max((t.txn_date.isoformat() for t in txns),default=None),
          'snapshot_timing_note':'Broker monthly totals can include dividends posted on the next business day; original event timestamps are preserved.'}
    if daily:
        meta.update(statement_type='daily', opening_net_asset_value=_daily_opening_nav(pages[0]['text'],base_currency),
                    snapshot_timing_note='Broker daily totals can include cash events posted on the next business day; original event timestamps are preserved.')
    return ParsedStatement(bank='Moomoo',account_type='investment',account_id='Moomoo-'+account_number,
            sort_code=None,account_number=account_number,period_start=start,period_end=end,
            opening_balance=None,closing_balance=None,txns=sorted(txns,key=lambda t:t.meta['time']),source_file=path,meta=meta)
