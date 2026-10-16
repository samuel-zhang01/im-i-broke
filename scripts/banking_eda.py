"""Read-only banking exploration; produces aggregates, never credentials/raw rows.

Usage: PYTHONPATH=backend python scripts/banking_eda.py DATABASE --output FILE
Monetary findings are recorded GBP ledger movements, not an estimate of missing
history. Foreign bank-cache observations are counted separately, never summed.
"""
import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app import analytics


def explore(path, today=None):
    today=today or date.today()
    path=Path(path).resolve();c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
    allrows=analytics._rows(c)
    bank=[r for r in allrows if r['account_type']!='investment']
    accounts=[dict(r) for r in c.execute("SELECT id,bank,account_type,currency FROM accounts WHERE account_type!='investment'")]
    movements=[v for r in bank if (v:=analytics._household_component(r)) is not None]
    spending=[r for r in movements if r['amount']<0]
    refunds=[r for r in movements if r['amount']>0 and r['is_refund']]
    gross=sum(-r['amount'] for r in spending);refunded=sum(r['amount'] for r in refunds)
    category=defaultdict(lambda:{'payments':0,'gross':0,'refunds':0})
    for r in movements:
        key=r['category'] or 'Uncategorized'
        if r['amount']<0:category[key]['payments']+=1;category[key]['gross']-=r['amount']
        elif r['is_refund']:category[key]['refunds']+=r['amount']
    monthly=analytics._aggregate_months(movements)
    coverage=analytics.month_coverage(c,[r['m'] for r in monthly],today=today)
    for m in monthly:
        observed={r['account_id'] for r in bank if r['txn_date'].startswith(m['m'])}
        m.update(observed_accounts=len(observed),statement_coverage_complete=coverage[m['m']]['coverage_complete'],missing_statement_accounts=len(coverage[m['m']]['missing_accounts']))
    bymonth=defaultdict(list)
    for r in spending:bymonth[r['txn_date'][:7]].append(r)
    for m in monthly:
        rows=bymonth[m['m']];m['spending_payments']=len(rows)
        m['uncategorized_payments']=sum(r['category'] in (None,'','Uncategorized') for r in rows)
        m['uncategorized_gross']=round(sum(-r['amount'] for r in rows if r['category'] in (None,'','Uncategorized')),2)
    merchant=defaultdict(lambda:{'payments':0,'gross':0})
    for r in spending:
        item=merchant[r['merchant'] or r['source_merchant'] or r['description']];item['payments']+=1;item['gross']-=r['amount']
    ranked=sorted(merchant.values(),key=lambda r:-r['gross'])
    unknown=[r for r in spending if r['category'] in (None,'','Uncategorized')]
    counts=Counter(r['merchant_kind'] or 'unknown' for r in unknown)
    unknown_by_label=defaultdict(lambda:{'payments':0,'gross':0})
    for r in unknown:
        item=unknown_by_label[r['merchant'] or r['source_merchant'] or r['description']];item['payments']+=1;item['gross']-=r['amount']
    account_report=[]
    for a in accounts:
        ar=[r for r in bank if r['account_id']==a['id']]
        feed=c.execute('SELECT id FROM bank_connections WHERE account_id=?',(a['id'],)).fetchone()
        observations=c.execute('SELECT count(*),min(txn_date),max(txn_date) FROM bank_transactions WHERE connection_id=?',(feed[0],)).fetchone() if feed else (0,None,None)
        account_report.append({'institution':a['bank'],'type':a['account_type'],'currency':a['currency'],'ledger_rows':len(ar),'ledger_first':min((r['txn_date'] for r in ar),default=None),'ledger_last':max((r['txn_date'] for r in ar),default=None),'cached_rows':observations[0],'cache_first':observations[1],'cache_last':observations[2]})
    total=sum(c.execute('select count(*) from transactions').fetchone())
    result={'as_of':today.isoformat(),'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
      'scope':{'all_ledger_rows':total,'bank_ledger_rows':len(bank),'excluded_investment_rows':total-len(bank),'bank_accounts':len(accounts),'movement_rows':len(movements),'spending_rows':len(spending),'refund_rows':len(refunds),'gross_spending':round(gross,2),'refunds':round(refunded,2),'net_spending':round(gross-refunded,2),'currency':'GBP','date_first':min(r['txn_date'] for r in bank),'date_last':max(r['txn_date'] for r in bank)},
      'quality':{'foreign_key_errors':len(c.execute('PRAGMA foreign_key_check').fetchall()),'integrity':c.execute('PRAGMA integrity_check').fetchone()[0], 'null_bank_dates':c.execute("select count(*) from transactions t join accounts a on a.id=t.account_id where a.account_type!='investment' and t.txn_date is null").fetchone()[0], 'future_bank_dates':sum(r['txn_date']>today.isoformat() for r in bank), 'zero_amount_bank_rows':sum(r['amount']==0 for r in bank),'zero_ledger_accounts':sum(a['ledger_rows']==0 for a in account_report),'observed_months':len(monthly),'all_accounts_complete_months':sum(r['statement_coverage_complete'] for r in monthly)},
      'bank_cache_states':[dict(r) for r in c.execute('select status,currency,is_pending,count(*) records from bank_transactions group by status,currency,is_pending order by status,currency')],
      'source_links':{'bank_rows_with_statement':sum(r['statement_id'] is not None for r in bank),'bank_rows_without_statement':sum(r['statement_id'] is None for r in bank)},
      'exclusion_counts':dict(Counter(analytics._exclusion_reason(r) or 'Included movement' for r in bank)),
      'category_totals':[{'category':key,**{k:round(v,2) for k,v in val.items()},'net':round(val['gross']-val['refunds'],2)} for key,val in sorted(category.items(),key=lambda p:-p[1]['gross'])],
      'concentration':{'merchant_labels':len(merchant),'top_5_gross_share':sum(r['gross'] for r in ranked[:5])/gross,'top_10_gross_share':sum(r['gross'] for r in ranked[:10])/gross,'payments_under_10':sum(-r['amount']<10 for r in spending),'median_payment':statistics.median(-r['amount'] for r in spending)},
      'uncategorized':{'payments':len(unknown),'gross':round(sum(-r['amount'] for r in unknown),2),'payment_share':len(unknown)/len(spending),'gross_share':sum(-r['amount'] for r in unknown)/gross,'label_count':len(unknown_by_label),'merchant_kinds':dict(counts),'top10_labels_gross_share':sum(r['gross'] for r in sorted(unknown_by_label.values(),key=lambda r:-r['gross'])[:10])/sum(-r['amount'] for r in unknown)},
      'accounts':account_report,'monthly':monthly,
      'caveat':'One personal ledger, unequal account histories, incomplete statement coverage and unresolved bank overlaps. No usage telemetry or verified anomaly truth; design priorities are inferred from record structure and tested tasks, not measured user behaviour.'}
    c.rollback();c.close();return result


def main():
    p=argparse.ArgumentParser();p.add_argument('database');p.add_argument('--output',required=True);args=p.parse_args()
    result=explore(args.database);target=Path(args.output);target.write_text(json.dumps(result,indent=2));target.chmod(0o600)
    print(json.dumps({k:result[k] for k in ['scope','quality','concentration','uncategorized','exclusion_counts']},indent=2))
if __name__=='__main__':main()
