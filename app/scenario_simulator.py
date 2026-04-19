"""Симулятор сценариев «что если…» — три ветки + углубление по кнопке."""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.llm_agent import LLMAgent

logger = logging.getLogger(__name__)

TTL_SECONDS = 45 * 60
MAX_SESSIONS = 200

CB_PREFIX = "scen:"

_SCEN_TRIGGERS = (
    "что если",
    "что будет если",
    "а если ",
    "если бы ",
    "какие варианты",
    "какой вариант",
    "стоит ли",
    "имеет смысл ли",
    "риски и выгоды",
    "три сценария",
    "сценарии развития",
    "прогноз если",
    "что лучше:",
    "как поступить если",
)

_NEG_DEFINITION = (
    "что такое ",
    "кто такой ",
    "кто такая ",
    "как работает ",
    "объясни что такое",
    "объясни, что такое",
    "докажи",
    "теорема",
    "лемма",
    "аксиом",
    "выведи формулу",
    "выведи ",
)

SCEN_GEN_SYSTEM = (
    "Ты — Кузьма, AI-помощник. Пиши по-русски: живо, с лёгким юмором и 2–3 эмодзи, но без выдачи выдуманных фактов.\n"
    "Задача: дать РОВНО три сценария развития событий по вопросу пользователя: оптимистичный 🟢, реалистичный 🟡, "
    "пессимистичный 🔴.\n"
    "Для каждого: оценочная вероятность в % (с тильдой «~», как иллюстрация, не как точный прогноз), "
    "2–4 ключевых фактора (маркером «•»), исход в деньгах/сроках — только как пример или диапазон с оговоркой «условно», "
    "коротко риски.\n"
    "Явно напомни: это не финансовая/юридическая рекомендация, цифры ориентировочные.\n"
    "Без заголовков через #. Без JSON и без кнопок.\n\n"
    "Строго соблюдай разметку для парсера (три блока подряд):\n"
    "###SCEN_O\n"
    "(текст оптимистичного сценария)\n"
    "###SCEN_R\n"
    "(текст реалистичного сценария)\n"
    "###SCEN_P\n"
    "(текст пессимистичного сценария)\n"
)

SCEN_EXPAND_SYSTEM = (
    "Ты — Кузьма. Пользователь выбрал один из трёх сценариев «что если». Разверни его подробно: шаги, тайминг, "
    "что мониторить, что делать если пойдёт наперекосяк, альтернативы. Стиль живой, 2–3 эмодзи, без #.\n"
    "Не выдавай гарантированные рыночные цифры; оговаривай неопределённость. Без JSON в конце.\n"
)


@dataclass
class ScenarioSession:
    user_id: int
    original: str
    optimist: str
    realist: str
    pessimist: str
    intro_text: str
    created: float


_sessions: dict[str, ScenarioSession] = {}


def _norm(s: str) -> str:
    t = (s or "").strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", t)


def _extract_params_note(text: str) -> str:
    t = text or ""
    parts: list[str] = []
    for m in re.finditer(
        r"(\d[\d\s]*(?:тыс|к|млн|млрд)?)\s*(?:руб|₽|р\.|usd|\$)?",
        t,
        flags=re.IGNORECASE,
    ):
        parts.append(m.group(0).strip())
    for m in re.finditer(
        r"\b(\d{1,2})\s*(?:лет|год|месяц|мес\.?)\b",
        t,
        flags=re.IGNORECASE,
    ):
        parts.append(m.group(0).strip())
    return ", ".join(parts[:5]) if parts else "— не выделил явно, опирайся на формулировку"


def classify_scenario_request(text: str) -> tuple[bool, str, dict[str, Any]]:
    """
    (is_scenario, topic_for_prompt, params).
    topic_for_prompt — исходная формулировка пользователя.
    """
    raw = (text or "").strip()
    t = _norm(raw)
    if len(t) < 18:
        return False, "", {}

    has_trigger = any(tr in t for tr in _SCEN_TRIGGERS)
    if not has_trigger:
        if t.startswith("что такое") or t.startswith("кто такой") or t.startswith("кто такая"):
            return False, "", {}
        if t.startswith("как работает") and "если" not in t[:48]:
            return False, "", {}
        if LLMAgent._concierge_intent(raw):
            return False, "", {}
        return False, "", {}

    if LLMAgent._concierge_intent(raw) and not any(
        x in t
        for x in (
            "что если",
            "если бы ",
            "а если ",
            "стоит ли",
            "какие варианты",
            "какой вариант",
        )
    ):
        return False, "", {}

    if any(neg in t for neg in _NEG_DEFINITION) and not any(
        x in t for x in ("что если", "если бы ", "а если ", "стоит ли", "какие варианты")
    ):
        return False, "", {}

    params = {"params_note": _extract_params_note(raw)}
    return True, raw, params


