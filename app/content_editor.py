"""Редактор контента: Tavily + черновик → апрув в ЛС → публикация в канал @kriptogeograph."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Config
from app.database import (
    get_connection,
    get_draft_feedback_rejected_approved_counts,
    get_draft_feedback_slice,
    get_editorial_feedbacks_baseline,
    get_editorial_rules,
    get_feedbacks_count,
    get_feedback_category_counts_in_window,
    get_recent_draft_feedback_window,
    get_recent_expired_urls,
    save_editorial_rules,
)
from app.llm_agent import LLMAgent
from app.memory import ChatMemory
from app.statistics import get_channel_quality_snapshot, get_channel_quality_top_bottom

logger = logging.getLogger(__name__)

try:
    from app.news_bot_patterns import BREAKING_PATTERN, match_categories, score_text

    USE_PATTERNS = True
except ImportError:
    USE_PATTERNS = False
    BREAKING_PATTERN = re.compile(r"$^")

    def match_categories(text: str) -> dict[str, list[str]]:
        return {}

    def score_text(text: str) -> dict[str, int]:
        return {}

# Канал @kriptogeograph (v1 — константа; позже можно вынести в config)
DEFAULT_EDITOR_CHANNEL_ID = "-1001985473246"
MAX_POST_CHARS = 1000

PREF_ENABLED = "content_editor_enabled"
PREF_CHANNEL = "content_editor_channel_id"
PREF_TOPICS = "content_editor_topics"
PREF_SOURCES = "content_editor_sources"
PREF_REJECT_HINTS = "content_editor_reject_hints"
PREF_REJECT_COUNT = "content_editor_reject_count"
PREF_APPROVE_COUNT = "content_editor_approve_count"

PREF_AUTO_ENABLED = "content_editor_auto_enabled"
PREF_AUTO_INTERVAL_HOURS = "content_editor_auto_interval_hours"
PREF_LAST_AUTO_FETCH_TS = "content_editor_last_auto_fetch_ts"
PREF_AUTO_PAUSED_UNTIL_TS = "content_editor_auto_paused_until_ts"
PREF_AUTO_DISABLED_REASON = "content_editor_auto_disabled_reason"
PREF_LAST_LIMIT_NOTIFY_TS = "content_editor_last_limit_notify_ts"
PREF_AUTO_INTRO_SENT = "content_editor_auto_intro_sent"
PREF_SOURCE_MODE = "content_editor_source_mode"
PREF_TG_CHANNELS = "content_editor_tg_channels"
PREF_HOST_REJECT_COUNTS = "content_editor_reject_host_counts"
PREF_DRAFT_DEADLINE_HOURS = "content_editor_draft_deadline_hours"
PREF_SEARCH_WINDOW_DAYS = "content_editor_search_window_days"

DEFAULT_AUTO_INTERVAL_HOURS = 0.5
MIN_AUTO_INTERVAL_HOURS = 0.5
MAX_AUTO_INTERVAL_HOURS = 168
DEFAULT_DRAFT_DEADLINE_HOURS = 24
ALLOWED_DRAFT_DEADLINE_HOURS = frozenset({24, 48, 72})
DEFAULT_SEARCH_WINDOW_DAYS = 2
MIN_SEARCH_WINDOW_DAYS = 1
MAX_SEARCH_WINDOW_DAYS = 30
# Максимум черновиков в статусе draft на пользователя (ручной /drafts, авто-поиск, insert).
MAX_PENDING_UNAPPROVED_DRAFTS = 6
# Окна исключения source_url (подставляются из Config в init_content_editor_defaults).
_EXCLUDE_POSTED_DAYS = 14
_EXCLUDE_REJECTED_DAYS = 7

AUTO_DIRECTIVE_RE = re.compile(
    r"(?is)(?<!\S)авто\s*:\s*((?:\d{1,3}(?:[.,]\d+)?|off|выкл|0))\b",
)
DEADLINE_DIRECTIVE_RE = re.compile(r"(?is)(?<!\S)дедлайн\s*:\s*(24|48|72)\b")
SOURCES_MODE_RE = re.compile(r"(?is)(?<!\S)источники\s*:\s*(web|tg|both)\b")
# «тканалы» — частая опечатка вместо «тгканалы» (без буквы г).
TG_CHANNELS_RE = re.compile(
    r"(?is)(?<!\S)(?:тгканалы|тканалы|tg_channels)\s*:\s*([^\n]+?)(?=\s+(?:источники|тгканалы|тканалы|tg_channels|авто|темы|тема|topics)\s*:|$)",
)
# «тгканалы @a @b» без двоеточия (после слова — пробел, не :).
_NEXT_EDITOR_DIRECTIVE = (
    r"(?=\s+(?:источники|тгканалы|тканалы|tg_channels|авто|темы|тема|topics)\s*:|$)"
)
TG_CHANNELS_SPACE_RE = re.compile(
    rf"(?is)(?<!\S)(?:тгканалы|тканалы|tg_channels)\s+(?!:)([^\n]+?){_NEXT_EDITOR_DIRECTIVE}",
)
TOPICS_DIRECTIVE_RE = re.compile(
    rf"(?is)(?<!\S)(?:темы|тема|topics)\s*:\s*([^\n]+?){_NEXT_EDITOR_DIRECTIVE}",
)
TOPICS_SPACE_RE = re.compile(
    rf"(?is)(?<!\S)(?:темы|тема|topics)\s+(?!:)([^\n]+?){_NEXT_EDITOR_DIRECTIVE}",
)

SOURCE_MODES = frozenset({"web", "tg", "both"})
HOST_HARD_REJECT_THRESHOLD = 4
URL_REJECT_PREFIX = "url:"
VESTI_CLEANUP_USER_ID = 504425191
VESTI_HOST_KEYS = frozenset({"vesti.ru", "www.vesti.ru"})
WEB_PROMO_DOMAINS = (
    "goha.ru",
    "store.steampowered.com",
    "epicgames.com",
    "apps.apple.com",
    "play.google.com",
    # Промо/агрегаторы: передаём в Tavily как exclude_domains (не в текст запроса).
    "ign.com",
)
LOW_QUALITY_WEB_DOMAINS = frozenset(
    {
        "t.me",
        "telegram.me",
        "telegra.ph",
        "dzen.ru",
        "zen.yandex.ru",
        "pikabu.ru",
    }
)
MIN_WEB_SNIPPET_LEN = 100
TRUSTED_DOMAINS = frozenset(
    {
        "ria.ru",
        "tass.ru",
        "rbc.ru",
        "kommersant.ru",
        "vedomosti.ru",
        "lenta.ru",
        "meduza.io",
        "bbc.com",
        "reuters.com",
        "techcrunch.com",
        "ign.com",
        "kotaku.com",
        "animenewsnetwork.com",
        "crunchyroll.com",
    }
)
SEMANTIC_DUP_STOPWORDS = frozenset(
    {
        "в",
        "на",
        "и",
        "с",
        "по",
        "за",
        "из",
        "от",
        "для",
        "что",
        "как",
        "это",
        "он",
        "она",
        "они",
        "the",
        "a",
        "an",
        "is",
        "of",
        "to",
        "in",
        "and",
        "for",
    }
)
_DRAFT_ACTION_VERBS = (
    "вышел",
    "запустил",
    "объявил",
    "сообщил",
    "показал",
    "представил",
    "выпустил",
    "анонсировал",
    "revealed",
    "announced",
    "launched",
    "released",
    "reported",
    "confirmed",
)

_TG_DEFAULT_USERNAMES: list[str] = [
    "rian_ru",
    "readovkanews",
    "meduzalive",
    "tass_agency",
    "thebell_io",
]

CB_APPROVE = "a"
CB_EDIT = "e"
CB_REJECT = "r"
CB_EXPIRED = "x"  # устарел контент — не blacklist / не штраф канала

_pending_edit: dict[int, int] = {}
_pending_feedback: dict[int, dict[str, Any]] = {}

DRAFT_SYSTEM = (
    "Ты — Кузьма, редактор коротких постов для Telegram-канала @kriptogeograph. "
    "Темы задаёт пользователь — это могут быть новости, наука, шоу-бизнес, технологии, путешествия, спорт и что угодно ещё; "
    "не впихивай финансовую рамку, если материал не про деньги, рынки или вложения.\n"
    "Пиши в новостном стиле: факты, событие, что произошло и почему это важно.\n"
    "Запрещено: рекламные призывы, восторженные описания продуктов и маркетинговые формулировки "
    "вроде «лучший», «невероятный», «потрясающий».\n"
    "Разрешено: лёгкий юмор и живой язык, но основа текста — новостная фактура.\n"
    "Если источник написан рекламно, перепиши факты своими словами и не копируй его тон.\n"
    "Избегай копирования структуры предыдущих постов. Каждый пост должен иметь уникальную подачу, даже если тема похожа.\n"
    "Напиши ОДИН готовый пост: цепляющий заголовок в первой строке, пустая строка, "
    "2–3 предложения саммари с лёгким юмором и 2–3 разных эмодзи, пустая строка, "
    "строка «Источник:» и URL из входных данных.\n"
    "Без хештегов # и без JSON. Максимум символов в ответе: 1000 (если длиннее — сожми).\n"
)

# Добавляется к системному промпту только если тема/сниппет похожи на финансы или крипто.
DRAFT_FINANCE_OVERLAY = (
    "\nДополнительно для этого материала: не обещай гарантированную доходность и не давай персональных советов по вложениям; "
    "в самом конце поста одна короткая оговорка, что это не персональная инвестиционная рекомендация.\n"
)

_FINANCE_TOPIC_SUBSTRINGS = (
    "биткоин",
    "bitcoin",
    "крипт",
    "crypt",
    "эфир",
    "ethereum",
    "defi",
    "токен",
    "блокчейн",
    "blockchain",
    "инвест",
    "трейд",
    "trading",
    "акци",
    "облигац",
    "ipo",
    "бирж",
    "форекс",
    "forex",
    "nft",
    "ico",
    "майнинг",
    "фьючерс",
    "маржа",
    "котировк",
    "дивиденд",
    "etf",
    "портфел",
    "волатильн",
    "web3",
    "стейк",
    "бинанс",
    "binance",
    "ставка цб",
    "ключевой ставк",
    "нефть",
    "brent",
    "wti",
)

_FINANCE_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "крипта": (
        "биткоин",
        "bitcoin",
        "крипт",
        "crypt",
        "эфир",
        "ethereum",
        "defi",
        "токен",
        "блокчейн",
        "blockchain",
        "nft",
        "ico",
        "майнинг",
        "web3",
        "бинанс",
        "binance",
    ),
    "финансы": (
        "инвест",
        "трейд",
        "trading",
        "акци",
        "облигац",
        "ipo",
        "бирж",
        "форекс",
        "forex",
        "фьючерс",
        "маржа",
        "котировк",
        "дивиденд",
        "etf",
        "портфел",
        "волатильн",
    ),
    "экономика": (
        "эконом",
        "инфляц",
        "ввп",
        "рецес",
        "ставка цб",
        "ключевой ставк",
        "монетарн",
        "макроэконом",
        "нефть",
        "brent",
        "wti",
    ),
}


def _detect_finance_categories(title: str, snippet: str) -> set[str]:
    blob = f"{title} {snippet}".lower().replace("ё", "е")
    categories: set[str] = set()
    for cat, words in _FINANCE_CATEGORY_KEYWORDS.items():
        if any(w in blob for w in words):
            categories.add(cat)
    return categories


def _draft_material_sounds_financial(topics: str, title: str, snippet: str) -> bool:
    # Критично: дисклеймер определяется по текущему материалу (title+snippet), а не по общим темам пользователя.
    # Иначе нерелевантные посты (стриминг/игры) получают финансовую оговорку из-за старых "крипто" тем в prefs.
    categories = _detect_finance_categories(title, snippet)
    if categories:
        logger.debug(
            "Draft finance classifier: enabled categories=%s title=%r",
            sorted(categories),
            (title or "")[:120],
        )
        return True
    # Фолбэк на старый список ключей, но только по материалу (без topics).
    blob = f"{title} {snippet}".lower().replace("ё", "е")
    for w in _FINANCE_TOPIC_SUBSTRINGS:
        if w in blob:
            logger.debug(
                "Draft finance classifier: enabled by keyword=%r title=%r",
                w,
                (title or "")[:120],
            )
            return True
    logger.debug(
        "Draft finance classifier: disabled title=%r snippet=%r",
        (title or "")[:120],
        (snippet or "")[:160],
    )
    return False


def _prefs(memory: ChatMemory, user_id: int) -> dict[str, str]:
    return memory.get_style_preferences(user_id)


def is_editor_enabled(memory: ChatMemory, user_id: int) -> bool:
    return _prefs(memory, user_id).get(PREF_ENABLED, "").strip() in ("1", "true", "yes", "on")


def _truthy_pref(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def is_auto_enabled_pref(prefs: dict[str, str]) -> bool:
    return _truthy_pref(prefs.get(PREF_AUTO_ENABLED))


def _fmt_hours_value(hours: float) -> str:
    if float(hours).is_integer():
        return str(int(hours))
    return f"{hours:.2f}".rstrip("0").rstrip(".")


def format_auto_interval_label(hours: float) -> str:
    if hours < 1:
        mins = int(round(hours * 60))
        return f"~{mins} мин"
    return f"~{_fmt_hours_value(hours)} ч"


def auto_interval_hours_from_prefs(prefs: dict[str, str]) -> float:
    try:
        h = float(
            (prefs.get(PREF_AUTO_INTERVAL_HOURS) or str(DEFAULT_AUTO_INTERVAL_HOURS))
            .strip()
            .replace(",", ".")
        )
    except ValueError:
        h = DEFAULT_AUTO_INTERVAL_HOURS
    return max(MIN_AUTO_INTERVAL_HOURS, min(MAX_AUTO_INTERVAL_HOURS, h))


def draft_deadline_hours_from_prefs(prefs: dict[str, str]) -> int:
    raw = (prefs.get(PREF_DRAFT_DEADLINE_HOURS) or "").strip()
    try:
        v = int(raw)
    except ValueError:
        v = DEFAULT_DRAFT_DEADLINE_HOURS
    if v not in ALLOWED_DRAFT_DEADLINE_HOURS:
        v = DEFAULT_DRAFT_DEADLINE_HOURS
    return v


def parse_auto_directive_from_rest(rest: str) -> tuple[str, dict[str, str] | None]:
    """Достаёт «авто:X» из хвоста /editor_prefs; последняя директива определяет значение."""
    s = (rest or "").strip()
    if not s:
        return "", None
    matches = list(AUTO_DIRECTIVE_RE.finditer(s))
    if not matches:
        return s, None
    last = matches[-1]
    raw = last.group(1).strip().lower()
    cleaned = AUTO_DIRECTIVE_RE.sub(" ", s)
    cleaned = " ".join(cleaned.split()).strip()
    updates: dict[str, str] = {}
    if raw in ("off", "выкл", "0"):
        updates[PREF_AUTO_ENABLED] = "0"
    else:
        try:
            h = float(raw.replace(",", "."))
        except ValueError:
            return s, None
        h = max(MIN_AUTO_INTERVAL_HOURS, min(MAX_AUTO_INTERVAL_HOURS, h))
        updates[PREF_AUTO_ENABLED] = "1"
        updates[PREF_AUTO_INTERVAL_HOURS] = _fmt_hours_value(h)
        updates[PREF_AUTO_DISABLED_REASON] = ""
    return cleaned, updates


def parse_deadline_directive_from_rest(rest: str) -> tuple[str, dict[str, str] | None]:
    """Достаёт «дедлайн:24|48|72» из хвоста /editor_prefs; последняя директива определяет значение."""
    s = (rest or "").strip()
    if not s:
        return "", None
    matches = list(DEADLINE_DIRECTIVE_RE.finditer(s))
    if not matches:
        return s, None
    last = matches[-1]
    val = int(last.group(1))
    cleaned = DEADLINE_DIRECTIVE_RE.sub(" ", s)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned, {PREF_DRAFT_DEADLINE_HOURS: str(val)}


def init_content_editor_defaults(cfg: Config | None = None) -> None:
    """Подставляет дефолтный список публичных TG-каналов из Config (env)."""
    global _TG_DEFAULT_USERNAMES, _EXCLUDE_POSTED_DAYS, _EXCLUDE_REJECTED_DAYS
    if cfg is None:
        return
    pd = int(getattr(cfg, "content_editor_exclude_posted_days", 14) or 14)
    rd = int(getattr(cfg, "content_editor_exclude_rejected_days", 7) or 7)
    _EXCLUDE_POSTED_DAYS = max(1, min(365, pd))
    _EXCLUDE_REJECTED_DAYS = max(1, min(365, rd))
    raw = (getattr(cfg, "content_editor_tg_default_channels", None) or "").strip()
    if not raw:
        return
    parsed = _parse_username_csv(raw)
    if parsed:
        _TG_DEFAULT_USERNAMES = parsed
        logger.info("content_editor: дефолтные TG-каналы: %s", ",".join(parsed))


def _vesti_related_reject_entry(x: str) -> bool:
    t = (x or "").strip().lower()
    if not t:
        return False
    if t in VESTI_HOST_KEYS or t.lstrip("www.") == "vesti.ru":
        return True
    if t.startswith(URL_REJECT_PREFIX.lower()):
        rest = t[len(URL_REJECT_PREFIX) :].strip()
        if "vesti.ru" in rest:
            return True
    return False


def migrate_strip_vesti_reject_hints(memory: ChatMemory, user_id: int = VESTI_CLEANUP_USER_ID) -> None:
    """Точечно убирает vesti.ru из отказов и счётчиков хостов (без сброса остальных prefs)."""
    prefs = _prefs(memory, user_id)
    cur = _reject_list(prefs)
    new_r = [x for x in cur if not _vesti_related_reject_entry(x)]
    updates: dict[str, str] = {}
    if new_r != cur:
        updates[PREF_REJECT_HINTS] = json.dumps(new_r, ensure_ascii=False)
    counts = _load_host_reject_counts(prefs)
    new_c = {k: v for k, v in counts.items() if k not in VESTI_HOST_KEYS and k != "vesti.ru"}
    if new_c != counts:
        updates[PREF_HOST_REJECT_COUNTS] = json.dumps(new_c, ensure_ascii=False)
    if updates:
        memory.update_style_preferences(user_id, updates)
        logger.info(
            "content_editor migrate: подчистил vesti.ru в reject_hints/счётчиках для user_id=%s",
            user_id,
        )


def parse_editor_extra_directives(rest: str) -> tuple[str, dict[str, str]]:
    """Вырезает директивы: источники:, тгканалы:/тгканалы , тканалы:, темы:/темы ."""
    s = (rest or "").strip()
    if not s:
        return "", {}
    updates: dict[str, str] = {}
    for m in SOURCES_MODE_RE.finditer(s):
        mode = m.group(1).strip().lower()
        if mode in SOURCE_MODES:
            updates[PREF_SOURCE_MODE] = mode
    s = SOURCES_MODE_RE.sub(" ", s)
    for m in TG_CHANNELS_RE.finditer(s):
        raw_ch = m.group(1).strip()
        if raw_ch:
            updates[PREF_TG_CHANNELS] = _parse_username_csv_to_pref(raw_ch)
    s = TG_CHANNELS_RE.sub(" ", s)
    for m in TG_CHANNELS_SPACE_RE.finditer(s):
        raw_ch = m.group(1).strip()
        if raw_ch:
            updates[PREF_TG_CHANNELS] = _parse_username_csv_to_pref(raw_ch)
    s = TG_CHANNELS_SPACE_RE.sub(" ", s)
    for m in TOPICS_DIRECTIVE_RE.finditer(s):
        tt, ss = _topics_pair_from_capture(m.group(1))
        if tt:
            updates[PREF_TOPICS] = tt
            updates[PREF_SOURCES] = ss
    s = TOPICS_DIRECTIVE_RE.sub(" ", s)
    for m in TOPICS_SPACE_RE.finditer(s):
        tt, ss = _topics_pair_from_capture(m.group(1))
        if tt:
            updates[PREF_TOPICS] = tt
            updates[PREF_SOURCES] = ss
    s = TOPICS_SPACE_RE.sub(" ", s)
    cleaned = " ".join(s.split()).strip()
    tg_list_dbg = (
        _parse_username_csv(updates[PREF_TG_CHANNELS])
        if PREF_TG_CHANNELS in updates
        else []
    )
    logger.debug(
        "editor_prefs parsed: source_mode=%s tg_channels=%s topics=%r cleaned_tail=%r",
        updates.get(PREF_SOURCE_MODE),
        tg_list_dbg,
        updates.get(PREF_TOPICS),
        cleaned[:220],
    )
    if updates:
        logger.info(
            "editor_prefs parse: source_mode=%s tg_channels_pref=%r topics=%r",
            updates.get(PREF_SOURCE_MODE, "—"),
            updates.get(PREF_TG_CHANNELS, "—"),
            updates.get(PREF_TOPICS, "—"),
        )
    return cleaned, updates


def get_source_mode(prefs: dict[str, str]) -> str:
    m = (prefs.get(PREF_SOURCE_MODE) or "").strip().lower()
    return m if m in SOURCE_MODES else "both"


def editor_needs_tavily(prefs: dict[str, str]) -> bool:
    return get_source_mode(prefs) in ("web", "both")


def _parse_username_csv(raw: str) -> list[str]:
    """Список username: запятая, точка с запятой или пробел между @каналами."""
    out: list[str] = []
    for part in re.split(r"[;,\s]+", (raw or "").strip()):
        u = part.strip().lstrip("@").lower()
        u = re.sub(r"[^a-z0-9_]", "", u)
        if u and u not in out:
            out.append(u)
    return out


def _parse_username_csv_to_pref(raw: str) -> str:
    # Длинные списки каналов; SQLite TEXT без жёсткого лимита — оставляем запас под десятки username.
    return ",".join(_parse_username_csv(raw))[:4000]


def _merged_tg_channel_names(prefs: dict[str, str]) -> list[str]:
    """Если пользователь задал тгканалы — только они; иначе дефолтный список из конфига."""
    extra = _parse_username_csv(prefs.get(PREF_TG_CHANNELS) or "")
    if extra:
        return extra
    return list(_TG_DEFAULT_USERNAMES)


def _approved_posts_count(user_id: int) -> int:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM draft_posts
                WHERE user_id=? AND status='posted' AND approved_at IS NOT NULL
                """,
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("approved_posts_count: %s", exc)
        return 0


