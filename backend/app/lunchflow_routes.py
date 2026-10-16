"""Private, same-origin bank routes protected by Am I Broke? authentication."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import lunchflow_client as client, lunchflow_store as store

router = APIRouter(prefix='/api/banking',tags=['Connected banks'])
HEADERS = {'Cache-Control':'no-store','Pragma':'no-cache'}


def _open():
    try:
        return store.database()
    except ValueError as error:
        raise HTTPException(503,str(error)) from None


@router.get('')
def banking():
    conn = _open()
    try:
        return JSONResponse(store.summary(conn),headers=HEADERS)
    finally:
        conn.close()


@router.get('/health')
def banking_health():
    from .banking_health import snapshot
    conn = _open()
    try:
        return JSONResponse(snapshot(conn), headers=HEADERS)
    finally:
        conn.close()


def run_sync(reader=None, db_path=None, provider_connection_id='environment:lunchflow'):
    """Persist independent successes; never clear cache after a failed read."""
    if not client.configured() and reader is None:
        raise ValueError('Lunch Flow is not configured on the server.')
    reader = reader or client.read
    conn = store.database(db_path)
    owner = None
    try:
        owner,run_id = store.acquire_sync(conn,provider_connection_id=provider_connection_id)
        first_new_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM transactions').fetchone()[0]
        # Account commits intentionally survive later failures. Persist the
        # earliest unfinished analytics boundary before any account is saved,
        # so an idempotent retry also repairs previously imported rows.
        with conn:
            conn.execute('INSERT INTO bank_derived_refresh_work(run_id,after_id) VALUES(?,?)',
                         (run_id, first_new_id))
        errors, stats = [], {'accounts_read':0,'accounts_failed':0}
        started = store.now()
        deadline = time.monotonic() + client.REQUEST_BUDGET_SECONDS
        try:
            accounts = client.list_accounts(reader('accounts'))
            if provider_connection_id != 'environment:lunchflow':
                accounts = [{**account, 'external_id': account['id'],
                             'id': provider_connection_id + ':' + account['id']} for account in accounts]
            with conn:
                store.discover_accounts(conn,accounts,started,provider_connection_id)
            excluded = {row[0] for row in conn.execute('SELECT id FROM bank_connections WHERE excluded=1')}
            eligible = [account for account in accounts if account['id'] not in excluded]
            with ThreadPoolExecutor(max_workers=4,thread_name_prefix='bank-read') as pool:
                futures = {pool.submit(client.fetch_account,a.get('external_id',a['id']),reader,deadline):a for a in eligible}
                for future in as_completed(futures):
                    account = futures[future]
                    try:
                        result = future.result()
                        with conn:
                            # Reestablish ownership in the same transaction as
                            # each saved account, including an expired lease's
                            # late result after another run repaired old work.
                            conn.execute('INSERT OR IGNORE INTO bank_derived_refresh_work(run_id,after_id) VALUES(?,?)',
                                         (run_id, first_new_id))
                            store.record_account_result(conn,account['id'],result,store.now())
                        stats['accounts_read'] += 1
                        if result['errors']:
                            stats['accounts_failed'] += 1
                            errors.extend(result['errors'])
                    except Exception:
                        error = 'This bank account could not be saved. Previous records are preserved.'
                        with conn:
                            conn.execute('UPDATE bank_connections SET last_error=? WHERE id=?',(error,account['id']))
                        errors.append(error)
                        stats['accounts_failed'] += 1
        except Exception as error:
            errors.append(client.safe_error(error))
        try:
            with conn:
                store._admit_writer(conn)
                boundaries = [first_new_id]
                boundaries.extend(row[0] for row in conn.execute('SELECT after_id FROM bank_derived_refresh_work'))
                boundaries.extend(row[0] for row in conn.execute('SELECT after_id FROM bank_derived_refresh'))
                refresh_derived(conn,min(boundaries))
                # Another running connection can still save more accounts.
                # Only this run and completed failed runs are satisfied here.
                conn.execute('''DELETE FROM bank_derived_refresh_work WHERE run_id=? OR run_id IN
                    (SELECT id FROM bank_sync_runs WHERE status<>'running')''', (run_id,))
                conn.execute('DELETE FROM bank_derived_refresh WHERE singleton=1')
        except Exception:
            errors.append('Bank records were saved, but derived spending insights need a refresh.')
        error = ' '.join(dict.fromkeys(errors)) or None
        store.finish_sync(conn,owner,run_id,error,stats)
        owner = None
        result = store.summary(conn)
        result['result'] = stats | {'error':error}
        return result
    finally:
        if owner:
            # Even unexpected local failures release the durable lease; never
            # emit exception text that could include provider payloads.
            try:
                conn.rollback()
                store.finish_sync(conn,owner,run_id,'Bank sync could not complete. Previously saved records are preserved.')
            except Exception:
                pass
        conn.close()


@router.post('/sync')
def sync_banking():
    try:
        from .product_connections import has_saved_connections, refresh_saved_connections
        if has_saved_connections('lunchflow'):
            refresh_saved_connections('lunchflow')
            if client.configured():
                run_sync()
            conn = _open()
            try:
                return JSONResponse(store.summary(conn), headers=HEADERS)
            finally:
                conn.close()
        return JSONResponse(run_sync(),headers=HEADERS)
    except ValueError as error:
        status = 429 if ('minute' in str(error) or 'running' in str(error)) else 503
        return JSONResponse({'detail':str(error)},status,headers={**HEADERS,**({'Retry-After':'60'} if status==429 else {})})
    except Exception:
        return JSONResponse({'detail':'The bank sync could not complete. Your saved data is preserved.'},503,headers=HEADERS)


def refresh_derived(conn, after_id):
    ids = [row[0] for row in conn.execute("SELECT id FROM transactions WHERE id>? AND type_code='bank_feed'", (after_id,))]
    if ids:
        from .categorize import apply_categorization
        from .analytics import recompute_all
        apply_categorization(conn,transaction_ids=ids)
        recompute_all(conn,commit=False)


class Mapping(BaseModel):
    account_id: str | None = Field(default=None,max_length=300)
    create_new: bool = False
    amount_multiplier: Literal[-1,1] | None = None
    balance_multiplier: Literal[-1,1] | None = None
    account_type: Literal['current','savings','credit','cash'] | None = None
    replace_connection_id: str | None = Field(default=None, max_length=300)
    confirm_reconnection: bool = False
    confirm_distinct_account: bool = False


@router.post('/accounts/{provider_id}/mapping')
def mapping(provider_id: str, body: Mapping):
    conn = _open()
    try:
        with conn:
            first_new_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM transactions').fetchone()[0]
            store.map_account(conn,provider_id,**body.model_dump())
            refresh_derived(conn,first_new_id)
        return JSONResponse(store.summary(conn),headers=HEADERS)
    except LookupError as error:
        raise HTTPException(404,str(error)) from None
    except ValueError as error:
        raise HTTPException(409,str(error)) from None
    finally:
        conn.close()


@router.get('/accounts/{provider_id}')
def account_detail(provider_id: str, limit: int=Query(100,ge=1,le=500), offset: int=Query(0,ge=0),
                   status: Literal['all','booked','pending','review']='all',q: str | None=Query(None,max_length=200),
                   date_from: date | None=None,date_to: date | None=None):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422,'The start date must be on or before the end date.')
    conn = _open()
    try:
        return JSONResponse(store.detail(conn,provider_id,limit,offset,status,q,date_from,date_to),headers=HEADERS)
    except LookupError as error:
        raise HTTPException(404,str(error)) from None
    finally:
        conn.close()


class BalanceRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    balance_type: Literal['available_credit', 'reported']
    credit_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    currency: str | None = Field(default=None, pattern='^[A-Z]{3}$')


@router.post('/accounts/{provider_id}/balance-rule')
def balance_rule(provider_id: str, body: BalanceRule):
    from .bank_balance_rules import set_rule
    conn = _open()
    try:
        with conn:
            set_rule(conn, provider_id, **body.model_dump())
        return JSONResponse(store.summary(conn), headers=HEADERS)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    finally:
        conn.close()


class Review(BaseModel):
    action: Literal['match','import','ignore']
    transaction_id: int | None = Field(default=None,ge=1)


@router.post('/reviews/{cache_id}')
def review(cache_id: int, body: Review):
    conn = _open()
    try:
        with conn:
            first_new_id = conn.execute('SELECT COALESCE(MAX(id),0) FROM transactions').fetchone()[0]
            store.resolve_review(conn,cache_id,body.action,body.transaction_id)
            refresh_derived(conn,first_new_id)
        return JSONResponse({'ok':True},headers=HEADERS)
    except LookupError as error:
        raise HTTPException(404,str(error)) from None
    except ValueError as error:
        raise HTTPException(409,str(error)) from None
    finally:
        conn.close()
