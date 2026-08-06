"""Citation / sources layer (Watchdog canon adapted for Kuzya chat path).

- Relevance filter on title+snippet vs query terms
- HTML numbered clickable sources + URL inline buttons
- Freshness honesty note for актуальность markers
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

FRESHNESS_MARKERS = (
    "сегодня",
    "сейчас",
    "последн",
    "текущ",
    "свеж",
    "live",
    "breaking",
    "today",
    "latest",
    "current",
    "новост",
)

_LEADING_QUESTION_WORDS = frozenset(
    {
        "что",
        "как",
        "где",
        "когда",
        "куда",
        "зачем",
        "почему",
        "какой",
        "какая",
        "какие",
        "какое",
        "who",
        "what",
        "where",
        "when",
        "why",
        "how",
        "which",
    }
)

_STOPWORDS = frozenset(
    {
        "и",
        "в",
        "во",
        "на",
        "по",
        "про",
        "о",
        "об",
        "от",
        "для",
        "из",
        "к",
        "ко",
        "с",
        "со",
        "у",
        "а",
        "но",
        "же",
        "бы",
        "то",
        "это",
        "этот",
        "эта",
        "эти",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "about",
        "is",
        "are",
        "be",
    }
) | _LEADING_QUESTION_WORDS

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)
_SNIPPET_LIMIT = 700

_DATE_PATTERNS = (
    re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b"),
    re.compile(
        r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)\s+(20\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
        re.IGNORECASE,
    ),
)

_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_EN_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


class SourceItem(TypedDict):
    title: str
    url: str
    snippet: str
    published_at: str


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def significant_terms(text: str) -> set[str]:
    """Content-bearing tokens: drop stop/question words and ultra-short noise."""
    terms: set[str] = set()
    for tok in _tokenize(text):
        if tok in _STOPWORDS:
            continue
        if len(tok) < 2:
            continue
        if len(tok) == 2 and not tok.isalpha():
            continue
        terms.add(tok)
    return terms


def topic_needs_freshness(topic: str) -> bool:
    text = (topic or "").strip().lower().replace("ё", "е")
    return any(marker in text for marker in FRESHNESS_MARKERS)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_source_date(text: str, *, today: date | None = None) -> date | None:
    blob = (text or "").strip()
    if not blob:
        return None
    m = _DATE_PATTERNS[0].search(blob)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_PATTERNS[1].search(blob)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        for day, month in ((d, mo), (mo, d)):
            try:
                return date(y, month, day)
            except ValueError:
                continue
    m = _DATE_PATTERNS[2].search(blob)
    if m:
        day = int(m.group(1))
        month = _RU_MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                pass
    m = _DATE_PATTERNS[3].search(blob)
    if m:
        month = _EN_MONTHS.get(m.group(1).lower())
        day = int(m.group(2))
        year = int(m.group(3))
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                pass
    # ISO datetime prefix
    if len(blob) >= 10 and blob[4] == "-" and blob[7] == "-":
        try:
            return date.fromisoformat(blob[:10])
        except ValueError:
            pass
    return None


def source_item_date(item: dict[str, Any], *, today: date | None = None) -> date | None:
    for key in ("published_at", "published_date", "published"):
        raw = str(item.get(key) or "").strip()
        if raw:
            d = parse_source_date(raw, today=today)
            if d:
                return d
    snippet = str(item.get("snippet") or item.get("content") or "")
    return parse_source_date(snippet, today=today)


def normalize_source_items(raw: list[Any] | None) -> list[SourceItem]:
    items: list[SourceItem] = []
    seen: set[str] = set()
    for entry in raw or []:
        title = ""
        url = ""
        snippet = ""
        published_at = ""
        if isinstance(entry, str):
            url = entry.strip()
            title = url
        elif isinstance(entry, dict):
            url = str(entry.get("url", "")).strip()
            title = str(entry.get("title") or url).strip() or url
            snippet = str(entry.get("snippet") or entry.get("content") or "").strip()
            published_at = str(
                entry.get("published_at")
                or entry.get("published_date")
                or entry.get("published")
                or ""
            ).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if len(snippet) > _SNIPPET_LIMIT:
            snippet = snippet[:_SNIPPET_LIMIT].rstrip() + "…"
        items.append(
            {
                "title": title[:120],
                "url": url,
                "snippet": snippet,
                "published_at": published_at[:64],
            }
        )
    return items


def filter_relevant_sources(
    query: str,
    sources: list[SourceItem] | list[dict[str, Any]] | None,
) -> list[SourceItem]:
    """Drop sources whose title+snippet have no significant-term overlap with query."""
    query_terms = significant_terms(query)
    normalized = normalize_source_items(list(sources or []))
    if not query_terms:
        return normalized
    kept: list[SourceItem] = []
    for item in normalized:
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        title_terms = significant_terms(f"{title} {snippet}")
        overlap = query_terms & title_terms
        if not overlap:
            logger.info(
                "source relevance filter: drop title=%r url=%r reason=no_term_overlap query_terms=%s",
                title[:120],
                str(item.get("url") or "")[:180],
                sorted(query_terms)[:12],
            )
            continue
        kept.append(item)
    return kept


def freshness_honesty_note(
    topic: str,
    sources: list[dict[str, Any]] | None,
    *,
    days: int = 2,
    today: date | None = None,
) -> str:
    """
    Writer/user-facing note when freshness markers are present and data is stale
    or undated.
    """
    if not topic_needs_freshness(topic):
        return ""
    window = max(1, int(days))
    ref = today or utc_today()
    cutoff = ref - timedelta(days=window)
    dates = [d for d in (source_item_date(s, today=ref) for s in (sources or [])) if d]
    if not dates:
        return (
            f"⚠️ Запрос про актуальные данные, но датированных источников нет. "
            f"Ниже — по доступным материалам; не выдавай устаревшее за новости на {ref.isoformat()}."
        )
    newest = max(dates)
    if newest >= cutoff:
        return ""
    return (
        f"⚠️ Запрос про «сегодня» ({ref.isoformat()}), а самые свежие данные в источниках — "
        f"{newest.isoformat()} (старше окна {window} дн.). "
        f"Привожу данные на {newest.isoformat()}, а не новости текущего дня."
    )


def format_sources_context_for_llm(sources: list[SourceItem], *, limit: int = 5) -> str:
    """Plain-text evidence cards for the model (not HTML)."""
    items = sources[:limit]
    if not items:
        return "Подтверждённые источники отсутствуют."
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        published = str(item.get("published_at") or "").strip() or "дата не указана"
        snippet = str(item.get("snippet") or "").strip() or "(сниппет отсутствует)"
        lines.append(
            f"{idx}. {item['title']}\n"
            f"   URL: {item['url']}\n"
            f"   Дата публикации: {published}\n"
            f"   Сниппет: {snippet[:240]}"
        )
    return "\n".join(lines)


def format_sources_list_html(
    sources: list[SourceItem],
    *,
    limit: int = 5,
) -> str:
    """Numbered clickable titles as HTML anchors to the full article URL."""
    lines: list[str] = []
    for index, item in enumerate(sources[:limit], start=1):
        raw_url = str(item.get("url") or "").strip()
        raw_title = str(item.get("title") or raw_url or "Источник").strip()
        title = html.escape(raw_title, quote=False)
        if not raw_url:
            lines.append(f"{index}. <b>{title}</b>")
            continue
        href = html.escape(raw_url, quote=True)
        lines.append(f'{index}. <a href="{href}"><b>{title}</b></a>')
    return "\n".join(lines)


def format_sources_message_html(sources: list[SourceItem], *, limit: int = 5) -> str:
    body = format_sources_list_html(sources, limit=limit)
    if not body:
        return ""
    return f"🔗 <b>Источники</b>\n{body}"


def _truncate_button_label(text: str, *, limit: int = 64) -> str:
    t = (text or "").replace("\n", " ").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)] + "…"


def build_sources_keyboard(sources: list[SourceItem], *, limit: int = 5):
    """Inline URL buttons with the same URLs as the HTML list."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(sources[:limit], start=1):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        title = (item.get("title") or url).strip()
        label = _truncate_button_label(f"{index}. {title}", limit=64)
        rows.append([InlineKeyboardButton(text=label, url=url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_concierge_callback(callback_data: str) -> bool:
    cd = (callback_data or "").strip().lower().replace(" ", "")
    return cd == "concierge_run" or "concierge" in cd


def filter_out_concierge_buttons(
    buttons: list[dict[str, str]] | None,
    *,
    concierge_enabled: bool,
) -> list[dict[str, str]]:
    """When concierge is off, drop callback buttons tied to concierge; keep url buttons."""
    raw = list(buttons or [])
    if concierge_enabled:
        return raw
    kept: list[dict[str, str]] = []
    for b in raw:
        if b.get("url"):
            kept.append(b)
            continue
        cd = str(b.get("callback_data") or "")
        if is_concierge_callback(cd):
            logger.info("hide concierge button (CONCIERGE_ENABLED=false): %r", cd)
            continue
        kept.append(b)
    return kept
