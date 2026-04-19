"""Распознавание предпочтений стиля ответа и текст для system prompt."""

from __future__ import annotations

# Значения: response_length — short | long; tone — serious | default; language_level — simple | default

_LENGTH_SHORT = (
    "отвечай короче",
    "короче",
    "лаконично",
    "меньше воды",
    "без воды",
    "кратко",
    "сжато",
    "не размусоливай",
)
_LENGTH_LONG = (
    "больше деталей",
    "подробнее",
    "развернут",
    "развёрнут",
    "детальнее",
    "расширенно",
    "поглубже",
)
_TONE_SERIOUS = (
    "без шуток",
    "без юмора",
    "серьезно",
    "серьёзно",
    "строго",
    "по делу",
    "деловой тон",
    "без иронии",
)
_TONE_PLAYFUL = (
    "с шутками",
    "с юмором",
    "пошути",
    "веселее",
    "пофлуди",
)
_LANG_SIMPLE = (
    "объясни проще",
    "попроще",
    "проще",
    "без терминов",
    "без сложных слов",
    "как для новичка",
    "простым языком",
    "на пальцах",
)
_LANG_TECH = (
    "можно сложнее",
    "технические термины",
    "профессиональнее",
    "для специалиста",
)


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е").strip()


def detect_style_updates(user_text: str) -> dict[str, str]:
    """Возвращает только изменённые ключи для записи в user_preferences."""
    t = _norm(user_text)
    if not t:
        return {}

    updates: dict[str, str] = {}

    if any(p in t for p in _LENGTH_LONG):
        updates["response_length"] = "long"
    elif any(p in t for p in _LENGTH_SHORT):
        updates["response_length"] = "short"

    if any(p in t for p in _TONE_SERIOUS):
        updates["tone"] = "serious"
    elif any(p in t for p in _TONE_PLAYFUL):
        updates["tone"] = "default"

    if any(p in t for p in _LANG_SIMPLE):
        updates["language_level"] = "simple"
    elif any(p in t for p in _LANG_TECH):
        updates["language_level"] = "default"

    # Эвристика: короткая реплика (не «ок»/«да») без явной просьбы о развёрнутости
    if "response_length" not in updates:
        stripped = user_text.strip()
        if (
            4 <= len(stripped) <= 32
            and "\n" not in stripped
            and not any(p in t for p in _LENGTH_LONG)
        ):
            updates["response_length"] = "short"

    return updates


def format_style_block(prefs: dict[str, str]) -> str | None:
    if not prefs:
        return None
    lines: list[str] = [
        "\n\n=== ПРЕДПОЧТЕНИЯ ПОЛЬЗОВАТЕЛЯ В ЭТОМ ЧАТЕ (обязательно учти) ===",
    ]
    rl = prefs.get("response_length", "")
    if rl == "short":
        lines.append(
            "Длина: отвечай сжато — 1–2 коротких абзаца, только суть; не раздувай вступление и итог."
        )
    elif rl == "long":
        lines.append(
            "Длина: развёрнутый ответ — 4–5 абзацев где уместно, больше контекста, примеров и нюансов."
        )

    tone = prefs.get("tone", "")
    if tone == "serious":
        lines.append(
            "Тон: без шуток, иронии и лишних метафор; дружеливо, но по делу; эмодзи минимум (0–1) или без них."
        )

    lang = prefs.get("language_level", "")
    if lang == "simple":
        lines.append(
            "Язык: простые формулировки, поясняй термины коротко; избегай канцелярита без нужды."
        )

    if len(lines) <= 1:
        return None
    lines.append("Остальные правила персонажа Кузьма сохраняй, кроме прямого конфликта с блоком выше.")
    return "\n".join(lines)
