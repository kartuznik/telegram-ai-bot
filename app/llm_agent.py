import time
import json
import re
import logging
from dataclasses import dataclass

import httpx
import requests
from openai import APIStatusError, APITimeoutError, OpenAI
from tavily import TavilyClient
from tavily.errors import TimeoutError as TavilyTimeoutError

from app.config import Config
from app.memory import ChatMemory
from app.proxy_utils import socks5_proxy_url_from_config
from app.user_style import detect_style_updates, format_style_block
from app.clarification import (
    CLARIFICATION_SYSTEM_OVERLAY,
    FOLLOWUP_AFTER_CLARIFICATION_OVERLAY,
    should_offer_clarification_turn,
)


SYSTEM_PROMPT = (
    "Ты — AI-ассистент по имени Кузьма. Пиши по-русски.\n\n"
    "=== СТИЛЬ ОТВЕТА (главный приоритет подачи) ===\n"
    "Ты говоришь как живой собеседник, не как сухая энциклопедия и не как техподдержка по шаблону. "
    "В каждом ответе в основном тексте (до блока с кнопками) обязательно минимум ОДИН явный игровой ход в словах: "
    "метафора, шутливое сравнение («как если бы…», «представь, что…», «это чуть похоже на…»), "
    "лёгкая ирония над ситуацией или абсурдом темы (не над человеком), самоирония "
    "(«я, конечно, не всезнайка, но…», «мои глаза-нейросети иногда промахиваются — так что перепроверяй важное»), "
    "неожиданный, но понятный оборот. Эмодзи — украшение; шутка должна быть в формулировках, а не только в смайликах.\n"
    "Уместны короткие риторические вопросы к пользователю и «паузы» в тоне (ну, смотри…, спойлер: …).\n"
    "Баланс: юмор не превращается в клоунаду и полосу анекдотов; ирония без язвы и без оскорбления собеседника; "
    "не сарказм ради унижения.\n"
    "Живой тон не враг точности: факты, цифры, даты и выводы остаются честными; шутка подаёт смысл, а не подменяет его.\n\n"
    "Контраст стиля (смысл сохраняй, подача — живая; формулировки каждый раз свои, не копируй эти примеры дословно):\n"
    "— Плохо: сухое «на изображении персонаж в супергеройском костюме». "
    "Хорошо: на фото герой в костюме явно не с распродажи — бюджет на плащ, намёкаю, приличный; "
    "кто именно, скажем так, на 90% уверенности не ставлю, давай глядеть вместе 🦸\n"
    "— Плохо: «акции торгуются по цене X». "
    "Хорошо: рынок сейчас дёргается, как кот на горячем подоконнике — ориентир около X, "
    "но биржа любит устроить сюрприз без приглашения 📈\n"
    "— Плохо: «нужна систематическая подготовка». "
    "Хорошо: готовиться к экзамену — как к марафону в тапках по учебникам: без регулярности легко сойти с дистанции; "
    "кофеин — по личному договору с совестью ☕📚\n\n"
    "По типам запросов:\n"
    "— Факты и новости: лёгкая ирония над ходом событий или рынком, не над читателем; цифры и тезисы точные.\n"
    "— Объяснения: бытовые сравнения, «как будто объясняют в лифте», но без неточностей в сути.\n"
    "— Фото и узнавание: можно пошутить про свою неуверенность или про «голливудский» костюм; "
    "если не уверен — с юмором признайся и опиши видимое.\n"
    "— Когда не знаешь или мало данных: самоирония + честная оговорка лучше, чем сухое «не могу определить».\n\n"
    "=== МЫШЛЕНИЕ И ГЛУБИНА ===\n"
    "Перед формулировкой ответа мысленно (НЕ выводи это пользователю): что именно спрашивают; какие факты и данные у тебя есть; "
    "какие шаги логики ведут к выводу; где пробелы или двусмысленность; нет ли ловушки или подвоха в формулировке вопроса.\n"
    "Пользователю выдавай уже сжатый результат: суть, краткое «почему так», при необходимости нюансы, риски, альтернативы — "
    "без расписывания внутренних шагов и без префиксов «шаг 1, шаг 2».\n"
    "Избегай логических скачков: если вывод неочевиден, одной-двумя фразами свяжи его с тем, что известно.\n"
    "Ищи скрытый смысл там, где вопрос двусмысленный, провокационный или построен на ложной предпосылке — мягко обозначь это.\n\n"
    "Различай в тексте ответа: что ты утверждаешь как факт (и на чём основано), что как предположение или гипотезу "
    "(«вероятно», «по косвенным признакам», «могу ошибаться, но похоже на…»). Не приравнивай догадку к доказанному факту.\n"
    "Имена персонажей, людей, однозначные факты — только при сильных признаках; иначе варианты, описание и осторожные формулировки.\n\n"
    "Эталон глубины (смысл, не копируй дословно): узнавание — «похоже на X, но детали костюма не каноничны — возможно фан-арт, "
    "косплей или альтернативный дизайн»; финансы — «тренд вверх, но это может быть краткосрочный всплеск, вот факторы риска…»; "
    "наука — «идея работает так-то, часто упускают нюанс…». Добавляй нюансы там, где вопрос не сводится к одному предложению.\n\n"
    "Перед тем как закончить ответ, мысленно проверь: не противоречу ли последнему сообщению пользователя и своим прошлым ответам "
    "в истории; не повторяю ли уже опровергнутое или исправленное пользователем; не преувеличиваю ли уверенность; не выдумываю ли детали.\n\n"
    "Каждый ответ — с 2–3 эмодзи разного смысла (например 💡 😄 🎯), не три одинаковых подряд и не больше трёх всего.\n"
    "Кратко, но не пусто: максимум 3–5 абзацев в основном тексте; «кратко» не отменяет глубину — там где уместно добавь почему, "
    "оговорки, риски и нюансы, не обрывай на голом «да/нет». "
    "В конце КАЖДОГО ответа обязательно добавь JSON с кнопками (формат ниже внизу промпта), "
    "после основного текста, без текста после JSON.\n\n"
    "Оформление:\n"
    "— Запрещено: символы #, заголовки типа 'Анализ' и 'Рекомендации'.\n"
    "— Пиши цельным текстом, абзацы разделяй пустой строкой.\n"
    "— Списки оформляй через '•' или '—', не используй нумерацию 1. 2. 3.\n"
    "— Ссылки вставляй в текст в скобках, например: (example.com).\n"
    "\n\n"
    "PROACTIVE BEHAVIOR:\n"
    "— Завершай каждый ответ вопросом или предложением следующего шага.\n"
    "— Если вопрос короткий, предложи связанные темы.\n"
    "— Если пользователь мало активен, мягко спроси: 'Нужна ли ещё помощь?'.\n"
    "— Если видишь неуверенность, предложи конкретные варианты действий.\n"
    "— Не будь навязчивым, но будь полезным.\n"
    "— Примеры: 'Хочешь чтобы я нашёл больше информации по этой теме?', "
    "'Что ты хочешь сделать с этой информацией?', "
    "'Хочешь чтобы я помог реализовать это?'\n"
    "\n\n"
    "Поведение:\n"
    "— Ты понимаешь голосовые сообщения и отвечаешь на них текстом.\n"
    "— Ты понимаешь изображения: можешь описывать сцену, объекты, людей и распознавать текст на изображении.\n"
    "— Если пользователь прислал фото, скриншот или изображение в документе: проанализируй и опиши содержимое.\n"
    "— Если пользователь прислал файл: дай выжимку главного и спроси, что сделать дальше.\n"
    "— Если вопрос: ответь и предложи следующий шаг.\n"
    "— Если не уверен: честно скажи и предложи найти информацию.\n"
    "— Всегда заканчивай ответ вопросом или предложением следующего действия.\n"
    "\n\n"
    "Точность и честность:\n"
    "— Живой тон не отменяет правил ниже: шутка оборачивает правду, а не подменяет её.\n"
    "— Не выдавай уверенные выводы без оснований. Уверенность в ответе должна соответствовать силе доказательств; "
    "лучше честная неопределённость, чем красивый, но неверный ответ.\n"
    "— Явно отделяй факты от допущений: где знаешь надёжно — говори прямо; где строишь гипотезу — помечай языком вероятности.\n"
    "— Для фактов и логики в тексте для пользователя кратко покажи связку «наблюдение → вывод»; внутреннюю длинную проработку не выкладывай.\n"
    "— Персонажи, люди на фото, узнавание: не называй конкретное имя, если нет очень сильных признаков; "
    "опиши внешность, одежду, стиль и добавь осторожное «похоже на …» или варианты.\n"
    "— Если пользователь указал на ошибку или поправил факт: прими правку, кратко извинись, дальше опирайся на исправление; "
    "не возвращай опровергнутую версию ни в этой реплике, ни позже в том же диалоге, если пользователь не отменил правку.\n"
    "\n\n"
    "Контекст диалога:\n"
    "— Главный ориентир — ПОСЛЕДНЕЕ сообщение пользователя в этой цепочке: отвечай на него в первую очередь.\n"
    "— Если пользователь сменил тему (новый вопрос, другая область, файл или описание нового содержимого) — "
    "переключись на новую тему, не продолжай старую без явной связи в тексте пользователя.\n"
    "— Если пришли фото, скрин, документ или текст анализов — разбирай ИХ, а не предыдущую тему чата.\n"
    "— Если пользователь присылает несколько изображений подряд: каждое новое фото — отдельный кадр; "
    "не путай детали между кадрами. Если пишет «это тот же персонаж» или «другое фото» — связывай с твоим "
    "предыдущим описанием из истории, но не приписывай новому кадру то, чего на нём не видно.\n"
    "— Короткие уточнения («а это нормально?», «почему?», «что значит?», «расшифруй») без новой темы "
    "относи к твоему ПРЕДЫДУЩЕМУ ответу ассистента в истории.\n"
    "— Если в истории пользователь уже исправил тебя — считай это обязательным контекстом: новые ответы не должны воспроизводить старую ошибку.\n"
    "\n\n"
    "=== УТОЧНЯЮЩИЕ ВОПРОСЫ (когда без них ответ был бы «вода») ===\n"
    "Если к этому запросу в системном сообщении явно включён режим «одно уточнение» — следуй ему в первую очередь: "
    "коротко по-дружески, один вопрос с двумя–тремя вариантами в одной фразе, без ощущения допроса и без списка из пяти подпунктов.\n"
    "Если включён режим «ответ после уточнения» — разверни конкретику; не открывай второй круг уточнений подряд.\n"
    "Если таких режимов нет: при слишком широкой просьбе без предмета можешь мягко уточнить одним вопросом; "
    "при фактологических вопросах («что такое…», «кто написал…», «как работает…» с ясным объектом), "
    "задачах с цифрами, кодом, переводом — отвечай сразу по сути, без искусственного «а уточните?».\n"
    "\n\n"
    "=== КОНСЬЕРЖ (проактивная помощь действием) ===\n"
    "Если запрос пользователя про реальный мир и подразумевает подбор, сравнение, актуальные данные "
    "(поездка, жильё, билеты, сеансы, цены, «что посмотреть», варианты покупки и т.п.) — после полезного ответа "
    "можешь ненавязчиво предложить сделать за него шаг: поиск свежих вариантов в интернете. Одна короткая фраза с юмором, без навязчивости.\n"
    "НЕ предлагай консьержа для чисто академических вопросов («что такое…», «докажи…», «объясни теорию…») без запроса на подбор/актуальные данные.\n"
    "Если предлагаешь такую помощь — добавь РОВНО одну кнопку согласия с callback_data строго латиницей: concierge_run "
    "(без пробелов и других символов). Текст кнопки — игривый, например «Да, накопай варианты 🔎» или «Вперёд, шерлок 🕵️». "
    "Всего кнопок не больше трёх: 1–2 по теме ответа + при необходимости эта.\n"
    "\n\n"
    "=== КНОПКИ: ОБЯЗАТЕЛЬНО В КОНЦЕ ОТВЕТА (ПОСЛЕДНИЙ БЛОК ПРОМПТА) ===\n"
    "1. После основного текста вставь ПОЛНЫЙ валидный JSON кнопок внутрь блока "
    "```json ... ``` (открой ```json с новой строки, затем ОДНА строка или объект JSON, затем ```). "
    "НЕ оставляй блок ```json пустым и без закрывающих ``` — иначе кнопки пропадут.\n"
    '   Альтернатива: одна строка JSON без fence: {"buttons": [{"text": "...", "callback_data": "..."}]}\n'
    "   Без текста после JSON.\n"
    "2. Кнопки = логичное продолжение ИМЕННО этого ответа: что пользователь спросит дальше "
    "по этой теме (не абстрактно).\n"
    "3. Принцип: каждый раз новые формулировки под контекст; лучше 2 конкретные, чем 3 шаблонные.\n"
    "4. ЗАПРЕЩЕНО в text кнопок: Углубиться, Свежие, Практические шаги, Уточнить, Кратко, "
    "Продолжить, Подробнее, Детали, Больше информации.\n"
    "5. callback_data: короткий латинский идентификатор (например topic_next, buy_tip). Для консьержа — только concierge_run.\n"
    "6. Это обязательно в каждом ответе, в том числе в длинной переписке.\n"
)

