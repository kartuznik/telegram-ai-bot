"""Самодиагностика бота для админов (/selftest)."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import app.scenario_simulator as scenario_simulator

from app.config import Config
from app.database import get_connection
from app.llm_agent import LLMAgent
from app.user_anchors import classify_anchor_command
from app.user_templates import classify_save_template_command

logger = logging.getLogger(__name__)

_SELFTEST_CONCIERGE_UID = -9_000_001
_SELFTEST_SCENARIO_UID = -8_000_002

REQUIRED_TABLES = frozenset(
    {
        "users",
        "conversations",
        "user_preferences",
        "user_templates",
        "conversation_anchors",
        "draft_posts",
    }
)


class BotSelfTest:
    def __init__(self, config: Config, agent: LLMAgent | None = None) -> None:
        self.config = config
        self.agent = agent

    def test_template_commands(self) -> dict[str, Any]:
        cases = ("сохрани", "сохрани это")
        for phrase in cases:
            ok, reason = classify_save_template_command(phrase)
            if not ok:
                return {
                    "status": "fail",
                    "message": f"Шаблон: «{phrase}» не распознался ({reason})",
                }
        ok_z, z_reason = classify_save_template_command("запомни")
        if ok_z:
            return {
                "status": "fail",
                "message": f"Шаблон: одно «запомни» ошибочно ушло в шаблон ({z_reason})",
            }
        return {
            "status": "pass",
            "message": "Шаблоны: «сохрани» / «сохрани это» ловятся; «запомни» не в шаблоны ✓",
        }

    def test_anchor_commands(self) -> dict[str, Any]:
        m1, r1, _ = classify_anchor_command("запомни этот момент")
        if not m1 or not str(r1).startswith("create"):
            return {
                "status": "fail",
                "message": f"Якорь: «запомни этот момент» → {m1!r} / {r1!r}",
            }
        m2, r2, t2 = classify_anchor_command("якорь: тестовая тема")
        if not m2 or not str(r2).startswith("create"):
            return {
                "status": "fail",
                "message": f"Якорь: «якорь: тема» → {m2!r} / {r2!r}",
            }
        if not (t2 and "тест" in t2.lower()):
            return {
                "status": "fail",
                "message": f"Якорь: название после двоеточия не извлеклось: {t2!r}",
            }
        return {
            "status": "pass",
            "message": "Якоря: «запомни этот момент» и «якорь: …» на месте, как шпаргалка на холодильнике 🔖",
        }

    def test_concierge_triggers(self) -> dict[str, Any]:
        pos = LLMAgent._concierge_intent("подбери варианты билетов в театр на субботу")
        neg = LLMAgent._concierge_intent("что такое теорема пифагора")
        if not pos:
            return {
                "status": "fail",
                "message": "Консьерж: позитивный кейс не сработал (должен быть intent=True)",
            }
        if neg:
            return {
                "status": "fail",
                "message": "Консьерж: негативный кейс ошибочно дал intent=True",
            }
        return {
            "status": "pass",
            "message": "Консьерж: «подбери билеты» — да, «что такое теорема» — нет. Разум включён 🧠✨",
        }

    def test_database(self) -> dict[str, Any]:
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            names = {str(r["name"]) for r in rows}
        except Exception as exc:
            return {"status": "fail", "message": f"БД: не открылась — {exc}"}
        missing = sorted(REQUIRED_TABLES - names)
        if missing:
            return {
                "status": "fail",
                "message": f"БД: нет таблиц: {', '.join(missing)}",
            }
        return {
            "status": "pass",
            "message": f"БД: все нужные таблицы на месте ({len(REQUIRED_TABLES)} шт.) — SQLite не зевает 🗄️",
        }

    def test_api_connections(self) -> dict[str, Any]:
        key = (self.config.openai_api_key or "").strip()
        if len(key) < 20:
            return {"status": "fail", "message": "OpenAI: ключ подозрительно короткий или пустой"}
        if not (key.startswith("sk-") or key.startswith("sk-proj-")):
            return {
                "status": "fail",
                "message": "OpenAI: ключ не похож на sk-… / sk-proj-… (проверь .env)",
            }
        parts = ["OpenAI: ключ на вид живой (формат ок), без сетевого пинга — так и задумано 🤫"]
        tv = self.config.tavily_api_key
        if tv and len(str(tv).strip()) >= 8:
            parts.append("Tavily: ключ задан, длина нормальная")
        else:
            parts.append("Tavily: не задан или короткий — веб-поиск может спать, это не баг сонли 🌙")
        return {"status": "pass", "message": " ".join(parts)}

    def test_json_buttons(self) -> dict[str, Any]:
        sample = (
            "Краткий ответ по делу.\n\n"
            '```json\n{"buttons": [{"text": "Шаг 1 📋", "callback_data": "ask_followup"}, '
            '{"text": "Ещё детали 📚", "callback_data": "summarize"}]}\n```'
        )
        try:
            clean, buttons, _is_gen = LLMAgent._extract_buttons(sample)
        except Exception as exc:
            return {"status": "fail", "message": f"JSON-кнопки: парсер упал — {exc}"}
        if len(buttons) < 1:
            return {"status": "fail", "message": "JSON-кнопки: массив пустой после разбора"}
        for b in buttons:
            if not b.get("text") or not b.get("callback_data"):
                return {"status": "fail", "message": f"JSON-кнопки: битая кнопка {b!r}"}
            cd = str(b["callback_data"])
            try:
                cd.encode("ascii")
            except UnicodeEncodeError:
                return {
                    "status": "fail",
                    "message": f"JSON-кнопки: callback_data не латиница: {cd!r}",
                }
        if "```json" in clean.lower():
            return {"status": "fail", "message": "JSON-кнопки: забор ```json``` не вычистился из текста"}
        if self.agent and self.agent.concierge_enabled:
            merged, _ = self.agent._apply_concierge_layer(
                _SELFTEST_CONCIERGE_UID,
                "подбери билеты в кино",
                list(buttons),
                False,
            )
            if not any(x.get("callback_data") == "concierge_run" for x in merged):
                return {
                    "status": "fail",
                    "message": "Консьерж-слой: не добавил concierge_run для действенного запроса",
                }
            self.agent.consume_pending_concierge(_SELFTEST_CONCIERGE_UID)
        return {
            "status": "pass",
            "message": f"Кнопки: из тестового JSON выловил {len(buttons)} шт., callback латиницей — как учили 📎",
        }

    def test_scenario_simulator(self) -> dict[str, Any]:
        pos = (
            "Что если я вложу сто тысяч рублей в акции сейчас при такой волатильности рынка?",
            "Стоит ли покупать квартиру в этом году при высоких ставках по ипотеке и неопределённости?",
            "Какие варианты развития событий если я решу уволиться через полгода и сменить сферу?",
        )
        for p in pos:
            ok, topic, _params = scenario_simulator.classify_scenario_request(p)
            if not ok or not (topic or "").strip():
                return {
                    "status": "fail",
                    "message": f"🎲 Симулятор: позитив не распознан ({p[:56]}…)",
                }

        neg = (
            "Что такое диверсификация простыми словами для новичка в инвестициях?",
            "Найди дешёвые отели в Сочи на июль без посредников и сравни цены на букинг",
            "что если",
        )
        for p in neg:
            ok, _, _ = scenario_simulator.classify_scenario_request(p)
            if ok:
                return {
                    "status": "fail",
                    "message": f"🎲 Симулятор: ложное срабатывание на: {p[:56]}…",
                }

        concierge_only = (
            "Найди дешёвые билеты на самолёт Москва Сочи завтра утром без пересадок, "
            "подбери варианты и сравни цены на разных сайтах пожалуйста"
        )
        ok_c, _, _ = scenario_simulator.classify_scenario_request(concierge_only)
        if ok_c:
            return {
                "status": "fail",
                "message": "🎲 Симулятор: перехватил чистый консьерж-запрос без «что если» — непорядок",
            }

        mix = (
            "Что если я найду отели в Сочи сам через интернет без агентов и сэкономлю бюджет?"
        )
        ok_m, _, _ = scenario_simulator.classify_scenario_request(mix)
        if not ok_m:
            return {
                "status": "fail",
                "message": "🎲 Симулятор: «что если» + поиск должен остаться сценарием",
            }

        sample = (
            "###SCEN_O\n"
            "__TEST_O__ ~40% риск фактор исход\n"
            "###SCEN_R\n"
            "__TEST_R__ баланс вероятность исход\n"
            "###SCEN_P\n"
            "__TEST_P__ негативный исход риски\n"
        )
        o, r_, p = scenario_simulator.parse_scenario_blocks(sample)
        if "__TEST_O__" not in o or "__TEST_R__" not in r_ or "__TEST_P__" not in p:
            return {
                "status": "fail",
                "message": "🎲 Симулятор: маркеры ###SCEN_O/R/P разобрались криво",
            }

        sid = scenario_simulator.put_session(
            _SELFTEST_SCENARIO_UID,
            "orig_q",
            "opt_body",
            "real_body",
            "pess_body",
            "intro_full",
        )
        try:
            sess = scenario_simulator.get_session(_SELFTEST_SCENARIO_UID, sid)
            if not sess or sess.realist != "real_body":
                return {
                    "status": "fail",
                    "message": "🎲 Симулятор: get_session не вернул то, что положили в кэш",
                }
            if scenario_simulator.get_session(_SELFTEST_SCENARIO_UID + 7, sid) is not None:
                return {
                    "status": "fail",
                    "message": "🎲 Симулятор: чужой user_id прочитал сессию — дыра в безопасности",
                }
            raw = scenario_simulator._sessions.get(sid)
            if raw is None or time.time() - raw.created > 90.0:
                return {
                    "status": "fail",
                    "message": "🎲 Симулятор: поле created/TTL ведёт себя странно",
                }
        finally:
            scenario_simulator._sessions.pop(sid, None)

        if not scenario_simulator.is_scenario_callback("scen:o:deadbeef"):
            return {"status": "fail", "message": "🎲 Симулятор: scen:o: не узнали как свой callback"}
        if scenario_simulator.is_scenario_callback("tpl:o:1"):
            return {"status": "fail", "message": "🎲 Симулятор: tpl: ошибочно принят за scen:"}
        if scenario_simulator.is_scenario_callback("anchor:o:1"):
            return {"status": "fail", "message": "🎲 Симулятор: anchor: ошибочно принят за scen:"}

        a1, s1 = scenario_simulator.parse_scenario_callback("scen:r:a1b2c3d4")
        if a1 != "r" or s1 != "a1b2c3d4":
            return {
                "status": "fail",
                "message": f"🎲 Симулятор: parse scen:r → {(a1, s1)!r}",
            }
        a2, s2 = scenario_simulator.parse_scenario_callback("scen:x:a1b2c3d4")
        if a2 or s2:
            return {"status": "fail", "message": "🎲 Симулятор: невалидный action не отфильтрован"}

        return {
            "status": "pass",
            "message": (
                "🎲 Симулятор: классификация ок, три маркера парсятся, кэш и scen:* колбэки на месте — "
                "кубик не подкручен 🎯"
            ),
        }

    def run_all(self) -> dict[str, dict[str, Any]]:
        specs: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            ("templates", self.test_template_commands),
            ("anchors", self.test_anchor_commands),
            ("concierge", self.test_concierge_triggers),
            ("database", self.test_database),
            ("api_keys", self.test_api_connections),
            ("json_buttons", self.test_json_buttons),
            ("scenario_simulator", self.test_scenario_simulator),
        ]
        out: dict[str, dict[str, Any]] = {}
        for name, fn in specs:
            try:
                res = fn()
            except Exception as exc:
                logger.exception("selftest: %s упал с исключением", name)
                res = {"status": "fail", "message": str(exc)}
            out[name] = res
            logger.info(
                "selftest: %s → %s — %s",
                name,
                res.get("status", "?"),
                (res.get("message") or "")[:500],
            )
        return out

    @staticmethod
    def format_report(results: dict[str, dict[str, Any]]) -> str:
        lines: list[str] = [
            "🤖 Самодиагностика Кузьмы",
            "Проверил себя под микроскопом юмора — вот вердикт:",
            "",
        ]
        ok = sum(1 for r in results.values() if r.get("status") == "pass")
        total = len(results)
        for key, r in results.items():
            st = r.get("status", "?")
            icon = "✅" if st == "pass" else "❌"
            label = {
                "templates": "Шаблоны",
                "anchors": "Якоря",
                "concierge": "Консьерж",
                "database": "База SQLite",
                "api_keys": "Ключи API",
                "json_buttons": "JSON-кнопки",
                "scenario_simulator": "Симулятор сценариев",
            }.get(key, key)
            msg = (r.get("message") or "").strip()
            lines.append(f"{icon} {label} — {msg}")
        lines.append("")
        if ok == total:
            lines.append(
                f"🏆 Итог: {ok}/{total} — как утро после кофе: всё завелось. Можно спокойно катить фичи дальше ☕✨"
            )
        else:
            lines.append(
                f"⚠️ Итог: {ok}/{total} ок, остальное подкрутим — даже роботам иногда нужен второй завтрак 🔧😅"
            )
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3980] + "\n…"
        return text
