"""Общая сборка SOCKS5 URL из Config (Telegram, OpenAI httpx и т.д.)."""

from __future__ import annotations

from urllib.parse import quote

from app.config import Config


def socks5_proxy_url_from_config(config: Config) -> str | None:
    if not (config.proxy_host or "").strip():
        return None
    host = config.proxy_host.strip()
    port = (config.proxy_port or "1080").strip()
    user = (config.proxy_username or "").strip() or None
    password = (config.proxy_password or "").strip() or None
    if user and password:
        u = quote(user, safe="")
        p = quote(password, safe="")
        return f"socks5://{u}:{p}@{host}:{port}"
    return f"socks5://{host}:{port}"
