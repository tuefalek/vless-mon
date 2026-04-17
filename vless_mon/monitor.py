"""Core monitoring logic: check all active servers and fire alerts on state change."""
from __future__ import annotations

import asyncio

from loguru import logger

from .config import Config
from .database import Database
from .mihomo import MihomoClient
from .models import Server
from .telegram import TelegramNotifier


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
        delay = await self._mihomo.check_delay(server.name)
        if delay is not None:
            await self._on_success(server, delay)
        else:
            await self._on_failure(server)

    async def _on_success(self, server: Server, delay: int) -> None:
        prev_alert = server.last_alert_status

        # If we previously alerted DOWN, this is a recovery → alert UP
        new_alert = "up" if prev_alert == "down" else prev_alert

        await self._db.update_server_status(
            name=server.name,
            status="up",
            fail_count=0,
            last_alert_status=new_alert,
        )

        if prev_alert == "down":
            logger.info(f"[RECOVERED] {server.name}  delay={delay}ms")
            await self._tg.alert_up(server.name, delay)
        else:
            logger.debug(f"[OK] {server.name}  delay={delay}ms")

    async def _on_failure(self, server: Server) -> None:
        new_fail_count = server.fail_count + 1
        threshold = self._cfg.fail_threshold

        # Alert only on the exact crossing of the threshold and only when we
        # have not already alerted for the current outage.
        should_alert = (
            new_fail_count >= threshold
            and server.last_alert_status != "down"
        )

        new_alert = "down" if should_alert else server.last_alert_status

        await self._db.update_server_status(
            name=server.name,
            status="down",
            fail_count=new_fail_count,
            last_alert_status=new_alert,
        )

        if should_alert:
            logger.warning(
                f"[DOWN] {server.name} — {new_fail_count} consecutive failures"
            )
            await self._tg.alert_down(server.name, new_fail_count)
        else:
            logger.debug(
                f"[FAIL {new_fail_count}/{threshold}] {server.name}"
            )
