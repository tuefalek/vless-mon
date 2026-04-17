"""Main daemon: wires up all components, runs async loops, handles shutdown."""
from __future__ import annotations

import asyncio

import aiohttp
from loguru import logger

from .config import Config
from .database import Database
from .mihomo import MihomoClient, MihomoConfigManager
from .monitor import Monitor
from .subscription import fetch_subscription
from .telegram import TelegramNotifier


class VlessMonDaemon:
    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._db = Database(config.db_path)
        self._session: aiohttp.ClientSession | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("VLESS monitor daemon starting")

        self._session = aiohttp.ClientSession()
        mihomo = MihomoClient(self._cfg, self._session)
        telegram = TelegramNotifier(self._cfg, self._session)
        cfg_mgr = MihomoConfigManager(self._cfg)
        monitor = Monitor(self._cfg, self._db, mihomo, telegram)

        await self._db.connect()

        # Perform an initial subscription sync before entering the loops so
        # Mihomo has the full proxy list from the very first check cycle.
        await self._sync_subscription(cfg_mgr, mihomo)

        tasks = [
            asyncio.create_task(
                self._monitor_loop(monitor), name="monitor-loop"
            ),
            asyncio.create_task(
                self._subscription_loop(cfg_mgr, mihomo), name="subscription-loop"
            ),
        ]

        await self._stop.wait()
        logger.info("Shutdown event received — cancelling tasks")

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self._shutdown()

    def request_stop(self) -> None:
        """Called from signal handlers to trigger graceful shutdown."""
        logger.info("Stop requested via signal")
        self._stop.set()

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _monitor_loop(self, monitor: Monitor) -> None:
        interval = self._cfg.check_interval
        logger.info(f"Monitor loop started (interval={interval}s)")
        while True:
            try:
                await monitor.check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"Monitor loop error: {exc}")
            await asyncio.sleep(interval)

    async def _subscription_loop(
        self, cfg_mgr: MihomoConfigManager, mihomo: MihomoClient
    ) -> None:
        interval = self._cfg.subscription_interval
        logger.info(f"Subscription loop started (interval={interval}s)")
        while True:
            await asyncio.sleep(interval)
            try:
                await self._sync_subscription(cfg_mgr, mihomo)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"Subscription loop error: {exc}")

    # ------------------------------------------------------------------
    # Subscription sync
    # ------------------------------------------------------------------

    async def _sync_subscription(
        self, cfg_mgr: MihomoConfigManager, mihomo: MihomoClient
    ) -> None:
        if not self._cfg.subscription_url:
            logger.debug("SUBSCRIPTION_URL not configured — skipping sync")
            return

        logger.info("Syncing subscription…")
        servers = await fetch_subscription(self._cfg.subscription_url, self._session)
        if not servers:
            logger.warning("Subscription returned no VLESS servers")
            return

        new_count = 0
        for srv in servers:
            is_new = await self._db.upsert_server(srv)
            if is_new:
                new_count += 1

        if new_count > 0 or not cfg_mgr.provider_path.exists():
            logger.info(f"Subscription sync: {new_count} new server(s) discovered")
            all_active = await self._db.get_active_servers()
            changed = cfg_mgr.write_proxies(all_active)
            if changed:
                # Reload the proxy-provider so Mihomo exposes new nodes for
                # /proxies/{name}/delay without a full restart.
                await mihomo.reload_provider(self._cfg.mihomo_provider_name)
        else:
            logger.info("Subscription sync: no new servers")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        logger.info("Releasing resources…")
        await self._db.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("VLESS monitor stopped")
