import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Домены, нежелательные для черновиков (web + Tavily exclude_domains), если BLOCKED_SEARCH_DOMAINS не задан.
_DEFAULT_BLOCKED_SEARCH_DOMAINS: tuple[str, ...] = (
    "tiktok.com",
    "vm.tiktok.com",
    "store.steampowered.com",
    "epicgames.com",
    "apps.apple.com",
    "play.google.com",
)


def _parse_blocked_search_domains_env() -> list[str]:
    """Список доменов из BLOCKED_SEARCH_DOMAINS; пустая строка = явно пустой список."""
    raw = os.getenv("BLOCKED_SEARCH_DOMAINS")
    if raw is None:
        return list(_DEFAULT_BLOCKED_SEARCH_DOMAINS)
    s = raw.strip()
    if not s:
        return []
    return [p.strip().lower() for p in s.split(",") if p.strip()]


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


def _parse_int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _parse_temperature(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _parse_float_env(name: str, default: float, lo: float, hi: float) -> float:
    """Общий парсер float из .env (запятая как десятичный разделитель допускается)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _parse_log_level_env(name: str, default: str = "INFO") -> str:
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
    return raw if raw in allowed else default


@dataclass
class Config:
    telegram_token: str
    openai_api_key: str
    tavily_api_key: str | None
    proxy_host: str | None
    proxy_port: str | None
    proxy_username: str | None
    proxy_password: str | None
    telegram_proxy_host: str | None
    telegram_proxy_port: str | None
    telegram_proxy_username: str | None
    telegram_proxy_password: str | None
    admin_id: int | None
    model_name: str = "gpt-4o"
    max_history_messages: int = 20
    chat_temperature: float = 0.75
    vision_temperature: float = 0.38
    concierge_enabled: bool = True
    anchors_enabled: bool = True
    content_editor_autofetch_enabled: bool = False
    tavily_freshness_days: int = 2
    log_level: str = "INFO"
    telegram_proxy_fallback_direct: bool = False
    max_user_templates: int = 30
    max_user_anchors: int = 25
    # Tavily: HTTP-таймаут одной попытки (сек), число повторов после первой неудачи, множитель паузы между повторами.
    # База 36 с и рост 1.5x на попытку (в коде агента) даёт лестницу ~36→54→72 с при двух ретраях, в пределах cap SDK 120 с.
    tavily_timeout_seconds: int = 36
    tavily_max_retries: int = 2
    tavily_retry_backoff_multiplier: float = 2.0
    # Список username публичных TG-каналов для черновиков (через t.me/s/…), через запятую без @.
    content_editor_tg_default_channels: str = (
        "rian_ru,readovkanews,meduzalive,tass_agency,thebell_io"
    )
    # Не предлагать снова source_url из опубликованных постов (по approved_at) и отклонённых (по created_at).
    # POSTED_EXCLUSION_DAYS в .env — алиас к CONTENT_EDITOR_EXCLUDE_POSTED_DAYS (если задано числом).
    content_editor_exclude_posted_days: int = 30
    content_editor_exclude_rejected_days: int = 7
    # Мин. «эффективный» pattern-score для веб-кандидата в _pick_draft_item (× quality × breaking).
    draft_pick_min_web_effective_score: float = 1.5
    # Блокировка доменов в редакторе (Pick draft) и в Tavily exclude_domains; переопределяется env.
    blocked_search_domains: list[str] = field(
        default_factory=lambda: list(_DEFAULT_BLOCKED_SEARCH_DOMAINS)
    )
    auto_retry_weak_drafts: bool = field(
        default_factory=lambda: os.getenv("AUTO_RETRY_WEAK_DRAFTS", "false").lower()
        == "true"
    )
    feedback_decay_rate: float = 0.88
    feedback_window_size: int = 20
    feedback_min_count_for_full_pref: int = 1
    feedback_low_count_scale: float = 0.0
    feedback_pref_max_gain: float = 0.10
    feedback_pref_gain_per_unit: float = 0.15
    feedback_novelty_bonus: float = 0.05
    novelty_recent_drafts: int = 10
    diversity_same_category_window: int = 3
    diversity_narrow_mix_window: int = 5
    diversity_narrow_mix_categories: int = 2
    diversity_same_category_penalty: float = 0.30
    diversity_other_categories_bonus: float = 0.20


def load_config() -> Config:
    load_dotenv()

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip() or None
    proxy_host = os.getenv("PROXY_HOST", "").strip() or None
    proxy_port = os.getenv("PROXY_PORT", "").strip() or None
    proxy_username = os.getenv("PROXY_USERNAME", "").strip() or None
    proxy_password = os.getenv("PROXY_PASSWORD", "").strip() or None
    telegram_proxy_host = os.getenv("TELEGRAM_PROXY_HOST", "").strip() or None
    telegram_proxy_port = os.getenv("TELEGRAM_PROXY_PORT", "").strip() or None
    telegram_proxy_username = os.getenv("TELEGRAM_PROXY_USERNAME", "").strip() or None
    telegram_proxy_password = os.getenv("TELEGRAM_PROXY_PASSWORD", "").strip() or None

    if not telegram_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is missing in .env")

    admin_raw = os.getenv("ADMIN_ID", "").strip()
    admin_id: int | None = None
    if admin_raw:
        try:
            admin_id = int(admin_raw)
        except ValueError:
            admin_id = None

    chat_temp = _parse_temperature("MODEL_TEMPERATURE", 0.75, 0.5, 0.9)
    vision_temp = _parse_temperature("VISION_MODEL_TEMPERATURE", 0.38, 0.15, 0.55)
    concierge_on = _parse_bool_env("CONCIERGE_ENABLED", True)
    anchors_on = _parse_bool_env("ANCHORS_ENABLED", True)
    autofetch_on = _parse_bool_env("CONTENT_EDITOR_AUTOFETCH_ENABLED", False)
    # PREF_AUTO_ENABLED в .env — алиас «выключить autofetch» (0/false), если явный флаг не задан.
    pref_auto_raw = os.getenv("PREF_AUTO_ENABLED", "").strip().lower()
    if pref_auto_raw in ("0", "false", "no", "off", "нет") and os.getenv(
        "CONTENT_EDITOR_AUTOFETCH_ENABLED", ""
    ).strip() == "":
        autofetch_on = False
    tavily_freshness_days = _parse_int_env("TAVILY_FRESHNESS_DAYS", 2, 1, 30)
    log_level = _parse_log_level_env("LOG_LEVEL", "INFO")
    telegram_proxy_fallback = _parse_bool_env("TELEGRAM_PROXY_FALLBACK_DIRECT", False)
    max_templates = _parse_int_env("MAX_USER_TEMPLATES", 30, 1, 100)
    max_anchors = _parse_int_env("MAX_USER_ANCHORS", 25, 0, 100)
    tavily_timeout = _parse_int_env("TAVILY_TIMEOUT_SECONDS", 36, 10, 120)
    tavily_retries = _parse_int_env("TAVILY_MAX_RETRIES", 2, 0, 5)
    tavily_backoff = _parse_float_env("TAVILY_RETRY_BACKOFF_MULTIPLIER", 2.0, 1.0, 4.0)
    tg_default_ch = os.getenv("CONTENT_EDITOR_TG_DEFAULT_CHANNELS", "").strip()
    if not tg_default_ch:
        tg_default_ch = "rian_ru,readovkanews,meduzalive,tass_agency,thebell_io"
    posted_alt = os.getenv("POSTED_EXCLUSION_DAYS", "").strip()
    ex_posted_default = int(posted_alt) if posted_alt.isdigit() else 30
    ex_posted = _parse_int_env(
        "CONTENT_EDITOR_EXCLUDE_POSTED_DAYS", ex_posted_default, 1, 365
    )
    ex_rejected = _parse_int_env("CONTENT_EDITOR_EXCLUDE_REJECTED_DAYS", 7, 1, 365)
    draft_min_web_eff = _parse_float_env(
        "DRAFT_PICK_MIN_WEB_EFFECTIVE_SCORE", 1.5, 0.1, 50.0
    )
    blocked_domains = _parse_blocked_search_domains_env()
    feedback_decay_rate = _parse_float_env("FEEDBACK_DECAY_RATE", 0.88, 0.5, 0.999)
    feedback_window_size = _parse_int_env("FEEDBACK_WINDOW_SIZE", 20, 5, 100)
    feedback_min_pref = _parse_int_env("FEEDBACK_MIN_COUNT_FOR_FULL_PREF", 1, 1, 50)
    feedback_low_scale = _parse_float_env("FEEDBACK_LOW_COUNT_SCALE", 0.0, 0.0, 1.0)
    feedback_pref_max_gain = _parse_float_env("FEEDBACK_PREF_MAX_GAIN", 0.10, 0.0, 0.5)
    feedback_pref_gain_per_unit = _parse_float_env(
        "FEEDBACK_PREF_GAIN_PER_UNIT", 0.15, 0.0, 1.0
    )
    feedback_novelty_bonus = _parse_float_env("FEEDBACK_NOVELTY_BONUS", 0.05, 0.0, 0.5)
    novelty_recent_drafts = _parse_int_env("NOVELTY_RECENT_DRAFTS", 10, 1, 50)
    div_same_window = _parse_int_env("DIVERSITY_SAME_CATEGORY_WINDOW", 3, 2, 10)
    div_mix_window = _parse_int_env("DIVERSITY_NARROW_MIX_WINDOW", 5, 3, 15)
    div_mix_cats = _parse_int_env("DIVERSITY_NARROW_MIX_CATEGORIES", 2, 1, 5)
    div_same_penalty = _parse_float_env("DIVERSITY_SAME_CATEGORY_PENALTY", 0.30, 0.0, 0.9)
    div_other_bonus = _parse_float_env("DIVERSITY_OTHER_CATEGORIES_BONUS", 0.20, 0.0, 1.0)

    return Config(
        telegram_token=telegram_token,
        openai_api_key=openai_api_key,
        tavily_api_key=tavily_api_key,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        telegram_proxy_host=telegram_proxy_host,
        telegram_proxy_port=telegram_proxy_port,
        telegram_proxy_username=telegram_proxy_username,
        telegram_proxy_password=telegram_proxy_password,
        admin_id=admin_id,
        chat_temperature=chat_temp,
        vision_temperature=vision_temp,
        concierge_enabled=concierge_on,
        anchors_enabled=anchors_on,
        content_editor_autofetch_enabled=autofetch_on,
        tavily_freshness_days=tavily_freshness_days,
        log_level=log_level,
        telegram_proxy_fallback_direct=telegram_proxy_fallback,
        max_user_templates=max_templates,
        max_user_anchors=max_anchors,
        tavily_timeout_seconds=tavily_timeout,
        tavily_max_retries=tavily_retries,
        tavily_retry_backoff_multiplier=tavily_backoff,
        content_editor_tg_default_channels=tg_default_ch,
        content_editor_exclude_posted_days=ex_posted,
        content_editor_exclude_rejected_days=ex_rejected,
        draft_pick_min_web_effective_score=draft_min_web_eff,
        blocked_search_domains=blocked_domains,
        feedback_decay_rate=feedback_decay_rate,
        feedback_window_size=feedback_window_size,
        feedback_min_count_for_full_pref=feedback_min_pref,
        feedback_low_count_scale=feedback_low_scale,
        feedback_pref_max_gain=feedback_pref_max_gain,
        feedback_pref_gain_per_unit=feedback_pref_gain_per_unit,
        feedback_novelty_bonus=feedback_novelty_bonus,
        novelty_recent_drafts=novelty_recent_drafts,
        diversity_same_category_window=div_same_window,
        diversity_narrow_mix_window=div_mix_window,
        diversity_narrow_mix_categories=div_mix_cats,
        diversity_same_category_penalty=div_same_penalty,
        diversity_other_categories_bonus=div_other_bonus,
    )
