import asyncio
import base64
import logging
import re
import time
import mimetypes
import os
import tempfile
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
)
from aiohttp import ClientError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.admin import (
    NO_ADMIN_RIGHTS,
    ban_user,
    broadcast_message,
    get_users_preview,
    init_admin_db,
    is_admin,
    unban_user,
)
from app.content_editor import (
    AUTO_DIRECTIVE_RE,
    DRAFT_SYSTEM,
    DEFAULT_EDITOR_CHANNEL_ID,
    PREF_APPROVE_COUNT,
    PREF_AUTO_INTRO_SENT,
    PREF_LAST_AUTO_FETCH_TS,
    PREF_CHANNEL,
    PREF_ENABLED,
    PREF_REJECT_COUNT,
    PREF_SEARCH_WINDOW_DAYS,
    PREF_SOURCES,
    PREF_TG_CHANNELS,
    PREF_TOPICS,
    PREF_SOURCE_MODE,
    PREF_AUTO_DISABLED_REASON,
    PREF_AUTO_ENABLED,
    PREF_AUTO_INTERVAL_HOURS,
    MAX_AUTO_INTERVAL_HOURS,
    MIN_AUTO_INTERVAL_HOURS,
    _fmt_hours_value,
    auto_interval_hours_from_prefs,
    draft_deadline_hours_from_prefs,
    detect_primary_category,
    estimate_feedback_quality_score,
    append_reject_hint,
    extract_tg_channel_username_from_url,
    MAX_PENDING_UNAPPROVED_DRAFTS,
    MAX_SEARCH_WINDOW_DAYS,
    MIN_SEARCH_WINDOW_DAYS,
    build_editor_keyboard,
    bump_approve,
    channel_publish_text_from_draft_body,
    count_drafts,
    create_draft_from_search,
    draft_dm_text,
    build_editorial_rules_overlay,
    build_voice_examples_overlay,
    format_auto_interval_label,
    format_editor_info_text,
    format_search_settings_message,
    format_search_window_settings_message,
    format_sources_settings_message,
    format_topics_settings_message,
    get_draft,
    get_oldest_draft,
    get_pending_edit,
    get_pending_feedback,
    is_draft_expired,
    get_source_mode,
    hint_for_reject_from_draft,
    init_content_editor_defaults,
    insert_draft,
    is_auto_enabled_pref,
    is_editor_callback,
    is_editor_enabled,
    is_private_chat,
    maybe_distill_editorial_rules_sync,
    merge_sources_into_pref,
    merge_topics_into_pref,
    maybe_note_shorter_edit,
    migrate_strip_vesti_reject_hints,
    parse_auto_directive_from_rest,
    parse_deadline_directive_from_rest,
    parse_editor_callback,
    remove_sources_from_pref,
    remove_topics_from_pref,
    parse_editor_extra_directives,
    pop_pending_edit,
    pop_pending_feedback,
    reset_editor_reject_state,
    set_draft_status,
    set_pending_edit,
    set_pending_feedback,
    split_source_command_tokens,
    split_topic_command_tokens,
    sources_list_from_pref,
    topics_list_from_pref,
    update_draft_content,
)
from app.content_editor_background import run_content_editor_autofetch_loop
from app.scenario_simulator import (
    build_scenario_choice_keyboard,
    build_scenario_deep_keyboard,
    classify_scenario_request,
    format_scenario_intro,
    generate_scenarios,
    get_session,
    is_scenario_callback,
    parse_scenario_callback,
    put_session,
    run_scenario_expand,
)
from app.database import (
    get_editorial_feedbacks_baseline,
    get_editorial_rules,
    init_db,
    save_draft_feedback,
)
from app.health_check import (
    OWNER_RESTART_USER_ID,
    attach_self_diagnostics,
    restart_process,
    router as health_check_router,
)
from app.self_diagnostics import SelfDiagnostics, install_diagnostics_heartbeat_middleware
from app.config import load_config
from app.middlewares.admin_auth import AdminAuthMiddleware
from app.middlewares.ban_check import BanCheckMiddleware
from app.statistics import (
    bump_channel_quality,
    get_daily_breakdown,
    get_stats,
    get_top_users,
    init_stats_db,
    log_user_message,
)
from app.llm_agent import LLMAgent
from app.memory import ChatMemory
from app.proxy_utils import (
    telegram_socks5_proxy_url_from_config,
)
from app.pdf_extractor import extract_text_from_pdf, extract_text_from_txt
from app.telegram_ui import (
    CALLBACK_START_HELP,
    build_default_keyboard,
    build_help_text,
    build_keyboard_from_buttons,
    build_start_keyboard,
    reply_with_help_text,
    setup_bot_command_menu,
)
from app.user_anchors import (
    auto_title_anchor,
    build_anchor_view_keyboard,
    build_anchors_list_keyboard,
    classify_anchor_command,
    delete_anchor_by_id,
    delete_anchor_by_title,
    format_anchor_message,
    build_anchor_snippet_and_ref,
    get_anchor,
    insert_anchor,
    is_anchor_callback,
    list_anchor_rows,
    parse_anchor_callback,
)
from app.user_templates import (
    auto_title_from_content,
    build_template_view_keyboard,
    build_templates_list_keyboard,
    classify_save_template_command,
    delete_template_by_id,
    delete_template_by_title,
    extract_save_title,
    format_template_body,
    get_last_assistant_reply,
    get_template,
    insert_template,
    is_template_callback,
    list_template_rows,
    looks_like_list_templates_command,
    parse_delete_template_title,
    parse_template_callback,
    user_explicitly_wants_own_message_saved,
)


config = load_config()
logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO))
logger = logging.getLogger(__name__)

memory = ChatMemory(max_messages=config.max_history_messages)
init_content_editor_defaults(config)
migrate_strip_vesti_reject_hints(memory)
agent = LLMAgent(config=config, memory=memory)

_self_diagnostics = SelfDiagnostics(
    config=config,
    agent=agent,
    restart_process=restart_process,
)
attach_self_diagnostics(_self_diagnostics)

router = Router()

_telegram_direct_fallback_applied = False
_DRAFT_TRIGGER_WORDS = ("в канал", "пост", "черновик", "draft", "в редактор")
_FILE_DRAFT_CONTEXT_TTL_SEC = 180.0
_recent_user_text_for_draft: dict[int, tuple[str, float, int]] = {}
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
EXPIRED_APPROVE_YES = "editor_expired_yes"
EXPIRED_APPROVE_NO = "editor_expired_no"
FEEDBACK_SKIP = "feedback_skip"

_FEEDBACK_WHY_SYSTEM = (
    "Ты — Кузьма, редактор крипто-новостного канала. Пользователь только что принял решение по черновику поста "
    "(апрув, отказ или правка). Задай ОДИН короткий живой вопрос (1–2 предложения), который проясняет мотивацию "
    "и тон решения — без канцелярита, не начинай с «почему» / «зачем именно», можно с лёгким юмором. "
    "Только сам вопрос, без преамбулы и без кавычек вокруг."
)


def _map_editor_action_to_feedback(action: str) -> str:
    a = (action or "").strip().lower()
    if a == "approved":
        return "апрув (пост ушёл в канал)"
    if a == "rejected":
        return "отказ"
    if a == "edited":
        return "правка текста черновика"
    if a == "expired_content":
        return "материал устарел (не отказ по качеству источника)"
    return a or "решение по черновику"


def _feedback_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить →", callback_data=FEEDBACK_SKIP)]
        ]
    )


async def _ask_why_after_action(
    bot: Bot,
    chat_id: int,
    user_id: int,
    draft_id: int | None,
    action: str,
    draft_body: str,
) -> None:
    """Асинхронно: один GPT-вопрос после ✅/❌/✏️; pending ставим только после удачной отправки."""
    preview = (draft_body or "")[:400]
    act_label = _map_editor_action_to_feedback(action)
    user_prompt = (
        f"Решение: {act_label}\n\n"
        f"Фрагмент черновика (для контекста):\n{preview}\n\n"
        "Сформулируй один вопрос пользователю."
    )
    try:
        q = await asyncio.to_thread(
            lambda: agent.run_raw_completion(
                system=_FEEDBACK_WHY_SYSTEM,
                user=user_prompt,
                temperature=0.85,
            )
        )
    except Exception as exc:
        logger.warning("feedback_why: GPT error user_id=%s: %s", user_id, exc)
        return
    q = (q or "").strip()
    if not q:
        return
    if len(q) > 1000:
        q = q[:997] + "..."
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=q,
            reply_markup=_feedback_skip_keyboard(),
        )
    except TelegramBadRequest as exc:
        logger.warning("feedback_why: send failed user_id=%s: %s", user_id, exc)
        return
    draft_title = (draft_body.splitlines()[0] if draft_body else "").strip()
    category = detect_primary_category(draft_title, draft_body)
    q_score = estimate_feedback_quality_score(action)
    set_pending_feedback(
        user_id,
        draft_id,
        action,
        draft_body,
        category=category,
        quality_score=q_score,
    )


def _schedule_ask_why(
    bot: Bot,
    chat_id: int,
    user_id: int,
    draft_id: int | None,
    action: str,
    draft_body: str,
) -> None:
    asyncio.create_task(
        _ask_why_after_action(bot, chat_id, user_id, draft_id, action, draft_body)
    )


def _expired_approve_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, публикуем",
                    callback_data=f"{EXPIRED_APPROVE_YES}:{draft_id}",
                ),
                InlineKeyboardButton(
                    text="Нет, отменить",
                    callback_data=f"{EXPIRED_APPROVE_NO}:{draft_id}",
                ),
            ]
        ]
    )


async def _switch_telegram_to_direct_session(bot: Bot) -> None:
    """Закрывает прокси-сессию и подставляет обычный AiohttpSession (один раз за процесс)."""
    global _telegram_direct_fallback_applied
    if _telegram_direct_fallback_applied:
        return
    old = bot.session
    try:
        await old.close()
    except Exception as exc:
        logger.debug("Telegram session close: %s", exc)
    bot.session = AiohttpSession()
    _telegram_direct_fallback_applied = True
    logger.warning(
        "Telegram Bot API: включено прямое подключение (TELEGRAM_PROXY_FALLBACK_DIRECT)"
    )


async def safe_send_chat_action(
    bot: Bot,
    chat_id: int,
    action: str = "typing",
) -> None:
    """send_chat_action с retry; сетевые сбои прокси не роняют хендлер."""
    delays = (1.0, 2.0, 4.0)
    last_exc: BaseException | None = None

    for attempt in range(3):
        try:
            await bot.send_chat_action(chat_id, action)
            return
        except asyncio.CancelledError:
            raise
        except TelegramAPIError as exc:
            if not isinstance(exc, TelegramNetworkError):
                logger.debug("send_chat_action: без retry (%s)", exc)
                return
            last_exc = exc
        except (ClientError, TimeoutError, OSError) as exc:
            last_exc = exc
        except Exception as exc:
            last_exc = exc

        if attempt < 2:
            await asyncio.sleep(delays[attempt])

    logger.warning(
        "send_chat_action: сбой после 3 попыток (chat_id=%s): %s",
        chat_id,
        last_exc,
    )

    if (
        config.telegram_proxy_fallback_direct
        and telegram_socks5_proxy_url_from_config(config)
        and not _telegram_direct_fallback_applied
    ):
        try:
            await _switch_telegram_to_direct_session(bot)
            try:
                await bot.send_chat_action(chat_id, action)
            except Exception as exc:
                logger.warning("send_chat_action после fallback: %s", exc)
        except Exception:
            logger.exception("Не удалось переключить Telegram на прямую сессию")


def build_telegram_session() -> AiohttpSession | None:
    # Proxy applies only to Telegram API requests.
    proxy_url = telegram_socks5_proxy_url_from_config(config)
    if not proxy_url:
        logger.info("Telegram: direct connection")
        return None
    host = (config.telegram_proxy_host or "").strip()
    port = (config.telegram_proxy_port or "1080").strip()
    logger.info("Telegram: proxy %s:%s", host, port)
    return AiohttpSession(proxy=proxy_url)


async def ensure_bot_identity(bot: Bot) -> None:
    delays = (5, 6, 7, 8)
    last_exc: BaseException | None = None
    for attempt in range(1, 6):
        try:
            await bot.get_me()
            if attempt > 1:
                logger.info("bot.get_me(): успешно на попытке %s/5", attempt)
            return
        except TelegramConflictError:
            raise
        except (TelegramNetworkError, ClientError, TimeoutError, OSError) as exc:
            last_exc = exc
        except Exception as exc:
            last_exc = exc

        if attempt < 5:
            delay = delays[attempt - 1]
            logger.warning(
                "bot.get_me(): попытка %s/5 неуспешна (%s). Повтор через %ss",
                attempt,
                last_exc,
                delay,
            )
            await asyncio.sleep(delay)

    logger.error("bot.get_me(): все 5 попыток неуспешны, завершаю запуск")
    if last_exc:
        raise last_exc
    raise RuntimeError("bot.get_me(): startup failed after 5 attempts")


