import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import app.statistics as statistics

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

NO_ADMIN_RIGHTS = "❌ У вас нет прав для этой команды"


def _stats_db_path() -> Path:
    return statistics._path()


def init_admin_db() -> None:
    """Таблица банов в той же БД, что и статистика (user_stats)."""
    try:
        with sqlite3.connect(_stats_db_path()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id INTEGER PRIMARY KEY,
                    banned_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()
    except Exception as exc:
        logger.exception("Админка: ошибка init_admin_db: %s", exc)


def is_admin(user_id: int) -> bool:
    from app.config import load_config

    cfg = load_config()
    return cfg.admin_id is not None and user_id == cfg.admin_id


def get_all_users() -> list[int]:
    try:
        with sqlite3.connect(_stats_db_path()) as conn:
            rows = conn.execute(
                "SELECT user_id FROM user_stats ORDER BY last_seen DESC"
            ).fetchall()
        return [int(r[0]) for r in rows]
    except Exception as exc:
        logger.exception("Админка: get_all_users: %s", exc)
        return []


def get_users_preview(limit: int = 50) -> list[dict[str, object]]:
    """user_id, username, message_count, banned."""
    try:
        with sqlite3.connect(_stats_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT u.user_id, u.username, u.message_count,
                    EXISTS (
                        SELECT 1 FROM banned_users b WHERE b.user_id = u.user_id
                    ) AS banned
                FROM user_stats u
                ORDER BY u.last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "user_id": int(r["user_id"]),
                "username": r["username"],
                "message_count": int(r["message_count"]),
                "banned": bool(r["banned"]),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.exception("Админка: get_users_preview: %s", exc)
        return []


def is_banned(user_id: int) -> bool:
    try:
        with sqlite3.connect(_stats_db_path()) as conn:
            row = conn.execute(
                "SELECT 1 FROM banned_users WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
        return row is not None
    except Exception as exc:
        logger.exception("Админка: is_banned: %s", exc)
        return False


def ban_user(user_id: int) -> bool:
    try:
        with sqlite3.connect(_stats_db_path()) as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO banned_users (user_id, banned_at)
                VALUES (?, datetime('now'))
                """,
                (user_id,),
            )
            conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        logger.exception("Админка: ban_user: %s", exc)
        return False


def unban_user(user_id: int) -> bool:
    try:
        with sqlite3.connect(_stats_db_path()) as conn:
            cur = conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
            conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        logger.exception("Админка: unban_user: %s", exc)
        return False


async def broadcast_message(bot: "Bot", text: str) -> int:
    """Рассылка всем из user_stats; забаненные пропускаются. Возвращает число успешных send_message."""
    users = get_all_users()
    if not text.strip():
        return 0
    ok = 0
    for uid in users:
        if is_banned(uid):
            continue
        try:
            await bot.send_message(chat_id=uid, text=text)
            ok += 1
        except Exception:
            logger.exception("Админка: broadcast не доставлено user_id=%s", uid)
    return ok
