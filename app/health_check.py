"""Команды мониторинга: /status, /selftest, /fulldiag, /restart и рестарт процесса."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.admin import NO_ADMIN_RIGHTS, is_admin
from app.self_diagnostics import SelfDiagnostics

logger = logging.getLogger(__name__)

router = Router(name="health_check")

# Перезапуск процесса — только этот Telegram user id (владелец).
OWNER_RESTART_USER_ID = 504425191

_diagnostics: SelfDiagnostics | None = None


def attach_self_diagnostics(sd: SelfDiagnostics) -> None:
    """Вызывается из main после создания SelfDiagnostics."""
    global _diagnostics
    _diagnostics = sd


def get_self_diagnostics() -> SelfDiagnostics:
    if _diagnostics is None:
        raise RuntimeError("SelfDiagnostics не подключён — вызовите attach_self_diagnostics из main")
    return _diagnostics


def _can_view_status(user_id: int) -> bool:
    """Полная сводка /status — только админ из конфига или владелец по user id."""
    return is_admin(user_id) or user_id == OWNER_RESTART_USER_ID


async def restart_process(bot: Bot) -> None:
    """Закрывает сессию Telegram и подменяет процесс через exec (общий путь для /restart и авто-диагностики)."""
    try:
        await bot.session.close()
    except Exception as exc:
        logger.warning("restart_process: session.close: %s", exc)

    await asyncio.sleep(0.5)

    argv = [sys.executable, *sys.argv]
    try:
        logger.warning(
            "restart_process: os.execv executable=%r argv=%r cwd=%s",
            sys.executable,
            argv,
            os.getcwd(),
        )
        os.execv(sys.executable, argv)
    except Exception as exc:
        logger.exception("restart_process: os.execv не удался: %s", exc)
        raise


@router.message(Command("status"))
async def status_cmd(message: Message) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if not _can_view_status(uid):
        await message.answer(NO_ADMIN_RIGHTS)
        return

    sd = get_self_diagnostics()
    try:
        text = await sd.technical_check()
    except Exception as exc:
        logger.exception("health: technical_check: %s", exc)
        await message.answer(f"Не удалось выполнить проверку: {exc}")
        return
    await message.answer(text)


@router.message(Command("selftest"))
async def selftest_cmd(message: Message) -> None:
    if not message.from_user:
        return
    if not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    sd = get_self_diagnostics()
    _results, report = sd.functional_check()
    await message.answer(report)


@router.message(Command("fulldiag"))
async def fulldiag_cmd(message: Message) -> None:
    if not message.from_user:
        return
    if not is_admin(message.from_user.id):
        await message.answer(NO_ADMIN_RIGHTS)
        return
    sd = get_self_diagnostics()
    try:
        text = await sd.full_check()
    except Exception as exc:
        logger.exception("health: full_check: %s", exc)
        await message.answer(f"Не удалось выполнить полную диагностику: {exc}")
        return
    await message.answer(text)


@router.message(Command("restart"))
async def restart_cmd(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    if message.from_user.id != OWNER_RESTART_USER_ID:
        await message.answer("Команда /restart доступна только владельцу бота.")
        return

    try:
        await message.answer(
            "Перезапуск: закрываю сессию Telegram и подменяю процесс через exec "
            f"(PID {os.getpid()} → новый). Через несколько секунд бот снова онлайн."
        )
    except Exception as exc:
        logger.warning("restart: не удалось отправить подтверждение: %s", exc)

    try:
        await restart_process(bot)
    except Exception:
        try:
            await message.answer(
                "Не удалось выполнить перезапуск процесса (exec). "
                "Проверьте логи и перезапустите сервис вручную."
            )
        except Exception as send_exc:
            logger.error("restart: не удалось сообщить об ошибке exec: %s", send_exc)
