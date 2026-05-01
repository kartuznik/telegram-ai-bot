import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from app.database import get_connection


class ChatMemory:
    def __init__(self, max_messages: int = 20) -> None:
        self.max_messages = max_messages
        self.logger = logging.getLogger(__name__)
        self.db_available = True
        self._store: Dict[int, List[dict[str, str]]] = defaultdict(list)
        self._pref_store: Dict[int, Dict[str, str]] = defaultdict(dict)

    def add(self, user_id: int, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            return
        try:
            self._ensure_user(user_id)
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)",
                    (str(user_id), role, content),
                )
                conn.execute(
                    "UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE user_id=?",
                    (str(user_id),),
                )
                conn.commit()
            self.logger.info("SQLite: сохранено сообщение для user_id=%s", user_id)
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            self._store[user_id].append({"role": role, "content": content})
            if len(self._store[user_id]) > self.max_messages:
                self._store[user_id] = self._store[user_id][-self.max_messages :]

    def get(self, user_id: int) -> List[dict[str, str]]:
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT role, content
                    FROM (
                        SELECT role, content, id
                        FROM conversations
                        WHERE user_id=?
                        ORDER BY id DESC
                        LIMIT ?
                    ) t
                    ORDER BY id ASC
                    """,
                    (str(user_id), self.max_messages),
                ).fetchall()
            messages = [{"role": row["role"], "content": row["content"]} for row in rows]
            self.logger.info("SQLite: загружено %s сообщений из истории", len(messages))
            return messages
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            return list(self._store[user_id])

    def clear(self, user_id: int) -> None:
        self.clear_user_memory(user_id)

    def get_user_memory(self, user_id: int) -> List[dict[str, str]]:
        messages = self.get(user_id)
        self.logger.info("SQLite: формат памяти — список из %s сообщений", len(messages))
        return messages

    def save_user_memory(self, user_id: int, user_msg: str, bot_msg: str) -> None:
        self.add(user_id, "user", user_msg)
        self.add(user_id, "assistant", bot_msg)

    def get_style_preferences(self, user_id: int) -> dict[str, str]:
        uid = str(user_id)
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT pref_key, pref_value FROM user_preferences WHERE user_id=?",
                    (uid,),
                ).fetchall()
            return {str(r["pref_key"]): str(r["pref_value"]) for r in rows}
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite user_preferences read: %s", exc)
            return dict(self._pref_store[user_id])

    def update_style_preferences(self, user_id: int, updates: dict[str, str]) -> None:
        if not updates:
            return
        uid = str(user_id)
        try:
            self._ensure_user(user_id)
            with get_connection() as conn:
                for key, val in updates.items():
                    conn.execute(
                        """
                        INSERT INTO user_preferences (user_id, pref_key, pref_value, updated_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id, pref_key) DO UPDATE SET
                            pref_value=excluded.pref_value,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (uid, key, val),
                    )
                conn.commit()
            self.logger.info(
                "Стиль: обновлены предпочтения user_id=%s: %s",
                user_id,
                updates,
            )
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite user_preferences write: %s", exc)
            self._pref_store[user_id].update(updates)

    def clear_style_preferences(self, user_id: int) -> None:
        uid = str(user_id)
        try:
            with get_connection() as conn:
                conn.execute("DELETE FROM user_preferences WHERE user_id=?", (uid,))
                conn.commit()
            self.logger.info("Стиль: сброшены предпочтения user_id=%s", user_id)
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite user_preferences clear: %s", exc)
        self._pref_store.pop(user_id, None)

    def count_user_image_messages(self, user_id: int) -> int:
        """Сколько user-сообщений с префиксом [Изображение] уже в истории."""
        try:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM conversations
                    WHERE user_id=? AND role='user' AND content LIKE '[Изображение]%'
                    """,
                    (str(user_id),),
                ).fetchone()
            return int(row["c"]) if row else 0
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            return sum(
                1
                for m in self._store[user_id]
                if m["role"] == "user" and m["content"].startswith("[Изображение]")
            )

    def get_previous_image_assistant_reply(self, user_id: int, max_chars: int = 1000) -> str | None:
        """Текст последнего ответа ассистента после пары user [Изображение] → assistant."""
        msgs = self.get(user_id)
        last_text: str | None = None
        for i in range(len(msgs) - 1):
            if msgs[i]["role"] == "user" and msgs[i]["content"].startswith("[Изображение]"):
                if msgs[i + 1]["role"] == "assistant":
                    last_text = msgs[i + 1]["content"]
        if not last_text:
            return None
        return last_text[:max_chars] if len(last_text) > max_chars else last_text

    def build_vision_history_context(self, user_id: int, max_chars: int = 2800) -> str | None:
        """Фрагмент истории чата для vision: прошлые [Изображение], ответы и имена от пользователя."""
        msgs = self.get(user_id)
        if not msgs:
            return None
        lines: List[str] = []
        for m in msgs:
            label = "Пользователь" if m["role"] == "user" else "Кузьма"
            content = (m["content"] or "").strip().replace("\n", " ")
            if len(content) > 600:
                content = content[:597] + "…"
            lines.append(f"{label}: {content}")
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return "… [начало обрезано — ниже последние реплики]\n" + text[-(max_chars - 45) :]

    def get_all_conversations(self, user_id: int) -> List[dict[str, str]]:
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT role, content
                    FROM conversations
                    WHERE user_id=?
                    ORDER BY id ASC
                    """,
                    (str(user_id),),
                ).fetchall()
            return [{"role": row["role"], "content": row["content"]} for row in rows]
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            return list(self._store[user_id])

    def clear_user_memory(self, user_id: int) -> None:
        try:
            with get_connection() as conn:
                conn.execute(
                    "DELETE FROM conversations WHERE user_id=?",
                    (str(user_id),),
                )
                conn.execute(
                    "UPDATE users SET last_seen=CURRENT_TIMESTAMP WHERE user_id=?",
                    (str(user_id),),
                )
                conn.commit()
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            self._store[user_id].clear()

    def get_user_activity_count(self, user_id: int) -> int:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM conversations
                    WHERE user_id=?
                      AND role='user'
                      AND date(created_at) = date('now', 'localtime')
                    """,
                    (str(user_id),),
                ).fetchone()
            return int(row["cnt"]) if row else 0
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            return 0

    def get_last_message_time(self, user_id: int) -> datetime | None:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT created_at
                    FROM conversations
                    WHERE user_id=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (str(user_id),),
                ).fetchone()
            if not row or not row["created_at"]:
                return None
            return datetime.fromisoformat(str(row["created_at"]).replace(" ", "T"))
        except Exception as exc:
            self.db_available = False
            self.logger.exception("SQLite ошибка: %s", exc)
            return None

    def _ensure_user(self, user_id: int) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id)
                VALUES (?)
                ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP
                """,
                (str(user_id),),
            )
            conn.commit()
