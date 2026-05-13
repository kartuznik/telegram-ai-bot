import logging
from typing import TYPE_CHECKING

from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

if TYPE_CHECKING:
    from aiogram import Bot

from app.health_check import OWNER_RESTART_USER_ID

logger = logging.getLogger(__name__)

CALLBACK_START_HELP = "show_help"


def build_user_bot_commands() -> list[BotCommand]:
    """Команды меню для всех пользователей (BotCommandScopeDefault)."""
    return [
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="help", description="Помощь и список команд"),
        BotCommand(command="simulate", description="Симулятор сценариев (вопрос в строке)"),
        BotCommand(command="editor_start", description="Включить редактор контента"),
        BotCommand(command="editor_stop", description="Выключить редактор"),
        BotCommand(command="editor_prefs", description="Настройки редактора"),
        BotCommand(command="editor_info", description="Текущие настройки редактора"),
        BotCommand(command="editor_rules", description="Правила редактора для черновиков"),
        BotCommand(command="editor_reset_rejects", description="Сброс отказов и банов каналов"),
        BotCommand(command="drafts", description="Черновики постов для канала"),
        BotCommand(command="draft", description="🔍 Найти тему для черновика"),
        BotCommand(command="find_topic", description="Найти тему (черновик)"),
        BotCommand(command="find", description="Найти тему (черновик)"),
        BotCommand(command="editor", description="Панель редактора + черновик"),
        BotCommand(command="bookmarks", description="Якоря в диалоге"),
        BotCommand(command="templates", description="Шаблоны ответов"),
        BotCommand(command="clear", description="Очистить историю диалога"),
        BotCommand(command="reset_style", description="Сбросить стиль ответов"),
    ]


def build_health_bot_commands() -> list[BotCommand]:
    """Команды мониторинга для меню (setMyCommands)."""
    return [
        BotCommand(command="status", description="Проверка здоровья бота"),
        BotCommand(command="restart", description="Перезапуск бота (только владелец)"),
    ]


def build_admin_bot_commands() -> list[BotCommand]:
    """Полное меню для ADMIN_ID (пользовательские + служебные)."""
    return build_user_bot_commands() + [
        BotCommand(command="learning", description="[Владелец] Режим обучения голосу"),
        BotCommand(command="learning_stats", description="[Владелец] Статистика обучения"),
        BotCommand(command="style_profile", description="[Владелец] Профиль стиля / export / import"),
        BotCommand(command="stats", description="[Админ] Статистика бота"),
        BotCommand(command="admin", description="[Админ] Панель администратора"),
        BotCommand(command="selftest", description="[Админ] Самодиагностика (функции)"),
        BotCommand(command="fulldiag", description="[Админ] Полная диагностика"),
        BotCommand(command="broadcast", description="[Админ] Рассылка всем"),
        BotCommand(command="ban", description="[Админ] Забанить user_id"),
        BotCommand(command="unban", description="[Админ] Разбанить user_id"),
        BotCommand(command="users", description="[Админ] Список пользователей"),
        BotCommand(command="topics", description="[Админ] Темы поиска редактора"),
        BotCommand(command="sources", description="[Админ] Уточнение к поиску"),
        BotCommand(command="searchwindow", description="[Админ] Окно дней поиска (Tavily)"),
        BotCommand(command="searchmode", description="[Админ] Режим источников web/tg/both"),
        BotCommand(command="automode", description="[Админ] Интервал и вкл/выкл авто-поиска"),
        *build_health_bot_commands(),
    ]


async def setup_bot_command_menu(bot: "Bot", admin_id: int | None) -> None:
    """setMyCommands: общий набор для всех; расширенный — только для чата ADMIN_ID (личка)."""
    user_cmds = build_user_bot_commands()
    await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
    logger.info("Commands set: %s user commands for all users (BotCommandScopeDefault)", len(user_cmds))
    admin_cmds = build_admin_bot_commands()

    if admin_id is not None:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=admin_id))
            logger.info(
                "Commands set: %s admin commands for ADMIN_ID=%s (BotCommandScopeChat)",
                len(admin_cmds),
                admin_id,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось установить команды для админа ADMIN_ID=%s: %s (чат с ботом должен существовать)",
                admin_id,
                exc,
            )
    else:
        logger.warning("ADMIN_ID not set — расширенное меню админа через BotCommandScopeChat не настроено")

    # Владелец /restart может не совпадать с ADMIN_ID — отдельное меню в его ЛС.
    owner_cmds = build_user_bot_commands() + build_health_bot_commands()
    if OWNER_RESTART_USER_ID != admin_id:
        try:
            await bot.set_my_commands(
                owner_cmds,
                scope=BotCommandScopeChat(chat_id=OWNER_RESTART_USER_ID),
            )
            logger.info(
                "Commands set: %s commands for owner user_id=%s (BotCommandScopeChat)",
                len(owner_cmds),
                OWNER_RESTART_USER_ID,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось установить команды для владельца user_id=%s: %s (нужна личка с ботом)",
                OWNER_RESTART_USER_ID,
                exc,
            )