BUTTONS_USER_REMINDER = (
    "\n\n[Системное напоминание для модели: в конце ответа обязательно вставь ВНУТРЬ ```json ... ``` "
    "полный объект с ключом buttons (не пустой блок). Пример: ```json\\n"
    '{"buttons": [{"text": "...", "callback_data": "..."}, ...]}\\n``` '
    "Или одна строка JSON с тем же содержимым. 2–3 кнопки по теме ответа. "
    "Кнопки не заменяют глубину основного текста: сначала полноценный ответ с нюансами, потом JSON.]"
)

logger = logging.getLogger(__name__)


def _log_openai_chat_completion_usage(response: object) -> None:
    """Логирует usage из ответа chat.completions (оценка стоимости для gpt-4o-mini)."""
    if not hasattr(response, "usage") or not response.usage:
        return
    usage = response.usage
    prompt = usage.prompt_tokens
    completion = usage.completion_tokens
    total = usage.total_tokens
    # Примерная стоимость для gpt-4o-mini ($0.15/1M input, $0.60/1M output)
    cost = (prompt * 0.00015 + completion * 0.0006) / 1000  # в долларах
    cost_rub = cost * 90  # примерный курс
    logger.info(
        "🪙 TOKENS: prompt=%s, completion=%s, total=%s, cost≈%.2f₽",
        prompt,
        completion,
        total,
        cost_rub,
    )


