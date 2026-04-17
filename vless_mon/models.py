from dataclasses import dataclass
from typing import Optional


@dataclass
class Server:
    name: str
    address: str
    port: int
    raw_uri: str
    id: Optional[int] = None
    # "up" | "degraded" | "down" | "unknown"
    status: str = "unknown"
    # consecutive failed Mihomo checks
    fail_count: int = 0
    last_check: Optional[str] = None
    # last status we actually sent an alert for: "up" | "degraded" | "down"
    last_alert_status: Optional[str] = None
    # last TCP ping latency in ms (None = no response)
    ping_ms: Optional[int] = None
    active: bool = True