async def _send_templates_list(message: Message, user_id: int) -> None:
    rows = list_template_rows(user_id)
    if not rows:
        await message.answer(
            "Пока пусто — сохрани любой мой ответ фразой вроде «сохрани это», "
            "и потом заглядывай сюда как в блокнот удачи 📔"
        )
        return
    kb = build_templates_list_keyboard(rows)
    await message.answer(
        f"Вот твои шаблоны ({len(rows)} шт.) — жми на название, открою текст:",
        reply_markup=kb,
    )


async def _send_anchors_list(message: Message, user_id: int) -> None:
    rows = list_anchor_rows(user_id)
    if not rows:
        await message.answer(
            "Пока якорей нет — поставь метку фразой вроде «запомни этот момент» или «якорь: тема», "
            "и я пришпилю разговор, как стикер на холодильник 🧲🔖"
        )
        return
    kb = build_anchors_list_keyboard(rows)
    await message.answer(
        f"Твои якоря ({len(rows)} шт.) — жми на название, вспомним момент:",
        reply_markup=kb,
    )


async def send_ai_reply(
    message: Message, text: str, buttons: list[dict[str, str]] | None = None
) -> None:
    if buttons:
        keyboard = build_keyboard_from_buttons(buttons)
    else:
        keyboard, _ = build_default_keyboard()
    await message.answer(text, reply_markup=keyboard)


def resolve_proactive_buttons(
    user_id: int,
    context_text: str,
    model_buttons: list[dict[str, str]] | None,
    is_generic: bool = False,
    user_query: str | None = None,
) -> list[dict[str, str]]:
    """Возвращает кнопки: от модели ИЛИ фоллбэк если модель дала универсальные."""

    logger.info("Кнопки от GPT: %s", [b["text"] for b in (model_buttons or [])])

    if model_buttons and len(model_buttons) >= 2 and not is_generic:
        logger.info("Источник кнопок: GPT | Кнопки универсальные? Нет | Фоллбэк? Нет")
        return model_buttons[:3]

    reasons: list[str] = []
    if not model_buttons:
        reasons.append("нет JSON от GPT")
    elif len(model_buttons) < 2:
        reasons.append("мало кнопок (<2)")
    if is_generic:
        reasons.append("generic-текст в кнопках")
    logger.info(
        "Источник кнопок: fallback | Причина: %s | Фоллбэк? Да",
        ", ".join(reasons) if reasons else "неизвестно",
    )
    snippet = context_text[:200].replace("\n", " ")
    if len(context_text) > 200:
        snippet += "..."
    logger.info(
        "fallback: анализируем текст последнего ответа бота (%s символов): %s",
        len(context_text),
        snippet,
    )
    if user_query and user_query.strip():
        uq = user_query.strip()
        logger.info(
            "fallback: текущий запрос пользователя (%s символов): %s",
            len(uq),
            uq[:200].replace("\n", " ") + ("..." if len(uq) > 200 else ""),
        )
    buttons, branch = generate_context_buttons_fallback(context_text, user_query=user_query)
    logger.info("Fallback ветка: %s | ключи (фрагмент ответа): %s", branch, context_text[:120].replace("\n", " "))
    return buttons


@router.message(Command("start"))
async def start_cmd(message: Message) -> None:
    await message.answer(
        "Привет! 👋 Я Кузьма — твой AI-помощник.\n"
        "Пришли текст, PDF или TXT файл, и я помогу с разбором 😊",
        reply_markup=build_start_keyboard(),
    )


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    await message.answer(build_help_text(is_admin=is_admin(uid)))


@router.message(Command("templates"))
async def templates_cmd(message: Message) -> None:
    if not message.from_user:
        return
    await _send_templates_list(message, message.from_user.id)


@router.message(Command("bookmarks"))
async def bookmarks_cmd(message: Message) -> None:
    if not message.from_user:
        return
    await _send_anchors_list(message, message.from_user.id)


@router.message(Command("simulate"))
async def simulate_cmd(message: Message, bot: Bot) -> None:
    """Симулятор сценариев из меню: вопрос в той же строке после /simulate."""
    if not message.from_user:
        return
    topic = (message.text or "").partition(" ")[2].strip()
    if not topic:
        await message.answer(
            "Симулятор сценариев: напиши вопрос в одной строке, например:\n"
            "/simulate Стоит ли брать ипотеку под высокий процент?\n\n"
            "Или без команды: «Что если…», «Какие варианты…» — тоже сработает 🎲"
        )
        return
    await _run_scenario_for_user_message(message, bot, message.from_user.id, topic)


async def _editor_require_private(message: Message) -> bool:
    if not message.from_user:
        return False
    if not is_private_chat(message):
        await message.answer(
            "Редактор контента — только в личке со мной. Напиши мне в ЛС: там черновики и кнопки апрува ✍️😊"
        )
        return False
    return True


async def _require_owner_private(message: Message) -> bool:
    """Настройки поиска: только владелец (ADMIN_ID) в личке."""
    if not await _editor_require_private(message) or not message.from_user:
        return False
    if not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return False
    return True


