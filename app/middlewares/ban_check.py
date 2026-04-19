from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.admin import is_admin, is_banned


class BanCheckMiddleware(BaseMiddleware):
    """Не даёт забаненным пользователям пользоваться ботом (кроме админа)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user:
            uid = from_user.id
            if is_banned(uid) and not is_admin(uid):
                await event.answer("🚫 Вы заблокированы.")
                return None
        return await handler(event, data)
