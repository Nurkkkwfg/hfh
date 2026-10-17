import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from database.models import Order, User
from handlers.common import ROLE_COURIER, ROLE_DISPATCHER, status_label

logger = logging.getLogger(__name__)


async def notify_couriers_today(bot: Bot, db, order: Order) -> None:
    couriers = (
        await db.scalars(
            select(User).where(
                User.role == ROLE_COURIER,
                User.is_approved.is_(True),
                User.is_on_shift.is_(True),
            )
        )
    ).all()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть заказ", callback_data=f"courier:open:{order.id}")]
        ]
    )
    for courier in couriers:
        try:
            await bot.send_message(
                courier.telegram_id,
                f"Новый заказ #{order.id} на сегодня: {order.address}, {order.pickup_slot}.",
                reply_markup=markup,
            )
        except TelegramAPIError:
            logger.exception("Unable to notify courier %s about order %s", courier.telegram_id, order.id)


async def notify_dispatchers(
    bot: Bot,
    db,
    text: str,
    *,
    exclude_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    dispatchers = (
        await db.scalars(
            select(User).where(
                User.role == ROLE_DISPATCHER,
                User.is_approved.is_(True),
            )
        )
    ).all()
    for dispatcher in dispatchers:
        if dispatcher.telegram_id == exclude_id:
            continue
        try:
            await bot.send_message(dispatcher.telegram_id, text, reply_markup=reply_markup)
        except TelegramAPIError:
            logger.exception("Unable to notify dispatcher %s", dispatcher.telegram_id)


async def notify_order_status(
    bot: Bot,
    db,
    order: Order,
    *,
    status: str,
    unpaid: bool = False,
) -> None:
    text = f"Заказ #{order.id}: {status_label(status)}."
    if unpaid:
        text += " Клиент не оплатил заказ."
    await notify_dispatchers(bot, db, text)
