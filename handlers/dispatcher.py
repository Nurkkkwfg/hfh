from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import Settings
from database.models import Order
from handlers.common import ROLE_DISPATCHER, RoleFilter, send_order_card
from utils.geocoding import geocode_bishkek_address

router = Router(name="dispatcher")
router.message.filter(RoleFilter(ROLE_DISPATCHER))
router.callback_query.filter(RoleFilter(ROLE_DISPATCHER))


class OrderForm(StatesGroup):
    client_name = State()
    address = State()
    pickup_day = State()
    edit_value = State()


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
    await message.answer("Когда забрать? Например: сегодня, завтра или 25.10.2026:")


@router.message(OrderForm.pickup_day)
async def set_pickup_day(message: Message, state: FSMContext, db, settings: Settings):
    pickup_day = (message.text or "").strip()
    if not pickup_day:
        await message.answer("Укажите день забора:")
        return
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
    await message.answer(f"Заказ #{order.id} создан.")
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
async def begin_edit(callback: CallbackQuery, state: FSMContext):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    await state.set_state(OrderForm.edit_value)
    await state.update_data(edit_order_id=order_id)
    if callback.message:
        await callback.message.answer(f"Введите новые данные для заказа #{order_id} в формате:\nимя клиента | адрес | день забора")
    await callback.answer()


@router.message(OrderForm.edit_value)
async def save_edit(message: Message, state: FSMContext, db, settings: Settings):
    values = [part.strip() for part in (message.text or "").split("|", 2)]
    if len(values) != 3 or not all(values):
        await message.answer("Нужны три значения через |: имя клиента | адрес | день забора")
        return
    data = await state.get_data()
    order = await db.get(Order, data["edit_order_id"])
    if order is None or order.status in {"delivered", "cancelled"}:
        await state.clear()
        await message.answer("Заказ не найден или уже закрыт.")
        return
    order.client_name, order.address, order.pickup_day = values
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