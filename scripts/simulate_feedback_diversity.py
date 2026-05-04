"""
Симуляция математики preference + diversity + novelty (как в app/content_editor._pick_draft_item).
Только stdlib + sqlite3, без aiogram.
Запуск: python3 scripts/simulate_feedback_diversity.py
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

# Значения по умолчанию как в load_config() после фикса переобучения
DECAY_RATE = 0.88
FEEDBACK_WINDOW = 20
MIN_PREF = 3
MAX_PREF_GAIN = 0.10
PREF_GAIN_PER_UNIT = 0.15
NOVELTY_BONUS = 0.05
NOVELTY_RECENT = 10

SAME_CATEGORY_WINDOW = 3
NARROW_MIX_WINDOW = 5
NARROW_MIX_CATEGORIES = 2
SAME_CATEGORY_PENALTY = 0.30
OTHER_CATEGORIES_BONUS = 0.20


@dataclass
class Candidate:
    name: str
    category: str
    base_score: float


def feedback_signal(action: str, quality_score: int | None) -> float:
    base = {
        "approved": 0.4,
        "edited": 0.35,
        "rejected": -1.0,
        "expired_content": -0.6,
    }.get((action or "").strip().lower(), 0.0)
    if quality_score is None:
        return base
    q = max(1, min(10, int(quality_score)))
    q_centered = (q - 5.5) / 4.5
    return max(-1.0, min(1.0, 0.7 * base + 0.3 * q_centered))


def category_preferences(conn: sqlite3.Connection, decay: float, window: int) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT action, category, quality_score
        FROM draft_feedback
        ORDER BY id DESC
        LIMIT ?
        """,
        (window,),
    ).fetchall()
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    for i, r in enumerate(rows):
        cat = (str(r["category"] or "").strip().lower() or "other")[:64]
        w = decay**i
        s = feedback_signal(str(r["action"] or ""), r["quality_score"])
        num[cat] = num.get(cat, 0.0) + s * w
        den[cat] = den.get(cat, 0.0) + w
    return {k: (num[k] / den[k]) for k in den if den[k] > 0}


def feedback_category_counts(conn: sqlite3.Connection, window: int) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT category
        FROM draft_feedback
        ORDER BY id DESC
        LIMIT ?
        """,
        (window,),
    ).fetchall()
    c: Counter[str] = Counter()
    for r in rows:
        cat = (str(r["category"] or "").strip().lower() or "other")[:64]
        c[cat] += 1
    return dict(c)


def diversity_multiplier(category: str, recent_categories: list[str]) -> float:
    mult = 1.0
    same_slice = recent_categories[-SAME_CATEGORY_WINDOW:]
    if len(same_slice) == SAME_CATEGORY_WINDOW and len(set(same_slice)) == 1:
        if same_slice[-1] == category:
            mult *= 1.0 - SAME_CATEGORY_PENALTY
    mix_slice = recent_categories[-NARROW_MIX_WINDOW:]
    if len(mix_slice) >= NARROW_MIX_WINDOW and len(set(mix_slice)) <= NARROW_MIX_CATEGORIES:
        if category not in set(mix_slice):
            mult *= 1.0 + OTHER_CATEGORIES_BONUS
    return mult


def pref_mult_for_category(
    category: str,
    pref_raw: float,
    counts: dict[str, int],
) -> tuple[float, float]:
    """Возвращает (pref_gain, pref_mult)."""
    n = int(counts.get(category, 0))
    if n < MIN_PREF:
        return 0.0, 1.0
    raw_delta = PREF_GAIN_PER_UNIT * pref_raw
    pref_gain = max(-MAX_PREF_GAIN, min(MAX_PREF_GAIN, raw_delta))
    return pref_gain, 1.0 + pref_gain


def print_table(rows: list[tuple[str, str, float, float, float, float, float, float, float]]) -> None:
    print(
        "| Раунд | Категория | Базовый | pref_raw | pref_mult | novelty | diversity | final_mult | score |"
    )
    print("|" + "|".join(["---"] * 9) + "|")
    for row in rows:
        print(
            f"| {row[0]} | {row[1]} | {row[2]:.2f} | {row[3]:.3f} | {row[4]:.3f} | "
            f"{row[5]:.3f} | {row[6]:.3f} | {row[7]:.3f} | {row[8]:.2f} |"
        )


def main() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE draft_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            category TEXT,
            quality_score INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO draft_feedback(action, category, quality_score) VALUES (?, ?, ?)",
        ("approved", "игры", 8),
    )
    conn.commit()

    preferences = category_preferences(conn, DECAY_RATE, FEEDBACK_WINDOW)
    counts = feedback_category_counts(conn, FEEDBACK_WINDOW)
    print(f"preference map: {preferences}")
    print(f"feedback counts in window: {counts}")

    # Базовые score близки друг к другу (как сопоставимые pattern scores в проде),
    # иначе при pref_mult=1 доминирует просто самый высокий base без отношения к фиксу preference.
    candidates_pool = [
        Candidate("C1", "игры", 9.05),
        Candidate("C2", "фильмы", 9.35),
        Candidate("C3", "аниме", 9.28),
        Candidate("C4", "новости", 9.22),
    ]
    picked_categories: list[str] = []
    round_rows: list[tuple[str, str, float, float, float, float, float, float, float]] = []

    for i in range(5):
        novelty_set = set(picked_categories[-NOVELTY_RECENT:])
        best_final = -10**9
        best_row: tuple[str, str, float, float, float, float, float, float, float] | None = None

        for cand in candidates_pool:
            pref_raw = preferences.get(cand.category, 0.0)
            pref_gain, pref_mult = pref_mult_for_category(cand.category, pref_raw, counts)
            div_m = diversity_multiplier(cand.category, picked_categories)
            nov_m = (1.0 + NOVELTY_BONUS) if cand.category not in novelty_set else 1.0
            final = cand.base_score * pref_mult * div_m * nov_m
            if final > best_final:
                best_final = final
                best_row = (
                    cand.name,
                    cand.category,
                    cand.base_score,
                    pref_raw,
                    pref_mult,
                    nov_m,
                    div_m,
                    pref_mult * div_m * nov_m,
                    final,
                )

        assert best_row is not None
        round_rows.append((f"Раунд {i + 1}",) + best_row[1:])
        picked_categories.append(best_row[1])

    print()
    print_table(round_rows)
    games_count = picked_categories.count("игры")
    print(f"\nВыбрано категорий: {picked_categories}")
    print(f"'игры' выбраны {games_count} раз(а) из 5")
    g = preferences.get("игры", 0.0)
    print(f"preference['игры'] (один апрув в БД): {g:.3f}")
    pg, pm = pref_mult_for_category("игры", g, counts)
    print(f"для 'игры': feedback_n={counts.get('игры', 0)} pref_gain={pg:.3f} pref_mult={pm:.3f} (ожид. 1.0 при n<3)")
    if games_count <= 2:
        print("OK: «игры» не чаще 2 из 5 (цель 1–2).")
    elif games_count <= 3:
        print("WARN: «игры» 3 раза — приемлемо, но выше цели 1–2.")
    else:
        print("FAIL: «игры» слишком часто доминируют после одного апрува.")


if __name__ == "__main__":
    main()
