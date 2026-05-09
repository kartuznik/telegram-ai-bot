from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().parent.parent / "bot_database.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    try:
        with get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT NOT NULL,
                    pref_key TEXT NOT NULL,
                    pref_value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, pref_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id ON user_preferences(user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_templates_user_id ON user_templates(user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_anchors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    context_snippet TEXT NOT NULL,
                    message_ref INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_anchors_user_id "
                "ON conversation_anchors(user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS draft_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_url TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    media_url TEXT,
                    expires_at TIMESTAMP,
                    was_edited INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Миграции draft_posts: SQLite не поддерживает «ADD COLUMN IF NOT EXISTS» —
            # колонки добавляем только если их нет в PRAGMA table_info (идемпотентно).
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(draft_posts)").fetchall()
            }
            if "expires_at" not in cols:
                conn.execute("ALTER TABLE draft_posts ADD COLUMN expires_at TIMESTAMP")
            if "was_edited" not in cols:
                conn.execute(
                    "ALTER TABLE draft_posts ADD COLUMN was_edited INTEGER NOT NULL DEFAULT 0"
                )
            # Редакторские метрики (Кузьма / content_editor):
            # - confidence_score: 0–100, уверенность фактчека после перекрёстных источников + LLM;
            # - requires_verification: 1 = нужна ручная проверка (<2 доменов, противоречия, слабый JSON и т.д.);
            # - seo_score: 0–100 (REAL), оценка SEO первой строки черновика (длина + ключевые слова тем).
            if "confidence_score" not in cols:
                conn.execute(
                    "ALTER TABLE draft_posts ADD COLUMN confidence_score INTEGER"
                )
            if "requires_verification" not in cols:
                conn.execute(
                    "ALTER TABLE draft_posts ADD COLUMN requires_verification INTEGER NOT NULL DEFAULT 0"
                )
            if "seo_score" not in cols:
                conn.execute("ALTER TABLE draft_posts ADD COLUMN seo_score REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_posts_user_status "
                "ON draft_posts(user_id, status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS draft_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    draft_id INTEGER,
                    action TEXT NOT NULL,
                    draft_preview TEXT,
                    feedback_text TEXT,
                    category TEXT,
                    quality_score INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            fb_cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(draft_feedback)").fetchall()
            }
            if "category" not in fb_cols:
                conn.execute("ALTER TABLE draft_feedback ADD COLUMN category TEXT")
            if "quality_score" not in fb_cols:
                conn.execute("ALTER TABLE draft_feedback ADD COLUMN quality_score INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_feedback_user_id "
                "ON draft_feedback(user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS editorial_rules (
                    user_id TEXT PRIMARY KEY,
                    rules_text TEXT,
                    feedbacks_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_training (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    rewritten_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_voice_training_owner_created "
                "ON voice_training(owner_id, created_at DESC, id DESC)"
            )
            conn.commit()
    except Exception as exc:
        logger.exception("SQLite ошибка: %s", exc)


def _uid_str(user_id: int | str) -> str:
    return str(int(user_id))


def save_draft_feedback(
    user_id: int | str,
    draft_id: int | None,
    action: str,
    draft_preview: str,
    feedback_text: str,
    category: str | None = None,
    quality_score: int | None = None,
) -> int | None:
    """Сохраняет ответ пользователя на вопрос после решения по черновику. Возвращает id строки или None."""
    uid = _uid_str(user_id)
    act = (action or "").strip().lower()
    if act not in ("approved", "rejected", "edited", "expired_content"):
        logger.warning("save_draft_feedback: неизвестный action=%r user_id=%s", action, uid)
        return None
    prev = (draft_preview or "")[:100]
    text = feedback_text or ""
    cat = (category or "").strip().lower()[:64] or None
    q_score: int | None = None
    if quality_score is not None:
        try:
            q_score = max(1, min(10, int(quality_score)))
        except (TypeError, ValueError):
            q_score = None
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO draft_feedback (
                    user_id, draft_id, action, draft_preview, feedback_text, category, quality_score
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, draft_id, act, prev, text, cat, q_score),
            )
            conn.commit()
            return int(cur.lastrowid)
    except Exception as exc:
        logger.exception("save_draft_feedback: %s", exc)
        return None


def get_recent_expired_urls(user_id: int | str, hours: int = 24) -> set[str]:
    """
    source_url черновиков, по которым за последние `hours` часов записан фидбек action=expired_content.
    Используется, чтобы не предлагать тот же устаревший URL повторно в одной «сессии» подбора.
    """
    uid = _uid_str(user_id)
    h = max(1, min(24 * 14, int(hours)))
    mod = f"-{h} hours"
    out: set[str] = set()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT TRIM(dp.source_url) AS u
                FROM draft_feedback df
                INNER JOIN draft_posts dp ON dp.id = df.draft_id AND dp.user_id = df.user_id
                WHERE df.user_id = ?
                  AND LOWER(TRIM(df.action)) = 'expired_content'
                  AND df.draft_id IS NOT NULL
                  AND datetime(df.created_at) > datetime('now', ?)
                  AND TRIM(COALESCE(dp.source_url, '')) != ''
                """,
                (uid, mod),
            ).fetchall()
        for r in rows:
            u = str(r["u"] or "").strip()
            if u:
                out.add(u)
    except Exception as exc:
        logger.exception("get_recent_expired_urls: %s", exc)
    return out


def get_draft_feedback_rejected_approved_counts(user_id: int | str) -> tuple[int, int]:
    """
    Сколько раз пользователь оставил фидбек после явного отказа (rejected) и после апрува (approved).
    «Устарело» (expired_content), правки и прочие action сюда не входят — для расчёта reject_spree.
    """
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN LOWER(TRIM(action)) = 'rejected' THEN 1 ELSE 0 END), 0) AS r,
                    COALESCE(SUM(CASE WHEN LOWER(TRIM(action)) = 'approved' THEN 1 ELSE 0 END), 0) AS a
                FROM draft_feedback
                WHERE user_id = ?
                """,
                (uid,),
            ).fetchone()
        if not row:
            return 0, 0
        return int(row["r"] or 0), int(row["a"] or 0)
    except Exception as exc:
        logger.exception("get_draft_feedback_rejected_approved_counts: %s", exc)
        return 0, 0


