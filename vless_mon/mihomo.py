"""Mihomo (Clash.Meta) REST API client and proxy-provider config manager."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import aiohttp
import yaml
from loguru import logger

from .config import Config
from .models import Server


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class MihomoClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._base = config.mihomo_api_url.rstrip("/")
        self._session = session
        self._timeout_ms = config.check_timeout
        self._check_url = config.check_url
        self._headers: dict[str, str] = {}
        if config.mihomo_secret:
            self._headers["Authorization"] = f"Bearer {config.mihomo_secret}"

    async def check_delay(self, node_name: str) -> Optional[int]:
        """Return round-trip delay (ms) if the node is reachable, else None."""
        encoded = quote(node_name, safe="")
        url = f"{self._base}/proxies/{encoded}/delay"
        params = {"timeout": self._timeout_ms, "url": self._check_url}
        # Give aiohttp a bit more headroom than Mihomo's own timeout
        total = self._timeout_ms / 1000 + 3
        try:
            async with self._session.get(
                url,
                params=params,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=total),
            ) as resp:
                data = await resp.json(content_type=None)
                delay = data.get("delay")
                if delay is None:
                    logger.debug(
                        f"check_delay({node_name!r}): HTTP {resp.status} — {data}"
                    )
                return delay or None
        except Exception as exc:
            logger.debug(f"check_delay({node_name!r}): {exc}")
            return None

    async def reload_provider(self, provider_name: str) -> None:
        """Force Mihomo to re-read a proxy-provider file."""
        url = f"{self._base}/providers/proxies/{provider_name}"
        try:
            async with self._session.put(
                url,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Proxy provider '{provider_name}' reloaded")
                else:
                    body = await resp.text()
                    logger.warning(
                        f"reload_provider returned {resp.status}: {body[:200]}"
                    )
        except Exception as exc:
            logger.error(f"reload_provider failed: {exc}")

    async def reload_config(self, config_path: str, *, force: bool = True) -> None:
        """Reload the entire Mihomo configuration (PUT /configs)."""
        url = f"{self._base}/configs"
        payload: dict[str, Any] = {"path": config_path, "force": force}
        try:
            async with self._session.put(
                url,
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 204):
                    logger.info("Mihomo config reloaded via PUT /configs")
                else:
                    body = await resp.text()
                    logger.warning(
                        f"reload_config returned {resp.status}: {body[:200]}"
                    )
        except Exception as exc:
            logger.error(f"reload_config failed: {exc}")


# ---------------------------------------------------------------------------
# Proxy-provider YAML manager
# ---------------------------------------------------------------------------

class MihomoConfigManager:
    """Maintains the proxy-provider YAML that Mihomo reads for VLESS proxies."""

    def __init__(self, config: Config) -> None:
        self.provider_path = Path(config.mihomo_provider_path)
        self.provider_name = config.mihomo_provider_name

    def write_proxies(self, servers: list[Server]) -> bool:
        """Serialize *servers* to the provider YAML.

        Returns True when the file was actually changed (content differs or
        file did not exist), so callers can decide whether to reload Mihomo.
        """
        proxies = []
        for s in servers:
            p = _vless_to_mihomo(s.raw_uri, s.name)
            if p:
                proxies.append(p)
            else:
                logger.warning(f"Could not build Mihomo proxy dict for '{s.name}', skipping")

        new_content = yaml.dump(
            {"proxies": proxies},
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )

        if self.provider_path.exists():
            if self.provider_path.read_text(encoding="utf-8") == new_content:
                return False

        self.provider_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider_path.write_text(new_content, encoding="utf-8")
        logger.info(
            f"Proxy provider updated: {self.provider_path} ({len(proxies)} proxies)"
        )
        return True


# ---------------------------------------------------------------------------
# VLESS URI → Mihomo proxy dict
# ---------------------------------------------------------------------------

def _vless_to_mihomo(uri: str, name: str) -> Optional[dict[str, Any]]:
    try:
        return _parse(uri, name)
    except Exception as exc:
        logger.debug(f"_vless_to_mihomo({name!r}): {exc}")
        return None


def _parse(uri: str, name: str) -> dict[str, Any]:
    parsed = urlparse(uri)
    if parsed.scheme != "vless":
        raise ValueError("not a vless URI")

    uuid = parsed.username
    host = parsed.hostname
    port = parsed.port
    if not (uuid and host and port):
        raise ValueError("missing uuid/host/port")

    qs = parse_qs(parsed.query)

    def p(key: str, default: str = "") -> str:
        return qs.get(key, [default])[0]

    security = p("security", "none")
    network = p("type", "tcp")

    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": uuid,
        "udp": True,
    }

    # ── TLS / Reality ────────────────────────────────────────────────────
    if security in ("tls", "reality"):
        proxy["tls"] = True
        sni = p("sni")
        if sni:
            proxy["servername"] = sni
        fp = p("fp")
        if fp:
            proxy["client-fingerprint"] = fp
        alpn = p("alpn")
        if alpn:
            proxy["alpn"] = [a for a in alpn.split(",") if a]

    if security == "reality":
        proxy["reality-opts"] = {
            "public-key": p("pbk"),
            "short-id": p("sid", ""),
        }

    # ── Flow (e.g. xtls-rprx-vision) ────────────────────────────────────
    flow = p("flow")
    if flow:
        proxy["flow"] = flow

    # ── Transport ────────────────────────────────────────────────────────
    if network == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {
            "path": unquote(p("path", "/")),
            "headers": {"Host": p("host", host)},
        }
    elif network == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": p("serviceName", "")}
    elif network == "h2":
        proxy["network"] = "h2"
        proxy["h2-opts"] = {
            "path": unquote(p("path", "/")),
            "host": [p("host", host)],
        }
    elif network == "httpupgrade":
        proxy["network"] = "httpupgrade"
        proxy["httpupgrade-opts"] = {
            "path": unquote(p("path", "/")),
            "host": p("host", host),
        }
    # tcp / kcp / quic — no extra opts needed

    return proxy
