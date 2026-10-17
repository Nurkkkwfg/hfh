import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update

from config import Settings
from database.models import Order, User
from handlers.common import (
    ROLE_ADMIN,
    ROLE_COURIER,
    ROLE_DISPATCHER,
    ROLE_LAUNDRY,
    callback_is_admin,
    role_keyboard,
    status_label,
)
from utils.dates import format_bishkek_datetime

router = Router(name="admin")
logger = logging.getLogger(__name__)
ROLE_LABELS = {
    ROLE_ADMIN: "Super Admin",
    ROLE_DISPATCHER: "Диспетчер",
    ROLE_COURIER: "Курьер",
    ROLE_LAUNDRY: "Прачечная / цех",
}
ASSIGNABLE_ROLES = {
    ROLE_DISPATCHER: ROLE_LABELS[ROLE_DISPATCHER],
    ROLE_COURIER: ROLE_LABELS[ROLE_COURIER],
    ROLE_LAUNDRY: ROLE_LABELS[ROLE_LAUNDRY],
}
STAFF_LIST_LIMIT = 30
ORDER_LIST_LIMIT = 20


def staff_action_keyboard(user: User, admin_ids: frozenset[int]) -> InlineKeyboardMarkup | None:
    if user.telegram_id in admin_ids:
        return None
    if not user.is_approved:
        rows = [
            [InlineKeyboardButton(text=label, callback_data=f"staff:role:{user.telegram_id}:{role}")]
            for role, label in ASSIGNABLE_ROLES.items()
        ]
    else:
        rows = [[InlineKeyboardButton(text="✏️ Изменить роль", callback_data=f"staff:manage:{user.telegram_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def staff_role_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"staff:change:{user_id}:{role}")]
        for role, label in ASSIGNABLE_ROLES.items()
    ]
    rows.append([InlineKeyboardButton(text="⛔ Отключить сотрудника", callback_data=f"staff:disable:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message, db, bot: Bot, settings: Settings, user_role: str | None):
    person = message.from_user
    if person.id in settings.admin_ids:
        user = await db.get(User, person.id)
        if user is None:
            user = User(telegram_id=person.id, full_name=person.full_name, role=ROLE_ADMIN, is_approved=True)
            db.add(user)
        else:
            user.role, user.is_approved = ROLE_ADMIN, True
        await db.commit()
        await message.answer("Панель администратора.", reply_markup=role_keyboard(ROLE_ADMIN))
        return

    user = await db.get(User, person.id)
    is_new_user = user is None
    if user is None:
        user = User(
            telegram_id=person.id,
            username=person.username,
            full_name=person.full_name,
            is_approved=False,
        )
        db.add(user)
        await db.commit()

    if not user.is_approved and not is_new_user:
        await message.answer("Запрос уже отправлен, ожидайте.")
        return

    if is_new_user:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"staff:role:{person.id}:{role}")]
                for role, label in ASSIGNABLE_ROLES.items()
            ]
        )
        notified = False
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"Запрос доступа от {person.full_name} (@{person.username or 'без username'}), ID {person.id}:",
                    reply_markup=keyboard,
                )
            except TelegramAPIError:
                logger.exception("Unable to deliver access request for Telegram user %s to admin %s", person.id, admin_id)
            else:
                notified = True
        if not notified:
            await message.answer("Не удалось отправить запрос администраторам. Попробуйте позже.")
            return

    if user_role:
        await message.answer("Вы уже зарегистрированы.", reply_markup=role_keyboard(user_role))
    else:
        await message.answer("Запрос доступа отправлен администраторам. Дождитесь назначения роли.")


@router.message(Command("admin"))
async def admin_panel(message: Message, settings: Settings):
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Команда доступна только администраторам.")
        return
    await message.answer("Панель администратора.", reply_markup=role_keyboard(ROLE_ADMIN))


