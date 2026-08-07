"""Редактор контента: Tavily + черновик → апрув в ЛС → публикация в канал @kriptogeograph."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Config
from app.database import (
    bump_pattern_usage,
    get_connection,
    get_active_rejection_patterns,
    get_draft_feedback_rejected_approved_counts,
    get_draft_feedback_slice,
    get_editorial_feedbacks_baseline,
    get_editorial_rules,
    get_feedbacks_count,
    get_feedback_category_counts_in_window,
    get_feedback_hard_reject_category_counts_in_window,
    get_recent_draft_feedback_window,
    get_recent_expired_urls,
    get_voice_training_examples,
    get_voice_negative_training_examples,
    count_voice_negative_training_examples,
    count_voice_training_examples,
    count_draft_posts_by_status,
    count_draft_posts_total,
    count_posted_drafts_unedited,
    count_user_rejection_patterns,
    count_draft_feedback_by_action,
    get_recent_voice_training_pairs,
    list_all_rejection_patterns,
    get_feedback_rejected_category_counts_in_window,
    list_user_drafts_by_status,
    save_editorial_rules,
    save_rejection_pattern,
    save_voice_training,
    get_rejection_pattern_by_id,
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
PREF_PICK_FAIL_STREAK = "content_editor_pick_fail_streak"
PREF_QUALITY_MIN_LEN_SLACK = "content_editor_quality_min_len_slack"
PREF_PROMO_BLOCKED_HOSTS = "content_editor_promo_blocked_hosts"

# Inline «выбор темы», если автопоиск не нашёл черновик
CALLBACK_DRAFT_TOPIC_PREFIX = "draft_topic"

# Причина отказа после ❌ (callback_data fbrej:<slug>:<draft_id>)
CALLBACK_REJECT_REASON_PREFIX = "fbrej"
REJECT_REASON_SLUG_SKIP = "skip"
REJECT_REASON_SLUGS = frozenset(
    {
        "not_interested",
        "weak_content",
        "bad_source",
        "promotional",
        REJECT_REASON_SLUG_SKIP,
    }
)

# Нормализация pattern_type из GPT при разборе отклонённых черновиков (один список — без дублей в коде).
ALLOWED_REJECTION_PATTERN_TYPES: frozenset[str] = frozenset(
    {
        "weak_content",
        "promotional",
        "bad_source",
        "not_interested",
        "editor_preference",
        "unknown",
    }
)

DRAFT_TOPIC_PICKER_INTRO = (
    "Не нашёл подходящих черновиков. Выбери тему для поиска:\n\n"
    "1️⃣ Технологии\n"
    "2️⃣ Наука\n"
    "3️⃣ Кино\n"
    "4️⃣ Музыка\n"
    "5️⃣ Свой вариант (напиши тему)\n\n"
    "Или расширь темы: /editor_prefs"
)

POPULAR_DRAFT_TOPIC_SLUGS: tuple[tuple[str, str], ...] = (
    ("tech", "1️⃣ Технологии"),
    ("science", "2️⃣ Наука"),
    ("movie", "3️⃣ Кино"),
    ("music", "4️⃣ Музыка"),
)

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
_EXCLUDE_POSTED_DAYS = 30
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
# Жёсткий бан домена/tg-ключа в подборе после N отказов с этим источником (см. _bump_host_reject_count).
HOST_HARD_REJECT_THRESHOLD = 3
URL_REJECT_PREFIX = "url:"
VESTI_CLEANUP_USER_ID = 504425191
LEARNING_OWNER_ID = 504425191
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
CB_SEEN = "s"  # уже видел — исключить URL, без отказа и фидбека

_pending_edit: dict[int, int] = {}
_pending_feedback: dict[int, dict[str, Any]] = {}
_pending_learning: dict[int, str] = {}

DRAFT_SYSTEM = (
    "Ты — Кузьма, редактор коротких постов для Telegram-канала @kriptogeograph. "
    "Темы задаёт пользователь — это могут быть новости, наука, шоу-бизнес, технологии, путешествия, спорт и что угодно ещё; "
    "не впихивай финансовую рамку, если материал не про деньги, рынки или вложения.\n"
    "Пиши в новостном стиле: факты, событие, что произошло и почему это важно.\n"
    "Запрещено: рекламные призывы, восторженные описания продуктов и маркетинговые формулировки "
    "вроде «лучший», «невероятный», «потрясающий».\n"
    "Разрешено: лёгкий юмор и живой язык, но основа текста — новостная фактура.\n"
    "Если источник написан рекламно, перепиши факты своими словами и не копируй его тон.\n"
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
    pd = int(getattr(cfg, "content_editor_exclude_posted_days", 30) or 30)
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
    """Примеры голоса: сначала обучение voice_training, затем апрувнутые посты."""
    uid = str(user_id)
    lim = max(3, min(5, int(limit or 3)))
    try:
        trained = get_voice_training_examples(uid, limit=lim)
        if trained:
            return [t[:900] for t in trained if t][:lim]
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
        "строго следуй стилю, тону и структуре этих примеров — это приоритет над базовыми инструкциями:\n"
        + "\n\n".join(lines)
        + "\n"
    )


def get_voice_overlay_priority(user_id: int, limit: int = 3) -> str:
    uid = str(user_id)
    lim = max(3, min(5, int(limit or 3)))
    trained = [t[:900] for t in get_voice_training_examples(uid, limit=lim) if t][:lim]
    if not trained:
        return ""
    lines = [f"{i + 1}) {txt}" for i, txt in enumerate(trained)]
    return (
        "\nВАЖНО: следующие примеры задают стиль канала — пиши точно в таком же тоне, "
        "с такой же лексикой и структурой.\n"
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


_MAX_STYLE_REJECTION_OVERLAY_CHARS = 3600

_PREF_LONG_MARKER = "Предпочитает развёрнутые материалы"
_PREF_SHORT_MARKER = "Предпочитает лаконичные посты"
_PREF_HEADERS_MARKER = "Предпочитает явную структуру с подзаголовками"


def patterns_for_rejection_gate(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Паттерны для отсева кандидатов: без «положительных» editor_preference."""
    out: list[dict[str, Any]] = []
    for p in patterns:
        pt = str(p.get("pattern_type") or "").strip().lower()
        if pt == "editor_preference":
            continue
        out.append(p)
    return out


def build_editor_style_rejection_overlay(user_id: int, *, limit: int = 10) -> str:
    """
    Блок для system prompt: что нравится / не нравится по паттернам отказов
    и негативным примерам voice_training.
    """
    uid = str(user_id)
    rows = get_active_rejection_patterns(uid, limit=max(6, min(int(limit), 16)))
    prefer: list[dict[str, Any]] = []
    avoid: list[dict[str, Any]] = []
    for r in rows:
        pt = str(r.get("pattern_type") or "").strip().lower()
        if pt == "editor_preference":
            prefer.append(r)
        else:
            avoid.append(r)
    neg_voice = get_voice_negative_training_examples(uid, limit=2)
    lines: list[str] = [
        "\nСТИЛЬ РЕДАКТОРА (профиль канала — учитывай при генерации черновика):\n",
    ]
    if prefer:
        lines.append("✅ Что нравится (на основе одобренных постов и явных предпочтений):\n")
        for p in prefer[:6]:
            d = str(p.get("pattern_description") or "").strip()
            if d:
                w = p.get("pattern_weight")
                extra = f" (вес {float(w):.1f})" if w is not None else ""
                lines.append(f"• {d[:700]}{extra}\n")
    if avoid:
        lines.append("\n❌ Что НЕ нравится (на основе отклонённых постов и паттернов отказов):\n")
        for p in avoid[:8]:
            d = str(p.get("pattern_description") or "").strip()
            if d:
                w = p.get("pattern_weight")
                extra = f" — вес {float(w):.1f}" if w is not None else ""
                lines.append(f"• {d[:700]}{extra}\n")
    if neg_voice:
        lines.append("\nПРИМЕРЫ «как не надо» (из отклонённых черновиков):\n")
        for nv in neg_voice[:3]:
            lines.append(f"— {nv[:900]}\n")
    if len(prefer) + len(avoid) == 0 and not neg_voice:
        return ""
    lines.append(
        "\nПри генерации нового черновика: следуй стилю одобренных постов (см. примеры выше в промпте) "
        "и ИЗБЕГАЙ формулировок и приёмов из списка «не нравится» и негативных примеров.\n"
    )
    block = "".join(lines).strip()
    if len(block) > _MAX_STYLE_REJECTION_OVERLAY_CHARS:
        block = block[: _MAX_STYLE_REJECTION_OVERLAY_CHARS - 1] + "…"
    return block + "\n" if block else ""


