"""Telegram bot: long-polling command handler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
from loguru import logger

from .database import Database
from .mihomo import MihomoClient

_DIVIDER = "━" * 28


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        session: aiohttp.ClientSession,
        db: Database,
        mihomo: MihomoClient,
    ) -> None:
        self._token = token
        self._allowed_chat = allowed_chat_id
        self._session = session
        self._db = db
        self._mihomo = mihomo
        self._api = f"https://api.telegram.org/bot{token}"
        self._offset = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if not self._token:
            logger.info("Bot polling skipped (TELEGRAM_BOT_TOKEN not set)")
            return
        logger.info("Telegram bot polling started")
        while True:
            try:
                updates = await self._get_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Bot getUpdates error: {exc}")
                await asyncio.sleep(5)
                continue

            for update in updates:
                try:
                    await self._dispatch(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"Bot dispatch error: {exc}")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _get_updates(self) -> list[dict[str, Any]]:
        params = {
            "offset": self._offset,
            "timeout": 25,
            "allowed_updates": ["message"],
        }
        try:
            async with self._session.get(
                f"{self._api}/getUpdates",
                params=params,
                # slightly longer than the long-poll timeout
                timeout=aiohttp.ClientTimeout(total=35),
            ) as resp:
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return []

        if not data.get("ok"):
            logger.warning(f"getUpdates not ok: {data}")
            await asyncio.sleep(5)
            return []

        updates: list[dict] = data["result"]
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not msg:
            return

        chat_id = str(msg.get("chat", {}).get("id", ""))
        if self._allowed_chat and chat_id != self._allowed_chat:
            logger.debug(f"Ignoring message from unauthorized chat {chat_id}")
            return

        text: str = msg.get("text", "")
        # Accept "/check" and "/check@botname"
        if text.split("@")[0].strip() == "/check":
            await self._cmd_check(chat_id, msg.get("message_id"))

    # ------------------------------------------------------------------
    # /check command
    # ------------------------------------------------------------------

    async def _cmd_check(self, chat_id: str, reply_to: int | None) -> None:
        servers = await self._db.get_active_servers()
        if not servers:
            await self._send(chat_id, "❌ No active servers in the database.", reply_to)
            return

        await self._send(
            chat_id,
            f"⏳ Checking <b>{len(servers)}</b> servers…",
            reply_to,
        )

        delays = await asyncio.gather(
            *[self._mihomo.check_delay(s.name) for s in servers],
            return_exceptions=True,
        )

        rows: list[tuple[str, int | None]] = []
        for srv, result in zip(servers, delays):
            d = result if isinstance(result, int) else None
            rows.append((srv.name, d))

        # Sort: UP servers by delay ascending, then DOWN
        rows.sort(key=lambda r: (r[1] is None, r[1] or 0))

        up_count = sum(1 for _, d in rows if d is not None)
        down_count = len(rows) - up_count

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines: list[str] = [
            f"📊 <b>VLESS Server Status</b>",
            _DIVIDER,
        ]
        for name, delay in rows:
            safe = _esc(name)
            if delay is not None:
                lines.append(f"🟢 <code>{safe}</code>  —  {delay}\u202fms")
            else:
                lines.append(f"🔴 <code>{safe}</code>  —  timeout")
        lines += [
            _DIVIDER,
            f"✅ UP: <b>{up_count}</b>    ❌ DOWN: <b>{down_count}</b>",
            f"🕐 {ts}",
        ]

        report = "\n".join(lines)
        for chunk in _split(report):
            await self._send(chat_id, chunk)

    # ------------------------------------------------------------------
    # Send helper
    # ------------------------------------------------------------------

    async def _send(
        self,
        chat_id: str,
        text: str,
        reply_to: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        try:
            async with self._session.post(
                f"{self._api}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"sendMessage {resp.status}: {body[:200]}")
        except Exception as exc:
            logger.error(f"Bot send failed: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split(text: str, limit: int = 4096) -> list[str]:
    """Split a message that exceeds Telegram's character limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
