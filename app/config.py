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
    content_editor_exclude_posted_days: int = 14
    content_editor_exclude_rejected_days: int = 7
    # Блокировка доменов в редакторе (Pick draft) и в Tavily exclude_domains; переопределяется env.
    blocked_search_domains: list[str] = field(
        default_factory=lambda: list(_DEFAULT_BLOCKED_SEARCH_DOMAINS)
    )


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
    log_level = _parse_log_level_env("LOG_LEVEL", "INFO")
    telegram_proxy_fallback = _parse_bool_env("TELEGRAM_PROXY_FALLBACK_DIRECT", False)
    max_templates = _parse_int_env("MAX_USER_TEMPLATES", 30, 1, 100)
    max_anchors = _parse_int_env("MAX_USER_ANCHORS", 25, 1, 100)
    tavily_timeout = _parse_int_env("TAVILY_TIMEOUT_SECONDS", 36, 10, 120)
    tavily_retries = _parse_int_env("TAVILY_MAX_RETRIES", 2, 0, 5)
    tavily_backoff = _parse_float_env("TAVILY_RETRY_BACKOFF_MULTIPLIER", 2.0, 1.0, 4.0)
    tg_default_ch = os.getenv("CONTENT_EDITOR_TG_DEFAULT_CHANNELS", "").strip()
    if not tg_default_ch:
        tg_default_ch = "rian_ru,readovkanews,meduzalive,tass_agency,thebell_io"
    ex_posted = _parse_int_env("CONTENT_EDITOR_EXCLUDE_POSTED_DAYS", 14, 1, 365)
    ex_rejected = _parse_int_env("CONTENT_EDITOR_EXCLUDE_REJECTED_DAYS", 7, 1, 365)
    blocked_domains = _parse_blocked_search_domains_env()

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
        blocked_search_domains=blocked_domains,
    )