def maybe_add_negative_voice_on_rejection_pattern(
    user_id: int,
    pattern_id: int,
    rejected_draft_body: str,
    *,
    count_after_save: int,
) -> None:
    """После 3-го срабатывания одного паттерна отказа — негативный пример для контрастного обучения."""
    if count_after_save != 3:
        return
    row = get_rejection_pattern_by_id(int(pattern_id))
    if not row:
        return
    desc = str(row.get("pattern_description") or "").strip()
    if not desc:
        return
    draft_snip = (rejected_draft_body or "").strip()[:3500]
    if len(draft_snip) < 30:
        return
    instruction = (
        f"НЕ пиши так (фрагмент отклонённого черновика):\n{draft_snip}\n\n"
        f"Потому что: {desc[:1200]}"
    )
    save_voice_training(
        str(user_id),
        f"отказ: паттерн #{pattern_id}",
        instruction,
        is_negative=True,
    )


def maybe_infer_editor_preference_from_voice(user_id: int) -> None:
    """
    Если пользователь стабильно переписывает тексты в одну сторону (длина / структура),
    фиксируем это как pattern_type=editor_preference (не участвует в отсеве кандидатов, только в промпте).
    """
    uid = str(user_id)
    pairs = get_recent_voice_training_pairs(uid, limit=20)
    if len(pairs) < 5:
        return
    existing = list_all_rejection_patterns(uid)
    existing_desc = " ".join(str(p.get("pattern_description") or "") for p in existing)

    def _has(marker: str) -> bool:
        return marker in existing_desc

    longer = 0
    shorter = 0
    more_headers = 0
    for orig, rew in pairs:
        lo, lr = len(orig), len(rew)
        if lr >= lo + 180 and lr >= 420:
            longer += 1
        elif lo >= 160 and lr > 40 and lr <= int(lo * 0.72):
            shorter += 1
        oh = orig.count("\n##") + orig.count("\n###")
        rh = rew.count("\n##") + rew.count("\n###")
        if rh >= max(oh + 1, 2):
            more_headers += 1

    if longer >= 5 and not _has(_PREF_LONG_MARKER):
        save_rejection_pattern(
            uid,
            {
                "pattern_type": "editor_preference",
                "pattern_description": (
                    f"{_PREF_LONG_MARKER}: больше контекста, деталей и абзацев, "
                    "а не короткие сводки без конкретики."
                ),
                "problems": [],
                "requirements": ["Достаточная глубина, факты и несколько связных абзацев."],
                "keywords_to_avoid": [],
            },
            category=None,
        )
    if shorter >= 5 and not _has(_PREF_SHORT_MARKER):
        save_rejection_pattern(
            uid,
            {
                "pattern_type": "editor_preference",
                "pattern_description": (
                    f"{_PREF_SHORT_MARKER}: убирать воду, оставлять ядро сообщения."
                ),
                "problems": [],
                "requirements": ["Сжатая подача без лишних повторов."],
                "keywords_to_avoid": [],
            },
            category=None,
        )
    if more_headers >= 5 and not _has(_PREF_HEADERS_MARKER):
        save_rejection_pattern(
            uid,
            {
                "pattern_type": "editor_preference",
                "pattern_description": (
                    f"{_PREF_HEADERS_MARKER} (Markdown ##), чтобы текст был проще сканировать."
                ),
                "problems": [],
                "requirements": ["Логичные подзаголовки по смыслу блоков."],
                "keywords_to_avoid": [],
            },
            category=None,
        )


def _top_keys_from_counts(d: dict[str, int], *, n: int = 2) -> list[str]:
    if not d:
        return []
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in items[:n] if k and k != "other"]


def format_learning_style_stats_block(user_id: int) -> str:
    """Расширение /learning_stats: анти-предпочтения, паттерны, сводные цифры."""
    uid = str(user_id)
    win = 80
    patterns = get_active_rejection_patterns(uid, limit=8)
    prefer = [p for p in patterns if str(p.get("pattern_type") or "").lower() == "editor_preference"]
    avoid = [p for p in patterns if str(p.get("pattern_type") or "").lower() != "editor_preference"]
    neg_n = count_voice_negative_training_examples(uid)
    appr = count_draft_feedback_by_action(uid, "approved")
    rej = count_draft_feedback_by_action(uid, "rejected")
    p_total = count_user_rejection_patterns(uid)
    voice_total = count_voice_training_examples(uid)
    lines: list[str] = ["", "📊 ТВОЙ РЕДАКТОРСКИЙ СТИЛЬ:", ""]
    if prefer:
        lines.append("✅ Предпочтения (в т.ч. из переписываний):")
        for p in prefer[:5]:
            d = str(p.get("pattern_description") or "").strip()
            if d:
                lines.append(f"• {d[:420]}")
        lines.append("")
    if avoid:
        lines.append("❌ Анти-предпочтения (из отклонённых):")
        for p in avoid[:6]:
            d = str(p.get("pattern_description") or "").strip()
            c = int(p.get("count") or 0)
            if d:
                lines.append(f"• {d[:420]} (×{c})")
        lines.append("")
    lines.append("📈 Статистика:")
    lines.append(f"• Позитивных примеров голоса: {voice_total}")
    lines.append(f"• Негативных примеров (отказы): {neg_n}")
    lines.append(f"• Одобрено (feedback): {appr} / отклонено: {rej}")
    lines.append(f"• Паттернов отказов в базе: {p_total}")
    if appr + rej > 0:
        lines.append(f"• Доля одобрений: {100 * appr // (appr + rej)}% (по последним решениям в ленте feedback)")
    lines.append("")
    lines.append("🎯 Активные паттерны (по весу свежести):")
    for i, p in enumerate(patterns[:6], start=1):
        d = str(p.get("pattern_description") or "").strip()[:200]
        c = int(p.get("count") or 0)
        w = p.get("pattern_weight")
        ws = f", вес {float(w):.1f}" if w is not None else ""
        if d:
            lines.append(f"{i}. {d} — ×{c}{ws}")
    cat_ok = get_feedback_category_counts_in_window(uid, min(win, 120))
    cat_bad = get_feedback_rejected_category_counts_in_window(uid, min(win, 120))
    top_ok = _top_keys_from_counts(cat_ok, n=1)
    top_bad = _top_keys_from_counts(cat_bad, n=1)
    if top_ok or top_bad:
        lines.append("")
        lines.append("📌 Тренды (по последним записям draft_feedback):")
        if top_ok:
            lines.append(f"• Чаще разбираешь категории: {', '.join(top_ok)}")
        if top_bad:
            lines.append(f"• Чаще отклоняешь (по категории): {', '.join(top_bad)}")
    return "\n".join(lines).strip()


def format_style_profile_message(user_id: int, prefs: dict[str, str] | None) -> str:
    """Текст команды /style_profile — полный редакторский профиль."""
    uid = str(user_id)
    topics = (prefs or {}).get(PREF_TOPICS) if prefs else None
    topics_line = (topics or "").strip()[:200] or "—"
    patterns = get_active_rejection_patterns(uid, limit=12)
    prefer = [p for p in patterns if str(p.get("pattern_type") or "").lower() == "editor_preference"]
    avoid = [p for p in patterns if str(p.get("pattern_type") or "").lower() != "editor_preference"]
    st = count_draft_posts_by_status(uid)
    n_total = count_draft_posts_total(uid)
    n_posted = int(st.get("posted") or 0)
    n_draft = int(st.get("draft") or 0)
    n_rejected = int(st.get("rejected") or 0)
    appr_fb = count_draft_feedback_by_action(uid, "approved")
    rej_fb = count_draft_feedback_by_action(uid, "rejected")
    voice_pos = count_voice_training_examples(uid)
    neg_n = count_voice_negative_training_examples(uid)
    p_n = count_user_rejection_patterns(uid)
    posted_clean = count_posted_drafts_unedited(uid)
    acc = int(100 * posted_clean / n_posted) if n_posted > 0 else None
    acc2 = int(100 * appr_fb / (appr_fb + rej_fb)) if appr_fb + rej_fb > 0 else None
    lines: list[str] = [
        "🎨 ТВОЙ РЕДАКТОРСКИЙ ПРОФИЛЬ",
        "",
        "📌 Основные принципы:",
    ]
    if prefer:
        lines.append("✅ Пиши: " + "; ".join(str(p.get("pattern_description") or "").strip()[:160] for p in prefer[:4] if str(p.get("pattern_description") or "").strip()))
    else:
        lines.append("✅ Пиши: (пока нет явных «плюсовых» паттернов — добавь /learning или одобряй черновики)")
    if avoid:
        lines.append("❌ Избегай: " + "; ".join(str(p.get("pattern_description") or "").strip()[:160] for p in avoid[:5] if str(p.get("pattern_description") or "").strip()))
    else:
        lines.append("❌ Избегай: (паттерны отказов появятся после отклонений с уточнением причины)")
    lines.extend(
        [
            "",
            "📊 Статистика:",
            f"• Всего черновиков в базе: {n_total} (черновик в очереди: {n_draft}, опубликовано: {n_posted}, отклонено статусом: {n_rejected})",
            f"• Feedback: одобрено {appr_fb}, отклонено {rej_fb}",
            f"• Обучение голоса: позитивных примеров {voice_pos}, негативных {neg_n}, паттернов отказов {p_n}",
            f"• Темы в настройках: {topics_line}",
            "",
        ]
    )
    if acc is not None:
        lines.append(
            f"🎯 Точность «с первого раза» (опубликовано без правки was_edited): {acc}% "
            f"({posted_clean}/{n_posted})"
        )
    if acc2 is not None:
        lines.append(f"🎯 Доля одобрений по кнопкам: {acc2}%")
    lines.append("")
    lines.append("🎯 Активные паттерны:")
    for i, p in enumerate(patterns[:8], start=1):
        d = str(p.get("pattern_description") or "").strip()[:220]
        c = int(p.get("count") or 0)
        if d:
            lines.append(f"{i}. {d} — встречался ×{c}")
    lines.append("")
    lines.append("💡 Рекомендации:")
    lines.append("• Экспорт профиля: /style_profile export")
    lines.append("• Импорт: ответь файлом .json на сообщение бота с командой /style_profile import")
    if topics_line not in ("—", ""):
        lines.append(f"• Текущие темы можно сузить/расширить: /editor_prefs (сейчас: {topics_line[:120]})")
    out = "\n".join(lines)
    if len(out) > 4000:
        out = out[:3990] + "…"
    return out