def get_voice_examples(user_id: int, limit: int = 3) -> list[str]:
    """Последние апрувнутые посты пользователя — как эталоны голоса канала."""
    uid = str(user_id)
    lim = max(3, min(5, int(limit or 3)))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT content
                FROM draft_posts
                WHERE user_id=? AND status='posted' AND approved_at IS NOT NULL
                ORDER BY datetime(approved_at) DESC, id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
        out: list[str] = []
        for r in rows:
            t = str(r["content"] or "").strip()
            if t:
                out.append(t[:900])
        return out
    except Exception as exc:
        logger.exception("get_voice_examples: %s", exc)
        return []


def build_voice_examples_overlay(user_id: int, limit: int = 3) -> str:
    examples = get_voice_examples(user_id, limit=limit)
    if not examples:
        return ""
    lines = [f"{i + 1}) {txt}" for i, txt in enumerate(examples)]
    return (
        "\nВот примеры постов, которые автор канала одобрил ранее — "
        "используй их как ориентир стиля и тона:\n"
        + "\n\n".join(lines)
        + "\n"
    )


_MAX_RULES_OVERLAY_CHARS = 3200


def build_editorial_rules_overlay(user_id: int) -> str:
    """Текст для system prompt: дистиллированные пожелания редактора из БД (пусто, если ещё нет)."""
    raw = get_editorial_rules(user_id)
    if not raw:
        return ""
    t = raw.strip()
    if not t:
        return ""
    if len(t) > _MAX_RULES_OVERLAY_CHARS:
        t = t[: _MAX_RULES_OVERLAY_CHARS - 1] + "…"
    return (
        "\nУстойчивые пожелания редактора (учитывай в тоне и структуре поста; не цитируй список дословно):\n"
        f"{t}\n"
    )


