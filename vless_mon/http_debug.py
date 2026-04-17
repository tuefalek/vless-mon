"""aiohttp TraceConfig that logs every HTTP request and response at DEBUG level."""
from __future__ import annotations

import re
import types
from typing import Any

import aiohttp
from loguru import logger

# Mask Telegram bot token in URLs: /bot123456:ABCdef…/ → /bot[TOKEN]/
_TOKEN_RE = re.compile(r"/bot\d{6,}:[A-Za-z0-9_-]{30,}/")


def make_trace_config() -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(_on_start)
    tc.on_request_chunk_sent.append(_on_req_chunk)
    tc.on_response_chunk_received.append(_on_resp_chunk)
    tc.on_request_end.append(_on_end)
    tc.on_request_exception.append(_on_exc)
    return tc


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

async def _on_start(
    _session: aiohttp.ClientSession,
    ctx: types.SimpleNamespace,
    params: aiohttp.TraceRequestStartParams,
) -> None:
    ctx.req_body: list[bytes] = []
    ctx.resp_body: list[bytes] = []
    url = _mask(str(params.url))
    logger.debug(f"→ {params.method} {url}")


async def _on_req_chunk(
    _session: aiohttp.ClientSession,
    ctx: types.SimpleNamespace,
    params: aiohttp.TraceRequestChunkSentParams,
) -> None:
    if params.chunk:
        ctx.req_body.append(params.chunk)


async def _on_resp_chunk(
    _session: aiohttp.ClientSession,
    ctx: types.SimpleNamespace,
    params: aiohttp.TraceResponseChunkReceivedParams,
) -> None:
    if params.chunk:
        ctx.resp_body.append(params.chunk)


async def _on_end(
    _session: aiohttp.ClientSession,
    ctx: types.SimpleNamespace,
    params: aiohttp.TraceRequestEndParams,
) -> None:
    status = params.response.status
    req_raw = b"".join(ctx.req_body)
    resp_raw = b"".join(ctx.resp_body)

    parts: list[str] = [f"← {status}"]
    if req_raw:
        parts.append(f"  body→ {_decode(req_raw, 400)}")
    if resp_raw:
        parts.append(f"  body← {_decode(resp_raw, 600)}")

    logger.debug("".join(parts))


async def _on_exc(
    _session: aiohttp.ClientSession,
    ctx: types.SimpleNamespace,
    params: aiohttp.TraceRequestExceptionParams,
) -> None:
    url = _mask(str(params.url))
    logger.debug(f"✗ {params.method} {url}  exc={params.exception!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask(url: str) -> str:
    return _TOKEN_RE.sub("/bot[TOKEN]/", url)


def _decode(raw: bytes, limit: int) -> str:
    return raw[:limit].decode("utf-8", errors="replace")
