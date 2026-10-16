# Setup troubleshooting

Start with [the quick start](../README.md#quick-start). The installer creates a
new private directory and never initializes over an existing household.

| Problem | Action |
| --- | --- |
| `docker` or Compose is missing | Install Docker Engine and its Compose plugin on Linux, or Docker Desktop with Linux containers on macOS/Windows. Check `docker compose version` and `docker info`. |
| Docker daemon is unavailable | Start Docker. On Linux, configure Docker access for your account; avoid running the app setup as root just to bypass a socket permission error. |
| Windows commands fail | Use a WSL 2 terminal with Docker Desktop's WSL integration enabled. Keep the checkout and installation inside WSL. |
| `python3` is missing | Install Python 3.10 or newer; check `python3 --version`. Host Python needs only the standard library for setup. |
| Remote Docker context refused | Run setup on the machine hosting Docker. Select its local context; do not point this helper at an SSH/TCP Docker daemon. |
| Installation directory exists | Choose a new directory. If it is an existing installation, use its `manage.py` instead of running setup again. |
| Build failed | Read the build error and check internet/disk space. Retry with `python3 ~/ledger-home/manage.py build` after fixing it. Keep the original checkout available until the first successful build. |
| Port is busy | Choose another with `--port 8088`. Existing installations retain the original port in their private configuration. |
| Browser cannot open the address | The printed localhost address belongs to the Docker host. Check `manage.py status`, `manage.py check` and `manage.py logs`. For another device, configure [HTTPS hosting](DEPLOYMENT.md#https-and-a-managed-service). |
| Setup token expired | Run `python3 ~/ledger-home/manage.py renew-token`. This works only before owner enrollment. |
| Lost authenticator | Use a saved recovery code. If necessary, follow [offline owner recovery](../SECURITY.md#owner-recovery); preserve the matching installation key. |
| Bank connection is missing or fails | Manual accounts and CSV work without a provider. Check [provider troubleshooting](PROVIDERS.md#troubleshooting) before adding another connection. |

To ask for help, open a GitHub issue with the app revision, OS, Docker/Python
versions and a short description. Remove financial data, keys, setup tokens,
session cookies, hostnames and private paths from logs before sharing. Use
[private reporting](../SECURITY.md#report-a-vulnerability) for vulnerabilities.

Stopping the app keeps its data. Never use `docker compose down -v` as a repair
or update command; it deletes the installation's volumes.
