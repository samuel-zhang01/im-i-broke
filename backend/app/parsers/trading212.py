"""Trading 212 CSV events, with separate native-currency cash movements.

``amount`` is the actual GBP cash impact, never a native amount relabelled GBP.
Non-GBP events retain shares, cash and realised results in metadata. Historical
GBP cost is explicitly unknown where the export supplies no GBP conversion.
No statement balance is provided, so this ledger cannot independently reconcile.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional

from .base import RawTxn, ParsedStatement

BUY = {'Market buy', 'Limit buy', 'Stop buy', 'Stop limit buy'}
SELL = {'Market sell', 'Limit sell', 'Stop sell', 'Stop limit sell'}
INCOME = {'Interest on cash', 'Lending interest', 'Spending cashback'}
SPEND = {'Card debit', 'New card cost'}


def _amount_and_category(action: str, total: float) -> tuple[float, str]:
    a = action.strip()
    if a in BUY:
        return -abs(total), 'Investments'
    if a in SELL:
        return abs(total), 'Investments'
    if a == 'Deposit':
        return abs(total), 'Transfers & payments'
    if a == 'Withdrawal':
        return -abs(total), 'Transfers & payments'
    if a.startswith('Dividend') or a in INCOME:
        return total, 'Investment income'
    if a == 'Card credit':
        return abs(total), 'Shopping'  # reversal of card spending, not investment return
    if a in SPEND:
        return -abs(total), 'Shopping' if a == 'Card debit' else 'Fees & interest'
    if a == 'Currency conversion':
        return total, 'Investments'
    return total, 'Investments'


def _date(s: str) -> Optional[date]:
    try:
        return datetime.fromisoformat((s or '').strip().replace('Z', '+00:00')).date()
    except ValueError:
        return None


def canonical_time(value: str) -> str:
    """Compare equivalent old clock strings and newly explicit UTC timestamps.

    Legacy exports omit the timezone. Their original value and that limitation
    remain in metadata; an explicit offset is converted to UTC before ordering.
    """
    parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=' ')


def _f(s: str | None) -> Optional[float]:
    if s is None or not s.strip():
        return None
    try:
        value = float(s.replace(',', '').strip())
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def _currency(value: str | None) -> str:
    """Preserve the case-sensitive GBp (pence) unit before uppercasing codes."""
    value = (value or '').strip()
    return 'GBX' if value == 'GBp' else value.upper()


def _cash_movements(row: dict, action: str, signed_total: float, currency: str) -> dict[str, float]:
    if action != 'Currency conversion':
        if not currency:
            raise ValueError('Trading 212 cash event is missing its currency')
        return {currency: signed_total}
    # On currency-conversion rows Total is the fee. The transfer itself is in
    # the explicit from/to columns; to-amount is before the separate fee.
    movements: dict[str, Decimal] = {}
    for column, sign in (('Currency conversion from amount', -1), ('Currency conversion to amount', 1)):
        value = _f(row.get(column))
        ccy = _currency(row.get(f'Currency ({column})'))
        if value is None or not ccy:
            raise ValueError('Trading 212 currency conversion is missing a cash leg')
        movements[ccy] = movements.get(ccy, Decimal(0)) + sign * Decimal(str(abs(value)))
    fee = _f(row.get('Currency conversion fee'))
    fee_currency = _currency(row.get('Currency (Currency conversion fee)') or currency)
    if fee is None:
        fee = signed_total
    if fee:
        if not fee_currency:
            raise ValueError('Trading 212 currency-conversion fee currency is missing')
        movements[fee_currency] = movements.get(fee_currency, Decimal(0)) - Decimal(str(abs(fee)))
    return {k: float(v) for k, v in movements.items()}


def parse(path: str, account_kind: str | None = None) -> ParsedStatement:
    txns: list[RawTxn] = []
    with open(path, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        if not {'Action', 'Total', 'Currency (Total)'}.issubset(columns) or not {'Time', 'Time (UTC)'}.intersection(columns):
            raise ValueError('Trading 212 export is missing required cash-ledger columns')
        if len(columns) != len(set(columns)):
            raise ValueError('Trading 212 export contains duplicate column names')
        for line_no, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f'Trading 212 row {line_no} has more values than the header; check its CSV quoting')
            action = (row.get('Action') or '').strip()
            if not action and not any(row.values()):
                continue
            # Source exports currently omit corporate actions. Never guess that
            # an unfamiliar split/consolidation quantity is a purchase or a
            # cash event: a future importer must reconcile it to the existing
            # effective-dated ledger before accepting it.
            if any(word in action.lower() for word in ('split', 'consolidation', 'corporate action')):
                raise ValueError(f'Trading 212 row {line_no}: corporate-action rows require review against the '
                                 'sourced corporate-action ledger; no changes have been imported')
            total = _f(row.get('Total'))
            time_column = 'Time (UTC)' if row.get('Time (UTC)') else 'Time'
            source_time = (row.get(time_column) or '').strip()
            try:
                time = canonical_time(source_time)
            except ValueError:
                time = ''
            dt = _date(time)
            if not action or total is None or dt is None:
                raise ValueError(f'Trading 212 row {line_no} has an invalid action, total or date')
            currency = _currency(row.get('Currency (Total)'))
            signed_total, category = _amount_and_category(action, total)
            movements = _cash_movements(row, action, signed_total, currency)
            amount = round(movements.get('GBP', 0.0), 2)
            ticker = (row.get('Ticker') or '').strip()
            name = (row.get('Name') or '').strip()
            notes = (row.get('Notes') or '').strip()
            desc = ' '.join(p for p in [action, ticker, name] if p)
            if notes and not ticker:
                desc = f'{desc} — {notes}'
            merchant = (row.get('Merchant name') or '').strip() or ticker or name or action
            shares = _f(row.get('No. of shares'))
            signed_shares = None
            if shares and action not in BUY | SELL and not action.startswith('Dividend'):
                raise ValueError(f'Trading 212 row {line_no}: unsupported share-bearing event {action!r}; '
                                 'review its corporate-action treatment before importing')
            if action in BUY | SELL:
                if shares is None or shares <= 0 or not (row.get('ISIN') or '').strip():
                    raise ValueError(f'Trading 212 row {line_no} has invalid share quantity or security identity')
                signed_shares = shares if action in BUY else -shares
            result = _f(row.get('Result'))
            if row.get('Result') and result is None:
                raise ValueError(f'Trading 212 row {line_no} has an invalid realised result')
            result_currency = _currency(row.get('Currency (Result)') or currency)
            price = _f(row.get('Price / share'))
            price_currency = _currency(row.get('Currency (Price / share)'))
            exchange_rate = _f(row.get('Exchange rate'))
            if action in BUY | SELL and ((row.get('Price / share') and (price is None or price <= 0))
                                        or (row.get('Exchange rate') and (exchange_rate is None or exchange_rate <= 0))):
                raise ValueError(f'Trading 212 row {line_no} has invalid execution price or exchange rate')
            fees = []
            for field in columns:
                if field.startswith('Currency (') or not any(word in field.lower() for word in ('fee', 'tax')) or not row.get(field):
                    continue
                fee = _f(row[field])
                fee_currency = _currency(row.get(f'Currency ({field})'))
                if fee is None or (fee != 0 and not fee_currency):
                    raise ValueError(f'Trading 212 row {line_no} has invalid {field} or missing fee currency')
                fees.append({'name': field, 'amount': fee, 'currency': fee_currency or None})
            provider_id = (row.get('ID') or '').strip() or None
            trade_value_check = None
            if action in BUY | SELL and price is not None and exchange_rate and price_currency:
                converted_fees = Decimal(0)
                fee_currencies_known = True
                for fee in fees:
                    value = abs(Decimal(str(fee['amount'])))
                    if fee['currency'] == currency:
                        converted_fees += value
                    elif fee['currency'] == price_currency:
                        converted_fees += value / Decimal(str(exchange_rate))
                    elif value:
                        fee_currencies_known = False
                if fee_currencies_known:
                    gross = Decimal(str(shares)) * Decimal(str(price)) / Decimal(str(exchange_rate))
                    calculated = gross + (converted_fees if action in BUY else -converted_fees)
                    residual = Decimal(str(abs(total))) - calculated
                    if abs(residual) > Decimal('0.02'):
                        raise ValueError(f'Trading 212 row {line_no}: total disagrees with units, execution price, exchange rate and fees; review the export before importing')
                    trade_value_check = {'calculated_total': float(calculated), 'reported_total': abs(total),
                                         'residual': float(residual), 'currency': currency,
                                         'formula': 'units × printed price ÷ exchange rate, plus buy fees or minus sell fees'}
            meta = {
                'action': action, 'time': time,
                'source_time': source_time, 'source_time_column': time_column,
                'time_timezone': 'UTC' if time_column == 'Time (UTC)' or datetime.fromisoformat(source_time.replace('Z', '+00:00')).tzinfo else 'unspecified',
                'ticker': ticker or None, 'security_name': name or None,
                'provider_id': provider_id,
                'provider_id_kind': ('fill' if provider_id.startswith('EOF') else 'event') if provider_id else None,
                'execution_price': price, 'execution_price_currency': price_currency or None,
                'execution_price_major': price / 100 if price is not None and price_currency == 'GBX' else price,
                'execution_currency_major': 'GBP' if price_currency == 'GBX' else price_currency or None,
                'exchange_rate': exchange_rate,
                # Trade totals divide the printed price currency by this rate.
                # Dividend-rate semantics differ, so never reuse that inference.
                'exchange_rate_direction': 'price_currency_per_total_currency' if action in BUY | SELL and exchange_rate else None,
                'trade_value_check': trade_value_check,
                'fees': fees, 'source_row': dict(row), 'source_line': line_no,
                'source_columns': columns,
                'isin': (row.get('ISIN') or '').strip() or None, 'shares': signed_shares,
                'native_currency': currency or None, 'native_amount': signed_total,
                'cash_movements': movements,
                'gbp_value_known': currency == 'GBP' or action == 'Currency conversion',
                'cost_basis_gbp': abs(amount) if action in BUY and currency == 'GBP' else None,
                'proceeds_gbp': abs(amount) if action in SELL and currency == 'GBP' else None,
                'result': result if result_currency == 'GBP' else None,
                'result_native': result, 'result_currency': result_currency or None,
            }
            txns.append(RawTxn(
                txn_date=dt, description=desc, amount=amount, category=category, merchant=merchant,
                ext_id=provider_id, type_code=action, raw=desc, meta=meta))

    dated = [t.txn_date for t in txns]
    statement = ParsedStatement(
        bank='Trading212', account_type='investment', account_id='Trading212-invest',
        sort_code=None, account_number=None, period_start=min(dated) if dated else None,
        period_end=max(dated) if dated else None, opening_balance=None, closing_balance=None,
        txns=txns, source_file=path)
    if account_kind:
        from ..trading212_accounts import assign
        assign(statement, account_kind)
    return statement
