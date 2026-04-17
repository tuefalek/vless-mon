from __future__ import annotations

from typing import Optional

import aiohttp
from loguru import logger

from .config import Config


class TelegramNotifier:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id
        self._session = session
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            logger.warning("Telegram notifier disabled (token/chat_id not set)")

    async def send(self, text: str) -> None:
        if not self._enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with self._session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Telegram API {resp.status}: {body[:200]}")
        except Exception as exc:
            logger.error(f"Telegram send failed: {exc}")

    async def alert_down(self, server_name: str, fail_count: int) -> None:
        await self.send(
            f"\U0001f534 <b>VLESS DOWN</b>\n"
            f"Server: <code>{server_name}</code>\n"
            f"Host unreachable — failed checks: {fail_count}"
        )

    async def alert_degraded(
        self, server_name: str, fail_count: int, ping_ms: Optional[int]
    ) -> None:
        ping_str = f"{ping_ms}\u202fms" if ping_ms is not None else "—"
        await self.send(
            f"\U0001f7e1 <b>VLESS DEGRADED</b>\n"
            f"Server: <code>{server_name}</code>\n"
            f"Mihomo failed ({fail_count} checks) | ping: {ping_str}"
        )

    async def alert_up(
        self, server_name: str, delay_ms: int, ping_ms: Optional[int]
    ) -> None:
        ping_str = f"{ping_ms}\u202fms" if ping_ms is not None else "—"
        await self.send(
            f"\U0001f7e2 <b>VLESS UP</b>\n"
            f"Server: <code>{server_name}</code>\n"
            f"Delay: {delay_ms}\u202fms | ping: {ping_str}"
        )
