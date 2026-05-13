from __future__ import annotations

import json
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
            if "reason" not in fb_cols:
                conn.execute("ALTER TABLE draft_feedback ADD COLUMN reason TEXT")
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
            vt_cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(voice_training)").fetchall()
            }
            if "is_negative" not in vt_cols:
                conn.execute(
                    "ALTER TABLE voice_training ADD COLUMN is_negative INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_rejection_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    pattern_type TEXT NOT NULL,
                    pattern_description TEXT NOT NULL,
                    pattern_keywords TEXT,
                    requirements TEXT,
                    problems TEXT,
                    category TEXT,
                    count INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_rejection_patterns_user "
                "ON user_rejection_patterns(user_id)"
            )
            conn.commit()
    except Exception as exc:
        logger.exception("SQLite ошибка: %s", exc)


def _uid_str(user_id: int | str) -> str:
    return str(int(user_id))


_REJECT_REASON_ALLOWED = frozenset(
    {
        "not_interested",
        "weak_content",
        "bad_source",
        "promotional",
    }
)


def normalize_draft_feedback_reason(reason: str | None) -> str | None:
    """Допустимые структурированные причины для action=rejected (ограничение на уровне приложения)."""
    if reason is None:
        return None
    r = (reason or "").strip().lower()
    if not r or r == "skip":
        return None
    if r in _REJECT_REASON_ALLOWED:
        return r
    return None


