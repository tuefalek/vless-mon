"""Core monitoring logic: check all active servers and fire alerts on state change."""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from .config import Config
from .database import Database
from .mihomo import MihomoClient
from .models import Server
from .telegram import TelegramNotifier

_TCP_PING_TIMEOUT = 3.0  # seconds


async def tcp_ping(host: str, port: int, timeout: float = _TCP_PING_TIMEOUT) -> int | None:
    """Return TCP connect latency in ms, or None on failure."""
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        elapsed = int((time.monotonic() - start) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return elapsed
    except Exception:
        return None


class Monitor:
    def __init__(
        self,
        config: Config,
        db: Database,
        mihomo: MihomoClient,
        telegram: TelegramNotifier,
    ) -> None:
        self._cfg = config
        self._db = db
        self._mihomo = mihomo
        self._tg = telegram

    async def check_all(self) -> None:
        servers = await self._db.get_active_servers()
        if not servers:
            logger.debug("No active servers in DB — nothing to check")
            return

        results = await asyncio.gather(
            *[self._check_one(s) for s in servers],
            return_exceptions=True,
        )
        for srv, exc in zip(servers, results):
            if isinstance(exc, BaseException):
                logger.error(f"Unhandled error checking '{srv.name}': {exc}")

    # ------------------------------------------------------------------
    # Per-server logic
    # ------------------------------------------------------------------

    async def _check_one(self, server: Server) -> None:
        mihomo_delay, ping_ms = await asyncio.gather(
            self._mihomo.check_delay(server.name),
            tcp_ping(server.address, server.port),
        )

        if mihomo_delay is not None:
            status = "up"
        elif ping_ms is not None:
            status = "degraded"
        else:
            status = "down"

        await self._db.insert_check_result(
            server_name=server.name,
            mihomo_ms=mihomo_delay,
            ping_ms=ping_ms,
            status=status,
        )

        if mihomo_delay is not None:
            await self._on_success(server, mihomo_delay, ping_ms)
        else:
            await self._on_failure(server, ping_ms, status)

    async def _on_success(self, server: Server, delay: int, ping_ms: int | None) -> None:
        prev_alert = server.last_alert_status

        new_alert = "up" if prev_alert in ("down", "degraded") else prev_alert

        await self._db.update_server_status(
            name=server.name,
            status="up",
            fail_count=0,
            last_alert_status=new_alert,
            ping_ms=ping_ms,
        )

        if prev_alert in ("down", "degraded"):
            logger.info(f"[RECOVERED] {server.name}  delay={delay}ms  ping={ping_ms}ms")
            await self._tg.alert_up(server.name, delay, ping_ms)
        else:
            logger.debug(f"[OK] {server.name}  delay={delay}ms  ping={ping_ms}ms")

    async def _on_failure(self, server: Server, ping_ms: int | None, status: str) -> None:
        new_fail_count = server.fail_count + 1
        threshold = self._cfg.fail_threshold

        should_alert = (
            new_fail_count >= threshold
            and server.last_alert_status not in ("down", "degraded")
        )
        # Also alert when status worsens from degraded → down
        if (
            new_fail_count >= threshold
            and status == "down"
            and server.last_alert_status == "degraded"
        ):
            should_alert = True

        new_alert = status if should_alert else server.last_alert_status

        await self._db.update_server_status(
            name=server.name,
            status=status,
            fail_count=new_fail_count,
            last_alert_status=new_alert,
            ping_ms=ping_ms,
        )

        if should_alert:
            if status == "down":
                logger.warning(
                    f"[DOWN] {server.name} — {new_fail_count} consecutive failures, no ping"
                )
                await self._tg.alert_down(server.name, new_fail_count)
            else:
                logger.warning(
                    f"[DEGRADED] {server.name} — {new_fail_count} consecutive Mihomo failures, ping={ping_ms}ms"
                )
                await self._tg.alert_degraded(server.name, new_fail_count, ping_ms)
        else:
            logger.debug(
                f"[FAIL {new_fail_count}/{threshold}] {server.name}  status={status}  ping={ping_ms}ms"
            )
