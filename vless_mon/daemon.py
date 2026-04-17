"""Main daemon: wires up all components, runs async loops, handles shutdown."""
from __future__ import annotations

import asyncio

import aiohttp
from aiohttp_socks import ProxyConnector
from loguru import logger

from .bot import TelegramBot
from .config import Config
from .database import Database
from .http_debug import make_trace_config
from .mihomo import MihomoClient, MihomoConfigManager
from .monitor import Monitor
from .subscription import fetch_subscription
from .telegram import TelegramNotifier


class VlessMonDaemon:
    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._db = Database(config.db_path)
        self._session: aiohttp.ClientSession | None = None      # Mihomo + subscription
        self._tg_session: aiohttp.ClientSession | None = None   # Telegram (optionally via SOCKS)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("VLESS monitor daemon starting")

        traces = [make_trace_config()] if self._cfg.debug else []
        self._session = aiohttp.ClientSession(trace_configs=traces)
        self._tg_session = self._make_tg_session(traces)
        mihomo = MihomoClient(self._cfg, self._session)
        telegram = TelegramNotifier(self._cfg, self._tg_session)
        cfg_mgr = MihomoConfigManager(self._cfg)
        monitor = Monitor(self._cfg, self._db, mihomo, telegram)
        bot = TelegramBot(
            token=self._cfg.telegram_bot_token,
            allowed_chat_id=str(self._cfg.telegram_chat_id),
            admin_ids=self._cfg.telegram_admin_ids,
            session=self._tg_session,
            db=self._db,
            mihomo=mihomo,
        )

        await self._db.connect()

        # Perform an initial subscription sync before entering the loops so
        # Mihomo has the full proxy list from the very first check cycle.
        await self._sync_subscription(cfg_mgr, mihomo)

        # Always force-reload the provider on startup so Mihomo has our proxy
        # list even after it was restarted independently of this daemon.
        if cfg_mgr.provider_path.exists():
            await mihomo.reload_provider(self._cfg.mihomo_provider_name)
        else:
            logger.warning(
                f"Provider file not found: {cfg_mgr.provider_path}. "
                "No subscription fetched yet or wrong MIHOMO_PROVIDER_PATH."
            )

        await self._startup_diagnostics(mihomo)

        tasks = [
            asyncio.create_task(
                self._monitor_loop(monitor), name="monitor-loop"
            ),
            asyncio.create_task(
                self._subscription_loop(cfg_mgr, mihomo), name="subscription-loop"
            ),
            asyncio.create_task(
                bot.run(), name="bot-polling"
            ),
        ]

        await self._stop.wait()
        logger.info("Shutdown event received — cancelling tasks")

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self._shutdown()

    def _make_tg_session(self, traces: list) -> aiohttp.ClientSession:
        proxy_url = self._cfg.telegram_socks_proxy
        if proxy_url:
            connector = ProxyConnector.from_url(proxy_url, rdns=True)
            logger.info(f"Telegram session: SOCKS proxy {proxy_url}")
        else:
            connector = aiohttp.TCPConnector()
        return aiohttp.ClientSession(connector=connector, trace_configs=traces)

    def request_stop(self) -> None:
        """Called from signal handlers to trigger graceful shutdown."""
        logger.info("Stop requested via signal")
        self._stop.set()

    # ------------------------------------------------------------------
    # Startup diagnostics
    # ------------------------------------------------------------------

    async def _startup_diagnostics(self, mihomo: MihomoClient) -> None:
        db_servers = await self._db.get_active_servers()
        db_names = {s.name for s in db_servers}
        mihomo_names = await mihomo.list_proxy_names()

        matched = db_names & mihomo_names
        missing = db_names - mihomo_names
        extra = mihomo_names - db_names

        logger.info(
            f"Diagnostics: DB={len(db_names)} | Mihomo={len(mihomo_names)} | "
            f"matched={len(matched)} | missing_in_mihomo={len(missing)} | "
            f"extra_in_mihomo={len(extra)}"
        )
        if missing:
            samples = sorted(missing)[:5]
            dots = "…" if len(missing) > 5 else ""
            logger.error(
                f"These {len(missing)} DB proxies are UNKNOWN to Mihomo: "
                f"{samples}{dots}"
            )
            logger.error(
                f"Fix: add proxy-providers.{self._cfg.mihomo_provider_name} to "
                f"Mihomo config.yaml pointing to {self._cfg.mihomo_provider_path}, "
                "then reload Mihomo."
            )
        if extra:
            samples = sorted(extra)[:5]
            dots = "…" if len(extra) > 5 else ""
            logger.info(
                f"Mihomo has {len(extra)} proxies not in our DB (from other providers): "
                f"{samples}{dots}"
            )
        if matched:
            logger.info(f"Ready to monitor {len(matched)} matched proxies.")

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
        if self._tg_session and not self._tg_session.closed:
            await self._tg_session.close()
        logger.info("VLESS monitor stopped")