# Текст справки без «жёстко зашитого» списка команд — список собирается в build_help_text.
_HELP_INTRO = (
    "👋 Привет! Я Кузьма — твой AI-помощник. Пишу по-русски, с лёгким юмором и по делу.\n\n"
    "📌 Что я умею:\n"
    "• Отвечать на вопросы по разным темам и помнить контекст диалога\n"
    "• Анализировать фото, скриншоты и картинки в документах\n"
    "• Распознавать голосовые сообщения\n"
    "• Читать и разбирать PDF и TXT файлы\n"
    "• При необходимости искать свежую информацию в интернете\n\n"
)

_HELP_USER_COMMANDS = (
    "⌨️ Доступные команды:\n"
    "• /start — поздороваться и начать\n"
    "• /help — эта инструкция\n"
    "• /simulate — симулятор сценариев (вопрос в той же строке после команды)\n"
    "• /clear — очистить историю диалога\n"
    "• /reset_style — сбросить настройки стиля ответов\n"
    "• /templates — сохранённые ответы (то же, что «покажи шаблоны»)\n"
    "• /bookmarks — якоря в диалоге (то же, что «покажи якоря»)\n\n"
)

_HELP_ADMIN_COMMANDS = (
    "🔐 [Админ] /stats /admin /selftest /fulldiag /broadcast /ban /unban /users /status /restart\n\n"
)

_HELP_MIDDLE = (
    "📌 Шаблоны — чтобы советы не потерялись\n"
    "Понравился мой разбор? Скажи «сохрани это» — утащу последний ответ в твою личную коллекцию. "
    "Можно с именем: «сохрани как Диверсификация» или в кавычках.\n"
    "• «Покажи шаблоны» или /templates — список кнопками, жми и читай снова\n"
    "• «Удали шаблон [название]» или кнопка «Удалить» под записью — уберу лишнее\n"
    "Шаблоны только твои, чужие не подсмотреть. Ни один удачный совет не утонет в переписке 🎯\n\n"
    "🔖 Якоря в диалоге — важная мысль? Скажи «запомни этот момент» или «якорь: тема», "
    "потом «покажи якоря» или /bookmarks для быстрого возврата. Не теряй ценное в потоке беседы! 🎯\n"
    "• «Удали якорь [название]» — уберу закладку, если передумал\n"
    "Якоря — это не шаблоны: сохраняю кусок переписки (ты + я), чтобы вернуться к теме, а не готовый текст на повтор 🔖😊\n\n"
    "🎲 Симулятор сценариев — не знаешь, что выбрать?\n"
    "Команда /simulate … или спроси: «Что если…», «Какие варианты…», «Стоит ли…» — покажу три ветки: 🟢 оптимистичный, 🟡 реалистичный, "
    "🔴 пессимистичный. Нажми кнопку — разверну детали, шаги и оговорки. Решения принимай с открытыми глазами 👁️✨\n\n"
    "✍️ Редактор контента — бот помогает вести канал @kriptogeograph:\n"
    "• /editor_start — включить подбор новостей (черновики только в личке с ботом)\n"
    "• /editor_prefs темы:мемы,юмор или /editor_prefs темы мемы,юмор — только темы (после запятой — уточнение к поиску)\n"
    "• /editor_prefs тгканалы:@a,@b или /editor_prefs тгканалы @a @b — только список TG-каналов (опечатка «тканалы:»)\n"
    "• /editor_prefs биткоин,defi — по-прежнему можно одной строкой без слова «темы»\n"
    "• /editor_prefs источники:both — web (Tavily), tg (t.me/s), both (по умолчанию both); для web нужен TAVILY_API_KEY\n"
    "• /searchmode web|tg|both — то же про источники одной командой (без хвоста — текущий режим)\n"
    "• /editor_info — сводка настроек; /editor_rules — накопленные правила для генерации черновиков; "
    "/editor_reset_rejects — сброс отказов и жёстких банов по сайту или tg:каналу\n"
    "• /editor_prefs авто:0.5 — авто-поиск черновиков примерно раз в 30 минут (также можно авто:1, авто:24 и т.д. до 168); "
    "авто:off — только ручной /drafts\n"
    "• /automode 0.5|1|2|off|on — интервал и вкл/выкл авто-поиска (без хвоста — текущие настройки)\n"
    "• /drafts — очередь черновиков: покажу самый старый на решение; пусто — подберу новый; "
    "/drafts ещё — добавить ещё один материал в очередь (лимит неапрувнутых в коде, сейчас 6) ✅✏️❌\n"
    "• /editor_stop — пауза редактора и авто-поиска; в канал уйдёт только то, что ты одобришь в ЛС\n"
    "Учусь на отказах: запоминаю конкретный URL; для веба жёсткий бан по домену (vesti.ru), для TG — по каналу (tg:username), "
    "не весь t.me. Если неапрувнутых черновиков слишком много — новый автоматом не пришлю, "
    "напомню в ЛС. Ты дирижёр эфира, я суфлёр 🎯\n\n"
    "🎨 Стиль ответов — на твоей стороне\n"
    "Запоминаю, как тебе удобнее: не шаблоном давлю, а подстраиваюсь под тебя 😄 "
    "Просто напиши в чат, например:\n"
    "• «Отвечай короче» — буду лаконичнее\n"
    "• «Дай больше деталей» — раскрою тему шире\n"
    "• «Без шуток, по делу» — меньше шуток, больше сути\n"
    "• «Объясни проще» — без горы терминов\n"
    "Настройки только твои, в базе лежат отдельно от других людей.\n\n"
    "🔄 Надоело экспериментировать? Жми /reset_style — вернусь к привычному балансу.\n\n"
    "🔘 Кнопки под ответами:\n"
    "Нажимай на них, чтобы углубиться в тему, продолжить разговор или вызвать поиск — "
    "я подстраиваю подсказки под твой вопрос.\n\n"
    "💡 Примеры запросов:\n"
    "• «как приготовить борщ?»\n"
    "• «объясни теорему Пифагора»\n"
    "• «куда поехать в отпуск?»\n"
    "• «проанализируй это фото» (с приложенным изображением)\n\n"
    "Есть вопрос? Просто напиши — помогу! 😊"
)


