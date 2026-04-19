from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.admin import NO_ADMIN_RIGHTS, is_admin

ADMIN_COMMANDS = frozenset(
    {"admin", "broadcast", "ban", "unban", "users", "stats", "selftest"}
)


class AdminAuthMiddleware(BaseMiddleware):
    """Блокирует админ-команды для пользователей без прав."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        text = getattr(event, "text", None)
        from_user = getattr(event, "from_user", None)
        if text and from_user:
            raw = text.strip()
            if raw.startswith("/"):
                cmd = raw.split()[0][1:].split("@", 1)[0].lower()
                if cmd in ADMIN_COMMANDS:
                    if not is_admin(from_user.id):
                        await event.answer(NO_ADMIN_RIGHTS)
                        return None
        return await handler(event, data)
