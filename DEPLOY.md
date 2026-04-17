# Deployment Guide — vless-monitor

## Prerequisites

- Python 3.11+
- [Mihomo (Clash.Meta)](https://github.com/MetaCubeX/mihomo) running locally with external controller enabled
- A Telegram bot token and chat ID

## Mihomo config.yaml — required additions

Add a proxy-provider block so Mihomo knows about the daemon-managed file:

```yaml
external-controller: 127.0.0.1:9090
secret: your_clash_api_secret   # must match MIHOMO_SECRET in .env

proxy-providers:
  vless-mon:                     # must match MIHOMO_PROVIDER_NAME in .env
    type: file
    path: /etc/mihomo/vless-mon-proxies.yaml   # must match MIHOMO_PROVIDER_PATH
    health-check:
      enable: false              # daemon handles health-checks itself
```

## Installation

```bash
# 1. Create a dedicated system user
sudo useradd -r -s /bin/false -d /opt/vless-mon vless-mon

# 2. Deploy code
sudo mkdir -p /opt/vless-mon
sudo cp -r /path/to/vless_mon /opt/vless-mon/
sudo python3 -m venv /opt/vless-mon/venv
sudo /opt/vless-mon/venv/bin/pip install -r /opt/vless-mon/requirements.txt

# 3. Create runtime directories
sudo mkdir -p /var/lib/vless-mon /var/log/vless-mon
sudo chown vless-mon:vless-mon /var/lib/vless-mon /var/log/vless-mon

# 4. Configure
sudo cp /opt/vless-mon/.env.example /opt/vless-mon/.env
sudo nano /opt/vless-mon/.env
sudo chown vless-mon:vless-mon /opt/vless-mon/.env
sudo chmod 640 /opt/vless-mon/.env

# 5. Allow the daemon user to write to the Mihomo provider file
sudo chown vless-mon:vless-mon /etc/mihomo/vless-mon-proxies.yaml \
  || sudo touch /etc/mihomo/vless-mon-proxies.yaml \
  && sudo chown vless-mon:vless-mon /etc/mihomo/vless-mon-proxies.yaml

# 6. Install and start the systemd service
sudo cp /opt/vless-mon/vless-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vless-monitor
```

## Operations

```bash
# Live logs
sudo journalctl -u vless-monitor -f

# File logs (rotated automatically, compressed after 10 MB)
tail -f /var/log/vless-mon/vless-mon.log

# Restart after config change
sudo systemctl restart vless-monitor

# Status
sudo systemctl status vless-monitor

# Stop gracefully (sends SIGTERM → 30 s timeout)
sudo systemctl stop vless-monitor
```

## Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `MIHOMO_API_URL` | `http://127.0.0.1:9090` | Mihomo external controller |
| `MIHOMO_SECRET` | _(empty)_ | Bearer token for Mihomo API |
| `MIHOMO_PROVIDER_NAME` | `vless-mon` | proxy-provider key in config.yaml |
| `MIHOMO_PROVIDER_PATH` | `/etc/mihomo/vless-mon-proxies.yaml` | File daemon writes proxies to |
| `MIHOMO_CONFIG_PATH` | `/etc/mihomo/config.yaml` | Path for full config reload (fallback) |
| `TELEGRAM_BOT_TOKEN` | _(required)_ | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | _(required)_ | Target chat/channel ID |
| `TELEGRAM_SOCKS_PROXY` | _(empty)_ | SOCKS5/4 proxy for Telegram, e.g. `socks5://host:1080` |
| `SUBSCRIPTION_URL` | _(required)_ | VLESS subscription URL |
| `SUBSCRIPTION_INTERVAL` | `1800` | Seconds between subscription refreshes |
| `CHECK_INTERVAL` | `30` | Seconds between monitoring rounds |
| `FAIL_THRESHOLD` | `3` | Consecutive failures before DOWN alert |
| `CHECK_TIMEOUT` | `5000` | ms passed to Mihomo delay API |
| `CHECK_URL` | `http://www.gstatic.com/generate_204` | Probe URL for Mihomo |
| `DB_PATH` | `/var/lib/vless-mon/vless_mon.db` | SQLite database path |
| `LOG_LEVEL` | `INFO` | loguru level (DEBUG/INFO/WARNING) |
| `LOG_FILE` | `/var/log/vless-mon/vless-mon.log` | Rotating log file path |

## Alert logic

```
fail_count < FAIL_THRESHOLD   → silent (anti-flapping)
fail_count >= FAIL_THRESHOLD
  AND last_alert != "down"    → send DOWN alert, set last_alert = "down"

check succeeds
  AND last_alert == "down"    → send UP alert, reset fail_count = 0
  otherwise                   → silent
```

## Troubleshooting

**Daemon starts but no checks run**
- Check `MIHOMO_API_URL` is reachable: `curl http://127.0.0.1:9090/proxies`
- Ensure Mihomo has the `vless-mon` proxy-provider in its config

**404 on /proxies/{name}/delay**
- The node name in DB must exactly match the proxy name in Mihomo
- Reload Mihomo after provider file is written: `curl -X PUT http://127.0.0.1:9090/providers/proxies/vless-mon`

**Permission denied writing provider YAML**
- `sudo chown vless-mon:vless-mon /etc/mihomo/vless-mon-proxies.yaml`
- Or add `/etc/mihomo` to `ReadWritePaths` in the .service file
