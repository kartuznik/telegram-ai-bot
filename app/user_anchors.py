"""Закладки «якоря» — фрагмент диалога для быстрого возврата (не шаблоны)."""
from __future__ import annotations

import logging
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database import get_connection

logger = logging.getLogger(__name__)

MAX_ANCHOR_TITLE_LEN = 200
MAX_SNIPPET_LEN = 1000

# callback_data ≤ 64 байт
CB_ANCHOR_LIST = "anchor:l"
CB_ANCHOR_OPEN_PREFIX = "anchor:o:"
CB_ANCHOR_DEL_PREFIX = "anchor:d:"

# Фразы «запомни…» → якорь (фрагмент диалога), не шаблон. Порядок: длинные раньше коротких (substring).
_ANCHOR_REMEMBER_PHRASES: tuple[str, ...] = (
    "запомни этот момент",
    "запомни этот ответ",
    "запомни это сообщение",
    "запомни мое сообщение",
    "запомни моё сообщение",
    "запомни мой текст",
    "запомни ответ",
    "запомни сообщение",
    "запомни обсуждение",
    "запомни разговор",
    "запомни тему",
    "запомни это",
    "запомни",
)


def _norm(s: str) -> str:
    t = (s or "").strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", t)


def parse_delete_anchor_title(text: str) -> str | None:
    """Если не «удали якорь …» — None; иначе название (может быть пустым)."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.match(
        r"^(?:удали|удалить)\s+якорь\s+(.+)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    name = m.group(1).strip()
    if (name.startswith('"') and name.endswith('"')) or (
        name.startswith("'") and name.endswith("'")
    ):
        name = name[1:-1].strip()
    if name.startswith("«") and name.endswith("»"):
        name = name[1:-1].strip()
    return name[:MAX_ANCHOR_TITLE_LEN] if name else ""


def looks_like_list_anchors_command(text: str) -> bool:
    t = _norm(text)
    if not t:
        return False
    if t in {"покажи якоря", "мои якоря", "список якорей", "закладки", "мои закладки"}:
        return True
    return t.startswith("/bookmarks")


def _extract_quoted(text: str) -> str | None:
    for pat in (
        r'«([^»]{1,200})»',
        r'"([^"]{1,200})"',
        r"'([^']{1,200})'",
    ):
        m = re.search(pat, text)
        if m:
            s = m.group(1).strip()
            if s:
                return s[:MAX_ANCHOR_TITLE_LEN]
    return None


def _extract_after_colon_prefix(t: str, prefixes: tuple[str, ...]) -> str | None:
    for p in prefixes:
        if t.startswith(p):
            rest = t[len(p) :].strip()
            if rest:
                q = _extract_quoted(rest) or rest.split("—", 1)[0].strip()
                return q[:MAX_ANCHOR_TITLE_LEN] if q else None
    return None


def classify_anchor_command(text: str) -> tuple[bool, str, str | None]:
    """
    (matched, reason, title).
    title: подсказка названия для create, имя для delete, None для list.
    """
    raw = (text or "").strip()
    if not raw:
        return False, "", None

    del_parsed = parse_delete_anchor_title(text)
    if del_parsed is not None:
        if not del_parsed.strip():
            return True, "delete_empty", None
        return True, "delete", del_parsed.strip()

    if looks_like_list_anchors_command(text):
        return True, "list", None

    t = _norm(text)

    for p in sorted(_ANCHOR_REMEMBER_PHRASES, key=len, reverse=True):
        if p in t:
            return True, f"create:phrase:{p}", _extract_quoted(raw)

    if "сохрани это обсуждение" in t or "сохранить это обсуждение" in t:
        return True, "create:save_discussion", _extract_quoted(raw)

    title_colon = _extract_after_colon_prefix(
        t,
        ("якорь:", "закладка:", "закладки:", "anchor:"),
    )
    if title_colon is not None:
        return True, "create:colon", title_colon
    if t.startswith("якорь ") and len(t) > len("якорь "):
        rest = t[len("якорь ") :].strip()
        if rest:
            return True, "create:anchor_space", rest[:MAX_ANCHOR_TITLE_LEN]

    if "поставь якорь" in t or "поставь закладку" in t:
        hint = _extract_after_colon_prefix(t, ("поставь якорь:", "поставь закладку:"))
        if hint is None:
            hint = _extract_quoted(raw)
        return True, "create:place_anchor", hint

    m_ret = re.match(
        r"^\s*верн[её]мся\s+к\s+(.+)$",
        raw.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_ret:
        rest = m_ret.group(1).strip()
        q = _extract_quoted(rest) or rest.strip(" —:-")
        if q:
            return True, "create:return_to", q[:MAX_ANCHOR_TITLE_LEN]

    return False, "", None


def auto_title_anchor(snippet: str, fallback: str = "Якорь") -> str:
    line = (snippet or "").strip().split("\n", 1)[0].strip()
    line = re.sub(r"^\s*ты:\s*", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\s+", " ", line)
    if not line:
        return fallback[:MAX_ANCHOR_TITLE_LEN]
    if len(line) > 72:
        line = line[:69] + "…"
    return line[:MAX_ANCHOR_TITLE_LEN]


def _unique_anchor_title(conn, user_id: int, base: str) -> str:
    uid = str(user_id)
    base = (base or "Якорь")[:MAX_ANCHOR_TITLE_LEN]
    title = base
    n = 2
    while True:
        row = conn.execute(
            "SELECT 1 FROM conversation_anchors WHERE user_id=? AND title=? LIMIT 1",
            (uid, title),
        ).fetchone()
        if not row:
            return title
        suffix = f" ({n})"
        title = (base[: MAX_ANCHOR_TITLE_LEN - len(suffix)] + suffix)[:MAX_ANCHOR_TITLE_LEN]
        n += 1
        if n > 500:
            return base[: MAX_ANCHOR_TITLE_LEN - 20] + " (копия)"


def build_anchor_snippet_and_ref(user_id: int) -> tuple[str | None, int | None, str]:
    """
    Сниппет (до MAX_SNIPPET_LEN), id последнего ответа assistant в conversations,
    черновик названия (из последнего user перед ним).
    """
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, content FROM conversations
                WHERE user_id=? AND role='assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (uid,),
            ).fetchone()
            if not row:
                return None, None, "Якорь"
            aid = int(row["id"])
            asst = (row["content"] or "").strip()
            prev = conn.execute(
                """
                SELECT role, content FROM conversations
                WHERE user_id=? AND id < ?
                ORDER BY id DESC
                LIMIT 12
                """,
                (uid, aid),
            ).fetchall()
            user_chunks: list[str] = []
            for r in prev:
                if r["role"] == "user":
                    c = (r["content"] or "").strip()
                    if c:
                        user_chunks.append(c)
                    if len(user_chunks) >= 2:
                        break
                elif r["role"] == "assistant":
                    break
            user_chunks.reverse()
            parts: list[str] = []
            for u in user_chunks:
                parts.append(f"Ты: {u}")
            parts.append(f"Кузьма: {asst}")
            snippet = "\n\n".join(parts).strip()
            if len(snippet) > MAX_SNIPPET_LEN:
                snippet = snippet[-MAX_SNIPPET_LEN:]
                if not snippet.startswith("…"):
                    snippet = "…" + snippet.lstrip()
            draft = user_chunks[-1] if user_chunks else asst
            draft_line = draft.split("\n", 1)[0].strip()
            if len(draft_line) > 80:
                draft_line = draft_line[:77] + "…"
            return snippet, aid, draft_line or "Якорь"
    except Exception as exc:
        logger.exception("build_anchor_snippet: %s", exc)
        return None, None, "Якорь"


def count_anchors(user_id: int) -> int:
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation_anchors WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:
        logger.exception("count_anchors: %s", exc)
        return 0


def insert_anchor(
    user_id: int,
    title: str,
    context_snippet: str,
    message_ref: int,
    max_anchors: int,
) -> tuple[bool, str]:
    if max_anchors < 1:
        max_anchors = 20
    uid = str(user_id)
    snippet = (context_snippet or "")[:MAX_SNIPPET_LEN]
    try:
        with get_connection() as conn:
            n = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM conversation_anchors WHERE user_id=?",
                    (uid,),
                ).fetchone()["c"]
            )
            if n >= max_anchors:
                return (
                    False,
                    "У тебя уже полная коллекция якорей — мест нет, как на верхней полке 📚 "
                    "Удали парочку старых: «покажи якоря» → корзина, или «удали якорь Название». "
                    "Освободим слот — и снова в путь! 🎯",
                )
            final_title = _unique_anchor_title(conn, user_id, title)
            conn.execute(
                """
                INSERT INTO conversation_anchors (user_id, title, context_snippet, message_ref)
                VALUES (?, ?, ?, ?)
                """,
                (uid, final_title, snippet, message_ref),
            )
            conn.commit()
        return True, final_title
    except Exception as exc:
        logger.exception("insert_anchor: %s", exc)
        return False, "Что-то пошло не так с базой — попробуй ещё раз чуть позже 😥"


def list_anchor_rows(user_id: int) -> list[dict]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, title, created_at
                FROM conversation_anchors
                WHERE user_id=?
                ORDER BY id DESC
                """,
                (str(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("list_anchor_rows: %s", exc)
        return []


def get_anchor(user_id: int, anchor_id: int) -> dict | None:
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, title, context_snippet, message_ref, created_at
                FROM conversation_anchors
                WHERE id=? AND user_id=?
                """,
                (anchor_id, str(user_id)),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.exception("get_anchor: %s", exc)
        return None


def delete_anchor_by_id(user_id: int, anchor_id: int) -> tuple[bool, str | None]:
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT title FROM conversation_anchors WHERE id=? AND user_id=?",
                (anchor_id, uid),
            ).fetchone()
            if not row:
                return False, None
            title = str(row["title"])
            conn.execute(
                "DELETE FROM conversation_anchors WHERE id=? AND user_id=?",
                (anchor_id, uid),
            )
            conn.commit()
        return True, title
    except Exception as exc:
        logger.exception("delete_anchor_by_id: %s", exc)
        return False, None


def delete_anchor_by_title(user_id: int, title: str) -> tuple[bool, str | None]:
    uid = str(user_id)
    needle = (title or "").strip()
    if not needle:
        return False, None
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, title FROM conversation_anchors
                WHERE user_id=? AND LOWER(title)=LOWER(?)
                ORDER BY id DESC LIMIT 1
                """,
                (uid, needle),
            ).fetchone()
            if not row:
                return False, None
            tid = int(row["id"])
            removed = str(row["title"])
            conn.execute(
                "DELETE FROM conversation_anchors WHERE id=? AND user_id=?",
                (tid, uid),
            )
            conn.commit()
        return True, removed
    except Exception as exc:
        logger.exception("delete_anchor_by_title: %s", exc)
        return False, None


def is_anchor_callback(data: str) -> bool:
    d = data or ""
    return d == CB_ANCHOR_LIST or d.startswith(CB_ANCHOR_OPEN_PREFIX) or d.startswith(
        CB_ANCHOR_DEL_PREFIX
    )


def parse_anchor_callback(data: str) -> tuple[str, int | None]:
    """kind: list|open|delete, id."""
    d = data or ""
    if d == CB_ANCHOR_LIST:
        return "list", None
    if d.startswith(CB_ANCHOR_OPEN_PREFIX):
        rest = d[len(CB_ANCHOR_OPEN_PREFIX) :]
        try:
            return "open", int(rest)
        except ValueError:
            return "open", None
    if d.startswith(CB_ANCHOR_DEL_PREFIX):
        rest = d[len(CB_ANCHOR_DEL_PREFIX) :]
        try:
            return "delete", int(rest)
        except ValueError:
            return "delete", None
    return "", None


def _button_title(title: str) -> str:
    t = (title or "Якорь").replace("\n", " ").strip()
    if len(t) > 40:
        t = t[:37] + "…"
    return f"🔖 {t}"


def build_anchors_list_keyboard(rows: list[dict]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for r in rows:
        aid = int(r["id"])
        title = str(r["title"])
        cb = f"{CB_ANCHOR_OPEN_PREFIX}{aid}"
        if len(cb.encode("utf-8")) > 64:
            continue
        buttons.append([InlineKeyboardButton(text=_button_title(title), callback_data=cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_anchor_view_keyboard(anchor_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"{CB_ANCHOR_DEL_PREFIX}{anchor_id}",
                ),
                InlineKeyboardButton(
                    text="📌 Ещё якоря",
                    callback_data=CB_ANCHOR_LIST,
                ),
            ]
        ]
    )


def format_anchor_message(row: dict) -> str:
    title = str(row.get("title") or "Якорь")
    body = (row.get("context_snippet") or "").strip()
    if len(body) > 3500:
        body = body[:3490] + "\n…"
    return (
        f"🔖 Якорь «{title}» — вот что мы там наговорили:\n\n"
        f"{body}\n\n"
        f"Хочешь продолжить тему — просто напиши в чат, я подхвачу ход мысли с последних сообщений 💬✨"
    )
