from __future__ import annotations

import re
from collections import OrderedDict

_FLAGS = re.IGNORECASE | re.UNICODE
_WORD_EXT = r"[а-яёa-z]*"


def _compile_token(token: str) -> re.Pattern[str]:
    token = token.strip()
    if not token:
        return re.compile(r"$^", _FLAGS)
    if "*" in token:
        base = re.escape(token.replace("*", ""))
        return re.compile(rf"\b{base}{_WORD_EXT}\b", _FLAGS)
    if " " in token:
        return re.compile(re.escape(token), _FLAGS)
    return re.compile(rf"\b{re.escape(token)}\b", _FLAGS)


_CATEGORY_TOKENS: OrderedDict[str, tuple[str, ...]] = OrderedDict(
    [
        (
            "юмор",
            (
                "юмор",
                "смех",
                "шутк*",
                "прикол*",
                "мем*",
                "угар*",
                "ржак*",
                "стендап*",
                "comedy",
                "lol",
                "хахаха",
                "фейл*",
                "fail",
                "roast",
                "ситком",
                "комеди*",
            ),
        ),
        (
            "новости",
            (
                "новост*",
                "событи*",
                "происшестви*",
                "breaking news",
                "срочно",
                "эксклюзив*",
                "тренд*",
                "trending",
                "viral",
                "вирусн*",
                "репортаж*",
                "расследовани*",
                "инцидент*",
                "слух*",
                "инсайд*",
                "утечк*",
            ),
        ),
        (
            "игры",
            (
                "игр*",
                "геймпле*",
                "гейм*",
                "gaming",
                "games",
                "прохождени*",
                "летсплей*",
                "let's play",
                "патч*",
                "релиз*",
                "инди*",
                "indie",
                "battle royale",
                "шутер*",
                "rpg",
                "mmorpg",
                "roguelike",
                "ps5",
                "xbox",
                "nintendo",
                "steam",
                "киберспорт*",
                "esport*",
                "турнир*",
                "чит*",
                "баг*",
                "эксплойт*",
            ),
        ),
        (
            "онлайн",
            (
                "онлайн*",
                "интернет*",
                "соцсет*",
                "тикток*",
                "tiktok",
                "ютуб*",
                "youtube",
                "реддит*",
                "reddit",
                "twitter",
                "вконтакте",
                "челлендж*",
                "challenge",
                "дискорд*",
                "discord",
                "telegram",
                "хейт*",
                "токсик*",
                "фандом*",
                "fandom",
            ),
        ),
        (
            "twitch",
            (
                "twitch",
                "твич*",
                "стрим*",
                "трансляци*",
                "донат*",
                "donation",
                "сабатон*",
                "subathon",
                "raid",
                "рейд*",
                "хайлайт*",
                "highlight*",
                "клип*",
                "подписчик*",
                "follower*",
            ),
        ),
        (
            "аниме",
            (
                "аниме*",
                "anime",
                "манга*",
                "manga",
                "ранобэ*",
                "light novel",
                "сейю*",
                "seiyuu",
                "сёнэн",
                "сэйнэн",
                "shounen",
                "seinen",
                "исекай*",
                "isekai",
                "косплей*",
                "cosplay*",
                "опенинг*",
                "эндинг*",
            ),
        ),
        (
            "фильмы",
            (
                "фильм*",
                "кино*",
                "film*",
                "movie*",
                "cinema*",
                "трейлер*",
                "trailer*",
                "премьер*",
                "оскар*",
                "oscar*",
                "нетфликс*",
                "netflix",
                "disney",
                "hbo",
                "marvel",
                "dc",
                "сиквел*",
                "sequel*",
                "приквел*",
                "ремейк*",
                "remake*",
                "box office",
            ),
        ),
        (
            "мультики",
            (
                "мультик*",
                "мультфильм*",
                "анимаци*",
                "cartoon*",
                "animation*",
                "пиксар*",
                "pixar",
                "дисней*",
                "гибли*",
                "ghibli",
                "мультсериал*",
                "аркейн*",
                "arcane",
            ),
        ),
        (
            "отношения",
            (
                "отношени*",
                "любовь",
                "романтик*",
                "расставани*",
                "измен*",
                "брак*",
                "развод*",
                "знакомств*",
                "свидани*",
                "тиндер*",
                "tinder",
                "red flag",
                "красный флаг",
            ),
        ),
        (
            "скандал",
            (
                "скандал*",
                "хайп*",
                "разоблачени*",
                "конфликт*",
                "драм*",
                "фьюд*",
                "feud*",
                "cancel culture",
                "отмен*",
                "пранк*",
                "prank*",
                "провокаци*",
                "слит* видео",
            ),
        ),
        (
            "ужасы",
            (
                "ужас*",
                "хоррор*",
                "horror*",
                "страшилк*",
                "мистик*",
                "паранормальн*",
                "призрак*",
                "ghost*",
                "крипипаст*",
                "creepypasta",
                "scp",
                "триллер*",
                "thriller*",
                "nosleep",
                "scary*",
                "urban legend",
            ),
        ),
    ]
)

CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(_compile_token(token) for token in tokens)
    for category, tokens in _CATEGORY_TOKENS.items()
}


def match_categories(text: str) -> dict[str, list[str]]:
    body = text or ""
    out: dict[str, list[str]] = {}
    for category, patterns in CATEGORY_PATTERNS.items():
        hits: list[str] = []
        for pat in patterns:
            for m in pat.finditer(body):
                hit = m.group(0).strip()
                if hit:
                    hits.append(hit)
        if hits:
            out[category] = hits
    return out


def score_text(text: str) -> dict[str, int]:
    matched = match_categories(text)
    scored = {category: len(hits) for category, hits in matched.items() if hits}
    sorted_items = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_items)
