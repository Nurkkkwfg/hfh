from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import select

from database.models import Order, User
from handlers.common import ROLE_COURIER, RoleFilter
from utils.geo import haversine_km, sort_orders_by_distance

router = Router(name="courier")
router.message.filter(RoleFilter(ROLE_COURIER))
router.callback_query.filter(RoleFilter(ROLE_COURIER))


@router.message(Command("shift"))
@router.message(F.text == "🟢 Начать смену")
async def start_shift(message: Message, db):
    user = await db.get(User, message.from_user.id)
    user.is_on_shift = True
    await db.commit()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Смена начата. Отправьте текущую геопозицию кнопкой ниже или включите трансляцию: "
        "📎 → Геопозиция → Транслировать геопозицию. Обновления трансляции сохраняются автоматически.",
        reply_markup=keyboard,
    )


@router.message(Command("end_shift"))
@router.message(F.text == "🔴 Завершить смену")
async def end_shift(message: Message, db):
    user = await db.get(User, message.from_user.id)
    user.is_on_shift = False
    await db.commit()
    await message.answer("Смена завершена.")


async def save_location(message: Message, db, *, notify: bool = True):
    if not message.location:
        return
    user = await db.get(User, message.from_user.id)
    if user is None or not user.is_on_shift:
        await message.answer("Сначала начните смену.")
        return
    user.latitude, user.longitude = message.location.latitude, message.location.longitude
    await db.commit()
    if notify:
        await message.answer("Геопозиция обновлена.")
        await show_tasks(message, db, user)


@router.message(F.location)
async def receive_location(message: Message, db):
    await save_location(message, db)


@router.edited_message(F.location)
async def receive_live_location_update(message: Message, db):
    await save_location(message, db, notify=False)


@router.message(Command("tasks"))
@router.message(F.text == "📋 Мои задачи")
async def tasks(message: Message, db):
    user = await db.get(User, message.from_user.id)
    if user.latitude is None or user.longitude is None:
        await message.answer("Сначала отправьте геопозицию после начала смены.")
        return
    await show_tasks(message, db, user)


async def show_tasks(message: Message, db, courier: User):
    orders = (
        await db.scalars(
            select(Order).where(
                Order.status.in_(["new", "pickup_en_route", "ready", "delivery_en_route"])
            )
        )
    ).all()
    available = [
        order
        for order in orders
        if order.status == "new"
        or (order.status == "pickup_en_route" and order.pickup_courier_id == courier.telegram_id)
        or order.status == "ready"
        or (order.status == "delivery_en_route" and order.delivery_courier_id == courier.telegram_id)
    ]
    available = sort_orders_by_distance(available, courier.latitude, courier.longitude)
    if not available:
        await message.answer("Открытых задач нет.")
        return
    for order in available:
        distance = (
            f"{haversine_km(courier.latitude, courier.longitude, order.latitude, order.longitude):.1f} км"
            if order.latitude is not None and order.longitude is not None
            else "расстояние неизвестно"
        )
        text = (
            f"Заказ #{order.id}\nКлиент: {order.client_name}\nАдрес: {order.address}\n"
            f"Забор: {order.pickup_day}\nСтатус: {order.status}\nДо адреса: {distance}"
        )
        buttons = []
        if order.latitude is not None and order.longitude is not None:
            buttons.append(
                [InlineKeyboardButton(
                    text="🗺 Открыть в 2GIS",
                    url=f"https://2gis.kg/bishkek/search/{order.latitude},{order.longitude}",
                )]
            )
        action = {
            "new": ("🛻 Выехал за заказом", "courier:pickup:"),
            "pickup_en_route": ("✅ Забрал (везу в цех)", "courier:picked:"),
            "ready": ("🚚 Выехал на доставку", "courier:delivery:"),
            "delivery_en_route": ("✅ Доставлено клиенту", "courier:delivered:"),
        }.get(order.status)
        if action:
            buttons.append([InlineKeyboardButton(text=action[0], callback_data=f"{action[1]}{order.id}")])
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("courier:"))
async def update_order_status(callback: CallbackQuery, db):
    try:
        action, order_id_raw = callback.data.rsplit(":", 1)
        order_id = int(order_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректная задача.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    courier_id = callback.from_user.id
    transitions = {
        "courier:pickup": ("new", "pickup_en_route", "pickup_courier_id"),
        "courier:picked": ("pickup_en_route", "picked_up", "pickup_courier_id"),
        "courier:delivery": ("ready", "delivery_en_route", "delivery_courier_id"),
        "courier:delivered": ("delivery_en_route", "delivered", "delivery_courier_id"),
    }
    transition = transitions.get(action)
    if transition is None or order.status != transition[0]:
        await callback.answer("Статус уже изменён или задача недоступна.", show_alert=True)
        return
    owner_field = transition[2]
    owner_id = getattr(order, owner_field)
    if owner_id is not None and owner_id != courier_id:
        await callback.answer("Эта задача закреплена за другим курьером.", show_alert=True)
        return
    setattr(order, owner_field, courier_id)
    order.status = transition[1]
    await db.commit()
    await callback.answer("Статус заказа обновлён.")
    if callback.message:
        await callback.message.edit_text(f"Заказ #{order.id}: {order.status}")