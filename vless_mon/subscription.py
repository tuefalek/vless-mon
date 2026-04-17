"""VLESS subscription fetcher and URI parser."""
from __future__ import annotations

import base64
import re
from dataclasses import replace
from urllib.parse import unquote, urlparse

import aiohttp
from loguru import logger

from .models import Server

_VLESS_SCHEME = "vless://"
# Characters not safe for use as Mihomo proxy names
_UNSAFE_RE = re.compile(r"[^\w\-\. ()\u4e00-\u9fff]", re.UNICODE)


async def fetch_subscription(
    url: str, session: aiohttp.ClientSession
) -> list[Server]:
    """Download a subscription URL and return parsed VLESS Server objects."""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            raw = await resp.text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.error(f"Subscription fetch failed ({url}): {exc}")
        return []

    lines = _decode_content(raw)
    servers: list[Server] = []
    for line in lines:
        line = line.strip()
        if not line.startswith(_VLESS_SCHEME):
            continue
        srv = _parse_vless_uri(line)
        if srv:
            servers.append(srv)

    servers = _deduplicate(servers)
    logger.info(f"Subscription parsed: {len(servers)} VLESS servers")
    return servers


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_content(content: str) -> list[str]:
    """Try base64 decode; fall back to treating content as plain text."""
    stripped = content.strip()
    try:
        # Pad to make valid base64, then decode
        decoded = base64.b64decode(stripped + "==").decode("utf-8")
        if "://" in decoded:
            return decoded.splitlines()
    except Exception:
        pass
    return stripped.splitlines()


def _parse_vless_uri(uri: str) -> Server | None:
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "vless":
            return None

        uuid = parsed.username
        host = parsed.hostname
        port = parsed.port or 443

        if not (uuid and host):
            logger.debug(f"Incomplete VLESS URI (no uuid/host): {uri[:80]}")
            return None

        # Fragment (#…) is the human-readable label
        fragment = unquote(parsed.fragment or "").strip()
        name = fragment if fragment else f"{host}:{port}"
        name = _sanitize_name(name)

        return Server(name=name, address=host, port=port, raw_uri=uri)
    except Exception as exc:
        logger.debug(f"URI parse error: {exc} — {uri[:80]}")
        return None


def _sanitize_name(name: str) -> str:
    """Replace characters that Mihomo rejects in proxy names."""
    sanitized = _UNSAFE_RE.sub("_", name).strip("_").strip()
    return sanitized or "unnamed"


def _deduplicate(servers: list[Server]) -> list[Server]:
    """Ensure every server has a unique name within the batch."""
    seen: dict[str, int] = {}
    result: list[Server] = []
    for srv in servers:
        if srv.name not in seen:
            seen[srv.name] = 0
            result.append(srv)
        else:
            seen[srv.name] += 1
            result.append(replace(srv, name=f"{srv.name}_{seen[srv.name]}"))
    return result
