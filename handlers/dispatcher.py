import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram import F, Router
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
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
from sqlalchemy import func, or_, select, update

from config import Settings
from database.models import Order, OrderEvent, User
from handlers.common import ROLE_DISPATCHER, RoleFilter, role_keyboard, send_order_card, status_label
from utils.dates import bishkek_today, format_bishkek_datetime, format_pickup_date, pickup_date_value
from utils.geocoding import geocode_bishkek_address
from utils.geo import haversine_km
from utils.notify import notify_couriers_today

router = Router(name="dispatcher")
router.message.filter(RoleFilter(ROLE_DISPATCHER))
router.callback_query.filter(RoleFilter(ROLE_DISPATCHER))
logger = logging.getLogger(__name__)

COURIER_MAX_ACTIVE = 5
PAGE_SIZE = 10
PICKUP_SLOTS = ("09–12", "12–15", "15–18", "18–21", "Любое")
ITEM_OPTIONS = ("Ковёр", "Шторы", "Одеяло/подушки", "Мягкая мебель", "Другое")
CANCEL_REASONS = ("Клиент передумал", "Дубль", "Не дозвонились", "Другое")
ORDER_FILTERS = {
    "all": ("Все", None),
    "new": ("Новые", ("new",)),
    "enroute": ("В пути", ("pickup_en_route", "delivery_en_route")),
    "laundry": ("В цехе", ("picked_up", "processing")),
    "ready": ("Готовы", ("ready",)),
    "delivered": ("Доставлены", ("delivered",)),
    "cancelled": ("Отменены", ("cancelled",)),
}
STATUS_ICON = {
    "new": "🆕",
    "pickup_en_route": "🟡",
    "picked_up": "🧺",
    "processing": "🧺",
    "ready": "🟢",
    "delivery_en_route": "🔵",
    "delivered": "✅",
    "cancelled": "❌",
    "pickup_failed": "⚠️",
    "delivery_failed": "⚠️",
}

class OrderForm(StatesGroup):
    phone = State()
    client_match = State()
    client_name = State()
    address = State()
    address_confirm = State()
    address_type = State()
    address_details = State()
    pickup_day = State()
    pickup_slot = State()
    items = State()
    comment = State()
    confirm = State()
    edit_value = State()
    edit_comment = State()
    search = State()
    cancel_reason = State()
    cancel_other = State()
    cancel_confirm = State()
    reschedule_date = State()
    reschedule_slot = State()


PICKUP_OTHER_DATE = "📅 Другая дата"
EDIT_FIELDS = {
    "client_name": "Имя",
    "client_phone": "Телефон",
    "address": "Адрес",
    "pickup_date": "Дата забора",
    "pickup_slot": "Интервал",
    "items": "Что забрать",
    "comment": "Комментарий",
}
DATE_EDITABLE_STATUSES = {"new", "pickup_failed", "delivery_failed"}
OPEN_STATUSES = {
    "new",
    "pickup_en_route",
    "picked_up",
    "processing",
    "ready",
    "delivery_en_route",
    "pickup_failed",
    "delivery_failed",
}


def normalize_phone(value: str) -> str | None:
    stripped = value.strip()
    if not re.fullmatch(r"[+\d\s().-]+", stripped):
        return None
    digits = re.sub(r"\D", "", stripped)
    if digits.startswith("996"):
        digits = digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = digits[1:]
    if len(digits) != 9:
        return None
    return f"+996{digits}"


def address_base_from_full_address(address: str) -> str:
    apartment_details = re.search(
        r",\s*(?:подъезд|под\.?|этаж|эт\.?|квартира|кв\.?)\s*",
        address,
        flags=re.IGNORECASE,
    )
    if apartment_details:
        return address[:apartment_details.start()].strip()
    private_house_suffix = " (частный дом)"
    if address.endswith(private_house_suffix):
        return address[:-len(private_house_suffix)].strip()
    return address.strip()


def format_order_line(order: Order, *, overdue: bool = False) -> str:
    address = order.address.split(",")[0].strip()
    date_text = format_pickup_date(order.pickup_date)
    marker = "⚠️ " if overdue else ""
    return (
        f"{marker}#{order.id} · {STATUS_ICON.get(order.status, '⚪')} "
        f"{status_label(order.status)} · {address} · {date_text} {order.pickup_slot}"
    )


def list_markup(filter_name: str, page: int, pages: int) -> InlineKeyboardMarkup:
    filter_buttons = [
        InlineKeyboardButton(
            text=("✅ " if key == filter_name else "") + title,
            callback_data=f"ds:list:{key}:0",
        )
        for key, (title, _) in ORDER_FILTERS.items()
    ]
    rows = [filter_buttons[index:index + 3] for index in range(0, len(filter_buttons), 3)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ds:list:{filter_name}:{page - 1}"))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=f"ds:list:{filter_name}:{page}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ds:list:{filter_name}:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def step_back_markup(previous: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ds:back:{previous}")]
        ]
    )


async def prompt_pickup_date(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="ds:date:today"),
                InlineKeyboardButton(text="Завтра", callback_data="ds:date:tomorrow"),
            ],
            [InlineKeyboardButton(text=PICKUP_OTHER_DATE, callback_data="ds:date:other")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:address_details")],
        ]
    )
    await message.answer("Выберите дату забора:", reply_markup=keyboard)


async def prompt_pickup_slot(message: Message) -> None:
    buttons = [
        InlineKeyboardButton(text=slot, callback_data=f"ds:slot:{index}")
        for index, slot in enumerate(PICKUP_SLOTS)
    ]
    await message.answer(
        "Выберите интервал забора:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[button] for button in buttons]
            + [[InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:pickup_day")]]
        ),
    )


def item_keyboard(items: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if item in items else "") + item,
                callback_data=f"ds:item:{index}",
            )
        ]
        for index, item in enumerate(ITEM_OPTIONS)
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="✅ Готово", callback_data="ds:item:done")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:pickup_slot")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def prompt_items(message: Message, items: list[str]) -> None:
    await message.answer("Что забрать? Можно выбрать несколько вариантов:", reply_markup=item_keyboard(items))


async def order_summary(message: Message, data: dict) -> None:
    text = (
        "Проверьте заявку:\n"
        f"Телефон: {data['client_phone']}\nКлиент: {data['client_name']}\n"
        f"Адрес: {data['address']}\n"
        f"Забор: {format_pickup_date(data['pickup_date'])} {data['pickup_slot']}\n"
        f"Что забрать: {', '.join(data['items']) or 'не указано'}\n"
        f"Комментарий: {data.get('comment') or 'нет'}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="ds:create")],
            [InlineKeyboardButton(text="✏️ Исправить поле", callback_data="ds:review:edit")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="ds:review:cancel")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:comment")],
        ]
    )
    await message.answer(text, reply_markup=keyboard)