def get_pending_feedbacks(user_id: int | str, since_last_count: int) -> list[dict[str, Any]]:
    """
    Последние фидбеки пользователя (новые сверху), с OFFSET since_last_count.
    До 20 строк — удобно для дистилляции правил.
    """
    uid = _uid_str(user_id)
    off = max(0, int(since_last_count))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, draft_id, action, draft_preview, feedback_text, created_at
                FROM draft_feedback
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 20 OFFSET ?
                """,
                (uid, off),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("get_pending_feedbacks: %s", exc)
        return []


def get_editorial_rules(user_id: int | str) -> str | None:
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT rules_text FROM editorial_rules WHERE user_id=?",
                (uid,),
            ).fetchone()
        if not row or row["rules_text"] is None:
            return None
        t = str(row["rules_text"]).strip()
        return t if t else None
    except Exception as exc:
        logger.exception("get_editorial_rules: %s", exc)
        return None


def save_editorial_rules(
    user_id: int | str,
    rules_text: str,
    feedbacks_count: int,
) -> bool:
    uid = _uid_str(user_id)
    try:
        fc = max(0, int(feedbacks_count))
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO editorial_rules (user_id, rules_text, feedbacks_count, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    rules_text = excluded.rules_text,
                    feedbacks_count = excluded.feedbacks_count,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (uid, rules_text or "", fc),
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.exception("save_editorial_rules: %s", exc)
        return False


def get_feedbacks_count(user_id: int | str) -> int:
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM draft_feedback WHERE user_id=?",
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("get_feedbacks_count: %s", exc)
        return 0


def get_editorial_feedbacks_baseline(user_id: int | str) -> int:
    """Сколько записей draft_feedback уже учтено в последней дистилляции (0, если строки нет)."""
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT feedbacks_count FROM editorial_rules WHERE user_id=?",
                (uid,),
            ).fetchone()
        return int(row["feedbacks_count"]) if row else 0
    except Exception as exc:
        logger.exception("get_editorial_feedbacks_baseline: %s", exc)
        return 0


def get_draft_feedback_slice(
    user_id: int | str,
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Срез фидбеков в порядке id ASC (для пакетной дистилляции)."""
    uid = _uid_str(user_id)
    off = max(0, int(offset))
    lim = max(1, min(int(limit), 50))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, draft_id, action, draft_preview, feedback_text, created_at
                FROM draft_feedback
                WHERE user_id=?
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                (uid, lim, off),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("get_draft_feedback_slice: %s", exc)
        return []


def get_feedback_category_counts_in_window(
    user_id: int | str,
    limit: int,
) -> dict[str, int]:
    """Сколько записей draft_feedback по каждой категории в последних `limit` строках (id DESC)."""
    uid = _uid_str(user_id)
    lim = max(1, min(int(limit), 200))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT category
                FROM draft_feedback
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
    except Exception as exc:
        logger.exception("get_feedback_category_counts_in_window: %s", exc)
        return {}
    out: dict[str, int] = {}
    for r in rows:
        cat = (str(r["category"] or "").strip().lower() or "other")[:64]
        out[cat] = out.get(cat, 0) + 1
    return out


def get_recent_draft_feedback_window(
    user_id: int | str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Последние N записей draft_feedback (newest first) с category/quality_score."""
    uid = _uid_str(user_id)
    lim = max(1, min(int(limit), 200))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, action, category, quality_score, created_at
                FROM draft_feedback
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("get_recent_draft_feedback_window: %s", exc)
        return []


def save_voice_training(
    owner_id: int | str,
    original_text: str,
    rewritten_text: str,
) -> int | None:
    uid = _uid_str(owner_id)
    src = (original_text or "").strip()
    dst = (rewritten_text or "").strip()
    if not src or not dst:
        return None
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO voice_training (owner_id, original_text, rewritten_text)
                VALUES (?, ?, ?)
                """,
                (uid, src, dst),
            )
            conn.commit()
            return int(cur.lastrowid)
    except Exception as exc:
        logger.exception("save_voice_training: %s", exc)
        return None


def get_voice_training_examples(owner_id: int | str, limit: int = 5) -> list[str]:
    uid = _uid_str(owner_id)
    lim = max(1, min(int(limit), 20))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT rewritten_text
                FROM voice_training
                WHERE owner_id=?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
        out: list[str] = []
        for r in rows:
            t = str(r["rewritten_text"] or "").strip()
            if t:
                out.append(t)
        return out
    except Exception as exc:
        logger.exception("get_voice_training_examples: %s", exc)
        return []


def count_voice_training_examples(owner_id: int | str) -> int:
    uid = _uid_str(owner_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM voice_training
                WHERE owner_id=?
                """,
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_voice_training_examples: %s", exc)
        return 0
