from dataclasses import dataclass
from typing import Optional


@dataclass
class Server:
    name: str
    address: str
    port: int
    raw_uri: str
    id: Optional[int] = None
    # "up" | "down" | "unknown"
    status: str = "unknown"
    # consecutive failed checks
    fail_count: int = 0
    last_check: Optional[str] = None
    # last status we actually sent an alert for
    last_alert_status: Optional[str] = None
    active: bool = True