async def record_event(
    db,
    order: Order,
    *,
    actor_id: int | None,
    actor_name: str,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    note: str | None = None,
) -> None:
    db.add(
        OrderEvent(
            order_id=order.id,
            actor_id=actor_id,
            actor_name=actor_name,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            note=note,
        )
    )


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def dispatcher_help(message: Message):
    await message.answer(
        "Диспетчер:\n"
        "➕ Новая заявка — телефон, клиент, адрес (проверьте по карте), дата, интервал и вещи.\n"
        "🔎 Поиск — номер, телефон, имя или адрес; 📋 Заказы — список с фильтрами.\n"
        "⚠️ Проблемные — просроченные и требующие внимания заказы; 📅 На сегодня — сегодняшние заборы.\n"
        "Отменять заказ, менять адрес или дату можно только в статусах «Новый» и «Проблема»; "
        "контакт и комментарий доступны до закрытия заказа. Причину отмены выберите в меню.\n"
        "Во время ввода используйте «◀️ Назад» или /cancel."
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
    await state.set_state(OrderForm.phone)
    await message.answer("Введите телефон клиента, например +996555123456:")


@router.message(OrderForm.phone)
async def set_client_phone(message: Message, state: FSMContext, db):
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer("Неверный номер. Введите номер Кыргызстана в формате +996XXXXXXXXX:")
        return
    previous = await db.scalar(
        select(Order)
        .where(Order.client_phone == phone)
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    await state.update_data(client_phone=phone)
    if previous is not None:
        await state.update_data(
            previous_client_name=previous.client_name,
            previous_address=previous.address,
            previous_pickup_base=address_base_from_full_address(previous.address),
        )
        await state.set_state(OrderForm.client_match)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Использовать данные", callback_data="ds:client:reuse")],
                [InlineKeyboardButton(text="✏️ Ввести заново", callback_data="ds:client:new")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:phone")],
            ]
        )
        await message.answer(
            f"Найден клиент: {previous.client_name}, последний адрес {previous.address}",
            reply_markup=keyboard,
        )
        return
    await state.set_state(OrderForm.client_name)
    await message.answer("Введите имя клиента:", reply_markup=step_back_markup("phone"))


@router.callback_query(F.data == "ds:client:reuse")
async def reuse_client(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await state.get_state() != OrderForm.client_match.state or not data.get("previous_address"):
        await callback.answer("Данные клиента больше недоступны.", show_alert=True)
        return
    await state.update_data(
        client_name=data["previous_client_name"],
        address=data["previous_address"],
        address_base=data["previous_pickup_base"],
    )
    await state.set_state(OrderForm.pickup_day)
    if callback.message:
        await prompt_pickup_date(callback.message)
    await callback.answer()


@router.callback_query(F.data == "ds:client:new")
async def enter_new_client_data(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.client_match.state:
        await callback.answer("Сначала введите телефон клиента.", show_alert=True)
        return
    await state.set_state(OrderForm.client_name)
    if callback.message:
        await callback.message.answer("Введите имя клиента:", reply_markup=step_back_markup("phone"))
    await callback.answer()


@router.message(OrderForm.client_name)
async def set_client_name(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Имя не должно быть пустым. Введите имя клиента:")
        return
    await state.update_data(client_name=value)
    await state.set_state(OrderForm.address)
    await message.answer(
        "Введите улицу и номер дома. Подъезд, этаж и квартиру добавим после проверки адреса на карте:",
        reply_markup=step_back_markup("client_name"),
    )


def address_confirmation_markup(*, can_confirm: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_confirm:
        rows.append([InlineKeyboardButton(text="✅ Адрес на карте верный", callback_data="dispatch:address:confirm")])
    rows.extend(
        [
            [InlineKeyboardButton(text="✏️ Изменить адрес", callback_data="dispatch:address:edit")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:client_name")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def address_map_url(latitude: float, longitude: float) -> str:
    return (
        "https://static.maps.2gis.com/1.0"
        f"?center={longitude},{latitude}"
        f"&zoom=16"
        f"&size=600,400"
        f"&markers={longitude},{latitude}"
    )


async def prompt_address_confirmation(message: Message, address: str, coordinates: tuple[float, float] | None):
    keyboard = address_confirmation_markup(can_confirm=coordinates is not None)
    if coordinates is None:
        await message.answer(
            f"Не удалось найти адрес на карте: {address}\nИзмените адрес и попробуйте снова.",
            reply_markup=keyboard,
        )
        return
    latitude, longitude = coordinates
    await message.answer_photo(
        photo=address_map_url(latitude, longitude),
        caption=f"Проверьте точку на карте для адреса:\n{address}",
        reply_markup=keyboard,
    )


@router.message(OrderForm.address)
async def set_address(message: Message, state: FSMContext, settings: Settings):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Адрес не должен быть пустым. Введите полный адрес:")
        return
    coordinates = await geocode_bishkek_address(
        value, settings.geocoder_url, settings.geocoder_user_agent
    )
    await state.update_data(
        address_base=value,
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
    )
    await state.set_state(OrderForm.address_confirm)
    await prompt_address_confirmation(message, value, coordinates)


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
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:address_confirm")],
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
        await callback.message.answer("Введите адрес улицы и дома для поиска на карте:")
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
                [InlineKeyboardButton(text="Пропустить уточнения", callback_data="dispatch:address:skip")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:address_type")],
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
        coordinates = await geocode_bishkek_address(
            base_address, settings.geocoder_url, settings.geocoder_user_agent
        )
        result = await db.execute(
            update(Order)
            .where(
                Order.id == data["edit_order_id"],
                Order.status.in_(DATE_EDITABLE_STATUSES),
            )
            .values(
                address=address,
                latitude=coordinates[0] if coordinates else None,
                longitude=coordinates[1] if coordinates else None,
            )
        )
        if result.rowcount != 1:
            await db.rollback()
            await state.clear()
            await message.answer("Адрес можно изменить только у нового заказа.")
            return
        order = await db.get(Order, data["edit_order_id"])
        await record_event(
            db,
            order,
            actor_id=message.from_user.id,
            actor_name=message.from_user.full_name,
            note="изменено поле Адрес",
        )
        await db.commit()
        order = await db.get(Order, data["edit_order_id"])
        await state.clear()
        await message.answer("Адрес обновлён.", reply_markup=role_keyboard(ROLE_DISPATCHER))
        await send_order_card(message, order, show_dispatcher_actions=True)
        return

    if data.get("review_mode"):
        coordinates = await geocode_bishkek_address(
            base_address, settings.geocoder_url, settings.geocoder_user_agent
        )
        await state.update_data(
            latitude=coordinates[0] if coordinates else None,
            longitude=coordinates[1] if coordinates else None,
        )
        await state.set_state(OrderForm.confirm)
        await order_summary(message, await state.get_data())
    else:
        await state.set_state(OrderForm.pickup_day)
        await message.answer(f"Адрес сохранён: {address}")
        await prompt_pickup_date(message)


@router.callback_query(F.data.startswith("ds:back:"))
async def go_back(callback: CallbackQuery, state: FSMContext, db):
    previous = callback.data.rsplit(":", 1)[1]
    if previous == "order_card":
        data = await state.get_data()
        order_id = data.get("edit_order_id")
        await state.clear()
        order = await db.get(Order, order_id) if order_id else None
        if callback.message and order:
            await dispatcher_order_card(callback.message, db, order)
        await callback.answer()
        return
    if previous == "search":
        await state.clear()
        if callback.message:
            await callback.message.answer("Поиск отменён.", reply_markup=role_keyboard(ROLE_DISPATCHER))
        await callback.answer()
        return
    states = {
        "phone": (OrderForm.phone, "Введите телефон клиента, например +996555123456:"),
        "client_name": (OrderForm.client_name, "Введите имя клиента:"),
        "address": (
            OrderForm.address,
            "Введите улицу и номер дома для поиска на карте:",
        ),
        "address_confirm": (OrderForm.address_confirm, "Проверьте адрес на карте или измените его."),
        "address_type": (OrderForm.address_type, "Выберите тип жилья."),
        "address_details": (OrderForm.address_details, "Укажите подъезд, этаж и квартиру."),
        "pickup_day": (OrderForm.pickup_day, "Выберите дату забора."),
        "pickup_slot": (OrderForm.pickup_slot, "Выберите интервал забора."),
        "items": (OrderForm.items, "Выберите, что забрать."),
        "comment": (OrderForm.comment, "Добавьте комментарий или пропустите."),
    }
    step = states.get(previous)
    if step is None:
        await callback.answer("Неизвестный шаг.", show_alert=True)
        return
    await state.set_state(step[0])
    if callback.message:
        data = await state.get_data()
        if previous == "address_confirm" and data.get("address_base"):
            value = data["address_base"]
            latitude, longitude = data.get("latitude"), data.get("longitude")
            coordinates = (latitude, longitude) if latitude is not None and longitude is not None else None
            await prompt_address_confirmation(callback.message, value, coordinates)
        elif previous == "address_type":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏢 Многоквартирный дом", callback_data="dispatch:address:type:apartment")],
                    [InlineKeyboardButton(text="🏠 Частный дом", callback_data="dispatch:address:type:house")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:address_confirm")],
                ]
            )
            await callback.message.answer(step[1], reply_markup=keyboard)
        elif previous == "address_details":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Пропустить уточнения", callback_data="dispatch:address:skip")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:address_type")],
                ]
            )
            await callback.message.answer(step[1], reply_markup=keyboard)
        elif previous == "pickup_day":
            await prompt_pickup_date(callback.message)
        elif previous == "pickup_slot":
            await prompt_pickup_slot(callback.message)
        elif previous == "items":
            await prompt_items(callback.message, data.get("items", []))
        elif previous == "comment":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Пропустить", callback_data="ds:comment:skip")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:items")],
                ]
            )
            await callback.message.answer(step[1], reply_markup=keyboard)
        elif previous == "client_name":
            await callback.message.answer(step[1], reply_markup=step_back_markup("phone"))
        elif previous == "address":
            await callback.message.answer(step[1], reply_markup=step_back_markup("client_name"))
        elif previous == "phone":
            await callback.message.answer(step[1])
        elif previous == "pickup_slot":
            await prompt_pickup_slot(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("ds:date:"))
