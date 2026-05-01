"""Персональные шаблоны ответов пользователя (SQLite)."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database import get_connection

if TYPE_CHECKING:
    from app.memory import ChatMemory

logger = logging.getLogger(__name__)

# callback_data ≤ 64 байт (латиница)
CB_LIST = "tpl:l"
CB_OPEN_PREFIX = "tpl:o:"
CB_DEL_PREFIX = "tpl:d:"

MAX_TITLE_LEN = 200
MAX_CONTENT_STORE = 120_000
MAX_DISPLAY_CHARS = 3800

# Одиночные команды — только точное совпадение (см. classify), не через substring `p in t`,
# иначе «не сохрани» и т.п. дадут ложное срабатывание.
# «Запомни» — только якоря (app.user_anchors), не шаблоны.
_STANDALONE_SAVE_WORDS = frozenset({"сохрани", "save", "remember"})

# Все строки в нижнем регистре, «ё» → «е» (совпадение после _norm на всём тексте).
_SAVE_PHRASES_RU = (
    # --- сохрани ---
    "сохрани это",
    "сохрани его",
    "сохрани ее",
    "сохрани ответ",
    "сохрани сообщение",
    "сохрани текст",
    "сохрани этот ответ",
    "сохрани это сообщение",
    "сохрани мой ответ",
    "сохрани мое сообщение",
    "сохрани нашу переписку",
    "сохрани в шаблоны",
    "сохрани в шаблон",
    "сохрани как шаблон",
    "сохрани в избранное",
    "сохрани это в избранное",
    "сохрани ответ в избранное",
    "сохранить это",
    "сохранить ответ",
    "сохранить сообщение",
    "сохранить текст",
    "сохранить в шаблоны",
    "сохранить в шаблон",
    "сохрани плиз",
    "сохрани pls",
    "сохрани plz",
    "сохрани пж",
    "закинь в шаблоны",
    "закинь в шаблон",
    "положи в шаблоны",
    "положи в шаблон",
    # --- добавь / сделай ---
    "добавь в шаблоны",
    "добавь в шаблон",
    "добавь это в шаблоны",
    "добавь ответ в шаблоны",
    "добавь сообщение в шаблоны",
    "добавь текст в шаблоны",
    "добавь в избранное",
    "добавь это в избранное",
    "добавь ответ в избранное",
    "добавить в шаблоны",
    "добавить в шаблон",
    "добавить это в шаблоны",
    "сделай шаблоном",
    "сделай это шаблоном",
    "сделай ответ шаблоном",
    "сделай сообщение шаблоном",
    "сделай шаблон",
)

_SAVE_PHRASES_EN = (
    "save this",
    "save this answer",
    "save this message",
    "save the response",
    "save response",
    "save to templates",
    "save to template",
    "save to saved",
    "add to templates",
    "add to template",
    "add to saved",
    "add this to templates",
    "add this to template",
    "please save this",
    "keep this",
    "save pls",
    "save plz",
)

_SAVE_PHRASES_MIX = (
    "save это",
    "save ответ",
    "save сообщение",
    "save текст",
    "сохрани this",
    "add в шаблоны",
    "add в шаблон",
    "keep это",
    "keep ответ",
)

_SAVE_PHRASES = _SAVE_PHRASES_RU + _SAVE_PHRASES_EN + _SAVE_PHRASES_MIX

# Триггеры и маркеры для fuzzy (короткие реплики ≤150 символов).
_FUZZY_TRIGGER_TOKENS = frozenset(
    {
        "сохрани",
        "сохранить",
        "добавь",
        "сделай",
        "добавить",
        "сделать",
        "закинь",
        "положи",
        "save",
        "keep",
        "add",
    }
)
_FUZZY_MARKER_TOKENS = frozenset(
    {
        "это",
        "его",
        "ее",
        "ответ",
        "сообщение",
        "текст",
        "шаблон",
        "шаблоны",
        "шаблоном",
        "избранное",
        "реплику",
        "совет",
        "вывод",
        "результат",
        "заметку",
        "версию",
        "this",
        "that",
        "it",
        "its",
        "answer",
        "message",
        "messages",
        "text",
        "response",
        "templates",
        "template",
        "saved",
        "мой",
        "мое",
        "моя",
        "мою",
        "моего",
        "мои",
        "мою",
        "нашу",
        "pls",
        "plz",
        "плиз",
        "пж",
        "please",
    }
)
_FUZZY_BARE_ONLY = frozenset(
    {"сохрани", "save", "remember", "keep", "pls", "plz", "плиз", "пж", "please"}
)

# Явная просьба сохранить реплику пользователя, а не ответ бота
_OWN_MESSAGE_MARKERS = (
    "мое сообщение",
    "моё сообщение",
    "мой текст",
    "свое сообщение",
    "своё сообщение",
    "сохрани мое",
    "сохрани мое сообщение",
    "сохрани моё",
    "сохрани моё сообщение",
    "сохрани мой текст",
)

_LIST_PHRASES = (
    "покажи шаблоны",
    "мои шаблоны",
    "список шаблонов",
    "открой шаблоны",
)


def _norm(s: str) -> str:
    t = (s or "").strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", t)


def _tokens_for_intent(t: str) -> list[str]:
    """Слова без пунктуации — чтобы «его» не ловилось внутри «категория»."""
    return re.findall(r"[а-яa-z0-9]+", t, flags=re.IGNORECASE)


def user_explicitly_wants_own_message_saved(text: str) -> bool:
    t = _norm(text)
    return any(m in t for m in _OWN_MESSAGE_MARKERS)


def classify_save_template_command(text: str) -> tuple[bool, str]:
    """
    Распознавание намерения «сохранить в шаблоны последний ответ бота».
    Возвращает (matched, reason) — reason для логов.
    """
    t = _norm(text)
    logger.debug(
        "classify_save_template_command: text=%r normalized=%r core=%r",
        text,
        t,
        t.strip(" ?!.,;:\u200b\ufeff"),
    )
    if not t:
        return False, "empty"
    # Одно слово: «сохрани», «save», … (+невидимые пробелы / знаки по краям)
    core = re.sub(r"^[\s\u200b\ufeff]+|[\s\u200b\ufeff]+$", "", t).strip()
    core = core.strip("?!.,;:…").strip()
    if core and len(core) <= 12 and core in _STANDALONE_SAVE_WORDS:
        return True, f"phrase:{core}"

    for p in _SAVE_PHRASES:
        if p in t:
            return True, f"phrase:{p}"
    if len(t) > 150:
        return False, "no_fixed_phrase_text_too_long"
    toks = [x.lower().replace("ё", "е") for x in _tokens_for_intent(t)]
    if not toks:
        return False, "no_fixed_phrase_no_tokens"
    st = frozenset(toks)

    # Короткая реплика только из «служебных» слов: «сохрани плиз», «save pls», …
    if len(t) <= 24 and st <= _FUZZY_BARE_ONLY and (st & {"сохрани", "save", "remember", "keep"}):
        return True, "fuzzy:bare_trigger"
    # Короткое сообщение и один токен-триггер (подстраховка к standalone)
    if len(t) <= 16 and len(toks) == 1 and toks[0] in _STANDALONE_SAVE_WORDS:
        return True, f"fuzzy:single_token:{toks[0]}"

    triggers = st & _FUZZY_TRIGGER_TOKENS
    if not triggers:
        return False, "no_fixed_phrase_no_trigger"

    # «add» / «keep» без указателя на что сохранить — пропускаем
    if triggers <= {"add"} and not (
        st & _FUZZY_MARKER_TOKENS or {"templates", "template", "saved"} & st
    ):
        return False, "no_fixed_phrase_add_without_target"
    if triggers <= {"keep"} and not (st & _FUZZY_MARKER_TOKENS):
        return False, "no_fixed_phrase_keep_without_target"

    markers_hit = st & _FUZZY_MARKER_TOKENS
    if any(x.startswith("переписк") for x in toks):
        markers_hit = markers_hit | frozenset({"переписка"})

    # «… в шаблоны» / «… в избранное» (нужен триггер сохранения)
    if "в шаблоны" in t and triggers:
        return True, "fuzzy:into_templates"
    if "в избранное" in t and triggers:
        return True, "fuzzy:into_favorites"

    if markers_hit:
        return True, f"fuzzy:trigger+marker:{','.join(sorted(triggers))}"

    # «добавь»/«сделай» + шаблон (указатель уже мог быть в другой форме)
    if (st & {"добавь", "добавить", "сделай", "сделать"}) and (
        st & {"шаблон", "шаблоны", "шаблоном", "template", "templates"}
    ):
        return True, "fuzzy:add_make+template"

    return False, "no_fixed_phrase"


def looks_like_list_templates_command(text: str) -> bool:
    t = _norm(text)
    if not t:
        return False
    return any(p in t for p in _LIST_PHRASES)


def parse_delete_template_title(text: str) -> str | None:
    """Возвращает название после «удали шаблон …» / «удалить шаблон …»."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.match(
        r"^(?:удали|удалить)\s+шаблон\s+(.+)$",
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
    return name[:MAX_TITLE_LEN] if name else None