def format_editor_info_text(prefs: dict[str, str], *, user_id: int | None = None) -> str:
    """Текст для /editor_info — сводка prefs редактора."""
    sm = get_source_mode(prefs)
    tg_raw = (prefs.get(PREF_TG_CHANNELS) or "").strip()
    tg_parsed = _parse_username_csv(tg_raw)
    if tg_parsed:
        tg_line = ", ".join(f"@{x}" for x in tg_parsed)
        tg_note = "При поиске TG используются только эти каналы (дефолтный список не смешивается)."
    else:
        tg_line = ", ".join(f"@{x}" for x in _TG_DEFAULT_USERNAMES)
        tg_note = "Свой список: /editor_prefs тгканалы:@канал1,@канал2 (допустима опечатка «тканалы:»)."
    topics = (prefs.get(PREF_TOPICS) or "").strip() or "—"
    sources = (prefs.get(PREF_SOURCES) or "").strip() or "—"
    auto_on = is_auto_enabled_pref(prefs)
    auto = "включён" if auto_on else "выключен"
    ah = auto_interval_hours_from_prefs(prefs)
    deadline_h = draft_deadline_hours_from_prefs(prefs)
    voice_line = ""
    quality_line = ""
    if user_id is not None:
        approved_n = _approved_posts_count(user_id)
        if approved_n >= 10:
            logger.info("editor_voice: user_id=%s voice сформирован (%s+ апрувов)", user_id, approved_n)
            voice_line = f"• Голос канала: сформирован ({approved_n} апрувов)\n"
        else:
            voice_line = f"• Голос канала: в обучении ({approved_n}/10 апрувов)\n"
        top_ch, bad_ch = get_channel_quality_top_bottom(top_limit=5, bottom_limit=3)
        if top_ch:
            top_txt = "; ".join(
                f"@{x['channel_username']} (A:{x['approved_count']} / R:{x['rejected_count']})"
                for x in top_ch
            )
        else:
            top_txt = "пока нет данных"
        if bad_ch:
            bad_txt = "; ".join(
                f"@{x['channel_username']} (A:{x['approved_count']} / R:{x['rejected_count']})"
                for x in bad_ch
            )
        else:
            bad_txt = "пока нет данных"
        quality_line = (
            f"• Топ-5 каналов по апрувам: {top_txt}\n"
            f"• Худшие 3 канала по апрувам: {bad_txt}\n"
        )
    return (
        "Текущие настройки редактора:\n\n"
        f"• Источники: {sm} (web / tg / both)\n"
        f"• ТГ-каналы: {tg_line}\n"
        f"  ({tg_note})\n\n"
        f"• Темы: {topics[:500]}{'…' if len(topics) > 500 else ''}\n"
        f"• Уточнение к поиску: {sources[:500]}{'…' if len(sources) > 500 else ''}\n\n"
        f"• Срок жизни черновика: {deadline_h} ч\n"
        + voice_line
        + quality_line
        + f"• Авто-поиск: {auto}"
        + (f", интервал {format_auto_interval_label(ah)}" if auto_on else "")
        + "\n\n"
        "Команды настройки (можно смешивать в одной строке):\n"
        "• темы: … или темы … — только темы и уточнение (через запятую после первой темы);\n"
        "• тгканалы: … или тгканалы @a @b — только список каналов;\n"
        "• источники: web|tg|both — режим материалов;\n"
        "• дедлайн:24|48|72 — срок жизни черновика.\n"
        "Сброс отказов и жёстких банов по каналу/сайту: /editor_reset_rejects"
    )


def _load_host_reject_counts(prefs: dict[str, str]) -> dict[str, int]:
    raw = (prefs.get(PREF_HOST_REJECT_COUNTS) or "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in d.items():
        kk = str(k).strip().lower().lstrip("www.")
        if not kk:
            continue
        # Раньше считали netloc t.me — банили весь Telegram; такие ключи игнорируем.
        if kk in ("t.me", "telegram.me", "telegram.dog"):
            continue
        try:
            out[kk] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _bump_host_reject_count(memory: ChatMemory, user_id: int, host: str) -> None:
    h = (host or "").strip().lower().lstrip("www.")
    if not h:
        return
    prefs = _prefs(memory, user_id)
    counts = _load_host_reject_counts(prefs)
    counts[h] = counts.get(h, 0) + 1
    nv = counts[h]
    memory.update_style_preferences(
        user_id,
        {PREF_HOST_REJECT_COUNTS: json.dumps(counts, ensure_ascii=False)},
    )
    if nv >= HOST_HARD_REJECT_THRESHOLD:
        logger.warning(
            "content_editor reject: достигнут порог жёсткого бана (%s/%s) для ключа %r user_id=%s",
            nv,
            HOST_HARD_REJECT_THRESHOLD,
            h,
            user_id,
        )


def _norm_cmp_url(url: str) -> str:
    u = (url or "").strip().lower()
    try:
        p = urlparse(u)
        path = (p.path or "").rstrip("/")
        net = (p.netloc or "").lower().lstrip("www.")
        if net and not p.scheme:
            return f"https://{net}{path}".rstrip("/")
        scheme = (p.scheme or "https").lower()
        return f"{scheme}://{net}{path}".rstrip("/")
    except Exception:
        return u.rstrip("/")


def extract_tg_channel_username_from_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        p = urlparse(raw)
        host = (p.netloc or "").lower()
        if "t.me" not in host and "telegram.me" not in host:
            return None
        parts = [x for x in (p.path or "").split("/") if x]
        if not parts:
            return None
        if parts[0].lower() == "s" and len(parts) >= 2:
            return parts[1].strip().lstrip("@").lower() or None
        return parts[0].strip().lstrip("@").lower() or None
    except Exception:
        return None


def _source_verification_line(url: str, *, from_telegram: bool) -> str:
    if from_telegram:
        return ""
    raw = (url or "").strip()
    if not raw:
        return "⚠️ Источник не верифицирован"
    try:
        host = (urlparse(raw).netloc or "").lower().lstrip("www.")
    except Exception:
        host = ""
    if not host:
        return "⚠️ Источник не верифицирован"
    if "t.me" in host or "telegram.me" in host:
        return ""
    if any(host == d or host.endswith(f".{d}") for d in TRUSTED_DOMAINS):
        return "✅ Источник проверен"
    return "⚠️ Источник не верифицирован"


def _title_tokens_for_similarity(title: str) -> set[str]:
    t = (title or "").lower().replace("ё", "е")
    words = re.findall(r"[a-zа-я0-9]+", t)
    return {w for w in words if len(w) > 1 and w not in SEMANTIC_DUP_STOPWORDS}


def _is_duplicate_by_title(title: str, user_id: int) -> bool:
    words_new = _title_tokens_for_similarity(title)
    if not words_new:
        return False
    uid = str(user_id)
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT content
                FROM draft_posts
                WHERE user_id=?
                  AND status='posted'
                  AND approved_at IS NOT NULL
                  AND datetime(approved_at) >= datetime('now', '-14 days')
                ORDER BY datetime(approved_at) DESC, id DESC
                LIMIT 30
                """,
                (uid,),
            ).fetchall()
    except Exception as exc:
        logger.exception("semantic_duplicate check: %s", exc)
        return False

    for r in rows:
        prev_content = str(r["content"] or "")
        prev_title = (prev_content.splitlines()[0] if prev_content else "").strip()
        words_prev = _title_tokens_for_similarity(prev_title)
        if not words_prev:
            continue
        similarity = len(words_new & words_prev) / max(1, len(words_new))
        if similarity >= 0.6:
            logger.debug(
                "skip semantic_duplicate title=%r prev=%r similarity=%.3f",
                (title or "")[:120],
                prev_title[:120],
                similarity,
            )
            return True
    return False


def _assess_draft_quality(draft_text: str) -> tuple[bool, str]:
    text = (draft_text or "").strip()
    if len(text) < 100:
        return False, "too_short"
    if not re.search(r"\d", text):
        return False, "no_numbers_or_dates"
    if text.count("!") > 3:
        return False, "too_emotional_exclamations"
    low = text.lower().replace("ё", "е")
    if not any(v in low for v in _DRAFT_ACTION_VERBS):
        return False, "no_action_verb"
    return True, "ok"


def _append_weak_draft_marker(text: str) -> str:
    """Финальная пометка для слабого черновика (после исчерпания ретраев или при выключенной опции)."""
    warn_line = "⚠️ Требует проверки"
    t = (text or "").strip()
    if warn_line in t:
        return t
    return f"{t.rstrip()}\n\n{warn_line}".strip()


def _get_related_context(user_id: int, title: str, limit: int = 20) -> str | None:
    words_new = _title_tokens_for_similarity(title)
    if not words_new:
        return None
    uid = str(user_id)
    lim = max(1, min(50, int(limit or 20)))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT content
                FROM draft_posts
                WHERE user_id=? AND status='posted'
                ORDER BY datetime(approved_at) DESC, id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
    except Exception as exc:
        logger.exception("related_context check: %s", exc)
        return None

    for r in rows:
        prev_content = str(r["content"] or "")
        prev_title = (prev_content.splitlines()[0] if prev_content else "").strip()
        if not prev_title:
            continue
        words_prev = _title_tokens_for_similarity(prev_title)
        if not words_prev:
            continue
        similarity = len(words_new & words_prev) / max(1, len(words_new))
        if similarity >= 0.4:
            return f"Кстати, ранее по теме: {prev_title}"
    return None


def _insert_after_source_line(text: str, extra_line: str) -> str:
    if not extra_line.strip():
        return text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("источник:"):
            out = lines[: i + 1] + ["", extra_line] + lines[i + 1 :]
            return "\n".join(out).strip()
    return f"{text.rstrip()}\n\n{extra_line}".strip()


def _is_domain_only_hint(h: str) -> bool:
    t = (h or "").strip().lower()
    if not t or t.startswith(URL_REJECT_PREFIX.lower()) or " " in t:
        return False
    return bool(re.fullmatch(r"[\w.-]+\.[a-z]{2,}", t))


def iter_content_editor_user_ids() -> list[int]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT user_id FROM user_preferences
                WHERE pref_key=? AND LOWER(TRIM(pref_value)) IN ('1','true','yes','on')
                """,
                (PREF_ENABLED,),
            ).fetchall()
        return [int(r["user_id"]) for r in rows]
    except Exception as exc:
        logger.exception("iter_content_editor_user_ids: %s", exc)
        return []