@router.message(Command("staff"))
@router.message(F.text == "👥 Сотрудники")
async def list_staff(message: Message, db, settings: Settings):
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Команда доступна только администраторам.")
        return
    users = (
        await db.scalars(select(User).order_by(User.created_at.desc()).limit(STAFF_LIST_LIMIT))
    ).all()
    if not users:
        await message.answer("Сотрудников пока нет.")
        return
    await message.answer(f"Сотрудники (последние {len(users)}):")
    for user in users:
        status = ROLE_LABELS.get(user.role, "Ожидает одобрения")
        if not user.is_approved:
            status = "Ожидает одобрения"
        await message.answer(
            f"{user.full_name} | ID {user.telegram_id} | {status} | "
            f"Создан: {format_bishkek_datetime(user.created_at)}",
            reply_markup=staff_action_keyboard(user, settings.admin_ids),
        )


@router.message(F.text == "⏳ Заявки")
async def pending_staff(message: Message, db, settings: Settings):
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Команда доступна только администраторам.")
        return
    users = (
        await db.scalars(
            select(User)
            .where(User.is_approved.is_(False))
            .order_by(User.created_at.asc())
            .limit(STAFF_LIST_LIMIT)
        )
    ).all()
    if not users:
        await message.answer("Новых заявок нет.")
        return
    await message.answer(f"Ожидают одобрения (до {STAFF_LIST_LIMIT} заявок):")
    for user in users:
        await message.answer(
            f"{user.full_name} (@{user.username or 'без username'}), ID {user.telegram_id}",
            reply_markup=staff_action_keyboard(user, settings.admin_ids),
        )


@router.message(F.text == "📊 Статистика")
async def admin_statistics(message: Message, db, settings: Settings):
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Команда доступна только администраторам.")
        return
    approved_count = await db.scalar(select(func.count()).select_from(User).where(User.is_approved.is_(True)))
    pending_count = await db.scalar(select(func.count()).select_from(User).where(User.is_approved.is_(False)))
    order_count = await db.scalar(select(func.count()).select_from(Order))
    status_counts = (
        await db.execute(select(Order.status, func.count()).group_by(Order.status).order_by(Order.status))
    ).all()
    lines = [
        "Статистика",
        f"Сотрудников одобрено: {approved_count or 0}",
        f"Заявок на доступ: {pending_count or 0}",
        f"Всего заказов: {order_count or 0}",
    ]
    lines.extend(f"{status_label(status)}: {count}" for status, count in status_counts)
    await message.answer("\n".join(lines))


