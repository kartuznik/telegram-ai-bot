"""Редактор контента: Tavily + черновик → апрув в ЛС → публикация в канал @kriptogeograph."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Config
from app.database import get_connection
from app.llm_agent import LLMAgent
from app.memory import ChatMemory

logger = logging.getLogger(__name__)

try:
    from app.news_bot_patterns import match_categories, score_text

    USE_PATTERNS = True
except ImportError:
    USE_PATTERNS = False

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

DEFAULT_AUTO_INTERVAL_HOURS = 0.5
MIN_AUTO_INTERVAL_HOURS = 0.5
MAX_AUTO_INTERVAL_HOURS = 168
# Максимум черновиков в статусе draft на пользователя (ручной /drafts, авто-поиск, insert).
MAX_PENDING_UNAPPROVED_DRAFTS = 6
# Окна исключения source_url (подставляются из Config в init_content_editor_defaults).
_EXCLUDE_POSTED_DAYS = 14
_EXCLUDE_REJECTED_DAYS = 7

AUTO_DIRECTIVE_RE = re.compile(
    r"(?is)(?<!\S)авто\s*:\s*((?:\d{1,3}(?:[.,]\d+)?|off|выкл|0))\b",
)
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
CALLBACK_PREFIX = "editor:"

_pending_edit: dict[int, int] = {}

DRAFT_SYSTEM = (
    "Ты — Кузьма, редактор коротких постов для Telegram-канала @kriptogeograph. "
    "Темы задаёт пользователь — это могут быть новости, наука, шоу-бизнес, технологии, путешествия, спорт и что угодно ещё; "
    "не впихивай финансовую рамку, если материал не про деньги, рынки или вложения.\n"
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


def editor_needs_telegram_feed(prefs: dict[str, str]) -> bool:
    return get_source_mode(prefs) in ("tg", "both")


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


def format_editor_info_text(prefs: dict[str, str]) -> str:
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
    return (
        "Текущие настройки редактора:\n\n"
        f"• Источники: {sm} (web / tg / both)\n"
        f"• ТГ-каналы: {tg_line}\n"
        f"  ({tg_note})\n\n"
        f"• Темы: {topics[:500]}{'…' if len(topics) > 500 else ''}\n"
        f"• Уточнение к поиску: {sources[:500]}{'…' if len(sources) > 500 else ''}\n\n"
        f"• Авто-поиск: {auto}"
        + (f", интервал {format_auto_interval_label(ah)}" if auto_on else "")
        + "\n\n"
        "Команды настройки (можно смешивать в одной строке):\n"
        "• темы: … или темы … — только темы и уточнение (через запятую после первой темы);\n"
        "• тгканалы: … или тгканалы @a @b — только список каналов;\n"
        "• источники: web|tg|both — режим материалов.\n"
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


def reject_spree_should_pause(prefs: dict[str, str]) -> bool:
    rc = int(prefs.get(PREF_REJECT_COUNT, "0") or 0)
    ac = int(prefs.get(PREF_APPROVE_COUNT, "0") or 0)
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
    if act not in (CB_APPROVE, CB_EDIT, CB_REJECT):
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
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"editor:{CB_REJECT}:{draft_id}")],
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


def insert_draft(
    user_id: int,
    channel_id: str,
    content: str,
    source_url: str | None,
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
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO draft_posts (user_id, channel_id, content, source_url, status)
                VALUES (?, ?, ?, ?, 'draft')
                """,
                (uid, channel_id, body, source_url or None),
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
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at, media_url
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
                UPDATE draft_posts SET content=? WHERE id=? AND user_id=? AND status='draft'
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
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at, media_url
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
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, channel_id, content, source_url, status, created_at, approved_at, media_url
                FROM draft_posts WHERE user_id=? AND status=?
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