def load_editor_exclude_source_urls(user_id: int) -> set[str]:
    """URL источников, которые не стоит снова предлагать: недавно опубликованные, недавно отклонённые, все висящие черновики."""
    uid = str(user_id)
    posted_d = _EXCLUDE_POSTED_DAYS
    rej_d = _EXCLUDE_REJECTED_DAYS
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT TRIM(source_url) AS u
                FROM draft_posts
                WHERE user_id=?
                  AND source_url IS NOT NULL
                  AND TRIM(source_url) != ''
                  AND (
                    (
                      status = 'posted'
                      AND approved_at IS NOT NULL
                      AND datetime(approved_at) >= datetime('now', ?)
                    )
                    OR (
                      status = 'rejected'
                      AND datetime(created_at) >= datetime('now', ?)
                    )
                    OR (status = 'draft')
                  )
                """,
                (uid, f"-{posted_d} days", f"-{rej_d} days"),
            ).fetchall()
            posted_n = conn.execute(
                """
                SELECT COUNT(*) AS c FROM draft_posts
                WHERE user_id=? AND status='posted' AND source_url IS NOT NULL AND TRIM(source_url) != ''
                  AND approved_at IS NOT NULL
                  AND datetime(approved_at) >= datetime('now', ?)
                """,
                (uid, f"-{posted_d} days"),
            ).fetchone()
            rej_n = conn.execute(
                """
                SELECT COUNT(*) AS c FROM draft_posts
                WHERE user_id=? AND status='rejected' AND source_url IS NOT NULL AND TRIM(source_url) != ''
                  AND datetime(created_at) >= datetime('now', ?)
                """,
                (uid, f"-{rej_d} days"),
            ).fetchone()
            draft_n = conn.execute(
                """
                SELECT COUNT(*) AS c FROM draft_posts
                WHERE user_id=? AND status='draft' AND source_url IS NOT NULL AND TRIM(source_url) != ''
                """,
                (uid,),
            ).fetchone()
        out = {str(r["u"]).strip() for r in rows if r and r["u"]}
        logger.info(
            "Draft exclude list: user_id=%s distinct_urls=%s "
            "(posted_rows≤%sd=%s, rejected_rows≤%sd=%s, draft_rows_pending=%s)",
            user_id,
            len(out),
            posted_d,
            int(posted_n["c"]) if posted_n else 0,
            rej_d,
            int(rej_n["c"]) if rej_n else 0,
            int(draft_n["c"]) if draft_n else 0,
        )
        return out
    except Exception as exc:
        logger.exception("load_editor_exclude_source_urls: %s", exc)
        return set()


def recent_draft_source_urls(user_id: int, days: int | None = None) -> set[str]:
    """Совместимость с авто-поиском: то же, что load_editor_exclude_source_urls (аргумент days игнорируется)."""
    if days is not None:
        logger.debug("recent_draft_source_urls: параметр days устарел, используйте Config CONTENT_EDITOR_EXCLUDE_*")
    return load_editor_exclude_source_urls(user_id)


def auto_paused_until_ts(prefs: dict[str, str]) -> float | None:
    raw = (prefs.get(PREF_AUTO_PAUSED_UNTIL_TS) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_auto_paused(prefs: dict[str, str]) -> bool:
    u = auto_paused_until_ts(prefs)
    if u is None:
        return False
    return time.time() < u


def reject_spree_should_pause(user_id: int, prefs: dict[str, str]) -> bool:
    """
    Пауза авто-поиска при «перекосе» в сторону отказов.
    Учитываются только явные отказы (фидбек action=rejected), не «Устарело» (expired_content).
    Если ни одного фидбека rejected/approved ещё нет — сохраняем счётчики из prefs (клики ❌/✅ до ответа на «почему»).
    """
    rc_fb, ac_fb = get_draft_feedback_rejected_approved_counts(user_id)
    rc_p = int(prefs.get(PREF_REJECT_COUNT, "0") or 0)
    ac_p = int(prefs.get(PREF_APPROVE_COUNT, "0") or 0)
    # Исторически feedback-таблица может быть неполной после миграций.
    # Берём максимум между feedback и prefs, чтобы не ловить ложную «серию отказов».
    rc, ac = max(rc_fb, rc_p), max(ac_fb, ac_p)
    return rc >= 8 and rc >= ac + 6


def seconds_until_next_auto_fetch(prefs: dict[str, str]) -> float:
    raw = (prefs.get(PREF_LAST_AUTO_FETCH_TS) or "").strip()
    if not raw:
        return 0.0
    try:
        ts = float(raw)
    except ValueError:
        return 0.0
    interval_sec = float(auto_interval_hours_from_prefs(prefs)) * 3600.0
    left = ts + interval_sec - time.time()
    return max(0.0, left)


def set_pending_edit(user_id: int, draft_id: int) -> None:
    _pending_edit[user_id] = draft_id


def pop_pending_edit(user_id: int) -> int | None:
    return _pending_edit.pop(user_id, None)


def get_pending_edit(user_id: int) -> int | None:
    return _pending_edit.get(user_id)


def set_pending_feedback(
    user_id: int,
    draft_id: int | None,
    action: str,
    draft_preview: str,
    category: str | None = None,
    quality_score: int | None = None,
) -> None:
    """Ожидание свободного ответа пользователя на вопрос после ✅/❌/✏️."""
    _pending_feedback[user_id] = {
        "draft_id": draft_id,
        "action": (action or "").strip().lower(),
        "draft_preview": (draft_preview or "")[:100],
        "category": (category or "").strip().lower()[:64],
        "quality_score": (
            max(1, min(10, int(quality_score)))
            if quality_score is not None
            else None
        ),
    }


def get_pending_feedback(user_id: int) -> dict[str, Any] | None:
    return _pending_feedback.get(user_id)


def pop_pending_feedback(user_id: int) -> dict[str, Any] | None:
    return _pending_feedback.pop(user_id, None)


EDITORIAL_DISTILL_EVERY = 5

_EDITORIAL_DISTILL_SYSTEM = (
    "Ты помогаешь редактору крипто-канала. Во входе два блока фидбека: по устаревшим новостям "
    "(источник ок, материал не свежий) и по отклонённым постам (нежелательно по сути). "
    "Могут быть и прочие пояснения (апрув, правки).\n"
    "Сформулируй правила отдельно для: (1) как определять устаревший контент; "
    "(2) что делает пост нежелательным независимо от даты.\n"
    "Формат: маркированный список на русском, без воды и повторов; можно два логических подпункта в списке.\n"
    "Если даны старые правила — объедини с новым смыслом, убери дубли и явные противоречия (новые факты из батча важнее).\n"
    "Не больше ~2000 символов. Только список, без вступлений и без подписи."
)


def maybe_distill_editorial_rules_sync(agent: LLMAgent, user_id: int) -> None:
    """Каждые EDITORIAL_DISTILL_EVERY новых фидбеков — пересобрать rules_text в SQLite. При сбое GPT — только WARNING."""
    baseline = get_editorial_feedbacks_baseline(user_id)
    total = get_feedbacks_count(user_id)
    if total - baseline < EDITORIAL_DISTILL_EVERY:
        return

    batch = get_draft_feedback_slice(
        user_id, offset=baseline, limit=EDITORIAL_DISTILL_EVERY
    )
    if len(batch) < EDITORIAL_DISTILL_EVERY:
        logger.warning(
            "editorial_distill: ожидалось %s строк, получено %s (user_id=%s baseline=%s total=%s)",
            EDITORIAL_DISTILL_EVERY,
            len(batch),
            user_id,
            baseline,
            total,
        )
        return

    expired_lines: list[str] = []
    rejected_lines: list[str] = []
    other_lines: list[str] = []
    for r in batch:
        act = str(r.get("action") or "").strip().lower()
        prev = str(r.get("draft_preview") or "").replace("\n", " ").strip()
        fb = str(r.get("feedback_text") or "").replace("\n", " ").strip()
        line = (
            f"Действие: {act}. Фрагмент черновика: {prev[:180]}. Комментарий: {fb[:600]}"
        )
        if act == "expired_content":
            expired_lines.append(line)
        elif act == "rejected":
            rejected_lines.append(line)
        else:
            other_lines.append(line)

    def _numbered(block: list[str]) -> str:
        if not block:
            return "(нет)"
        return "\n".join(f"{i}. {t}" for i, t in enumerate(block, start=1))

    prior = (get_editorial_rules(user_id) or "").strip()
    prior_block = prior if prior else "(пока нет — составь с нуля по этому батчу)"

    user_payload = (
        "Текущие правила:\n"
        f"{prior_block}\n\n"
        f"Новые {EDITORIAL_DISTILL_EVERY} пояснений редактора (разбивка по типу решения).\n\n"
        "Фидбек по устаревшим новостям (action=expired_content):\n"
        f"{_numbered(expired_lines)}\n\n"
        "Фидбек по отклонённым постам (action=rejected):\n"
        f"{_numbered(rejected_lines)}\n"
    )
    if other_lines:
        user_payload += (
            "\nПрочие пояснения (другие действия по черновикам):\n"
            f"{_numbered(other_lines)}\n"
        )
    user_payload += (
        "\nСформулируй правила отдельно для:\n"
        "1. Как определять устаревший контент\n"
        "2. Что делает пост нежелательным независимо от даты\n\n"
        "Обнови итог одним связным списком (допустимы подзаголовки внутри списка)."
    )

    try:
        out = agent.run_raw_completion(
            system=_EDITORIAL_DISTILL_SYSTEM,
            user=user_payload,
            temperature=0.25,
            max_tokens=1200,
        )
    except Exception as exc:
        logger.warning("editorial_distill: GPT ошибка user_id=%s: %s", user_id, exc)
        return

    rules = (out or "").strip()
    if not rules:
        logger.warning("editorial_distill: пустой ответ модели user_id=%s", user_id)
        return

    new_baseline = baseline + EDITORIAL_DISTILL_EVERY
    if not save_editorial_rules(user_id, rules, new_baseline):
        logger.warning("editorial_distill: не удалось сохранить в БД user_id=%s", user_id)
        return

    logger.info(
        "editorial_distill: правила обновлены user_id=%s baseline %s → %s",
        user_id,
        baseline,
        new_baseline,
    )


def is_private_chat(message: Any) -> bool:
    ch = getattr(message, "chat", None)
    return bool(ch and getattr(ch, "type", None) == "private")


def is_editor_callback(data: str) -> bool:
    act, did = parse_editor_callback(data or "")
    return bool(act and did is not None)


def parse_editor_callback(data: str) -> tuple[str, int | None]:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "editor":
        return "", None
    act, sid = parts[1], parts[2]
    if act not in (CB_APPROVE, CB_EDIT, CB_REJECT, CB_EXPIRED):
        return "", None
    try:
        return act, int(sid)
    except ValueError:
        return "", None


def build_editor_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать",
                    callback_data=f"editor:{CB_APPROVE}:{draft_id}",
                ),
                InlineKeyboardButton(
                    text="✏️ Редактировать",
                    callback_data=f"editor:{CB_EDIT}:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"editor:{CB_REJECT}:{draft_id}",
                ),
                InlineKeyboardButton(
                    text="🕐 Устарело",
                    callback_data=f"editor:{CB_EXPIRED}:{draft_id}",
                ),
            ],
        ]
    )


def _reject_list(prefs: dict[str, str]) -> list[str]:
    raw = (prefs.get(PREF_REJECT_HINTS) or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return [x.strip() for x in raw.split("|") if x.strip()]


def append_reject_hint(memory: ChatMemory, user_id: int, hint: str) -> None:
    hint = (hint or "").strip()[:200]
    if not hint:
        return
    cur = _reject_list(_prefs(memory, user_id))
    cur.append(hint)
    cur = cur[-40:]
    memory.update_style_preferences(user_id, {PREF_REJECT_HINTS: json.dumps(cur, ensure_ascii=False)})
    rc = int(_prefs(memory, user_id).get(PREF_REJECT_COUNT, "0") or 0)
    memory.update_style_preferences(user_id, {PREF_REJECT_COUNT: str(rc + 1)})
    if hint.lower().startswith(URL_REJECT_PREFIX.lower()):
        rest = hint[len(URL_REJECT_PREFIX) :].strip()
        ban_key = _reject_host_key_for_url(rest)
        if ban_key:
            _bump_host_reject_count(memory, user_id, ban_key)
            logger.info(
                "content_editor reject: user_id=%s url_hint bump_ban_key=%r (не netloc t.me для TG)",
                user_id,
                ban_key,
            )
    logger.info(
        "content_editor reject: user_id=%s kind=%s value=%r",
        user_id,
        "url" if hint.lower().startswith(URL_REJECT_PREFIX.lower()) else "text",
        hint[:160],
    )


def bump_approve(memory: ChatMemory, user_id: int) -> None:
    ac = int(_prefs(memory, user_id).get(PREF_APPROVE_COUNT, "0") or 0)
    memory.update_style_preferences(user_id, {PREF_APPROVE_COUNT: str(ac + 1)})


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _is_blocked_source_domain(url: str, blocked_domains: frozenset[str]) -> bool:
    """True, если host URL совпадает с записью из blocked_domains или является её поддоменом / родителем в списке."""
    raw = _host(url)
    if not raw:
        return False
    host = raw.split(":", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    if host in blocked_domains:
        return True
    for blocked in blocked_domains:
        if host.endswith(f".{blocked}"):
            return True
    parts = host.split(".")
    if len(parts) >= 2:
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            if parent in blocked_domains:
                return True
    return False


def _reject_host_key_for_url(url: str) -> str:
    """Ключ счётчика отказов: обычный сайт — домен (vesti.ru); посты Telegram — tg:username, не t.me."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    if "://" not in u and (u.startswith("t.me/") or u.startswith("telegram.me/")):
        u = "https://" + u
    try:
        p = urlparse(u)
        net = (p.netloc or "").lower().split(":")[0]
        path = (p.path or "").strip("/")
        if net in ("t.me", "telegram.me", "telegram.dog"):
            parts = [x for x in path.split("/") if x]
            if not parts:
                return net
            first = parts[0].lower()
            if first == "s" and len(parts) >= 2:
                un = re.sub(r"[^a-z0-9_]", "", parts[1])
                return f"tg:{un}" if un else net
            un = re.sub(r"[^a-z0-9_]", "", first)
            return f"tg:{un}" if un else net
        return net.lstrip("www.")
    except Exception:
        return ""