def format_editor_info_text(prefs: dict[str, str], *, user_id: int | None = None) -> str:
    """Текст для /editor_info — сводка prefs редактора."""
    sm = get_source_mode(prefs)
    search_window_line = format_search_window_settings_message(prefs)
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
        "Настройки поиска для подбора черновиков:\n"
        f"• Темы поиска: {topics[:500]}{'…' if len(topics) > 500 else ''}\n"
        f"• Режим источников: {sm} (web / tg / both)\n"
        f"• {search_window_line}\n"
        f"• TG-каналы: {tg_line}\n"
        f"  ({tg_note})\n\n"
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


def _max_promo_blocklist_entries() -> int:
    return 40


def _load_promo_blocklist_keys(prefs: dict[str, str]) -> frozenset[str]:
    raw = (prefs.get(PREF_PROMO_BLOCKED_HOSTS) or "").strip()
    if not raw:
        return frozenset()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    out: set[str] = set()
    for x in data:
        k = str(x).strip().lower()
        if k:
            out.add(k[:120])
    return frozenset(out)


def _promo_blocklist_key_for_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    h = _host(u).split(":")[0].lower().lstrip("www.")
    if not h:
        return ""
    if h in ("t.me", "telegram.me", "telegram.dog"):
        ch = extract_tg_channel_username_from_url(u)
        return f"tg:{ch}" if ch else f"host:{h}"
    return f"host:{h}"


def url_matches_user_promo_blocklist(url: str, keys: frozenset[str]) -> bool:
    """Совпадение URL с пользовательским списком после отказа «рекламный пост»."""
    if not keys or not (url or "").strip():
        return False
    h = _host(url).split(":")[0].lower().lstrip("www.")
    ch = extract_tg_channel_username_from_url(url)
    for k in keys:
        if k.startswith("tg:"):
            if ch and k == f"tg:{ch}":
                return True
        elif k.startswith("host:"):
            dom = k[5:].strip().lower()
            if not dom:
                continue
            if h == dom or (h.endswith(f".{dom}") and len(h) > len(dom)):
                return True
    return False


def append_promo_blocklist_from_url(memory: ChatMemory, user_id: int, url: str) -> None:
    key = _promo_blocklist_key_for_url(url)
    if not key or key == "tg:":
        return
    prefs = _prefs(memory, user_id)
    raw = (prefs.get(PREF_PROMO_BLOCKED_HOSTS) or "").strip()
    try:
        cur_l: list[Any] = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        cur_l = []
    if not isinstance(cur_l, list):
        cur_l = []
    norm = [str(x).strip().lower()[:120] for x in cur_l if str(x).strip()]
    if key not in norm:
        norm.append(key)
    cap = _max_promo_blocklist_entries()
    norm = norm[-cap:]
    memory.update_style_preferences(
        user_id,
        {PREF_PROMO_BLOCKED_HOSTS: json.dumps(norm, ensure_ascii=False)},
    )
    logger.info(
        "promo_blocklist: user_id=%s added=%r size=%s",
        user_id,
        key,
        len(norm),
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


def _assess_draft_quality(draft_text: str, *, min_len_slack: int = 0) -> tuple[bool, str]:
    text = (draft_text or "").strip()
    slack = max(0, min(60, int(min_len_slack)))
    min_len = max(40, 100 - slack)
    if len(text) < min_len:
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
                    OR (
                      status = 'seen'
                      AND datetime(created_at) >= datetime('now', ?)
                    )
                    OR (status = 'draft')
                  )
                """,
                (uid, f"-{posted_d} days", f"-{rej_d} days", f"-{rej_d} days"),
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


def set_pending_learning(user_id: int, original_text: str) -> None:
    _pending_learning[user_id] = (original_text or "").strip()


def get_pending_learning(user_id: int) -> str | None:
    return _pending_learning.get(user_id)


def pop_pending_learning(user_id: int) -> str | None:
    return _pending_learning.pop(user_id, None)


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
        rs = str(r.get("reason") or "").strip()
        rs_part = f" Причина отказа: {rs}." if rs and act == "rejected" else ""
        line = (
            f"Действие: {act}. Фрагмент черновика: {prev[:180]}.{rs_part} Комментарий: {fb[:600]}"
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
    if act not in (CB_APPROVE, CB_EDIT, CB_REJECT, CB_EXPIRED, CB_SEEN):
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
            [
                InlineKeyboardButton(
                    text="👁 Уже видел",
                    callback_data=f"editor:{CB_SEEN}:{draft_id}",
                ),
            ],
        ],
    )


def parse_reject_reason_callback(data: str) -> tuple[str, int] | None:
    """fbrej:<slug>:<draft_id> → (slug, draft_id) или None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_REJECT_REASON_PREFIX:
        return None
    slug, sid = parts[1], parts[2]
    if slug not in REJECT_REASON_SLUGS:
        return None
    try:
        return slug, int(sid)
    except ValueError:
        return None


def build_reject_reason_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    did = int(draft_id)
    p = CALLBACK_REJECT_REASON_PREFIX
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="😐 Не интересно",
                    callback_data=f"{p}:not_interested:{did}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📉 Слабый контент",
                    callback_data=f"{p}:weak_content:{did}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📰 Источник не нравится",
                    callback_data=f"{p}:bad_source:{did}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📢 Рекламный пост",
                    callback_data=f"{p}:promotional:{did}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🤐 Не хочу уточнять",
                    callback_data=f"{p}:{REJECT_REASON_SLUG_SKIP}:{did}",
                ),
            ],
        ]
    )


def draft_topic_slug_to_query_prefix(slug: str) -> str:
    return {
        "tech": "технологии",
        "science": "наука",
        "movie": "кино",
        "music": "музыка",
    }.get((slug or "").strip().lower(), (slug or "").strip())


def build_draft_topic_picker_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=f"{CALLBACK_DRAFT_TOPIC_PREFIX}:{slug}",
            )
        ]
        for slug, label in POPULAR_DRAFT_TOPIC_SLUGS
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="5️⃣ Свой вариант",
                callback_data=f"{CALLBACK_DRAFT_TOPIC_PREFIX}:custom",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_draft_picker_failure_message(msg: str) -> bool:
    if not msg:
        return False
    needles = (
        "Ничего подходящего не нашёл",
        "С публичных TG-каналов сейчас пусто",
    )
    return any(n in msg for n in needles)


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


def _broaden_tavily_news_query(
    original_q: str,
    *,
    single_topic: str | None,
    topics_general: str,
) -> str:
    """Упрощённый запрос, если Tavily дважды не дал пригодных сниппетов."""
    _ = (original_q or "").split(". Исключай или обходи")[0].strip()
    y = f"{datetime.utcnow().year}"
    if single_topic and single_topic.strip():
        st = single_topic.strip()
        return re.sub(r"\s+", " ", f"новости {st} обзор главное {y}")[:400]
    first = (topics_general.split(",")[0] or "новости").strip()
    return re.sub(r"\s+", " ", f"{first} новости сегодня главное события {y}")[:400]


