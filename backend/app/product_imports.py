"""Explicit, bounded CSV imports with preview, idempotency and source evidence."""
from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import account_settings

router = APIRouter(prefix='/api/workspace/imports', tags=['CSV imports'])
MAX_ROWS = 10000
MAX_BYTES = 512 * 1024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS csv_import_templates (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, mapping TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS csv_import_batches (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), source_name TEXT NOT NULL,
 content_sha256 TEXT NOT NULL, mapping TEXT NOT NULL, imported_count INTEGER NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS csv_import_rows (
 dedupe_key TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES csv_import_batches(id),
 transaction_id INTEGER NOT NULL REFERENCES transactions(id), row_number INTEGER NOT NULL,
 source_record TEXT NOT NULL, canonical TEXT NOT NULL);
'''


def ensure_schema(conn):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(csv_import_batches)')}
    if 'source_filename' not in columns:
        conn.execute('ALTER TABLE csv_import_batches ADD COLUMN source_filename TEXT')


class CsvMapping(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    date_column: str = Field(min_length=1, max_length=200)
    description_column: str = Field(min_length=1, max_length=200)
    amount_column: str | None = Field(default=None, min_length=1, max_length=200)
    debit_column: str | None = Field(default=None, min_length=1, max_length=200)
    credit_column: str | None = Field(default=None, min_length=1, max_length=200)
    currency_column: str | None = Field(default=None, min_length=1, max_length=200)
    id_column: str | None = Field(default=None, min_length=1, max_length=200)
    currency: str = Field(default='GBP', pattern='^[A-Z]{3}$')
    date_format: Literal['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d'] = '%Y-%m-%d'
    delimiter: Literal[',', ';', '\t'] = ','
    decimal_separator: Literal['.', ','] = '.'
    amount_multiplier: Literal[-1, 1] = 1

    @model_validator(mode='after')
    def amount_columns(self):
        if bool(self.amount_column) == bool(self.debit_column or self.credit_column):
            raise ValueError('Choose one amount column or both debit and credit columns.')
        if not self.amount_column and not (self.debit_column and self.credit_column):
            raise ValueError('Both debit and credit columns are required.')
        if self.debit_column and self.debit_column == self.credit_column:
            raise ValueError('Debit and credit must use different columns.')
        return self


class CsvPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str = Field(min_length=1, max_length=300)
    csv_text: str = Field(min_length=1, max_length=MAX_BYTES)
    mapping: CsvMapping
    source_name: str = Field(default='CSV import', min_length=1, max_length=200)
    source_filename: str | None = Field(default=None, min_length=1, max_length=200)


class CsvCommit(CsvPreview):
    preview_token: str = Field(pattern='^[a-f0-9]{64}$')
    confirm_overlaps: bool = False


class CsvTemplate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    mapping: CsvMapping


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _money(value, separator, *, blank=False):
    value = str(value or '').strip()
    if blank and not value:
        return Decimal(0)
    # Do not guess currency symbols, grouping, parentheses or sign conventions.
    pattern = r'[+-]?\d+(?:' + re.escape(separator) + r'\d{1,8})?'
    if not re.fullmatch(pattern, value):
        raise ValueError('Use an ungrouped number with the selected decimal separator.')
    try:
        result = Decimal(value.replace(separator, '.'))
    except InvalidOperation:
        raise ValueError('Invalid amount.') from None
    if not result.is_finite() or abs(result) > Decimal('1000000000000'):
        raise ValueError('Amount exceeds the supported range.')
    return result


def prepare(conn, body):
    ensure_schema(conn)
    account = account_settings.account_view(conn, body.account_id)
    if account['archived']:
        raise ValueError('Unarchive the account before importing transactions.')
    if len(body.csv_text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('CSV files must be no larger than 512 KiB. Split larger exports into separate files.')
    reader = csv.DictReader(io.StringIO(body.csv_text.lstrip('\ufeff'), newline=''), delimiter=body.mapping.delimiter, strict=True)
    try:
        columns = reader.fieldnames
    except csv.Error:
        raise ValueError('The CSV header contains malformed quoting or an oversized field.') from None
    if not columns or len(columns) > 100 or len(set(columns)) != len(columns):
        raise ValueError('The CSV needs a unique header row with at most 100 columns.')
    required = [value for key, value in body.mapping.model_dump().items() if key.endswith('_column') and value]
    if any(column not in columns for column in required):
        raise ValueError('A mapped column is missing from the CSV header.')
    mapping = body.mapping
    rows, errors, occurrences, seen_ids = [], [], Counter(), set()
    try:
        for row_number, raw in enumerate(reader, 2):
            if row_number > MAX_ROWS + 1:
                raise ValueError('Split CSV files into at most 10,000 transactions.')
            if None in raw or any(value is None for value in raw.values()):
                errors.append({'row': row_number, 'message': 'This row has a different column count from the header.'})
                continue
            if not any(value.strip() for value in raw.values()):
                continue
            try:
                day = datetime.strptime(raw[mapping.date_column].strip(), mapping.date_format).date().isoformat()
                description = raw[mapping.description_column].strip()
                if not description or len(description) > 2000:
                    raise ValueError('Description must contain between 1 and 2,000 characters.')
                currency = raw[mapping.currency_column].strip().upper() if mapping.currency_column else mapping.currency
                if currency != account['currency']:
                    raise ValueError('Transaction currency must match the account currency. Use a separate account for other currencies.')
                if account['opened_on'] and day < account['opened_on'] or account['closed_on'] and day > account['closed_on']:
                    raise ValueError('Transaction date is outside the account opening and closing dates.')
                if mapping.amount_column:
                    amount = _money(raw[mapping.amount_column], mapping.decimal_separator)
                else:
                    debit = _money(raw[mapping.debit_column], mapping.decimal_separator, blank=True)
                    credit = _money(raw[mapping.credit_column], mapping.decimal_separator, blank=True)
                    if debit < 0 or credit < 0 or debit and credit:
                        raise ValueError('Debit and credit values must be positive, with at most one populated per row.')
                    amount = credit - debit
                amount *= mapping.amount_multiplier
                canonical = {'date': day, 'description': description, 'amount': str(amount.normalize()), 'currency': currency}
                if mapping.id_column:
                    external_id = raw[mapping.id_column].strip()
                    if not external_id or len(external_id) > 500:
                        raise ValueError('Each source transaction ID must contain between 1 and 500 characters.')
                    if external_id in seen_ids:
                        raise ValueError('Duplicate source transaction ID in this CSV.')
                    seen_ids.add(external_id)
                    identity = [body.account_id, 'source', body.source_name.strip(), external_id]
                else:
                    fingerprint = _json(canonical)
                    occurrence = occurrences[fingerprint]
                    occurrences[fingerprint] += 1
                    identity = [body.account_id, 'row', canonical, occurrence]
                key = 'csv:' + _hash(_json(identity))
                existing = conn.execute('SELECT canonical FROM csv_import_rows WHERE dedupe_key=?', (key,)).fetchone()
                status, reason = 'new', None
                if existing:
                    if existing[0] != _json(canonical):
                        raise ValueError('This source ID changed after an earlier import. Original history is preserved; resolve the source revision first.')
                    status = 'duplicate'
                elif conn.execute('''SELECT 1 FROM transactions WHERE account_id=? AND txn_date=? AND ABS(amount-?)<0.00000001 LIMIT 1''',
                                  (body.account_id, day, float(amount))).fetchone():
                    status, reason = 'review', 'Existing transaction on the same date with the same amount; confirm this is an additional transaction.'
                elif conn.execute('''SELECT 1 FROM statements WHERE account_id=? AND period_start<=? AND period_end>=? LIMIT 1''',
                                  (body.account_id, day, day)).fetchone():
                    status, reason = 'review', 'Inside an imported statement period; confirm this is an additional transaction.'
                rows.append({'row': row_number, **canonical, 'amount': float(amount), 'status': status, 'reason': reason,
                             '_canonical': _json(canonical), '_key': key, '_raw': _json(raw)})
            except (ValueError, OverflowError) as error:
                errors.append({'row': row_number, 'message': str(error) if isinstance(error, ValueError) else 'Invalid date or amount.'})
    except csv.Error:
        raise ValueError('The CSV contains malformed quoting or an oversized field.') from None
    if not rows and not errors:
        errors.append({'row': 1, 'message': 'No transactions were found in this CSV.'})
    counts = Counter(row['status'] for row in rows)
    # Token includes the previewed disposition, so a changed ledger requires a
    # new preview. It is an intent/freshness digest, never an auth credential.
    token = _hash(_json({'account': account, 'mapping': mapping.model_dump(), 'source_name': body.source_name,
                         'content': _hash(body.csv_text), 'rows': rows, 'errors': errors}))
    return {'preview_token': token, 'columns': columns, 'rows': rows, 'row_count': len(rows),
            'new_count': counts['new'], 'duplicate_count': counts['duplicate'], 'review_count': counts['review'],
            'errors': errors[:100], 'error_count': len(errors), 'can_import': not errors}


def public_preview(result):
    def public_row(row):
        return {key: value for key, value in row.items() if not key.startswith('_')}
    # The ordinary sample stays bounded, but every overlap must be inspectable
    # before the user confirms the complete file. MAX_ROWS bounds this evidence.
    return {**result, 'rows': [public_row(row) for row in result['rows'][:200]],
            'review_rows': [public_row(row) for row in result['rows'] if row['status'] == 'review'],
            'preview_limit': 200}


def commit_import(conn, body):
    from .categorize import auto_category, normalize_merchant, apply_categorization
    from .analytics import recompute_all
    preview = prepare(conn, body)
    if body.preview_token != preview['preview_token']:
        raise ValueError('The CSV or account changed since the preview. Preview it again before importing.')
    if not preview['can_import']:
        raise ValueError('Fix every CSV validation error before importing.')
    if preview['review_count'] and not body.confirm_overlaps:
        raise ValueError('Confirm the possible overlaps after reviewing them before importing.')
    rows = [row for row in preview['rows'] if row['status'] != 'duplicate']
    if not rows:
        return {'imported_count': 0, 'duplicate_count': preview['duplicate_count'], 'batch_id': None}
    batch_id = uuid.uuid4().hex
    conn.execute('INSERT INTO csv_import_batches(id,account_id,source_name,content_sha256,mapping,imported_count,source_filename) VALUES(?,?,?,?,?,?,?)',
                 (batch_id, body.account_id, body.source_name, _hash(body.csv_text), _json(body.mapping.model_dump()), len(rows), body.source_filename))
    ids = []
    currency_column = 'currency' in {r[1] for r in conn.execute('PRAGMA table_info(transactions)')}
    for row in rows:
        if row['currency'] != 'GBP' and not currency_column:
            raise ValueError('Upgrade the database before importing other transaction currencies.')
        merchant = normalize_merchant(row['description']) or row['description']
        meta = _json({'source': 'csv', 'source_name': body.source_name, 'batch_id': batch_id,
                      'source_filename': body.source_filename, 'currency': row['currency'], 'source_row': row['row'], 'amount_multiplier': body.mapping.amount_multiplier})
        cursor = conn.execute('''INSERT INTO transactions(account_id,txn_date,description,merchant,source_merchant,amount,type_code,category,raw,meta,dedupe_key)
            VALUES(?,?,?,?,?,?,'csv_import',?,?,?,?)''',
            (body.account_id, row['date'], row['description'], merchant, merchant, row['amount'],
             auto_category(row['description'], merchant, row['amount']), row['_raw'], meta, row['_key']))
        ids.append(cursor.lastrowid)
        if currency_column:
            conn.execute('UPDATE transactions SET currency=? WHERE id=?', (row['currency'], cursor.lastrowid))
        conn.execute('INSERT INTO csv_import_rows VALUES(?,?,?,?,?,?)',
                     (row['_key'], batch_id, cursor.lastrowid, row['row'], row['_raw'], row['_canonical']))
    apply_categorization(conn, transaction_ids=ids)
    recompute_all(conn, commit=False)
    return {'imported_count': len(ids), 'duplicate_count': preview['duplicate_count'], 'batch_id': batch_id}


@router.post('/preview')
def preview_csv(body: CsvPreview):
    with account_settings.database() as conn:
        try:
            return JSONResponse(public_preview(prepare(conn, body)), headers=account_settings.HEADERS)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None


@router.post('/commit')
def import_csv(body: CsvCommit):
    with account_settings.database() as conn:
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                result = commit_import(conn, body)
            return JSONResponse(result, headers=account_settings.HEADERS)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None


@router.get('/templates')
def templates():
    with account_settings.database() as conn:
        ensure_schema(conn)
        return {'templates': [{'id': row['id'], 'name': row['name'], 'mapping': json.loads(row['mapping'])}
                              for row in conn.execute('SELECT * FROM csv_import_templates ORDER BY name,id')]}


@router.post('/templates', status_code=201)
def save_template(body: CsvTemplate):
    with account_settings.database() as conn:
        ensure_schema(conn)
        key = uuid.uuid4().hex
        with conn:
            conn.execute('INSERT INTO csv_import_templates(id,name,mapping) VALUES(?,?,?)', (key, body.name, _json(body.mapping.model_dump())))
        return {'id': key, **body.model_dump()}