def save_draft_feedback(
    user_id: int | str,
    draft_id: int | None,
    action: str,
    draft_preview: str,
    feedback_text: str,
    category: str | None = None,
    quality_score: int | None = None,
    reason: str | None = None,
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
    reason_db: str | None = None
    if act == "rejected":
        reason_db = normalize_draft_feedback_reason(reason)
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO draft_feedback (
                    user_id, draft_id, action, draft_preview, feedback_text, category, quality_score, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, draft_id, act, prev, text, cat, q_score, reason_db),
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
                SELECT id, user_id, draft_id, action, draft_preview, feedback_text,
                       category, quality_score, reason, created_at
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


def count_draft_feedback_for_draft(user_id: int | str, draft_id: int) -> int:
    """Сколько строк фидбека уже есть по draft_id (защита от повторного нажатия кнопок)."""
    uid = _uid_str(user_id)
    did = int(draft_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM draft_feedback
                WHERE user_id=? AND draft_id=?
                """,
                (uid, did),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_draft_feedback_for_draft: %s", exc)
        return 0


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


def get_feedback_rejected_category_counts_in_window(
    user_id: int | str,
    limit: int,
) -> dict[str, int]:
    """
    В последних `limit` строках draft_feedback (любой action, порядок id DESC):
    сколько из них с action=rejected по каждой категории.

    Нужно, чтобы отличать явные отказы по категории от «случайного» отрицательного
    preference без rejected в окне (например устарело / низкая оценка апрува).
    """
    uid = _uid_str(user_id)
    lim = max(1, min(int(limit), 200))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT action, category, reason
                FROM draft_feedback
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
    except Exception as exc:
        logger.exception("get_feedback_rejected_category_counts_in_window: %s", exc)
        return {}
    out: dict[str, int] = {}
    for r in rows:
        act = str(r["action"] or "").strip().lower()
        if act != "rejected":
            continue
        cat = (str(r["category"] or "").strip().lower() or "other")[:64]
        out[cat] = out.get(cat, 0) + 1
    return out


_SOFT_REJECT_REASONS = frozenset({"weak_content", "bad_source"})


def get_feedback_hard_reject_category_counts_in_window(
    user_id: int | str,
    limit: int,
) -> dict[str, int]:
    """
    Как get_feedback_rejected_category_counts_in_window, но без «мягких» причин:
    weak_content / bad_source не считаются (дело не в теме категории).

    Для temporary_topic: сохранять штраф по категории только если есть такой «жёсткий» отказ.
    """
    uid = _uid_str(user_id)
    lim = max(1, min(int(limit), 200))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT action, category, reason
                FROM draft_feedback
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
    except Exception as exc:
        logger.exception("get_feedback_hard_reject_category_counts_in_window: %s", exc)
        return {}
    out: dict[str, int] = {}
    for r in rows:
        act = str(r["action"] or "").strip().lower()
        if act != "rejected":
            continue
        reason = (str(r["reason"] or "").strip().lower())
        if reason in _SOFT_REJECT_REASONS:
            continue
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
                SELECT id, action, category, quality_score, reason, created_at
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
    *,
    is_negative: bool = False,
) -> int | None:
    uid = _uid_str(owner_id)
    src = (original_text or "").strip()
    dst = (rewritten_text or "").strip()
    if not src or not dst:
        return None
    neg = 1 if is_negative else 0
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO voice_training (owner_id, original_text, rewritten_text, is_negative)
                VALUES (?, ?, ?, ?)
                """,
                (uid, src, dst, neg),
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
                WHERE owner_id=? AND COALESCE(is_negative, 0) = 0
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


def get_voice_negative_training_examples(owner_id: int | str, limit: int = 3) -> list[str]:
    """Фрагменты «как не надо» (is_negative=1), поле rewritten_text — инструкция + контекст."""
    uid = _uid_str(owner_id)
    lim = max(1, min(int(limit), 10))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT rewritten_text
                FROM voice_training
                WHERE owner_id=? AND COALESCE(is_negative, 0) = 1
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
        return [str(r["rewritten_text"] or "").strip() for r in rows if str(r["rewritten_text"] or "").strip()]
    except Exception as exc:
        logger.exception("get_voice_negative_training_examples: %s", exc)
        return []


def count_voice_training_examples(owner_id: int | str) -> int:
    uid = _uid_str(owner_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM voice_training
                WHERE owner_id=? AND COALESCE(is_negative, 0) = 0
                """,
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_voice_training_examples: %s", exc)
        return 0


def count_voice_negative_training_examples(owner_id: int | str) -> int:
    uid = _uid_str(owner_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM voice_training
                WHERE owner_id=? AND COALESCE(is_negative, 0) = 1
                """,
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_voice_negative_training_examples: %s", exc)
        return 0


def get_recent_voice_training_pairs(
    owner_id: int | str,
    limit: int = 24,
) -> list[tuple[str, str]]:
    """Последние позитивные пары (оригинал → переписанный) для эвристик предпочтений."""
    uid = _uid_str(owner_id)
    lim = max(1, min(int(limit), 60))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT original_text, rewritten_text
                FROM voice_training
                WHERE owner_id=? AND COALESCE(is_negative, 0) = 0
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
        out: list[tuple[str, str]] = []
        for r in rows:
            o = str(r["original_text"] or "").strip()
            w = str(r["rewritten_text"] or "").strip()
            if o and w:
                out.append((o, w))
        return out
    except Exception as exc:
        logger.exception("get_recent_voice_training_pairs: %s", exc)
        return []


def _json_dumps_safe(obj: Any) -> str | None:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def save_rejection_pattern(
    user_id: int | str,
    data: dict[str, Any],
    *,
    category: str | None = None,
) -> int | None:
    """
    Сохранить / усилить паттерн отказа по данным GPT (problems, pattern_description, …).
    При совпадении pattern_type + pattern_description увеличивает count.
    """
    uid = _uid_str(user_id)
    desc = (str(data.get("pattern_description") or "")).strip()[:2000]
    if not desc:
        logger.warning("save_rejection_pattern: пустое pattern_description user_id=%s", uid)
        return None
    ptype = (str(data.get("pattern_type") or "unknown")).strip().lower()[:64] or "unknown"
    problems = data.get("problems")
    if not isinstance(problems, list):
        problems = []
    req = data.get("requirements")
    if not isinstance(req, list):
        req = []
    kw = data.get("keywords_to_avoid")
    if kw is None:
        kw = data.get("pattern_keywords")
    if not isinstance(kw, list):
        kw = []
    cat = (category or "").strip().lower()[:64] or None
    pj = _json_dumps_safe(problems)
    rj = _json_dumps_safe(req)
    kj = _json_dumps_safe(kw)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id FROM user_rejection_patterns
                WHERE user_id=? AND pattern_type=? AND lower(trim(pattern_description))=lower(trim(?))
                LIMIT 1
                """,
                (uid, ptype, desc),
            ).fetchone()
            if row:
                pid = int(row["id"])
                conn.execute(
                    """
                    UPDATE user_rejection_patterns
                    SET count = count + 1,
                        last_used_at = CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (pid,),
                )
                conn.commit()
                return pid
            cur = conn.execute(
                """
                INSERT INTO user_rejection_patterns (
                    user_id, pattern_type, pattern_description,
                    pattern_keywords, requirements, problems, category, count, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                """,
                (uid, ptype, desc, kj, rj, pj, cat),
            )
            conn.commit()
            return int(cur.lastrowid)
    except Exception as exc:
        logger.exception("save_rejection_pattern: %s", exc)
        return None


def get_active_rejection_patterns(
    user_id: int | str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Активные паттерны: приоритет count × свежесть (новые сильнее)."""
    uid = _uid_str(user_id)
    lim = max(1, min(int(limit), 24))
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, pattern_type, pattern_description,
                       pattern_keywords, requirements, problems, category, count,
                       created_at, last_used_at,
                       (count * (
                           CASE
                               WHEN (julianday('now') - julianday(datetime(COALESCE(last_used_at, created_at)))) <= 1
                                   THEN 2.0
                               WHEN (julianday('now') - julianday(datetime(COALESCE(last_used_at, created_at)))) <= 7
                                   THEN 1.0
                               ELSE 0.5
                           END
                       )) AS pattern_weight
                FROM user_rejection_patterns
                WHERE user_id=?
                ORDER BY pattern_weight DESC,
                         datetime(COALESCE(last_used_at, created_at)) DESC,
                         id DESC
                LIMIT ?
                """,
                (uid, lim),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("get_active_rejection_patterns: %s", exc)
        return []


def get_rejection_pattern_by_id(pattern_id: int) -> dict[str, Any] | None:
    try:
        pid = int(pattern_id)
    except (TypeError, ValueError):
        return None
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, pattern_type, pattern_description,
                       pattern_keywords, requirements, problems, category, count,
                       created_at, last_used_at
                FROM user_rejection_patterns
                WHERE id=?
                """,
                (pid,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.exception("get_rejection_pattern_by_id: %s", exc)
        return None


def list_all_rejection_patterns(user_id: int | str) -> list[dict[str, Any]]:
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, pattern_type, pattern_description,
                       pattern_keywords, requirements, problems, category, count,
                       created_at, last_used_at
                FROM user_rejection_patterns
                WHERE user_id=?
                ORDER BY id ASC
                """,
                (uid,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("list_all_rejection_patterns: %s", exc)
        return []


def list_all_voice_training_rows(owner_id: int | str) -> list[dict[str, Any]]:
    uid = _uid_str(owner_id)
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, owner_id, original_text, rewritten_text,
                       COALESCE(is_negative, 0) AS is_negative, created_at
                FROM voice_training
                WHERE owner_id=?
                ORDER BY id ASC
                """,
                (uid,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("list_all_voice_training_rows: %s", exc)
        return []


def export_style_profile_json(user_id: int | str) -> dict[str, Any]:
    uid = _uid_str(user_id)
    return {
        "version": 1,
        "user_id": uid,
        "voice_training": list_all_voice_training_rows(uid),
        "rejection_patterns": list_all_rejection_patterns(uid),
    }


def import_style_profile_json(user_id: int | str, data: dict[str, Any]) -> tuple[bool, str]:
    """Заменяет паттерны отказов и дополняет voice_training (без удаления старых позитивных)."""
    uid = _uid_str(user_id)
    if not isinstance(data, dict):
        return False, "Некорректный JSON"
    if int(data.get("version") or 0) != 1:
        return False, "Поддерживается только version=1"
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM user_rejection_patterns WHERE user_id=?", (uid,))
            for p in data.get("rejection_patterns") or []:
                if not isinstance(p, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO user_rejection_patterns (
                        user_id, pattern_type, pattern_description,
                        pattern_keywords, requirements, problems, category, count, last_used_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                    """,
                    (
                        uid,
                        str(p.get("pattern_type") or "unknown")[:64],
                        str(p.get("pattern_description") or "")[:2000],
                        p.get("pattern_keywords"),
                        p.get("requirements"),
                        p.get("problems"),
                        (str(p.get("category") or "").strip().lower()[:64] or None),
                        max(1, int(p.get("count") or 1)),
                        p.get("last_used_at"),
                    ),
                )
            for v in data.get("voice_training") or []:
                if not isinstance(v, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO voice_training (owner_id, original_text, rewritten_text, is_negative)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        uid,
                        str(v.get("original_text") or "")[:12000],
                        str(v.get("rewritten_text") or "")[:12000],
                        1 if int(v.get("is_negative") or 0) else 0,
                    ),
                )
            conn.commit()
        return True, "Импорт выполнен: паттерны заменены, примеры голоса добавлены."
    except Exception as exc:
        logger.exception("import_style_profile_json: %s", exc)
        return False, f"Ошибка импорта: {exc}"


def count_user_rejection_patterns(user_id: int | str) -> int:
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM user_rejection_patterns WHERE user_id=?",
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_user_rejection_patterns: %s", exc)
        return 0


def count_draft_feedback_by_action(user_id: int | str, action: str) -> int:
    uid = _uid_str(user_id)
    act = (action or "").strip().lower()[:32]
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM draft_feedback
                WHERE user_id=? AND LOWER(TRIM(action))=?
                """,
                (uid, act),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_draft_feedback_by_action: %s", exc)
        return 0


def count_draft_posts_by_status(user_id: int | str) -> dict[str, int]:
    uid = _uid_str(user_id)
    out: dict[str, int] = {}
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM draft_posts
                WHERE user_id=?
                GROUP BY status
                """,
                (uid,),
            ).fetchall()
        for r in rows:
            out[str(r["status"] or "").strip().lower()] = int(r["c"] or 0)
        return out
    except Exception as exc:
        logger.exception("count_draft_posts_by_status: %s", exc)
        return out


def count_draft_posts_total(user_id: int | str) -> int:
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM draft_posts WHERE user_id=?",
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_draft_posts_total: %s", exc)
        return 0


def count_posted_drafts_unedited(user_id: int | str) -> int:
    """Опубликовано без ручной правки текста (was_edited=0)."""
    uid = _uid_str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM draft_posts
                WHERE user_id=? AND status='posted'
                  AND COALESCE(was_edited, 0) = 0
                """,
                (uid,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_posted_drafts_unedited: %s", exc)
        return 0


def bump_pattern_usage(pattern_id: int) -> bool:
    try:
        pid = int(pattern_id)
    except (TypeError, ValueError):
        return False
    try:
        with get_connection() as conn:
            n = conn.execute(
                """
                UPDATE user_rejection_patterns
                SET count = count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (pid,),
            ).rowcount
            conn.commit()
        return n > 0
    except Exception as exc:
        logger.exception("bump_pattern_usage: %s", exc)
        return False


def list_user_drafts_by_status(
    user_id: int | str,
    status: str = "draft",
    *,
    exclude_draft_id: int | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    uid = _uid_str(user_id)
    st = (status or "draft").strip().lower()[:24]
    lim = max(1, min(int(limit), 30))
    ex = int(exclude_draft_id) if exclude_draft_id is not None else None
    try:
        with get_connection() as conn:
            if ex is not None:
                rows = conn.execute(
                    """
                    SELECT id, user_id, content, source_url, status, created_at
                    FROM draft_posts
                    WHERE user_id=? AND status=? AND id != ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (uid, st, ex, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id, content, source_url, status, created_at
                    FROM draft_posts
                    WHERE user_id=? AND status=?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (uid, st, lim),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("list_user_drafts_by_status: %s", exc)
        return []