def _topics_pair_from_capture(raw: str) -> tuple[str, str]:
    t = (raw or "").strip()
    if not t:
        return "", ""
    # В /editor_prefs директива "темы:" должна сохранять весь CSV как список тем.
    # Уточнение к поиску задаётся отдельной директивой "источники:", а не хвостом после первой запятой.
    return t[:800], ""


def _normalize_user_topics(raw_topics: str) -> list[str]:
    parts = re.split(r"[,\n;|/]+", (raw_topics or "").strip())
    out: list[str] = []
    for p in parts:
        topic = p.strip().lower().replace("ё", "е")
        topic = re.sub(r"\s+", " ", topic)
        if topic and topic not in out:
            out.append(topic)
    return out


def topics_list_from_pref(pref_topics: str) -> list[str]:
    """Упорядоченный список тем из значения PREF_TOPICS."""
    return _normalize_user_topics(pref_topics)


def topics_pref_from_list(topics: list[str]) -> str:
    """Строка для SQLite user_preferences (обрезка как у прочих prefs)."""
    s = ", ".join(topics)
    return s[:800]


def merge_topics_into_pref(old_pref: str, tokens: list[str]) -> tuple[str, list[str]]:
    """Добавляет темы; возвращает (новое значение pref, список реально добавленных)."""
    cur = _normalize_user_topics(old_pref)
    added: list[str] = []
    for t in tokens:
        if not t:
            continue
        if t not in cur:
            cur.append(t)
            added.append(t)
    return topics_pref_from_list(cur), added


def remove_topics_from_pref(old_pref: str, tokens: list[str]) -> tuple[str, list[str]]:
    """Удаляет темы по нормализованному совпадению."""
    cur = _normalize_user_topics(old_pref)
    remove_set = {t for t in _normalize_user_topics(", ".join(tokens)) if t}
    if not remove_set:
        return topics_pref_from_list(cur), []
    removed: list[str] = []
    new_list: list[str] = []
    for t in cur:
        if t in remove_set:
            removed.append(t)
        else:
            new_list.append(t)
    return topics_pref_from_list(new_list), removed


def split_topic_command_tokens(blob: str) -> list[str]:
    """Разбор хвоста после `add` / `remove`: запятые, пробелы, переводы строк."""
    parts = re.split(r"[,\n;|/\s]+", (blob or "").strip())
    out: list[str] = []
    for p in parts:
        topic = p.strip().lower().replace("ё", "е")
        topic = re.sub(r"\s+", " ", topic)
        if topic:
            out.append(topic)
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def format_topics_settings_message(pref_topics: str | None) -> str:
    """Текущее состояние тем для ответа пользователю после /topics."""
    raw = (pref_topics or "").strip()
    lst = topics_list_from_pref(raw)
    if not lst:
        return "⚙️ Темы поиска: пусто (при подборе может использоваться значение по умолчанию)."
    return f"⚙️ Темы поиска ({len(lst)}): " + ", ".join(lst)


def sources_list_from_pref(pref_sources: str) -> list[str]:
    """Упорядоченный список фрагментов из значения PREF_SOURCES (как у тем)."""
    return _normalize_user_topics(pref_sources)


def merge_sources_into_pref(old_pref: str, tokens: list[str]) -> tuple[str, list[str]]:
    return merge_topics_into_pref(old_pref, tokens)


def remove_sources_from_pref(old_pref: str, tokens: list[str]) -> tuple[str, list[str]]:
    return remove_topics_from_pref(old_pref, tokens)


def split_source_command_tokens(blob: str) -> list[str]:
    return split_topic_command_tokens(blob)


def format_sources_settings_message(pref_sources: str | None) -> str:
    """Текущее уточнение к поиску (PREF_SOURCES) для ответа после /sources."""
    raw = (pref_sources or "").strip()
    lst = sources_list_from_pref(raw)
    if not lst:
        return "⚙️ Уточнение к поиску: пусто (при подборе остаётся только строка тем)."
    return f"⚙️ Уточнение к поиску ({len(lst)}): " + ", ".join(lst)


def primary_search_window_days_from_prefs(prefs: dict[str, str]) -> int:
    """Сколько дней «назад» передавать в Tavily на первой попытке (по умолчанию как в коде: 2)."""
    raw = (prefs.get(PREF_SEARCH_WINDOW_DAYS) or "").strip()
    if not raw:
        return DEFAULT_SEARCH_WINDOW_DAYS
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_SEARCH_WINDOW_DAYS
    return max(MIN_SEARCH_WINDOW_DAYS, min(n, MAX_SEARCH_WINDOW_DAYS))


def tavily_fallback_days_after_empty(primary: int) -> int:
    """Вторая попытка Tavily, если первая вернула 0 результатов (раньше: 2 → 5)."""
    return min(max(primary, 5), MAX_SEARCH_WINDOW_DAYS)


def format_search_window_settings_message(prefs: dict[str, str]) -> str:
    """Текст сводки для /searchwindow (Tavily days + fallback)."""
    raw = (prefs.get(PREF_SEARCH_WINDOW_DAYS) or "").strip()
    p = primary_search_window_days_from_prefs(prefs)
    fb = tavily_fallback_days_after_empty(p)
    if not raw:
        return (
            f"⚙️ Окно поиска (Tavily): 1-я попытка — {p} дн., 2-я (если пусто) — {fb} дн. "
            "— по умолчанию."
        )
    return (
        f"⚙️ Окно поиска (Tavily): 1-я попытка — {p} дн., 2-я (если пусто) — {fb} дн. "
        "— задано вручную."
    )


def _score_matches_user_topics(
    score_map: dict[str, int], user_topics: list[str], post_text_low: str
) -> tuple[bool, str]:
    if not score_map or not user_topics:
        return False, "no_score_or_topics"
    score_categories = [k.lower().replace("ё", "е") for k in score_map.keys()]
    for ut in user_topics:
        if ut in score_categories:
            return True, f"user_topic_eq_category:{ut}"
        for cat in score_categories:
            if ut in cat or cat in ut:
                return True, f"user_topic_partial_category:{ut}->{cat}"
    # "стримы" -> twitch and similar topical aliases via post text relevance
    if any("twitch" in c for c in score_categories):
        for ut in user_topics:
            if any(x in ut for x in ("стрим", "твич", "stream", "twitch")):
                return True, f"user_topic_stream_alias:{ut}"
    for ut in user_topics:
        if ut and ut in post_text_low:
            return True, f"user_topic_in_post_text:{ut}"
    return False, "score_no_topic_match"


def detect_primary_category(title: str, snippet: str) -> str:
    """Определяет ведущую категорию для материала (или 'other')."""
    text = f"{title} {snippet}".strip()
    if not text or not USE_PATTERNS:
        return "other"
    try:
        score_map = score_text(text)
    except Exception:
        return "other"
    if not score_map:
        return "other"
    return next(iter(score_map.keys()), "other") or "other"


def estimate_feedback_quality_score(action: str) -> int:
    """Оценка качества 1..10 для fallback, если пользователь не дал числовой рейтинг."""
    act = (action or "").strip().lower()
    if act == "approved":
        return 8
    if act == "edited":
        return 6
    if act == "rejected":
        return 3
    if act == "expired_content":
        return 2
    return 5


def _feedback_signal_from_row(action: str, quality_score: int | None) -> float:
    """Перевод feedback в сигнал -1..1 для preference."""
    base = {
        # Один апрув не должен доминировать: слабее базовый сигнал, чем раньше (1.0).
        "approved": 0.4,
        "edited": 0.35,
        "rejected": -1.0,
        "expired_content": -0.6,
    }.get((action or "").strip().lower(), 0.0)
    if quality_score is None:
        return base
    q = max(1, min(10, int(quality_score)))
    q_centered = (q - 5.5) / 4.5  # 1->-1, 10->1
    return max(-1.0, min(1.0, 0.7 * base + 0.3 * q_centered))


def category_preferences_for_user(
    user_id: int,
    *,
    window_size: int = 20,
    decay_rate: float = 0.95,
) -> dict[str, float]:
    """
    Скользящее окно с затуханием:
    preference[category] = sum(signal_i * decay^i) / sum(decay^i),
    где i=0 — самый свежий feedback.
    """
    rows = get_recent_draft_feedback_window(user_id, limit=window_size)
    if not rows:
        return {}
    decay = max(0.5, min(0.999, float(decay_rate)))
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    for i, r in enumerate(rows):
        category = (str(r.get("category") or "").strip().lower() or "other")[:64]
        weight = decay**i
        signal = _feedback_signal_from_row(
            str(r.get("action") or ""),
            int(r["quality_score"]) if r.get("quality_score") is not None else None,
        )
        num[category] = num.get(category, 0.0) + signal * weight
        den[category] = den.get(category, 0.0) + weight
    out: dict[str, float] = {}
    for cat, d in den.items():
        if d > 0:
            out[cat] = num.get(cat, 0.0) / d
    return out


def recent_draft_categories(
    user_id: int,
    *,
    limit: int = 5,
) -> list[str]:
    """Категории последних черновиков (draft/posted/rejected) для diversity-механики."""
    uid = str(user_id)
    lim = max(1, min(30, int(limit)))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT content
                FROM draft_posts
                WHERE user_id=?
                  AND status IN ('draft', 'posted', 'rejected')
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
    except Exception as exc:
        logger.exception("recent_draft_categories: %s", exc)
        return []
    out: list[str] = []
    for r in rows:
        txt = str(r["content"] or "").strip()
        if not txt:
            continue
        title = txt.splitlines()[0].strip() if txt.splitlines() else txt[:120]
        out.append(detect_primary_category(title, txt[:400]))
    return out


def reset_editor_reject_state(memory: ChatMemory, user_id: int) -> tuple[int, int]:
    """Сбрасывает url/text отказы и счётчики tg:/доменов. Возвращает (число старых hints, число ключей счётчиков)."""
    prefs = _prefs(memory, user_id)
    n_hints = len(_reject_list(prefs))
    n_keys = len(_load_host_reject_counts(prefs))
    memory.update_style_preferences(
        user_id,
        {
            PREF_REJECT_HINTS: json.dumps([], ensure_ascii=False),
            PREF_HOST_REJECT_COUNTS: json.dumps({}, ensure_ascii=False),
            PREF_REJECT_COUNT: "0",
        },
    )
    logger.info(
        "editor_reset_rejects: user_id=%s сброшено hint_entries=%s host_or_tg_keys=%s",
        user_id,
        n_hints,
        n_keys,
    )
    return n_hints, n_keys


def count_drafts(user_id: int, status: str = "draft") -> int:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM draft_posts WHERE user_id=? AND status=?",
                (uid, status),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_drafts: %s", exc)
        return 999


def _expire_due_drafts(user_id: int) -> int:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            n = conn.execute(
                """
                UPDATE draft_posts
                SET status='expired'
                WHERE user_id=?
                  AND status='draft'
                  AND expires_at IS NOT NULL
                  AND datetime(expires_at) <= datetime('now')
                """,
                (uid,),
            ).rowcount
            conn.commit()
        if n > 0:
            logger.info("draft_expire: user_id=%s auto_expired=%s", user_id, n)
        return int(n)
    except Exception as exc:
        logger.exception("draft_expire: %s", exc)
        return 0


