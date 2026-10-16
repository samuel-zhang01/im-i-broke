"""Synthetic extracted PDF layouts: financial facts and ruled-column boundaries."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.db import init_db
from app.ingest import _store_statement, reconciled
from app.parsers import moomoo


def words_at(text,x,y):
    result=[]
    for token in text.split():
        width=max(2,len(token)*2.2)
        result.append(dict(text=token,x0=x,x1=x+width,top=y,bottom=y+5))
        x+=width+2
    return result


def table(bbox,header):
    return SimpleNamespace(rows=[SimpleNamespace(bbox=bbox)]), [[header]]


def fixture(*, fills=(2,), cash='799.00', price='100.0000', gift=False, split=False):
    """One independently balanced statement, no personal data or source files."""
    shares=sum(fills)+(0.1 if gift else 0)
    if split: shares=20
    text=('Monthly Statement of Margin Account (1234) - Securities\n'
          'Moomoo Financial Singapore\nAccount Number 999900001234 Account Type Margin\n'
          'Starting Net Asset Value 20260630\nEnding Net Asset Value 20260731\n'
          f'Net Asset Value: {float(cash)*1.3+shares*110*1.3:.2f}\n'
          f'Base Currency: SGD 0.00 {shares*110*1.3:.2f} {float(cash)*1.3:.2f}\nTotal Number: 1')
    w=[]
    def put(t,x,y):w.extend(words_at(t,x,y))
    for i,qty in enumerate(fills):
        y=100+i*30
        put('Buy to Open',49.5,y);put('Apple',127,y-4);put('AAPL',127,y+6)
        put('US',211,y);put('Agency',295,y);put('USD',378,y)
        put('2026/07/06',462,y-4);put('12:34:56',462,y+6)
        put(price,607,y);put(str(qty),692,y);put(f'{qty*100:.2f}',764,y)
    sy=100+len(fills)*30+18
    for t,x in [('Subtotal: 1.00',49.8),(f'Number of Transactions: {len(fills)}',132),
                (f'Transaction Amount: {sum(fills)*100:.2f}',215),
                (f'Net Transaction Amount: -{sum(fills)*100+1:.2f}',297),
                ('Platform Fees: 1.00',397)]:put(t,x,sy)
    put('USD',53,240);put('Date/Time',287,240);put('Type',418,240);put('Amount',549,240);put('Comment',680,240)
    put('Starting Cash 0.00',53,265);put('Ending Cash '+cash,53,290)
    put('Ending Settled Cash '+cash,53,310);put('Ending Unsettled Cash 0.00',53,330)
    put('2026/07/06 10:00:00',287,360);put('Cash In Out',418,360);put('+1,000.00',549,360);put('Synthetic deposit',680,360)
    positions=[]
    def pw(t,x,y):positions.extend(words_at(t,x,y))
    pw('Apple',50,87);pw('AAPL',50,98)
    for t,x in [('US',110),('USD',155),('0',227),('-',275),('0.00',350),
                (str(shares),410),('110.0000',445),(f'{shares*110:.2f}',534),(f'+{shares*110:.2f}',579),
                (str(sum(fills)),640),('0',690),('0.1' if gift else '0',740),('0',790)]:pw(t,x,91)
    pw('Apple',50,187);pw('AAPL',50,198)
    for t,x in [('US',112),('USD',175),(str(shares),274),('0',352),(str(shares),400),('1',479),
                ('110.0000',520),(f'{shares*110:.2f}',650),('1.300000',710),(f'{shares*143:.2f}',775)]:pw(t,x,191)
    tables=[table((44,82,800,113),'Starting Buy Quantity'),table((44,182,800,213),'Settled Quantity')]
    if gift:
        for t,x,y in [('2026/07/20',50,303),('09:00:00',50,313),('Stock Gift',144,309),('US',238,309),
                      ('Apple',333,303),('AAPL',333,313),('USD',427,309),('In',522,309),('+0.1',616,309),('Gift Share',710,309)]:pw(t,x,y)
        tables.append(table((44,298,800,326),'Date/Time Type Direticon Quantity'))
    if split:
        for idx,(direction,quantity) in enumerate([('Out','-2'),('In','+20')]):
            y=303+idx*35
            for t,x,dy in [('2026/07/20',50,0),('09:00:00',50,10),('Corporate Action',144,6),('US',238,6),
                           ('Apple',333,0),('AAPL',333,10),('USD',427,6),(direction,522,6),(quantity,616,6),
                           ('AAPL FORWARD SPLIT 10',710,-3),('For 1, POSITION',710,7)]:pw(t,x,y+dy)
            tables.append(table((44,y-8,800,y+25),'Date/Time Type Direticon Quantity'))
    return [{'words':w,'text':text,'tables':[]},{'words':positions,'text':'Securities','tables':tables}]


def daily(pages, *, previous='20260705', ending='20260706', opening_usd=0):
    """Add the independently printed six-currency opening NAV layout."""
    pages=deepcopy(pages)
    opening=(f'Starting Net Asset Value {previous}\nSGD USD HKD CNH JPY KRW\n'
             f'Equal to(SGD)\n0.00 {opening_usd:.2f} 0.00 0.00 0.00 0.00\n'
             f'{opening_usd*1.3:.2f}\nexchange rate : - exchange rate : 1.300000 '
             'exchange rate : - exchange rate : - exchange rate : - exchange rate : -')
    pages[0]['text']=pages[0]['text'].replace('Monthly Statement','Daily Statement').replace(
        'Starting Net Asset Value 20260630',opening).replace('Ending Net Asset Value 20260731',f'Ending Net Asset Value {ending}')
    return pages


def sgx_exchange_fixture():
    """Four identical SGX fills and exactly two printed currency-exchange legs."""
    pages=daily(fixture(fills=(1,1,1,1),cash='119.00'),opening_usd=400)
    pages[0]['text']=pages[0]['text'].replace('Net Asset Value: 726.70','Net Asset Value: 559.00').replace(
        'Base Currency: SGD 0.00 572.00 154.70','Base Currency: SGD 0.00 440.00 119.00')
    w=[]
    def put(t,x,y):w.extend(words_at(t,x,y))
    for i in range(4):
        y=100+i*30
        for t,x,dy in [('Buy to Open',49.5,0),('DBS',138,-4),('D05',138,6),('SGX',234,0),
                       ('SGD',328,0),('2026/07/06',422,-4),('12:34:56',422,6),
                       ('100.0000',600,0),('1',692,0),('100.00',764,0)]:put(t,x,y+dy)
    sy=238
    for t,x in [('Subtotal: 1.00',49.8),('Number of Transactions: 4',132),('Transaction Amount: 400.00',215),
                ('Net Transaction Amount: -401.00',297),('Commission: 0.40',397),('Trading Fees: 0.20',490),
                ('Clearing Fees: 0.30',583),('Consumption Tax: 0.10',682)]:put(t,x,sy)
    for t,x,y in [('USD',53,300),('Date/Time',287,300),('Starting Cash 400.00',53,325),
                  ('Ending Cash 0.00',53,345),('2026/07/06 10:00:00',287,400),
                  ('Currency Exchange',418,400),('-400.00',549,400),
                  ('TRANSFER FROM UNIVERSAL',680,390),('SECURITIES ACCOUNT',680,400),
                  ('(USD -> SGD 1.30000)',680,410)]:put(t,x,y)
    pages[0]['words']=w
    for item in pages[1]['words']:
        item['text']={'Apple':'DBS','AAPL':'D05','US':'SGX','USD':'SGD','1.300000':'1.000000','572.00':'440.00'}.get(item['text'],item['text'])
    sgd=[]
    for t,x,y in [('SGD',53,100),('Date/Time',287,100),('Starting Cash 0.00',53,125),
                  ('Ending Cash 119.00',53,145),('2026/07/06 10:00:00',287,200),
                  ('Currency Exchange',418,200),('+520.00',549,200),
                  ('TRANSFER TO UNIVERSAL',680,190),('SECURITIES ACCOUNT',680,200),
                  ('(USD -> SGD 1.30000)',680,210)]:sgd.extend(words_at(t,x,y))
    pages.append({'words':sgd,'text':'Cash movements','tables':[]})
    return pages


class MoomooDailyParserTests(unittest.TestCase):
    def parse_pages(self,pages):
        with patch.object(moomoo,'_extract_pages',return_value=pages):return moomoo.parse('synthetic.pdf')

    def test_daily_weekend_interval_preserves_both_printed_anchor_dates(self):
        s=self.parse_pages(daily(fixture(),previous='20260703'))
        self.assertEqual((s.period_start,s.period_end),(date(2026,7,4),date(2026,7,6)))
        self.assertEqual(s.meta['previous_snapshot_date'],'2026-07-03')
        self.assertEqual(s.meta['opening_net_asset_value'],0)
        self.assertEqual(s.meta['statement_type'],'daily')
        self.assertTrue(all(t.meta['source_statement_start']=='2026-07-04' for t in s.txns))

    def test_daily_and_monthly_execution_identities_are_identical(self):
        monthly=self.parse_pages(fixture(fills=(1,1)))
        short=self.parse_pages(daily(fixture(fills=(1,1))))
        self.assertEqual([t.ext_id for t in monthly.txns],[t.ext_id for t in short.txns])
        self.assertNotIn('statement_type',monthly.meta)
        self.assertNotIn('opening_net_asset_value',monthly.meta)
        self.assertTrue(all('source_statement_start' not in t.meta for t in monthly.txns))

    def test_daily_opening_nav_must_match_independent_currency_breakdown(self):
        pages=daily(fixture())
        pages[0]['text']=pages[0]['text'].replace('0.00\nexchange rate','1.00\nexchange rate')
        with self.assertRaisesRegex(ValueError,'opening NAV disagrees'):self.parse_pages(pages)
        pages=daily(fixture(),previous='20260601')
        with self.assertRaisesRegex(ValueError,'reporting interval'):self.parse_pages(pages)

    def test_sgx_fills_keep_exact_native_cash_and_paired_fx(self):
        s=self.parse_pages(sgx_exchange_fixture())
        fills=[t for t in s.txns if t.type_code=='Market buy']
        self.assertEqual(len(fills),4)
        self.assertEqual(len({t.ext_id for t in fills}),4)
        self.assertEqual(sum(t.meta['native_amount'] for t in fills),-401)
        self.assertTrue(all(t.meta['isin']=='SG1L01001701' and t.meta['exchange']=='SGX' for t in fills))
        fx=next(t for t in s.txns if t.type_code=='Currency exchange')
        self.assertEqual(fx.meta['cash_movements'],{'USD':-400,'SGD':520})
        self.assertEqual(len(fx.meta['source_legs']),2)
        self.assertIsNone(fx.meta['shares'])
        self.assertEqual(s.meta['cash_balances'],{'USD':0,'SGD':119})
        self.assertEqual(s.meta['opening_net_asset_value'],520)
        self.assertEqual(s.meta['net_asset_value'],559)
        self.assertEqual(moomoo._identity('O39','SGX')['isin'],'SG1S04926220')

    def test_missing_or_inconsistent_exchange_leg_is_rejected(self):
        pages=sgx_exchange_fixture();pages.pop()
        with self.assertRaisesRegex(ValueError,'matching debit and credit'):self.parse_pages(pages)
        pages=sgx_exchange_fixture()
        for w in pages[-1]['words']:
            if w['text']=='+520.00':w['text']='+521.00'
        with self.assertRaisesRegex(ValueError,'printed rate'):self.parse_pages(pages)

    def test_us_execution_venue_keeps_independent_listing_identity(self):
        for venue in ('JNST','XNAS'):
            with self.subTest(venue=venue):
                pages=daily(fixture())
                next(w for w in pages[0]['words'] if w['text']=='US')['text']=venue
                s=self.parse_pages(pages);trade=next(t for t in s.txns if t.type_code=='Market buy')
                self.assertEqual(trade.meta['exchange'],venue)
                self.assertEqual(trade.meta['listing_exchange'],'US')
                self.assertEqual(trade.meta['isin'],'US0378331005')
                for w in pages[1]['words']:
                    if w['text']=='AAPL':w['text']='MSFT'
                with self.assertRaisesRegex(ValueError,'no matching US/USD'):self.parse_pages(pages)

    def test_unknown_sgx_security_or_market_currency_fails_closed(self):
        pages=sgx_exchange_fixture()
        for page in pages:
            for w in page['words']:
                if w['text']=='D05':w['text']='FAKE'
        with self.assertRaisesRegex(ValueError,'verified security mapping'):self.parse_pages(pages)
        pages=sgx_exchange_fixture()
        for w in pages[1]['words']:
            if w['text']=='SGD':w['text']='USD'
        with self.assertRaisesRegex(ValueError,'columns could not be aligned'):self.parse_pages(pages)

    def test_wrapped_net_cash_does_not_become_the_adjacent_zero_cat_fee(self):
        pages=daily(fixture())
        pages[0]['words']=[w for w in pages[0]['words'] if not 147<=w['top']<=166]
        for t,x,dy in [('Subtotal: 1.00',49.8,0),('Number of Transactions: 1',132,0),
                       ('Transaction Amount: 200.00',215,0),('Net Transaction Amount:',297,0),
                       ('-201.00',297,5.4),('Platform Fees: 0.70',397,0),('SEC Fees: 0.10',490,0),
                       ('Trading Activity Fees: 0.10',585,0),('Consolidated Audit Trail Fees:',690,0),
                       ('0.00',690,5.4),('Consumption Tax: 0.10',397,10.8)]:
            pages[0]['words'].extend(words_at(t,x,148+dy))
        s=self.parse_pages(pages);trade=next(t for t in s.txns if t.type_code=='Market buy')
        self.assertEqual(trade.meta['native_amount'],-201)
        self.assertEqual({f['name']:f['amount'] for f in trade.meta['order_fees']},{
            'Platform Fees':.7,'SEC Fees':.1,'Trading Activity Fees':.1,
            'Consolidated Audit Trail Fees':0,'Consumption Tax':.1})
        next(w for w in pages[0]['words'] if w['text']=='0.00' and w['x0']==690)['text']='0.01'
        with self.assertRaisesRegex(ValueError,'fees do not reconcile'):self.parse_pages(pages)

    def test_new_dividend_cash_types_preserve_posted_dates_and_withholding(self):
        pages=daily(fixture(cash='799.07'))
        for y,typ,amount in [(410,'Cash Dividend','+0.10'),(450,'NRA Withholding Tax','-0.03')]:
            for t,x,dy in [('2026/07/07 17:22:46',287,0),(typ,418,0),(amount,549,0),
                           ('MICROSOFT CORP',680,-10),('COM(MSFT) dividend,',680,0),('USD 0.91 per share',680,10)]:
                pages[0]['words'].extend(words_at(t,x,y+dy))
        s=self.parse_pages(pages);income=[t for t in s.txns if t.type_code.startswith('Dividend')]
        self.assertEqual([t.type_code for t in income],['Dividend','Dividend withholding tax'])
        self.assertEqual([t.meta['native_amount'] for t in income],[.1,-.03])
        self.assertTrue(all(t.txn_date==date(2026,7,7) and t.meta['ticker']=='MSFT' for t in income))
        self.assertEqual(s.period_end,date(2026,7,6))

class MoomooParserTests(unittest.TestCase):
    def parsed(self,**kwargs):
        with patch.object(moomoo,'_extract_pages',return_value=fixture(**kwargs)):
            return moomoo.parse('synthetic.pdf')

    def test_native_cash_cost_units_and_snapshot_are_separate(self):
        s=self.parsed();self.assertTrue(reconciled(s))
        self.assertIsNone(s.opening_balance);self.assertIsNone(s.closing_balance)
        self.assertEqual(s.account_id,'Moomoo-999900001234')
        self.assertEqual(s.period_start,date(2026,7,1))
        trade=next(t for t in s.txns if t.type_code=='Market buy')
        self.assertEqual(trade.amount,0)
        self.assertEqual(trade.meta['cash_movements'],{'USD':-201})
        self.assertEqual(trade.meta['shares'],2)
        self.assertFalse(trade.meta['gbp_value_known'])
        self.assertIsNone(trade.meta['cost_basis_gbp'])
        self.assertEqual(trade.meta['time_timezone'],'unspecified')
        self.assertEqual(s.meta['positions'][0]['shares'],2)
        self.assertEqual(s.meta['cash_balances'],{'USD':799})
        self.assertFalse(trade.meta['security_identity']['printed_isin'])

    def test_same_second_identical_fills_survive_with_order_fees_once(self):
        s=self.parsed(fills=(1,1));trades=[t for t in s.txns if t.type_code=='Market buy']
        self.assertEqual(len(trades),2)
        self.assertEqual(len({t.ext_id for t in trades}),2)
        self.assertEqual(sum(t.meta['native_amount'] for t in trades),-201)
        self.assertEqual([t.meta['fees'][0]['amount'] for t in trades],[.5,.5])

    def test_unreconciled_cash_fails_instead_of_manufacturing_balance(self):
        with self.assertRaisesRegex(ValueError,'cash does not reconcile'):
            self.parsed(cash='899.00')

    def test_unexplained_one_cent_is_not_forgiven_or_balanced(self):
        with self.assertRaisesRegex(ValueError,'cash does not reconcile'):
            self.parsed(cash='799.01')

    def test_execution_price_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError,'amount disagrees'):
            self.parsed(price='900.00')

    def test_gift_is_not_purchase_and_does_not_invent_cost(self):
        s=self.parsed(gift=True)
        gift=next(t for t in s.txns if t.type_code=='Stock gift')
        self.assertEqual(gift.meta['shares'],.1)
        self.assertEqual(gift.meta['native_amount'],0)
        self.assertIsNone(gift.meta['cost_basis_native'])
        self.assertEqual(sum(t.meta.get('shares') or 0 for t in s.txns),2.1)

    def test_split_legs_collapse_to_one_cost_preserving_event(self):
        s=self.parsed(split=True)
        split=next(t for t in s.txns if t.type_code=='Stock split')
        self.assertEqual(split.meta['split_ratio'],10)
        self.assertEqual(split.meta['shares'],18)
        self.assertEqual(split.meta['split_before_shares'],2)
        self.assertEqual(split.meta['split_after_shares'],20)
        self.assertEqual(len(split.meta['source_legs']),2)
        self.assertEqual(split.meta['native_amount'],0)

    def test_real_table_vertical_glyph_jitter_keeps_numeric_column_order(self):
        pages=fixture()
        for w in pages[1]['words']:
            if w['text']=='US': w['top']-=2.5;w['bottom']-=2.5
        with patch.object(moomoo,'_extract_pages',return_value=pages):
            s=moomoo.parse('synthetic.pdf')
        self.assertEqual(s.meta['positions'][0]['shares'],2)

    def test_reimport_preserves_ids_reviews_raw_and_source_observations(self):
        with TemporaryDirectory() as tmp:
            conn=init_db(Path(tmp)/'qa.db');s=self.parsed(fills=(1,1))
            try:
                a=_store_statement(conn,s,'first.pdf');conn.commit()
                before=[dict(r) for r in conn.execute('SELECT * FROM transactions ORDER BY id')]
                conn.execute("INSERT INTO overrides(dedupe_key,note) VALUES(?,'Keep note')",(before[0]['dedupe_key'],));conn.commit()
                b=_store_statement(conn,s,'renamed.pdf');conn.commit()
                self.assertEqual((a['new_txns'],b['new_txns']),(3,0))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0],1)
                after=[dict(r) for r in conn.execute('SELECT * FROM transactions ORDER BY id')]
                for old,new in zip(before,after):
                    for key in ('id','raw','dedupe_key','statement_id','amount'):
                        self.assertEqual(old[key],new[key])
                    self.assertEqual(len(json.loads(new['meta'])['source_observations']),2)
                self.assertEqual(conn.execute('SELECT note FROM overrides').fetchone()[0],'Keep note')
                self.assertEqual(_store_statement(conn,s,'renamed.pdf')['new_txns'],0)
            finally:conn.close()

    def test_changed_monthly_events_fail_without_deleting_original(self):
        with TemporaryDirectory() as tmp:
            conn=init_db(Path(tmp)/'qa.db')
            try:
                s=self.parsed();_store_statement(conn,s,'first.pdf');conn.commit()
                changed=deepcopy(s);changed.txns=changed.txns[:-1]
                with self.assertRaisesRegex(ValueError,'changed events'):
                    _store_statement(conn,changed,'replacement.pdf')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],2)
            finally:conn.close()

    def test_changed_snapshot_reexport_is_rejected(self):
        with TemporaryDirectory() as tmp:
            conn=init_db(Path(tmp)/'qa.db')
            try:
                s=self.parsed();_store_statement(conn,s,'first.pdf');conn.commit()
                changed=deepcopy(s);changed.meta['positions'][0]['price']=900
                with self.assertRaisesRegex(ValueError,'changed its cash or position snapshot'):
                    _store_statement(conn,changed,'replacement.pdf')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM statements').fetchone()[0],1)
            finally:conn.close()

    def test_unknown_security_fails_instead_of_mapping_arbitrary_ticker(self):
        with self.assertRaisesRegex(ValueError,'verified identity'):
            moomoo._identity('NOTREAL','US')


if __name__=='__main__':unittest.main()
