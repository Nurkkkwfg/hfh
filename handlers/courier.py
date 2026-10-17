from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import func, select, update

from database.models import Order, OrderEvent, User
from handlers.common import ROLE_COURIER, RoleFilter, role_keyboard, status_label
from utils.dates import bishkek_today, format_pickup_date, parse_pickup_day
from utils.geo import haversine_km, sort_orders_by_distance
from utils.notify import notify_dispatchers, notify_order_status

router = Router(name="courier")
router.message.filter(RoleFilter(ROLE_COURIER))
router.callback_query.filter(RoleFilter(ROLE_COURIER))


class CourierProblem(StatesGroup):
    reason = State()


@router.message(Command("shift"))
@router.message(F.text == "🟢 Начать смену")
async def start_shift(message: Message, db):
    user = await db.get(User, message.from_user.id)
    if user is None:
        await message.answer("Учётная запись не найдена. Обратитесь к администратору.")
        return
    user.is_on_shift = True
    user.latitude = None
    user.longitude = None
    await db.commit()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геопозицию", request_location=True)],
            [KeyboardButton(text="🔴 Завершить смену")],
        ],
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
    if user is None:
        await message.answer("Учётная запись не найдена. Обратитесь к администратору.")
        return
    active_count = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            (Order.status == "pickup_en_route") & (Order.pickup_courier_id == user.telegram_id)
            | (Order.status == "delivery_en_route") & (Order.delivery_courier_id == user.telegram_id)
        )
    )
    user.is_on_shift = False
    user.latitude = None
    user.longitude = None
    await db.commit()
    text = "Смена завершена."
    if active_count:
        text += f" Внимание: за вами закреплено активных заказов: {active_count}. Завершите их при следующем выходе на смену."
    await message.answer(text, reply_markup=role_keyboard(ROLE_COURIER))


async def save_location(message: Message, db, *, notify: bool = True):
    if not message.location:
        return
    user = await db.get(User, message.from_user.id)
    if user is None or not user.is_on_shift:
        if not notify:
            return
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
    if not user or not user.is_on_shift:
        await message.answer("Сначала начните смену.")
        return
    if user.latitude is None or user.longitude is None:
        await message.answer("Отправьте текущую геопозицию после начала смены.")
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
    current_date = bishkek_today()
    available = []
    for order in orders:
        if order.status == "new":
            pickup_date = getattr(order, "pickup_date", None)
            if pickup_date is None:
                pickup_date = parse_pickup_day(getattr(order, "pickup_day", ""))
            if pickup_date is None or pickup_date > current_date:
                continue
        elif order.status == "pickup_en_route" and order.pickup_courier_id != courier.telegram_id:
            continue
        elif order.status == "delivery_en_route" and order.delivery_courier_id != courier.telegram_id:
            continue
        elif order.status not in {"pickup_en_route", "ready", "delivery_en_route"}:
            continue
        available.append(order)
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
            f"Забор: {format_pickup_date(pickup_date if order.status == 'new' else getattr(order, 'pickup_date', bishkek_today()))} "
            f"{getattr(order, 'pickup_slot', 'Любое')}\n"
            f"Статус: {status_label(order.status)}\nДо адреса: {distance}"
        )
        buttons = []
        if order.latitude is not None and order.longitude is not None:
            buttons.append(
                [InlineKeyboardButton(
                    text="🗺 Открыть в 2GIS",
                    url=f"https://2gis.kg/bishkek/geo/{order.longitude},{order.latitude}",
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
        if (
            order.status == "pickup_en_route" and order.pickup_courier_id == courier.telegram_id
        ) or (
            order.status == "delivery_en_route" and order.delivery_courier_id == courier.telegram_id
        ):
            buttons.append([InlineKeyboardButton(text="⚠️ Проблема", callback_data=f"courier:problem:{order.id}")])
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.regexp(r"^courier:(pickup|picked|delivery|delivered):\d+$"))
async def update_order_status(callback: CallbackQuery, db):
    try:
        action, order_id_raw = callback.data.rsplit(":", 1)
        order_id = int(order_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректная задача.", show_alert=True)
        return
    courier_id = callback.from_user.id
    courier = await db.get(User, courier_id)
    if courier is None or not courier.is_on_shift:
        await callback.answer("Смена не активна. Начните смену перед обновлением заказа.", show_alert=True)
        return

    transitions = {
        "courier:pickup": ("new", "pickup_en_route", "pickup_courier_id"),
        "courier:picked": ("pickup_en_route", "picked_up", "pickup_courier_id"),
        "courier:delivery": ("ready", "delivery_en_route", "delivery_courier_id"),
        "courier:delivered": ("delivery_en_route", "delivered", "delivery_courier_id"),
    }
    transition = transitions.get(action)
    if transition is None:
        await callback.answer("Статус уже изменён или задача недоступна.", show_alert=True)
        return
    owner_field = transition[2]
    owner_column = getattr(Order, owner_field)
    owner_condition = (
        owner_column.is_(None)
        if action in {"courier:pickup", "courier:delivery"}
        else owner_column == courier_id
    )
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == transition[0], owner_condition)
        .values(status=transition[1], **{owner_field: courier_id})
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Задача уже изменена или закреплена за другим курьером.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is not None:
        order.status = transition[1]
        setattr(order, owner_field, courier_id)
        db.add(
            OrderEvent(
                order_id=order.id,
                actor_id=courier_id,
                actor_name=courier.full_name,
                from_status=transition[0],
                to_status=transition[1],
            )
        )
    await db.commit()
    if transition[1] == "delivered" and order is not None:
        await notify_order_status(
            callback.bot,
            db,
            order,
            status="delivered",
            unpaid=not order.is_paid,
        )
    await callback.answer("Статус заказа обновлён.")
    if callback.message:
        await callback.message.edit_text(f"Заказ #{order_id}: {status_label(transition[1])}")


@router.callback_query(F.data.startswith("courier:open:"))
async def open_notified_order(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None or order.status not in {"new", "pickup_en_route", "ready", "delivery_en_route"}:
        await callback.answer("Заказ больше недоступен.", show_alert=True)
        return
    if order.status == "pickup_en_route" and order.pickup_courier_id != callback.from_user.id:
        await callback.answer("Заказ закреплён за другим курьером.", show_alert=True)
        return
    if order.status == "delivery_en_route" and order.delivery_courier_id != callback.from_user.id:
        await callback.answer("Заказ закреплён за другим курьером.", show_alert=True)
        return
    courier = await db.get(User, callback.from_user.id)
    if courier is None:
        await callback.answer("Профиль курьера не найден.", show_alert=True)
        return
    if callback.message:
        if courier.is_on_shift and courier.latitude is not None and courier.longitude is not None:
            await show_tasks(callback.message, db, courier)
        else:
            action = {
                "new": ("🛻 Выехал за заказом", f"courier:pickup:{order.id}"),
                "pickup_en_route": ("Открыть заказ", f"courier:picked:{order.id}"),
                "ready": ("🚚 Выехал на доставку", f"courier:delivery:{order.id}"),
                "delivery_en_route": ("✅ Доставлено", f"courier:delivered:{order.id}"),
            }.get(order.status)
            markup = (
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=action[0], callback_data=action[1])]
                    ]
                )
                if action
                else None
            )
            await callback.message.answer(
                f"Заказ #{order.id}\nКлиент: {order.client_name}\nАдрес: {order.address}\n"
                f"Забор: {format_pickup_date(order.pickup_date)} {order.pickup_slot}",
                reply_markup=markup,
            )
    await callback.answer()