def extract_save_title(text: str) -> str | None:
    """Явное имя из кавычек или фразы «как …»."""
    raw = (text or "").strip()
    if not raw:
        return None
    for pat in (
        r"как\s+«([^»]{1,200})»",
        r'(?:как|название)\s*[«"]([^»"]{1,200})[»"]',
        r"(?:как|название)\s*'([^']{1,200})'",
    ):
        m = re.search(pat, raw, flags=re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            if title:
                return title[:MAX_TITLE_LEN]
    m = re.search(
        r"(?:сохрани|назови|добавь)\s+(?:это\s+)?как\s+(.+)$",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        cand = m.group(1).strip()
        for q in ('"', "'", "«", "»"):
            cand = cand.strip(q)
        cand = cand.rstrip(".,!?")
        if cand and cand.lower() not in ("это", "шаблон", "шаблоном"):
            return cand[:MAX_TITLE_LEN]
    return None


def auto_title_from_content(content: str) -> str:
    line = (content or "").strip().split("\n", 1)[0].strip()
    line = re.sub(r"\s+", " ", line)
    if not line:
        return "Без названия"
    if len(line) > 72:
        line = line[:69] + "…"
    return line


def get_last_assistant_reply(memory: ChatMemory, user_id: int) -> str | None:
    for m in reversed(memory.get(user_id)):
        if m.get("role") == "assistant":
            c = (m.get("content") or "").strip()
            return c or None
    return None


def _unique_title(conn, user_id: int, base: str) -> str:
    uid = str(user_id)
    base = (base or "Без названия")[:MAX_TITLE_LEN]
    title = base
    n = 2
    while True:
        row = conn.execute(
            "SELECT 1 FROM user_templates WHERE user_id=? AND title=? LIMIT 1",
            (uid, title),
        ).fetchone()
        if not row:
            return title
        suffix = f" ({n})"
        title = (base[: MAX_TITLE_LEN - len(suffix)] + suffix)[:MAX_TITLE_LEN]
        n += 1
        if n > 500:
            return base[: MAX_TITLE_LEN - 20] + " (копия)"


def list_template_rows(user_id: int) -> list[dict]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, title, created_at
                FROM user_templates
                WHERE user_id=?
                ORDER BY id DESC
                """,
                (str(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("user_templates list: %s", exc)
        return []


def get_template(user_id: int, template_id: int) -> dict | None:
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, title, content, created_at
                FROM user_templates
                WHERE id=? AND user_id=?
                """,
                (template_id, str(user_id)),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.exception("user_templates get: %s", exc)
        return None


def insert_template(user_id: int, title: str, content: str, max_per_user: int) -> tuple[bool, str]:
    """Возвращает (ok, сообщение_для_пользователя_или_ошибка)."""
    uid = str(user_id)
    body = (content or "").strip()
    if not body:
        return False, "Пока нечего сохранять — я ещё не отвечал в этом чате или история пуста. Задай вопрос, а потом скажи «сохрани это» 📎"
    if len(body) > MAX_CONTENT_STORE:
        body = body[:MAX_CONTENT_STORE] + "\n… (текст обрезан по длине)"
    try:
        with get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM user_templates WHERE user_id=?",
                (uid,),
            ).fetchone()
            if int(cnt["c"]) >= max_per_user:
                return (
                    False,
                    f"У тебя уже максимум шаблонов ({max_per_user}) 🗂️ Удали что-то лишнее командой «удали шаблон …» или кнопкой под записью.",
                )
            final_title = _unique_title(conn, user_id, title)
            conn.execute(
                """
                INSERT INTO user_templates (user_id, title, content)
                VALUES (?, ?, ?)
                """,
                (uid, final_title, body),
            )
            conn.commit()
        return True, final_title
    except Exception as exc:
        logger.exception("user_templates insert: %s", exc)
        return False, "Не вышло сохранить шаблон — что-то с базой. Попробуй ещё раз чуть позже 😔"


def delete_template_by_id(user_id: int, template_id: int) -> tuple[bool, str | None]:
    """Возвращает (успех, title удалённого)."""
    uid = str(user_id)
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT title FROM user_templates WHERE id=? AND user_id=?",
                (template_id, uid),
            ).fetchone()
            if not row:
                return False, None
            title = str(row["title"])
            conn.execute(
                "DELETE FROM user_templates WHERE id=? AND user_id=?",
                (template_id, uid),
            )
            conn.commit()
        return True, title
    except Exception as exc:
        logger.exception("user_templates delete id: %s", exc)
        return False, None