async def choose_pickup_date(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.pickup_day.state:
        await callback.answer("Сейчас дата не запрашивается.", show_alert=True)
        return
    choice = callback.data.rsplit(":", 1)[1]
    if choice == "other":
        await callback.message.answer(
            "Введите дату в формате ДД.ММ.ГГГГ:",
            reply_markup=step_back_markup("address_details"),
        )
        await callback.answer()
        return
    value = "Сегодня" if choice == "today" else "Завтра" if choice == "tomorrow" else None
    pickup_date = pickup_date_value(value) if value else None
    if pickup_date is None:
        await callback.answer("Не удалось определить дату.", show_alert=True)
        return
    await state.update_data(pickup_date=pickup_date)
    await state.set_state(OrderForm.pickup_slot)
    if callback.message:
        await prompt_pickup_slot(callback.message)
    await callback.answer()


@router.message(OrderForm.pickup_day)
async def set_pickup_date(message: Message, state: FSMContext):
    pickup_date = pickup_date_value(message.text or "")
    if pickup_date is None:
        await message.answer(
            "Укажите «Сегодня», «Завтра» или будущую дату в формате ДД.ММ.ГГГГ:",
            reply_markup=step_back_markup("address_details"),
        )
        return
    await state.update_data(pickup_date=pickup_date)
    await state.set_state(OrderForm.pickup_slot)
    await prompt_pickup_slot(message)


@router.callback_query(F.data.startswith("ds:slot:"))
async def choose_pickup_slot(callback: CallbackQuery, state: FSMContext, db):
    current_state = await state.get_state()
    if current_state == OrderForm.reschedule_slot.state:
        await save_rescheduled_slot(callback, state, db)
        return
    if current_state != OrderForm.pickup_slot.state:
        await callback.answer("Сейчас интервал не запрашивается.", show_alert=True)
        return
    try:
        slot_index = int(callback.data.rsplit(":", 1)[1])
        slot = PICKUP_SLOTS[slot_index]
    except (ValueError, IndexError):
        await callback.answer("Неизвестный интервал.", show_alert=True)
        return
    data = await state.get_data()
    if data.get("edit_order_id"):
        order = await db.get(Order, data["edit_order_id"])
        if order is None or not await save_order_field(
            db,
            order,
            "pickup_slot",
            slot,
            callback.from_user.id,
            callback.from_user.full_name,
        ):
            await callback.answer("Не удалось изменить интервал.", show_alert=True)
            return
        await db.refresh(order)
        await state.clear()
        if callback.message:
            await dispatcher_order_card(callback.message, db, order)
        await callback.answer("Интервал изменён.")
        return
    if data.get("review_mode"):
        await state.update_data(pickup_slot=slot)
        await state.set_state(OrderForm.confirm)
        if callback.message:
            await order_summary(callback.message, await state.get_data())
        await callback.answer()
        return
    await state.update_data(pickup_slot=slot, items=[])
    await state.set_state(OrderForm.items)
    if callback.message:
        await prompt_items(callback.message, [])
    await callback.answer()


@router.callback_query(F.data.startswith("ds:item:"))
async def toggle_order_item(callback: CallbackQuery, state: FSMContext, db):
    if await state.get_state() != OrderForm.items.state:
        await callback.answer("Сейчас выбор вещей не запрашивается.", show_alert=True)
        return
    choice = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("items", []))
    if choice == "done":
        if not selected:
            await callback.answer("Выберите хотя бы один вариант.", show_alert=True)
            return
        if data.get("edit_order_id"):
            order = await db.get(Order, data["edit_order_id"])
            if order is None or not await save_order_field(
                db,
                order,
                "items",
                selected,
                callback.from_user.id,
                callback.from_user.full_name,
            ):
                await callback.answer("Не удалось изменить состав заказа.", show_alert=True)
                return
            await db.refresh(order)
            await state.clear()
            if callback.message:
                await dispatcher_order_card(callback.message, db, order)
            await callback.answer("Состав заказа обновлён.")
            return
        if data.get("review_mode"):
            await state.set_state(OrderForm.confirm)
            if callback.message:
                await order_summary(callback.message, data)
            await callback.answer()
            return
        await state.set_state(OrderForm.comment)
        if callback.message:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Пропустить", callback_data="ds:comment:skip")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="ds:back:items")],
                ]
            )
            await callback.message.answer("Добавьте комментарий к заказу или пропустите:", reply_markup=keyboard)
        await callback.answer()
        return
    try:
        item = ITEM_OPTIONS[int(choice)]
    except (ValueError, IndexError):
        await callback.answer("Неизвестный тип вещи.", show_alert=True)
        return
    if item in selected:
        selected.remove(item)
    else:
        selected.append(item)
    await state.update_data(items=selected)
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=item_keyboard(selected))
    await callback.answer()


