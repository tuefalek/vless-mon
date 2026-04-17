"""Telegram bot: long-polling command handler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
from loguru import logger

from .database import Database
from .mihomo import MihomoClient
from .monitor import tcp_ping

_DIVIDER = "━" * 28


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        admin_ids: frozenset[int],
        session: aiohttp.ClientSession,
        db: Database,
        mihomo: MihomoClient,
    ) -> None:
        self._token = token
        self._allowed_chat = allowed_chat_id
        self._admin_ids = admin_ids
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
        if self._admin_ids:
            logger.info(f"Bot polling started — admin IDs: {sorted(self._admin_ids)}")
        else:
            logger.warning("Bot polling started — TELEGRAM_ADMIN_IDS not set, all users in chat can run commands")
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

        sender_id: int = msg.get("from", {}).get("id", 0)
        if self._admin_ids and sender_id not in self._admin_ids:
            logger.debug(f"Ignoring command from non-admin user {sender_id}")
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

        checks = await asyncio.gather(
            *[
                asyncio.gather(
                    self._mihomo.check_delay(s.name),
                    tcp_ping(s.address, s.port),
                )
                for s in servers
            ],
            return_exceptions=True,
        )

        # Each entry: (name, mihomo_ms | None, ping_ms | None)
        rows: list[tuple[str, int | None, int | None]] = []
        for srv, result in zip(servers, checks):
            if isinstance(result, BaseException):
                rows.append((srv.name, None, None))
            else:
                mihomo_ms, ping_ms = result
                mihomo_ms = mihomo_ms if isinstance(mihomo_ms, int) else None
                rows.append((srv.name, mihomo_ms, ping_ms))

        # Sort: up first (by delay), then degraded (by ping), then down
        def _sort_key(r: tuple[str, int | None, int | None]) -> tuple[int, int]:
            _, m, p = r
            if m is not None:
                return (0, m)
            if p is not None:
                return (1, p)
            return (2, 0)

        rows.sort(key=_sort_key)

        up_count = sum(1 for _, m, _ in rows if m is not None)
        degraded_count = sum(1 for _, m, p in rows if m is None and p is not None)
        down_count = sum(1 for _, m, p in rows if m is None and p is None)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines: list[str] = [
            "📊 <b>VLESS Server Status</b>",
            _DIVIDER,
        ]
        for name, mihomo_ms, ping_ms in rows:
            safe = _esc(name)
            ping_str = f"{ping_ms}\u202fms" if ping_ms is not None else "—"
            if mihomo_ms is not None:
                lines.append(
                    f"🟢 <code>{safe}</code>  —  {mihomo_ms}\u202fms | {ping_str}"
                )
            elif ping_ms is not None:
                lines.append(
                    f"🟡 <code>{safe}</code>  —  timeout | {ping_str}"
                )
            else:
                lines.append(f"🔴 <code>{safe}</code>  —  offline")

        summary_parts = [f"🟢 UP: <b>{up_count}</b>"]
        if degraded_count:
            summary_parts.append(f"🟡 DEGRADED: <b>{degraded_count}</b>")
        summary_parts.append(f"🔴 DOWN: <b>{down_count}</b>")

        lines += [
            _DIVIDER,
            "  ".join(summary_parts),
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