def parse_scenario_blocks(llm_text: str) -> tuple[str, str, str]:
    """Извлекает три блока; при сбое кладёт остаток в реалистичный."""
    text = llm_text or ""
    o = re.search(r"###SCEN_O\s*([\s\S]*?)(?=###SCEN_R|$)", text, re.IGNORECASE)
    r_ = re.search(r"###SCEN_R\s*([\s\S]*?)(?=###SCEN_P|$)", text, re.IGNORECASE)
    p = re.search(r"###SCEN_P\s*([\s\S]*)$", text, re.IGNORECASE)
    if o and r_ and p:
        return o.group(1).strip(), r_.group(1).strip(), p.group(1).strip()
    if o and r_:
        return o.group(1).strip(), r_.group(1).strip(), "Третий блок не разобрался — гляни целиком ответ выше 🔴"
    body = text.strip()
    if not body:
        body = "Модель вернула пустоту — попробуй переформулировать вопрос 🎲"
    return (
        "🟢 Оптимистичный: см. общий текст выше.",
        body[:3500],
        "🔴 Пессимистичный: см. общий текст выше.",
    )


def _prune_sessions() -> None:
    now = time.time()
    dead = [k for k, v in _sessions.items() if now - v.created > TTL_SECONDS]
    for k in dead:
        _sessions.pop(k, None)
    if len(_sessions) > MAX_SESSIONS:
        for k, _ in sorted(_sessions.items(), key=lambda kv: kv[1].created)[: len(_sessions) - MAX_SESSIONS + 20]:
            _sessions.pop(k, None)


def put_session(user_id: int, original: str, optimist: str, realist: str, pessimist: str, intro_text: str) -> str:
    _prune_sessions()
    sid = secrets.token_hex(4)
    _sessions[sid] = ScenarioSession(
        user_id=user_id,
        original=original,
        optimist=optimist,
        realist=realist,
        pessimist=pessimist,
        intro_text=intro_text,
        created=time.time(),
    )
    logger.info("scenario: новая сессия user_id=%s id=%s", user_id, sid)
    return sid


def get_session(user_id: int, sid: str) -> ScenarioSession | None:
    _prune_sessions()
    s = _sessions.get(sid)
    if not s or s.user_id != user_id:
        return None
    if time.time() - s.created > TTL_SECONDS:
        _sessions.pop(sid, None)
        return None
    return s


def is_scenario_callback(data: str) -> bool:
    d = data or ""
    return d.startswith(CB_PREFIX) and len(d) >= 10


def parse_scenario_callback(data: str) -> tuple[str, str]:
    """action: o|r|p|b|s|n, session_id hex."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "scen":
        return "", ""
    act, sid = parts[1], parts[2]
    if act not in ("o", "r", "p", "b", "s", "n") or not re.fullmatch(
        r"[0-9a-f]{8}", sid, flags=re.IGNORECASE
    ):
        return "", ""
    return act, sid.lower()


def build_scenario_choice_keyboard(sid: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🟢 Оптимистичный", callback_data=f"{CB_PREFIX}o:{sid}"),
            InlineKeyboardButton(text="🟡 Реалистичный", callback_data=f"{CB_PREFIX}r:{sid}"),
        ],
        [InlineKeyboardButton(text="🔴 Пессимистичный", callback_data=f"{CB_PREFIX}p:{sid}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_scenario_deep_keyboard(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 К трём вариантам",
                    callback_data=f"{CB_PREFIX}b:{sid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📌 Сохранить как шаблон",
                    callback_data=f"{CB_PREFIX}s:{sid}",
                ),
                InlineKeyboardButton(
                    text="💬 Другой вопрос",
                    callback_data=f"{CB_PREFIX}n:{sid}",
                ),
            ],
        ]
    )


def format_scenario_intro(original: str, o: str, r: str, p: str) -> str:
    head = (
        "Давай рассмотрим 3 варианта развития событий 🎲\n"
        f"Твой вопрос: {original.strip()}\n\n"
        "🟢 Оптимистичный\n"
        f"{o}\n\n"
        "🟡 Реалистичный\n"
        f"{r}\n\n"
        "🔴 Пессимистичный\n"
        f"{p}\n\n"
        "Какой сценарий разобрать подробнее? Жми кнопку ниже 👇"
    )
    if len(head) > 4000:
        head = head[:3980] + "\n…"
    return head


def generate_scenarios(agent: LLMAgent, topic: str, params: dict[str, Any]) -> tuple[str, str, str, str]:
    note = str(params.get("params_note") or "—")
    user_payload = f"Вопрос пользователя:\n{topic}\n\nВыделенные детали из текста: {note}"
    raw = agent.run_raw_completion(
        system=SCEN_GEN_SYSTEM,
        user=user_payload,
        max_tokens=2800,
        temperature=min(0.88, getattr(agent, "_chat_temperature", 0.75) + 0.08),
    )
    o, r_, p = parse_scenario_blocks(raw)
    return o, r_, p, raw


def run_scenario_expand(
    agent: LLMAgent,
    original: str,
    label: str,
    body: str,
) -> str:
    user_payload = (
        f"Исходный вопрос пользователя:\n{original}\n\n"
        f"Выбранный тип сценария: {label}\n\n"
        f"Текст сценария:\n{body}\n\n"
        "Разверни подробно этот вариант."
    )
    return agent.run_raw_completion(
        system=SCEN_EXPAND_SYSTEM,
        user=user_payload,
        max_tokens=3200,
        temperature=getattr(agent, "_chat_temperature", 0.75),
    )
