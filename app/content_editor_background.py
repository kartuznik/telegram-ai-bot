"""Фоновый цикл авто-поиска черновиков для редактора контента (Tavily + ЛС)."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from app.content_editor import (
    PREF_AUTO_DISABLED_REASON,
    PREF_AUTO_ENABLED,
    PREF_AUTO_PAUSED_UNTIL_TS,
    PREF_LAST_AUTO_FETCH_TS,
    PREF_LAST_LIMIT_NOTIFY_TS,
    MAX_PENDING_UNAPPROVED_DRAFTS,
    auto_paused_until_ts,
    build_editor_keyboard,
    count_drafts,
    create_draft_from_search,
    draft_dm_text,
    get_draft,
    get_source_mode,
    is_auto_enabled_pref,
    is_auto_paused,
    is_editor_enabled,
    iter_content_editor_user_ids,
    reject_spree_should_pause,
    seconds_until_next_auto_fetch,
)
from app.llm_agent import LLMAgent
from app.memory import ChatMemory

logger = logging.getLogger(__name__)

TICK_SECONDS = 15 * 60
LIMIT_NOTIFY_COOLDOWN_SEC = 12 * 3600
REJECT_PAUSE_SEC = 48 * 3600


def _disable_auto_no_dm(memory: ChatMemory, user_id: int, reason: str) -> None:
    memory.update_style_preferences(
        user_id,
        {
            PREF_AUTO_ENABLED: "0",
            PREF_AUTO_DISABLED_REASON: reason,
        },
    )


async def run_content_editor_autofetch_loop(
    bot: Bot,
    agent: LLMAgent,
    memory: ChatMemory,
) -> None:
    # Явная строка для grep в логах при проверке деплоя
    logger.info("Auto-search: starting (interval between ticks=%ss)", TICK_SECONDS)
    logger.info("content_editor autofetch: loop entered (same as Auto-search)")
    try:
        while True:
            try:
                await _autofetch_tick(bot, agent, memory)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("content_editor autofetch: tick failed")
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("Auto-search: stopped (cancelled)")
        logger.info("content_editor autofetch: loop cancelled")
        raise


async def _autofetch_tick(bot: Bot, agent: LLMAgent, memory: ChatMemory) -> None:
    uids = iter_content_editor_user_ids()
    if not uids:
        return
    for uid in uids:
        try:
            await _process_user_autofetch(bot, agent, memory, uid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("content_editor autofetch: user_id=%s", uid)
        await asyncio.sleep(0.35)


async def _process_user_autofetch(bot: Bot, agent: LLMAgent, memory: ChatMemory, user_id: int) -> None:
    prefs = memory.get_style_preferences(user_id)
    if not is_editor_enabled(memory, user_id):
        return
    if get_source_mode(prefs) == "web" and not getattr(agent, "tavily", None):
        logger.debug(
            "content_editor autofetch: user_id=%s источники:web без Tavily — пропуск",
            user_id,
        )
        return
    if not is_auto_enabled_pref(prefs):
        return

    p_until = auto_paused_until_ts(prefs)
    if p_until is not None and time.time() >= p_until:
        memory.update_style_preferences(user_id, {PREF_AUTO_PAUSED_UNTIL_TS: ""})
        prefs = memory.get_style_preferences(user_id)

    if is_auto_paused(prefs):
        logger.debug("content_editor autofetch: user_id=%s на паузе", user_id)
        return

    if reject_spree_should_pause(user_id, prefs):
        until = time.time() + REJECT_PAUSE_SEC
        memory.update_style_preferences(user_id, {PREF_AUTO_PAUSED_UNTIL_TS: str(until)})
        logger.warning(
            "content_editor autofetch: серия отказов — пауза ~48ч, user_id=%s",
            user_id,
        )
        try:
            await bot.send_message(
                user_id,
                "Кузьма на мастерских: ты частенько жмёшь «отмена» — автопоиск на паузе на пару дней, "
                "чтобы не долбить одно и то же 📎🙃 Руками /drafts работают. Когда отлежится душа — вернёмся к авто.",
            )
        except TelegramForbiddenError:
            _disable_auto_no_dm(memory, user_id, "telegram_forbidden_pause_notify")
        except Exception as exc:
            logger.warning("content_editor autofetch: не удалось уведомить о паузе user_id=%s: %s", user_id, exc)
        return

    n_pending = count_drafts(user_id, "draft")
    if n_pending >= MAX_PENDING_UNAPPROVED_DRAFTS:
        logger.info(
            "content_editor autofetch: лимит user_id=%s pending_drafts=%s >= %s — новый черновик не создаём",
            user_id,
            n_pending,
            MAX_PENDING_UNAPPROVED_DRAFTS,
        )
        now = time.time()
        last_ln = (prefs.get(PREF_LAST_LIMIT_NOTIFY_TS) or "").strip()
        send_note = True
        if last_ln:
            try:
                if now - float(last_ln) < LIMIT_NOTIFY_COOLDOWN_SEC:
                    send_note = False
            except ValueError:
                pass
        if send_note:
            memory.update_style_preferences(user_id, {PREF_LAST_LIMIT_NOTIFY_TS: str(now)})
            msg = (
                f"На подоконнике уже {MAX_PENDING_UNAPPROVED_DRAFTS} неразобранных черновиков — новый автоматом не принёс, "
                "чтобы не устроить хаос как в редакции перед дедлайном 📑✋ Разгреби ✅/✏️/❌ — и снова пойду в интернет."
            )
            try:
                await bot.send_message(user_id, msg)
                logger.info(
                    "content_editor autofetch: лимит черновиков, уведомление user_id=%s",
                    user_id,
                )
            except TelegramForbiddenError:
                _disable_auto_no_dm(memory, user_id, "telegram_forbidden_limit_notify")
            except Exception as exc:
                logger.warning("content_editor autofetch: лимит — не отправилось user_id=%s: %s", user_id, exc)
        else:
            logger.debug(
                "content_editor autofetch: лимит черновиков (уведомление недавно), user_id=%s",
                user_id,
            )
        return

    wait = seconds_until_next_auto_fetch(prefs)
    if wait > 0:
        logger.debug(
            "content_editor autofetch: user_id=%s следующий проход через ~%.0fs",
            user_id,
            wait,
        )
        return

    ok, res, _dm = await asyncio.to_thread(
        create_draft_from_search,
        agent,
        memory,
        user_id,
    )
    memory.update_style_preferences(user_id, {PREF_LAST_AUTO_FETCH_TS: str(time.time())})

    if not ok:
        logger.info(
            "content_editor autofetch: черновик не создан user_id=%s: %s",
            user_id,
            res,
        )
        return

    draft_id = int(res)
    row = get_draft(user_id, draft_id)
    if not row:
        logger.error("content_editor autofetch: черновик не читается user_id=%s id=%s", user_id, draft_id)
        return
    text = draft_dm_text(row)
    try:
        await bot.send_message(
            user_id,
            text,
            reply_markup=build_editor_keyboard(draft_id),
        )
        logger.info(
            "content_editor autofetch: черновик отправлен user_id=%s draft_id=%s",
            user_id,
            draft_id,
        )
    except TelegramForbiddenError:
        logger.error(
            "content_editor autofetch: нет ЛС с user_id=%s — авто выключаю",
            user_id,
        )
        _disable_auto_no_dm(memory, user_id, "telegram_forbidden_draft_dm")
    except Exception as exc:
        logger.exception("content_editor autofetch: отправка черновика user_id=%s: %s", user_id, exc)
