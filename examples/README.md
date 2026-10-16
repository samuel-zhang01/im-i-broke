# Try a CSV without connecting a bank

Every row in [transactions.csv](transactions.csv) is invented. Use a separate
test installation if you do not want sample rows in your personal workspace.

1. In **Settings → Account settings**, add a manual GBP cash account called
   **Demo account**. Add a balance separately if you want a known current value;
   imported transactions alone are not a verified current balance.
2. Open **Settings → Import a CSV**. Choose Demo account and a new import source
   called **Synthetic example**. Upload `transactions.csv`.
3. Map `Date`, `Description`, `Amount`, and `ID`. Choose GBP, ISO dates
   (`YYYY-MM-DD`), a dot decimal separator and negative amounts for spending.
4. Check the preview: three new rows, £2,000 income and £47 spending. Confirm.
5. In **Workspace reports**, choose GBP and August 2026. The report should show
   those amounts. Preview the same file with the same import source again:
   all three rows should be recognized as already imported.

The sample does not create a provider connection or use real account details.
