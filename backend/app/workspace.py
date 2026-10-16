"""Instance-owned preferences and stable category identities.

One database is one household. A request can never select a different workspace
or database path. Missing settings retain the original installation's defaults.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from contextlib import closing
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from typing import Literal

from . import db

router = APIRouter(prefix='/api/workspace', tags=['Workspace'])
DEFAULTS = {'reporting_currency': 'GBP', 'locale': 'en-GB', 'timezone': 'Europe/London'}
BUILTINS = ['Income', 'Housing', 'Friends & shared costs', 'Groceries', 'Eating out',
            'Transport', 'Shopping', 'Entertainment', 'Travel', 'Home & personal care',
            'Bills & utilities', 'Phone & internet', 'Subscriptions', 'Health & education',
            'Transfers & payments', 'Cash', 'Fees & interest', 'Investments', 'Uncategorized',
            'Investment income', 'Software & online services']
TREATMENTS = {'normal', 'income', 'transfer', 'investment', 'refund'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS workspace (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT UNIQUE NOT NULL,
    preferences TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS category_catalog (
    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    semantic_name TEXT NOT NULL, treatment TEXT NOT NULL DEFAULT 'normal',
    archived INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 100,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS category_names (
    name TEXT PRIMARY KEY COLLATE NOCASE, category_id TEXT NOT NULL REFERENCES category_catalog(id)
);
CREATE TABLE IF NOT EXISTS workspace_changes (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id TEXT,
    before_json TEXT, after_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''


def category_id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'finance:category:' + name.casefold()))


def default_treatment(name):
    return {'Income': 'income', 'Investment income': 'income',
            'Transfers & payments': 'transfer', 'Investments': 'investment'}.get(name, 'normal')


def ensure_schema(conn):
    # Do not use executescript: callers may already own a migration transaction.
    for sql in SCHEMA.split(';'):
        if sql.strip():
            conn.execute(sql)
    conn.execute('INSERT OR IGNORE INTO workspace(singleton,id) VALUES(1,?)', (str(uuid.uuid4()),))
    names = list(BUILTINS)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('transactions', 'merchants', 'rules', 'overrides'):
        if table in tables:
            active = ' AND enabled=1' if table == 'merchants' else ''
            names.extend(r[0] for r in conn.execute(f"SELECT DISTINCT category FROM {table} WHERE TRIM(COALESCE(category,''))<>''" + active))
    # Preserve the existing alphabetical picker order on the first migration;
    # later explicit sort choices remain untouched.
    for index, name in enumerate(sorted(dict.fromkeys(names), key=str.casefold)):
        known = conn.execute('SELECT category_id FROM category_names WHERE name=?', (name,)).fetchone()
        if known:
            continue
        ident = category_id(name)
        conn.execute('INSERT OR IGNORE INTO category_catalog(id,name,semantic_name,treatment,sort_order) VALUES(?,?,?,?,?)',
                     (ident, name, name, default_treatment(name), index))
        conn.execute('INSERT OR IGNORE INTO category_names(name,category_id) VALUES(?,?)', (name, ident))


def exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def preferences(conn):
    if not exists(conn, 'workspace'):
        return dict(DEFAULTS)
    row = conn.execute('SELECT preferences FROM workspace WHERE singleton=1').fetchone()
    return {**DEFAULTS, **json.loads(row[0])} if row else dict(DEFAULTS)


def reporting_today(conn):
    return datetime.now(ZoneInfo(preferences(conn)['timezone'])).date()


def category_maps(conn):
    if not exists(conn, 'category_catalog'):
        return {}, {}
    categories = {r['id']: dict(r) for r in conn.execute('SELECT * FROM category_catalog')}
    by_name = {r['name'].casefold(): categories[r['category_id']] for r in conn.execute('SELECT * FROM category_names')}
    return categories, by_name


def annotate_category(row, by_name):
    category = by_name.get((row.get('category') or '').casefold())
    if category:
        row.update(category_id=category['id'], category_semantic=category['semantic_name'],
                   category_treatment=category['treatment'])
    return row


def semantic(row):
    return row.get('category_semantic', row.get('category'))


def category_name(conn, name):
    if not name:
        return name
    _, mapping = category_maps(conn)
    return mapping.get(name.casefold(), {}).get('name', name)


def category_options(conn):
    # Older databases used directly in tests/recovery retain their old choices.
    if not exists(conn, 'category_catalog'):
        return None
    catalog, mapping = category_maps(conn)
    result = [r['name'] for r in sorted(catalog.values(), key=lambda c: (c['sort_order'], c['name'].casefold())) if not r['archived']]
    for row in conn.execute("SELECT category FROM transactions UNION SELECT category FROM merchants WHERE enabled=1"):
        name = row[0]
        if name and name.strip() and name.casefold() not in mapping and name not in result:
            result.append(name)
    return result


def _open():
    if not db.DB_PATH.is_file():
        raise HTTPException(503, 'The workspace database is unavailable. Initialize a new installation or restore its backup.')
    conn = db.connect(db.DB_PATH)
    try:
        ensure_schema(conn)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


class PreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reporting_currency: str | None = Field(default=None, min_length=3, max_length=3)
    locale: str | None = Field(default=None, min_length=2, max_length=35)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)


@router.get('/preferences')
def read_preferences():
    with closing(_open()) as conn:
        return preferences(conn)


@router.get('/calendar')
def read_calendar():
    """Authoritative date for user-entered observations and history coverage."""
    with closing(_open()) as conn:
        conn.execute('BEGIN')
        return {'today': reporting_today(conn).isoformat(),
                'timezone': preferences(conn)['timezone']}


@router.patch('/preferences')
def update_preferences(body: PreferencesInput):
    values = body.model_dump(exclude_unset=True)
    if any(v is None for v in values.values()):
        raise HTTPException(422, 'Preferences cannot be null.')
    if 'reporting_currency' in values:
        from .currency_reporting import currency_code
        try:
            values['reporting_currency'] = currency_code(values['reporting_currency'])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    if 'locale' in values and not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', values['locale']):
        raise HTTPException(422, 'Use a language tag such as en-GB or fr-FR.')
    if 'timezone' in values:
        try:
            ZoneInfo(values['timezone'])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, 'Choose a valid IANA timezone, such as Europe/London.') from None
    with closing(_open()) as conn, conn:
        # Own the writer slot before reading the JSON replacement. Independent
        # PATCHes must merge with the most recently committed preferences.
        conn.execute('BEGIN IMMEDIATE')
        before = preferences(conn)
        after = {**before, **values}
        conn.execute('UPDATE workspace SET preferences=? WHERE singleton=1', (json.dumps(after),))
        conn.execute('INSERT INTO workspace_changes(kind,before_json,after_json) VALUES(?,?,?)',
                     ('preferences', json.dumps(before), json.dumps(after)))
        return after


class CategoryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    treatment: Literal['normal', 'income', 'transfer', 'investment', 'refund'] = 'normal'
    archived: StrictBool = False
    sort_order: int = Field(default=100, ge=0, le=100000)


class CategoryPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=120)
    treatment: Literal['normal', 'income', 'transfer', 'investment', 'refund'] | None = None
    archived: StrictBool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100000)


def public_category(row):
    item = dict(row)
    item['archived'] = bool(item['archived'])
    return item


@router.get('/categories')
def list_categories():
    with closing(_open()) as conn:
        return {'categories': [public_category(r) for r in conn.execute('SELECT * FROM category_catalog ORDER BY sort_order,name')]}


@router.get('/category-options')
def ordered_category_options():
    with closing(_open()) as conn:
        return category_options(conn)


@router.post('/categories', status_code=201)
def create_category(body: CategoryInput):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, 'Enter a category name.')
    with closing(_open()) as conn, conn:
        if name.casefold() in category_maps(conn)[1]:
            raise HTTPException(409, 'That category name is already in use or belongs to a renamed category.')
        ident = str(uuid.uuid4())
        conn.execute('INSERT INTO category_catalog(id,name,semantic_name,treatment,archived,sort_order) VALUES(?,?,?,?,?,?)',
                     (ident, name, name, body.treatment, int(body.archived), body.sort_order))
        conn.execute('INSERT INTO category_names(name,category_id) VALUES(?,?)', (name, ident))
        return public_category(conn.execute('SELECT * FROM category_catalog WHERE id=?', (ident,)).fetchone())


@router.patch('/categories/{ident}')
def update_category(ident: str, body: CategoryPatch):
    values = body.model_dump(exclude_unset=True)
    if any(v is None for v in values.values()):
        raise HTTPException(422, 'Category fields cannot be null.')
    with closing(_open()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM category_catalog WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Category not found.')
            before = dict(row)
            after = {**before, **values}
            after['name'] = after['name'].strip()
            if not after['name']:
                raise HTTPException(422, 'Enter a category name.')
            taken = category_maps(conn)[1].get(after['name'].casefold())
            if taken and taken['id'] != ident:
                raise HTTPException(409, 'That category name already belongs to another category.')
            conn.execute('UPDATE category_catalog SET name=?,treatment=?,archived=?,sort_order=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                         (after['name'], after['treatment'], int(after['archived']), after['sort_order'], ident))
            conn.execute('INSERT OR IGNORE INTO category_names(name,category_id) VALUES(?,?)', (after['name'], ident))
            if before['name'] != after['name']:
                # Rename saved decisions and derived labels atomically. Raw source
                # records and immutable category semantics remain untouched.
                for table in ('transactions', 'merchants', 'rules', 'overrides', 'recurring'):
                    conn.execute(f'UPDATE {table} SET category=? WHERE category=?', (after['name'], before['name']))
            conn.execute('INSERT INTO workspace_changes(kind,item_id,before_json,after_json) VALUES(?,?,?,?)',
                         ('category', ident, json.dumps(before), json.dumps(after)))
            from .categorize import apply_categorization
            from .analytics import recompute_all
            apply_categorization(conn)
            recompute_all(conn, commit=False)
            conn.commit()
            return public_category(conn.execute('SELECT * FROM category_catalog WHERE id=?', (ident,)).fetchone())
        except Exception:
            conn.rollback()
            raise