@router.callback_query(F.data.startswith("courier:decline:"))
async def decline_assigned_order(callback: CallbackQuery, db):
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный заказ.", show_alert=True)
        return
    order = await db.get(Order, order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    result = await db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == "pickup_en_route",
            Order.pickup_courier_id == callback.from_user.id,
        )
        .values(status="new", pickup_courier_id=None)
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Заказ уже изменён или принадлежит другому курьеру.", show_alert=True)
        return
    courier = await db.get(User, callback.from_user.id)
    db.add(
        OrderEvent(
            order_id=order_id,
            actor_id=callback.from_user.id,
            actor_name=courier.full_name if courier else "Курьер",
            from_status="pickup_en_route",
            to_status="new",
            note="Курьер отказался от заказа",
        )
    )
    await db.commit()
    await notify_dispatchers(callback.bot, db, f"Курьер отказался от заказа #{order_id}.")
    await callback.answer("Заказ возвращён в очередь.")


@router.callback_query(F.data.startswith("courier:problem:"))
async def ask_problem_reason(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(CourierProblem.reason)
    await state.update_data(problem_order_id=order_id)
    if callback.message:
        await callback.message.answer("Опишите проблему с заказом:")
    await callback.answer()


@router.message(CourierProblem.reason)
async def save_problem_reason(message: Message, state: FSMContext, db):
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Укажите причину текстом:")
        return
    data = await state.get_data()
    order_id = data.get("problem_order_id")
    order = await db.get(Order, order_id) if order_id else None
    courier = await db.get(User, message.from_user.id)
    if order is None or courier is None:
        await state.clear()
        await message.answer("Заказ или профиль курьера не найден.")
        return
    if order.pickup_courier_id != courier.telegram_id and order.delivery_courier_id != courier.telegram_id:
        await state.clear()
        await message.answer("Этот заказ вам не назначен.")
        return
    old_status = order.status
    failed_status = "pickup_failed" if old_status == "pickup_en_route" else "delivery_failed"
    owner_column = Order.pickup_courier_id if old_status == "pickup_en_route" else Order.delivery_courier_id
    result = await db.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status == old_status,
            owner_column == courier.telegram_id,
        )
        .values(status=failed_status)
    )
    if result.rowcount != 1:
        await db.rollback()
        await state.clear()
        await message.answer("Статус заказа уже изменился. Обновите задачи.")
        return
    db.add(
        OrderEvent(
            order_id=order.id,
            actor_id=courier.telegram_id,
            actor_name=courier.full_name,
            from_status=old_status,
            to_status=failed_status,
            reason=reason,
        )
    )
    await db.commit()
    await state.clear()
    await notify_dispatchers(
        message.bot,
        db,
        f"Проблема по заказу #{order.id}: {reason}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть", callback_data=f"ds:card:{order.id}")]
            ]
        ),
    )
    await message.answer("Проблема передана диспетчеру.")