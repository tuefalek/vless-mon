"""Entry point: python -m vless_mon"""
from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from .config import Config
from .daemon import VlessMonDaemon


def _setup_logging(level: str, log_file: str) -> None:
    fmt = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
        "{name}:{function}:{line} — {message}"
    )
    logger.remove()
    logger.add(sys.stderr, level=level, format=fmt, colorize=True, enqueue=True)
    if log_file:
        logger.add(
            log_file,
            level=level,
            format=fmt,
            rotation="10 MB",
            retention="14 days",
            compression="gz",
            enqueue=True,
        )


async def _async_main() -> None:
    config = Config.from_env()
    level = "DEBUG" if config.debug else config.log_level
    _setup_logging(level, config.log_file)
    if config.debug:
        logger.warning("DEBUG mode enabled — all HTTP requests/responses will be logged")

    daemon = VlessMonDaemon(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.request_stop)

    await daemon.run()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass  # request_stop() already triggered; clean shutdown via daemon
    sys.exit(0)


if __name__ == "__main__":
    main()