def is_draft_expired(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    exp = (row.get("expires_at") or "").strip()
    if not exp:
        return False
    try:
        with get_connection() as conn:
            r = conn.execute(
                "SELECT datetime(?) <= datetime('now') AS is_exp",
                (exp,),
            ).fetchone()
        return bool(r and int(r["is_exp"]) == 1)
    except Exception:
        return False


def _draft_kind_for_insert(source_url: str | None) -> str:
    # Для текущего этапа считаем новостными черновики, пришедшие из URL/веб-источников.
    return "news" if (source_url or "").strip() else "other"


def insert_draft(
    user_id: int,
    channel_id: str,
    content: str,
    source_url: str | None,
    *,
    deadline_hours: int | None = None,
) -> tuple[bool, int | str]:
    uid = str(user_id)
    pending = count_drafts(user_id, "draft")
    if pending >= MAX_PENDING_UNAPPROVED_DRAFTS:
        logger.info(
            "insert_draft: отказ user_id=%s pending=%s >= limit=%s",
            user_id,
            pending,
            MAX_PENDING_UNAPPROVED_DRAFTS,
        )
        return (
            False,
            f"Черновиков уже {MAX_PENDING_UNAPPROVED_DRAFTS} — утверди или отмени старые, иначе я захламлюсь как черновик в столе 📎",
        )
    body = (content or "").strip()[:MAX_POST_CHARS]
    dl_h = deadline_hours or DEFAULT_DRAFT_DEADLINE_HOURS
    if dl_h not in ALLOWED_DRAFT_DEADLINE_HOURS:
        dl_h = DEFAULT_DRAFT_DEADLINE_HOURS
    kind = _draft_kind_for_insert(source_url)
    ttl_h = 24 if kind == "news" else 72
    # Пользователь может задать дедлайн, но не больше базового TTL по типу материала.
    ttl_h = max(1, min(ttl_h, dl_h))
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO draft_posts (user_id, channel_id, content, source_url, status, expires_at)
                VALUES (?, ?, ?, ?, 'draft', datetime('now', ?))
                """,
                (uid, channel_id, body, source_url or None, f"+{ttl_h} hours"),
            )
            conn.commit()
            return True, int(cur.lastrowid)
    except Exception as exc:
        logger.exception("insert_draft: %s", exc)
        return False, str(exc)


def get_draft(user_id: int, draft_id: int) -> dict[str, Any] | None:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at, media_url, expires_at
                FROM draft_posts WHERE id=? AND user_id=?
                """,
                (draft_id, uid),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.exception("get_draft: %s", exc)
        return None


def update_draft_content(user_id: int, draft_id: int, new_content: str) -> bool:
    uid = str(user_id)
    body = (new_content or "").strip()[:MAX_POST_CHARS]
    try:
        with get_connection() as conn:
            n = conn.execute(
                """
                UPDATE draft_posts
                SET content=?, was_edited=1
                WHERE id=? AND user_id=? AND status='draft'
                """,
                (body, draft_id, uid),
            ).rowcount
            conn.commit()
        return n > 0
    except Exception as exc:
        logger.exception("update_draft_content: %s", exc)
        return False


def set_draft_status(
    user_id: int,
    draft_id: int,
    status: str,
    *,
    set_approved_at: bool = False,
) -> bool:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            if set_approved_at:
                n = conn.execute(
                    """
                    UPDATE draft_posts SET status=?, approved_at=CURRENT_TIMESTAMP
                    WHERE id=? AND user_id=?
                    """,
                    (status, draft_id, uid),
                ).rowcount
            else:
                n = conn.execute(
                    "UPDATE draft_posts SET status=? WHERE id=? AND user_id=?",
                    (status, draft_id, uid),
                ).rowcount
            conn.commit()
        return n > 0
    except Exception as exc:
        logger.exception("set_draft_status: %s", exc)
        return False


def get_latest_draft(user_id: int, status: str = "draft") -> dict[str, Any] | None:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at, media_url, expires_at
                FROM draft_posts WHERE user_id=? AND status=?
                ORDER BY id DESC LIMIT 1
                """,
                (uid, status),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.exception("get_latest_draft: %s", exc)
        return None


def get_oldest_draft(user_id: int, status: str = "draft") -> dict[str, Any] | None:
    """Самый старый черновик в очереди (FIFO для /drafts)."""
    uid = str(user_id)
    try:
        if status == "draft":
            _expire_due_drafts(user_id)
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at, media_url, expires_at
                FROM draft_posts
                WHERE user_id=?
                  AND status=?
                  AND (
                    expires_at IS NULL
                    OR datetime(expires_at) > datetime('now')
                  )
                ORDER BY id ASC LIMIT 1
                """,
                (uid, status),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.exception("get_oldest_draft: %s", exc)
        return None


def hint_for_reject_from_draft(row: dict[str, Any]) -> str:
    """Запоминаем конкретный URL отказа (префикс url:). Счётчик жёсткого бана: tg:username для t.me, иначе домен."""
    u = str(row.get("source_url") or "").strip()
    if u:
        nu = _norm_cmp_url(u)
        return f"{URL_REJECT_PREFIX}{nu}"[:200]
    return (str(row.get("content") or ""))[:48].replace("\n", " ").strip()


_EDITOR_SERVICE_LINES_EXACT = frozenset(
    {
        "⚡ Редактор: черновик требует проверки — возможно мало фактуры",
        "✅ Источник проверен",
        "⚠️ Источник не верифицирован",
    }
)


def channel_publish_text_from_draft_body(body: str) -> str:
    """Текст для публикации в канал: без служебных строк, которые показываются только в ЛС."""
    lines_out: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped in _EDITOR_SERVICE_LINES_EXACT:
            continue
        if stripped.startswith("🔗 Источник в базе:"):
            continue
        lines_out.append(line)
    out = "\n".join(lines_out).strip()
    if not out:
        return (body or "").strip()
    return out


def draft_dm_text(row: dict[str, Any]) -> str:
    sid = row.get("source_url") or ""
    head = "✍️ Черновик для канала @kriptogeograph — глянь и реши судьбу поста:\n\n"
    body = str(row.get("content") or "")
    tail = ""
    if sid:
        tail = f"\n\n🔗 Источник в базе: {sid}"
    msg = head + body + tail
    if len(msg) > 4090:
        msg = msg[:4070] + "\n…"
    return msg


@dataclass
class DraftPick:
    title: str
    url: str
    snippet: str
    from_telegram: bool = False
    telegram_display: str = ""
    source_channel_username: str = ""


def _reject_hints_for_tavily_query(rejects: list[str]) -> list[str]:
    out: list[str] = []
    for h in rejects:
        hs = (h or "").strip()
        if not hs or hs.lower().startswith(URL_REJECT_PREFIX.lower()):
            continue
        out.append(hs)
    return out[:8]


def _build_reject_filters(
    rejects: list[str], prefs: dict[str, str]
) -> tuple[set[str], frozenset[str], frozenset[str], list[str]]:
    reject_urls: set[str] = set()
    for h in rejects:
        hl = (h or "").strip()
        if hl.lower().startswith(URL_REJECT_PREFIX.lower()):
            ru = hl[len(URL_REJECT_PREFIX) :].strip()
            if ru:
                reject_urls.add(_norm_cmp_url(ru))
    counts = _load_host_reject_counts(prefs)
    hard_hosts = frozenset({h for h, c in counts.items() if c >= HOST_HARD_REJECT_THRESHOLD})
    soft_hosts: set[str] = set()
    kw: list[str] = []
    for h in rejects:
        hl = (h or "").strip()
        if not hl or hl.lower().startswith(URL_REJECT_PREFIX.lower()):
            continue
        if _is_domain_only_hint(hl):
            ho = hl.lower().lstrip("www.")
            if ho not in hard_hosts:
                soft_hosts.add(ho)
        elif len(hl) > 2:
            kw.append(hl)
    return reject_urls, hard_hosts, frozenset(soft_hosts), kw


def _snippet_from_tavily_item(it: dict[str, Any]) -> str:
    """Текст для черновика: content, иначе короткий кусок raw_content (Tavily иногда оставляет content пустым)."""
    text = (it.get("content") or "").strip().replace("\n", " ")
    if text:
        return text
    raw = it.get("raw_content")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().replace("\n", " ")[:1200]
    return ""


_TAVILY_MAX_CANDIDATE_AGE_DAYS = 30
_MIN_WEB_EFFECTIVE_SCORE = 5.0


def _tavily_published_utc(it: dict[str, Any]) -> datetime | None:
    """Дата публикации из ответа Tavily (ISO) или None."""
    pub = it.get("published_date") or it.get("published")
    if not pub or not isinstance(pub, str):
        return None
    s = pub.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _domain_matches(host: str, domains: frozenset[str]) -> bool:
    h = (host or "").split(":", 1)[0].lower().lstrip("www.")
    if not h:
        return False
    for d in domains:
        if h == d or h.endswith(f".{d}"):
            return True
    return False


def _freshness_terms_for_query(topics: str, sources: str) -> list[str]:
    """Ключевые слова свежести для Tavily в зависимости от языка темы."""
    text = f"{topics} {sources}".strip()
    has_cyrillic = bool(re.search(r"[а-яё]", text.lower()))
    if has_cyrillic:
        return ["сегодня", "вчера", "последние новости", "breaking"]
    return ["today", "yesterday", "latest news", "breaking"]


def _pick_draft_item(
    agent: LLMAgent,
    prefs: dict[str, str],
    user_id: int,
    excluded_urls: set[str] | None = None,
) -> DraftPick | None:
    """Подбор материала: Tavily (web), публичные TG-каналы (t.me/s), фильтры отказов."""
    from app.tg_feed_fetcher import fetch_many_channels

    mode = get_source_mode(prefs)
    excl_raw = {(u or "").strip().lower() for u in (excluded_urls or set())}
    excl_norm = {_norm_cmp_url(u).lower() for u in (excluded_urls or set()) if u}
    topics = (prefs.get(PREF_TOPICS) or "актуальные новости").strip()
    user_topics = _normalize_user_topics(topics)
    sources = (prefs.get(PREF_SOURCES) or "").strip()
    rejects = _reject_list(prefs)
    reject_urls, hard_hosts, soft_hosts, kw_strings = _build_reject_filters(rejects, prefs)
    channel_quality = get_channel_quality_snapshot()
    blocked_src = frozenset(agent.config.blocked_search_domains)
    cfg = getattr(agent, "config", None)
    feedback_window = int(getattr(cfg, "feedback_window_size", 20) or 20)
    decay_rate = float(getattr(cfg, "feedback_decay_rate", 0.95) or 0.95)
    same_cat_window = int(getattr(cfg, "diversity_same_category_window", 3) or 3)
    narrow_mix_window = int(getattr(cfg, "diversity_narrow_mix_window", 5) or 5)
    narrow_mix_categories = int(getattr(cfg, "diversity_narrow_mix_categories", 2) or 2)
    same_cat_penalty = float(getattr(cfg, "diversity_same_category_penalty", 0.30) or 0.30)
    other_cat_bonus = float(getattr(cfg, "diversity_other_categories_bonus", 0.20) or 0.20)
    cat_preferences = category_preferences_for_user(
        user_id,
        window_size=feedback_window,
        decay_rate=decay_rate,
    )
    feedback_cat_counts = get_feedback_category_counts_in_window(user_id, feedback_window)
    min_pref = int(getattr(cfg, "feedback_min_count_for_full_pref", 3) or 3)
    max_pref_gain = float(getattr(cfg, "feedback_pref_max_gain", 0.10) or 0.10)
    pref_gain_per_unit = float(getattr(cfg, "feedback_pref_gain_per_unit", 0.15) or 0.15)
    novelty_bonus = float(getattr(cfg, "feedback_novelty_bonus", 0.05) or 0.05)
    novelty_recent_n = int(getattr(cfg, "novelty_recent_drafts", 10) or 10)
    novelty_cat_set = set(
        recent_draft_categories(user_id, limit=max(1, novelty_recent_n))
    )
    recent_cats_same = recent_draft_categories(user_id, limit=same_cat_window)
    recent_cats_mix = recent_draft_categories(user_id, limit=narrow_mix_window)
    same_streak_cat = (
        recent_cats_same[0]
        if len(recent_cats_same) == same_cat_window
        and len(set(recent_cats_same)) == 1
        else ""
    )
    narrow_mix_active = (
        len(recent_cats_mix) >= narrow_mix_window
        and len(set(recent_cats_mix)) <= max(1, narrow_mix_categories)
    )
    if cat_preferences:
        prefs_log = ", ".join(
            f"preference[{k}]={v:.2f}" for k, v in sorted(cat_preferences.items())
        )
    else:
        prefs_log = "preference[other]=0.00"
    logger.info(
        "Pick draft prefs: user_id=%s %s feedback_counts=%s min_pref=%s "
        "diversity_same_streak=%r diversity_narrow_mix=%s recent_mix=%s novelty_recent_cats=%s",
        user_id,
        prefs_log,
        feedback_cat_counts,
        min_pref,
        same_streak_cat or "",
        narrow_mix_active,
        recent_cats_mix[:narrow_mix_window],
        sorted(novelty_cat_set),
    )

    candidates: list[dict[str, Any]] = []

    if mode in ("web", "both") and agent.tavily:
        promo_domains = list(WEB_PROMO_DOMAINS) + list(blocked_src)
        logger.debug(
            "Pick draft: Tavily exclude_domains (%s): %s",
            len(promo_domains),
            ", ".join(promo_domains),
        )
        now = datetime.utcnow()
        date_hint = f"{now.year}"
        raw_topics_pref = (prefs.get(PREF_TOPICS) or "").strip()
        topics_list = (
            [t.strip() for t in raw_topics_pref.split(",") if t.strip()]
            if raw_topics_pref
            else []
        )
        primary_days = primary_search_window_days_from_prefs(prefs)
        fallback_days = tavily_fallback_days_after_empty(primary_days)

        seen_web_urls: set[str] = set()
        _TAVILY_WEB_CAP = 15

        def _web_candidate_count() -> int:
            return sum(1 for c in candidates if not c.get("from_tg"))

        def _append_tavily_items_to_candidates(res: dict | None) -> int:
            """Добавляет результаты Tavily в candidates (дедуп по URL, лимит веб-кандидатов)."""
            if not res or not isinstance(res.get("results"), list):
                return 0
            cutoff_inner = datetime.now(timezone.utc) - timedelta(
                days=_TAVILY_MAX_CANDIDATE_AGE_DAYS
            )
            n = 0
            for it in res["results"]:
                if _web_candidate_count() >= _TAVILY_WEB_CAP:
                    break
                if not isinstance(it, dict):
                    continue
                pub_dt = _tavily_published_utc(it)
                if pub_dt is not None and pub_dt < cutoff_inner:
                    logger.debug(
                        "skip tavily_old_news published=%s url=%s",
                        it.get("published_date") or it.get("published"),
                        (it.get("url") or "").strip(),
                    )
                    continue
                url_raw = (it.get("url") or "").strip()
                if not url_raw:
                    continue
                host = _host(url_raw)
                if _domain_matches(host, LOW_QUALITY_WEB_DOMAINS):
                    logger.debug(
                        "skip tavily_low_quality_domain host=%s url=%s",
                        host or "?",
                        url_raw[:220],
                    )
                    continue
                url_key = _norm_cmp_url(url_raw).lower()
                if url_key in seen_web_urls:
                    continue
                snippet = _snippet_from_tavily_item(it)[:800]
                if len(snippet.strip()) < MIN_WEB_SNIPPET_LEN:
                    logger.debug(
                        "skip tavily_short_snippet len=%s url=%s",
                        len(snippet.strip()),
                        url_raw[:220],
                    )
                    continue
                seen_web_urls.add(url_key)
                candidates.append(
                    {
                        "title": (it.get("title") or "Без заголовка").strip(),
                        "url": url_raw,
                        "snippet": snippet,
                        "from_tg": False,
                        "tg_disp": "",
                        "source_channel_username": "",
                    }
                )
                n += 1
            return n

        if not topics_list:
            freshness_terms = _freshness_terms_for_query(topics, sources)
            freshness_hint = " ".join(freshness_terms)
            q = f"{topics} {sources} {freshness_hint} {date_hint}".strip()
            tail = _reject_hints_for_tavily_query(rejects)
            if tail:
                q += ". Исключай или обходи материалы, связанные с: " + ", ".join(tail)
            result = agent._tavily_search(
                q[:400],
                max_results=6,
                days=primary_days,
                exclude_domains=promo_domains,
            )
            result_items = result.get("results") if isinstance(result, dict) else None
            if isinstance(result_items, list) and len(result_items) == 0:
                logger.debug(
                    "Pick draft: Tavily вернул 0 результатов с days=%s, fallback на days=%s, query=%r",
                    primary_days,
                    fallback_days,
                    q[:220],
                )
                result = agent._tavily_search(
                    q[:400],
                    max_results=6,
                    days=fallback_days,
                    exclude_domains=promo_domains,
                )
            if result and isinstance(result.get("results"), list):
                _append_tavily_items_to_candidates(result)
            else:
                logger.warning(
                    "Pick draft: web пустой или нет results (mode=%s, has_result=%s)",
                    mode,
                    bool(result),
                )
        else:
            for topic in topics_list[:5]:
                if _web_candidate_count() >= _TAVILY_WEB_CAP:
                    break
                q = f"новости {topic} {date_hint}".strip()
                result = agent._tavily_search(
                    q[:400],
                    max_results=3,
                    days=primary_days,
                    exclude_domains=promo_domains,
                )
                result_items = result.get("results") if isinstance(result, dict) else None
                if isinstance(result_items, list) and len(result_items) == 0:
                    logger.debug(
                        "Pick draft: Tavily вернул 0 результатов с days=%s, fallback на days=%s, query=%r",
                        primary_days,
                        fallback_days,
                        q[:220],
                    )
                    result = agent._tavily_search(
                        q[:400],
                        max_results=3,
                        days=fallback_days,
                        exclude_domains=promo_domains,
                    )
                n_added = _append_tavily_items_to_candidates(result)
                logger.info("Tavily multi-query: topic=%s results=%s", topic, n_added)
            if _web_candidate_count() == 0:
                logger.warning(
                    "Pick draft: web пустой или нет results (mode=%s, multi-topic)",
                    mode,
                )

    if mode in ("tg", "both"):
        user_tg_raw = (prefs.get(PREF_TG_CHANNELS) or "").strip()
        user_tg_list = _parse_username_csv(user_tg_raw)
        chans = _merged_tg_channel_names(prefs)
        n_ch = len(chans)
        head = 12
        preview = ",".join(f"@{x}" for x in chans[:head])
        tail = f" …(+{n_ch - head} каналов в лог не помещаю)" if n_ch > head else ""
        if user_tg_list:
            logger.info(
                "TG fetch: total_channels=%s из настроек пользователя, базовый список (первые %s): %s%s; "
                "порядок HTTP после shuffle — см. tg_feed: fetch_many_channels",
                n_ch,
                min(n_ch, head),
                preview,
                tail,
            )
        else:
            logger.info(
                "TG fetch: total_channels=%s по умолчанию, базовый список (первые %s): %s%s; "
                "порядок после shuffle — см. tg_feed: fetch_many_channels",
                n_ch,
                min(n_ch, head),
                preview,
                tail,
            )
        # Порядок HTTP к каналам задаётся в fetch_many_channels (random.shuffle).
        tg_posts = fetch_many_channels(chans, per_channel=2)
        logger.info(
            "TG fetch result: total_posts=%s from %s channels",
            len(tg_posts),
            n_ch,
        )
        for p in tg_posts:
            candidates.append(
                {
                    "title": (p.get("title") or "Пост из Telegram").strip(),
                    "url": (p.get("url") or "").strip(),
                    "snippet": ((p.get("content") or "").strip())[:800],
                    "from_tg": True,
                    "tg_disp": f"@{p.get('channel_username', '')}",
                    "source_channel_username": str(p.get("channel_username") or "").strip().lstrip("@").lower(),
                }
            )

    logger.info(
        "Pick draft: source_mode=%s кандидатов=%s (web+TG)",
        mode,
        len(candidates),
    )
    if not candidates:
        return None

    ranked: list[
        tuple[
            int,
            float,
            int,
            dict[str, Any],
            str,
            dict[str, int],
            dict[str, list[str]],
            float,
            float,
            str,
            float,
            float,
            float,
            float,
            float,
            int,
        ]
    ] = []
    for i, c in enumerate(candidates):
        url = (c.get("url") or "").strip()
        host = _host(url).replace("www.", "")
        soft_pen = 1 if host and host in soft_hosts else 0
        if soft_pen:
            logger.debug("Pick draft: мягкий приоритет host=%s (не бан, сортировка)", host)
        title = (c.get("title") or "").strip()
        content = (c.get("snippet") or "").strip()
        post_text = f"{title} {content}".strip()
        post_text_low = post_text.lower()
        cat_matches: dict[str, list[str]] = {}
        score_map: dict[str, int] = {}
        pattern_reason = ""
        total_score = 0
        if USE_PATTERNS:
            try:
                score_map = score_text(post_text)
                cat_matches = match_categories(post_text)
                total_score = sum(score_map.values())
                _, pattern_reason = _score_matches_user_topics(
                    score_map, user_topics, post_text_low
                )
            except Exception as exc:
                logger.debug("Pick draft: patterns runtime fallback reason=%s", exc)
                pattern_reason = "patterns_runtime_error"
                score_map = {}
                cat_matches = {}
                total_score = 0
        src_ch = str(c.get("source_channel_username") or "").strip().lower()
        approved_c, rejected_c = channel_quality.get(src_ch, (0, 0))
        quality_mult = 1.0 + (approved_c / max(1, rejected_c))
        breaking_mult = 1.0
        if BREAKING_PATTERN.search(post_text):
            breaking_mult = 3.0
            logger.debug("breaking news boost url=%r", (c.get("url") or "")[:140])
        category = detect_primary_category(title, content)
        pref_raw = cat_preferences.get(category, 0.0)
        n_fb = int(feedback_cat_counts.get(category, 0))
        if n_fb < min_pref:
            pref_gain = 0.0
            pref_mult = 1.0
        else:
            raw_delta = pref_gain_per_unit * pref_raw
            pref_gain = max(-max_pref_gain, min(max_pref_gain, raw_delta))
            pref_mult = 1.0 + pref_gain
        diversity_mult = 1.0
        if same_streak_cat and category == same_streak_cat:
            diversity_mult *= 1.0 - max(0.0, min(0.9, same_cat_penalty))
        elif narrow_mix_active and category not in set(recent_cats_mix):
            diversity_mult *= 1.0 + max(0.0, min(1.0, other_cat_bonus))
        novelty_mult = (1.0 + novelty_bonus) if category not in novelty_cat_set else 1.0
        effective_score = (
            float(total_score)
            * quality_mult
            * breaking_mult
            * pref_mult
            * diversity_mult
            * novelty_mult
        )
        ranked.append(
            (
                soft_pen,
                -effective_score,
                i,
                c,
                pattern_reason,
                score_map,
                cat_matches,
                quality_mult,
                breaking_mult,
                category,
                pref_raw,
                pref_mult,
                diversity_mult,
                pref_gain,
                novelty_mult,
                n_fb,
            )
        )
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))

    n_excluded = n_hard_host = n_url_rej = n_kw = n_no_url = n_blocked_source = n_low_score = 0
    for (
        soft_pen,
        _score_sort,
        _idx,
        c,
        pattern_reason,
        score_map,
        cat_matches,
        quality_mult,
        breaking_mult,
        category,
        pref_raw,
        pref_mult,
        diversity_mult,
        pref_gain,
        novelty_mult,
        n_fb,
    ) in ranked:
        url = (c.get("url") or "").strip()
        if not url:
            n_no_url += 1
            logger.debug("Pick draft: skip no_url")
            continue
        u_low = url.lower()
        cu = _norm_cmp_url(url).lower()
        if u_low in excl_raw or cu in excl_norm:
            n_excluded += 1
            logger.debug("Pick draft: skip excluded url=%r", url[:100])
            continue
        if _norm_cmp_url(url) in reject_urls:
            n_url_rej += 1
            logger.debug("Pick draft: skip точный url: отказ %r", url[:100])
            continue
        rj_key = _reject_host_key_for_url(url)
        if rj_key and rj_key in hard_hosts:
            n_hard_host += 1
            logger.debug(
                "Pick draft: skip жёсткий бан по ключу=%r (>= порога отказов), netloc=%s",
                rj_key,
                _host(url) or "?",
            )
            continue
        if _is_blocked_source_domain(url, blocked_src):
            n_blocked_source += 1
            raw_h = _host(url)
            host = raw_h.split(":", 1)[0].lower()
            if host.startswith("www."):
                host = host[4:]
            logger.debug("skip blocked_source_domain domain=%s url=%s", host, url[:200])
            continue
        if (not c.get("from_tg")) and (float(sum(score_map.values()) if USE_PATTERNS else 0) * quality_mult * breaking_mult < _MIN_WEB_EFFECTIVE_SCORE):
            n_low_score += 1
            logger.debug(
                "Pick draft: skip low_effective_score url=%r eff_score=%.2f threshold=%.2f",
                url[:120],
                float(sum(score_map.values()) if USE_PATTERNS else 0) * quality_mult * breaking_mult,
                _MIN_WEB_EFFECTIVE_SCORE,
            )
            continue
        title = (c.get("title") or "").strip() or "Без заголовка"
        content = (c.get("snippet") or "").strip()
        blob = (title + " " + content).lower()
        if _is_duplicate_by_title(title, user_id):
            logger.debug("Pick draft: skip semantic_duplicate title=%r", title[:140])
            n_kw += 1
            continue
        pattern_accept = False
        pattern_info = pattern_reason
        if USE_PATTERNS:
            pattern_accept, pattern_info = _score_matches_user_topics(score_map, user_topics, blob)
            logger.debug(
                "Pick draft: patterns evaluate url=%r accept=%s reason=%s scores=%s categories=%s",
                url[:100],
                pattern_accept,
                pattern_info,
                score_map,
                list(cat_matches.keys()),
            )
        skip_kw = False
        if USE_PATTERNS:
            if not pattern_accept:
                n_kw += 1
                logger.debug("Pick draft: skip patterns_not_relevant reason=%s", pattern_info)
                continue
        else:
            matched_bad = ""
            for bad in kw_strings:
                if bad and len(bad) > 2 and "." not in bad and bad.lower() in blob:
                    skip_kw = True
                    matched_bad = bad[:60]
                    break
            if skip_kw:
                n_kw += 1
                logger.debug("Pick draft: skip keyword %r", matched_bad)
                continue
        final_mult = pref_mult * diversity_mult * novelty_mult
        logger.info(
            "Pick draft: выбран url=%r reject_key=%s netloc=%s tg=%s soft_penalty=%s "
            "category=%s feedback_n=%s preference[%s]=%.3f pref_gain=%.3f pref_mult=%.3f "
            "novelty_mult=%.3f diversity_mult=%.3f diversity_penalty=%.3f final_mult=%.3f "
            "total_score=%s quality_mult=%.3f breaking_mult=%.1f",
            url[:120],
            rj_key or "?",
            _host(url) or "?",
            c.get("from_tg"),
            soft_pen,
            category,
            n_fb,
            category,
            pref_raw,
            pref_gain,
            pref_mult,
            novelty_mult,
            diversity_mult,
            (1.0 - diversity_mult) if diversity_mult < 1.0 else 0.0,
            final_mult,
            sum(score_map.values()) if USE_PATTERNS else 0,
            quality_mult,
            breaking_mult,
        )
        return DraftPick(
            title=title,
            url=url,
            snippet=content[:800],
            from_telegram=bool(c.get("from_tg")),
            telegram_display=str(c.get("tg_disp") or ""),
            source_channel_username=str(c.get("source_channel_username") or "").strip().lower(),
        )
    if (
        n_excluded == len(candidates)
        and n_hard_host == 0
        and n_url_rej == 0
        and n_kw == 0
        and n_no_url == 0
    ):
        logger.warning(
            "Pick draft: все %s кандидатов отброшены как excluded_urls "
            "(уже опубликовано/отклонено/висит черновик с тем же source_url) — расширь темы или подожди новых постов",
            len(candidates),
        )
    else:
        logger.warning(
            "Pick draft: ни один кандидат не подошёл (mode=%s, n=%s excluded=%s hard_host=%s "
            "url_reject=%s blocked_source=%s low_score=%s kw=%s no_url=%s)",
            mode,
            len(candidates),
            n_excluded,
            n_hard_host,
            n_url_rej,
            n_blocked_source,
            n_low_score,
            n_kw,
            n_no_url,
        )
    return None


