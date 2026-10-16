"""Minimal normalized models for SnapTrade SDK 13.0.23; omit raw payloads."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re

RETURN_TIMEFRAMES = ('1D', '1W', '1M', 'YTD', '1Y', 'ALL')


def obj(value):
    return value if isinstance(value, Mapping) else {}


def text(value, fallback=None, limit=240):
    return value[:limit] if isinstance(value, str) and value else fallback


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(Decimal(str(value)))
        return result if math.isfinite(result) else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def currency(value):
    code = obj(value).get('code') if isinstance(value, Mapping) else value
    # Do not turn GBp (pence) into GBP by uppercasing an unfamiliar unit.
    return code if isinstance(code, str) and re.fullmatch(r'[A-Z]{3}', code) else None


def timestamp(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
            return value[:40]
        except ValueError:
            pass
    return None


def boolean(value):
    return value if isinstance(value, bool) else None


def account_name(value):
    """Account labels can embed complete account numbers, including old caches."""
    return re.sub(r'\d{5,}', lambda match: '…' + match.group()[-4:],
                  text(value, 'Brokerage account'))


def rows(value, key=None):
    if key is not None:
        value = obj(value).get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError('Unexpected provider response shape')
    if any(not isinstance(row, Mapping) for row in value):
        raise ValueError('Unexpected provider row shape')
    return value


def section(data=None, *, error=None, unsupported=False, truncated=False):
    data = [] if data is None else data
    return {'status': ('unsupported' if unsupported else 'error') if error else ('ok' if data else 'empty'),
            'data': data, 'error': error, 'truncated': truncated}


def account(raw):
    total = obj(obj(raw.get('balance')).get('total'))
    sync = obj(raw.get('sync_status'))
    holdings = obj(sync.get('holdings'))
    transactions = obj(sync.get('transactions'))
    suffix = re.sub(r'[^a-zA-Z0-9]', '', text(raw.get('number'), ''))[-4:] or None
    name = account_name(raw.get('name'))
    return {
        'id': text(raw.get('id')), 'name': name,
        'institution': text(raw.get('institution_name'), 'Brokerage'),
        'number_suffix': suffix, 'type': text(raw.get('raw_type')),
        'currency': currency(total.get('currency')), 'total_value': number(total.get('amount')),
        # The account-list total and the independently fetched positions can
        # have different source times. A positions read must not redate NAV.
        'total_value_as_of': timestamp(holdings.get('last_successful_sync')),
        'connection_id': text(raw.get('brokerage_authorization')),
        'holdings_synced_at': timestamp(holdings.get('last_successful_sync')),
        'activities_synced_at': timestamp(transactions.get('last_successful_sync')),
        # Some brokers expose balances/holdings but no transaction history.
        # Transaction readiness must not invalidate a complete current portfolio.
        'initial_sync_complete': boolean(holdings.get('initial_sync_completed')),
        'holdings_unavailable': holdings.get('holdings_unavailable') is True,
        'is_paper': raw.get('is_paper') is True,
        'balances': section(), 'positions': section(), 'orders': section(), 'activities': section(),
        'history': section(), 'return_rates': section(),
    }


def balance(raw):
    return {'currency': currency(raw.get('currency')), 'cash': number(raw.get('cash')),
            'buying_power': number(raw.get('buying_power'))}


def product(*values):
    if any(value is None for value in values):
        return None
    result = math.prod(values)
    return result if math.isfinite(result) else None


def position(raw, index=0):
    instrument = obj(raw.get('instrument'))
    kind = text(instrument.get('kind'))
    units, price, cost = (number(raw.get(key)) for key in ('units', 'price', 'cost_basis'))
    position_unit = obj(raw.get('currency')).get('code') if isinstance(raw.get('currency'), Mapping) else raw.get('currency')
    listing_unit = instrument.get('currency')
    position_currency = currency(position_unit)
    if position_unit in ('GBX', 'GBp'):
        # GBX/GBp are sterling pence. Convert monetary inputs before valuation;
        # quantity and multiplier are unchanged. Listing currency alone must
        # never rescale an already-GBP (or another currency) broker valuation.
        position_currency = 'GBP'
        price = price / 100 if price is not None else None
        cost = cost / 100 if cost is not None else None
    # Bond prices, CFD notionals and futures exposures cannot safely be treated
    # as cash portfolio values. Options require the actual contract multiplier.
    multiplier = 1 if kind in {'stock', 'etf', 'mutualfund', 'mutual_fund', 'cef', 'adr', 'crypto'} else None
    if kind == 'option':
        multiplier = number(instrument.get('multiplier'))
        if multiplier is not None and multiplier <= 0:
            multiplier = None
    value = product(units, price, multiplier)
    pnl = product(units, (price - cost) if price is not None and cost is not None else None, multiplier)
    return {
        'id': f'{text(instrument.get("id"), "position")}:{index}',
        'instrument_id': text(instrument.get('id')),
        'raw_symbol': text(instrument.get('raw_symbol')),
        'listing_currency': 'GBP' if listing_unit in ('GBX', 'GBp') else currency(listing_unit),
        'exchange': text(instrument.get('exchange')),
        'figi_code': text(obj(instrument.get('figi_instrument')).get('figi_code')),
        'figi_share_class': text(obj(instrument.get('figi_instrument')).get('figi_share_class')),
        'multiplier': multiplier,
        'symbol': text(instrument.get('symbol'), 'Unknown'),
        'name': text(instrument.get('description'), text(instrument.get('symbol'), 'Unknown security')),
        'asset_type': kind, 'quantity': units, 'price': price, 'value': value,
        # Price/cost use position currency, which can differ from listing currency.
        'currency': position_currency, 'average_cost': cost,
        'unrealized_pnl': pnl, 'is_cash': raw.get('cash_equivalent') is True,
    }


def order(raw, index=0):
    symbol = obj(raw.get('option_symbol')) or obj(raw.get('universal_symbol'))
    return {
        'id': text(raw.get('brokerage_order_id'), f'order:{index}'),
        'symbol': text(symbol.get('ticker'), text(symbol.get('symbol'), 'Unknown')),
        'action': text(raw.get('action')), 'status': text(raw.get('status')),
        'quantity': number(raw.get('total_quantity')), 'filled_quantity': number(raw.get('filled_quantity')),
        'limit_price': number(raw.get('limit_price')), 'execution_price': number(raw.get('execution_price')),
        'currency': currency(raw.get('quote_currency')) or currency(obj(raw.get('universal_symbol')).get('currency')),
        'placed_at': timestamp(raw.get('time_placed')), 'updated_at': timestamp(raw.get('time_updated')),
        'type': text(raw.get('order_type')),
    }


def activity(raw, index=0):
    symbol = obj(raw.get('option_symbol')) or obj(raw.get('symbol'))
    provider_id = text(raw.get('id'))
    return {
        'id': provider_id or f'activity:{index}', 'id_source': 'provider' if provider_id else 'generated',
        'type': text(raw.get('type'), 'OTHER'),
        'description': text(raw.get('description'), '', 500),
        'symbol': text(symbol.get('ticker'), text(symbol.get('symbol'))),
        'amount': number(raw.get('amount')), 'currency': currency(raw.get('currency')),
        'quantity': number(raw.get('units')),
        'date': timestamp(raw.get('trade_date')) or timestamp(raw.get('settlement_date')),
    }


def history(raw, *, today=None, limit=400):
    """Normalize provider-estimated account totals without inventing daily data.

    Currency belongs to the entire response, not its rows or the account's
    present-day summary. Conflicting observations for one date are withheld.
    Valid observations survive malformed rows with a visible partial error.
    """
    items = rows(raw, 'history')
    code = currency(obj(raw).get('currency'))
    if not code:
        raise ValueError('Historical currency is missing or invalid')
    today = today or datetime.now(timezone.utc).date()
    points, conflicts = {}, set()
    invalid = False
    for item in items:
        day = timestamp(item.get('date'))
        try:
            parsed = date.fromisoformat(day[:10]) if day else None
        except ValueError:
            parsed = None
        value = number(item.get('total_value'))
        if parsed is None or parsed > today or value is None:
            invalid = True
            continue
        day = parsed.isoformat()
        if day in points and points[day]['total_value'] != value:
            conflicts.add(day)
        points[day] = {'date': day, 'total_value': value, 'currency': code,
                       'source': 'snaptrade_estimate'}
    data = [points[day] for day in sorted(points) if day not in conflicts]
    truncated = len(data) > limit
    data = data[-limit:]
    error = ('Some historical values were omitted because their dates or amounts were invalid or conflicting.'
             if invalid or conflicts else None)
    return section(data, error=error, truncated=truncated)


def return_rates(raw):
    """Brokerage percentages are already in percentage units, not fractions.

    These six period summaries are not a daily return series. The provider does
    not identify its calculation method, so do not infer TWR/MWR or aggregate.
    """
    items = rows(raw, 'data')
    selected, conflicts = {}, set()
    invalid = False
    for item in items[:60]:
        timeframe = item.get('timeframe')
        value = number(item.get('return_percent'))
        if timeframe not in RETURN_TIMEFRAMES or value is None:
            invalid = True
            continue
        row = {'timeframe': timeframe, 'return_percent': value,
               'as_of': timestamp(item.get('created_date')),
               'method': 'unknown', 'source': 'broker_reported'}
        if timeframe in selected and selected[timeframe] != row:
            conflicts.add(timeframe)
        selected[timeframe] = row
    data = [selected[key] for key in RETURN_TIMEFRAMES if key in selected and key not in conflicts]
    return section(data, truncated=len(items) > 60,
                   error='Some broker returns were omitted because they were invalid or conflicting.'
                   if invalid or conflicts else None)


def connection(raw):
    kind = raw.get('type')
    return {
        'id': text(raw.get('id')), 'name': text(raw.get('name'), 'Brokerage connection'),
        'institution': text(obj(raw.get('brokerage')).get('display_name'),
                            text(obj(raw.get('brokerage')).get('name'), 'Brokerage')),
        'disabled': boolean(raw.get('disabled')),
        'is_read_only': kind == 'read' if kind in {'read', 'trade'} else None,
        'created_at': timestamp(raw.get('created_date')),
    }