def _topic_keyword_hints(topics: str) -> list[str]:
    raw = (topics or "").replace("ё", "е").lower()
    parts = re.split(r"[,;\n]+", raw)
    out: list[str] = []
    for p in parts:
        w = p.strip()
        if len(w) >= 3:
            out.append(w)
    for m in re.findall(r"[a-zа-яё]{3,}", raw.replace("ё", "е")):
        if m not in out:
            out.append(m)
    return out[:24]


def _seo_score_for_title(title: str, keyword_hints: list[str]) -> tuple[int, bool]:
    """SEO-оценка заголовка 0–100 и признак полного соответствия (длина 40–60 + ключевое слово)."""
    t = (title or "").strip()
    L = len(t)
    len_ok = 40 <= L <= 60
    low = t.lower().replace("ё", "е")
    hints = [h for h in keyword_hints if len(h) >= 3]
    kw_hit = any(h.lower().replace("ё", "е") in low for h in hints) if hints else True
    len_part = 100 if len_ok else (70 if 30 <= L <= 75 else 35)
    kw_part = 100 if kw_hit else 40
    score = int(round((len_part + kw_part) / 2))
    meets = len_ok and kw_hit
    return max(0, min(100, score)), meets


def _pick_fail_streak_get(memory: ChatMemory, user_id: int) -> int:
    try:
        return int(
            (_prefs(memory, user_id).get(PREF_PICK_FAIL_STREAK) or "0").strip() or 0
        )
    except ValueError:
        return 0


def _pick_fail_streak_bump(memory: ChatMemory, user_id: int) -> int:
    n = _pick_fail_streak_get(memory, user_id) + 1
    memory.update_style_preferences(user_id, {PREF_PICK_FAIL_STREAK: str(n)})
    return n


def _pick_fail_streak_reset(memory: ChatMemory, user_id: int) -> None:
    memory.update_style_preferences(user_id, {PREF_PICK_FAIL_STREAK: "0"})


def _exhaustion_message_suffix(streak: int) -> str:
    if streak >= 3:
        return (
            "\n\nТема исчерпана или слишком узкая — попробовать другую? "
            "Загляни в /topics 🔁"
        )
    return ""


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


def quality_min_len_slack_from_prefs(prefs: dict[str, str]) -> int:
    try:
        return max(0, min(60, int(float((prefs.get(PREF_QUALITY_MIN_LEN_SLACK) or "0").strip() or 0))))
    except (TypeError, ValueError):
        return 0


def bump_weak_content_quality_slack(memory: ChatMemory, user_id: int) -> None:
    """После отказа «слабый контент» — чуть снизить порог минимальной длины черновика."""
    prefs = _prefs(memory, user_id)
    cur = quality_min_len_slack_from_prefs(prefs)
    nxt = min(60, cur + 20)
    if nxt != cur:
        memory.update_style_preferences(user_id, {PREF_QUALITY_MIN_LEN_SLACK: str(nxt)})
        logger.info(
            "reject_reason weak_content: user_id=%s quality_min_len_slack %s→%s",
            user_id,
            cur,
            nxt,
        )


def apply_reject_reason_followup(
    memory: ChatMemory,
    user_id: int,
    reason_slug: str,
    draft_row: dict[str, Any],
) -> None:
    """Доп. действия по выбранной причине отказа (источник / порог длины / реклама)."""
    slug = (reason_slug or "").strip().lower()
    if slug == "weak_content":
        bump_weak_content_quality_slack(memory, user_id)
        return
    u = str(draft_row.get("source_url") or "").strip()
    if not u:
        return
    host = _host(u).split(":")[0].lower().lstrip("www.")
    if slug == "bad_source":
        if not host:
            return
        for _i in range(3):
            _bump_host_reject_count(memory, user_id, host)
        logger.info(
            "reject_reason bad_source: user_id=%s extra_host_bumps=3 host=%r",
            user_id,
            host[:120],
        )
        return
    if slug == "promotional":
        if host:
            for _i in range(5):
                _bump_host_reject_count(memory, user_id, host)
        append_promo_blocklist_from_url(memory, user_id, u)
        logger.info(
            "reject_reason promotional: user_id=%s host_bumps=5 host=%r",
            user_id,
            host[:120],
        )
        return


def _reject_category_signal_multiplier(reject_reason: str | None) -> float:
    """Вес отказа для preference[категория]: слабый контент/источник — не про тему; реклама — умеренный штраф."""
    r = (reject_reason or "").strip().lower()
    if r == "weak_content":
        return 0.5
    if r == "bad_source":
        return 0.35
    if r == "promotional":
        return 0.8
    if r == "already_seen":
        return 0.45
    return 1.0


