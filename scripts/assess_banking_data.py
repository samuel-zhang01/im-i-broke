"""Offline, aggregate-only evidence and a small merchant-alias retrieval benchmark.

No network, training service, or database writes. Run with a read-only backup.
The holdout removes the exact alias from training; singleton identities test
whether a matcher incorrectly assigns an unseen business to a known business.
This is a retrieval experiment, not a validated category or identity model.
"""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata


def normalized(text):
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def grams(text):
    text = ' ' + text + ' '
    return Counter(text[i:i+n] for n in (3, 4, 5) for i in range(max(0, len(text)-n+1)))


def benchmark(samples):
    # Exact context variants of the same text are one sample. Conflicts cannot
    # supply a reliable ground-truth label, so exclude them before evaluation.
    targets = defaultdict(set)
    for label, ident in samples: targets[normalized(label)].add(ident)
    data = [(label, next(iter(ids))) for label, ids in sorted(targets.items()) if label and len(ids) == 1]
    support = Counter(ident for _, ident in data)
    result = {name: Counter() for name in ('exact_lookup', 'sequence_ratio', 'character_tfidf')}
    for label, target in data:
        train = [(text, ident) for text, ident in data if text != label]
        known = support[target] > 1
        cohort = 'known' if known else 'unseen'
        predictions = {'exact_lookup': None}
        scores = defaultdict(float)
        for text, ident in train: scores[ident] = max(scores[ident], SequenceMatcher(None, label, text).ratio())
        ranked = sorted(scores.items(), key=lambda p: (-p[1], p[0]))
        predictions['sequence_ratio'] = ranked[0][0] if ranked and .78 <= ranked[0][1] < 1 else None
        documents = [grams(text) for text, _ in train]
        df = Counter(gram for doc in documents for gram in doc)
        idf = {gram: math.log((1+len(documents))/(1+count))+1 for gram,count in df.items()}
        def vector(counts):
            weighted = {gram: (1+math.log(count))*idf[gram] for gram,count in counts.items() if gram in idf}
            length = math.sqrt(sum(value*value for value in weighted.values())) or 1
            return {gram: value/length for gram,value in weighted.items()}
        query = vector(grams(label)); scores = defaultdict(float)
        for (_, ident), doc in zip(train, documents):
            value = sum(weight*query.get(gram,0) for gram,weight in vector(doc).items())
            scores[ident] = max(scores[ident], value)
        ranked = sorted(scores.items(), key=lambda p: (-p[1], p[0]))
        # Fixed in advance; no tuning on the reported holdout.
        predictions['character_tfidf'] = ranked[0][0] if ranked and ranked[0][1] >= .8 and (len(ranked)<2 or ranked[0][1]-ranked[1][1]>=.15) else None
        for name,prediction in predictions.items():
            counter=result[name];counter[cohort+'_queries']+=1
            if prediction is not None:
                counter[cohort+'_suggested']+=1
                counter[cohort+'_correct']+=int(prediction==target)
    output={}
    for name,counter in result.items():
        c=dict(counter);n=c.get('known_suggested',0);correct=c.get('known_correct',0)
        c['known_precision']=round(correct/n,4) if n else None
        c['known_coverage']=round(n/c.get('known_queries',1),4)
        c['unseen_false_accepts']=c.get('unseen_suggested',0)
        if n:
            p=correct/n;z=1.96
            c['precision_wilson_lower_95']=round((p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n),4)
        output[name]=c
    return {'labels':len(data),'identities':len(support),'conflicting_labels_excluded':sum(len(v)>1 for v in targets.values()),
            'method':'Leave one normalized exact alias out. All train-only IDF weights are refitted. Merchant identities with one alias form the unseen-business abstention test. Personal/financial names, prefix rules and machine-produced categories are excluded.',
            'limitations':'Small, non-independent aliases from existing user rules; not independent blinded identity labels, nor a temporal deployment trial. No category model is evaluated. Similarity scores are not probabilities.',
            'models':output}


def assess(path):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    counts=Counter();nonempty=Counter();merchant_quality=defaultdict(Counter)
    for row in conn.execute('SELECT b.raw,c.institution_name FROM bank_transactions b JOIN bank_connections c ON c.id=b.connection_id'):
        payload=json.loads(row['raw']);counts.update(payload.keys());nonempty.update(k for k,v in payload.items() if v is not None and v!='')
        q=merchant_quality[row['institution_name']];q['rows']+=1
        value=str(payload.get('merchant') or '')
        q['merchant_has_letters']+=int(any(c.isalpha() for c in value))
        q['merchant_numeric_only']+=int(bool(re.fullmatch(r'[\d\s-]+',value)))
        q['description_nonempty']+=int(bool(payload.get('description')))
    linked=dict(conn.execute("""SELECT COUNT(*) linked,
        SUM(lower(COALESCE(b.merchant,''))!=lower(COALESCE(t.source_merchant,''))) different_merchant_text,
        SUM(length(COALESCE(b.description,''))>length(COALESCE(t.description,''))) longer_bank_description
        FROM bank_transactions b JOIN transactions t ON t.id=b.transaction_id WHERE b.status='matched'""").fetchone())
    labels=[tuple(row) for row in conn.execute("""SELECT a.pattern,m.id FROM merchant_aliases a JOIN merchants m ON m.id=a.merchant_id
        WHERE m.enabled=1 AND m.source='user' AND m.kind='merchant' AND a.match_mode='exact'""")]
    result={'bank_records':sum(x['rows'] for x in merchant_quality.values()),'raw_fields':dict(counts),'nonempty_fields':dict(nonempty),
            'merchant_quality_by_institution':{k:dict(v) for k,v in merchant_quality.items()},'linked_comparison':linked,
            'explicit_category_corrections':conn.execute('SELECT COUNT(*) FROM overrides WHERE category IS NOT NULL').fetchone()[0],
            'alias_experiment':benchmark(labels)}
    conn.close();return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('database');parser.add_argument('--output');args=parser.parse_args()
    result=assess(args.database);text=json.dumps(result,indent=2)
    if args.output:
        path=Path(args.output);path.write_text(text);path.chmod(0o600)
    print(text)
