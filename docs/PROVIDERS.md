# Set up Lunch Flow and SnapTrade

Use Lunch Flow for bank balances and transactions, and SnapTrade for brokerage
accounts, holdings and activity. You can use either provider, both, or CSV/manual
accounts. First complete [installation and owner enrollment](DEPLOYMENT.md).
Personal provider keys are entered in the browser; no provider environment
variables or server restart are needed. These instructions describe the new
Settings controls; an existing server's environment connections continue working
without migration.

## Choose the connection method

| Situation | Lunch Flow | SnapTrade |
| --- | --- | --- |
| Your own self-hosted household | Personal API destination key | Personal `clientId` and `consumerKey` |
| Existing environment configuration | `LUNCH_FLOW_API` | `SNAPTRADE_ID`, `SNAPTRADE_KEY` |
| Customers authorizing your hosted product | Platform application and OAuth | Commercial application and read-only Connection Portal |

Personal keys entered in this app belong on an installation you control or
trust. They are submitted to its backend and encrypted with its installation
key. They are not returned by Settings or saved in browser storage. For a hosted
product, configure consumer authorization so customers use the provider's own
consent screen. This app currently implements SnapTrade Personal keys and the
Commercial portal; SnapTrade Personal OAuth is a separate integration and is not
implemented here.

## Lunch Flow: your own installation

1. Connect your banks in Lunch Flow. Create an API destination under
   **Destinations → Add Destination → API**, then copy its API key.
2. Open that destination's **Account Access** tab and enable the accounts you
   want the app to read. A connected bank can still be hidden from the API by
   these toggles. See Lunch Flow's [API destination guide](https://www.lunchflow.app/docs/guides/destinations/api).
3. In this app, open **Settings → Provider connections → Add provider connection**.
   Select **Lunch Flow**, paste your **API key**, and click **Connect and refresh**.
   The connection name is optional and defaults to Lunch Flow. The app saves the
   key, checks access and retrieves available accounts in one action.
4. Click **Review bank accounts**, select each account needing **Link history**, and
   choose its existing saved account. Use **Create a new … account** only when
   it has no saved history. Check bank identity and currency before confirming.
5. For a credit card, compare its balance with your statement and confirm how
   the provider reports debt. Check a known purchase separately before reversing
   transaction signs. Balance direction and transaction direction are independent.

Review any uncertain overlaps before counting them again. Pending transactions
remain separate. Non-GBP activity keeps its original currency; use
**Settings → Workspace reports** for converted cashflow. The established GBP
spending/planning views do not reinterpret foreign amounts as pounds.

If renewing a bank connection creates a new feed, open that account in
**Bank balances**, choose its existing **Saved account**, and confirm that it is
the same financial account. **Reconnect saved history** keeps the original
ledger and card settings. The previous feed must be missing, inactive or paused;
wait for any running refresh to finish. Create another account only for a
different financial account.

