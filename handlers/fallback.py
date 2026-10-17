from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from handlers.common import ROLE_ADMIN, ROLE_COURIER, ROLE_DISPATCHER, ROLE_LAUNDRY

router = Router(name="fallback")
ROLE_HELP = {
    ROLE_ADMIN: "Администратор: панель /admin. Кнопки: «Сотрудники», «Заявки», «Статистика», «Заказы». Запросы доступа приходят сообщением от бота.",
    ROLE_DISPATCHER: "Диспетчер: используйте кнопки меню для работы с заказами. Подробности: /help.",
    ROLE_COURIER: "Курьер: начните смену, отправьте геопозицию и откройте список задач. /tasks обновляет очередь.",
    ROLE_LAUNDRY: "Цех: найдите заказ по номеру, внесите замеры и отметьте готовность. Подробности: /help.",
}


@router.message(Command("help"))
async def help_message(message: Message, user_role: str | None):
    if user_role is None:
        await message.answer("Доступ ещё не одобрен. Отправьте /start для запроса доступа.")
        return
    await message.answer(ROLE_HELP.get(user_role, "Откройте меню бота для доступных действий."))


@router.message()
async def unknown_message(message: Message, user_role: str | None):
    if user_role is None:
        await message.answer("У вас пока нет доступа. Отправьте /start для запроса или дождитесь одобрения администратора.")
        return
    await message.answer("Не понял сообщение. Используйте кнопки меню или отправьте /help.")


@router.callback_query()
async def unknown_callback(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Откройте актуальную карточку заказа.", show_alert=True)