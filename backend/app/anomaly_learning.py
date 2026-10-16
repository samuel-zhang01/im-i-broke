"""Explainable banking review candidates using only earlier observed payments.

No training, provider calls, automatic decisions, or calibrated fraud scores.
Amounts are the existing GBP household ledger components; investments use the
legacy detector and never enter these banking comparison groups.
"""
from collections import defaultdict,deque
from datetime import date,timedelta
from itertools import groupby
import re
import statistics
from .workspace import semantic

WINDOW_DAYS=365
EVERYDAY_CATEGORIES={'Transport','Eating out','Groceries','Fees & interest'}
NO_CATEGORY_COMPARISON={'Uncategorized','Cash','Friends & shared costs','Housing','Subscriptions','Phone & internet','Bills & utilities'}


def merchant_key(row):
    from .merchants import _transfer_context
    name=(row.get('merchant') or '').casefold().strip()
    context=_transfer_context(row)
    if context:
        name=re.sub(r'^(?:(?:transfer|payment) (?:to|from)|to|from)\s+','',name)
    return row['account_id'],name,row.get('category') or 'Uncategorized',context


def detect(rows,z=3.5,min_history=5):
    rows=sorted((row for row in rows if row.get('account_type')!='investment'),key=lambda row:(row['txn_date'],row['id']))
    keys={row["id"]:merchant_key(row) for row in rows}
    merchant_history=defaultdict(deque);category_history=defaultdict(deque);duplicate_history=defaultdict(deque)
    results=[]
    for day_text,day_rows in groupby(rows,key=lambda row:row['txn_date'][:10]):
        today=date.fromisoformat(day_text);cutoff=(today-timedelta(days=min(WINDOW_DAYS,today.toordinal()-1))).isoformat()
        daily=list(day_rows);statistics_cache={}
        for row in daily:
            account,name,category,context=key=keys[row["id"]]
            peers=merchant_history[key]
            while peers and peers[0]['txn_date'][:10]<cutoff:peers.popleft()
            basis='merchant';minimum=max(3,min_history)
            if len(peers)<minimum:
                basis='category';peers=category_history[(account,category,context)]
                while peers and peers[0]['txn_date'][:10]<cutoff:peers.popleft()
                # Bills, rent, subscriptions and person payments have distinct
                # legitimate price levels. A cheap neighbouring service does
                # not establish a safe comparison for a new provider.
                if (semantic(row) in NO_CATEGORY_COMPARISON or row.get('merchant_kind')=='person'
                        or len(peers)<max(20,min_history)
                        or len({keys[peer['id']][1] for peer in peers})<5):
                    peers=[]
            candidate=None
            if peers:
                group=(basis,key if basis=='merchant' else (account,category,context))
                if group not in statistics_cache:
                    values=[abs(peer['amount']) for peer in peers]
                    median=statistics.median(values);mad=statistics.median(abs(amount-median) for amount in values)
                    scale=max(1.4826*mad,median*.1,1.0)
                    threshold=max(median+z*scale,median+(25 if basis=='merchant' else 50),median*(1.5 if basis=='merchant' else 5))
                    statistics_cache[group]=(median,scale,threshold)
                median,scale,threshold=statistics_cache[group]
                amount=abs(row['amount'])
                if amount>=threshold:
                    priority=('high' if amount>=max(250,threshold*2) else 'medium') if basis=='merchant' else ('medium' if amount>=250 else 'low')
                    basis_label='this merchant and account' if basis=='merchant' else 'this account’s category'
                    candidate={**row,'kind':'large_amount','reason':f"Larger than earlier {basis} payments",'z':round((amount-median)/scale,1),
                        'score_method':'prior median/MAD','baseline_amount':round(median,2),'threshold_amount':round(threshold,2),
                        'history_count':len(peers),'comparison_basis':basis,'baseline_from':peers[0]['txn_date'][:10],
                        'baseline_to':peers[-1]['txn_date'][:10],'comparison_transaction_ids':[peer['id'] for peer in list(peers)[-5:]],
                        'priority':priority,'priority_reason':'Size of the payment and strength of its earlier comparison history.',
                        'evidence_strength':'moderate' if basis=='merchant' else 'limited','method':'bank_history_v2',
                        'evidence_note':'Only strictly earlier dates within 365 days; no same-day or future payments enter the amount baseline.',
                        'explanation':f"£{amount:,.2f} compared with an earlier median of £{median:,.2f} across {len(peers)} payments for {basis_label}. Review threshold £{threshold:,.2f}. Categories contain different purchases; this is a review prompt, not evidence of fraud."}
            if name:
                dup_key=(account,name,round(abs(row['amount']),2))
                previous=duplicate_history[dup_key]
                while previous and (today-date.fromisoformat(previous[0]['txn_date'][:10])).days>2:previous.popleft()
                if previous:
                    same_day=[peer for peer in previous if peer['txn_date'][:10]==day_text]
                    comparison=same_day or list(previous)
                    small_everyday=abs(row['amount'])<25 and semantic(row) in EVERYDAY_CATEGORIES
                    priority='low' if not same_day or small_everyday else ('high' if abs(row['amount'])>=250 else 'medium')
                    if candidate:
                        candidate['related_kinds']=['large_amount','possible_duplicate']
                        candidate['duplicate_transaction_ids']=[peer['id'] for peer in comparison[-5:]]
                    else:
                        candidate={**row,'kind':'possible_duplicate','reason':'Repeated same-day amount' if same_day else 'Repeated amount on nearby dates',
                            'z':None,'comparison_basis':'same_account_merchant_amount','history_count':len(comparison),
                            'baseline_from':comparison[0]['txn_date'][:10],'baseline_to':comparison[-1]['txn_date'][:10],
                            'comparison_transaction_ids':[peer['id'] for peer in comparison[-5:]],'priority':priority,
                            'priority_reason':'Small routine purchases or different posting dates often repeat legitimately.' if priority=='low' else 'The same account has another same-day payment to this merchant for this amount.',
                            'evidence_strength':'limited','method':'bank_history_v2',
                            'evidence_note':'Matching amounts do not establish duplicate billing. Check the linked transactions or your receipt.',
                            'explanation':f"{len(comparison)} other payment(s) on {'the same date' if same_day else 'the preceding two days'} match the same account, merchant and amount. Both remain in spending until you decide otherwise."}
                previous.append(row)
            if candidate:results.append(candidate)
        # Add this date only after every payment has been assessed, so statement
        # ordering and repeated charges cannot leak into their own baseline.
        for row in daily:
            key=keys[row["id"]]
            if key[1]:merchant_history[key].append(row)
            if row.get('merchant_kind')!='person':category_history[(key[0],key[2],key[3])].append(row)
    return results