def _topics_command_tail(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"/topics(?:@\w+)?\s*(.*)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _sources_command_tail(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"/sources(?:@\w+)?\s*(.*)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _searchwindow_command_tail(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"/searchwindow(?:@\w+)?\s*(.*)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _searchmode_command_tail(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"/searchmode(?:@\w+)?\s*(.*)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _automode_command_tail(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"/automode(?:@\w+)?\s*(.*)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _telegram_answer_chunks(text: str, limit: int = 3900) -> list[str]:
    """Дробит длинный текст для Telegram (лимит сообщения 4096)."""
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= limit:
        return [t]
    chunks: list[str] = []
    pos = 0
    n = len(t)
    while pos < n:
        end = min(pos + limit, n)
        if end < n:
            window_start = pos + (end - pos) * 2 // 3
            nl = t.rfind("\n", window_start, end)
            if nl > pos:
                end = nl + 1
        piece = t[pos:end].strip()
        if piece:
            chunks.append(piece)
        pos = end
    return chunks


def _contains_draft_trigger(text: str) -> bool:
    low = (text or "").lower().strip()
    return any(word in low for word in _DRAFT_TRIGGER_WORDS)


def _remember_recent_user_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    txt = (message.text or "").strip()
    if not txt or txt.startswith("/"):
        return
    _recent_user_text_for_draft[message.from_user.id] = (
        txt,
        time.time(),
        message.chat.id,
    )


def _trigger_from_recent_text(user_id: int, chat_id: int) -> tuple[bool, str]:
    row = _recent_user_text_for_draft.get(user_id)
    if not row:
        return False, ""
    txt, ts, cid = row
    if cid != chat_id or (time.time() - ts) > _FILE_DRAFT_CONTEXT_TTL_SEC:
        return False, ""
    if _contains_draft_trigger(txt):
        return True, txt
    return False, ""


def _should_create_draft_from_file_message(message: Message) -> tuple[bool, str]:
    if not message.from_user:
        return False, ""
    uid = message.from_user.id
    if not is_editor_enabled(memory, uid):
        return False, ""
    caption = (message.caption or "").strip()
    if _contains_draft_trigger(caption):
        return True, caption
    trig, src = _trigger_from_recent_text(uid, message.chat.id)
    if trig:
        return True, src
    return False, ""


def _extract_document_text(file_name: str, mime_type: str, file_bytes: bytes) -> tuple[str, str | None]:
    name = (file_name or "").lower()
    mt = (mime_type or "").lower()
    try:
        if name.endswith(".pdf") or mt == "application/pdf":
            return extract_text_from_pdf(file_bytes), None
        if name.endswith(".txt") or mt.startswith("text/plain"):
            return extract_text_from_txt(file_bytes), None
        if name.endswith(".md") or mt in {"text/markdown", "text/x-markdown"}:
            return extract_text_from_txt(file_bytes), None
    except Exception as exc:
        return "", f"Не получилось обработать файл 😥\n{exc}"
    return "", "Поддерживаются только PDF, TXT и MD файлы 🙏"


async def _create_editor_draft_from_text(
    message: Message,
    content_text: str,
    source_label: str,
    *,
    source_url: str | None = None,
) -> bool:
    if not message.from_user:
        return False
    uid = message.from_user.id
    prefs = memory.get_style_preferences(uid)
    topics = (prefs.get(PREF_TOPICS) or "актуальные новости").strip()
    user_block = (
        f"Темы пользователя: {topics}\n"
        f"Заголовок источника: {source_label}\n"
        f"Краткое содержание: {(content_text or '').strip()[:12000]}\n"
        f"URL: {(source_url or 'файл пользователя')}\n"
    )
    voice_overlay = build_voice_examples_overlay(uid, limit=3)
    rules_overlay = build_editorial_rules_overlay(uid)
    try:
        draft_text = await asyncio.to_thread(
            agent.run_raw_completion,
            system=DRAFT_SYSTEM
            + voice_overlay
            + rules_overlay
            + "\nЕсли URL отсутствует, в строке «Источник:» укажи «файл пользователя».\n",
            user=user_block,
            max_tokens=1200,
            temperature=min(0.82, getattr(agent, "_chat_temperature", 0.75) + 0.05),
        )
    except Exception as exc:
        logger.exception("draft_from_file: GPT generation failed: %s", exc)
        await message.answer("Не удалось подготовить черновик из файла 😥 Попробуй ещё раз чуть позже.")
        return False

    body = (draft_text or "").strip()
    if not body:
        await message.answer("Файл прочитал, но черновик не собрался — попробуй другой файл или добавь больше контекста 🙏")
        return False

    if source_url:
        source_line = f"Источник: {source_url}".strip()
        if source_line.lower() not in body.lower():
            body = f"{body.rstrip()}\n\n{source_line}"

    ch = prefs.get(PREF_CHANNEL) or DEFAULT_EDITOR_CHANNEL_ID
    deadline_h = draft_deadline_hours_from_prefs(prefs)
    ok, res = await asyncio.to_thread(
        insert_draft,
        uid,
        ch,
        body,
        source_url,
        deadline_hours=deadline_h,
    )
    if not ok:
        await message.answer(str(res))
        return False

    row = get_draft(uid, int(res))
    if not row:
        await message.answer("Черновик создался, но не читается из базы — попробуй /drafts 🔧")
        return False

    await message.answer(
        draft_dm_text(row),
        reply_markup=build_editor_keyboard(int(res)),
    )
    return True


def _extract_first_url_from_text(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).strip().rstrip(").,!?;:\"'")
    return url or None


def _fetch_article_text_by_url(url: str) -> tuple[str, str | None]:
    if not getattr(agent, "tavily", None):
        return "", "Веб-поиск сейчас недоступен: в этом режиме нужен Tavily (TAVILY_API_KEY) 🌐"
    result = agent._tavily_search(url, max_results=3)
    if not result or not isinstance(result, dict):
        return "", "Не смог открыть ссылку через веб-поиск — попробуй другую ссылку или повтори позже."
    items = result.get("results") or []
    if not isinstance(items, list) or not items:
        return "", "По этой ссылке не удалось получить содержимое статьи."

    selected = None
    url_low = url.lower()
    for it in items:
        if not isinstance(it, dict):
            continue
        item_url = str(it.get("url") or "").strip().lower()
        if item_url and (item_url == url_low or url_low in item_url or item_url in url_low):
            selected = it
            break
    if selected is None:
        selected = items[0] if isinstance(items[0], dict) else None
    if not selected:
        return "", "По ссылке вернулся пустой результат."

    content = (selected.get("raw_content") or "").strip()
    if not content:
        content = (selected.get("content") or "").strip()
    if not content:
        return "", "Страница открылась, но текст извлечь не удалось."
    return content[:12000], None


def _should_create_draft_from_url_text(message: Message) -> tuple[bool, str | None]:
    if not message.from_user or not message.text:
        return False, None
    if not is_editor_enabled(memory, message.from_user.id):
        return False, None
    txt = (message.text or "").strip()
    if not _contains_draft_trigger(txt):
        return False, None
    return True, _extract_first_url_from_text(txt)


@router.message(Command("editor_start"))
async def editor_start_cmd(message: Message) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    memory.update_style_preferences(
        uid,
        {
            PREF_ENABLED: "1",
            PREF_CHANNEL: DEFAULT_EDITOR_CHANNEL_ID,
        },
    )
    pop_pending_edit(uid)
    await message.answer(
        f"Редактор включён 🎉 Цель — канал @kriptogeograph (`{DEFAULT_EDITOR_CHANNEL_ID}`). "
        "Дальше всё просто: /topics — рулим темами, /searchmode — выбираем источник (web/tg/both), "
        "/automode — настраиваем интервал авто-поиска, а /drafts приносит черновик вручную. "
        "С черновиками работаем кнопками: ✅ публикуем, ✏️ правим, ❌ скипаем, 🕐 откладываем. "
        "Без твоего окея в канал ничего не полетит 🎯"
    )


@router.message(Command("editor_stop"))
async def editor_stop_cmd(message: Message) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    memory.update_style_preferences(
        message.from_user.id,
        {PREF_ENABLED: "0", PREF_AUTO_ENABLED: "0"},
    )
    pop_pending_edit(message.from_user.id)
    await message.answer(
        "Поставил редактор на паузу ⏸️ Черновики в базе остаются — снова включить: /editor_start ✍️"
    )


@router.message(Command("editor_prefs"))
async def editor_prefs_cmd(message: Message) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    raw_rest = (message.text or "").partition(" ")[2].strip()
    prefs = memory.get_style_preferences(uid)

    if not raw_rest:
        topics = prefs.get(PREF_TOPICS, "— ещё не задавали")
        sources = prefs.get(PREF_SOURCES, "—")
        sm = get_source_mode(prefs)
        tg_extra = (prefs.get(PREF_TG_CHANNELS) or "").strip() or "—"
        rc = prefs.get(PREF_REJECT_COUNT, "0")
        ac = prefs.get(PREF_APPROVE_COUNT, "0")
        ae = "включён" if is_auto_enabled_pref(prefs) else "выключен"
        ah = auto_interval_hours_from_prefs(prefs)
        dh = draft_deadline_hours_from_prefs(prefs)
        reason = (prefs.get(PREF_AUTO_DISABLED_REASON) or "").strip()
        extra = f"\nАвто-поиск черновиков: {ae}, интервал {format_auto_interval_label(ah)}"
        extra += f"\nСрок жизни черновика: {dh} ч"
        if reason and not is_auto_enabled_pref(prefs):
            extra += f" (остановка: {reason[:120]}{'…' if len(reason) > 120 else ''})"
        await message.answer(
            f"Текущие темы: {topics}\n"
            f"Уточнение к поиску (вторая часть после запятой): {sources}\n"
            f"Режим материалов: {sm} (web / tg / both) — см. источники: в /editor_prefs\n"
            f"Свои TG-каналы (если заданы — при поиске только они): {tg_extra[:200]}{'…' if len(tg_extra) > 200 else ''}\n"
            f"Счётчики: апрувов {ac}, отказов {rc} — на них мягко опираюсь при подборе 📊{extra}\n\n"
            "Только темы: /editor_prefs темы:мемы,юмор или /editor_prefs темы мемы,юмор\n"
            "Только TG: /editor_prefs тгканалы:@a,@b или /editor_prefs тгканалы @a @b\n"
            "Источники: /editor_prefs источники:both (или web, tg)\n"
            "Старый стиль: /editor_prefs биткоин,defi — темы и уточнение одной строкой\n"
            "Сброс отказов/банов по каналам: /editor_reset_rejects\n"
            "Авто: /editor_prefs авто:0.5 (30 минут), /editor_prefs авто:1 (1 час) или /editor_prefs авто:off\n"
            "Дедлайн: /editor_prefs дедлайн:24 (или 48, 72)"
        )
        return

    prefs_before = prefs
    extra_clean, extra_updates = parse_editor_extra_directives(raw_rest)
    if extra_updates:
        memory.update_style_preferences(uid, extra_updates)
        if PREF_TG_CHANNELS in extra_updates:
            logger.info(
                "editor_prefs saved user_id=%s tg_channels=%s",
                uid,
                extra_updates[PREF_TG_CHANNELS][:500],
            )
        elif PREF_TOPICS in extra_updates:
            logger.info(
                "editor_prefs saved user_id=%s topics=%r sources=%r",
                uid,
                (extra_updates.get(PREF_TOPICS) or "")[:200],
                (extra_updates.get(PREF_SOURCES) or "")[:200],
            )
        else:
            logger.info(
                "editor_prefs saved user_id=%s keys=%s",
                uid,
                list(extra_updates.keys()),
            )

    auto_cleaned, auto_updates = parse_auto_directive_from_rest(extra_clean)
    if auto_updates is None and re.search(r"(?is)авто\s*:", extra_clean) and not AUTO_DIRECTIVE_RE.search(extra_clean):
        await message.answer(
            "Не разобрал «авто». Примеры: авто:0.5 — раз в 30 минут, авто:1 — раз в час, авто:off — выключить автодобычу черновиков 🧪"
        )
        return
    deadline_cleaned, deadline_updates = parse_deadline_directive_from_rest(auto_cleaned)
    if (
        deadline_updates is None
        and re.search(r"(?is)дедлайн\s*:", auto_cleaned)
        and "дедлайн:" in auto_cleaned.lower()
    ):
        await message.answer("Не разобрал «дедлайн». Примеры: дедлайн:24, дедлайн:48, дедлайн:72 ⏳")
        return

    rest_topics = (deadline_cleaned if deadline_updates is not None else auto_cleaned).strip()

    if auto_updates:
        was_auto = is_auto_enabled_pref(prefs_before)
        memory.update_style_preferences(uid, auto_updates)
        prefs_mid = memory.get_style_preferences(uid)
        now_auto = is_auto_enabled_pref(prefs_mid)
        if now_auto and not was_auto:
            ts_upd: dict[str, str] = {PREF_LAST_AUTO_FETCH_TS: str(time.time())}
            need_intro = prefs_before.get(PREF_AUTO_INTRO_SENT, "").strip() != "1"
            if need_intro:
                ts_upd[PREF_AUTO_INTRO_SENT] = "1"
            memory.update_style_preferences(uid, ts_upd)
            if need_intro:
                await message.answer(
                    "Авто-поиск включён 🎰 Буду сам приносить черновики сюда с кнопками — как редактор с подносом, "
                    "только поднос цифровой. В канал ничего не вылетит без твоего ✅. Надоело — /editor_prefs авто:off 🙃"
                )
        elif not now_auto and was_auto:
            await message.answer(
                "Авто-поиск выключен — черновики только по твоему /drafts, без самодеятельности 📎✋"
            )
    if deadline_updates:
        memory.update_style_preferences(uid, deadline_updates)

    bits: list[str] = []
    if extra_updates:
        if PREF_SOURCE_MODE in extra_updates:
            bits.append(f"источники: {extra_updates[PREF_SOURCE_MODE]}")
        if PREF_TG_CHANNELS in extra_updates:
            bits.append(
                f"тгканалы: {extra_updates[PREF_TG_CHANNELS][:100]}{'…' if len(extra_updates[PREF_TG_CHANNELS]) > 100 else ''}"
            )
        if PREF_TOPICS in extra_updates:
            td = (extra_updates.get(PREF_TOPICS) or "").strip()
            sd = (extra_updates.get(PREF_SOURCES) or "").strip()
            bits.append(
                f"темы «{td[:120]}{'…' if len(td) > 120 else ''}», "
                f"уточнение «{sd[:120]}{'…' if len(sd) > 120 else ''}»"
            )
    if rest_topics and PREF_TOPICS not in (extra_updates or {}):
        if "," in rest_topics:
            topics_disp, _, tail = rest_topics.partition(",")
            topics_disp = topics_disp.strip()
            sources_disp = tail.strip()
        else:
            topics_disp = rest_topics.strip()
            sources_disp = ""
        memory.update_style_preferences(
            uid,
            {
                PREF_TOPICS: topics_disp[:800],
                PREF_SOURCES: sources_disp[:800],
            },
        )
        bits.append(
            f"темы «{topics_disp[:120]}{'…' if len(topics_disp) > 120 else ''}», "
            f"уточнение «{sources_disp[:120]}{'…' if len(sources_disp) > 120 else ''}»"
        )

    prefs = memory.get_style_preferences(uid)
    if auto_updates:
        if is_auto_enabled_pref(prefs):
            bits.append(f"авто {format_auto_interval_label(auto_interval_hours_from_prefs(prefs))}")
        else:
            bits.append("авто выкл")
    if deadline_updates:
        bits.append(f"дедлайн {draft_deadline_hours_from_prefs(prefs)} ч")
    if not bits:
        await message.answer("Нечего менять — глянь /editor_prefs без хвоста для сводки 📋")
        return
    await message.answer("Записал: " + "; ".join(bits) + " — жми /drafts или жди авто 🚀")


@router.message(Command("topics"))
async def topics_cmd(message: Message) -> None:
    """Темы поиска редактора: просмотр и правки только для ADMIN_ID."""
    if not await _require_owner_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    tail = _topics_command_tail(message.text or "")
    prefs = memory.get_style_preferences(uid)
    old_val = prefs.get(PREF_TOPICS, "") or ""

    if not tail:
        await message.answer(format_topics_settings_message(old_val))
        return

    parts = tail.split(maxsplit=1)
    sub = parts[0].lower()
    body = (parts[1] if len(parts) > 1 else "").strip()

    if sub == "clear":
        new_val = ""
        memory.update_style_preferences(uid, {PREF_TOPICS: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_TOPICS,
            old_val,
            new_val,
        )
        await message.answer(
            "✅ Темы очищены.\n\n" + format_topics_settings_message(new_val)
        )
        return

    if sub == "add":
        tokens = split_topic_command_tokens(body)
        if not tokens:
            await message.answer(
                "❌ Укажи темы: например `/topics add игры, аниме`",
            )
            return
        new_val, added = merge_topics_into_pref(old_val, tokens)
        if not added:
            await message.answer(
                "ℹ️ Все указанные темы уже в списке.\n\n"
                + format_topics_settings_message(old_val),
            )
            return
        memory.update_style_preferences(uid, {PREF_TOPICS: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_TOPICS,
            old_val,
            new_val,
        )
        n_total = len(topics_list_from_pref(new_val))
        await message.answer(
            f"✅ Добавлены темы: {', '.join(added)} (всего {n_total}).\n\n"
            + format_topics_settings_message(new_val),
        )
        return

    if sub == "remove":
        tokens = split_topic_command_tokens(body)
        if not tokens:
            await message.answer(
                "❌ Укажи тему: например `/topics remove юмор`",
            )
            return
        new_val, removed = remove_topics_from_pref(old_val, tokens)
        if not removed:
            await message.answer(
                "ℹ️ Таких тем в списке не было.\n\n"
                + format_topics_settings_message(old_val),
            )
            return
        memory.update_style_preferences(uid, {PREF_TOPICS: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_TOPICS,
            old_val,
            new_val,
        )
        await message.answer(
            "✅ Удалены темы: "
            + ", ".join(removed)
            + ".\n\n"
            + format_topics_settings_message(new_val),
        )
        return

    await message.answer(
        "Не разобрал команду. Варианты:\n"
        "• `/topics` — текущий список\n"
        "• `/topics add тема1, тема2`\n"
        "• `/topics remove тема`\n"
        "• `/topics clear`",
    )


@router.message(Command("sources"))
async def sources_cmd(message: Message) -> None:
    """Уточнение к поиску (PREF_SOURCES): только владелец в личке."""
    if not await _require_owner_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    tail = _sources_command_tail(message.text or "")
    prefs = memory.get_style_preferences(uid)
    old_val = prefs.get(PREF_SOURCES, "") or ""

    if not tail:
        await message.answer(format_sources_settings_message(old_val))
        return

    parts = tail.split(maxsplit=1)
    sub = parts[0].lower()
    body = (parts[1] if len(parts) > 1 else "").strip()

    if sub == "clear":
        new_val = ""
        memory.update_style_preferences(uid, {PREF_SOURCES: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_SOURCES,
            old_val,
            new_val,
        )
        await message.answer(
            "✅ Уточнение к поиску очищено.\n\n" + format_sources_settings_message(new_val)
        )
        return

    if sub == "add":
        tokens = split_source_command_tokens(body)
        if not tokens:
            await message.answer(
                "❌ Укажи фрагменты: например `/sources add defi, мемы`",
            )
            return
        new_val, added = merge_sources_into_pref(old_val, tokens)
        if not added:
            await message.answer(
                "ℹ️ Все указанные фрагменты уже в списке.\n\n"
                + format_sources_settings_message(old_val),
            )
            return
        memory.update_style_preferences(uid, {PREF_SOURCES: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_SOURCES,
            old_val,
            new_val,
        )
        n_total = len(sources_list_from_pref(new_val))
        await message.answer(
            f"✅ Добавлено к уточнению: {', '.join(added)} (всего {n_total}).\n\n"
            + format_sources_settings_message(new_val),
        )
        return

    if sub == "remove":
        tokens = split_source_command_tokens(body)
        if not tokens:
            await message.answer(
                "❌ Укажи фрагмент: например `/sources remove defi`",
            )
            return
        new_val, removed = remove_sources_from_pref(old_val, tokens)
        if not removed:
            await message.answer(
                "ℹ️ Таких фрагментов в списке не было.\n\n"
                + format_sources_settings_message(old_val),
            )
            return
        memory.update_style_preferences(uid, {PREF_SOURCES: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_SOURCES,
            old_val,
            new_val,
        )
        await message.answer(
            "✅ Удалены фрагменты: "
            + ", ".join(removed)
            + ".\n\n"
            + format_sources_settings_message(new_val),
        )
        return

    await message.answer(
        "Не разобрал команду. Варианты:\n"
        "• `/sources` — текущее уточнение\n"
        "• `/sources add фраза1, фраза2`\n"
        "• `/sources remove фраза`\n"
        "• `/sources clear`",
    )


@router.message(Command("searchwindow"))
async def searchwindow_cmd(message: Message) -> None:
    """Окно дней для Tavily (веб-подбор): только владелец в личке."""
    if not await _require_owner_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    tail = _searchwindow_command_tail(message.text or "")
    prefs = memory.get_style_preferences(uid)
    old_val = prefs.get(PREF_SEARCH_WINDOW_DAYS, "") or ""

    if not tail:
        await message.answer(format_search_window_settings_message(prefs))
        return

    if tail.lower() == "clear":
        new_val = ""
        memory.update_style_preferences(uid, {PREF_SEARCH_WINDOW_DAYS: new_val})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            PREF_SEARCH_WINDOW_DAYS,
            old_val,
            new_val,
        )
        prefs2 = memory.get_style_preferences(uid)
        await message.answer(
            "✅ Окно поиска сброшено на значение по умолчанию.\n\n"
            + format_search_window_settings_message(prefs2),
        )
        return

    try:
        n = int(tail.strip())
    except ValueError:
        await message.answer(
            f"❌ Укажи целое число дней от {MIN_SEARCH_WINDOW_DAYS} до {MAX_SEARCH_WINDOW_DAYS}, "
            "например `/searchwindow 7`, или `/searchwindow clear` для сброса.",
        )
        return

    if n < MIN_SEARCH_WINDOW_DAYS or n > MAX_SEARCH_WINDOW_DAYS:
        await message.answer(
            f"❌ Допустимо {MIN_SEARCH_WINDOW_DAYS}–{MAX_SEARCH_WINDOW_DAYS} дней.",
        )
        return

    new_val = str(n)
    memory.update_style_preferences(uid, {PREF_SEARCH_WINDOW_DAYS: new_val})
    logger.info(
        "search_setting_change user_id=%s key=%s old=%r new=%r",
        uid,
        PREF_SEARCH_WINDOW_DAYS,
        old_val,
        new_val,
    )
    prefs2 = memory.get_style_preferences(uid)
    await message.answer(
        f"✅ Записал окно поиска: {n} дн.\n\n"
        + format_search_window_settings_message(prefs2),
    )


@router.message(Command("searchmode"))
async def searchmode_cmd(message: Message) -> None:
    """Режим источников подбора (web / tg / both): только владелец в личке."""
    if not await _require_owner_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    tail = _searchmode_command_tail(message.text or "")
    prefs = memory.get_style_preferences(uid)
    old_mode = get_source_mode(prefs)

    if not tail:
        await message.answer(
            "Режим источников: "
            f"{old_mode} — "
            "web (только веб), tg (только Telegram), both (веб и TG) 🌐"
        )
        return

    mode = tail.strip().lower()
    if mode not in ("web", "tg", "both"):
        await message.answer(
            "❌ Укажи режим: `/searchmode web`, `/searchmode tg` или `/searchmode both`.",
        )
        return

    if mode == old_mode:
        await message.answer(f"Режим источников уже {mode} — без изменений ✓")
        return

    memory.update_style_preferences(uid, {PREF_SOURCE_MODE: mode})
    logger.info(
        "search_setting_change user_id=%s key=%s old=%r new=%r",
        uid,
        "source_mode",
        old_mode,
        mode,
    )
    await message.answer(
        f"✅ Режим источников: {mode} (было {old_mode}). "
        "Сводка: /searchsettings или /editor_info 📋"
    )


@router.message(Command("automode"))
async def automode_cmd(message: Message) -> None:
    """Интервал и вкл/выкл авто-поиска черновиков: только владелец в личке."""
    if not await _require_owner_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    tail = _automode_command_tail(message.text or "")
    prefs = memory.get_style_preferences(uid)

    if not tail:
        ah = auto_interval_hours_from_prefs(prefs)
        if is_auto_enabled_pref(prefs):
            st = f"включён, интервал {format_auto_interval_label(ah)} ({_fmt_hours_value(ah)} ч)"
        else:
            st = f"выключен (последний интервал {format_auto_interval_label(ah)} — {_fmt_hours_value(ah)} ч)"
        await message.answer(f"Авто-поиск: {st}")
        return

    low = tail.strip().lower()

    if low in ("off", "выкл", "0"):
        if not is_auto_enabled_pref(prefs):
            await message.answer("Авто-поиск уже выключен ✓")
            return
        old_h = auto_interval_hours_from_prefs(prefs)
        memory.update_style_preferences(uid, {PREF_AUTO_ENABLED: "0"})
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            "auto_interval",
            _fmt_hours_value(old_h),
            "off",
        )
        await message.answer("✅ Авто-поиск выключен (`/automode on` или число часов — снова включить)")
        return

    if low in ("on", "вкл"):
        h = auto_interval_hours_from_prefs(prefs)
        if is_auto_enabled_pref(prefs):
            await message.answer(
                f"Авто-поиск уже включён: {format_auto_interval_label(h)} ({_fmt_hours_value(h)} ч) ✓"
            )
            return
        memory.update_style_preferences(
            uid,
            {
                PREF_AUTO_ENABLED: "1",
                PREF_AUTO_INTERVAL_HOURS: _fmt_hours_value(h),
                PREF_AUTO_DISABLED_REASON: "",
            },
        )
        logger.info(
            "search_setting_change user_id=%s key=%s old=%r new=%r",
            uid,
            "auto_interval",
            "off",
            _fmt_hours_value(h),
        )
        await message.answer(
            f"✅ Авто-поиск включён: {format_auto_interval_label(h)} ({_fmt_hours_value(h)} ч)"
        )
        return

    try:
        new_h = float(tail.replace(",", ".").strip())
    except ValueError:
        await message.answer(
            "❌ Не разобрал. Примеры: `/automode 0.5`, `/automode 1`, `/automode off`, `/automode on`."
        )
        return

    new_h = max(MIN_AUTO_INTERVAL_HOURS, min(MAX_AUTO_INTERVAL_HOURS, new_h))
    old_h = auto_interval_hours_from_prefs(prefs)
    hs = _fmt_hours_value(new_h)
    memory.update_style_preferences(
        uid,
        {
            PREF_AUTO_ENABLED: "1",
            PREF_AUTO_INTERVAL_HOURS: hs,
            PREF_AUTO_DISABLED_REASON: "",
        },
    )
    logger.info(
        "search_setting_change user_id=%s key=%s old=%r new=%r",
        uid,
        "auto_interval",
        _fmt_hours_value(old_h),
        hs,
    )
    await message.answer(
        f"✅ Авто-поиск включён: {format_auto_interval_label(new_h)} ({hs} ч)"
    )


@router.message(Command("searchsettings"))
async def searchsettings_cmd(message: Message) -> None:
    """Сводка настроек поиска: только владелец в личке (без записи в prefs)."""
    if not await _require_owner_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    prefs = memory.get_style_preferences(uid)
    await message.answer(format_search_settings_message(prefs))


@router.message(Command("editor_reset_rejects"))
async def editor_reset_rejects_cmd(message: Message) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    if not is_editor_enabled(memory, uid):
        await message.answer("Сначала /editor_start — сброс относится к редактору ✍️")
        return
    nh, nk = reset_editor_reject_state(memory, uid)
    await message.answer(
        f"Сбросил отказы: записей в «чёрной тетради» было {nh}, ключей жёсткого бана (сайт или tg:канал) — {nk}. "
        f"Подбор снова с чистого листа; при ❌ отказах счётчики снова накапливаются 📎"
    )


@router.message(Command("editor_info"))
async def editor_info_cmd(message: Message) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    prefs = memory.get_style_preferences(uid)
    await message.answer(format_editor_info_text(prefs, user_id=uid))


@router.message(Command("editor_rules"))
async def editor_rules_cmd(message: Message) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    rules = get_editorial_rules(uid)
    if not rules or not str(rules).strip():
        await message.answer(
            "Пока нет сохранённых правил редактора — они складываются из твоих ответов на мой вопрос после ✅✏️❌. "
            "Каждые пять таких ответов я пересобираю список и подмешиваю его в новые черновики. "
            "Загляни сюда снова после пары решений 📋"
        )
        return
    baseline = get_editorial_feedbacks_baseline(uid)
    header = (
        "📌 Устойчивые правила редактора — подмешиваются в генерацию черновиков "
        f"(срез после {baseline} учтённых ответов на вопросы после решений):\n\n"
    )
    full = header + str(rules).strip()
    for part in _telegram_answer_chunks(full, limit=3900):
        await message.answer(part)


@router.message(Command("drafts"))
async def drafts_cmd(message: Message, bot: Bot) -> None:
    if not await _editor_require_private(message) or not message.from_user:
        return
    uid = message.from_user.id
    if not is_editor_enabled(memory, uid):
        await message.answer("Сначала /editor_start — без этого я не знаю, что тебе подкладывать в ленту ✍️")
        return
    prefs_dm = memory.get_style_preferences(uid)
    if get_source_mode(prefs_dm) == "web" and not agent.tavily:
        await message.answer(
            "Tavily не настроен — в режиме источники:web без веб-поиска не обойтись. Добавь TAVILY_API_KEY в .env "
            "или поставь /editor_prefs источники:tg / both (both без ключа попробует хотя бы TG) 🌐"
        )
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    tail = (parts[1] or "").strip().lower() if len(parts) > 1 else ""
    want_more = tail in ("ещё", "еще", "new", "more", "+")

    pending = count_drafts(uid, "draft")
    logger.info(
        "drafts_cmd: user_id=%s pending_drafts=%s limit=%s want_more=%s",
        uid,
        pending,
        MAX_PENDING_UNAPPROVED_DRAFTS,
        want_more,
    )

    if want_more:
        if pending >= MAX_PENDING_UNAPPROVED_DRAFTS:
            await message.answer(
                f"Уже {MAX_PENDING_UNAPPROVED_DRAFTS} неразобранных черновиков в очереди — новый не создаю. "
                "Разгреби ✅/✏️/❌ по текущим, потом снова /drafts ещё 📎"
            )
            return
        await safe_send_chat_action(bot, message.chat.id, "typing")
        ok, res, _dm = await asyncio.to_thread(create_draft_from_search, agent, memory, uid)
        if not ok:
            await message.answer(str(res))
            return
        row = get_draft(uid, int(res))
        if not row:
            await message.answer("Черновик создался, но я его не вижу — глюк матрицы 🫠")
            return
        await message.answer(
            draft_dm_text(row),
            reply_markup=build_editor_keyboard(int(res)),
        )
        return

    if pending > 0:
        oldest = get_oldest_draft(uid, "draft")
        if oldest:
            extra = (
                f"\n\nВ очереди ещё {pending - 1} черновик(ов). Свежую подборку в хвост очереди — "
                f"/drafts ещё (лимит {MAX_PENDING_UNAPPROVED_DRAFTS} шт.)."
                if pending > 1
                else f"\n\nЕщё одну подборку в очередь — /drafts ещё (до {MAX_PENDING_UNAPPROVED_DRAFTS} шт.)."
            )
            await message.answer(
                draft_dm_text(oldest) + extra,
                reply_markup=build_editor_keyboard(int(oldest["id"])),
            )
            return

    await safe_send_chat_action(bot, message.chat.id, "typing")
    ok, res, _dm = await asyncio.to_thread(create_draft_from_search, agent, memory, uid)
    if not ok:
        await message.answer(str(res))
        return
    row = get_draft(uid, int(res))
    if not row:
        await message.answer("Черновик создался, но я его не вижу — глюк матрицы 🫠")
        return
    await message.answer(
        draft_dm_text(row),
        reply_markup=build_editor_keyboard(int(res)),
    )


async def _handle_editor_revision_message(message: Message, bot: Bot, draft_id: int) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    text = (message.text or "").strip()
    row = get_draft(uid, draft_id)
    if not row or str(row.get("status")) != "draft":
        pop_pending_edit(uid)
        await message.answer("Этот черновик уже не в статусе «черновик» — начни с /drafts ✍️")
        return
    old = str(row.get("content") or "")
    if not update_draft_content(uid, draft_id, text):
        pop_pending_edit(uid)
        await message.answer("Не удалось сохранить правки — глянь /drafts 📎")
        return
    maybe_note_shorter_edit(memory, uid, old, text)
    pop_pending_edit(uid)
    row2 = get_draft(uid, draft_id)
    if not row2:
        await message.answer("Странно, черновик пропал — попробуй /drafts 🫠")
        return
    await message.answer(
        draft_dm_text(row2),
        reply_markup=build_editor_keyboard(draft_id),
    )
    _schedule_ask_why(
        bot,
        message.chat.id,
        uid,
        draft_id,
        "edited",
        str(row2.get("content") or ""),
    )


@router.message(Command("clear"))
async def clear_cmd(message: Message) -> None:
    if message.from_user:
        memory.clear_user_memory(message.from_user.id)
        agent.clear_clarification_pending(message.from_user.id)
    await message.answer("Память диалога очищена 🧹")


@router.message(Command("reset_style"))
async def reset_style_cmd(message: Message) -> None:
    if message.from_user:
        memory.clear_style_preferences(message.from_user.id)
    await message.answer(
        "Настройки стиля ответа сброшены: снова обычный баланс длины и тона ✨"
    )


@router.message(Command("stats"))
async def stats_cmd(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    stats = get_stats()
    top = get_top_users(5)
    daily = get_daily_breakdown(7)
    lines = [
        "📊 Статистика бота",
        "",
        f"👥 Всего пользователей: {stats['total_users']}",
        f"💬 Всего сообщений: {stats['total_messages']}",
        f"📅 Сегодня (сообщений): {stats['today_messages']}",
        f"👤 Сегодня (уникальных): {stats['today_users']}",
        "",
        "📈 Активность по дням (последние 7):",
    ]
    if daily:
        for row in reversed(daily):
            lines.append(
                f"  • {row['date']}: 💬 {row['messages']}, 👤 {row['unique_users']}"
            )
    else:
        lines.append("  (пока нет данных)")
    lines.append("")
    lines.append("🏆 Топ по сообщениям:")
    if top:
        for i, u in enumerate(top, start=1):
            uname = f"@{u['username']}" if u.get("username") else f"id {u['user_id']}"
            lines.append(f"  {i}. {uname} — {u['message_count']} сообщ.")
    else:
        lines.append("  (пока нет данных)")
    await message.answer("\n".join(lines))


ADMIN_HELP = (
    "🔧 Панель администратора\n\n"
    "Доступные команды:\n"
    "• /broadcast текст — рассылка всем пользователям из статистики "
    "(или ответьте /broadcast на сообщение)\n"
    "• /ban <user_id> — заблокировать пользователя\n"
    "• /unban <user_id> — разблокировать\n"
    "• /users — список пользователей (до 50)\n"
    "• /selftest — функциональная самодиагностика (шаблоны, якоря, консьерж, БД, ключи, JSON-кнопки)\n"
    "• /fulldiag — полная: техника + функционал 🤖✨\n"
    "• /admin — это меню"
)


@router.message(Command("admin"))
async def admin_cmd(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    await message.answer(ADMIN_HELP)


@router.message(Command("broadcast"))
async def broadcast_cmd(message: Message, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text.strip()
    if not text:
        await message.answer(
            "Укажите текст после команды или ответьте /broadcast на сообщение, которое нужно разослать."
        )
        return
    n = await broadcast_message(bot, text)
    await message.answer(f"✅ Рассылка завершена. Доставлено сообщений: {n}")


@router.message(Command("ban"))
async def ban_cmd(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /ban <user_id>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.answer("user_id должен быть целым числом.")
        return
    if config.admin_id is not None and target == config.admin_id:
        await message.answer("Нельзя заблокировать администратора.")
        return
    if ban_user(target):
        await message.answer(f"🚫 Пользователь {target} заблокирован.")
    else:
        await message.answer("Не удалось заблокировать (см. логи).")


@router.message(Command("unban"))
async def unban_cmd(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unban <user_id>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.answer("user_id должен быть целым числом.")
        return
    if unban_user(target):
        await message.answer(f"✅ Пользователь {target} разблокирован.")
    else:
        await message.answer(f"Пользователь {target} не был в списке блокировок.")


@router.message(Command("users"))
async def users_cmd(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    rows = get_users_preview(50)
    if not rows:
        await message.answer("Пока нет пользователей в статистике.")
        return
    lines = ["👥 Пользователи (до 50, по активности):", ""]
    for r in rows:
        uname = f"@{r['username']}" if r.get("username") else "—"
        ban_mark = " 🚫" if r.get("banned") else ""
        lines.append(f"• id {r['user_id']} {uname} — {r['message_count']} сообщ.{ban_mark}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await message.answer(text)


async def _run_scenario_for_user_message(
    message: Message,
    bot: Bot,
    uid: int,
    topic: str,
) -> None:
    """Генерация трёх веток сценария и отправка вводного сообщения с кнопками."""
    await safe_send_chat_action(bot, message.chat.id, "typing")
    try:
        o_blk, r_blk, p_blk, _ = await asyncio.to_thread(
            generate_scenarios, agent, topic, {}
        )
    except Exception as exc:
        logger.exception("scenario: ошибка генерации: %s", exc)
        await message.answer(
            "Симулятор споткнулся об нейросеть — попробуй ещё раз чуть позже или переформулируй вопрос 🎲😅"
        )
        return
    intro = format_scenario_intro(topic, o_blk, r_blk, p_blk)
    sid = put_session(uid, topic, o_blk, r_blk, p_blk, intro)
    kb = build_scenario_choice_keyboard(sid)
    memory.save_user_memory(uid, topic, intro)
    await message.answer(intro, reply_markup=kb)


@router.message(F.text)
async def text_handler(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.text:
        return
    log_user_message(message.from_user.id, message.from_user.username)
    _remember_recent_user_text(message)
    uid = message.from_user.id
    text = message.text

    if is_private_chat(message) and not text.strip().startswith("/"):
        pe = get_pending_edit(uid)
        if pe is not None:
            await _handle_editor_revision_message(message, bot, pe)
            return
        pf = get_pending_feedback(uid)
        if pf is not None:
            did = pf.get("draft_id")
            draft_id = int(did) if did is not None else None
            rid = save_draft_feedback(
                uid,
                draft_id,
                str(pf.get("action") or ""),
                str(pf.get("draft_preview") or ""),
                text.strip(),
                category=str(pf.get("category") or ""),
                quality_score=(
                    int(pf["quality_score"]) if pf.get("quality_score") is not None else None
                ),
            )
            pop_pending_feedback(uid)
            if rid is not None:
                asyncio.create_task(
                    asyncio.to_thread(maybe_distill_editorial_rules_sync, agent, uid)
                )
                await message.answer(
                    "Принял твой комментарий к решению по черновику — учту в подборе и тоне следующих постов 📝✅"
                )
            else:
                await message.answer(
                    "Не вышло сохранить комментарий — глюк на линии. Можешь написать ещё раз одним сообщением или продолжить с /drafts 📎"
                )
            return

    wants_url_draft, found_url = _should_create_draft_from_url_text(message)
    if wants_url_draft and found_url:
        await message.answer("Читаю статью, готовлю черновик... ⏳")
        article_text, err = await asyncio.to_thread(_fetch_article_text_by_url, found_url)
        if err:
            await message.answer(err)
            return
        await _create_editor_draft_from_text(
            message,
            article_text,
            source_label=f"статья по ссылке {found_url}",
            source_url=found_url,
        )
        return
    if wants_url_draft and not found_url:
        await message.answer("Вижу триггер для черновика, но не нашёл ссылку в сообщении. Добавь URL вида https://... 🔗")
        return

    am, areason, atitle = classify_anchor_command(text)
    if am:
        if areason == "delete_empty":
            await message.answer(
                "Напиши так: «удали якорь Название» — и я уберу нужную закладку 🗑️🔖"
            )
            return
        if areason == "delete":
            ok_ad, removed_a = delete_anchor_by_title(uid, atitle or "")
            if ok_ad and removed_a:
                await message.answer(
                    f"Якорь «{removed_a}» снят — как магнит с холодильника, только без скрипа 😌 "
                    f"Загляни в «покажи якоря», если хочешь проверить список 📋"
                )
            else:
                await message.answer(
                    "Такого якоря не нашёл — глянь «покажи якоря» или поправь название 🔎"
                )
            return
        if areason == "list":
            await _send_anchors_list(message, uid)
            return
        if areason.startswith("create"):
            logger.info(
                "anchor_save: MATCH user_id=%s reason=%s title_hint=%r",
                uid,
                areason,
                atitle,
            )
            snippet, msg_ref, draft = build_anchor_snippet_and_ref(uid)
            if not snippet or msg_ref is None:
                await message.answer(
                    "Сейчас нечего метить: в истории ещё нет нашего обмена репликами. "
                    "Напиши вопрос — я отвечу — и тогда скажи «запомни этот момент» 🎯😊"
                )
                return
            hint = (atitle or "").strip()
            base_title = hint if hint else auto_title_anchor(snippet, draft)
            ok_ins, info = insert_anchor(
                uid, base_title, snippet, msg_ref, config.max_user_anchors
            )
            if ok_ins:
                await message.answer(
                    f"Готово 🔖 Якорь «{info}» поставил — не потеряем нить. "
                    f"«Покажи якоря» — и вспомним этот кусок беседы снова ✨"
                )
            else:
                await message.answer(info)
            return

    del_title = parse_delete_template_title(text)
    if del_title is not None:
        if not del_title.strip():
            await message.answer(
                "Напиши так: «удали шаблон Название» — и я уберу нужную запись 🗑️"
            )
            return
        ok_del, removed = delete_template_by_title(uid, del_title)
        if ok_del and removed:
            await message.answer(
                f"Удалил шаблон «{removed}» — как снег на голову, только полезнее ❄️ "
                f"Место в коллекции освободилось!"
            )
        else:
            await message.answer(
                "Такого шаблона не нашёл — проверь название или открой список: «покажи шаблоны» 🔎"
            )
        return

    if looks_like_list_templates_command(text):
        await _send_templates_list(message, uid)
        return

    logger.debug(
        "template_save: проверка user_id=%s len=%s text=%r",
        uid,
        len(text),
        text[:500],
    )
    matched_save, save_reason = classify_save_template_command(text)
    if matched_save:
        logger.info(
            "template_save: MATCH user_id=%s reason=%s",
            uid,
            save_reason,
        )
    else:
        logger.debug(
            "template_save: NO_MATCH user_id=%s reason=%s",
            uid,
            save_reason,
        )
        if "запомни" in (text or "").lower():
            logger.info(
                "template_save: NO_MATCH user_id=%s reason=%s (запомни → якоря, не шаблон)",
                uid,
                save_reason,
            )

    if matched_save:
        last = get_last_assistant_reply(memory, uid)
        wants_own = user_explicitly_wants_own_message_saved(text)
        if wants_own and not last:
            await message.answer(
                "Я сохраняю свои ответы, а не твои сообщения 😊 "
                "Сначала задай вопрос → я отвечу → потом скажи «сохрани это»!"
            )
            return
        if not last:
            await message.answer(
                "Сейчас нечего класть в шаблоны: в истории ещё нет моего ответа, только твоя реплика. "
                "Задай вопрос — я отвечу — и тогда нажми «сохрани это» (или «сохрани его») 🎯"
            )
            return
        explicit = extract_save_title(text)
        base_title = explicit or auto_title_from_content(last or "")
        ok_ins, info = insert_template(
            uid, base_title, last or "", config.max_user_templates
        )
        if ok_ins:
            await message.answer(
                f"Сохранено 📌 Шаблон «{info}» добавлен в твою коллекцию! "
                f"Потом скажи «покажи шаблоны» — покажу всё кнопками 😊"
            )
        else:
            await message.answer(info)
        return

    is_scen, scen_topic, scen_params = classify_scenario_request(text)
    if is_scen:
        logger.info(
            "scenario: MATCH user_id=%s topic=%r",
            uid,
            scen_topic[:200] if scen_topic else "",
        )
        await safe_send_chat_action(bot, message.chat.id, "typing")
        try:
            o_blk, r_blk, p_blk, _ = await asyncio.to_thread(
                generate_scenarios, agent, scen_topic, scen_params
            )
        except Exception as exc:
            logger.exception("scenario: ошибка генерации: %s", exc)
            await message.answer(
                "Симулятор споткнулся об нейросеть — попробуй ещё раз чуть позже или переформулируй вопрос 🎲😅"
            )
            return
        intro = format_scenario_intro(scen_topic, o_blk, r_blk, p_blk)
        sid = put_session(uid, scen_topic, o_blk, r_blk, p_blk, intro)
        kb = build_scenario_choice_keyboard(sid)
        memory.save_user_memory(uid, text, intro)
        await message.answer(intro, reply_markup=kb)
        return

    await safe_send_chat_action(bot, message.chat.id, "typing")
    response = agent.process_message_with_agent(
        user_id=message.from_user.id, user_text=message.text
    )
    buttons = resolve_proactive_buttons(
        message.from_user.id,
        response.answer,
        response.buttons,
        getattr(response, "is_generic", False),
        user_query=message.text,
    )
    await send_ai_reply(message, response.answer, buttons)


@router.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.document:
        return

    log_user_message(message.from_user.id, message.from_user.username)

    document = message.document
    mime_type = (document.mime_type or "").lower()
    wants_draft, trigger_src = _should_create_draft_from_file_message(message)
    if mime_type in {"image/jpeg", "image/png", "image/gif"}:
        if wants_draft:
            await message.answer("Читаю файл, готовлю черновик... ⏳")
            cap = (message.caption or "").strip()
            question = cap if cap else "Опиши содержимое изображения для черновика поста."
            analysis = await _analyze_image_file(
                bot=bot,
                file_id=document.file_id,
                question=question,
                default_suffix=".jpg",
                user_id=message.from_user.id,
            )
            if not analysis:
                await message.answer("Не удалось прочитать изображение для черновика 😥")
                return
            await _create_editor_draft_from_text(
                message,
                analysis,
                source_label=f"изображение (триггер: {trigger_src[:80]})",
            )
            return
        cap = (message.caption or "").strip()
        question = cap if cap else "Что на этом изображении? Опиши подробно."
        memory_user_text = (
            f"[Изображение] {cap}" if cap else "[Изображение] Без подписи — разбор содержимого."
        )
        await _handle_image_message(
            message=message,
            bot=bot,
            file_id=document.file_id,
            question=question,
            memory_user_text=memory_user_text,
            default_suffix=".jpg",
        )
        return

    if wants_draft:
        await message.answer("Читаю файл, готовлю черновик... ⏳")
        filename = document.file_name or ""
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        extracted, err = _extract_document_text(filename, mime_type, buffer.getvalue())
        if err:
            await message.answer(err)
            return
        if not extracted.strip():
            await message.answer("В файле не найден текст для черновика 🤷")
            return
        await _create_editor_draft_from_text(
            message,
            extracted,
            source_label=f"файл {filename or 'без названия'} (триггер: {trigger_src[:80]})",
        )
        return

    await safe_send_chat_action(bot, message.chat.id, "typing")
    filename = (document.file_name or "").lower()
    buffer = BytesIO()
    await bot.download(document, destination=buffer)
    file_bytes = buffer.getvalue()

    extracted, err = _extract_document_text(filename, mime_type, file_bytes)
    if err:
        await message.answer(err)
        return

    if not extracted:
        await message.answer("В файле не найден текст 🤷")
        return

    prompt = (
        "Пользователь прислал файл. Ниже извлеченный текст.\n"
        "Сделай полезный ответ по содержанию:\n\n"
        f"{extracted[:12000]}"
    )
    response = agent.process_message_with_agent(
        user_id=message.from_user.id,
        user_text=prompt,
        allow_clarification=False,
    )
    buttons = resolve_proactive_buttons(
        message.from_user.id,
        response.answer,
        response.buttons,
        getattr(response, "is_generic", False),
        user_query=None,
    )
    await send_ai_reply(message, response.answer, buttons)


@router.message(F.photo)
async def photo_handler(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.photo:
        return
    log_user_message(message.from_user.id, message.from_user.username)
    wants_draft, trigger_src = _should_create_draft_from_file_message(message)
    best_photo = message.photo[-1]
    cap = (message.caption or "").strip()
    if wants_draft:
        await message.answer("Читаю файл, готовлю черновик... ⏳")
        question = cap if cap else "Опиши содержимое изображения для черновика поста."
        analysis = await _analyze_image_file(
            bot=bot,
            file_id=best_photo.file_id,
            question=question,
            default_suffix=".jpg",
            user_id=message.from_user.id,
        )
        if not analysis:
            await message.answer("Не удалось прочитать фото для черновика 😥")
            return
        await _create_editor_draft_from_text(
            message,
            analysis,
            source_label=f"фото (триггер: {trigger_src[:80]})",
        )
        return
    question = cap if cap else "Что на этом изображении? Опиши подробно."
    memory_user_text = (
        f"[Изображение] {cap}" if cap else "[Изображение] Без подписи — разбор содержимого."
    )
    await safe_send_chat_action(bot, message.chat.id, "typing")
    await _handle_image_message(
        message=message,
        bot=bot,
        file_id=best_photo.file_id,
        question=question,
        memory_user_text=memory_user_text,
        default_suffix=".jpg",
    )


async def _handle_image_message(
    message: Message,
    bot: Bot,
    file_id: str,
    question: str,
    memory_user_text: str,
    default_suffix: str,
) -> None:
    analysis = await _analyze_image_file(
        bot=bot,
        file_id=file_id,
        question=question,
        default_suffix=default_suffix,
        user_id=message.from_user.id if message.from_user else 0,
    )
    if not analysis:
        await message.answer("Не удалось проанализировать изображение 😥")
        return
    fail_text = "Не удалось проанализировать изображение"
    if message.from_user and analysis.strip() and analysis.strip() != fail_text:
        memory.save_user_memory(
            message.from_user.id, memory_user_text, analysis.strip()
        )
        logger.info(
            "История: сохранён ответ по изображению для user_id=%s",
            message.from_user.id,
        )
    buttons = resolve_proactive_buttons(
        message.from_user.id if message.from_user else 0,
        analysis,
        None,
        True,
        user_query=question,
    )
    await send_ai_reply(message, analysis, buttons)


async def _analyze_image_file(
    *,
    bot: Bot,
    file_id: str,
    question: str,
    default_suffix: str,
    user_id: int,
) -> str:
    temp_file_path = ""
    try:
        file_info = await bot.get_file(file_id)
        guessed_mime, _ = mimetypes.guess_type(file_info.file_path or "")
        suffix = mimetypes.guess_extension(guessed_mime or "") or default_suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file_path = temp_file.name

        await bot.download_file(file_info.file_path, destination=temp_file_path)
        with open(temp_file_path, "rb") as image_file:
            raw = image_file.read()
        mime_type = guessed_mime or "image/jpeg"
        image_data_url = f"data:{mime_type};base64,{base64.b64encode(raw).decode('utf-8')}"
        img_seq = memory.count_user_image_messages(user_id) + 1
        dialogue_ctx = (
            memory.build_vision_history_context(user_id)
            if img_seq > 1
            else None
        )
        analysis = agent.analyze_image(
            image_data_url,
            question,
            image_sequence=img_seq,
            dialogue_context=dialogue_ctx,
        )
        return (analysis or "").strip()
    except Exception as exc:
        logger.exception("Ошибка обработки изображения: %s", exc)
        return ""
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError:
                pass


@router.callback_query(F.data == CALLBACK_START_HELP)
async def show_help_callback(callback: CallbackQuery) -> None:
    if callback.message:
        adm = is_admin(callback.from_user.id) if callback.from_user else False
        await reply_with_help_text(callback.message, is_admin_override=adm)
    await callback.answer()


@router.message(F.voice)
async def voice_handler(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.voice:
        return

    log_user_message(message.from_user.id, message.from_user.username)

    user_id = message.from_user.id
    logger.info("Голосовое сообщение: %s секунд", message.voice.duration)
    await safe_send_chat_action(bot, message.chat.id, "typing")

    temp_file_path = ""
    try:
        file = await bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
            temp_file_path = temp_file.name

        await bot.download_file(file.file_path, destination=temp_file_path)
        voice_text = agent.transcribe_voice(temp_file_path)
        if not voice_text:
            await message.answer("Не удалось распознать голосовое 😥 Попробуй отправить еще раз.")
            return

        response = agent.process_message_with_agent(user_id=user_id, user_text=voice_text)
        buttons = resolve_proactive_buttons(
            user_id,
            response.answer,
            response.buttons,
            getattr(response, "is_generic", False),
            user_query=voice_text,
        )
        await send_ai_reply(message, response.answer, buttons)
    except Exception as exc:
        logger.exception("Ошибка обработки голосового: %s", exc)
        await message.answer("Не удалось обработать голосовое сообщение 😥")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError:
                pass


@router.callback_query()
async def callback_handler(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message or not callback.from_user:
        return

    user_id = callback.from_user.id
    data = callback.data or ""
    logger.info("Callback received: %s", data)

    await callback.answer()

    if data == FEEDBACK_SKIP:
        pop_pending_feedback(user_id)
        await callback.message.answer(
            "Окей, без комментария — запомнил как пропуск ✋ В другой раз расскажешь, если захочешь."
        )
        return

    await safe_send_chat_action(bot, callback.message.chat.id, "typing")

    if is_anchor_callback(data):
        kind, aid = parse_anchor_callback(data)
        if kind == "list":
            await _send_anchors_list(callback.message, user_id)
            return
        if kind == "open" and aid is not None:
            row = get_anchor(user_id, aid)
            if not row:
                await callback.message.answer(
                    "Этот якорь уже не найден — возможно, сняли или протухла кнопка 🌊"
                )
                return
            body = format_anchor_message(row)
            if len(body) > 4096:
                body = body[:4080] + "\n…"
            await callback.message.answer(
                body,
                reply_markup=build_anchor_view_keyboard(aid),
            )
            return
        if kind == "delete" and aid is not None:
            ok_del_a, removed_a = delete_anchor_by_id(user_id, aid)
            if ok_del_a and removed_a:
                await callback.message.answer(
                    f"Убрал якорь «{removed_a}» 🗑️ Если нужно — загляни в «покажи якоря» или поставь новый 🔖"
                )
            else:
                await callback.message.answer(
                    "Не нашёл этот якорь — может, уже стёрли. Обнови список 📋"
                )
            return
        return

    if is_scenario_callback(data):
        act, sid = parse_scenario_callback(data)
        if not act or not sid:
            await callback.message.answer("Кнопка устарела — начни новый «что если…» в чате 🎲")
            return
        sess = get_session(user_id, sid)
        if not sess:
            await callback.message.answer(
                "Сессия сценариев уже сдулась (они живут недолго) — напиши вопрос заново, я снова накатаю три ветки 🎈"
            )
            return
        if act == "b":
            await callback.message.answer(
                sess.intro_text,
                reply_markup=build_scenario_choice_keyboard(sid),
            )
            return
        if act == "s":
            await callback.message.answer(
                "Чтобы шаблон попал в коллекцию: после любого моего ответа напиши «сохрани это» "
                "или «сохрани как Название» — утащу последний ответ бота в шаблоны 📌😊"
            )
            return
        if act == "n":
            await callback.message.answer(
                "Окей, ловлю следующий вопрос — пиши в чат, развернём новую тему 💬✨"
            )
            return
        if act in ("o", "r", "p"):
            block = {"o": sess.optimist, "r": sess.realist, "p": sess.pessimist}[act]
            label = {
                "o": "оптимистичный 🟢",
                "r": "реалистичный 🟡",
                "p": "пессимистичный 🔴",
            }[act]
            try:
                expanded = await asyncio.to_thread(
                    run_scenario_expand,
                    agent,
                    sess.original,
                    label,
                    block,
                )
            except Exception as exc:
                logger.exception("scenario: углубление: %s", exc)
                await callback.message.answer(
                    "Не вышло развернуть сценарий — сеть капризничает. Попробуй нажать кнопку ещё раз 🔁"
                )
                return
            if len(expanded) > 4096:
                expanded = expanded[:4070] + "\n…"
            memory.save_user_memory(
                user_id,
                f"Разбор сценария: {label}",
                expanded,
            )
            await callback.message.answer(
                expanded,
                reply_markup=build_scenario_deep_keyboard(sid),
            )
        return

    if data.startswith(f"{EXPIRED_APPROVE_YES}:") or data.startswith(f"{EXPIRED_APPROVE_NO}:"):
        parts = data.split(":", 1)
        if len(parts) != 2:
            await callback.message.answer("Кнопка подтверждения устарела — набери /drafts ✍️")
            return
        action, sid = parts
        try:
            did = int(sid)
        except ValueError:
            await callback.message.answer("Кнопка подтверждения протухла — набери /drafts ✍️")
            return
        if action == EXPIRED_APPROVE_NO:
            await callback.message.answer("Окей, отменяем публикацию этого устаревшего черновика 👌")
            return
        row = get_draft(user_id, did)
        if not row or str(row.get("status")) not in {"draft", "expired"}:
            await callback.message.answer("Этот черновик уже не ждёт решения — /drafts для новой заготовки 📋")
            return
        ch_id = int(str(row["channel_id"]))
        body = str(row.get("content") or "")
        pub_text = channel_publish_text_from_draft_body(body)
        try:
            await bot.send_message(
                chat_id=ch_id,
                text=pub_text,
                disable_web_page_preview=False,
            )
        except TelegramBadRequest as exc:
            logger.warning("editor: публикация просроченного черновика: %s", exc)
            set_draft_status(user_id, did, "failed")
            await callback.message.answer(
                f"Не вышло запостить в канал: {exc}. Проверь права бота (публикация сообщений) и id канала ✍️"
            )
            return
        set_draft_status(user_id, did, "posted", set_approved_at=True)
        bump_approve(memory, user_id)
        src_ch = extract_tg_channel_username_from_url(str(row.get("source_url") or ""))
        if src_ch:
            bump_channel_quality(src_ch, approved_inc=1)
        await callback.message.answer("Публикую несмотря на возраст новости — пост ушёл в канал ✅")
        _schedule_ask_why(bot, callback.message.chat.id, user_id, did, "approved", body)
        return

    if is_editor_callback(data):
        act, did = parse_editor_callback(data)
        if not act or did is None:
            await callback.message.answer("Кнопка протухла — набери /drafts, сделаем свежую ✍️")
            return
        if not is_private_chat(callback.message):
            await callback.message.answer(
                "Черновики и апрув — только в личке со мной, иначе кнопки теряются в толпе 💬"
            )
            return
        row = get_draft(user_id, did)
        if not row or str(row.get("status")) not in {"draft", "expired"}:
            await callback.message.answer("Этот черновик уже не ждёт решения — /drafts для новой заготовки 📋")
            return
        ch_id = int(str(row["channel_id"]))
        body = str(row.get("content") or "")
        if act == "a":
            if is_draft_expired(row):
                if str(row.get("status")) != "expired":
                    set_draft_status(user_id, did, "expired")
                await callback.message.answer(
                    "Этой новости уже 24+ часа — всё равно публикуем?",
                    reply_markup=_expired_approve_keyboard(did),
                )
                return
            pub_text = channel_publish_text_from_draft_body(body)
            try:
                await bot.send_message(
                    chat_id=ch_id,
                    text=pub_text,
                    disable_web_page_preview=False,
                )
            except TelegramBadRequest as exc:
                logger.warning("editor: публикация в канал: %s", exc)
                set_draft_status(user_id, did, "failed")
                await callback.message.answer(
                    f"Не вышло запостить в канал: {exc}. Проверь права бота (публикация сообщений) и id канала ✍️"
                )
                return
            set_draft_status(user_id, did, "posted", set_approved_at=True)
            bump_approve(memory, user_id)
            src_ch = extract_tg_channel_username_from_url(str(row.get("source_url") or ""))
            if src_ch:
                bump_channel_quality(src_ch, approved_inc=1)
            await callback.message.answer(
                "Пост улетел в @kriptogeograph — ты на режиссёре, я на суфлёре 🎬✅"
            )
            _schedule_ask_why(bot, callback.message.chat.id, user_id, did, "approved", body)
            pending_after = count_drafts(user_id, "draft")
            logger.info(
                "editor approve: user_id=%s draft_id=%s pending_after=%s",
                user_id,
                did,
                pending_after,
            )
            if pending_after > 0:
                next_row = get_oldest_draft(user_id, "draft")
                if next_row:
                    extra = (
                        f"\n\nВ очереди после этого ещё {pending_after - 1} черновик(ов). "
                        f"Хочешь добавить свежий в хвост — /drafts ещё (до {MAX_PENDING_UNAPPROVED_DRAFTS} шт.) 📎"
                        if pending_after > 1
                        else "\n\nЭто был предпоследний: после него останется пусто. "
                        "Если захочешь ещё материал — /drafts ещё 🚀"
                    )
                    await callback.message.answer(
                        draft_dm_text(next_row) + extra,
                        reply_markup=build_editor_keyboard(int(next_row["id"])),
                    )
                return
            prefs_after = memory.get_style_preferences(user_id)
            if is_auto_enabled_pref(prefs_after):
                await callback.message.answer(
                    "Очередь теперь пустая, но я уже грею моторы 🤖⚡ "
                    "Следующий черновик подъедет сам по авто-режиму. "
                    "Не хочешь ждать — жми /drafts, принесу вручную."
                )
            else:
                await callback.message.answer(
                    "Очередь чистая как лист бумаги 🧼📝 "
                    "Жми /drafts — и закину следующий материал на разбор."
                )
            return
        if act == "r":
            set_draft_status(user_id, did, "rejected")
            append_reject_hint(memory, user_id, hint_for_reject_from_draft(row))
            src_ch = extract_tg_channel_username_from_url(str(row.get("source_url") or ""))
            if src_ch:
                bump_channel_quality(src_ch, rejected_inc=1)
            await callback.message.answer(
                "Записал отказ в мою «чёрную маленькую тетрадь» подбора — в следующий раз уйду чуть в сторону 📝❌"
            )
            _schedule_ask_why(bot, callback.message.chat.id, user_id, did, "rejected", body)
            return
        if act == "x":
            set_draft_status(user_id, did, "rejected")
            await callback.message.answer(
                "Понял, новость протухла — не будем позорить канал залежалым 🗓️❌\n"
                "Запомнил что ищем только свежак."
            )
            pending_after = count_drafts(user_id, "draft")
            logger.info(
                "editor expired_content: user_id=%s draft_id=%s pending_after=%s",
                user_id,
                did,
                pending_after,
            )
            if pending_after > 0:
                next_row = get_oldest_draft(user_id, "draft")
                if next_row:
                    extra = (
                        f"\n\nВ очереди после этого ещё {pending_after - 1} черновик(ов). "
                        f"Хочешь добавить свежий в хвост — /drafts ещё (до {MAX_PENDING_UNAPPROVED_DRAFTS} шт.) 📎"
                        if pending_after > 1
                        else "\n\nЭто был предпоследний: после него останется пусто. "
                        "Если захочешь ещё материал — /drafts ещё 🚀"
                    )
                    await callback.message.answer(
                        draft_dm_text(next_row) + extra,
                        reply_markup=build_editor_keyboard(int(next_row["id"])),
                    )
                return
            prefs_after = memory.get_style_preferences(user_id)
            if is_auto_enabled_pref(prefs_after):
                await callback.message.answer(
                    "Очередь теперь пустая, но я уже грею моторы 🤖⚡ "
                    "Следующий черновик подъедет сам по авто-режиму. "
                    "Не хочешь ждать — жми /drafts, принесу вручную."
                )
            else:
                await callback.message.answer(
                    "Очередь чистая как лист бумаги 🧼📝 "
                    "Жми /drafts — и закину следующий материал на разбор."
                )
            return
        if act == "e":
            set_pending_edit(user_id, did)
            await callback.message.answer(
                "Жду новый текст одним сообщением (без /команд) — подменю черновик и снова дам кнопки ✏️👇"
            )
            return
        return

    if is_template_callback(data):
        kind, tid = parse_template_callback(data)
        if kind == "list":
            await _send_templates_list(callback.message, user_id)
            return
        if kind == "open" and tid is not None:
            row = get_template(user_id, tid)
            if not row:
                await callback.message.answer(
                    "Этот шаблон уже не найден — возможно, удалили или устарела кнопка 🤷"
                )
                return
            title = str(row["title"])
            body = format_template_body(str(row["content"]))
            header = f"Вот «{title}» — сохранённый ответ:\n\n"
            if len(header) + len(body) > 4096:
                body = body[: 4096 - len(header) - 30] + "\n…"
            await callback.message.answer(
                header + body,
                reply_markup=build_template_view_keyboard(int(row["id"])),
            )
            return
        if kind == "delete" and tid is not None:
            ok_del, removed_title = delete_template_by_id(user_id, tid)
            if ok_del and removed_title:
                await callback.message.answer(
                    f"Готово 🗑️ Шаблон «{removed_title}» удалён. "
                    f"Если нужно — загляни в «покажи шаблоны» или жми «Ещё шаблоны» под следующей записью."
                )
            else:
                await callback.message.answer(
                    "Не нашёл этот шаблон — возможно, его уже стёрли. Обнови список 📋"
                )
            return
        return

    if data == "concierge_run":
        if not agent.concierge_enabled:
            await callback.message.answer("Режим консьержа сейчас выключен ⚙️")
            return
        history = memory.get(user_id)
        last_user_message = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        query = agent.consume_pending_concierge(user_id) or (last_user_message or "").strip()
        if not query or query.startswith("Пользователь нажал кнопку"):
            query = last_user_message
        if not query:
            await callback.message.answer("Не пойму, по чему искать — напиши запрос текстом 🔎")
            return
        search_result = agent.search_with_tavily(query[:400])
        logger.info("Консьерж: Tavily для user_id=%s", user_id)
        response = agent.process_message_with_agent(
            user_id=user_id,
            user_text=(
                "Пользователь нажал кнопку «да, помоги действием» (консьерж). "
                f"Исходный запрос для поиска: {query}\n\n"
                f"Результаты веб-поиска:\n{search_result}\n\n"
                "Собери конкретный полезный ответ: варианты, факты, ссылки в скобках как в правилах. "
                "Если поиск недоступен, пуст или только заглушка — честно скажи и предложи уточнить город, даты, бюджет "
                "или включить веб-поиск в настройках. Стиль Кузьмы."
            ),
            skip_concierge_tracking=True,
        )
        logger.info("Отправляю ответ пользователю")
        buttons = resolve_proactive_buttons(
            user_id,
            response.answer,
            response.buttons,
            getattr(response, "is_generic", False),
            user_query=query or None,
        )
        await send_ai_reply(callback.message, response.answer, buttons)
    elif data in {"web_search", "latest_updates"}:
        history = memory.get(user_id)
        last_user_message = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        if not last_user_message:
            logger.info("Отправляю ответ пользователю")
            await callback.message.answer("Пока нет текста для поиска 🌐")
        else:
            if data == "latest_updates":
                query = f"свежие последние новости: {last_user_message[:260]}"
            else:
                query = last_user_message[:300]
            search_result = agent.web_search(query)
            logger.info("Callback: запускаю агента")
            response = agent.process_message_with_agent(
                user_id=user_id,
                user_text=f"Используй результаты веб-поиска и ответь пользователю:\n{search_result}",
                skip_concierge_tracking=True,
            )
            logger.info("Callback: агент вернул ответ")
            logger.info("Отправляю ответ пользователю")
            buttons = resolve_proactive_buttons(
                user_id,
                response.answer,
                response.buttons,
                getattr(response, "is_generic", False),
                user_query=last_user_message or None,
            )
            await send_ai_reply(callback.message, response.answer, buttons)
    elif data == "clear_history":
        memory.clear_user_memory(user_id)
        agent.clear_clarification_pending(user_id)
        logger.info("Отправляю ответ пользователю")
        await callback.message.answer("История диалога очищена 🗑️ Начнем заново?")
    else:
        logger.info("Callback: запускаю агента")
        response = agent.process_message_with_agent(
            user_id=user_id,
            user_text=f"Пользователь нажал кнопку: {data}. Продолжи диалог на основе контекста.",
            skip_concierge_tracking=True,
        )
        logger.info("Callback: агент вернул ответ")
        logger.info("Отправляю ответ пользователю")
        buttons = resolve_proactive_buttons(
            user_id,
            response.answer,
            response.buttons,
            getattr(response, "is_generic", False),
            user_query=None,
        )
        await send_ai_reply(callback.message, response.answer, buttons)


async def main() -> None:
    logger.info("Запуск бота, PID=%s", os.getpid())
    init_db()
    logger.info("База данных инициализирована: bot_database.db")
    init_stats_db()
    logger.info("База статистики инициализирована: bot_statistics.db")
    init_admin_db()
    logger.info("Админка: таблица банов готова (bot_statistics.db)")
    telegram_session = build_telegram_session()
    if telegram_session:
        bot = Bot(token=config.telegram_token, session=telegram_session)
    else:
        bot = Bot(token=config.telegram_token)

    dp = Dispatcher()
    install_diagnostics_heartbeat_middleware(dp, _self_diagnostics)
    dp.message.middleware(BanCheckMiddleware())
    dp.message.middleware(AdminAuthMiddleware())
    dp.include_router(health_check_router)
    dp.include_router(router)
    try:
        await ensure_bot_identity(bot)
        await setup_bot_command_menu(bot, config.admin_id)
    except TelegramConflictError:
        logger.warning("Другая копия бота уже запущена")
        await bot.session.close()
        agent.close()
        return

    autofetch_task = asyncio.create_task(
        run_content_editor_autofetch_loop(bot, agent, memory)
    )
    logger.info(
        "Auto-search: background task scheduled (task=%r)",
        autofetch_task,
    )
    diag_notify = (
        config.admin_id
        if config.admin_id is not None
        else (OWNER_RESTART_USER_ID if OWNER_RESTART_USER_ID else None)
    )
    _self_diagnostics.bind_dispatcher(dp)
    diag_task = asyncio.create_task(
        _self_diagnostics.auto_diagnostics_loop(bot, diag_notify, 1800.0)
    )
    logger.info(
        "Self-diagnostics: background task scheduled (task=%r notify_chat_id=%s)",
        diag_task,
        diag_notify,
    )
    _self_diagnostics.mark_user_activity()
    try:
        while True:
            await _self_diagnostics.run_long_polling(bot)
            if not _self_diagnostics.consume_pending_poll_restart():
                break
            await _self_diagnostics.complete_stuck_poll_recovery(bot, diag_notify)
            _self_diagnostics.schedule_poll_restarted_notice(bot, diag_notify)
    finally:
        diag_task.cancel()
        try:
            await diag_task
        except asyncio.CancelledError:
            pass
        autofetch_task.cancel()
        try:
            await autofetch_task
        except asyncio.CancelledError:
            pass
        agent.close()
        await bot.session.close()
        logger.info("Бот остановлен корректно")


def _match_fallback_branch(answer_text: str) -> tuple[list[dict[str, str]], str]:
    """Эвристика по одному фрагменту текста (ответ бота или текущий запрос пользователя)."""
    text = answer_text.lower()

    def hit(branch: str, buttons: list[dict[str, str]]) -> tuple[list[dict[str, str]], str]:
        logger.info("Fallback: сработали ключевые слова → ветка %s", branch)
        return buttons, branch

    if any(
        w in text
        for w in (
            "трансмисс",
            "кпп",
            "коробк передач",
            "сцеплен",
            "редуктор",
            "кардан",
            "суппорт",
            "привод",
            "дифференциал",
        )
    ) or (
        "передач" in text
        and any(x in text for x in ("коробк", "авто", "машин", "механизм", "ведущ"))
    ):
        return hit(
            "auto_drivetrain",
            [
                {"text": "Устройство по шагам ⚙️", "callback_data": "ask_followup"},
                {"text": "Типы и отличия 🔄", "callback_data": "ask_followup"},
                {"text": "Обслуживание и неисправности 🛠️", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "телеграм",
            "telegram",
            "aiogram",
            "айограм",
            "чат-бот",
            "чатбот",
            "телеграм-бот",
            "python-telegram-bot",
            "pytelegrambot",
        )
    ):
        return hit(
            "tech_dev",
            [
                {"text": "Код и структура 💻", "callback_data": "ask_followup"},
                {"text": "Библиотеки и API 📚", "callback_data": "ask_followup"},
                {"text": "Тестирование и запуск 🧪", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "авиабилет",
            "перелёт",
            "перелет",
            "аэропорт",
            "рейс",
            "самолёт",
            "самолет",
            "багаж",
            "регистрац",
        )
    ):
        return hit(
            "flights",
            [
                {"text": "Как выбрать рейс ✈️", "callback_data": "ask_followup"},
                {"text": "Цены и даты 💳", "callback_data": "ask_followup"},
                {"text": "Багаж и регистрация 🧳", "callback_data": "ask_followup"},
            ],
        )

    if any(w in text for w in ("егэ", "огэ")):
        return hit(
            "exam_prep",
            [
                {"text": "План подготовки 📅", "callback_data": "ask_followup"},
                {"text": "Ресурсы и материалы 📚", "callback_data": "ask_followup"},
                {"text": "Мотивация и режим 💪", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "дамли",
            "дампли",
            "пельмен",
            "момо",
            "вонтон",
            "гёдза",
            "баоцз",
            "мант",
        )
    ):
        return hit(
            "dumplings",
            [
                {"text": "Ингредиенты теста и начинки 🥟", "callback_data": "ask_followup"},
                {"text": "Лепка и варка по шагам 📋", "callback_data": "ask_followup"},
                {"text": "Соусы и подача 🍶", "callback_data": "ask_followup"},
            ],
        )

    if bool(re.search(r"\bэффект\b", text)) or any(
        w in text
        for w in (
            "физическ",
            "квантов",
            "термодинам",
            "электромагнит",
            "оптическ",
            "релятивист",
            "интерференц",
            "дифракц",
            "поляризац",
            "фотон",
            "электрон",
        )
    ):
        return hit(
            "physics",
            [
                {"text": "Где применяется 🔬", "callback_data": "ask_followup"},
                {"text": "Детали и суть 📐", "callback_data": "ask_followup"},
                {"text": "Похожие эффекты 🧪", "callback_data": "ask_followup"},
            ],
        )

    cooking_gotovk = bool(re.search(r"(?<!под)готовк", text))
    food_kw = any(
        w in text
        for w in (
            "приготовить",
            "рецепт",
            "блюд",
            "кулинар",
            "борщ",
            "запек",
            "туш",
            "жарен",
        )
    )
    soup_word = bool(re.search(r"(?<!супп)\bсуп\b", text))
    if food_kw or cooking_gotovk or soup_word:
        return hit(
            "food",
            [
                {"text": "Ингредиенты 🥕", "callback_data": "ask_followup"},
                {"text": "Пошагово 📋", "callback_data": "ask_followup"},
                {"text": "Время и нюансы ⏱️", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "отпуск",
            "путешеств",
            "поехать",
            "туризм",
            "курорт",
            "отдых",
            "виза",
            "поезд",
            "электричк",
            "плацкарт",
            "купе",
            "вагон",
            "железнодорож",
            "ржд",
            "ж/д",
        )
    ):
        return hit(
            "travel",
            [
                {"text": "Маршрут и билеты 🎫", "callback_data": "ask_followup"},
                {"text": "Комфорт в пути 🧳", "callback_data": "ask_followup"},
                {"text": "Сравнить с другим транспортом 🔄", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "носки",
            "носках",
            "перчатк",
            "варежк",
            "одежд",
            "обув",
            "аксессуар",
        )
    ) or (
        "размер" in text
        and any(w in text for w in ("перчатк", "обув", "одежд", "куртк"))
    ):
        return hit(
            "clothing",
            [
                {"text": "Материалы и уход 🧵", "callback_data": "ask_followup"},
                {"text": "Как подобрать размер 📏", "callback_data": "ask_followup"},
                {"text": "Где купить 🛒", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in ("биткоин", "крипт", "блокчейн", "инвест", "акци", "трейд", "бирж")
    ):
        return hit(
            "crypto_finance",
            [
                {"text": "Курс и динамика 📊", "callback_data": "ask_followup"},
                {"text": "Как купить безопасно 💱", "callback_data": "ask_followup"},
                {"text": "Риски и хранение ⚠️", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in ("двигатель", "автомобил", "машин", "тормоз", "подвеск", "аккумулятор")
    ):
        return hit(
            "auto_general",
            [
                {"text": "Принцип работы ⚙️", "callback_data": "ask_followup"},
                {"text": "Детали и узлы 🔩", "callback_data": "ask_followup"},
                {"text": "Обслуживание 🛠️", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "выучить",
            "изучить",
            "научиться",
            "урок",
            "образование",
            "обучен",
            "лекци",
        )
    ) or (
        "курс" in text
        and any(w in text for w in ("онлайн", "школ", "универс", "язык", "программ"))
    ) or (
        "подготовк" in text
        and "экзамен" in text
        and "егэ" not in text
        and "огэ" not in text
    ):
        return hit(
            "education",
            [
                {"text": "С чего начать 🚀", "callback_data": "ask_followup"},
                {"text": "Сроки и план ⏱️", "callback_data": "ask_followup"},
                {"text": "Бесплатные ресурсы 🆓", "callback_data": "ask_followup"},
            ],
        )

    if any(
        w in text
        for w in (
            "теорема",
            "формула",
            "математик",
            "уравнен",
            "доказательств",
            "геометр",
            "алгебр",
            "пифагор",
        )
    ):
        return hit(
            "math_science",
            [
                {"text": "Примеры задач 📐", "callback_data": "ask_followup"},
                {"text": "Как применить 🔧", "callback_data": "ask_followup"},
                {"text": "История и идея 🕰️", "callback_data": "ask_followup"},
            ],
        )

    if any(w in text for w in ("пошагово", "инструкц", "алгоритм", "последовательно")):
        return hit(
            "howto_general",
            [
                {"text": "Что понадобится 🔧", "callback_data": "ask_followup"},
                {"text": "Частые ошибки ⚠️", "callback_data": "ask_followup"},
                {"text": "Альтернативные способы 🔄", "callback_data": "ask_followup"},
            ],
        )

    if any(w in text for w in ("сравнить", "лучше", "разница", "отличие", "выбрать", "какой из")):
        return hit(
            "compare",
            [
                {"text": "Плюсы и минусы ⚖️", "callback_data": "ask_followup"},
                {"text": "Критерии выбора 📋", "callback_data": "ask_followup"},
                {"text": "Что взять в твоём случае 🎯", "callback_data": "ask_followup"},
            ],
        )

    if any(w in text for w in ("что такое", "это значит", "принцип", "объясни", "устроен")):
        return hit(
            "explain",
            [
                {"text": "Простыми словами ещё раз 💡", "callback_data": "ask_followup"},
                {"text": "Пример из практики 📚", "callback_data": "ask_followup"},
                {"text": "Что может пойти не так ⚠️", "callback_data": "ask_followup"},
            ],
        )

    if any(w in text for w in ("совет", "рекоменд", "стоит ли", "предлага")):
        return hit(
            "advice",
            [
                {"text": "Альтернативы 🔄", "callback_data": "ask_followup"},
                {"text": "План действий 🚀", "callback_data": "ask_followup"},
                {"text": "Экономия и лайфхаки 💡", "callback_data": "ask_followup"},
            ],
        )

    if any(w in text for w in ("цена", "стоим", "дорог", "дёшев", "бюджет", "платн")):
        return hit(
            "price",
            [
                {"text": "Где выгоднее 🛒", "callback_data": "ask_followup"},
                {"text": "Как сэкономить 💡", "callback_data": "ask_followup"},
                {"text": "Окупается ли 🤔", "callback_data": "ask_followup"},
            ],
        )

    return hit(
        "default",
        [
            {"text": "Развить тему дальше 💬", "callback_data": "ask_followup"},
            {"text": "Другой угол вопроса 🔄", "callback_data": "ask_followup"},
            {"text": "Помощь /help 🙌", "callback_data": "need_help"},
        ],
    )


def generate_context_buttons_fallback(
    answer_text: str,
    user_query: str | None = None,
) -> tuple[list[dict[str, str]], str]:
    """Кнопки по ответу бота; тема текущего запроса переопределяет ложные совпадения из «эха» в ответе."""
    q = user_query or ""
    a = answer_text or ""
    logger.info(
        "fallback: query='%s', answer='%s'",
        q[:50] + ("..." if len(q) > 50 else ""),
        a[:50] + ("..." if len(a) > 50 else ""),
    )
    ab, ba = _match_fallback_branch(answer_text)
    uq = (user_query or "").strip()
    if not uq:
        return ab, ba
    ub, bu = _match_fallback_branch(uq)
    if bu != "default" and (bu != ba or ba == "default"):
        logger.info(
            "Fallback: приоритет темы текущего запроса (ветка %s) над эвристикой ответа (ветка %s)",
            bu,
            ba,
        )
        return ub, bu
    return ab, ba


if __name__ == "__main__":
    asyncio.run(main())
