<div align="center">
  <img src="frontend/public/assets/brand/app-192-v1.png" alt="" width="88" height="88">
  <h1>Am I Broke?</h1>
  <p><strong>A calmer view of your money.</strong></p>
  <p>Spending, accounts and investments in a private, self-hosted workspace.</p>
  <p><a href="#quick-start">Get started</a> · <a href="#connect-your-accounts">Connect accounts</a> · <a href="#guides">Guides</a> · <a href="CONTRIBUTING.md">Contribute</a></p>
</div>

<p align="center">
  <a href="https://github.com/samuel-zhang01/im-i-broke/actions/workflows/ci.yml"><img src="https://github.com/samuel-zhang01/im-i-broke/actions/workflows/ci.yml/badge.svg" alt="Synthetic application CI"></a>
  <a href="LICENSE">AGPL-3.0</a>
</p>

---

See where your money goes, review recurring spending and follow your net worth.
Connect supported banks and brokerages, import a CSV, or start with a manual
account. Your existing records stay available when a connection is paused.
Each person or household runs their own private installation. This repository
contains the app's source; there is no shared signup service.

## Quick start

You need **Docker with the Compose plugin**, **Python 3.10+**, **Git** and an
**authenticator app**. Use Docker Engine on Linux or Docker Desktop with Linux
containers on macOS/Windows. On Windows, run these commands in **WSL 2** with
Docker Desktop's WSL integration enabled. Docker must be running; the first
build needs internet access and can take several minutes.

Open a terminal and run:

```sh
git clone https://github.com/samuel-zhang01/im-i-broke.git
cd im-i-broke
python3 scripts/setup.py ~/ledger-home --build
```

Choose an installation directory that does **not** already exist. Setup builds
and starts the app, creates private storage and prints a localhost address and
one-time setup token. **No `.env` editing or bank credentials required.**

1. Open the printed address and enter the setup token.
2. Choose a password, add the authenticator and save your recovery codes.
3. Follow the welcome guide to choose a currency and add your first account.

Try the [invented sample CSV](examples/README.md) to explore imports without a
bank connection. Keep your installation directory outside the source checkout.

The localhost address is available on the computer running Docker. For another
device or HTTPS hosting, follow the [deployment guide](docs/DEPLOYMENT.md).
Already running an installation? Keep your configuration and follow
[the upgrade instructions](docs/DEPLOYMENT.md#release-and-upgrade).

<details>
<summary><strong>Everyday management</strong></summary>

```sh
python3 ~/ledger-home/manage.py status
python3 ~/ledger-home/manage.py stop
python3 ~/ledger-home/manage.py start
```

Stopping keeps your data. `manage.py token` shows an unused setup token again;
`manage.py renew-token` renews one that expired before enrollment. See
[backup and recovery](docs/BACKUP.md) before upgrading or moving your installation.

</details>

<details>
<summary><strong>What you can customize</strong></summary>

| Make it yours | Keep the detail |
| --- | --- |
| Name accounts, group institutions and set ownership shares | Keep original transactions and source history |
| Reuse CSV mappings for different bank exports | Preview imports and review possible duplicates |
| Choose a reporting currency | See missing conversion evidence and partial totals |
| Use desktop or mobile | Protect access with a password and authenticator |

</details>

## Connect your accounts

Open **Settings → Provider connections** when you are ready. Connections are optional.

| Source | What you can bring in | Setup |
| --- | --- | --- |
| **Lunch Flow** | Bank balances and transactions | [Personal API key](docs/PROVIDERS.md#lunch-flow-your-own-installation) |
| **SnapTrade** | Brokerage accounts, holdings and activity | [Personal connection](docs/PROVIDERS.md#snaptrade-your-own-installation) |
| **CSV or manual** | Other accounts and bank exports | [Account settings and imports](docs/CUSTOMIZATION.md) |

Bank and brokerage availability depends on your provider and region. Providers
may require their own subscription. Native-currency and converted reports live
in **Settings → Workspace reports**; the established spending and planning views
remain GBP-based.
Workspace report amounts and exchange rates follow your chosen locale; the
interface remains in English.

## Guides

This README is the starting point. Open a guide only when you need that task.

| I want to… | Read |
| --- | --- |
| Host, manage or upgrade the app | [Deployment](docs/DEPLOYMENT.md) |
| Code elsewhere and deploy to an existing checkout | [Automatic deployment](docs/AUTOMATIC_DEPLOYMENT.md) |
| Connect Lunch Flow or SnapTrade | [Providers](docs/PROVIDERS.md) |
| Customize accounts or import a bank CSV | [Customization](docs/CUSTOMIZATION.md) |
| Back up or restore my installation | [Backup and recovery](docs/BACKUP.md) |
| Understand privacy and access protection | [Security](SECURITY.md) |
| Fix a setup problem | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| Check supported scope and release checks | [Known limitations](docs/KNOWN_LIMITATIONS.md) · [Validation](docs/RELEASE_VALIDATION.md) |
| Develop on several computers | [Development workflow](docs/DEVELOPMENT_WORKFLOW.md) |
| Contribute a change or run checks | [Contributing](CONTRIBUTING.md) |

## Project status

An evolving self-hosted app, with **one owner per household** and support for
multiple signed-in devices. Hosting separate households uses isolated instances;
public registration, billing and shared-database tenancy are not implemented.

Financial records stay on your server; configured providers and market-data
services receive the requests needed for their features. Live databases and
uploads are **not encrypted at rest by the app**. Protect the host and keep a
tested, encrypted backup with its matching installation key.

## License

Project code is licensed under [GNU AGPL version 3](LICENSE). If you host a
modified version, provide its users access to the corresponding source and
update the app's Settings source link to that version. Dependencies and
institution logos retain their own terms; see [NOTICE](NOTICE) and
[logo attribution](frontend/public/logos/SOURCES.md).