def _pick_draft_item(
    agent: LLMAgent,
    prefs: dict[str, str],
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

    candidates: list[dict[str, Any]] = []

    if mode in ("web", "both") and agent.tavily:
        q = f"{topics} {sources}".strip() + " последние новости"
        tail = _reject_hints_for_tavily_query(rejects)
        if tail:
            q += ". Исключай или обходи материалы, связанные с: " + ", ".join(tail)
        result = agent._tavily_search(q[:400], max_results=6)
        if result and isinstance(result.get("results"), list):
            for it in result["results"]:
                if isinstance(it, dict):
                    candidates.append(
                        {
                            "title": (it.get("title") or "Без заголовка").strip(),
                            "url": (it.get("url") or "").strip(),
                            "snippet": _snippet_from_tavily_item(it)[:800],
                            "from_tg": False,
                            "tg_disp": "",
                        }
                    )
        else:
            logger.warning(
                "Pick draft: web пустой или нет results (mode=%s, has_result=%s)",
                mode,
                bool(result),
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
        for p in fetch_many_channels(chans, per_channel=2):
            candidates.append(
                {
                    "title": (p.get("title") or "Пост из Telegram").strip(),
                    "url": (p.get("url") or "").strip(),
                    "snippet": ((p.get("content") or "").strip())[:800],
                    "from_tg": True,
                    "tg_disp": f"@{p.get('channel_username', '')}",
                }
            )

    logger.info(
        "Pick draft: source_mode=%s кандидатов=%s (web+TG)",
        mode,
        len(candidates),
    )
    if not candidates:
        return None

    ranked: list[tuple[int, int, int, dict[str, Any], str, dict[str, int], dict[str, list[str]]]] = []
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
        ranked.append((soft_pen, -total_score, i, c, pattern_reason, score_map, cat_matches))
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))

    n_excluded = n_hard_host = n_url_rej = n_kw = n_no_url = 0
    for soft_pen, _score_sort, _idx, c, pattern_reason, score_map, cat_matches in ranked:
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
        title = (c.get("title") or "").strip() or "Без заголовка"
        content = (c.get("snippet") or "").strip()
        blob = (title + " " + content).lower()
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
        logger.info(
            "Pick draft: выбран url=%r reject_key=%s netloc=%s tg=%s soft_penalty=%s total_score=%s",
            url[:120],
            rj_key or "?",
            _host(url) or "?",
            c.get("from_tg"),
            soft_pen,
            sum(score_map.values()) if USE_PATTERNS else 0,
        )
        return DraftPick(
            title=title,
            url=url,
            snippet=content[:800],
            from_telegram=bool(c.get("from_tg")),
            telegram_display=str(c.get("tg_disp") or ""),
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
            "url_reject=%s kw=%s no_url=%s)",
            mode,
            len(candidates),
            n_excluded,
            n_hard_host,
            n_url_rej,
            n_kw,
            n_no_url,
        )
    return None


def draft_post_from_snippet(
    agent: LLMAgent,
    title: str,
    snippet: str,
    url: str,
    topics: str,
    *,
    from_telegram: bool = False,
    telegram_channel_display: str = "",
) -> str:
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
    user_block = (
        f"Темы пользователя: {topics}\n"
        f"Заголовок источника: {title}\n"
        f"Краткое содержание: {snippet}\n"
        f"URL: {url}\n"
    )
    raw = agent.run_raw_completion(
        system=DRAFT_SYSTEM + tg_overlay + finance_overlay,
        user=user_block,
        max_tokens=1200,
        temperature=min(0.82, getattr(agent, "_chat_temperature", 0.75) + 0.05),
    )
    text = (raw or "").strip()
    preview = text[:100].replace("\n", " ")
    logger.info("Draft from snippet: len=%s chars, preview=%r", len(text), preview)
    if not text:
        logger.error("Draft from snippet: GPT вернул пустой completion")
    if len(text) > MAX_POST_CHARS:
        text = text[: MAX_POST_CHARS - 1] + "…"
    return text


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
    picked = _pick_draft_item(agent, prefs, merged_excl)
    if not picked:
        if mode == "tg":
            return (
                False,
                "С публичных TG-каналов сейчас пусто — сеть, вёрстка t.me или смени список "
                "«тгканалы:» в /editor_prefs. Веб не используется (источники:tg) 🛰️",
                None,
            )
        return False, "Ничего подходящего не нашёл — расширь темы в /editor_prefs или попробуй позже 🔎", None
    topics = prefs.get(PREF_TOPICS) or "новости"
    body = draft_post_from_snippet(
        agent,
        picked.title,
        picked.snippet,
        picked.url,
        topics,
        from_telegram=picked.from_telegram,
        telegram_channel_display=picked.telegram_display,
    )
    logger.info(
        "create_draft_from_search: черновик после GPT, len=%s, source_url=%r tg=%s",
        len(body),
        (picked.url or "")[:120],
        picked.from_telegram,
    )
    ch = prefs.get(PREF_CHANNEL) or DEFAULT_EDITOR_CHANNEL_ID
    ok, res = insert_draft(user_id, ch, body, picked.url)
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