@router.message(OrderForm.comment)
async def set_order_comment(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("Комментарий должен быть текстом или используйте «Пропустить».")
        return
    await state.update_data(comment=comment)
    await state.set_state(OrderForm.confirm)
    await order_summary(message, await state.get_data())


@router.callback_query(F.data == "ds:comment:skip")
async def skip_order_comment(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.comment.state:
        await callback.answer("Комментарий сейчас не запрашивается.", show_alert=True)
        return
    await state.update_data(comment=None)
    await state.set_state(OrderForm.confirm)
    if callback.message:
        await order_summary(callback.message, await state.get_data())
    await callback.answer()


@router.callback_query(F.data == "ds:review:edit")
async def edit_order_review(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.confirm.state:
        await callback.answer("Сначала завершите ввод заявки.", show_alert=True)
        return
    options = [
        ("client_phone", "Телефон"),
        ("client_name", "Имя"),
        ("address", "Адрес"),
        ("pickup_date", "Дата"),
        ("pickup_slot", "Интервал"),
        ("items", "Что забрать"),
        ("comment", "Комментарий"),
    ]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"ds:review:field:{field}")]
            for field, label in options
        ]
        + [[InlineKeyboardButton(text="◀️ Назад", callback_data="ds:review:summary")]]
    )
    if callback.message:
        await callback.message.answer("Какое поле исправить?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "ds:review:summary")
async def return_to_order_review(callback: CallbackQuery, state: FSMContext):
    if callback.message:
        await order_summary(callback.message, await state.get_data())
    await callback.answer()


@router.callback_query(F.data.startswith("ds:review:field:"))
async def edit_review_field(callback: CallbackQuery, state: FSMContext):
    field = callback.data.rsplit(":", 1)[1]
    if field not in EDIT_FIELDS:
        await callback.answer("Неизвестное поле.", show_alert=True)
        return
    await state.update_data(review_mode=True)
    if field == "address":
        await state.update_data(address_flow="create")
        await state.set_state(OrderForm.address)
        prompt = "Введите улицу и номер дома для поиска на карте:"
    elif field == "items":
        await state.set_state(OrderForm.items)
        if callback.message:
            await prompt_items(callback.message, (await state.get_data()).get("items", []))
        await callback.answer()
        return
    elif field == "pickup_date":
        await state.set_state(OrderForm.pickup_day)
        if callback.message:
            await prompt_pickup_date(callback.message)
        await callback.answer()
        return
    elif field == "pickup_slot":
        await state.set_state(OrderForm.pickup_slot)
        if callback.message:
            await prompt_pickup_slot(callback.message)
        await callback.answer()
        return
    else:
        await state.set_state(OrderForm.edit_value)
        await state.update_data(edit_field=field, review_mode=True)
        prompt = {
            "client_phone": "Введите новый телефон клиента:",
            "client_name": "Введите новое имя клиента:",
            "comment": "Введите новый комментарий или «-» чтобы очистить:",
        }[field]
    if callback.message:
        await callback.message.answer(prompt)
    await callback.answer()


@router.callback_query(F.data == "ds:review:cancel")
async def cancel_order_review(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != OrderForm.confirm.state:
        await callback.answer("Сначала завершите ввод заявки.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Создание заявки отменено.", reply_markup=role_keyboard(ROLE_DISPATCHER))
    await callback.answer()


@router.callback_query(F.data == "ds:create")
async def create_order(callback: CallbackQuery, state: FSMContext, db, bot: Bot, settings: Settings):
    if await state.get_state() != OrderForm.confirm.state:
        await callback.answer("Заявка больше не готова к созданию.", show_alert=True)
        return
    data = await state.get_data()
    coordinates = await geocode_bishkek_address(
        data.get("address_base", data["address"]),
        settings.geocoder_url,
        settings.geocoder_user_agent,
    )
    if coordinates:
        data["latitude"], data["longitude"] = coordinates
    order = Order(
        client_name=data["client_name"],
        client_phone=data["client_phone"],
        address=data["address"],
        pickup_date=data["pickup_date"],
        pickup_slot=data["pickup_slot"],
        items=json.dumps(data["items"], ensure_ascii=False),
        comment=data.get("comment"),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
    )
    db.add(order)
    await db.flush()
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        to_status="new",
        note="Заказ создан",
    )
    await db.commit()
    await state.clear()
    if callback.message:
        await callback.message.answer(f"Заказ #{order.id} создан.", reply_markup=role_keyboard(ROLE_DISPATCHER))
        await dispatcher_order_card(callback.message, db, order)
    if order.pickup_date == bishkek_today():
        await notify_couriers_today(bot, db, order)
    await callback.answer("Заказ создан.")


@router.message(Command("orders"))
@router.message(F.text == "📋 Заказы")
async def list_orders(message: Message, db):
    await send_order_list(message, db, "all", 0)


async def send_order_list(message: Message, db, filter_name: str, page: int):
    if filter_name not in ORDER_FILTERS:
        filter_name = "all"
    if filter_name == "today":
        await send_today_list(message, db, page)
        return
    query = select(Order)
    statuses = ORDER_FILTERS[filter_name][1]
    if statuses:
        query = query.where(Order.status.in_(statuses))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    orders = (
        await db.scalars(query.order_by(Order.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE))
    ).all()
    if not orders:
        await message.answer("Заказов пока нет.", reply_markup=list_markup(filter_name, page, pages))
        return
    lines = [f"{format_order_line(order)}" for order in orders]
    keyboard = list_markup(filter_name, page, pages)
    rows = [*keyboard.inline_keyboard]
    rows[0:0] = [
        [
            InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
            for order in orders[index:index + 5]
        ]
        for index in range(0, len(orders), 5)
    ]
    await message.answer(
        f"Заказы · {ORDER_FILTERS[filter_name][0]} · страница {page + 1}/{pages}\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ds:list:"))