def _feedback_signal_from_row(
    action: str,
    quality_score: int | None,
    reject_reason: str | None = None,
) -> float:
    """Перевод feedback в сигнал -1..1 для preference."""
    act_l = (action or "").strip().lower()
    if act_l == "rejected":
        # Один отказ ≈ -0.3; несколько отказов в окне усиливают негатив через среднее по категории.
        base_r = -0.3
        if quality_score is None:
            sig = base_r
        else:
            q = max(1, min(10, int(quality_score)))
            q_centered = (q - 5.5) / 4.5
            sig = max(-0.87, min(0.0, 0.82 * base_r + 0.18 * q_centered))
        mult = _reject_category_signal_multiplier(reject_reason)
        out = sig * mult
        return max(-0.87, min(0.0, out))
    base = {
        "approved": 0.4,
        "edited": 0.35,
        "expired_content": -0.6,
    }.get(act_l, 0.0)
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
        reason_raw = str(r.get("reason") or "").strip().lower() or None
        act_raw = str(r.get("action") or "").strip().lower()
        signal = _feedback_signal_from_row(
            act_raw,
            int(r["quality_score"]) if r.get("quality_score") is not None else None,
            reason_raw if act_raw == "rejected" else None,
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
                  AND status IN ('draft', 'posted', 'rejected', 'seen')
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


def _host_reject_soft_penalty_mult(host_reject_count: int) -> float:
    """Смягчённый штраф при 1–2 отказах по тому же источнику; 3+ — жёсткий бан в подборе."""
    n = int(host_reject_count or 0)
    if n <= 0:
        return 1.0
    if n == 1:
        return 0.7
    if n == 2:
        return 0.4
    return 1.0


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
    confidence_score: int | None = None,
    requires_verification: bool = False,
    seo_score: float | None = None,
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
                INSERT INTO draft_posts (
                    user_id, channel_id, content, source_url, status, expires_at,
                    confidence_score, requires_verification, seo_score
                )
                VALUES (?, ?, ?, ?, 'draft', datetime('now', ?), ?, ?, ?)
                """,
                (
                    uid,
                    channel_id,
                    body,
                    source_url or None,
                    f"+{ttl_h} hours",
                    confidence_score,
                    1 if requires_verification else 0,
                    seo_score,
                ),
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
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at,
                       media_url, expires_at, confidence_score, requires_verification, seo_score, was_edited
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
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at,
                       media_url, expires_at, confidence_score, requires_verification, seo_score, was_edited
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
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at,
                       media_url, expires_at, confidence_score, requires_verification, seo_score, was_edited
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

_VERIFICATION_WARNING_HEAD_RE = re.compile(
    r"⚠️\s*Требует проверки.*?(?=\n\n|\Z)",
    flags=re.DOTALL,
)

_RELATED_NEWS_LINE_RE = re.compile(
    r"(?m)^\s*Кстати,\s*ранее\s+по\s+теме:\s*.+$",
    flags=re.IGNORECASE,
)


def strip_verification_warning_block(text: str) -> str:
    """Убирает служебный блок «⚠️ Требует проверки» (и хвост до пустой строки или конца текста)."""
    return _VERIFICATION_WARNING_HEAD_RE.sub("", text or "").strip()


def strip_related_news_block(text: str) -> str:
    """Убирает вставку «Кстати, ранее по теме: …» перед публикацией в канал."""
    t = _RELATED_NEWS_LINE_RE.sub("", text or "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


_HASHTAG_STOPWORDS: frozenset[str] = frozenset(
    """
    это того тем что как для при без над под про все еще уже или же
    лишь вот тут там куда от из по ко да не ни на мы вы они она его
    них ней нем нас вас них ему ей им бы был была были будут было
    быть может можно нужно надо если чтобы после также такой такая
    такие этот эта эти того этой этом этих который которая которое
    которых когда где кто чем чего кому чему кого чему чем чём
    самый самая самое самые очень более менее очень весь вся всё все
    один одна одно одни первый первая первое новый новая новое
    другой другая другое другие любой любая любое любые каждый
    мой твой наш ваш их свой своя своё свои так же чем то что бы
    здесь тут сейчас тогда потом очень весь вся всего всей всем
    источник ссылка читать подробнее читайте также сообщает сообщили
    сообщает сообщение стало стали стать будет были был была
    """.split()
)


def _sanitize_hashtag_token(raw: str) -> str:
    s = (raw or "").strip().lower().replace("ё", "е")
    s = re.sub(r"[^0-9a-zа-яё_]+", "", s)
    return s[:48]


def build_channel_hashtag_footer(body: str) -> str:
    """
    3–5 хэштегов в конец поста: всегда #новости, рубрика из detect_primary_category,
    1–2 ключевых слова из текста (кириллица/латиница).
    """
    text = (body or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    head = (lines[0] if lines else "").strip()
    tail = "\n".join(lines[1:])[:6000]
    tags: list[str] = ["#новости"]
    seen = {t.lower() for t in tags}

    cat = detect_primary_category(head, tail)
    cat_t = _sanitize_hashtag_token(str(cat or ""))
    if cat_t and cat_t not in ("новости", "other", ""):
        ht = f"#{cat_t}"
        if ht.lower() not in seen:
            tags.append(ht)
            seen.add(ht.lower())

    words = re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", text)
    freq: Counter[str] = Counter()
    for w in words:
        wl = w.lower().replace("ё", "е")
        if wl in _HASHTAG_STOPWORDS or len(wl) < 2:
            continue
        freq[wl] += 1
    for w, _c in freq.most_common(40):
        tok = _sanitize_hashtag_token(w)
        if len(tok) < 2:
            continue
        ht = f"#{tok}"
        if ht.lower() in seen:
            continue
        tags.append(ht)
        seen.add(ht.lower())
        if len(tags) >= 5:
            break

    while len(tags) < 3:
        for filler in ("события", "факты", "обновление"):
            ht = f"#{filler}"
            if ht.lower() not in seen:
                tags.append(ht)
                seen.add(ht.lower())
                break
        else:
            break
        if len(tags) >= 3:
            break

    return " ".join(tags[:5])


def append_channel_hashtag_footer(text: str) -> str:
    base = (text or "").strip()
    if not base:
        return base
    footer = build_channel_hashtag_footer(base)
    if not footer:
        return base
    combined = f"{base.rstrip()}\n\n{footer}".strip()
    if len(combined) > 4090:
        combined = combined[:4080].rstrip() + "…"
    return combined


def channel_publish_text_from_draft_body(body: str) -> str:
    """Текст для публикации в канал: без служебных строк, которые показываются только в ЛС."""
    body = strip_verification_warning_block(body)
    body = strip_related_news_block(body)
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
        return append_channel_hashtag_footer((body or "").strip())
    return append_channel_hashtag_footer(out)


def draft_dm_text(row: dict[str, Any]) -> str:
    sid = row.get("source_url") or ""
    head = "✍️ Черновик для канала @kriptogeograph — глянь и реши судьбу поста:\n\n"
    body = str(row.get("content") or "")
    meta_lines: list[str] = []
    cs = row.get("confidence_score")
    if cs is not None:
        try:
            meta_lines.append(f"📊 Уверенность (фактчек): {int(cs)}%")
        except (TypeError, ValueError):
            pass
    ss = row.get("seo_score")
    if ss is not None:
        try:
            meta_lines.append(f"🔎 SEO заголовка: {float(ss):.0f}/100")
        except (TypeError, ValueError):
            pass
    if int(row.get("requires_verification") or 0):
        meta_lines.append("⚠️ Требуется ручная проверка фактов")
    meta = ("\n" + "\n".join(meta_lines) + "\n\n") if meta_lines else ""
    tail = ""
    if sid:
        tail = f"\n\n🔗 Источник в базе: {sid}"
    msg = head + meta + body + tail
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


_TAVILY_EN_RU_TOPIC_HINTS = frozenset({"twitch", "anime", "gaming"})


def _tavily_multi_topic_query(topic: str, date_hint: str) -> str:
    """Одна тема в multi-topic цикле: приоритет русскоязычных и RU-контекста."""
    t = (topic or "").strip()
    if not t:
        return f"новости Россия {date_hint}".strip()
    tl = t.lower()
    has_cyrillic = bool(re.search(r"[а-яё]", tl))
    base = f"новости {t} Россия {date_hint}".strip()
    if has_cyrillic:
        return base
    tokens = {tok for tok in re.split(r"[\s,_.\-/]+", tl) if tok}
    if tokens & _TAVILY_EN_RU_TOPIC_HINTS or any(
        k in tl for k in _TAVILY_EN_RU_TOPIC_HINTS
    ):
        return f"новости {t} русский Россия {date_hint}".strip()
    return base


def verify_topic_relevance(agent: LLMAgent, text: str, topic: str) -> bool:
    """GPT: новость действительно про тему или только случайное упоминание слова."""
    topic_s = (topic or "").strip()[:400]
    t = (text or "").strip()[:4000]
    if not topic_s or not t:
        return True
    try:
        raw = agent.run_raw_completion(
            system=(
                "Ты оцениваешь релевантность новости к заданной теме. "
                "Ответь ровно одним словом: ДА или НЕТ.\n"
                "ДА — если материал в основном про эту тему.\n"
                "НЕТ — если слово темы только мимоходом, в другом контексте, "
                "метафоре, рекламе или заголовке-приманке без содержания по теме."
            ),
            user=(
                f"Тема: {topic_s}\n\nТекст (заголовок и сниппет):\n{t}\n\n"
                f"Это в основном новость про «{topic_s}», а не случайное упоминание?"
            ),
            max_tokens=12,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("verify_topic_relevance: GPT error, принимаем кандидата: %s", exc)
        return True
    low = (raw or "").strip().lower()
    first = low.split()[0] if low else ""
    if first.startswith("нет") or low.startswith("no"):
        return False
    if first.startswith("да") or low.startswith("yes"):
        return True
    if "нет" in low[:24] and "да" not in low[:12]:
        return False
    return True


def _parse_gpt_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    s = raw.strip()
    if "```" in s:
        parts = s.split("```")
        for chunk in parts:
            c = chunk.strip()
            if c.lower().startswith("json"):
                c = c[4:].strip()
            if c.startswith("{") and "}" in c:
                s = c
                break
    i = s.find("{")
    j = s.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    try:
        out = json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


_REJECTION_ANALYSIS_SYSTEM = (
    "Ты редактор новостного канала. Проанализируй отклонённый черновик и верни ТОЛЬКО один JSON-объект "
    "без пояснений и без markdown. Ключи строго: problems (массив строк), pattern_description (строка), "
    "pattern_type (строка: weak_content|promotional|bad_source|not_interested|editor_preference|unknown), "
    "keywords_to_avoid (массив строк или []), requirements (массив строк)."
)


def analyze_rejected_draft_with_gpt(
    agent: LLMAgent,
    draft_content: str,
    reason: str | None,
) -> dict[str, Any] | None:
    """Смысловой разбор отклонённого черновика для обучения паттернам."""
    body = (draft_content or "").strip()
    if len(body) < 20:
        return None
    body = body[:6000]
    reason_s = (reason or "").strip().lower() or "не указана"
    user_block = (
        f"Текст черновика:\n{body}\n\n"
        f"Причина отказа (кнопка пользователя): {reason_s}\n\n"
        "Найди конкретные проблемы:\n"
        "1. Структура: короткие/длинные предложения, абзацы\n"
        "2. Содержание: есть ли имена, даты, цифры, факты\n"
        "3. Стиль: эмоциональный/сухой, активные/пассивные глаголы\n"
        "4. Слова-маркеры рекламы: «купить», «акция», «бесплатно» и т.п.\n"
        "5. Тональность: позитив / негатив / нейтрально\n\n"
        "Верни JSON по схеме из системного сообщения."
    )
    try:
        raw = agent.run_raw_completion(
            system=_REJECTION_ANALYSIS_SYSTEM,
            user=user_block,
            max_tokens=900,
            temperature=0.2,
        )
    except Exception as exc:
        logger.warning("analyze_rejected_draft_with_gpt: GPT error: %s", exc)
        return None
    data = _parse_gpt_json_object(raw)
    if not data:
        logger.warning("analyze_rejected_draft_with_gpt: не удалось разобрать JSON")
        return None
    if not isinstance(data.get("problems"), list):
        data["problems"] = []
    if not isinstance(data.get("requirements"), list):
        data["requirements"] = []
    if not isinstance(data.get("keywords_to_avoid"), list):
        data["keywords_to_avoid"] = []
    pt = str(data.get("pattern_type") or "unknown").strip().lower()
    if pt not in ALLOWED_REJECTION_PATTERN_TYPES:
        data["pattern_type"] = "unknown"
    return data


_REJECTION_CANDIDATE_CHECK_SYSTEM = (
    "Ты редактор. Сравни кандидат новости с паттернами отказов пользователя. "
    "Ответь ТОЛЬКО JSON без markdown: "
    '{"is_similar_to_rejected": false, "violated_pattern_id": null, "can_be_fixed": false, "fixed_version": null}. '
    "violated_pattern_id — целое id из списка паттернов или null. "
    "fixed_version — только если can_be_fixed true: краткий исправленный текст (заголовок + 1–3 абзаца) на русском, иначе null."
)


def check_candidate_against_rejection_patterns(
    agent: LLMAgent,
    title: str,
    snippet: str,
    patterns: list[dict[str, Any]],
) -> dict[str, Any]:
    """GPT: похож ли кандидат (заголовок+сниппет) на ранее отклонённые паттерны."""
    out: dict[str, Any] = {
        "is_similar_to_rejected": False,
        "violated_pattern_id": None,
        "can_be_fixed": False,
        "fixed_version": None,
    }
    if not patterns:
        return out
    cand = f"{(title or '').strip()}\n\n{(snippet or '').strip()}"[:4500]
    lines: list[str] = []
    for p in patterns:
        pid = p.get("id")
        desc = (str(p.get("pattern_description") or "")).strip()
        ptype = (str(p.get("pattern_type") or "")).strip()
        if not desc:
            continue
        lines.append(f"id={pid} type={ptype}: {desc[:900]}")
    if not lines:
        return out
    user_block = (
        "Текст кандидата (заголовок и сниппет):\n"
        f"{cand}\n\n"
        "Паттерны отказов пользователя:\n"
        + "\n".join(lines)
        + "\n\n"
        "Вопросы: (1) Похож ли пост на те, что отклонялись? (2) Какой id паттерна? "
        "(3) Можно ли исправить текст кандидата без смены темы? (4) Если да — дай fixed_version.\n"
        "Верни JSON строго по схеме из системного сообщения."
    )
    try:
        raw = agent.run_raw_completion(
            system=_REJECTION_CANDIDATE_CHECK_SYSTEM,
            user=user_block,
            max_tokens=700,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("check_candidate_against_rejection_patterns: GPT error: %s", exc)
        return out
    data = _parse_gpt_json_object(raw)
    if not data:
        return out
    out["is_similar_to_rejected"] = bool(data.get("is_similar_to_rejected"))
    vid = data.get("violated_pattern_id")
    if vid is not None:
        try:
            out["violated_pattern_id"] = int(vid)
        except (TypeError, ValueError):
            out["violated_pattern_id"] = None
    out["can_be_fixed"] = bool(data.get("can_be_fixed"))
    fx = data.get("fixed_version")
    if isinstance(fx, str) and fx.strip():
        out["fixed_version"] = fx.strip()[:4000]
    return out


def format_rejection_analysis_report(reason_slug: str, analysis: dict[str, Any]) -> str:
    """Телеграм-отчёт после GPT-анализа отказа."""
    labels = {
        "not_interested": "Не интересно",
        "weak_content": "Слабый контент",
        "bad_source": "Источник не нравится",
        "promotional": "Рекламный пост",
        "editor_preference": "Предпочтение редактора",
        "skip": "Без уточнения",
    }
    reason_h = labels.get((reason_slug or "").strip().lower(), reason_slug or "—")
    problems = analysis.get("problems") or []
    if not isinstance(problems, list):
        problems = []
    prob_lines = "\n".join(f"❌ {str(p).strip()}" for p in problems[:8] if str(p).strip())
    if not prob_lines:
        prob_lines = "❌ (модель не выделила отдельные пункты)"
    desc = str(analysis.get("pattern_description") or "").strip() or "—"
    req = analysis.get("requirements") or []
    if not isinstance(req, list):
        req = []
    req_lines = "\n".join(f"✅ {str(r).strip()}" for r in req[:8] if str(r).strip())
    if not req_lines:
        req_lines = "✅ (без явных требований)"
    return (
        f"✅ Записал причину: {reason_h}\n\n"
        f"📊 Проанализировал пост:\n{prob_lines}\n\n"
        f"🧠 Запомнил паттерн: «{desc[:500]}»\n\n"
        f"📋 В следующий раз буду искать:\n{req_lines}"
    )


def check_pending_draft_against_new_pattern(
    agent: LLMAgent,
    draft_body: str,
    pattern_description: str,
    pattern_type: str,
) -> bool:
    """Быстрая проверка: похож ли уже готовый черновик на только что извлечённый паттерн."""
    desc = (pattern_description or "").strip()[:1200]
    if len(desc) < 8 or len((draft_body or "").strip()) < 40:
        return False
    body = (draft_body or "").strip()[:4500]
    user_block = (
        f"Тип паттерна: {pattern_type}\n"
        f"Описание паттерна отказа: {desc}\n\n"
        f"Текст черновика в очереди:\n{body}\n\n"
        'Верни только JSON: {{"violates": true}} или {{"violates": false}}.'
    )
    try:
        raw = agent.run_raw_completion(
            system="Ты редактор. Ответь только JSON с ключом violates (boolean).",
            user=user_block,
            max_tokens=40,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("check_pending_draft_against_new_pattern: %s", exc)
        return False
    data = _parse_gpt_json_object(raw)
    return bool(data.get("violates")) if data else False


def scan_pending_drafts_for_new_pattern(
    agent: LLMAgent,
    user_id: int,
    exclude_draft_id: int,
    analysis: dict[str, Any],
) -> list[int]:
    """Список id черновиков в очереди, которые GPT считает похожими на новый паттерн."""
    desc = str(analysis.get("pattern_description") or "").strip()
    ptype = str(analysis.get("pattern_type") or "unknown").strip()
    if not desc:
        return []
    rows = list_user_drafts_by_status(
        user_id, "draft", exclude_draft_id=exclude_draft_id, limit=8
    )
    hits: list[int] = []
    for r in rows:
        body = str(r.get("content") or "")
        if check_pending_draft_against_new_pattern(agent, body, desc, ptype):
            try:
                hits.append(int(r["id"]))
            except (TypeError, ValueError):
                continue
    return hits


def _pick_draft_item(
    agent: LLMAgent,
    prefs: dict[str, str],
    user_id: int,
    excluded_urls: set[str] | None = None,
    *,
    _topic_kw_retry: bool = False,
    temporary_topic: str | None = None,
) -> DraftPick | None:
    """Подбор материала: Tavily (web), публичные TG-каналы (t.me/s), фильтры отказов."""
    from app.tg_feed_fetcher import fetch_many_channels

    mode = get_source_mode(prefs)
    excl_raw = {(u or "").strip().lower() for u in (excluded_urls or set())}
    excl_norm = {_norm_cmp_url(u).lower() for u in (excluded_urls or set()) if u}
    topics = (prefs.get(PREF_TOPICS) or "актуальные новости").strip()
    user_topics = _normalize_user_topics(topics)
    if _topic_kw_retry:
        user_topics = list(
            dict.fromkeys(
                list(user_topics) + ["событие", "развитие", "произошло"]
            )
        )
        logger.info(
            "Pick draft: повторная попытка с расширенными ключами тем (kw retry), user_id=%s",
            user_id,
        )
    sources = (prefs.get(PREF_SOURCES) or "").strip()
    rejects = _reject_list(prefs)
    reject_urls, hard_hosts, soft_hosts, kw_strings = _build_reject_filters(rejects, prefs)
    host_reject_counts = _load_host_reject_counts(prefs)
    user_promo_blocklist = _load_promo_blocklist_keys(prefs)
    rejection_patterns = get_active_rejection_patterns(user_id)
    blocking_patterns = patterns_for_rejection_gate(rejection_patterns)
    channel_quality = get_channel_quality_snapshot()
    blocked_src = frozenset(agent.config.blocked_search_domains)
    cfg = getattr(agent, "config", None)
    min_web_eff = float(
        getattr(cfg, "draft_pick_min_web_effective_score", 1.5) or 1.5
    )
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
    temp_topic_raw = (temporary_topic or "").strip()
    rejected_cat_counts: dict[str, int] = {}
    if temp_topic_raw:
        rejected_cat_counts = get_feedback_hard_reject_category_counts_in_window(
            user_id, feedback_window
        )
        preserved: list[str] = []
        softened: list[str] = []
        new_prefs: dict[str, float] = {}
        for k, v in cat_preferences.items():
            v_f = float(v)
            if v_f >= 0:
                new_prefs[k] = v_f
                continue
            if int(rejected_cat_counts.get(k, 0)) > 0:
                new_prefs[k] = v_f
                preserved.append(k)
            else:
                new_prefs[k] = 0.0
                softened.append(k)
        cat_preferences = new_prefs
        logger.info(
            "temporary_topic: soften negative prefs without explicit rejects in window; "
            "preserve negatives for rejected categories (topic=%r window=%s "
            "rejected_by_cat=%s preserved=%s softened=%s)",
            temp_topic_raw[:120],
            feedback_window,
            rejected_cat_counts,
            preserved,
            softened,
        )
    feedback_cat_counts = get_feedback_category_counts_in_window(user_id, feedback_window)
    min_pref = int(getattr(cfg, "feedback_min_count_for_full_pref", 1) or 1)
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
                if user_promo_blocklist and url_matches_user_promo_blocklist(
                    url_raw, user_promo_blocklist
                ):
                    logger.debug(
                        "skip tavily_user_promo_blocklist url=%s",
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
            lead = f"{topics} {sources} {freshness_hint}".strip()
            q = f"{lead} новости на русском {date_hint}".strip()
            tail = _reject_hints_for_tavily_query(rejects)
            if tail:
                q += ". Исключай или обходи материалы, связанные с: " + ", ".join(tail)
            mark_web = _web_candidate_count()
            result = agent._tavily_search(
                q[:400],
                max_results=6,
                days=primary_days,
                topic="general",
                country="russia",
                include_published_date=True,
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
                    topic="general",
                    country="russia",
                    include_published_date=True,
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
            if _web_candidate_count() == mark_web:
                q_broad = _broaden_tavily_news_query(
                    q, single_topic=None, topics_general=topics
                )
                days_3 = min(max(fallback_days, 7), MAX_SEARCH_WINDOW_DAYS)
                logger.info(
                    "Pick draft: веб-кандидатов всё ещё 0 — расширенный запрос Tavily, query=%r days=%s",
                    q_broad[:220],
                    days_3,
                )
                result3 = agent._tavily_search(
                    q_broad[:400],
                    max_results=8,
                    days=days_3,
                    topic="general",
                    country="russia",
                    include_published_date=True,
                    exclude_domains=promo_domains,
                )
                if result3 and isinstance(result3.get("results"), list):
                    _append_tavily_items_to_candidates(result3)
        else:
            for topic in topics_list[:5]:
                if _web_candidate_count() >= _TAVILY_WEB_CAP:
                    break
                q = _tavily_multi_topic_query(topic, date_hint)
                mark_web = _web_candidate_count()
                result = agent._tavily_search(
                    q[:400],
                    max_results=3,
                    days=primary_days,
                    topic="general",
                    country="russia",
                    include_published_date=True,
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
                        topic="general",
                        country="russia",
                        include_published_date=True,
                        exclude_domains=promo_domains,
                    )
                n_added = _append_tavily_items_to_candidates(result)
                logger.info("Tavily multi-query: topic=%s results=%s", topic, n_added)
                if _web_candidate_count() == mark_web:
                    q_broad = _broaden_tavily_news_query(
                        q, single_topic=topic, topics_general=topics
                    )
                    days_3 = min(max(fallback_days, 7), MAX_SEARCH_WINDOW_DAYS)
                    logger.info(
                        "Pick draft: тема %r — расширенный запрос Tavily, query=%r days=%s",
                        topic[:80],
                        q_broad[:220],
                        days_3,
                    )
                    result3 = agent._tavily_search(
                        q_broad[:400],
                        max_results=8,
                        days=days_3,
                        topic="general",
                        country="russia",
                        include_published_date=True,
                        exclude_domains=promo_domains,
                    )
                    if result3 and isinstance(result3.get("results"), list):
                        _append_tavily_items_to_candidates(result3)
            if _web_candidate_count() == 0:
                logger.warning(
                    "Pick draft: web пустой или нет results (mode=%s, multi-topic)",
                    mode,
                )

    web_candidates_after_tavily = sum(1 for c in candidates if not c.get("from_tg"))
    if mode in ("tg", "both"):
        skip_tg_after_empty_web = (
            mode == "both"
            and bool(getattr(agent, "tavily", None))
            and web_candidates_after_tavily == 0
        )
        if skip_tg_after_empty_web:
            logger.info(
                "Pick draft: режим both, веб-кандидатов после Tavily нет — "
                "не подмешиваю Telegram (без слабого TG-only fallback)"
            )
        else:
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
                purl = (p.get("url") or "").strip()
                if user_promo_blocklist and url_matches_user_promo_blocklist(
                    purl, user_promo_blocklist
                ):
                    logger.debug("skip tg_user_promo_blocklist url=%s", purl[:200])
                    continue
                candidates.append(
                    {
                        "title": (p.get("title") or "Пост из Telegram").strip(),
                        "url": purl,
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
        rj_key_rank = _reject_host_key_for_url(url)
        hrc = host_reject_counts.get(rj_key_rank, 0) if rj_key_rank else 0
        rej_src_soft = _host_reject_soft_penalty_mult(hrc)
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
        kw_hints_pick = _topic_keyword_hints(topics)
        _, seo_meets_pick = _seo_score_for_title(title, kw_hints_pick)
        seo_bonus_pick = 0.5 if seo_meets_pick else 0.0
        effective_score = (
            float(total_score)
            * quality_mult
            * breaking_mult
            * pref_mult
            * diversity_mult
            * novelty_mult
            * rej_src_soft
        ) + seo_bonus_pick
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

    n_excluded = n_hard_host = n_url_rej = n_kw = n_no_url = n_blocked_source = n_low_score = n_topic_rel = n_user_promo = n_pattern = 0
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
        if user_promo_blocklist and url_matches_user_promo_blocklist(url, user_promo_blocklist):
            n_user_promo += 1
            logger.debug("Pick draft: skip user_promo_blocklist url=%r", url[:120])
            continue
        raw_eff = float(sum(score_map.values()) if USE_PATTERNS else 0) * quality_mult * breaking_mult
        rj_soft_here = _host_reject_soft_penalty_mult(
            host_reject_counts.get(rj_key, 0) if rj_key else 0
        )
        if (not c.get("from_tg")) and (raw_eff * rj_soft_here < min_web_eff):
            n_low_score += 1
            logger.debug(
                "Pick draft: skip low_effective_score url=%r eff_score=%.2f threshold=%.2f",
                url[:120],
                raw_eff * rj_soft_here,
                min_web_eff,
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
                if temp_topic_raw:
                    logger.debug(
                        "Pick draft: temporary_topic skip topic/pattern gate (would reject) reason=%s",
                        pattern_info,
                    )
                else:
                    n_kw += 1
                    logger.debug(
                        "Pick draft: skip patterns_not_relevant reason=%s", pattern_info
                    )
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
        if temp_topic_raw:
            blob_rel = f"{title}\n{content}".strip()
            if not verify_topic_relevance(agent, blob_rel, temp_topic_raw):
                n_topic_rel += 1
                logger.info(
                    "Pick draft: verify_topic_relevance отклонил кандидата topic=%r url=%r",
                    temp_topic_raw[:120],
                    url[:120],
                )
                continue
        pick_title = title
        pick_snippet = content
        if blocking_patterns:
            pat_res = check_candidate_against_rejection_patterns(
                agent, pick_title, pick_snippet, blocking_patterns
            )
            if pat_res.get("is_similar_to_rejected"):
                vid = pat_res.get("violated_pattern_id")
                if vid is not None:
                    try:
                        bump_pattern_usage(int(vid))
                    except (TypeError, ValueError):
                        pass
                n_pattern += 1
                logger.info(
                    "Pick draft: skip user_rejection_pattern violated_id=%s url=%r",
                    vid,
                    url[:120],
                )
                continue
            if pat_res.get("can_be_fixed") and isinstance(pat_res.get("fixed_version"), str):
                fx = (pat_res.get("fixed_version") or "").strip()
                if len(fx) > 120:
                    lines_fx = [ln for ln in fx.splitlines() if ln.strip()]
                    if lines_fx:
                        pick_title = lines_fx[0].strip()[:500]
                        rest_fx = "\n".join(lines_fx[1:]).strip()
                        if rest_fx:
                            pick_snippet = rest_fx[:2400]
                        else:
                            pick_snippet = fx[:2400]
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
            title=pick_title,
            url=url,
            snippet=pick_snippet[:800],
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
            "url_reject=%s blocked_source=%s user_promo=%s low_score=%s kw=%s no_url=%s topic_rel=%s pattern=%s)",
            mode,
            len(candidates),
            n_excluded,
            n_hard_host,
            n_url_rej,
            n_blocked_source,
            n_user_promo,
            n_low_score,
            n_kw,
            n_no_url,
            n_topic_rel,
            n_pattern,
        )
    return None


def draft_post_from_snippet(
    agent: LLMAgent,
    memory: ChatMemory,
    user_id: int,
    title: str,
    snippet: str,
    url: str,
    topics: str,
    *,
    from_telegram: bool = False,
    telegram_channel_display: str = "",
    cross_ref_days: int = 7,
    cross_ref_exclude_domains: list[str] | None = None,
) -> tuple[str, bool, str, int, bool, int]:
    """Черновик + метрики: качество текста, confidence 0–100, флаг проверки, seo_score 0–100."""
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
    lim = 3
    voice_training_examples = [
        t[:900] for t in get_voice_training_examples(str(user_id), limit=lim) if t
    ][:lim]
    priority_voice_overlay = get_voice_overlay_priority(user_id, limit=lim)
    voice_overlay = priority_voice_overlay or build_voice_examples_overlay(user_id, limit=lim)
    if voice_training_examples:
        logger.info("voice_overlay: %s примеров из voice_training", len(voice_training_examples))
    rules_overlay = build_editorial_rules_overlay(user_id)
    style_rej_overlay = build_editor_style_rejection_overlay(user_id, limit=10)
    approved_n = _approved_posts_count(user_id)
    if approved_n >= 10:
        logger.info("editor_voice: user_id=%s voice сформирован (%s+ апрувов)", user_id, approved_n)
    bundle = agent.gather_cross_reference_for_primary(
        title,
        snippet,
        url,
        exclude_domains=cross_ref_exclude_domains,
        search_days=cross_ref_days,
    )
    user_block = (
        f"Темы пользователя: {topics}\n"
        f"Заголовок источника: {title}\n"
        f"Краткое содержание: {snippet}\n"
        f"URL: {url}\n\n"
        f"{bundle.prompt_block}\n"
    )
    raw = agent.run_raw_completion(
        system=DRAFT_SYSTEM
        + voice_overlay
        + tg_overlay
        + finance_overlay
        + style_rej_overlay
        + rules_overlay,
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
    len_slack = quality_min_len_slack_from_prefs(_prefs(memory, user_id))
    ok_quality, quality_reason = _assess_draft_quality(text, min_len_slack=len_slack)
    if not ok_quality:
        logger.warning(
            "weak draft detected reason=%s url=%s",
            quality_reason,
            (url or "")[:300],
        )
    if len(text) > MAX_POST_CHARS:
        text = text[: MAX_POST_CHARS - 1] + "…"

    gen_title = (text.splitlines()[0] if text else "").strip()
    seo_score, _ = _seo_score_for_title(gen_title, _topic_keyword_hints(topics))
    confidence, needs_ver, contrad = agent.editorial_factcheck_scores(
        text,
        bundle.prompt_block,
        distinct_hosts=bundle.distinct_source_hosts,
    )
    if contrad:
        needs_ver = True
    if bundle.distinct_source_hosts < 2:
        needs_ver = True

    logger.info(
        "Draft metrics: seo_score=%s confidence=%s needs_verification=%s distinct_hosts=%s",
        seo_score,
        confidence,
        needs_ver,
        bundle.distinct_source_hosts,
    )
    return text, ok_quality, quality_reason, confidence, needs_ver, seo_score


def create_draft_from_search(
    agent: LLMAgent,
    memory: ChatMemory,
    user_id: int,
    *,
    excluded_urls: set[str] | None = None,
    temporary_topic: str | None = None,
) -> tuple[bool, int | str, str | None]:
    if not is_editor_enabled(memory, user_id):
        return False, "Редактор выключен — жми /editor_start в этом чате, и я проснусь ✍️", None
    base_prefs = _prefs(memory, user_id)
    if temporary_topic and str(temporary_topic).strip():
        prefs = dict(base_prefs)
        prefs[PREF_TOPICS] = str(temporary_topic).strip()[:500]
        logger.info(
            "create_draft_from_search: user_id=%s temporary_topic=%r",
            user_id,
            prefs[PREF_TOPICS][:200],
        )
    else:
        prefs = base_prefs
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
            "Разгреби ✅/✏️/❌, потом снова /drafts, /draft или /drafts ещё 📎",
            None,
        )
    logger.info(
        "create_draft_from_search: user_id=%s pending_drafts=%s limit=%s — можно создавать новый",
        user_id,
        pending_n,
        MAX_PENDING_UNAPPROVED_DRAFTS,
    )
    logger.info("Starting draft search for user %s", user_id)
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
    cross_excl = list(WEB_PROMO_DOMAINS) + list(
        getattr(agent.config, "blocked_search_domains", []) or []
    )
    cross_days = primary_search_window_days_from_prefs(prefs)
    cfg = getattr(agent, "config", None)
    auto_retry_weak = bool(getattr(cfg, "auto_retry_weak_drafts", False)) if cfg else False

    picked: DraftPick | None = None
    body = ""
    ok_quality = True
    quality_reason = "ok"
    prev_weak_body: str | None = None
    prev_weak_pick: DraftPick | None = None
    prev_confidence = 45
    prev_needs_ver = True
    prev_seo = 0
    draft_confidence: int | None = None
    draft_needs_ver = False
    draft_seo: float | None = None

    for attempt in range(2):
        picked = _pick_draft_item(
            agent,
            prefs,
            user_id,
            merged_excl,
            temporary_topic=temporary_topic,
        )
        if not picked:
            if attempt == 1 and prev_weak_body is not None and prev_weak_pick is not None:
                picked = prev_weak_pick
                body = _append_weak_draft_marker(prev_weak_body)
                ok_quality = False
                quality_reason = "retry_no_pick"
                draft_confidence = prev_confidence
                draft_needs_ver = True
                draft_seo = float(prev_seo)
                break
            streak = _pick_fail_streak_bump(memory, user_id)
            ex = _exhaustion_message_suffix(streak)
            if mode == "tg":
                return (
                    False,
                    "С публичных TG-каналов сейчас пусто — сеть, вёрстка t.me или смени список "
                    "«тгканалы:» в /editor_prefs. Веб не используется (источники:tg) 🛰️"
                    + ex,
                    None,
                )
            return (
                False,
                "Ничего подходящего не нашёл — расширь темы в /editor_prefs или попробуй позже 🔎"
                + ex,
                None,
            )

        body, ok_quality, quality_reason, confidence, needs_ver, seo_score = (
            draft_post_from_snippet(
                agent,
                memory,
                user_id,
                picked.title,
                picked.snippet,
                picked.url,
                topics,
                from_telegram=picked.from_telegram,
                telegram_channel_display=picked.telegram_display,
                cross_ref_days=cross_days,
                cross_ref_exclude_domains=cross_excl,
            )
        )
        draft_confidence = confidence
        draft_needs_ver = needs_ver
        draft_seo = float(seo_score)
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
            prev_confidence, prev_needs_ver, prev_seo = confidence, needs_ver, seo_score
            continue
        body = _append_weak_draft_marker(body)
        draft_needs_ver = True
        break

    if picked is None:
        streak = _pick_fail_streak_bump(memory, user_id)
        ex = _exhaustion_message_suffix(streak)
        return (
            False,
            "Ничего подходящего не нашёл — расширь темы в /editor_prefs или попробуй позже 🔎" + ex,
            None,
        )

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
        confidence_score=draft_confidence,
        requires_verification=draft_needs_ver,
        seo_score=draft_seo,
    )
    if not ok:
        return False, str(res), None
    if temporary_topic and str(temporary_topic).strip():
        logger.info(
            "create_draft_from_search: created draft_id=%s via temporary_topic=%r "
            "(negative prefs softened except categories with rejected feedback in window)",
            int(res),
            str(temporary_topic).strip()[:200],
        )
    _pick_fail_streak_reset(memory, user_id)
    row = get_draft(user_id, int(res))
    if not row:
        return False, "Черновик создался, но не читается из базы — мистика БД 🫠", None
    return True, int(res), draft_dm_text(row)


def create_draft_for_specific_topic(
    agent: LLMAgent,
    memory: ChatMemory,
    user_id: int,
    topic_name: str,
    *,
    excluded_urls: set[str] | None = None,
) -> tuple[bool, int | str, str | None]:
    topic_clean = (topic_name or "").strip()
    if len(topic_clean) < 2:
        return False, "Слишком короткая тема — напиши пару слов 🔎", None
    logger.info(
        "create_draft_for_specific_topic: user_id=%s topic=%r",
        user_id,
        topic_clean[:200],
    )
    return create_draft_from_search(
        agent,
        memory,
        user_id,
        excluded_urls=excluded_urls,
        temporary_topic=topic_clean[:500],
    )


def maybe_note_shorter_edit(memory: ChatMemory, user_id: int, old: str, new: str) -> None:
    if len(new) < len(old) * 0.82:
        memory.update_style_preferences(
            user_id,
            {"content_editor_response_short": "1"},
        )
