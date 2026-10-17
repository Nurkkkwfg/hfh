from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Settings
from database.models import User
from handlers.common import (
    ROLE_ADMIN,
    ROLE_COURIER,
    ROLE_DISPATCHER,
    ROLE_LAUNDRY,
    callback_is_admin,
    role_keyboard,
)

router = Router(name="admin")
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
    if user is None:
        user = User(
            telegram_id=person.id,
            username=person.username,
            full_name=person.full_name,
            is_approved=False,
        )
        db.add(user)
        await db.commit()

    if not user.is_approved:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=f"staff:role:{person.id}:{role}")]
                for role, label in ASSIGNABLE_ROLES.items()
            ]
        )
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"Запрос доступа от {person.full_name} (@{person.username or 'без username'}), ID {person.id}:",
                    reply_markup=keyboard,
                )
            except Exception:
                continue

    if user_role:
        await message.answer("Вы уже зарегистрированы.", reply_markup=role_keyboard(user_role))
    else:
        await message.answer("Запрос доступа отправлен администраторам. Дождитесь назначения роли.")


@router.message(Command("staff"))
@router.message(F.text == "👥 Сотрудники")
async def list_staff(message: Message, db, settings: Settings):
    if message.from_user.id not in settings.admin_ids:
        await message.answer("Команда доступна только администраторам.")
        return
    users = (await db.scalars(select(User).order_by(User.created_at.desc()))).all()
    if not users:
        await message.answer("Сотрудников пока нет.")
        return
    lines = [
        f"{user.full_name} | ID {user.telegram_id} | {ROLE_LABELS.get(user.role, 'Ожидает одобрения')}"
        for user in users
    ]
    await message.answer("Сотрудники:\n" + "\n".join(lines))


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
    user.role, user.is_approved = role, True
    await db.commit()
    await callback.answer("Роль назначена.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, f"Вам назначена роль: {ROLE_LABELS[role]}.", reply_markup=role_keyboard(role))
    except Exception:
        pass