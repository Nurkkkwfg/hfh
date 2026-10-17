from urllib.parse import quote

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
    address_confirm = State()
    address_type = State()
    address_details = State()
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
        "При создании или изменении адреса проверьте его поиском в 2ГИС, затем выберите тип жилья "
        "и укажите детали квартиры. В карточке можно изменить отдельное поле или отменить заказ. "
        "Во время ввода отправьте /cancel, чтобы прервать действие."
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
    await state.update_data(address_flow="create")
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
    await message.answer("Введите улицу и номер дома. Подъезд, этаж и квартиру добавим после проверки адреса в 2ГИС:")


@router.message(OrderForm.address)
async def set_address(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Адрес не должен быть пустым. Введите полный адрес:")
        return
    await state.update_data(address_base=value)
    await state.set_state(OrderForm.address_confirm)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔎 Проверить адрес в 2ГИС",
                url=f"https://2gis.kg/bishkek/search/{quote(value, safe='')}",
            )],
            [InlineKeyboardButton(text="✅ Адрес проверен", callback_data="dispatch:address:confirm")],
            [InlineKeyboardButton(text="✏️ Изменить адрес", callback_data="dispatch:address:edit")],
        ]
    )
    await message.answer(
        f"Проверьте, что найден именно нужный адрес:\n{value}\n"
        "Откройте поиск 2ГИС, сверьте улицу и номер дома, затем подтвердите адрес.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "dispatch:address:confirm")
async def confirm_address(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.address_confirm.state:
        await callback.answer("Сначала введите адрес.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("address_base"):
        await callback.answer("Адрес не найден. Введите адрес заново.", show_alert=True)
        return
    await state.set_state(OrderForm.address_type)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏢 Многоквартирный дом", callback_data="dispatch:address:type:apartment")],
            [InlineKeyboardButton(text="🏠 Частный дом", callback_data="dispatch:address:type:house")],
        ]
    )
    if callback.message:
        await callback.message.answer("Выберите тип жилья:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "dispatch:address:edit")
async def edit_address_input(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.address_confirm.state:
        await callback.answer("Сначала введите адрес.", show_alert=True)
        return
    await state.set_state(OrderForm.address)
    if callback.message:
        await callback.message.answer("Введите адрес улицы и дома для нового поиска в 2ГИС:")
    await callback.answer()


@router.callback_query(F.data.startswith("dispatch:address:type:"))
async def choose_address_type(callback: CallbackQuery, state: FSMContext, db, settings: Settings):
    if await state.get_state() != OrderForm.address_type.state:
        await callback.answer("Сначала подтвердите адрес.", show_alert=True)
        return
    address_type = callback.data.rsplit(":", 1)[1]
    if address_type not in {"apartment", "house"}:
        await callback.answer("Неизвестный тип жилья.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось продолжить ввод адреса.", show_alert=True)
        return
    await state.update_data(address_type=address_type)
    if address_type == "house":
        await _finish_address(callback.message, state, db, settings)
        await callback.answer()
        return
    await state.set_state(OrderForm.address_details)
    if callback.message:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Пропустить уточнения", callback_data="dispatch:address:skip")]
            ]
        )
        await callback.message.answer(
            "Укажите подъезд, этаж и квартиру (например: подъезд 2, этаж 4, квартира 15). "
            "Можно написать только известные данные.",
            reply_markup=keyboard,
        )
    await callback.answer()


@router.callback_query(F.data == "dispatch:address:skip")
async def skip_address_details(callback: CallbackQuery, state: FSMContext, db, settings: Settings):
    if await state.get_state() != OrderForm.address_details.state:
        await callback.answer("Сейчас уточнения адреса не запрашиваются.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось продолжить ввод адреса.", show_alert=True)
        return
    await _finish_address(callback.message, state, db, settings)
    await callback.answer()


@router.message(OrderForm.address_details)
async def set_address_details(message: Message, state: FSMContext, db, settings: Settings):
    details = (message.text or "").strip()
    if not details:
        await message.answer("Введите уточнения текстом или нажмите «Пропустить уточнения».")
        return
    await _finish_address(message, state, db, settings, details)


async def _finish_address(message, state: FSMContext, db, settings: Settings, details: str | None = None):
    data = await state.get_data()
    base_address = data.get("address_base")
    if not base_address:
        await state.clear()
        await message.answer("Адрес не найден. Начните ввод заявки заново.")
        return
    if data.get("address_type") == "house":
        address = f"{base_address} (частный дом)"
    elif details:
        address = f"{base_address}, {details}"
    else:
        address = base_address
    await state.update_data(address=address)

    if data.get("address_flow") == "edit":
        order = await db.get(Order, data["edit_order_id"])
        if order is None or order.status in {"delivered", "cancelled"}:
            await state.clear()
            await message.answer("Заказ не найден или уже закрыт.")
            return
        coordinates = await geocode_bishkek_address(
            base_address, settings.geocoder_url, settings.geocoder_user_agent
        )
        order.address = address
        order.latitude, order.longitude = coordinates or (None, None)
        await db.commit()
        await state.clear()
        await message.answer("Адрес обновлён.", reply_markup=role_keyboard(ROLE_DISPATCHER))
        await send_order_card(message, order, show_dispatcher_actions=True)
        return

    await state.set_state(OrderForm.pickup_day)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
            [KeyboardButton(text=PICKUP_OTHER_DATE)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        f"Адрес сохранён: {address}\n"
        "Когда забрать? Выберите вариант или укажите дату в формате ДД.ММ.ГГГГ:",
        reply_markup=keyboard,
    )


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
        data["address_base"], settings.geocoder_url, settings.geocoder_user_agent
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
    if field == "address":
        await state.update_data(address_flow="edit", edit_order_id=order_id)
        await state.set_state(OrderForm.address)
        if callback.message:
            await callback.message.answer("Введите адрес улицы и дома для поиска в 2ГИС:")
        await callback.answer()
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