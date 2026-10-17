from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.models import Order
from handlers.common import ROLE_LAUNDRY, RoleFilter, role_keyboard, status_label

router = Router(name="laundry")
router.message.filter(RoleFilter(ROLE_LAUNDRY))
router.callback_query.filter(RoleFilter(ROLE_LAUNDRY))


class LaundryForm(StatesGroup):
    order_id = State()
    measurements = State()


@router.message(Command("help"))
async def laundry_help(message: Message):
    await message.answer(
        "Цех: нажмите «Найти заказ» или отправьте /order 15. В карточке добавьте замеры "
        "и отметьте заказ готовым к доставке. Отправьте /cancel, чтобы прервать ввод номера или замеров."
    )


@router.message(Command("cancel"))
async def cancel_laundry_flow(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Сейчас нет незавершённого действия.")
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=role_keyboard(ROLE_LAUNDRY))


@router.message(Command("order"))
async def order_command(message: Message, state: FSMContext, db):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        await show_order(message, db, parts[1].lstrip("#"))
        return
    await state.set_state(LaundryForm.order_id)
    await message.answer("Введите номер заказа, например 15 или #15:")


@router.message(F.text == "🔎 Найти заказ")
async def ask_order_id(message: Message, state: FSMContext):
    await state.set_state(LaundryForm.order_id)
    await message.answer("Введите номер заказа, например 15 или #15:")


@router.message(LaundryForm.order_id)
async def find_order(message: Message, state: FSMContext, db):
    await state.clear()
    await show_order(message, db, (message.text or "").strip().lstrip("#"))


async def show_order(message: Message, db, raw_order_id: str):
    try:
        order_id = int(raw_order_id)
    except ValueError:
        await message.answer("Номер заказа должен быть числом.")
        return
    order = await db.get(Order, order_id)
    if order is None:
        await message.answer(f"Заказ #{order_id} не найден.")
        return
    if order.status not in {"picked_up", "processing", "ready", "delivered", "cancelled"}:
        await message.answer(f"Заказ #{order.id} пока не передан в цех (статус: {order.status}).")
        return
    buttons = []
    if order.status in {"picked_up", "processing"}:
        buttons = [
            [InlineKeyboardButton(text="📐 Добавить замеры", callback_data=f"laundry:measure:{order.id}")],
            [InlineKeyboardButton(text="🧼 Чисто / готово к доставке", callback_data=f"laundry:ready:{order.id}")],
        ]
    text = (
        f"Заказ #{order.id}\nКлиент: {order.client_name}\nАдрес: {order.address}\n"
        f"Статус: {status_label(order.status)}\nЗамеры: {order.measurements or 'не добавлены'}"
    )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)


@router.callback_query(F.data.startswith("laundry:measure:"))
async def begin_measurement(callback: CallbackQuery, state: FSMContext):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    await state.set_state(LaundryForm.measurements)
    await state.update_data(order_id=order_id)
    if callback.message:
        await callback.message.answer("Введите замеры, например: Ковёр 3x4 м; шторы 4 шт. Можно добавить несколько изделий.")
    await callback.answer()


@router.message(LaundryForm.measurements)
async def save_measurement(message: Message, state: FSMContext, db):
    measurements = (message.text or "").strip()
    if not measurements:
        await message.answer("Введите замеры текстом:")
        return
    data = await state.get_data()
    order = await db.get(Order, data["order_id"])
    if order is None or order.status not in {"picked_up", "processing"}:
        await state.clear()
        await message.answer("Заказ уже недоступен для изменения.")
        return
    order.measurements = measurements
    order.status = "processing"
    await db.commit()
    await state.clear()
    await message.answer(f"Замеры сохранены для заказа #{order.id}.")
    await show_order(message, db, str(order.id))


@router.callback_query(F.data.startswith("laundry:ready:"))
async def mark_ready(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status not in {"picked_up", "processing"}:
        await callback.answer("Заказ не найден или его статус уже изменился.", show_alert=True)
        return
    order.status = "ready"
    await db.commit()
    await callback.answer("Заказ добавлен в очередь доставки.")
    if callback.message:
        await callback.message.edit_text(f"Заказ #{order.id}: готов к доставке.")