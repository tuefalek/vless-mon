from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from loguru import logger

from .models import Server

_NOW = lambda: datetime.now(timezone.utc).isoformat()

_DDL = """
CREATE TABLE IF NOT EXISTS servers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    UNIQUE NOT NULL,
    address          TEXT    NOT NULL,
    port             INTEGER NOT NULL,
    raw_uri          TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'unknown',
    fail_count       INTEGER NOT NULL DEFAULT 0,
    last_check       TEXT,
    last_alert_status TEXT,
    ping_ms          INTEGER,
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS check_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT    NOT NULL,
    checked_at  TEXT    NOT NULL,
    mihomo_ms   INTEGER,
    ping_ms     INTEGER,
    status      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_check_results_server_time
    ON check_results (server_name, checked_at);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_DDL)
        await self._conn.commit()
        await self._migrate()
        logger.info(f"Database connected: {self._path}")

    async def _migrate(self) -> None:
        async with self._conn.execute("PRAGMA table_info(servers)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "ping_ms" not in cols:
            await self._conn.execute(
                "ALTER TABLE servers ADD COLUMN ping_ms INTEGER"
            )
            await self._conn.commit()
            logger.info("DB migration: added servers.ping_ms column")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_active_servers(self) -> list[Server]:
        async with self._conn.execute(
            "SELECT * FROM servers WHERE active = 1 ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_server(r) for r in rows]

    async def server_exists(self, name: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM servers WHERE name = ?", (name,)
        ) as cur:
            return await cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_server(self, server: Server) -> bool:
        """Insert new server or reactivate+update existing one.

        Returns True when a brand-new row was inserted.
        """
        async with self._conn.execute(
            "SELECT id, active FROM servers WHERE name = ?", (server.name,)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            await self._conn.execute(
                "INSERT INTO servers (name, address, port, raw_uri) VALUES (?, ?, ?, ?)",
                (server.name, server.address, server.port, server.raw_uri),
            )
            await self._conn.commit()
            logger.debug(f"New server added to DB: {server.name}")
            return True

        # Server already exists — update mutable fields and ensure active
        await self._conn.execute(
            "UPDATE servers SET address = ?, port = ?, raw_uri = ?, active = 1 WHERE name = ?",
            (server.address, server.port, server.raw_uri, server.name),
        )
        await self._conn.commit()
        return False

    async def update_server_status(
        self,
        name: str,
        status: str,
        fail_count: int,
        last_alert_status: Optional[str],
        ping_ms: Optional[int] = None,
    ) -> None:
        await self._conn.execute(
            """UPDATE servers
               SET status = ?, fail_count = ?, last_check = ?,
                   last_alert_status = ?, ping_ms = ?
               WHERE name = ?""",
            (status, fail_count, _NOW(), last_alert_status, ping_ms, name),
        )
        await self._conn.commit()

    async def insert_check_result(
        self,
        server_name: str,
        mihomo_ms: Optional[int],
        ping_ms: Optional[int],
        status: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO check_results (server_name, checked_at, mihomo_ms, ping_ms, status)
               VALUES (?, ?, ?, ?, ?)""",
            (server_name, _NOW(), mihomo_ms, ping_ms, status),
        )
        await self._conn.commit()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _row_to_server(row: aiosqlite.Row) -> Server:
    return Server(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        port=row["port"],
        raw_uri=row["raw_uri"],
        status=row["status"],
        fail_count=row["fail_count"],
        last_check=row["last_check"],
        last_alert_status=row["last_alert_status"],
        ping_ms=row["ping_ms"],
        active=bool(row["active"]),
    )