def delete_template_by_title(user_id: int, title_query: str) -> tuple[bool, str | None]:
    """Удаляет последний по id шаблон с подходящим названием (без учёта регистра)."""
    uid = str(user_id)
    q = (title_query or "").strip()
    if not q:
        return False, None
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, title FROM user_templates
                WHERE user_id=? AND lower(title)=lower(?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (uid, q),
            ).fetchone()
            if not row:
                return False, None
            tid = int(row["id"])
            title = str(row["title"])
            conn.execute(
                "DELETE FROM user_templates WHERE id=? AND user_id=?",
                (tid, uid),
            )
            conn.commit()
        return True, title
    except Exception as exc:
        logger.exception("user_templates delete title: %s", exc)
        return False, None


def build_templates_list_keyboard(rows: list[dict]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    kb: list[list[InlineKeyboardButton]] = []
    for r in rows:
        tid = int(r["id"])
        label = str(r["title"])
        if len(label) > 56:
            label = label[:53] + "…"
        kb.append(
            [
                InlineKeyboardButton(
                    text=f"📌 {label}",
                    callback_data=f"{CB_OPEN_PREFIX}{tid}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=kb)


def build_template_view_keyboard(template_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"{CB_DEL_PREFIX}{template_id}",
                ),
                InlineKeyboardButton(
                    text="📚 Ещё шаблоны",
                    callback_data=CB_LIST,
                ),
            ],
        ]
    )


def format_template_body(content: str) -> str:
    c = (content or "").strip()
    if len(c) <= MAX_DISPLAY_CHARS:
        return c
    return c[: MAX_DISPLAY_CHARS - 40] + "\n\n… (часть текста обрезана для чата)"

def is_template_callback(data: str) -> bool:
    d = data or ""
    return d == CB_LIST or d.startswith(CB_OPEN_PREFIX) or d.startswith(CB_DEL_PREFIX)


def parse_template_callback(data: str) -> tuple[str, int | None]:
    """('list'|'open'|'delete', id_or_none)."""
    if data == CB_LIST:
        return "list", None
    if data.startswith(CB_OPEN_PREFIX):
        rest = data[len(CB_OPEN_PREFIX) :]
        try:
            return "open", int(rest)
        except ValueError:
            return "unknown", None
    if data.startswith(CB_DEL_PREFIX):
        rest = data[len(CB_DEL_PREFIX) :]
        try:
            return "delete", int(rest)
        except ValueError:
            return "unknown", None
    return "unknown", None