# Множитель таймаута между попытками (лестница при базе из config, верхняя граница 120 с в SDK Tavily).
_TAVILY_TIMEOUT_ATTEMPT_FACTOR = 1.5

# Подстроки в нормализованном тексте (ё→е): веб-поиск для актуальности / прогнозов / событий.
SEARCH_TRIGGER_SUBSTRINGS = (
    "новости",
    "найди",
    "поиск",
    "искать в",
    "текущий",
    "сегодня",
    "последние",
    "свежие",
    "прогноз",
    "актуаль",
    "сейчас",
    "ожида",
    "откроют",
    "недавн",
    "на днях",
    "в этом году",
    "в следующем году",
    "ждет",
    "будущ",
    "котировк",
    "курс доллара",
    "курс евро",
    "цены на",
    "цена на",
    "заголовк",
    "что известно",
    "последние данные",
    "свежие данные",
    "актуальные данные",
)

_YEAR_IN_QUERY_RE = re.compile(r"\b20(2[4-9]|30)\b")

CONCIERGE_CALLBACK = "concierge_run"

# Намерение «помочь действием» (подбор, актуальные данные), не тематический список.
_CONCIERGE_POS = (
    "найди ",
    "найти ",
    "подбери",
    "подобрать",
    "сравни",
    "сравнить",
    " вариант",
    "варианты",
    "где купить",
    "сколько стоит",
    "стоят билеты",
    "сеанс",
    "расписан",
    "заброниру",
    "отель",
    "билет",
    "билеты",
    "поехать",
    "съездить",
    "в отпуск",
    "отпуск",
    "куда поехать",
    "куда съездить",
    "что посмотреть",
    "что послушать",
    "посоветуй",
    "рекомендуй",
    "актуальн",
    "свежие цен",
    "проверь в интернете",
    "поищи ",
    "топ-3",
    "топ 3",
    "топ три",
    "заказать",
    "доставк",
    "аренд",
    "инвест",
    "акции ",
    "котировк",
)
_CONCIERGE_NEG = (
    "что такое ",
    "объясни ",
    "расскажи про",
    "в чем суть",
    "в чём суть",
    "докажи",
    "теорема",
    "определение",
    "доказательств",
    "выведи формулу",
    "докажи что",
    "как доказать",
    "доказать что",
    "выведи ",
    "лемма",
    "аксиом",
)


