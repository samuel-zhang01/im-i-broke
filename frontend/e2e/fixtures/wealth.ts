export const accounts = [
  { id: 'investment', name: 'Demo portfolio', institution: 'Demo Broker', kind: 'investment', source: 'snaptrade', value: 2500 },
  { id: 'cash', name: 'Demo current', institution: 'Demo Bank', kind: 'cash', source: 'lunchflow', value: 1000 },
  { id: 'card', name: 'Demo card', institution: 'Demo Bank', kind: 'credit', source: 'lunchflow', value: -100 },
].map(a => ({ ...a, currency: 'GBP', native_value: a.value, as_of: '2026-09-21', stale: false, estimated: false, history_points: 2, default_included: true, exclusion_reason: null }));
const points = ['2026-09-20', '2026-09-21'].map(date => ({ date, net_worth: 3400, assets: 3500, liabilities: 100, known_accounts: 3, missing_accounts: 0, estimated: false, carried_accounts: 0, coverage_key: 'demo', complete: true }));
export const wealth = {
  generated_at: '2026-09-21T12:00:00Z', currency: 'GBP', currencies: ['GBP'], range: '1m', accounts, selected_account_ids: accounts.map(a => a.id), points, warnings: [], methodology: [],
  summary: { net_worth: 3400, assets: 3500, liabilities: 100, known_accounts: 3, selected_accounts: 3, missing_accounts: 0, stale_accounts: 0, partial: false, change: 0, change_pct: 0, change_comparable: true },
  history: { mode: 'comparable_cohort', account_ids: accounts.map(a => a.id), excluded_account_ids: [], account_count: 3, selected_accounts: 3, start_date: '2026-09-20', end_date: '2026-09-21', current_value: 3400, end_value: 3400, change: 0, change_pct: 0, change_comparable: true, points, coverage_note: '' },
  cache: { saved_at: null, stale: false, refresh_error: null },
};