async def list_page(callback: CallbackQuery, db):
    try:
        _, _, filter_name, page_raw = callback.data.split(":", 3)
        page = int(page_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректный запрос списка.", show_alert=True)
        return
    if filter_name == "today":
        await replace_with_today_list(callback, db, page)
        return
    if filter_name not in ORDER_FILTERS:
        await callback.answer("Неизвестный фильтр.", show_alert=True)
        return
    await replace_with_order_list(callback, db, filter_name, page)


async def replace_with_order_list(callback: CallbackQuery, db, filter_name: str, page: int):
    query = select(Order)
    statuses = ORDER_FILTERS[filter_name][1]
    if statuses:
        query = query.where(Order.status.in_(statuses))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    orders = (
        await db.scalars(query.order_by(Order.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE))
    ).all()
    text = (
        f"Заказы · {ORDER_FILTERS[filter_name][0]} · страница {page + 1}/{pages}\n"
        + ("\n".join(format_order_line(order) for order in orders) if orders else "Заказов нет.")
    )
    keyboard = list_markup(filter_name, page, pages)
    rows = list(keyboard.inline_keyboard)
    rows[0:0] = [
        [
            InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
            for order in orders[index:index + 5]
        ]
        for index in range(0, len(orders), 5)
    ]
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def send_today_list(message: Message, db, page: int) -> None:
    today = bishkek_today()
    query = select(Order).where(
        or_(
            Order.pickup_date == today,
            (Order.status == "new") & (Order.pickup_date < today),
        )
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    orders = (
        await db.scalars(
            query.order_by(Order.pickup_date, Order.id).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
    ).all()
    overdue = lambda order: order.status == "new" and order.pickup_date < today
    lines = [format_order_line(order, overdue=overdue(order)) for order in orders]
    markup = list_markup("all", page, pages)
    rows = [*markup.inline_keyboard[:-1]]
    rows.extend(
        [
            [
                InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
                for order in orders[index:index + 5]
            ]
            for index in range(0, len(orders), 5)
        ]
    )
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"ds:list:today:{page - 1}"))
    navigation.append(InlineKeyboardButton(text="🔄", callback_data=f"ds:list:today:{page}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"ds:list:today:{page + 1}"))
    rows.append(navigation)
    await message.answer(
        f"Заказы на сегодня · страница {page + 1}/{pages}\n"
        + ("\n".join(lines) if lines else "Заказов нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def replace_with_today_list(callback: CallbackQuery, db, page: int) -> None:
    today = bishkek_today()
    query = select(Order).where(
        or_(
            Order.pickup_date == today,
            (Order.status == "new") & (Order.pickup_date < today),
        )
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    orders = (
        await db.scalars(
            query.order_by(Order.pickup_date, Order.id).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
    ).all()
    rows = list_markup("all", page, pages).inline_keyboard[:-1]
    rows.extend(
        [
            [
                InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
                for order in orders[index:index + 5]
            ]
            for index in range(0, len(orders), 5)
        ]
    )
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ds:list:today:{page - 1}"))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=f"ds:list:today:{page}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ds:list:today:{page + 1}"))
    rows.append(nav)
    if callback.message:
        await callback.message.edit_text(
            f"Заказы на сегодня · страница {page + 1}/{pages}\n"
            + ("\n".join(format_order_line(o, overdue=o.status == "new" and o.pickup_date < today) for o in orders)
               if orders else "Заказов нет."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.message(F.text == "📅 На сегодня")
async def today_orders(message: Message, db):
    await send_today_list(message, db, 0)


@router.callback_query(F.data.startswith("ds:card:"))
async def show_order_card_callback(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if callback.message:
        await dispatcher_order_card(callback.message, db, order)
    await callback.answer()


async def dispatcher_order_card(message: Message, db, order: Order):
    courier_pickup = await db.get(User, order.pickup_courier_id) if order.pickup_courier_id else None
    courier_delivery = await db.get(User, order.delivery_courier_id) if order.delivery_courier_id else None
    events = (
        await db.scalars(
            select(OrderEvent)
            .where(OrderEvent.order_id == order.id)
            .order_by(OrderEvent.created_at.desc())
            .limit(5)
        )
    ).all()
    try:
        items = json.loads(order.items or "[]")
    except json.JSONDecodeError:
        items = [order.items] if order.items else []
    text = (
        f"Заказ #{order.id} · {STATUS_ICON.get(order.status, '⚪')} {status_label(order.status)}\n"
        f"Клиент: {order.client_name}\nТелефон: {order.client_phone}\n"
        f"Адрес: {order.address}\nЗабор: {format_pickup_date(order.pickup_date)} {order.pickup_slot}\n"
        f"Что забрать: {', '.join(items) or 'не указано'}\n"
        f"Комментарий: {order.comment or 'нет'}\n"
        f"Курьер забора: {courier_pickup.full_name if courier_pickup else 'не назначен'}\n"
        f"Курьер доставки: {courier_delivery.full_name if courier_delivery else 'не назначен'}\n"
        f"Замеры: {order.measurements or 'нет'}\n"
        f"Сумма: {order.total_amount} сом · {'оплачено' if order.is_paid else 'не оплачено'}\n"
    )
    if events:
        text += "\nИстория:\n" + "\n".join(
            f"{format_bishkek_datetime(event.created_at)} · {event.actor_name} · "
            f"{status_label(event.from_status) if event.from_status else '—'} → "
            f"{status_label(event.to_status) if event.to_status else '—'}"
            f"{f' · {event.reason}' if event.reason else ''}"
            f"{f' · {event.note}' if event.note else ''}"
            for event in events
        )
    keyboard = order_card_keyboard(order)
    await message.answer(text, reply_markup=keyboard)


def order_card_keyboard(order: Order) -> InlineKeyboardMarkup | None:
    rows = []
    if order.status == "new":
        rows.append(
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"dispatch:edit:{order.id}"),
                InlineKeyboardButton(text="🚴 Назначить курьера", callback_data=f"ds:assign:{order.id}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="📞 Позвонить", callback_data=f"ds:call:{order.id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"ds:cancel:{order.id}"),
            ]
        )
    elif order.status in {"pickup_en_route", "delivery_en_route"}:
        rows.append(
            [
                InlineKeyboardButton(text="📞 Позвонить клиенту", callback_data=f"ds:call:{order.id}"),
                InlineKeyboardButton(text="📞 Связаться с курьером", callback_data=f"ds:courier:{order.id}"),
            ]
        )
        rows.append([InlineKeyboardButton(text="🔁 Снять с курьера", callback_data=f"ds:unassign:{order.id}")])
    elif order.status in {"pickup_failed", "delivery_failed"}:
        rows.append(
            [
                InlineKeyboardButton(text="🔁 Вернуть в очередь", callback_data=f"ds:requeue:{order.id}"),
                InlineKeyboardButton(text="📅 Перенести дату", callback_data=f"ds:reschedule:{order.id}"),
            ]
        )
        rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"ds:cancel:{order.id}")])
    elif order.status in {"picked_up", "processing", "ready"}:
        rows.append(
            [InlineKeyboardButton(text="✏️ Изменить контакт/комментарий", callback_data=f"dispatch:edit:{order.id}")]
        )
    elif order.status == "delivered":
        rows.append([InlineKeyboardButton(text="🔁 Повторить заказ", callback_data=f"ds:repeat:{order.id}")])
        if not order.is_paid:
            rows.append([InlineKeyboardButton(text="💰 Отметить оплату", callback_data=f"ds:paid:{order.id}")])
    if (
        order.status in DATE_EDITABLE_STATUSES
        and (order.latitude is None or order.longitude is None)
    ):
        rows.append([InlineKeyboardButton(text="✏️ Исправить адрес", callback_data=f"ds:fix_address:{order.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.callback_query(F.data.startswith("ds:fix_address:"))
async def begin_fix_address(callback: CallbackQuery, state: FSMContext, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status not in DATE_EDITABLE_STATUSES:
        await callback.answer("Адрес можно изменить только у нового или проблемного заказа.", show_alert=True)
        return
    await state.clear()
    await state.update_data(address_flow="edit", edit_order_id=order_id)
    await state.set_state(OrderForm.address)
    if callback.message:
        await callback.message.answer(
            "Введите улицу и номер дома для поиска на карте:",
            reply_markup=step_back_markup("order_card"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ds:paid:"))
async def mark_order_paid(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    result = await db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == "delivered",
            Order.is_paid.is_(False),
        )
        .values(is_paid=True, paid_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Оплата уже отмечена или заказ не доставлен.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        from_status="delivered",
        to_status="delivered",
        note="Отмечена оплата",
    )
    await db.commit()
    if callback.message:
        await dispatcher_order_card(callback.message, db, order)
    await callback.answer("Оплата отмечена.")


@router.message(F.text == "🔎 Поиск")
@router.message(Command("search"))
async def start_search(message: Message, state: FSMContext):
    await state.set_state(OrderForm.search)
    await message.answer("Введите номер (#15), телефон, имя клиента или часть адреса:", reply_markup=step_back_markup("search"))


@router.callback_query(F.data == "ds:back:search")
async def cancel_search(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.answer("Поиск отменён.", reply_markup=role_keyboard(ROLE_DISPATCHER))
    await callback.answer()


@router.message(OrderForm.search)
async def search_orders(message: Message, state: FSMContext, db):
    term = (message.text or "").strip()
    if not term:
        await message.answer("Введите номер, телефон, имя клиента или адрес.")
        return
    orders = await find_orders(db, term)
    if not orders:
        await message.answer("Ничего не найдено.")
        return
    await state.clear()
    await state.update_data(search_ids=[order.id for order in orders], search_page=0)
    await send_search_page(message, orders, 0)


async def find_orders(db, term: str) -> list[Order]:
    if term.startswith("#") or term.isdigit() and len(term) < 6:
        try:
            order_id = int(term.lstrip("#"))
        except ValueError:
            return []
        order = await db.get(Order, order_id)
        return [order] if order else []
    digits = re.sub(r"\D", "", term)
    if len(digits) >= 6:
        return list(
            (
                await db.scalars(
                    select(Order)
                    .where(Order.client_phone.ilike(f"%{digits}%"))
                    .order_by(Order.created_at.desc())
                    .limit(100)
                )
            ).all()
        )
    pattern = f"%{term}%"
    return list(
        (
            await db.scalars(
                select(Order)
                .where(or_(Order.client_name.ilike(pattern), Order.address.ilike(pattern)))
                .order_by(Order.created_at.desc())
                .limit(100)
            )
        ).all()
    )


async def send_search_page(message: Message, orders: list[Order], page: int):
    pages = max(1, (len(orders) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    entries = orders[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = f"Результаты поиска · страница {page + 1}/{pages}\n" + "\n".join(
        format_order_line(order) for order in entries
    )
    rows = [
        [
            InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
            for order in entries[index:index + 5]
        ]
        for index in range(0, len(entries), 5)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ds:search:{page - 1}"))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=f"ds:search:{page}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ds:search:{page + 1}"))
    rows.append(nav)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ds:search:"))
async def search_result_page(callback: CallbackQuery, state: FSMContext, db):
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    data = await state.get_data()
    ids = data.get("search_ids", [])
    if not ids:
        await callback.answer("Результаты поиска устарели. Выполните поиск заново.", show_alert=True)
        return
    orders = list((await db.scalars(select(Order).where(Order.id.in_(ids)).order_by(Order.id.desc()))).all())
    pages = max(1, (len(orders) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    entries = orders[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = f"Результаты поиска · страница {page + 1}/{pages}\n" + "\n".join(
        format_order_line(order) for order in entries
    )
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ds:search:{page - 1}"))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=f"ds:search:{page}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ds:search:{page + 1}"))
    rows = [
        [
            InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
            for order in entries[index:index + 5]
        ]
        for index in range(0, len(entries), 5)
    ]
    rows.append(nav)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.message(F.text == "⚠️ Проблемные")
async def problematic_orders(message: Message, db):
    today = bishkek_today()
    current_time = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = current_time - timedelta(hours=3)
    route_started_at = (
        select(func.max(OrderEvent.created_at))
        .where(
            OrderEvent.order_id == Order.id,
            OrderEvent.to_status == Order.status,
        )
        .scalar_subquery()
    )
    orders = (
        await db.scalars(
            select(Order)
            .where(
                or_(
                    Order.status.in_(["pickup_failed", "delivery_failed"]),
                    (Order.status == "new") & (Order.pickup_date < today),
                    Order.status.in_(["pickup_en_route", "delivery_en_route"])
                    & (func.coalesce(route_started_at, Order.created_at) < cutoff),
                    (Order.status == "delivered") & (Order.is_paid.is_(False)),
                    Order.latitude.is_(None) | Order.longitude.is_(None),
                )
            )
            .order_by(Order.id.desc())
            .limit(100)
        )
    ).all()
    if not orders:
        await message.answer("Проблемных заказов нет.")
        return
    text = "Проблемные заказы:\n" + "\n".join(format_order_line(order) for order in orders[:PAGE_SIZE])
    rows = [
        [
            InlineKeyboardButton(text=f"#{order.id}", callback_data=f"ds:card:{order.id}")
            for order in orders[index:index + 5]
        ]
        for index in range(0, min(len(orders), PAGE_SIZE), 5)
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(F.text == "📊 Сводка дня")
async def daily_summary(message: Message, db):
    await show_daily_summary(message, db, 0)


async def show_daily_summary(message: Message, db, period: int):
    today = bishkek_today()
    if period == 1:
        start_date, end_date = today - timedelta(days=1), today
        period_label = "Вчера"
    elif period == 7:
        start_date, end_date = today - timedelta(days=6), today + timedelta(days=1)
        period_label = "Неделя"
    else:
        start_date, end_date = today, today + timedelta(days=1)
        period_label = "Сегодня"
    tz = ZoneInfo("Asia/Bishkek")
    start_utc = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(end_date, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    orders = (
        await db.scalars(
            select(Order).where(Order.created_at >= start_utc, Order.created_at < end_utc)
        )
    ).all()
    events = (
        await db.scalars(
            select(OrderEvent).where(
                OrderEvent.created_at >= start_utc,
                OrderEvent.created_at < end_utc,
            )
        )
    ).all()
    transitions = [event.to_status for event in events]
    counts = {
        "Создано": len(orders),
        "Забрано": transitions.count("picked_up"),
        "В цехе": sum(status in {"picked_up", "processing"} for status in transitions),
        "Готово": transitions.count("ready"),
        "Доставлено": transitions.count("delivered"),
        "Отменено": transitions.count("cancelled"),
        "Проблемных": sum(status in {"pickup_failed", "delivery_failed"} for status in transitions),
    }
    delivered_ids = {event.order_id for event in events if event.to_status == "delivered"}
    due_orders = (
        await db.scalars(
            select(Order).where(
                Order.id.in_(delivered_ids or {-1}),
                Order.status == "delivered",
                Order.is_paid.is_(False),
            )
        )
    ).all()
    paid_orders = (
        await db.scalars(
            select(Order).where(
                Order.paid_at.is_not(None),
                Order.paid_at >= start_utc,
                Order.paid_at < end_utc,
                Order.is_paid.is_(True),
            )
        )
    ).all()
    due = sum(order.total_amount for order in due_orders)
    collected = sum(order.total_amount for order in paid_orders)
    text = f"Сводка · {period_label}\n" + "\n".join(f"{label}: {count}" for label, count in counts.items())
    text += f"\nСумма к получению: {due} сом\nСобрано: {collected} сом"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="ds:summary:0"),
                InlineKeyboardButton(text="Вчера", callback_data="ds:summary:1"),
                InlineKeyboardButton(text="Неделя", callback_data="ds:summary:7"),
            ]
        ]
    )
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ds:summary:"))
async def summary_period(callback: CallbackQuery, db):
    try:
        period = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный период.", show_alert=True)
        return
    if period not in {0, 1, 7}:
        await callback.answer("Неизвестный период.", show_alert=True)
        return
    if callback.message:
        await show_daily_summary(callback.message, db, period)
    await callback.answer()


@router.callback_query(F.data.startswith("dispatch:edit:"))
async def begin_edit(callback: CallbackQuery, state: FSMContext, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status in {"delivered", "cancelled"}:
        await callback.answer("Заказ закрыт или не найден.", show_alert=True)
        return
    await state.clear()
    fields = list(EDIT_FIELDS.items())
    if order.status not in DATE_EDITABLE_STATUSES:
        fields = [
            (key, label)
            for key, label in fields
            if key in {"client_name", "client_phone", "comment"}
        ]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"dispatch:field:{order_id}:{field}")]
            for field, label in fields
        ]
    )
    if callback.message:
        await callback.message.answer(f"Что изменить в заказе #{order_id}?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("dispatch:field:"))
async def choose_edit_field(callback: CallbackQuery, state: FSMContext, db):
    try:
        _, _, order_id_raw, field = callback.data.split(":", 3)
        order_id = int(order_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректное поле заказа.", show_alert=True)
        return
    if field not in EDIT_FIELDS:
        await callback.answer("Неизвестное поле заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status in {"delivered", "cancelled"}:
        await callback.answer("Заказ закрыт или не найден.", show_alert=True)
        return
    if field in {"address", "pickup_date"} and order.status not in DATE_EDITABLE_STATUSES:
        await callback.answer("Адрес и дату можно менять только в очереди или после неудачной доставки.", show_alert=True)
        return
    await state.clear()
    if field == "address":
        await state.update_data(address_flow="edit", edit_order_id=order_id)
        await state.set_state(OrderForm.address)
        if callback.message:
            await callback.message.answer(
                "Введите улицу и номер дома для поиска на карте:",
                reply_markup=step_back_markup("address"),
            )
    elif field == "pickup_slot":
        await state.update_data(edit_order_id=order_id, edit_field=field)
        await state.set_state(OrderForm.pickup_slot)
        if callback.message:
            await prompt_pickup_slot(callback.message)
    elif field == "items":
        try:
            items = json.loads(order.items or "[]")
        except json.JSONDecodeError:
            items = []
        await state.update_data(edit_order_id=order_id, edit_field=field, items=items)
        await state.set_state(OrderForm.items)
        if callback.message:
            await prompt_items(callback.message, items)
    else:
        await state.update_data(edit_order_id=order_id, edit_field=field)
        await state.set_state(OrderForm.edit_value)
        prompts = {
            "client_name": "Введите новое имя клиента:",
            "client_phone": "Введите новый телефон клиента:",
            "pickup_date": "Введите дату забора: ДД.ММ.ГГГГ:",
            "comment": "Введите новый комментарий или «-», чтобы очистить:",
        }
        if callback.message:
            await callback.message.answer(
                prompts[field],
                reply_markup=step_back_markup("order_card"),
            )
    await callback.answer()


async def save_order_field(db, order: Order, field: str, value, actor_id: int, actor_name: str, bot: Bot | None = None):
    previous_status = order.status
    allowed = (
        DATE_EDITABLE_STATUSES
        if field in {"address", "pickup_date"}
        else OPEN_STATUSES - {"cancelled", "delivered"}
    )
    values = {field: value}
    if field == "items":
        values[field] = json.dumps(value, ensure_ascii=False)
    result = await db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_(allowed))
        .values(**values)
    )
    if result.rowcount != 1:
        await db.rollback()
        return False
    await record_event(
        db,
        order,
        actor_id=actor_id,
        actor_name=actor_name,
        from_status=previous_status,
        to_status=previous_status,
        note=f"изменено поле {EDIT_FIELDS[field]}",
    )
    await db.commit()
    if bot and field in {"client_name", "client_phone", "comment"}:
        recipients = set()
        if order.pickup_courier_id:
            recipients.add(order.pickup_courier_id)
        if order.delivery_courier_id:
            recipients.add(order.delivery_courier_id)
        for courier_id in recipients:
            try:
                await bot.send_message(courier_id, f"Заказ #{order.id} изменён: {EDIT_FIELDS[field]}.")
            except TelegramAPIError:
                logger.exception("Unable to notify courier %s about order change", courier_id)
    return True


@router.message(OrderForm.edit_value)
async def save_edit(message: Message, state: FSMContext, db, bot: Bot):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Значение не должно быть пустым. Введите его текстом:")
        return
    data = await state.get_data()
    field = data["edit_field"]
    if field == "client_phone":
        value = normalize_phone(value)
        if value is None:
            await message.answer("Введите телефон в формате +996XXXXXXXXX:")
            return
    elif field == "pickup_date":
        value = pickup_date_value(value)
        if value is None:
            await message.answer("Укажите будущую дату в формате ДД.ММ.ГГГГ:")
            return
    elif field == "comment" and value == "-":
        value = None
    if data.get("review_mode"):
        await state.update_data(**{field: value})
        await state.set_state(OrderForm.confirm)
        await order_summary(message, await state.get_data())
        return
    order = await db.get(Order, data["edit_order_id"])
    if order is None:
        await state.clear()
        await message.answer("Заказ не найден.")
        return
    if not await save_order_field(
        db,
        order,
        field,
        value,
        message.from_user.id,
        message.from_user.full_name,
        bot,
    ):
        await state.clear()
        await message.answer("Это поле нельзя изменить при текущем статусе.")
        return
    await db.refresh(order)
    await state.clear()
    await message.answer("Данные обновлены.")
    await dispatcher_order_card(message, db, order)


@router.callback_query(F.data.regexp(r"^ds:cancel:\d+$"))
async def cancel_order(callback: CallbackQuery, state: FSMContext, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status not in DATE_EDITABLE_STATUSES:
        await callback.answer("Отменить можно только новый или проблемный заказ.", show_alert=True)
        return
    await state.clear()
    await state.update_data(cancel_order_id=order_id)
    await state.set_state(OrderForm.cancel_reason)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=reason, callback_data=f"ds:cancel:reason:{index}")]
            for index, reason in enumerate(CANCEL_REASONS)
        ]
        + [[InlineKeyboardButton(text="↩️ Нет", callback_data="ds:cancel:no")]]
    )
    if callback.message:
        await callback.message.answer("Выберите причину отмены:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ds:cancel:reason:"))
async def choose_cancel_reason(callback: CallbackQuery, state: FSMContext, db):
    if await state.get_state() != OrderForm.cancel_reason.state:
        await callback.answer("Причина отмены сейчас не запрашивается.", show_alert=True)
        return
    try:
        index = int(callback.data.rsplit(":", 1)[1])
        reason = CANCEL_REASONS[index]
    except (ValueError, IndexError):
        await callback.answer("Неизвестная причина.", show_alert=True)
        return
    if reason == "Другое":
        await state.set_state(OrderForm.cancel_other)
        if callback.message:
            await callback.message.answer("Введите причину отмены текстом:")
        await callback.answer()
        return
    await state.update_data(cancel_reason=reason)
    await confirm_cancel(callback, state)


@router.message(OrderForm.cancel_other)
async def set_cancel_other(message: Message, state: FSMContext):
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Введите причину отмены текстом:")
        return
    await state.update_data(cancel_reason=reason)
    await state.set_state(OrderForm.cancel_confirm)
    await message.answer("Точно отменить?", reply_markup=cancel_confirm_markup())


def cancel_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="ds:cancel:yes"),
                InlineKeyboardButton(text="↩️ Нет", callback_data="ds:cancel:no"),
            ]
        ]
    )


async def confirm_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.cancel_confirm)
    if callback.message:
        await callback.message.answer("Точно отменить?", reply_markup=cancel_confirm_markup())
    await callback.answer()


@router.callback_query(F.data == "ds:cancel:no")
async def reject_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.answer("Отмена заказа прекращена.")
    await callback.answer()


@router.callback_query(F.data == "ds:cancel:yes")
async def confirm_cancel_order(callback: CallbackQuery, state: FSMContext, db):
    if await state.get_state() != OrderForm.cancel_confirm.state:
        await callback.answer("Подтверждение отмены устарело.", show_alert=True)
        return
    data = await state.get_data()
    order_id = data.get("cancel_order_id")
    cancel_reason = data.get("cancel_reason")
    if not cancel_reason:
        await callback.answer("Причина отмены не выбрана.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await state.clear()
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    old_status = order.status
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(DATE_EDITABLE_STATUSES))
        .values(status="cancelled", cancel_reason=cancel_reason)
    )
    if result.rowcount != 1:
        await db.rollback()
        await state.clear()
        await callback.answer("Заказ больше нельзя отменить.", show_alert=True)
        return
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        from_status=old_status,
        to_status="cancelled",
        reason=cancel_reason,
    )
    await db.commit()
    await state.clear()
    await callback.answer("Заказ отменён.")
    if callback.message:
        await callback.message.edit_text(f"Заказ #{order_id} отменён. Причина: {cancel_reason}")


@router.callback_query(F.data.startswith("ds:call:"))
async def show_client_phone(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(f"Телефон клиента по заказу #{order.id}: {order.client_phone}")
    await callback.answer()


@router.callback_query(F.data.startswith("ds:courier:"))
async def contact_order_courier(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    courier_id = (
        order.delivery_courier_id
        if order.status == "delivery_en_route"
        else order.pickup_courier_id or order.delivery_courier_id
    )
    courier = await db.get(User, courier_id) if courier_id else None
    if courier is None:
        await callback.answer("Курьер не назначен.", show_alert=True)
        return
    if callback.message:
        if courier.username:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть чат с курьером", url=f"https://t.me/{courier.username}")]
                ]
            )
            await callback.message.answer(f"Курьер: {courier.full_name}", reply_markup=keyboard)
        else:
            await callback.message.answer(
                f"Курьер {courier.full_name}, телефон: {courier.phone or 'не указан'}"
            )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ds:unassign:\d+$"))
async def ask_unassign_order(callback: CallbackQuery):
    order_id = callback.data.rsplit(":", 1)[1]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"ds:unassign:yes:{order_id}"),
                InlineKeyboardButton(text="↩️ Нет", callback_data="ds:unassign:no"),
            ]
        ]
    )
    if callback.message:
        await callback.message.answer("Снять заказ с курьера?", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "ds:unassign:no")
async def cancel_unassign(callback: CallbackQuery):
    await callback.answer("Действие отменено.")


@router.callback_query(F.data.startswith("ds:unassign:yes:"))
async def unassign_order(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    old_status = order.status
    if old_status == "pickup_en_route":
        new_status, owner_field = "new", "pickup_courier_id"
    elif old_status == "delivery_en_route":
        new_status, owner_field = "ready", "delivery_courier_id"
    else:
        await callback.answer("Заказ уже не в пути.", show_alert=True)
        return
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == old_status)
        .values(status=new_status, **{owner_field: None})
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Статус заказа уже изменился.", show_alert=True)
        return
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        from_status=old_status,
        to_status=new_status,
        note="Заказ снят с курьера диспетчером",
    )
    await db.commit()
    await callback.answer("Заказ возвращён в очередь.")


