import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                pass
    return frozenset(result)


@dataclass(frozen=True)
class Config:
    # Mihomo API
    mihomo_api_url: str
    mihomo_secret: str
    mihomo_provider_name: str
    mihomo_provider_path: str
    mihomo_config_path: str

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    # Comma-separated Telegram user IDs allowed to send bot commands.
    # Empty → no restriction (any user in the chat can run commands).
    telegram_admin_ids: frozenset[int]
    # Optional SOCKS5/SOCKS4 proxy for outbound Telegram requests.
    # Format: socks5://user:pass@host:port  or  socks5://host:port
    telegram_socks_proxy: str

    # Subscription
    subscription_url: str
    subscription_interval: int  # seconds

    # Monitoring
    check_interval: int      # seconds
    fail_threshold: int      # consecutive failures before DOWN alert
    check_timeout: int       # milliseconds, passed to Mihomo API
    check_url: str

    # Storage / logging
    db_path: str
    log_level: str
    log_file: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            mihomo_api_url=os.getenv("MIHOMO_API_URL", "http://127.0.0.1:9090"),
            mihomo_secret=os.getenv("MIHOMO_SECRET", ""),
            mihomo_provider_name=os.getenv("MIHOMO_PROVIDER_NAME", "vless-mon"),
            mihomo_provider_path=os.getenv(
                "MIHOMO_PROVIDER_PATH", "/etc/mihomo/vless-mon-proxies.yaml"
            ),
            mihomo_config_path=os.getenv(
                "MIHOMO_CONFIG_PATH", "/etc/mihomo/config.yaml"
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            telegram_admin_ids=_parse_ids(os.getenv("TELEGRAM_ADMIN_IDS", "")),
            telegram_socks_proxy=os.getenv("TELEGRAM_SOCKS_PROXY", ""),
            subscription_url=os.getenv("SUBSCRIPTION_URL", ""),
            subscription_interval=int(os.getenv("SUBSCRIPTION_INTERVAL", "1800")),
            check_interval=int(os.getenv("CHECK_INTERVAL", "30")),
            fail_threshold=int(os.getenv("FAIL_THRESHOLD", "3")),
            check_timeout=int(os.getenv("CHECK_TIMEOUT", "5000")),
            check_url=os.getenv(
                "CHECK_URL", "http://www.gstatic.com/generate_204"
            ),
            db_path=os.getenv("DB_PATH", "/var/lib/vless-mon/vless_mon.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("LOG_FILE", "/var/log/vless-mon/vless-mon.log"),
        )