def draft_post_from_snippet(
    agent: LLMAgent,
    user_id: int,
    title: str,
    snippet: str,
    url: str,
    topics: str,
    *,
    from_telegram: bool = False,
    telegram_channel_display: str = "",
) -> tuple[str, bool, str]:
    tg_overlay = ""
    if from_telegram:
        disp = telegram_channel_display.strip() or "@канал"
        tg_overlay = (
            "\nИсточник — пост из публичного Telegram-канала. Сохрани лёгкость и живость формулировок автора, "
            "но чуть упорядочь структуру под стиль Кузьмы.\n"
            f"В конце одна строка: «Источник: ТГ-канал {disp}» и короткая ссылка на пост ({url}).\n"
        )
    finance_overlay = ""
    if _draft_material_sounds_financial(topics, title, snippet):
        finance_overlay = DRAFT_FINANCE_OVERLAY
        logger.debug("Draft: включён финансовый дисклеймер (тема/сниппет похожи на рынок или вложения)")
    voice_overlay = build_voice_examples_overlay(user_id, limit=3)
    rules_overlay = build_editorial_rules_overlay(user_id)
    approved_n = _approved_posts_count(user_id)
    if approved_n >= 10:
        logger.info("editor_voice: user_id=%s voice сформирован (%s+ апрувов)", user_id, approved_n)
    user_block = (
        f"Темы пользователя: {topics}\n"
        f"Заголовок источника: {title}\n"
        f"Краткое содержание: {snippet}\n"
        f"URL: {url}\n"
    )
    raw = agent.run_raw_completion(
        system=DRAFT_SYSTEM + tg_overlay + finance_overlay + voice_overlay + rules_overlay,
        user=user_block,
        max_tokens=1200,
        temperature=min(0.82, getattr(agent, "_chat_temperature", 0.75) + 0.05),
    )
    text = (raw or "").strip()
    preview = text[:100].replace("\n", " ")
    logger.info("Draft from snippet: len=%s chars, preview=%r", len(text), preview)
    if not text:
        logger.error("Draft from snippet: GPT вернул пустой completion")
    related_context = _get_related_context(user_id, title, limit=20)
    if related_context and related_context not in text:
        text = _insert_after_source_line(text, related_context)
    verification_line = _source_verification_line(url, from_telegram=from_telegram)
    if verification_line and verification_line not in text:
        text = f"{text.rstrip()}\n\n{verification_line}".strip()
    ok_quality, quality_reason = _assess_draft_quality(text)
    if not ok_quality:
        logger.warning(
            "weak draft detected reason=%s url=%s",
            quality_reason,
            (url or "")[:300],
        )
    if len(text) > MAX_POST_CHARS:
        text = text[: MAX_POST_CHARS - 1] + "…"
    return text, ok_quality, quality_reason