@router.callback_query(F.data.regexp(r"^ds:assign:\d+$"))
async def show_courier_choices(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status != "new":
        await callback.answer("Назначить можно только новый заказ.", show_alert=True)
        return
    couriers = (
        await db.scalars(
            select(User).where(
                User.role == "courier",
                User.is_approved.is_(True),
                User.is_on_shift.is_(True),
            )
        )
    ).all()
    rows = []
    for courier in couriers:
        active_count = await db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                or_(
                    (Order.pickup_courier_id == courier.telegram_id)
                    & (Order.status == "pickup_en_route"),
                    (Order.delivery_courier_id == courier.telegram_id)
                    & (Order.status == "delivery_en_route"),
                )
            )
        ) or 0
        if active_count >= COURIER_MAX_ACTIVE:
            continue
        distance = (
            f"{haversine_km(order.latitude, order.longitude, courier.latitude, courier.longitude):.1f} км"
            if None not in (order.latitude, order.longitude, courier.latitude, courier.longitude)
            else "расстояние неизвестно"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{courier.full_name} · активных {active_count} · {distance}",
                    callback_data=f"ds:assign:choose:{order_id}:{courier.telegram_id}",
                )
            ]
        )
    if not rows:
        await callback.answer("Нет курьеров на смене с доступным лимитом.", show_alert=True)
        return
    if callback.message:
        await callback.message.answer("Выберите курьера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("ds:assign:choose:"))
async def assign_courier(callback: CallbackQuery, db, bot: Bot):
    try:
        _, _, _, order_id_raw, courier_id_raw = callback.data.split(":", 4)
        order_id, courier_id = int(order_id_raw), int(courier_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректный выбор курьера.", show_alert=True)
        return
    active_count = (
        select(func.count())
        .select_from(Order)
        .where(
            or_(
                (Order.pickup_courier_id == courier_id) & (Order.status == "pickup_en_route"),
                (Order.delivery_courier_id == courier_id) & (Order.status == "delivery_en_route"),
            )
        )
        .scalar_subquery()
    )
    available_courier = select(User.telegram_id).where(
        User.telegram_id == courier_id,
        User.role == "courier",
        User.is_approved.is_(True),
        User.is_on_shift.is_(True),
    ).exists()
    result = await db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == "new",
            Order.pickup_courier_id.is_(None),
            active_count < COURIER_MAX_ACTIVE,
            available_courier,
        )
        .values(status="pickup_en_route", pickup_courier_id=courier_id)
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Заказ или лимит курьера изменился. Обновите список.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    courier = await db.get(User, courier_id)
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        from_status="new",
        to_status="pickup_en_route",
        note=f"Назначен курьер {courier.full_name}",
    )
    await db.commit()
    try:
        await bot.send_message(
            courier_id,
            f"Вам назначен заказ #{order_id}: {order.address}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть заказ", callback_data=f"courier:open:{order_id}")],
                    [InlineKeyboardButton(text="Отказаться", callback_data=f"courier:decline:{order_id}")],
                ]
            ),
        )
    except TelegramAPIError:
        logger.exception("Unable to notify courier %s about assigned order %s", courier_id, order_id)
        if callback.message:
            await callback.message.answer("Заказ назначен, но курьер не получил уведомление.")
    await callback.answer("Курьер назначен.")


