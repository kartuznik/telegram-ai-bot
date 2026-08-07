import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "bot_statistics.db"
_db_path: Path | None = None


def _path() -> Path:
    if _db_path is not None:
        return _db_path
    return _DEFAULT_DB


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_stats_db(db_path: str | Path | None = None) -> None:
    """Создаёт таблицы в SQLite (по умолчанию bot_statistics.db в корне проекта)."""
    global _db_path
    if db_path is not None:
        _db_path = Path(db_path)
    try:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS daily_stats (
                    stat_date TEXT PRIMARY KEY,
                    messages INTEGER NOT NULL DEFAULT 0,
                    unique_users INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS daily_active_users (
                    stat_date TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (stat_date, user_id)
                );
                CREATE TABLE IF NOT EXISTS channel_quality (
                    channel_username TEXT PRIMARY KEY,
                    approved_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            conn.commit()
    except Exception as exc:
        logger.exception("Статистика: ошибка инициализации БД: %s", exc)


def log_user_message(user_id: int, username: str | None = None) -> None:
    """Учитывает входящее сообщение пользователя (user_stats + дневная агрегация)."""
    today = date.today().isoformat()
    uname = (username or "").strip() or None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT message_count FROM user_stats WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            prev = int(row["message_count"]) if row else 0

            conn.execute(
                """
                INSERT INTO user_stats (user_id, username, first_seen, last_seen, message_count)
                VALUES (?, ?, datetime('now'), datetime('now'), 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen = datetime('now'),
                    message_count = user_stats.message_count + 1,
                    username = CASE
                        WHEN excluded.username IS NOT NULL AND excluded.username != ''
                        THEN excluded.username
                        ELSE user_stats.username
                    END
                """,
                (user_id, uname),
            )

            conn.execute(
                "INSERT OR IGNORE INTO daily_stats (stat_date, messages, unique_users) VALUES (?, 0, 0)",
                (today,),
            )
            conn.execute(
                "UPDATE daily_stats SET messages = messages + 1 WHERE stat_date = ?",
                (today,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO daily_active_users (stat_date, user_id) VALUES (?, ?)",
                (today, user_id),
            )
            conn.execute(
                """
                UPDATE daily_stats SET unique_users = (
                    SELECT COUNT(*) FROM daily_active_users WHERE stat_date = ?
                ) WHERE stat_date = ?
                """,
                (today, today),
            )
            conn.commit()

            new_count = prev + 1
            logger.info("Статистика: пользователь %s, сообщение #%s", user_id, new_count)
    except Exception as exc:
        logger.exception("Статистика: ошибка записи (user_id=%s): %s", user_id, exc)


def get_stats() -> dict[str, Any]:
    """Сводка: всего пользователей/сообщений и за сегодня."""
    today = date.today().isoformat()
    empty = {
        "total_users": 0,
        "total_messages": 0,
        "today_users": 0,
        "today_messages": 0,
    }
    try:
        with _connect() as conn:
            row_u = conn.execute("SELECT COUNT(*) AS c FROM user_stats").fetchone()
            row_m = conn.execute(
                "SELECT COALESCE(SUM(message_count), 0) AS s FROM user_stats"
            ).fetchone()
            row_d = conn.execute(
                "SELECT messages, unique_users FROM daily_stats WHERE stat_date = ?",
                (today,),
            ).fetchone()
        total_users = int(row_u["c"]) if row_u else 0
        total_messages = int(row_m["s"]) if row_m else 0
        today_messages = int(row_d["messages"]) if row_d else 0
        today_users = int(row_d["unique_users"]) if row_d else 0
        return {
            "total_users": total_users,
            "total_messages": total_messages,
            "today_users": today_users,
            "today_messages": today_messages,
        }
    except Exception as exc:
        logger.exception("Статистика: ошибка чтения get_stats: %s", exc)
        return empty


def get_top_users(limit: int = 5) -> list[dict[str, Any]]:
    """Топ пользователей по числу сообщений."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, username, message_count, last_seen
                FROM user_stats
                ORDER BY message_count DESC, last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "user_id": int(r["user_id"]),
                "username": r["username"],
                "message_count": int(r["message_count"]),
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.exception("Статистика: ошибка get_top_users: %s", exc)
        return []


def get_daily_breakdown(days: int = 7) -> list[dict[str, Any]]:
    """Последние `days` дней: сообщения и уникальные пользователи по дням."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT stat_date, messages, unique_users
                FROM daily_stats
                ORDER BY stat_date DESC
                LIMIT ?
                """,
                (days,),
            ).fetchall()
        return [
            {
                "date": r["stat_date"],
                "messages": int(r["messages"]),
                "unique_users": int(r["unique_users"]),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.exception("Статистика: ошибка get_daily_breakdown: %s", exc)
        return []


def _norm_channel_username(channel_username: str) -> str:
    return (channel_username or "").strip().lower().lstrip("@")


def bump_channel_quality(
    channel_username: str,
    *,
    approved_inc: int = 0,
    rejected_inc: int = 0,
) -> None:
    uname = _norm_channel_username(channel_username)
    if not uname:
        return
    ai = max(0, int(approved_inc))
    ri = max(0, int(rejected_inc))
    if ai == 0 and ri == 0:
        return
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_quality (channel_username, approved_count, rejected_count, last_updated)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(channel_username) DO UPDATE SET
                    approved_count = channel_quality.approved_count + excluded.approved_count,
                    rejected_count = channel_quality.rejected_count + excluded.rejected_count,
                    last_updated = datetime('now')
                """,
                (uname, ai, ri),
            )
            conn.commit()
    except Exception as exc:
        logger.exception("Статистика: bump_channel_quality(%s): %s", uname, exc)


def get_channel_quality_snapshot() -> dict[str, tuple[int, int]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT channel_username, approved_count, rejected_count
                FROM channel_quality
                """
            ).fetchall()
        return {
            str(r["channel_username"]): (int(r["approved_count"]), int(r["rejected_count"]))
            for r in rows
        }
    except Exception as exc:
        logger.exception("Статистика: get_channel_quality_snapshot: %s", exc)
        return {}


def get_channel_quality_top_bottom(
    *,
    top_limit: int = 5,
    bottom_limit: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT channel_username, approved_count, rejected_count
                FROM channel_quality
                WHERE approved_count > 0 OR rejected_count > 0
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            approved = int(r["approved_count"])
            rejected = int(r["rejected_count"])
            ratio = approved / max(1, rejected)
            items.append(
                {
                    "channel_username": str(r["channel_username"]),
                    "approved_count": approved,
                    "rejected_count": rejected,
                    "ratio": ratio,
                }
            )
        top_n = max(1, top_limit)
        bot_n = max(1, bottom_limit)
        top = sorted(
            items,
            key=lambda x: (x["ratio"], x["approved_count"], x["rejected_count"]),
            reverse=True,
        )[:top_n]
        top_names = {x["channel_username"] for x in top}
        rest = [x for x in items if x["channel_username"] not in top_names]
        bottom = sorted(
            rest,
            key=lambda x: (x["ratio"], -x["rejected_count"], -x["approved_count"]),
        )[:bot_n]
        return top, bottom
    except Exception as exc:
        logger.exception("Статистика: get_channel_quality_top_bottom: %s", exc)
        return [], []