@router.message(F.text == "📦 Заказы")
async def admin_orders(message: Message, db, settings: Settings):
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Команда доступна только администраторам.")
        return
    orders = (
        await db.scalars(select(Order).order_by(Order.id.desc()).limit(ORDER_LIST_LIMIT))
    ).all()
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    await message.answer(f"Последние {len(orders)} заказов:")
    for order in orders:
        text = (
            f"Заказ #{order.id}\nКлиент: {order.client_name}\nАдрес: {order.address}\n"
            f"Забор: {order.pickup_day}\nСтатус: {status_label(order.status)}\n"
            f"Создан: {format_bishkek_datetime(order.created_at)}"
        )
        keyboard = None
        if order.status not in {"delivered", "cancelled"}:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"admin:order:cancel:{order.id}")]
                ]
            )
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("staff:manage:"))
async def manage_staff(callback: CallbackQuery, db, settings: Settings):
    if not await callback_is_admin(callback, settings.admin_ids):
        return
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    if user_id in settings.admin_ids:
        await callback.answer("Нельзя изменять настроенного администратора.", show_alert=True)
        return
    user = await db.get(User, user_id)
    if user is None or not user.is_approved:
        await callback.answer("Сотрудник не найден или доступ уже отключён.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=staff_role_keyboard(user_id))
    await callback.answer()


@router.callback_query(F.data.startswith("staff:change:"))
async def change_staff_role(callback: CallbackQuery, db, bot: Bot, settings: Settings):
    if not await callback_is_admin(callback, settings.admin_ids):
        return
    try:
        _, _, user_id_raw, role = callback.data.split(":", 3)
        user_id = int(user_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    if role not in ASSIGNABLE_ROLES:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return
    if user_id in settings.admin_ids:
        await callback.answer("Нельзя изменять настроенного администратора.", show_alert=True)
        return
    result = await db.execute(
        update(User)
        .where(User.telegram_id == user_id, User.is_approved.is_(True))
        .values(role=role)
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Сотрудник не найден или доступ уже отключён.", show_alert=True)
        return
    user = await db.get(User, user_id)
    if user is not None:
        user.role = role
    await db.commit()
    await callback.answer("Роль изменена.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, f"Ваша роль изменена: {ROLE_LABELS[role]}.", reply_markup=role_keyboard(role))
    except TelegramAPIError:
        if callback.message:
            await callback.message.answer("Роль изменена, но уведомить сотрудника не удалось.")


@router.callback_query(F.data.startswith("staff:disable:"))
async def disable_staff(callback: CallbackQuery, db, bot: Bot, settings: Settings):
    if not await callback_is_admin(callback, settings.admin_ids):
        return
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    if user_id in settings.admin_ids:
        await callback.answer("Нельзя отключить настроенного администратора.", show_alert=True)
        return
    await db.execute(
        update(Order)
        .where(Order.status == "pickup_en_route", Order.pickup_courier_id == user_id)
        .values(status="new", pickup_courier_id=None)
    )
    await db.execute(
        update(Order)
        .where(Order.status == "delivery_en_route", Order.delivery_courier_id == user_id)
        .values(status="ready", delivery_courier_id=None)
    )
    result = await db.execute(
        update(User)
        .where(User.telegram_id == user_id, User.is_approved.is_(True))
        .values(role=None, is_approved=False, is_on_shift=False, latitude=None, longitude=None)
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Сотрудник не найден или уже отключён.", show_alert=True)
        return
    user = await db.get(User, user_id)
    if user is not None:
        user.role = None
        user.is_approved = False
        user.is_on_shift = False
        user.latitude = None
        user.longitude = None
    await db.commit()
    await callback.answer("Доступ сотрудника отключён.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, "Ваш доступ к рабочим функциям бота отключён.")
    except TelegramAPIError:
        if callback.message:
            await callback.message.answer("Доступ отключён, но уведомить сотрудника не удалось.")


@router.callback_query(F.data.startswith("admin:order:cancel:"))
async def admin_cancel_order(callback: CallbackQuery, db, settings: Settings):
    if not await callback_is_admin(callback, settings.admin_ids):
        return
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный номер заказа.", show_alert=True)
        return
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.not_in(["delivered", "cancelled"]))
        .values(status="cancelled")
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Заказ не найден или уже закрыт.", show_alert=True)
        return
    await db.commit()
    await callback.answer("Заказ отменён.")
    if callback.message:
        await callback.message.edit_text(f"Заказ #{order_id} отменён.")


@router.callback_query(F.data.startswith("staff:role:"))
async def assign_role(callback: CallbackQuery, db, bot: Bot, settings: Settings):
    if not await callback_is_admin(callback, settings.admin_ids):
        return
    try:
        _, _, user_id_raw, role = callback.data.split(":", 3)
        user_id = int(user_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    if role not in ASSIGNABLE_ROLES:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    user = await db.get(User, user_id)
    if user is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    if user.is_approved:
        await callback.answer("Пользователь уже одобрен.", show_alert=True)
        return
    result = await db.execute(
        update(User)
        .where(User.telegram_id == user_id, User.is_approved.is_(False))
        .values(role=role, is_approved=True)
    )
    if result.rowcount != 1:
        await db.rollback()
        await callback.answer("Пользователь уже одобрен.", show_alert=True)
        return
    user.role = role
    user.is_approved = True
    await db.commit()
    await callback.answer("Роль назначена.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, f"Вам назначена роль: {ROLE_LABELS[role]}.", reply_markup=role_keyboard(role))
    except TelegramAPIError:
        if callback.message:
            await callback.message.answer("Роль назначена, но уведомить сотрудника не удалось.")