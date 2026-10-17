from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from database.models import User

ROLE_DISPATCHER = "dispatcher"
ROLE_COURIER = "courier"
ROLE_LAUNDRY = "laundry"
ROLE_ADMIN = "super_admin"
STATUS_LABELS = {
    "new": "Новый",
    "pickup_en_route": "Курьер едет за заказом",
    "picked_up": "В цехе",
    "processing": "В обработке",
    "ready": "Готов к доставке",
    "delivery_en_route": "Курьер везёт клиенту",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, session_factory, admin_ids: frozenset[int]):
        self.session_factory = session_factory
        self.admin_ids = admin_ids

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        async with self.session_factory() as session:
            data["db"] = session
            data["user_role"] = None
            if user:
                if user.id in self.admin_ids:
                    data["user_role"] = ROLE_ADMIN
                else:
                    record = await session.get(User, user.id)
                    data["user_role"] = record.role if record and record.is_approved else None
            return await handler(event, data)


class RoleFilter(BaseFilter):
    def __init__(self, *roles: str):
        self.roles = frozenset(roles)

    async def __call__(self, event, user_role: str | None = None) -> bool:
        return user_role in self.roles


def role_keyboard(role: str) -> ReplyKeyboardMarkup:
    if role == ROLE_DISPATCHER:
        rows = [["➕ Новая заявка", "📋 Заказы"]]
    elif role == ROLE_COURIER:
        rows = [["🟢 Начать смену", "🔴 Завершить смену"], ["📋 Мои задачи"]]
    elif role == ROLE_LAUNDRY:
        rows = [["🔎 Найти заказ"]]
    else:
        rows = [["👥 Сотрудники", "⏳ Заявки"], ["📊 Статистика", "📦 Заказы"]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=label) for label in row] for row in rows], resize_keyboard=True)


async def send_order_card(message: Message, order, *, show_dispatcher_actions: bool = False) -> None:
    text = (
        f"Заказ #{order.id}\nКлиент: {order.client_name}\nАдрес: {order.address}\n"
        f"Забор: {order.pickup_day}\nСтатус: {status_label(order.status)}"
    )
    if order.measurements:
        text += f"\nЗамеры: {order.measurements}"
    if show_dispatcher_actions:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"dispatch:edit:{order.id}")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"dispatch:cancel:{order.id}")],
            ]
        )
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer(text)


async def callback_is_admin(callback: CallbackQuery, admin_ids: frozenset[int]) -> bool:
    if callback.from_user.id in admin_ids:
        return True
    await callback.answer("Действие доступно только администраторам.", show_alert=True)
    return False