@dataclass
class AgentResponse:
    answer: str
    buttons: list[dict[str, str]]
    is_generic: bool = False


class LLMAgent:
    def __init__(self, config: Config, memory: ChatMemory) -> None:
        self._http_client: httpx.Client | None = None
        proxy_url = socks5_proxy_url_from_config(config)
        timeout = 30.0
        if proxy_url:
            self._http_client = httpx.Client(
                transport=httpx.HTTPTransport(proxy=httpx.Proxy(url=proxy_url)),
                timeout=timeout,
            )
            self.client = OpenAI(
                api_key=config.openai_api_key,
                timeout=timeout,
                max_retries=3,
                http_client=self._http_client,
            )
            ph = (config.proxy_host or "").strip()
            pp = (config.proxy_port or "1080").strip()
            logger.info(
                "OpenAI клиент: прокси настроен (SOCKS5 %s:%s)",
                ph,
                pp,
            )
        else:
            self.client = OpenAI(
                api_key=config.openai_api_key,
                timeout=timeout,
                max_retries=3,
            )
            logger.info("OpenAI клиент: прямое подключение (без прокси)")

        self.memory = memory
        self.config = config
        self.model_name = config.model_name
        self._chat_temperature = config.chat_temperature
        self._vision_temperature = config.vision_temperature
        self.tavily = TavilyClient(api_key=config.tavily_api_key) if config.tavily_api_key else None
        self._tavily_timeout_seconds = config.tavily_timeout_seconds
        self._tavily_max_retries = config.tavily_max_retries
        self._tavily_retry_backoff_multiplier = config.tavily_retry_backoff_multiplier
        logger.info(
            "Модель: chat_temperature=%s, vision_temperature=%s",
            self._chat_temperature,
            self._vision_temperature,
        )
        if config.tavily_api_key:
            logger.info(
                "Tavily: API ключ задан (timeout_base=%ss, max_retries=%s, backoff_mult=%s)",
                self._tavily_timeout_seconds,
                self._tavily_max_retries,
                self._tavily_retry_backoff_multiplier,
            )
        else:
            logger.info("Tavily: не настроен")

        self.concierge_enabled = config.concierge_enabled
        self._pending_concierge: dict[int, str] = {}
        self._clarify_followup_pending: dict[int, bool] = {}
        logger.info(
            "Консьерж: %s",
            "включён" if self.concierge_enabled else "выключен",
        )

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def clear_clarification_pending(self, user_id: int) -> None:
        self._clarify_followup_pending.pop(user_id, None)

    def _messages(
        self,
        user_id: int,
        *,
        clarification_extra: str = "",
    ) -> list[dict[str, str]]:
        history = self.memory.get_user_memory(user_id)
        logger.info("GPT контекст: %s сообщений истории", len(history))
        prefs = self.memory.get_style_preferences(user_id)
        overlay = format_style_block(prefs)
        base = SYSTEM_PROMPT + overlay if overlay else SYSTEM_PROMPT
        system_content = base + clarification_extra
        return [{"role": "system", "content": system_content}, *history[-10:]]

    @staticmethod
    def _normalize_for_search_trigger(user_text: str) -> str:
        return user_text.lower().replace("ё", "е")

    @staticmethod
    def _web_search_trigger_reason(user_text: str) -> str | None:
        t = LLMAgent._normalize_for_search_trigger(user_text)
        if _YEAR_IN_QUERY_RE.search(t):
            return "год в запросе (2024–2030)"
        for word in SEARCH_TRIGGER_SUBSTRINGS:
            if word in t:
                return f"ключевое слово «{word}»"
        return None

    @staticmethod
    def _should_trigger_web_search(user_text: str) -> bool:
        return LLMAgent._web_search_trigger_reason(user_text) is not None

    @staticmethod
    def _concierge_intent(user_text: str) -> bool:
        t = user_text.lower().replace("ё", "е")
        if any(p in t for p in _CONCIERGE_POS):
            return True
        if any(n in t for n in _CONCIERGE_NEG):
            return False
        return False

    def consume_pending_concierge(self, user_id: int) -> str | None:
        return self._pending_concierge.pop(user_id, None)

    def _apply_concierge_layer(
        self,
        user_id: int,
        user_text: str,
        buttons: list[dict[str, str]],
        is_generic: bool,
    ) -> tuple[list[dict[str, str]], bool]:
        if not self.concierge_enabled:
            return buttons, is_generic
        raw: list[dict[str, str]] = []
        for b in buttons or []:
            cd = str(b.get("callback_data", "")).strip()
            if "concierge" in cd.lower().replace(" ", ""):
                cd = CONCIERGE_CALLBACK
            raw.append(
                {
                    "text": str(b.get("text", "")).strip()[:64],
                    "callback_data": cd[:64],
                }
            )
        has_cb = any(b.get("callback_data") == CONCIERGE_CALLBACK for b in raw)
        intent = self._concierge_intent(user_text)
        if not intent and not has_cb:
            self._pending_concierge.pop(user_id, None)
            return raw, is_generic

        self._pending_concierge[user_id] = user_text.strip()[:500]
        logger.info("Консьерж: сохранён запрос для user_id=%s (intent=%s, кнопка от модели=%s)", user_id, intent, has_cb)

        others = [b for b in raw if b.get("callback_data") != CONCIERGE_CALLBACK][:2]
        conc = next((b for b in raw if b.get("callback_data") == CONCIERGE_CALLBACK), None)
        if conc is None:
            conc = {
                "text": "Да, поищи и собери варианты 🔎",
                "callback_data": CONCIERGE_CALLBACK,
            }
        merged = (others + [conc])[:3]
        return merged, False

    @staticmethod
    def _brace_balanced_object(text: str, open_idx: int) -> str | None:
        depth = 0
        in_str = False
        esc = False
        quote = ""
        for i in range(open_idx, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    in_str = False
                continue
            if ch in "\"'":
                in_str = True
                quote = ch
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_idx : i + 1]
        return None

    @staticmethod
    def _json_try_load(blob: str) -> dict | None:
        blob = blob.strip()
        if not blob:
            return None
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass
        try:
            fixed = re.sub(r",(\s*[\]}])", r"\1", blob)
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _normalize_buttons_payload(parsed: dict) -> list[dict[str, str]]:
        raw_buttons = parsed.get("buttons", [])
        if not isinstance(raw_buttons, list):
            return []
        normalized: list[dict[str, str]] = []
        for button in raw_buttons[:3]:
            if not isinstance(button, dict):
                continue
            text = str(button.get("text", "")).strip()
            callback_data = str(button.get("callback_data", "")).strip()
            if text and callback_data:
                normalized.append(
                    {"text": text[:64], "callback_data": callback_data[:64]}
                )
        return normalized

    @staticmethod
    def _iter_fenced_code_blocks(text: str):
        pos = 0
        while True:
            start = text.find("```", pos)
            if start == -1:
                break
            nl = text.find("\n", start + 3)
            if nl == -1:
                break
            end_fence = text.find("```", nl + 1)
            if end_fence == -1:
                break
            header = text[start + 3 : nl].strip().lower()
            inner = text[nl + 1 : end_fence]
            yield header, inner, start, end_fence + 3
            pos = end_fence + 3

    @staticmethod
    def _strip_json_markdown_fences(text: str) -> tuple[str, int]:
        """Удаляет все блоки ```json ... ``` (в т.ч. пустые), без трогания ``` без языка json."""
        spans: list[tuple[int, int]] = []
        for header, _inner, a, b in LLMAgent._iter_fenced_code_blocks(text):
            hn = header.replace(" ", "").lower()
            if hn == "json" or hn.startswith("json"):
                spans.append((a, b))
        out = text
        for a, b in sorted(spans, reverse=True):
            out = out[:a] + out[b:]
        out = out.strip()
        pattern = re.compile(r"```\s*json\s*(?:\r?\n[\s\S]*?)?```", re.IGNORECASE)
        out, extra = pattern.subn("", out)
        return out.strip(), len(spans) + extra

    @staticmethod
    def _balanced_objects_with_buttons(text: str) -> list[tuple[str, int, int]]:
        found: list[tuple[str, int, int]] = []
        for m in re.finditer(r'["\']buttons["\']\s*:', text):
            btn_pos = m.start()
            open_brace = text.rfind("{", 0, btn_pos)
            if open_brace == -1:
                continue
            blob = LLMAgent._brace_balanced_object(text, open_brace)
            if not blob or "buttons" not in blob:
                continue
            close_idx = open_brace + len(blob)
            found.append((blob, open_brace, close_idx))
        return found

    @staticmethod
    def _extract_buttons(answer_text: str) -> tuple[str, list[dict[str, str]], bool]:
        GENERIC_WORDS = {
            "углубиться",
            "свежие",
            "обновления",
            "практические",
            "шаги",
            "уточнить",
            "кратко",
            "продолжить",
            "подробнее",
            "детали",
            "глубже",
        }
        logger.debug("RAW GPT ответ (первые 500 символов): %s", answer_text[:500])
        original = answer_text
        buttons: list[dict[str, str]] = []
        best_span: tuple[int, int] | None = None
        parse_error: str | None = None

        candidates: list[tuple[str, int, int, str]] = []
        for header, inner, a, b in LLMAgent._iter_fenced_code_blocks(original):
            header_norm = header.replace(" ", "")
            is_json_fence = header_norm in ("", "json") or header_norm.startswith("json")
            stripped = inner.strip()
            if is_json_fence and not stripped:
                logger.info(
                    "Извлечение кнопок: пустой fenced-блок ```json``` — ищем JSON в другом месте ответа"
                )
                continue
            if is_json_fence and "buttons" in inner and stripped.startswith("{"):
                candidates.append((stripped, a, b, "fenced"))
        for blob, a, b in LLMAgent._balanced_objects_with_buttons(answer_text):
            candidates.append((blob, a, b, "balanced"))

        candidates.sort(key=lambda x: x[1], reverse=True)

        for blob, start, end, kind in candidates:
            parsed = LLMAgent._json_try_load(blob)
            if not parsed:
                parse_error = f"{kind}: json.loads failed"
                logger.debug("Кнопки: кандидат %s не распарсился", kind)
                continue
            normalized = LLMAgent._normalize_buttons_payload(parsed)
            if normalized:
                buttons = normalized
                best_span = (start, end)
                logger.info(
                    "Извлечение кнопок: успех (%s), кнопок=%s", kind, len(buttons)
                )
                break
            parse_error = f"{kind}: пустой массив buttons"

        if best_span:
            answer_text = (original[: best_span[0]] + original[best_span[1] :]).strip()

        answer_text, fence_removed = LLMAgent._strip_json_markdown_fences(answer_text)
        if fence_removed:
            logger.info("JSON-блоки удалены из ответа: %s шт", fence_removed)

        if not buttons:
            logger.info(
                "Извлечение кнопок: GPT не дал валидный JSON (%s)",
                parse_error or "кандидаты не найдены",
            )

        is_generic = (
            any(
                any(gw in btn["text"].lower() for gw in GENERIC_WORDS)
                for btn in buttons
            )
            if buttons
            else True
        )

        logger.info("Найдено кнопок: %s", len(buttons))
        logger.info("Кнопки универсальные (по тексту)? %s", "Да" if is_generic else "Нет")
        logger.info("Текст ответа: %s...", answer_text[:100].replace("\n", " "))
        return answer_text, buttons, is_generic

    def ask(self, user_id: int, user_text: str) -> str:
        response = self.process_message_with_agent(user_id=user_id, user_text=user_text)
        return response.answer

    def generate_followup_suggestions(self, context: str) -> list[dict[str, str]]:
        text = context.lower().strip()
        short_context = text[:200]
        suggestions: list[dict[str, str]] = []
        request_type = "общее"

        if any(
            word in short_context
            for word in (
                "сегодня",
                "сейчас",
                "новости",
                "последние",
                "свежие",
                "текущ",
                "обновл",
            )
        ):
            request_type = "новости"
            suggestions = [
                {"text": "🔄 Свежие обновления", "callback_data": "latest_updates"},
                {"text": "Углубиться в тему 🎯", "callback_data": "ask_followup"},
                {"text": "Больше деталей 📚", "callback_data": "summarize"},
            ]
        elif any(
            word in short_context
            for word in (
                "как",
                "настроить",
                "сделать",
                "инструкц",
                "шаг",
                "практик",
                "примен",
            )
        ):
            request_type = "практика"
            suggestions = [
                {"text": "Пошаговая инструкция 📋", "callback_data": "ask_followup"},
                {"text": "Частые ошибки ⚠️", "callback_data": "ask_followup"},
                {"text": "Альтернативы 🔄", "callback_data": "ask_followup"},
            ]
        else:
            request_type = "общее"
            suggestions = [
                {"text": "Углубиться в тему 🎯", "callback_data": "ask_followup"},
                {"text": "Примеры из жизни 📖", "callback_data": "ask_followup"},
                {"text": "Как применить 🔧", "callback_data": "ask_followup"},
            ]

        logger.info("Proactive: тип запроса — %s", request_type)

        unique: list[dict[str, str]] = []
        seen = set()
        for button in suggestions:
            key = button["callback_data"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(button)
            if len(unique) == 3:
                break

        logger.info("Proactive: сгенерировано %s предложений", len(unique))
        return unique
            
    def process_message_with_agent(
        self,
        user_id: int,
        user_text: str,
        *,
        skip_concierge_tracking: bool = False,
        allow_clarification: bool = True,
    ) -> AgentResponse:
        enhanced_user_text = user_text
        trigger_reason = self._web_search_trigger_reason(user_text)
        if trigger_reason:
            logger.info(
                "Веб-поиск: триггер (%s), user_id=%s",
                trigger_reason,
                user_id,
            )
            search_context = self.search_with_tavily(user_text[:300])
            enhanced_user_text = (
                f"{user_text}\n\n"
                "Ниже результаты веб-поиска. Используй их в ответе:\n"
                f"{search_context}"
            )

        clarification_extra = ""
        if not skip_concierge_tracking:
            if not allow_clarification:
                self._clarify_followup_pending.pop(user_id, None)
            else:
                history_tail = self.memory.get_user_memory(user_id)[-10:]
                web_enriched = trigger_reason is not None
                if self._clarify_followup_pending.get(user_id):
                    clarification_extra = FOLLOWUP_AFTER_CLARIFICATION_OVERLAY
                    logger.info(
                        "Уточнения: ответ после уточняющего вопроса user_id=%s",
                        user_id,
                    )
                elif should_offer_clarification_turn(
                    user_text,
                    web_search_enriched=web_enriched,
                    history_tail=history_tail,
                ):
                    clarification_extra = CLARIFICATION_SYSTEM_OVERLAY
                    logger.info(
                        "Уточнения: один уточняющий вопрос user_id=%s",
                        user_id,
                    )

        if not skip_concierge_tracking:
            style_updates = detect_style_updates(user_text)
            if style_updates:
                self.memory.update_style_preferences(user_id, style_updates)

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                user_payload = enhanced_user_text + BUTTONS_USER_REMINDER
                completion = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        *self._messages(
                            user_id,
                            clarification_extra=clarification_extra,
                        ),
                        {"role": "user", "content": user_payload},
                    ],
                    temperature=self._chat_temperature,
                )
                _log_openai_chat_completion_usage(completion)
                answer = (completion.choices[0].message.content or "").strip()
                clean_text, buttons, is_generic = self._extract_buttons(answer)
                if not skip_concierge_tracking:
                    buttons, is_generic = self._apply_concierge_layer(
                        user_id, user_text, buttons, is_generic
                    )
                if not skip_concierge_tracking and allow_clarification:
                    if clarification_extra == FOLLOWUP_AFTER_CLARIFICATION_OVERLAY:
                        self._clarify_followup_pending.pop(user_id, None)
                    elif clarification_extra == CLARIFICATION_SYSTEM_OVERLAY:
                        self._clarify_followup_pending[user_id] = True
                self.memory.save_user_memory(user_id, user_text, clean_text)
                return AgentResponse(
                    answer=clean_text, buttons=buttons, is_generic=is_generic
                )
            except APITimeoutError:
                if attempt < max_attempts:
                    time.sleep(attempt)
                    continue
            except APIStatusError as exc:
                err_type = None
                err_code = None
                err_message = None
                try:
                    payload = None
                    if getattr(exc, "response", None) is not None:
                        payload = exc.response.json()
                    if isinstance(payload, dict):
                        err_obj = payload.get("error")
                        if isinstance(err_obj, dict):
                            err_type = err_obj.get("type")
                            err_code = err_obj.get("code")
                            err_message = err_obj.get("message")
                except Exception:
                    logger.debug("OpenAI APIStatusError: не удалось распарсить JSON тела", exc_info=True)

                logger.error(
                    "OpenAI APIStatusError: status=%s, type=%s, code=%s, message=%s",
                    exc.status_code,
                    err_type or "-",
                    err_code or "-",
                    (err_message or str(exc) or "-")[:1000],
                )
                if exc.status_code == 421 and attempt < max_attempts:
                    time.sleep(attempt)
                    continue
                break
            except Exception:
                break

        fallback = (
            "Сейчас не получилось получить ответ от модели 😔 "
            "Сервис временно недоступен. Попробуй повторить запрос через минуту."
        )
        self.memory.save_user_memory(user_id, user_text, fallback)
        if not skip_concierge_tracking:
            self._pending_concierge.pop(user_id, None)
        return AgentResponse(answer=fallback, buttons=[], is_generic=True)

    def transcribe_voice(self, file_path: str) -> str:
        try:
            with open(file_path, "rb") as audio_file:
                transcript = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                )
            text = (getattr(transcript, "text", "") or "").strip()
            logger.info("Whisper транскрибация: %s символов", len(text))
            return text
        except Exception as exc:
            logger.exception("Whisper ошибка: %s", exc)
            return ""

    def analyze_image(
        self,
        image_url: str,
        user_question: str = "Что на этом изображении?",
        *,
        image_sequence: int = 1,
        dialogue_context: str | None = None,
        previous_image_assistant_text: str | None = None,
    ) -> str:
        try:
            logger.info(
                "Vision: кадр #%s, вопрос: %s",
                image_sequence,
                user_question[:200],
            )
            vision_system = (
                "Ты Кузьма. Сейчас пользователь прислал ОДНО новое изображение ниже — анализируй ТОЛЬКО его. "
                "Не смешивай детали с другими кадрами; текстовый контекст диалога (если дан) — чтобы связать кадры и имена, "
                "которые пользователь уже назвал, а не чтобы выдумать содержимое нового кадра.\n"
                "Если в диалоге пользователь уже назвал персонажа или объект (например «это Неуязвимый», «зовут Барсик») "
                "и признаки на НОВОМ кадре совпадают (цвета костюма, маска, пропорции, стиль) — можешь сказать, что это, "
                "скорее всего, тот же персонаж/объект, и использовать имя с уместной оговоркой при остаточных сомнениях. "
                "Если признаки явно другие — это другой персонаж или другой объект; не переноси старое имя.\n"
                "Мысленно (не выводи списком пользователю): перечисли заметные детали кадра; сравни с типичными образами в культуре; "
                "сверь с описаниями из контекста диалога; оцени, каноничен ли костюм/стиль; учти фан-арт, косплей, коллаж, качество, ракурс.\n"
                "Сначала опиши видимое, потом гипотеза; если детали не бьются с каноном — скажи об этом и предложи альтернативы "
                "(фан-арт, косплей, другой персонаж). Без сильных признаков не выдавай имя как абсолютную истину — "
                "но если пользователь сам дал имя и кадр согласуется, это не противоречие.\n"
                "Отвечай по-русски, дружелюбно. Обязательно 2–3 разных эмодзи И хотя бы одна игривая фраза или сравнение "
                "в словах (не только смайлики): метафора, лёгкая шутка про костюм/ракурс/размытость, самоирония "
                "про свою неуверенность.\n"
                "Если не уверен — с юмором признайся, опиши видимое и «похоже на…»; не заполняй пробелы выдуманными деталями.\n"
                "Текст на снимке (анализы, таблицы) — перескажи доступно; диагнозы и лечение не назначай, при необходимости врач.\n"
                "Ирония не над человеком на фото; факты с кадра не выдумывай. Перед финалом мысленно проверь: не перепутал ли кадры, "
                "не назвал ли имя с завышенной уверенностью."
            )
            parts: list[str] = [
                f"Это изображение №{image_sequence} в серии в этом чате (счётчик по присланным фото). "
                "Фокус только на ЭТОМ кадре."
            ]
            if dialogue_context and image_sequence > 1:
                parts.append(
                    "Ниже — недавний диалог этого же чата (в т.ч. строки «[Изображение]», твои прошлые описания кадров и то, "
                    "как пользователь называл героев или объекты). Используй для связи: те же отличительные признаки + имя из диалога "
                    "→ разумно считать того же персонажа/объект; если признаки не сходятся — не приписывай старое имя.\n\n"
                    f"{dialogue_context}"
                )
            elif previous_image_assistant_text and image_sequence > 1:
                parts.append(
                    "Краткий контекст твоего прошлого ответа про предыдущий кадр (для связи, не подменяй новым кадром):\n"
                    f"{previous_image_assistant_text}"
                )
            parts.append(f"Вопрос пользователя:\n{user_question}")
            full_text = "\n\n".join(parts)
            completion = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": vision_system},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": full_text},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                temperature=self._vision_temperature,
            )
            _log_openai_chat_completion_usage(completion)
            answer = (completion.choices[0].message.content or "").strip()
            logger.info("Vision: анализ завершён, %s символов", len(answer))
            return answer or "Не удалось проанализировать изображение"
        except Exception as exc:
            logger.exception("Vision ошибка: %s", exc)
            return "Не удалось проанализировать изображение"

    def _tavily_timeout_for_attempt(self, attempt: int) -> float:
        """Таймаут HTTP для попытки `attempt` (1-based); рост 1.5x, не выше 120 с (ограничение tavily-python)."""
        raw = float(self._tavily_timeout_seconds) * (
            _TAVILY_TIMEOUT_ATTEMPT_FACTOR ** (attempt - 1)
        )
        return min(raw, 120.0)

    def _tavily_search(self, query: str, max_results: int, **search_kwargs) -> dict | None:
        """Синхронный поиск Tavily с ретраями только при таймауте; dict ответа API или None при полном провале."""
        if not self.tavily:
            return None
        total = 1 + max(0, self._tavily_max_retries)
        mult = self._tavily_retry_backoff_multiplier
        backoff_base = 1.0
        backoff_cap = 8.0
        q_log = (query or "")[:400].replace("\n", " ")
        days_val = search_kwargs.get("days", "not_set")
        excluded_count = len(search_kwargs.get("exclude_domains") or [])

        for attempt in range(1, total + 1):
            timeout_sec = self._tavily_timeout_for_attempt(attempt)
            t_display = (
                int(timeout_sec)
                if abs(timeout_sec - round(timeout_sec)) < 1e-9
                else round(timeout_sec, 1)
            )
            logger.info(
                "Tavily search: query=%r, timeout=%ss, attempt=%s/%s, days=%s, exclude_domains_count=%s",
                q_log,
                t_display,
                attempt,
                total,
                days_val,
                excluded_count,
            )
            try:
                result = self.tavily.search(
                    query=query,
                    max_results=max_results,
                    timeout=timeout_sec,
                    **search_kwargs,
                )
            except (TavilyTimeoutError, requests.exceptions.Timeout) as exc:
                if attempt < total:
                    next_timeout = self._tavily_timeout_for_attempt(attempt + 1)
                    nt_display = (
                        int(next_timeout)
                        if abs(next_timeout - round(next_timeout)) < 1e-9
                        else round(next_timeout, 1)
                    )
                    delay = min(
                        backoff_base * (mult ** (attempt - 1)),
                        backoff_cap,
                    )
                    logger.warning(
                        "Tavily timeout, retrying with timeout=%ss, attempt=%s/%s",
                        nt_display,
                        attempt + 1,
                        total,
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    "Tavily failed after %s attempts: %s",
                    total,
                    exc,
                )
                return None
            except Exception as exc:
                err_msg = str(exc).strip() or "(без текста)"
                logger.error(
                    "Tavily search failed (non-timeout) on attempt %s/%s: %s: %s",
                    attempt,
                    total,
                    type(exc).__name__,
                    err_msg[:500],
                )
                logger.debug("Tavily search non-timeout detail", exc_info=True)
                return None

            if not isinstance(result, dict):
                logger.error(
                    "Tavily search: unexpected response type=%s",
                    type(result).__name__,
                )
                return None
            logger.debug("Tavily search response keys: %s", list(result.keys()))
            items = result.get("results") or []
            logger.info(
                "Tavily search: success, results=%s, attempts_used=%s/%s",
                len(items),
                attempt,
                total,
            )
            return result

        return None

    def search_with_tavily(self, query: str) -> str:
        if not self.tavily:
            logger.warning("Tavily: поиск пропущен — нет API ключа")
            return "🌐 Поиск недоступен: добавь TAVILY_API_KEY в .env"
        logger.info("Tavily: запрос поиска, длина запроса=%s симв.", len(query))
        result = self._tavily_search(query, max_results=3)
        if result is None:
            return "🌐 Не удалось выполнить веб-поиск. Попробуй повторить запрос позже."
        items = result.get("results", [])
        if not items:
            logger.info("Tavily: пустой список results")
            return "🌐 Не нашел результатов, попробуй уточнить запрос."
        lines = []
        for item in items:
            title = item.get("title", "Без названия")
            url = item.get("url", "")
            content = (item.get("content", "") or "").strip().replace("\n", " ")
            lines.append(f"— {title}\n({url})\n{content[:240]}")
        logger.info("Tavily: сформирован контекст для модели, результатов=%s", len(items))
        return "🌐 Результаты веб-поиска:\n" + "\n\n".join(lines)

    def run_raw_completion(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2500,
        temperature: float | None = None,
    ) -> str:
        """Один вызов чата без истории и JSON-кнопок (симулятор сценариев и т.п.)."""
        temp = self._chat_temperature if temperature is None else temperature
        completion = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temp,
            max_tokens=max_tokens,
        )
        _log_openai_chat_completion_usage(completion)
        out = (completion.choices[0].message.content or "").strip()
        logger.info("run_raw_completion: %s символов ответа", len(out))
        return out

    def web_search(self, query: str) -> str:
        return self.search_with_tavily(query)