from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select

from config import Settings
from database.models import Order
from handlers.common import ROLE_DISPATCHER, RoleFilter, role_keyboard, send_order_card
from utils.dates import normalize_pickup_day
from utils.geocoding import geocode_bishkek_address

router = Router(name="dispatcher")
router.message.filter(RoleFilter(ROLE_DISPATCHER))
router.callback_query.filter(RoleFilter(ROLE_DISPATCHER))


class OrderForm(StatesGroup):
    client_name = State()
    address = State()
    pickup_day = State()
    edit_value = State()


PICKUP_OTHER_DATE = "📅 Другая дата"
EDIT_FIELDS = {
    "client_name": "Имя клиента",
    "address": "Адрес",
    "pickup_day": "День забора",
}


@router.message(Command("help"))
async def dispatcher_help(message: Message):
    await message.answer(
        "Диспетчер: «Новая заявка» создаёт заказ, «Заказы» показывает последние заявки. "
        "В карточке можно изменить отдельное поле или отменить заказ. Во время ввода отправьте /cancel, чтобы прервать действие."
    )


@router.message(Command("cancel"))
async def cancel_dispatcher_flow(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Сейчас нет незавершённого действия.")
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=role_keyboard(ROLE_DISPATCHER))


@router.message(Command("new_order"))
@router.message(F.text == "➕ Новая заявка")
async def new_order(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrderForm.client_name)
    await message.answer("Введите имя клиента:")


@router.message(OrderForm.client_name)
async def set_client_name(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Имя не должно быть пустым. Введите имя клиента:")
        return
    await state.update_data(client_name=value)
    await state.set_state(OrderForm.address)
    await message.answer("Введите полный адрес: улица, дом, подъезд, этаж, квартира:")


@router.message(OrderForm.address)
async def set_address(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Адрес не должен быть пустым. Введите полный адрес:")
        return
    await state.update_data(address=value)
    await state.set_state(OrderForm.pickup_day)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
            [KeyboardButton(text=PICKUP_OTHER_DATE)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("Когда забрать? Выберите вариант или укажите дату в формате ДД.ММ.ГГГГ:", reply_markup=keyboard)


@router.message(OrderForm.pickup_day)
async def set_pickup_day(message: Message, state: FSMContext, db, settings: Settings):
    pickup_day = (message.text or "").strip()
    if not pickup_day:
        await message.answer("Ответьте текстом или выберите день кнопкой:")
        return
    if pickup_day == PICKUP_OTHER_DATE:
        await message.answer("Введите дату забора в формате ДД.ММ.ГГГГ:")
        return
    normalized_pickup_day = normalize_pickup_day(pickup_day)
    if normalized_pickup_day is None:
        await message.answer("Укажите «Сегодня», «Завтра» или будущую дату в формате ДД.ММ.ГГГГ:")
        return
    pickup_day = normalized_pickup_day
    data = await state.get_data()
    latitude, longitude = await geocode_bishkek_address(
        data["address"], settings.geocoder_url, settings.geocoder_user_agent
    ) or (None, None)
    order = Order(
        client_name=data["client_name"],
        address=data["address"],
        pickup_day=pickup_day,
        latitude=latitude,
        longitude=longitude,
    )
    db.add(order)
    await db.commit()
    await state.clear()
    await message.answer(f"Заказ #{order.id} создан.", reply_markup=role_keyboard(ROLE_DISPATCHER))
    await send_order_card(message, order, show_dispatcher_actions=True)
    if latitude is None:
        await message.answer("Адрес не удалось геокодировать; заказ останется в конце списка курьерских задач.")


@router.message(Command("orders"))
@router.message(F.text == "📋 Заказы")
async def list_orders(message: Message, db):
    orders = (await db.scalars(select(Order).order_by(Order.id.desc()).limit(30))).all()
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    for order in orders:
        await send_order_card(message, order, show_dispatcher_actions=True)


@router.callback_query(F.data.startswith("dispatch:edit:"))
async def begin_edit(callback: CallbackQuery, state: FSMContext, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status in {"delivered", "cancelled"}:
        await callback.answer("Заказ уже закрыт или не найден.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"dispatch:field:{order_id}:{field}")]
                for field, label in EDIT_FIELDS.items()
            ]
        )
        await callback.message.answer(f"Что изменить в заказе #{order_id}?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("dispatch:field:"))
async def choose_edit_field(callback: CallbackQuery, state: FSMContext):
    try:
        _, _, order_id_raw, field = callback.data.split(":", 3)
        order_id = int(order_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректное поле заказа.", show_alert=True)
        return
    if field not in EDIT_FIELDS:
        await callback.answer("Неизвестное поле заказа.", show_alert=True)
        return
    await state.set_state(OrderForm.edit_value)
    await state.update_data(edit_order_id=order_id, edit_field=field)
    prompts = {
        "client_name": "Введите новое имя клиента:",
        "address": "Введите новый полный адрес:",
        "pickup_day": "Введите день забора (например, завтра или 25.10.2026):",
    }
    if callback.message:
        await callback.message.answer(prompts[field] + " Чтобы прервать, отправьте /cancel.")
    await callback.answer()


@router.message(OrderForm.edit_value)
async def save_edit(message: Message, state: FSMContext, db, settings: Settings):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Значение не должно быть пустым. Введите его текстом:")
        return
    data = await state.get_data()
    order = await db.get(Order, data["edit_order_id"])
    if order is None or order.status in {"delivered", "cancelled"}:
        await state.clear()
        await message.answer("Заказ не найден или уже закрыт.")
        return
    field = data["edit_field"]
    if field == "pickup_day":
        normalized_pickup_day = normalize_pickup_day(value)
        if normalized_pickup_day is None:
            await message.answer("Укажите «Сегодня», «Завтра» или будущую дату в формате ДД.ММ.ГГГГ:")
            return
        value = normalized_pickup_day
    setattr(order, field, value)
    if field == "address":
        coordinates = await geocode_bishkek_address(order.address, settings.geocoder_url, settings.geocoder_user_agent)
        order.latitude, order.longitude = coordinates or (None, None)
    await db.commit()
    await state.clear()
    await message.answer("Данные обновлены.")
    await send_order_card(message, order, show_dispatcher_actions=True)


@router.callback_query(F.data.startswith("dispatch:cancel:"))
async def cancel_order(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status in {"delivered", "cancelled"}:
        await callback.answer("Заказ уже закрыт или не найден.", show_alert=True)
        return
    order.status = "cancelled"
    await db.commit()
    await callback.answer("Заказ отменён.")
    if callback.message:
        await callback.message.edit_text(f"Заказ #{order.id} отменён.")