"""Preview/apply sourced exact aliases to uncategorised banking transactions.

The default runs entirely in memory. --apply writes one local transaction.
Explicit reviews, existing identities, source evidence, money and investments
are protected. A separately reviewed repair handles unusable API merchant refs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

from app import merchants, lunchflow_store
from app.categorize import apply_categorization
from app.analytics import recompute_all


def apply_research(conn, entries, *, repair_labels=False):
    conn.execute('BEGIN IMMEDIATE')
    try:
        before_uncat=conn.execute("SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.account_type!='investment' AND t.category='Uncategorized'").fetchone()[0]
        repairs=lunchflow_store.merchant_label_repairs(conn) if repair_labels else []
        for repair in repairs:
            conn.execute('UPDATE transactions SET source_merchant=?,meta=? WHERE id=?',(repair['source_merchant'],repair['meta'],repair['transaction_id']))
        if repairs:apply_categorization(conn,transaction_ids={r['transaction_id'] for r in repairs})
        rows=[merchants._source_row(r) for r in conn.execute("""SELECT t.*,a.bank,a.account_type FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.account_type!='investment'""")]
        reviews={row[0] for row in conn.execute('SELECT dedupe_key FROM overrides')}
        eligible={r['id'] for r in rows if r['category']=='Uncategorized' and r['amount']<0 and r['category_source']!='user'
                  and r['dedupe_key'] not in reviews and not r['exclude_from_analysis'] and not r['transfer_group']
                  and (r['spending_treatment'] or 'normal')=='normal'
                  and merchants._json(r['classification']).get('merchant_id') is None}
        original_aliases=merchants._prepared(conn)
        applied=[];skipped=[];targets=set();created=0;added_aliases=0
        for entry in entries:
            if entry.get('action')!='apply':continue
            url=urlparse(entry.get('source_url') or '')
            if url.scheme!='https' or not url.hostname or url.username or url.password:raise ValueError('Research entries need a public HTTPS source URL.')
            name=entry['canonical_name'].strip();category=entry['category'].strip()
            if not name or not category or category=='Uncategorized':raise ValueError('Research needs a resolved name and category.')
            normalized_aliases={merchants.key(alias):alias for alias in entry['aliases'] if len(merchants.key(alias))>=3 and any(c.isalpha() for c in alias)}
            patterns=[];matched=[]
            for key,alias in normalized_aliases.items():
                possibilities=[r for r in rows if key in {merchants.key(r.get('source_merchant')),merchants.key(r.get('description'))}]
                chosen=[r for r in possibilities if r['id'] in eligible]
                if not chosen:continue
                # An exact researched label cannot steal a saved identity/rule.
                conflict=next((identity for row in possibilities if (identity:=merchants.classify(row,original_aliases)) and merchants.key(identity['merchant'])!=merchants.key(name)),None)
                if conflict:
                    skipped.append({'alias':alias,'reason':'Existing saved identity takes precedence.'});continue
                patterns.append(alias);matched.extend(chosen)
            if not patterns:continue
            seed='research-20260918:'+hashlib.sha256((name+'\0'+category).encode()).hexdigest()[:24]
            target=conn.execute('SELECT * FROM merchants WHERE seed_key=?',(seed,)).fetchone()
            if target and not target['enabled']:
                skipped.append({'name':name,'reason':'Previously disabled researched identity retained.'});continue
            if target is None:
                # Keep the original directory entry and its user settings when
                # the owner already saved this exact identity/category.
                target=next((r for r in conn.execute('SELECT * FROM merchants WHERE enabled=1 AND kind=\'merchant\'') if merchants.key(r['canonical_name'])==merchants.key(name) and r['category']==category and r['spending_treatment']=='normal'),None)
            if target is None:
                cur=conn.execute("INSERT INTO merchants(canonical_name,kind,category,spending_treatment,source,source_url,notes,seed_key,confidence) VALUES(?,'merchant',?,'normal','public_research',?,?,?,?)",(name,category,entry['source_url'],entry.get('notes','')+' Verified 2026-09-18.',seed,min(.99,float(entry.get('confidence',.95)))))
                ident=cur.lastrowid;created+=1
            else:ident=target['id']
            for alias in patterns:
                key=merchants.key(alias)
                exists=conn.execute("SELECT 1 FROM merchant_aliases WHERE merchant_id=? AND pattern=? AND match_mode='exact' AND context='spending'",(ident,alias)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO merchant_aliases(merchant_id,pattern,match_mode,context) VALUES(?,?,'exact','spending')",(ident,alias));added_aliases+=1
            ids={r['id'] for r in matched};targets.update(ids)
            applied.append({'name':name,'category':category,'source_url':entry['source_url'],'aliases':patterns,'transactions':len(ids)})
        if targets:apply_categorization(conn,transaction_ids=targets)
        if targets or repairs:recompute_all(conn,commit=False)
        after_uncat=conn.execute("SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.account_type!='investment' AND t.category='Uncategorized'").fetchone()[0]
        outcome={'uncategorized_before':before_uncat,'uncategorized_after':after_uncat,'reduction':before_uncat-after_uncat,
                 'bank_label_repairs':len(repairs),'created_names':created,'added_exact_aliases':added_aliases,
                 'targeted_transactions':len(targets),'applied':applied,'skipped':skipped,
                 'changed_candidate_ids':sorted(targets|{r['transaction_id'] for r in repairs})}
        conn.commit();return outcome
    except Exception:
        conn.rollback();raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('database');parser.add_argument('catalog');parser.add_argument('--apply',action='store_true');parser.add_argument('--repair-labels',action='store_true');parser.add_argument('--output');args=parser.parse_args()
    path=Path(args.database).resolve()
    if args.apply:conn=sqlite3.connect(path)
    else:
        source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);conn=sqlite3.connect(':memory:');source.backup(conn);source.close()
    conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
    outcome=apply_research(conn,json.loads(Path(args.catalog).read_text())['merchants'],repair_labels=args.repair_labels)
    if args.output:
        output=Path(args.output);output.write_text(json.dumps(outcome,indent=2));output.chmod(0o600)
    print(json.dumps({k:v for k,v in outcome.items() if k not in {'applied','skipped','changed_candidate_ids'}},indent=2))
    conn.close()


if __name__=='__main__':main()