@router.callback_query(F.data.startswith("ds:requeue:"))
async def requeue_failed_order(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    old_status = order.status if order else None
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(["pickup_failed", "delivery_failed"]))
        .values(status="new", pickup_courier_id=None, delivery_courier_id=None)
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Заказ больше нельзя вернуть в очередь.", show_alert=True)
        return
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        from_status=old_status,
        to_status="new",
        note="Возвращён в очередь",
    )
    await db.commit()
    await callback.answer("Заказ возвращён в очередь.")


@router.callback_query(F.data.startswith("ds:reschedule:"))
async def begin_reschedule(callback: CallbackQuery, state: FSMContext, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status not in DATE_EDITABLE_STATUSES:
        await callback.answer("Перенести можно только заказ из очереди или проблемный.", show_alert=True)
        return
    await state.clear()
    await state.update_data(reschedule_order_id=order_id)
    await state.set_state(OrderForm.reschedule_date)
    if callback.message:
        await callback.message.answer("Введите новую дату ДД.ММ.ГГГГ:")
    await callback.answer()


@router.message(OrderForm.reschedule_date)
async def set_rescheduled_date(message: Message, state: FSMContext):
    new_date = pickup_date_value(message.text or "")
    if new_date is None:
        await message.answer("Введите будущую дату в формате ДД.ММ.ГГГГ:")
        return
    await state.update_data(reschedule_date=new_date)
    await state.set_state(OrderForm.reschedule_slot)
    await prompt_pickup_slot(message)


async def save_rescheduled_slot(callback: CallbackQuery, state: FSMContext, db):
    if await state.get_state() != OrderForm.reschedule_slot.state:
        await callback.answer("Сейчас перенос не запрашивается.", show_alert=True)
        return
    try:
        slot = PICKUP_SLOTS[int(callback.data.rsplit(":", 1)[1])]
    except (ValueError, IndexError):
        await callback.answer("Неизвестный интервал.", show_alert=True)
        return
    data = await state.get_data()
    order_id = data["reschedule_order_id"]
    order = await db.get(Order, order_id)
    old_status = order.status if order else None
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(DATE_EDITABLE_STATUSES))
        .values(
            pickup_date=data["reschedule_date"],
            pickup_slot=slot,
            status="new",
            pickup_courier_id=None,
            delivery_courier_id=None,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        await state.clear()
        await callback.answer("Заказ больше нельзя перенести.", show_alert=True)
        return
    await record_event(
        db,
        order,
        actor_id=callback.from_user.id,
        actor_name=callback.from_user.full_name,
        from_status=old_status,
        to_status="new",
        note=f"Перенесён на {format_pickup_date(data['reschedule_date'])} {slot}",
    )
    await db.commit()
    await state.clear()
    await callback.answer("Дата и интервал обновлены.")


@router.callback_query(F.data.startswith("ds:repeat:"))
async def repeat_order(callback: CallbackQuery, state: FSMContext, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status != "delivered":
        await callback.answer("Повторить можно только доставленный заказ.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        address_flow="create",
        client_phone=order.client_phone,
        client_name=order.client_name,
        address=order.address,
        address_base=address_base_from_full_address(order.address),
        latitude=order.latitude,
        longitude=order.longitude,
        items=json.loads(order.items or "[]"),
        comment=order.comment,
    )
    await state.set_state(OrderForm.pickup_day)
    if callback.message:
        await prompt_pickup_date(callback.message)
    await callback.answer("Заполните дату нового заказа.")