def create_draft_from_search(
    agent: LLMAgent,
    memory: ChatMemory,
    user_id: int,
    *,
    excluded_urls: set[str] | None = None,
) -> tuple[bool, int | str, str | None]:
    if not is_editor_enabled(memory, user_id):
        return False, "Редактор выключен — жми /editor_start в этом чате, и я проснусь ✍️", None
    prefs = _prefs(memory, user_id)
    pending_n = count_drafts(user_id, "draft")
    if pending_n >= MAX_PENDING_UNAPPROVED_DRAFTS:
        logger.info(
            "create_draft_from_search: skip user_id=%s pending_drafts=%s >= limit=%s (не создаём новый)",
            user_id,
            pending_n,
            MAX_PENDING_UNAPPROVED_DRAFTS,
        )
        return (
            False,
            f"На подоконнике уже {MAX_PENDING_UNAPPROVED_DRAFTS} неразобранных черновиков — новый не создаю. "
            "Разгреби ✅/✏️/❌, потом снова /drafts или /drafts ещё 📎",
            None,
        )
    logger.info(
        "create_draft_from_search: user_id=%s pending_drafts=%s limit=%s — можно создавать новый",
        user_id,
        pending_n,
        MAX_PENDING_UNAPPROVED_DRAFTS,
    )
    mode = get_source_mode(prefs)
    if editor_needs_tavily(prefs) and not agent.tavily:
        if mode == "web":
            return (
                False,
                "Tavily не настроен — без веб-поиска я как журналист без интернета 🌐 Добавь TAVILY_API_KEY в .env",
                None,
            )
        logger.warning("create_draft: режим both, Tavily нет — пробую только TG")
    from_db = load_editor_exclude_source_urls(user_id)
    merged_excl: set[str] = set(from_db)
    if excluded_urls:
        merged_excl |= excluded_urls
    for u in get_recent_expired_urls(user_id, hours=24):
        u = (u or "").strip()
        if not u:
            continue
        merged_excl.add(u.lower())
        norm = _norm_cmp_url(u)
        if norm:
            merged_excl.add(norm.lower())
    logger.info(
        "Draft creation: user_id=%s checking against %s excluded source URLs "
        "(DB posted≤%sd + rejected≤%sd + pending drafts; +caller_extra=%s)",
        user_id,
        len(merged_excl),
        _EXCLUDE_POSTED_DAYS,
        _EXCLUDE_REJECTED_DAYS,
        len(excluded_urls or ()),
    )
    if merged_excl and logger.isEnabledFor(logging.DEBUG):
        sample = ", ".join(sorted(merged_excl)[:12])
        logger.debug("Draft creation excluded URLs sample: %s", sample[:500])
    topics = prefs.get(PREF_TOPICS) or "новости"
    deadline_h = draft_deadline_hours_from_prefs(prefs)
    cfg = getattr(agent, "config", None)
    auto_retry_weak = bool(getattr(cfg, "auto_retry_weak_drafts", False)) if cfg else False

    picked: DraftPick | None = None
    body = ""
    ok_quality = True
    quality_reason = "ok"
    prev_weak_body: str | None = None
    prev_weak_pick: DraftPick | None = None

    for attempt in range(2):
        picked = _pick_draft_item(agent, prefs, user_id, merged_excl)
        if not picked:
            if attempt == 1 and prev_weak_body is not None and prev_weak_pick is not None:
                picked = prev_weak_pick
                body = _append_weak_draft_marker(prev_weak_body)
                ok_quality = False
                quality_reason = "retry_no_pick"
                break
            if mode == "tg":
                return (
                    False,
                    "С публичных TG-каналов сейчас пусто — сеть, вёрстка t.me или смени список "
                    "«тгканалы:» в /editor_prefs. Веб не используется (источники:tg) 🛰️",
                    None,
                )
            return False, "Ничего подходящего не нашёл — расширь темы в /editor_prefs или попробуй позже 🔎", None

        body, ok_quality, quality_reason = draft_post_from_snippet(
            agent,
            user_id,
            picked.title,
            picked.snippet,
            picked.url,
            topics,
            from_telegram=picked.from_telegram,
            telegram_channel_display=picked.telegram_display,
        )
        if ok_quality:
            break
        if auto_retry_weak and attempt < 1:
            logger.warning(
                "weak draft: retry 1/2 reason=%s url=%s",
                quality_reason,
                (picked.url or "")[:300],
            )
            u = (picked.url or "").strip()
            if u:
                merged_excl.add(u.lower())
                nu = _norm_cmp_url(u)
                if nu:
                    merged_excl.add(nu.lower())
            prev_weak_body, prev_weak_pick = body, picked
            continue
        body = _append_weak_draft_marker(body)
        break

    if picked is None:
        return False, "Ничего подходящего не нашёл — расширь темы в /editor_prefs или попробуй позже 🔎", None

    logger.info(
        "create_draft_from_search: черновик после GPT, len=%s, source_url=%r tg=%s",
        len(body),
        (picked.url or "")[:120],
        picked.from_telegram,
    )
    ch = prefs.get(PREF_CHANNEL) or DEFAULT_EDITOR_CHANNEL_ID
    ok, res = insert_draft(
        user_id,
        ch,
        body,
        picked.url,
        deadline_hours=deadline_h,
    )
    if not ok:
        return False, str(res), None
    row = get_draft(user_id, int(res))
    if not row:
        return False, "Черновик создался, но не читается из базы — мистика БД 🫠", None
    return True, int(res), draft_dm_text(row)


def maybe_note_shorter_edit(memory: ChatMemory, user_id: int, old: str, new: str) -> None:
    if len(new) < len(old) * 0.82:
        memory.update_style_preferences(
            user_id,
            {"content_editor_response_short": "1"},
        )