def build_help_text(*, is_admin: bool) -> str:
    """Полный текст /help: для админа добавляется блок служебных команд."""
    parts = [_HELP_INTRO, _HELP_USER_COMMANDS]
    if is_admin:
        parts.append(_HELP_ADMIN_COMMANDS)
    parts.append(_HELP_MIDDLE)
    return "".join(parts)


HELP_TEXT = build_help_text(is_admin=False)


def build_start_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопка под приветствием /start → показ справки по callback."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📖 Что я умею?",
                    callback_data=CALLBACK_START_HELP,
                )
            ],
        ]
    )


async def reply_with_help_text(
    message: Message,
    *,
    is_admin_override: bool | None = None,
) -> None:
    """Для callback можно передать is_admin_override (у Message не всегда заполнен from_user)."""
    from app.admin import is_admin

    if is_admin_override is not None:
        adm = is_admin_override
    else:
        uid = message.from_user.id if message.from_user else 0
        adm = is_admin(uid)
    await message.answer(build_help_text(is_admin=adm))


def build_keyboard_from_buttons(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button["text"], callback_data=button["callback_data"]
                )
                for button in buttons
            ]
        ]
    )


def build_default_keyboard() -> tuple[InlineKeyboardMarkup, list[dict[str, str]]]:
    buttons = [
        {"text": "🔍 Найти тему (черновик)", "callback_data": "find_draft_topic"},
        {"text": "🔍 Поиск в вебе", "callback_data": "web_search"},
        {"text": "🔄 Свежие обновления", "callback_data": "latest_updates"},
        {"text": "Углубиться в тему 🎯", "callback_data": "deep_dive"},
        {"text": "🗑️ Очистить историю", "callback_data": "clear_history"},
    ]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Найти тему (черновик)",
                    callback_data="find_draft_topic",
                ),
                InlineKeyboardButton(
                    text="🔍 Поиск в вебе",
                    callback_data="web_search",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Свежие обновления",
                    callback_data="latest_updates",
                ),
                InlineKeyboardButton(
                    text="Углубиться в тему 🎯",
                    callback_data="deep_dive",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Очистить историю",
                    callback_data="clear_history",
                ),
            ],
        ]
    )

    return keyboard, buttons
