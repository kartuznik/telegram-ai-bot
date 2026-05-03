"""Публичные посты каналов через страницу t.me/s/username (без Bot API)."""
from __future__ import annotations

import errno
import html as html_module
import logging
import random
import re
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_FETCH_HTTP_SEC = 18
FETCH_TIMEOUT_SEC = max(15, _FETCH_HTTP_SEC)

_RETRY_AFTER_TIMEOUT_SEC = 2.0


def _is_timeout_exc(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ETIMEDOUT:
        return True
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        return _is_timeout_exc(exc.reason)
    return False


def _norm_username(raw: str) -> str:
    u = (raw or "").strip().lstrip("@").lower()
    return re.sub(r"[^a-z0-9_]", "", u)


def _strip_tags(s: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html_module.unescape(t)


def fetch_public_channel_posts(username: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """
    Возвращает последние публичные посты с https://t.me/s/{username}.
    Каждый элемент: title, url, content (plain), channel_username.
    """
    un = _norm_username(username)
    if not un:
        return []
    url = f"https://t.me/s/{un}"
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    body: str | None = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            is_to = _is_timeout_exc(exc)
            if attempt == 1 and is_to:
                time.sleep(_RETRY_AFTER_TIMEOUT_SEC)
                continue
            if is_to:
                logger.warning(
                    "tg_feed: не удалось загрузить %s (таймаут после повтора): %s",
                    url,
                    exc,
                )
            else:
                logger.warning("tg_feed: не удалось загрузить %s: %s", url, exc)
            return []

    if body is None:
        return []

    chunks = body.split("tgme_widget_message_wrap")
    out: list[dict[str, Any]] = []
    for ch in chunks[1:]:
        if len(out) >= max(1, min(limit, 12)):
            break
        m_link = re.search(r'href="(https://t\.me/[^"/]+/\d+)"', ch)
        if not m_link:
            continue
        post_url = m_link.group(1).split("?")[0]
        m_text = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>',
            ch,
            re.I,
        )
        raw_txt = m_text.group(1) if m_text else ""
        text = _strip_tags(raw_txt).strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) > 1200:
            text = text[:1197] + "…"
        title = (text[:80] + "…") if len(text) > 80 else (text or "Пост из Telegram")
        out.append(
            {
                "title": title,
                "url": post_url,
                "content": text,
                "channel_username": un,
            }
        )
    if out:
        logger.info("tg_feed: %s постов с @%s", len(out), un)
    else:
        logger.debug("tg_feed: пусто для @%s (вёрстка или нет публичных постов)", un)
    return out


def fetch_many_channels(usernames: list[str], *, per_channel: int = 3) -> list[dict[str, Any]]:
    """Собирает посты с нескольких каналов (по per_channel с каждого). Порядок каналов каждый раз случайный."""
    order = [_norm_username(u) for u in usernames]
    order = [u for u in order if u]
    random.shuffle(order)
    n = len(order)
    if n:
        preview_n = min(n, 8)
        shown = ",".join(f"@{x}" for x in order[:preview_n])
        if n > preview_n:
            tail = f" … (всего {n} каналов, показаны первые {preview_n})"
        else:
            tail = ""
        logger.info(
            "tg_feed: fetch_many_channels total_channels=%s, shuffle_order: %s%s",
            n,
            shown,
            tail,
        )
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for nu in order:
        posts = fetch_public_channel_posts(nu, limit=per_channel)
        for p in posts:
            u = (p.get("url") or "").strip().lower()
            if not u or u in seen:
                continue
            seen.add(u)
            merged.append(p)
    return merged
