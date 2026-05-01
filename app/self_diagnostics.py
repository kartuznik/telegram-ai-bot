"""Единая самодиагностика: техника, функционал, фоновый цикл и heartbeat."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import psutil
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import TelegramObject

from app.config import Config
from app.database import DB_PATH, get_connection
from app.llm_agent import LLMAgent
from app.selftest import BotSelfTest
from app.statistics import _path as statistics_db_path

logger = logging.getLogger(__name__)

GET_ME_TIMEOUT_SEC = 20.0
GET_ME_FAIL_THRESHOLD = 3
# Нет входящих апдейтов дольше этого — считаем polling зависшим и перезапускаем.
POLL_STUCK_IDLE_SEC = 90 * 60


def _format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours or days:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    parts.append(f"{secs}с")
    return " ".join(parts)


def _system_uptime_sec() -> float | None:
    try:
        return max(0.0, time.time() - float(psutil.boot_time()))
    except Exception as exc:
        logger.warning("self_diagnostics: system uptime: %s", exc)
        return None


def _process_uptime_sec() -> float | None:
    try:
        return max(0.0, time.time() - float(psutil.Process(os.getpid()).create_time()))
    except Exception as exc:
        logger.warning("self_diagnostics: process uptime: %s", exc)
        return None


def _collect_system_metrics() -> dict[str, str]:
    lines: dict[str, str] = {}
    try:
        vm = psutil.virtual_memory()
        lines["memory_system"] = (
            f"{vm.percent:.1f}% занято, "
            f"доступно {vm.available / (1024**3):.2f} ГиБ из {vm.total / (1024**3):.2f} ГиБ"
        )
    except Exception as exc:
        lines["memory_system"] = f"ошибка: {exc}"
        logger.exception("self_diagnostics: virtual_memory")

    try:
        cpu = psutil.cpu_percent(interval=0.15)
        lines["cpu"] = f"{cpu:.1f}% (средняя загрузка CPU, короткий замер)"
    except Exception as exc:
        lines["cpu"] = f"ошибка: {exc}"
        logger.exception("self_diagnostics: cpu_percent")

    try:
        proc = psutil.Process(os.getpid())
        rss = proc.memory_info().rss
        lines["memory_process"] = f"RSS процесса бота: {rss / (1024**2):.1f} МиБ"
    except Exception as exc:
        lines["memory_process"] = f"ошибка: {exc}"
        logger.exception("self_diagnostics: process memory")

    su = _system_uptime_sec()
    lines["uptime_system"] = _format_duration(su) if su is not None else "недоступно"
    pu = _process_uptime_sec()
    lines["uptime_process"] = _format_duration(pu) if pu is not None else "недоступно"
    return lines


def _probe_sqlite(path: Path, timeout_sec: float = 5.0) -> tuple[bool, str | None]:
    try:
        with sqlite3.connect(str(path), timeout=timeout_sec) as conn:
            conn.execute("SELECT 1").fetchone()
        return True, None
    except Exception as exc:
        logger.exception("self_diagnostics: sqlite probe %s", path)
        return False, str(exc)


def _main_db_sync() -> tuple[bool, str | None]:
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return True, None
    except Exception as exc:
        logger.exception("self_diagnostics: main DB via get_connection")
        return False, str(exc)


RestartFn = Callable[[Bot], Awaitable[None]]


class SelfDiagnostics:
    def __init__(
        self,
        config: Config,
        agent: LLMAgent | None,
        restart_process: RestartFn,
    ) -> None:
        self._config = config
        self._agent = agent
        self._restart_process = restart_process
        self._last_update_ts = time.time()
        self._cycle_count = 0
        self._auto_run_functional_check = False
        self._dispatcher: Dispatcher | None = None
        self._pending_poll_restart = False
        self._poll_restart_done_for_idle_streak = False

    def mark_user_activity(self) -> None:
        """Вызывать из middleware на каждый входящий апдейт (heartbeat)."""
        self._last_update_ts = time.time()
        self._poll_restart_done_for_idle_streak = False

    def bind_dispatcher(self, dispatcher: Dispatcher | None) -> None:
        """Вызывать из main после создания Dispatcher (до start_polling и фоновых задач)."""
        self._dispatcher = dispatcher

    async def run_long_polling(self, bot: Bot) -> None:
        if self._dispatcher is None:
            raise RuntimeError("SelfDiagnostics: нет dispatcher — вызовите bind_dispatcher(dp)")
        await self._dispatcher.start_polling(bot)

    def consume_pending_poll_restart(self) -> bool:
        """После выхода из start_polling: True, если остановка была из-за авто-перезапуска polling."""
        if self._pending_poll_restart:
            self._pending_poll_restart = False
            return True
        return False

    async def complete_stuck_poll_recovery(
        self,
        bot: Bot,
        notify_chat_id: int | None,
    ) -> None:
        """Пауза после stop_polling; успех — отложенно, когда long polling уже снова в работе (см. main)."""
        await asyncio.sleep(5)

    def schedule_poll_restarted_notice(
        self,
        bot: Bot,
        notify_chat_id: int | None,
        delay_sec: float = 2.0,
    ) -> None:
        """Отправить «✅» через delay_sec после того, как main снова вызвал start_polling."""

        async def _go() -> None:
            try:
                await asyncio.sleep(delay_sec)
                await self._safe_send_alert(bot, notify_chat_id, "✅ Polling перезапущен!")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("self_diagnostics: schedule_poll_restarted_notice")

        asyncio.create_task(_go())

    async def technical_check(self) -> str:
        try:
            metrics = await asyncio.to_thread(_collect_system_metrics)
        except Exception as exc:
            logger.exception("self_diagnostics: metrics: %s", exc)
            return f"Не удалось собрать метрики системы: {exc}"

        try:
            db_lines = await self._check_databases()
        except Exception as exc:
            logger.exception("self_diagnostics: database checks: %s", exc)
            db_lines = [f"Проверка БД: ошибка — {exc}"]

        text = "\n".join(
            [
                "Состояние бота",
                "",
                f"PID: {os.getpid()}",
                f"Python: {sys.version.split()[0]}",
                "",
                f"CPU: {metrics.get('cpu', '—')}",
                f"Память (система): {metrics.get('memory_system', '—')}",
                f"Память (процесс): {metrics.get('memory_process', '—')}",
                f"Аптайм ОС: {metrics.get('uptime_system', '—')}",
                f"Аптайм процесса: {metrics.get('uptime_process', '—')}",
                "",
                *db_lines,
            ]
        )
        if len(text) > 4096:
            text = text[:4080] + "\n…"
        return text

    async def _check_databases(self) -> list[str]:
        main_ok, main_err = await asyncio.to_thread(_main_db_sync)
        stats_ok, stats_err = await asyncio.to_thread(_probe_sqlite, statistics_db_path())
        return [
            f"БД диалогов (`{DB_PATH.name}`): "
            + ("OK" if main_ok else f"ошибка: {main_err or 'unknown'}"),
            f"БД статистики (`{statistics_db_path().name}`): "
            + ("OK" if stats_ok else f"ошибка: {stats_err or 'unknown'}"),
        ]

    async def main_db_ok(self) -> bool:
        ok, _ = await asyncio.to_thread(_main_db_sync)
        return ok

    def functional_check(self) -> tuple[dict[str, dict[str, Any]], str]:
        tester = BotSelfTest(config=self._config, agent=self._agent)
        results = tester.run_all()
        return results, tester.format_report(results)

    async def full_check(self) -> str:
        tech = await self.technical_check()
        _results, func_report = self.functional_check()
        sep = "\n\n──────────\n\n"
        text = tech + sep + "Функциональная диагностика\n\n" + func_report
        if len(text) > 4096:
            text = text[:4080] + "\n…"
        return text

    async def _safe_send_alert(
        self,
        bot: Bot,
        notify_chat_id: int | None,
        text: str,
    ) -> None:
        if notify_chat_id is None:
            logger.warning("self_diagnostics: алерт без получателя (только лог): %s", text[:500])
            return
        try:
            t = text if len(text) <= 4096 else text[:4080] + "\n…"
            await bot.send_message(notify_chat_id, t)
        except Exception as exc:
            logger.warning("self_diagnostics: не удалось отправить алерт: %s", exc)

    async def auto_diagnostics_loop(
        self,
        bot: Bot,
        notify_chat_id: int | None,
        interval: float,
    ) -> None:
        """Фон: раз в interval сек — проверки, алерты, критический рестарт при сбоях БД или Telegram."""
        state = {"get_me_fail_streak": 0}
        logger.info(
            "self_diagnostics: auto loop started interval=%ss notify_chat_id=%s dispatcher=%s",
            interval,
            notify_chat_id,
            self._dispatcher is not None,
        )
        try:
            while True:
                self._cycle_count += 1
                n = self._cycle_count
                logger.info(
                    "auto_diagnostics: cycle #%s — entering iteration (last_update_ts=%.3f)",
                    n,
                    self._last_update_ts,
                )
                logger.info(
                    "auto_diagnostics: cycle #%s — before asyncio.sleep(%ss)",
                    n,
                    interval,
                )
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise

                try:
                    logger.info(
                        "auto_diagnostics: cycle #%s — after sleep, running checks",
                        n,
                    )
                    idle = time.time() - self._last_update_ts
                    if idle >= POLL_STUCK_IDLE_SEC and self._dispatcher is not None:
                        if not self._poll_restart_done_for_idle_streak:
                            self._poll_restart_done_for_idle_streak = True
                            warn = (
                                "⚠️ Polling завис 90+ мин. Перезапускаю..."
                            )
                            logger.warning(
                                "auto_diagnostics: idle=%.0fs >= %ss — %s",
                                idle,
                                POLL_STUCK_IDLE_SEC,
                                warn,
                            )
                            await self._safe_send_alert(bot, notify_chat_id, warn)
                            self._pending_poll_restart = True
                            try:
                                await self._dispatcher.stop_polling()
                            except Exception as exc:
                                self._pending_poll_restart = False
                                self._poll_restart_done_for_idle_streak = False
                                logger.exception(
                                    "auto_diagnostics: stop_polling после зависания: %s",
                                    exc,
                                )
                                await self._safe_send_alert(
                                    bot,
                                    notify_chat_id,
                                    f"❌ Не удалось остановить polling для перезапуска: {exc}",
                                )

                    logger.info(
                        "auto_diagnostics: cycle #%s — heartbeat section done "
                        "(idle_sec=%.1f last_update_ts=%.3f poll_restart_streak=%s)",
                        n,
                        idle,
                        self._last_update_ts,
                        self._poll_restart_done_for_idle_streak,
                    )

                    db_ok = await self.main_db_ok()
                    logger.info(
                        "auto_diagnostics: technical check completed, db_ok=%s",
                        db_ok,
                    )

                    if self._auto_run_functional_check:
                        await asyncio.to_thread(self.functional_check)
                        logger.info("auto_diagnostics: functional check completed")

                    if not db_ok:
                        logger.info(
                            "auto_diagnostics: основная БД не отвечает — рестарт процесса"
                        )
                        await self._safe_send_alert(
                            bot,
                            notify_chat_id,
                            "🚨 Критично: основная БД не отвечает. Перезапускаю процесс.",
                        )
                        await self._restart_process(bot)
                        return

                    try:
                        await asyncio.wait_for(bot.get_me(), timeout=GET_ME_TIMEOUT_SEC)
                        state["get_me_fail_streak"] = 0
                    except Exception as exc:
                        state["get_me_fail_streak"] = state["get_me_fail_streak"] + 1
                        fs = state["get_me_fail_streak"]
                        logger.error(
                            "self_diagnostics: get_me не удался (%s/%s): %s",
                            fs,
                            GET_ME_FAIL_THRESHOLD,
                            exc,
                        )
                        if fs >= GET_ME_FAIL_THRESHOLD:
                            logger.info(
                                "auto_diagnostics: %s подряд провалов get_me — рестарт",
                                GET_ME_FAIL_THRESHOLD,
                            )
                            await self._safe_send_alert(
                                bot,
                                notify_chat_id,
                                f"🚨 Критично: {GET_ME_FAIL_THRESHOLD} подряд провала get_me(). Перезапускаю процесс.",
                            )
                            await self._restart_process(bot)
                            return
                    logger.debug(
                        "auto_diagnostics: cycle #%s finished, next sleep in %ss",
                        n,
                        interval,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "auto_diagnostics: cycle #%s — error in check body, continuing loop",
                        n,
                    )
        except asyncio.CancelledError:
            logger.info("self_diagnostics: auto loop cancelled")
            raise


class DiagnosticsActivityMiddleware(BaseMiddleware):
    """Обновляет heartbeat для авто-диагностики на каждый входящий апдейт."""

    def __init__(self, sd: SelfDiagnostics) -> None:
        self._sd = sd

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        self._sd.mark_user_activity()
        logger.debug(
            "diagnostics_heartbeat: touch event=%s last_update_ts=%.3f",
            type(event).__name__,
            self._sd._last_update_ts,
        )
        return await handler(event, data)


def install_diagnostics_heartbeat_middleware(dp: Dispatcher, sd: SelfDiagnostics) -> None:
    """Регистрирует heartbeat на конкретные типы апдейтов (outer_middleware).

    Только `dp.update.middleware` ненадёжен: цепочка update.trigger до внутренних
    middleware может не совпасть с фактической обработкой. Сообщения и колбэки
    всегда проходят через соответствующие observers.
    """
    mw = DiagnosticsActivityMiddleware(sd)
    for observer in (
        dp.message,
        dp.edited_message,
        dp.channel_post,
        dp.edited_channel_post,
        dp.callback_query,
        dp.my_chat_member,
    ):
        observer.outer_middleware(mw)
    logger.info(
        "diagnostics_heartbeat: registered outer_middleware on "
        "message/edited_message/channel_post/edited_channel_post/callback_query/my_chat_member"
    )