Lunch Flow's [Personal API](https://www.lunchflow.app/docs/api/personal-api-overview)
uses an API destination key. In this app it reads accounts, balances and
transactions; it does not initiate payments. A refresh retrieves the data the
provider currently has and does not establish complete historical coverage.
Its [balance endpoint](https://www.lunchflow.app/docs/api/personal-api/getAccountBalance)
returns a current amount; the [transaction endpoint](https://www.lunchflow.app/docs/api/personal-api-overview)
does not expose running balances. The app cannot turn those transactions into
certified older balances without independent balance anchors. Imported bank
statements can supply older dated balances.

## Correct previously imported HSBC dates

This is an operator command; browser PDF uploads remain retired. For an
existing root-Compose installation, deploy the current source with `./deploy.sh`
first and retain its complete encrypted recovery set. Use the original HSBC
PDFs, with their original filenames. Files in the checkout's `Data/` tree are
mounted as `/data`; previously saved app uploads are mounted as `/app/uploads`.

Run `./deploy.sh --check` to get the existing project and ordered Compose files.
In Bash, define the command below with that **exact** project and overlay list.
Replace `PROJECT` and `YOUR_EXISTING_OVERLAY.yml`; omit the example overlay if
there is none and include every real overlay before `deploy/compose.auto.yml`.

```sh
compose=(docker compose --env-file .env -p PROJECT
  -f docker-compose.yml -f YOUR_EXISTING_OVERLAY.yml
  -f deploy/compose.auto.yml)
```

Stop backend writes and any separate import or maintenance jobs that write to
the database, then preview one selected original PDF. Replace its path with the
actual file. Multiple selected PDFs can be supplied to the same command.

```sh
"${compose[@]}" stop backend
"${compose[@]}" --profile import run --rm --no-deps import \
  python -m scripts.reprocess_hsbc "/data/HSBC Debit/Statement.pdf" --offline-source
```

The default changes a temporary copy only. Its JSON lists transaction IDs and
old/new dates, with `mode: "preview"`, `source_modified: false`, zero new
transactions and preserved reviews. Check those dates against the PDF. If the
preview fails or the proposed dates are unexpected, restart the backend and
inspect the source rather than forcing an import.

If the preview is correct, repeat the same command with `--apply`, then restart
the existing backend and reload the app:

```sh
"${compose[@]}" --profile import run --rm --no-deps import \
  python -m scripts.reprocess_hsbc "/data/HSBC Debit/Statement.pdf" --offline-source --apply
"${compose[@]}" start backend
```

Credit-card PDFs can use `/data/HSBC Credit/Statement.pdf`; an existing
upload uses its actual `/app/uploads/...` path instead. The import service uses
the deployed image, the existing database and no network. The command refuses
unreconciled PDFs, new purchases, changed financial facts, account mappings or
reviews. It rechecks the database and PDF content before applying, updates all
selected sources in one transaction, and refreshes derived results without
provider or market requests. Errors roll back live corrections. The preview
uses the container's bounded private `/tmp`; a larger database needs an explicit
private encrypted work directory supplied with `--work-dir`.

## SnapTrade: your own installation

1. Create a Personal account in the [SnapTrade Dashboard](https://dashboard.snaptrade.com/)
   and connect your brokerage there. Enable two-factor authentication and create
   a Personal API key. Keep its `clientId` and `consumerKey` together. These steps
   follow SnapTrade's [Personal quickstart](https://docs.snaptrade.com/docs/getting-started).
2. Open **Settings → Provider connections → Add provider connection** and select
   **SnapTrade**. Put `clientId` in **Client ID** and `consumerKey` in **Personal
   API key**. Click **Connect and refresh**. The optional name defaults to
   SnapTrade, and the first refresh runs automatically.
3. Click **Review investments**, then inspect **Net worth**. Check the account list, native
   currencies, balances, holdings and the last successful retrieval time.

A Personal connection does not need `userId` or `userSecret`; Commercial keys
must not be entered in this Personal form. See the provider's
[authentication rules](https://docs.snaptrade.com/docs/authentication-methods).
The app's data transport permits reads only, including when the underlying
provider account has broader permissions.

Subsequent **Refresh data** actions also verify access, so you do not need a
separate check first. **Check connection** is available for troubleshooting and
reads only the account list. Checks and refreshes share a one-minute cooldown.
If the first refresh fails, the saved connection stays available: correct its
credentials or consent and retry **Refresh data** rather than adding a duplicate.

Use **Edit connection → Replace credentials** to rotate a Personal key on the
same connection. This preserves its source identity. **Pause** retains saved
credentials and stale history for later resumption. **Disconnect** removes the
local credentials; disconnected brokerage history remains available and is
excluded from default net worth to avoid counting a reconnection twice.

## Keep existing environment credentials

Existing installations can retain these backend variables in their private
`.env` file:

```dotenv
LUNCH_FLOW_API=YOUR_PERSONAL_DESTINATION_KEY
SNAPTRADE_ID=YOUR_PERSONAL_CLIENT_ID
SNAPTRADE_KEY=YOUR_PERSONAL_CONSUMER_KEY
```

Configure only the providers you use. Environment connections are shown as
managed by the server and are not editable through Settings. Do not add the same
credentials as another saved connection: rotate the existing configuration or
connection instead. Existing deployments should use their existing Compose
files and [deployment procedure](DEPLOYMENT.md#existing-installations).

For the **new standalone distribution only**, after editing its private `.env`,
recreate the backend from that installation directory:

```sh
docker compose up -d --no-deps --wait backend
```

`LUNCH_FLOW_SYNC_SECONDS` defaults to `21600` and `SNAPTRADE_SYNC_SECONDS` to `600`
in the new distribution. `0` disables that provider's automatic reads; positive
values have a ten-minute minimum. Saved connections use the same intervals with
independent operation leases. The app must remain running to collect observations.

## Lunch Flow: customers connecting to your product

1. Create a Platform app in Lunch Flow's Developer Dashboard. Register the exact
   callback `https://YOUR_HOST/connect/callback/lunchflow` and obtain its client
   ID and secret. See the [Platform guide](https://www.lunchflow.app/docs/api/platform-api-overview).
2. Configure the household backend with the following values. Use the actual
   public hostname, and apply the HTTPS/reverse-proxy instructions in the
   [deployment guide](DEPLOYMENT.md#https-and-a-managed-service).

   ```dotenv
   FINANCE_DEPLOYMENT=public
   FINANCE_AUTH_ENABLED=true
   FINANCE_COOKIE_SECURE=true
   FINANCE_PUBLIC_ORIGIN=https://YOUR_HOST
   FINANCE_ALLOWED_HOSTS=YOUR_HOST,localhost,127.0.0.1,backend
   LUNCH_FLOW_CLIENT_ID=YOUR_PLATFORM_CLIENT_ID
   LUNCH_FLOW_CLIENT_SECRET=YOUR_PLATFORM_CLIENT_SECRET
   ```

3. Recreate that backend. Sign in and open **Settings → Provider connections →
   Connect with Lunch Flow**. Complete the provider's consent flow in the same
   browser session. Return to Settings and click **Refresh data**, then link
   bank history as described above.

The backend handles code exchange and encrypted token refresh. It requires a
confidential application secret; Lunch Flow's public-client variant is not
implemented. Requests go directly to `www.lunchflow.app`: the bare host currently
redirects, and credential-bearing requests deliberately refuse redirects.

## SnapTrade: customers connecting to your product

1. Create a SnapTrade Commercial account and a **test** API key for initial
   validation. Arrange the provider's production approval and billing before
   switching to production credentials. See its
   [Commercial quickstart](https://docs.snaptrade.com/docs/getting-started#commercial-quickstart).
2. Use the same HTTPS/authentication settings above and add:

   ```dotenv
   SNAPTRADE_COMMERCIAL_ID=YOUR_COMMERCIAL_CLIENT_ID
   SNAPTRADE_COMMERCIAL_KEY=YOUR_COMMERCIAL_CONSUMER_KEY
   ```

3. The return address is `https://YOUR_HOST/connect/callback/snaptrade`. Allow
   this origin/return address in any restrictions on your provider application.
   The app sends it as the portal's `customRedirect`, with a one-use state value;
   it is not a SnapTrade OAuth client registration.
4. Recreate the backend. Sign in and choose **Settings → Provider connections →
   Connect with SnapTrade**. Select a brokerage in the portal and complete
   consent. Back in Settings, click **Refresh data**, then inspect Investments.

The app registers and encrypts a household-specific SnapTrade user identity.
The portal requests `connectionType=read`. Commercial `userId` and `userSecret`
are managed server-side, following the provider's
[Commercial authentication flow](https://docs.snaptrade.com/docs/authentication-methods).
The app does not place orders or request a forced brokerage sync.

**Reconnect with provider** reuses an existing saved identity and opens the
portal. Its general UI can add a brokerage; targeted disabled-brokerage repair
is supported by the authorization API's `broker_connection_id`, not a dedicated
brokerage repair picker. If you disconnect locally, the saved user secret is
removed. A later SnapTrade connection gets a new identity; the old history is
retained separately. Manage remote consent in the provider account as well.

## Try an isolated household before connecting real accounts

Create a separate instance using the [new-installation guide](DEPLOYMENT.md),
with a unique project name, hostname/port and fresh volumes. Keep your current
deployment's `.env`, databases and key out of it. Use test credentials only.

SnapTrade provides a simulated **Sandbox** brokerage with non-production keys.
In the read-only portal, select **Sandbox**, then try **Self-directed**, **Cash
only**, **No transactions** and **No accounts**. It also offers failure scenarios
such as invalid credentials and rate limiting. Availability and scenario details
are in the [official Sandbox guide](https://docs.snaptrade.com/docs/sandbox).
Do not infer a complete history from a successful connection with no activity.

You can also rehearse imports without a provider: add a manual EUR account in
**Settings → Account settings**, then upload this synthetic file under
**Import a CSV** (map `Date`, `Description`, `Amount`, and `ID`; choose EUR):

```csv
Date,Description,Amount,ID
2026-08-10,Synthetic coffee,-4.50,coffee-one
2026-08-10,Synthetic coffee,-4.50,coffee-two
```

The preview should show two new purchases. Import, then preview the same data
with a different filename and the same **Import source**: both rows should be
already imported. In **Workspace reports**, select EUR and August 2026 to see
€9 spending without conversion. The established GBP spending view should not
count those two EUR amounts as pounds. Try ownership at 50% to see €4.50 in the
derived report while the original transactions remain €4.50 each.

For Lunch Flow, this repository includes offline fixtures for consent, expiring
tokens, native-currency transactions and independent account failures. They use
no provider credentials. A real consent rehearsal still requires your configured
Platform app and provider-side test arrangement; the offline checks do not claim
to have completed a live bank login.

Run the transport and authorization checks from the repository:

```sh
cd backend
../.venv/bin/python -m unittest tests.test_provider_wire_contracts tests.test_provider_oauth -v
```

The wire checks exercise the installed SnapTrade SDK, real request serialization,
encrypted storage and separate household sessions. Only provider responses are
synthetic; outbound sockets are blocked.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| No “Connect with …” button | HTTPS on standard port 443, secure cookies, enabled authentication, allowed hostname, public origin, both operator credentials and the original encryption key |
| “Connection checked” but no accounts in views | Run **Refresh data** after the one-minute cooldown; checking credentials does not import data |
| Lunch Flow account missing | Enable it in the API destination's Account Access tab, then refresh |
| “Link history” or a card excluded from totals | Link the correct saved account and confirm debt direction; resolve uncertain overlaps in Banks & accounts |
| One-minute wait / operation already running | Let the current operation finish; automatic and manual actions use the same connection lease |
| Consent expired or cancelled | Return to Settings and reconnect in the same signed-in browser; old callback links are one-use |
| Provider authorization unavailable after a restart | Restore the matching operator client configuration and installation key; do not substitute a different app identity |
| Missing conversion rates or partial totals | Review the currency report's withheld events and dated FX evidence; refresh references explicitly when needed |
| Saved credentials cannot be decrypted after restore | Restore the key belonging to the database backup; a newly generated key cannot decrypt old connections |

Provider documentation checked on 22 September 2026. The app-specific steps and
environment names above refer to this implementation, not generic SDK examples.

## Refresh, saved values and incomplete prices

**Refresh data** reads the provider and automatically saves a successful result as
its latest local snapshot. No additional Save action is needed. Refreshing the
browser reads saved data; it does not force a brokerage to produce a new balance.
Net worth now follows saved-data revisions, including background syncs and changes
in another tab. Provider observation dates remain separate from retrieval times.

Broker account balances include cash and determine investment account values in
net worth. Public market prices power the separate market valuation and session
move. A missing previous close can make the session move a subtotal even when all
current holdings and account values are known. The app shows the missing date and
coverage; it never fills a missing close with zero or an unrelated listing.

Price refresh tries a short recent window when the longer feed omits a previous
weekday. Previously verified closes are retained only for the same listing,
currency, exchange and split basis, with their original retrieval provenance.
A provider gap or exchange holiday can still leave a session move unavailable.
Repeated refreshes cannot guarantee that upstream publishes missing data.

Failed connections keep their last saved evidence and report the failure. Saved
connections are attempted even if the legacy environment connection fails.
Unavailable transaction history does not by itself mark current holdings stale.
Newly discovered bank accounts needing linking are called out in net worth;
confirm account type and debt direction before including a new card.

## Different households and institutions

Use **one private installation per household** for this release: a separate
database, encryption key, credentials and owner enrollment for each installation.
The owner and household members share that installation's financial data. This
is not a shared signup service for unrelated customers; deploying several tenants
in one database requires additional authorization and data isolation work.

Account discovery is driven by provider responses, not a fixed list of your
banks. Multiple institutions, same-name accounts and native currencies retain
separate account identities. Unknown institution names, including non-Latin
names, remain usable. New provider products may expose balances without activity,
cash-only accounts, pending initial sync, unsupported securities or missing FX;
coverage messages distinguish these cases instead of treating unknowns as zero.

Settings includes a searchable SnapTrade directory with locally bundled PNGs for
all **43 published integrations checked on 23 September 2026**. Actual eligibility
and account coverage depend on your provider, region and consent. Unknown/new
brands use initials until artwork is added; a missing logo never blocks discovery.
Lunch Flow supplies its own available bank list. Other providers currently need a
manual account/CSV import or a new tested adapter; adding artwork alone does not
implement a connector.

For maintainers, `frontend/src/institutionCatalog.json` records each artwork
source. Run `python3 scripts/sync_institution_logos.py --check` offline, or install
Pillow and CairoSVG in a development environment and run the script with
`--refresh` to update the public directory and local PNGs. Review its resulting
catalog/assets and run frontend checks before release. Runtime pages never load
artwork from account-supplied URLs.
