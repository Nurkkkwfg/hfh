import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from database.models import Order, OrderEvent, User
from database.session import create_database, initialize_database
from handlers.dispatcher import (
    CANCEL_REASONS,
    COURIER_MAX_ACTIVE,
    OrderForm,
    assign_courier,
    begin_reschedule,
    choose_edit_field,
    choose_cancel_reason,
    choose_pickup_slot,
    confirm_cancel_order,
    create_order,
    find_orders,
    list_markup,
    mark_order_paid,
    new_order,
    normalize_phone,
    requeue_failed_order,
    save_order_field,
    set_pickup_date,
    set_client_phone,
    set_rescheduled_date,
    show_daily_summary,
    send_order_list,
    send_today_list,
    skip_order_comment,
    toggle_order_item,
    replace_with_order_list,
)
from utils.dates import bishkek_today


class MemoryFSM:
    def __init__(self, current_state=None, data=None):
        self.current_state = current_state
        self.data = data or {}

    async def get_state(self):
        return self.current_state

    async def set_state(self, state):
        self.current_state = state.state

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.current_state = None
        self.data.clear()


class DispatcherFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.session_factory = create_database("sqlite+aiosqlite:///:memory:")
        await initialize_database(self.engine)
        self.db = self.session_factory()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def make_order(self, **values):
        values.setdefault("client_name", "Клиент")
        values.setdefault("address", "Ахунбаева 12")
        order = Order(**values)
        self.db.add(order)
        await self.db.commit()
        return order

    def callback(self, data, *, user_id=1, message=True):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id, full_name="Тестовый диспетчер"),
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()) if message else None,
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("+996 (555) 123-456"), "+996555123456")
        self.assertEqual(normalize_phone("0555123456"), "+996555123456")
        self.assertIsNone(normalize_phone("55512abc"))
        self.assertIsNone(normalize_phone("+99655512"))

    async def test_phone_finds_previous_client_and_address(self):
        await self.make_order(client_phone="+996555123456", address="Ахунбаева 12, кв. 4")
        state = MemoryFSM(OrderForm.phone.state)
        message = SimpleNamespace(
            text="0555123456",
            answer=AsyncMock(),
        )

        await set_client_phone(message, state, self.db)

        self.assertEqual(state.current_state, OrderForm.client_match.state)
        self.assertEqual(state.data["previous_pickup_base"], "Ахунбаева 12")
        self.assertIn("Найден клиент", message.answer.await_args.args[0])
        buttons = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "ds:client:reuse")

    async def test_create_wizard_starts_at_phone_and_back_preserves_previous_values(self):
        from handlers.dispatcher import go_back

        state = MemoryFSM()
        message = SimpleNamespace(answer=AsyncMock())
        await new_order(message, state)
        self.assertEqual(state.current_state, OrderForm.phone.state)

        await state.update_data(client_phone="+996555123456", client_name="Айгуль")
        await state.set_state(OrderForm.client_name)
        callback = self.callback("ds:back:phone")
        await go_back(callback, state, self.db)
        self.assertEqual(state.current_state, OrderForm.phone.state)
        self.assertEqual(state.data["client_phone"], "+996555123456")

    async def test_create_wizard_reaches_final_confirmation_after_slot_items_and_comment(self):
        state = MemoryFSM(
            OrderForm.pickup_day.state,
            {
                "client_phone": "+996555123456",
                "client_name": "Айгуль",
                "address": "Ахунбаева 12",
            },
        )
        date_message = SimpleNamespace(text="Завтра", answer=AsyncMock())
        await set_pickup_date(date_message, state)
        slot_callback = self.callback("ds:slot:1")
        await choose_pickup_slot(slot_callback, state, self.db)
        self.assertEqual(state.current_state, OrderForm.items.state)

        item_callback = self.callback("ds:item:0")
        item_callback.message.edit_reply_markup = AsyncMock()
        await toggle_order_item(item_callback, state, self.db)
        done_callback = self.callback("ds:item:done")
        await toggle_order_item(done_callback, state, self.db)
        self.assertEqual(state.current_state, OrderForm.comment.state)

        skip = self.callback("ds:comment:skip")
        await skip_order_comment(skip, state)
        self.assertEqual(state.current_state, OrderForm.confirm.state)
        self.assertEqual(state.data["items"], ["Ковёр"])
        self.assertIsNone(state.data["comment"])
        self.assertIn("✅ Создать", [
            button.text
            for row in skip.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ])

    async def test_create_confirmation_persists_geocoded_order_and_event(self):
        from handlers.dispatcher import create_order

        state = MemoryFSM(
            OrderForm.confirm.state,
            {
                "client_phone": "+996555123456",
                "client_name": "Айгуль",
                "address": "Ахунбаева 12, подъезд 2, этаж 4",
                "address_base": "Ахунбаева 12",
                "pickup_date": bishkek_today() + timedelta(days=1),
                "pickup_slot": "12–15",
                "items": ["Ковёр"],
                "comment": "Звонить за час",
            },
        )
        callback = self.callback("ds:create")
        bot = SimpleNamespace(send_message=AsyncMock())
        settings = SimpleNamespace(geocoder_url="https://geo.test", geocoder_user_agent="tests")
        with patch(
            "handlers.dispatcher.geocode_bishkek_address",
            new_callable=AsyncMock,
            return_value=(42.8, 74.6),
        ) as geocode:
            await create_order(callback, state, self.db, bot, settings)

        geocode.assert_awaited_once_with("Ахунбаева 12", "https://geo.test", "tests")
        order = await self.db.get(Order, 1)
        self.assertEqual(order.pickup_date, bishkek_today() + timedelta(days=1))
        self.assertEqual(order.latitude, 42.8)
        self.assertEqual(json.loads(order.items), ["Ковёр"])
        event = (await self.db.scalars(select(OrderEvent).where(OrderEvent.order_id == order.id))).one()
        self.assertEqual(event.note, "Заказ создан")
        self.assertIsNone(await state.get_state())

    async def test_address_and_date_are_locked_after_courier_takes_order(self):
        order = await self.make_order(status="pickup_en_route", pickup_courier_id=88)
        state = MemoryFSM()
        for field in ("address", "pickup_date"):
            callback = self.callback(f"dispatch:field:{order.id}:{field}")
            await choose_edit_field(callback, state, self.db)
            callback.answer.assert_awaited_once_with(
                "Адрес и дату можно менять только в очереди или после неудачной доставки.",
                show_alert=True,
            )

    async def test_failed_order_date_can_be_rescheduled_atomically(self):
        order = await self.make_order(status="delivery_failed", delivery_courier_id=89)
        state = MemoryFSM()
        callback = self.callback(f"ds:reschedule:{order.id}")
        await begin_reschedule(callback, state, self.db)
        message = SimpleNamespace(text=(bishkek_today() + timedelta(days=2)).strftime("%d.%m.%Y"), answer=AsyncMock())
        await set_rescheduled_date(message, state)
        self.assertEqual(state.current_state, OrderForm.reschedule_slot.state)

        slot_callback = self.callback("ds:slot:2")
        await choose_pickup_slot(slot_callback, state, self.db)

        await self.db.refresh(order)
        self.assertEqual(order.status, "new")
        self.assertEqual(order.pickup_date, bishkek_today() + timedelta(days=2))
        self.assertEqual(order.pickup_slot, "15–18")
        self.assertIsNone(order.delivery_courier_id)

    async def test_cancel_requires_reason_confirmation_and_records_event(self):
        order = await self.make_order()
        from handlers.dispatcher import cancel_order

        state = MemoryFSM()
        start = self.callback(f"ds:cancel:{order.id}")
        await cancel_order(start, state, self.db)
        self.assertEqual(state.current_state, OrderForm.cancel_reason.state)
        self.assertIn("Выберите причину", start.message.answer.await_args.args[0])

        reason = self.callback("ds:cancel:reason:0")
        await choose_cancel_reason(reason, state, self.db)
        self.assertEqual(state.current_state, OrderForm.cancel_confirm.state)
        self.assertIn("Точно отменить", reason.message.answer.await_args.args[0])

        confirm = self.callback("ds:cancel:yes")
        await confirm_cancel_order(confirm, state, self.db)
        await self.db.refresh(order)
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.cancel_reason, CANCEL_REASONS[0])
        events = list((await self.db.scalars(select(OrderEvent))).all())
        self.assertEqual(events[-1].reason, CANCEL_REASONS[0])

    async def test_failed_order_can_return_to_queue(self):
        order = await self.make_order(status="delivery_failed", delivery_courier_id=77)
        callback = self.callback(f"ds:requeue:{order.id}")

        from handlers.dispatcher import requeue_failed_order

        await requeue_failed_order(callback, self.db)

        await self.db.refresh(order)
        self.assertEqual(order.status, "new")
        self.assertIsNone(order.delivery_courier_id)

    async def test_manual_assignment_respects_status_and_active_limit(self):
        courier = User(
            telegram_id=55,
            full_name="Курьер",
            role="courier",
            is_approved=True,
            is_on_shift=True,
        )
        target = await self.make_order()
        courier_id = courier.telegram_id
        active_orders = [
            Order(
                client_name=f"Активный {index}",
                address="Адрес",
                pickup_date=bishkek_today(),
                status="pickup_en_route",
                pickup_courier_id=courier_id,
            )
            for index in range(COURIER_MAX_ACTIVE)
        ]
        self.db.add_all([courier, *active_orders])
        await self.db.commit()

        limited = self.callback(
            f"ds:assign:choose:{target.id}:{courier_id}",
            user_id=1,
            message=False,
        )
        await assign_courier(limited, self.db, limited.bot)
        await self.db.refresh(target)
        self.assertEqual(target.status, "new")
        limited.answer.assert_awaited_once()

        for active in active_orders:
            await self.db.refresh(active)
            active.status = "ready"
            active.pickup_courier_id = None
        await self.db.commit()
        assigned = self.callback(
            f"ds:assign:choose:{target.id}:{courier_id}",
            user_id=1,
            message=False,
        )
        await assign_courier(assigned, self.db, assigned.bot)
        await self.db.refresh(target)
        self.assertEqual(target.status, "pickup_en_route")
        self.assertEqual(target.pickup_courier_id, courier_id)
        assigned.bot.send_message.assert_awaited_once()

    async def test_manual_assignment_loses_race_if_courier_claimed_order(self):
        courier = User(
            telegram_id=56,
            full_name="Курьер",
            role="courier",
            is_approved=True,
            is_on_shift=True,
        )
        courier_id = courier.telegram_id
        target = await self.make_order(status="pickup_en_route", pickup_courier_id=courier_id)
        self.db.add(courier)
        await self.db.commit()
        callback = self.callback(f"ds:assign:choose:{target.id}:{courier.telegram_id}", message=False)

        await assign_courier(callback, self.db, callback.bot)

        await self.db.refresh(target)
        self.assertEqual(target.pickup_courier_id, courier_id)
        callback.bot.send_message.assert_not_awaited()
        callback.answer.assert_awaited_once()

    async def test_editing_assigned_order_notifies_its_courier(self):
        courier = User(telegram_id=57, full_name="Курьер", role="courier", is_approved=True)
        order = await self.make_order(status="pickup_en_route", pickup_courier_id=57)
        self.db.add(courier)
        await self.db.commit()
        bot = SimpleNamespace(send_message=AsyncMock())

        saved = await save_order_field(
            self.db,
            order,
            "comment",
            "Звонить за час",
            actor_id=1,
            actor_name="Диспетчер",
            bot=bot,
        )

        self.assertTrue(saved)
        bot.send_message.assert_awaited_once_with(57, f"Заказ #{order.id} изменён: Комментарий.")
        event = (await self.db.scalars(select(OrderEvent).where(OrderEvent.order_id == order.id))).one()
        self.assertEqual(event.note, "изменено поле Комментарий")

    async def test_search_modes_and_pagination_controls(self):
        order = await self.make_order(client_phone="+996555123456", client_name="Айгуль")
        self.assertEqual((await find_orders(self.db, f"#{order.id}"))[0].id, order.id)
        self.assertEqual((await find_orders(self.db, "555123456"))[0].id, order.id)
        self.assertEqual((await find_orders(self.db, "Айгуль"))[0].id, order.id)
        markup = list_markup("all", 1, 3)
        navigation = markup.inline_keyboard[-1]
        self.assertEqual(navigation[0].callback_data, "ds:list:all:0")
        self.assertEqual(navigation[-1].callback_data, "ds:list:all:2")

    async def test_order_lists_paginate_and_apply_status_filters(self):
        for index in range(11):
            await self.make_order(
                client_name=f"Клиент {index}",
                pickup_date=bishkek_today(),
                status="new",
            )
        await self.make_order(client_name="Готовый заказ", status="ready")

        message = SimpleNamespace(answer=AsyncMock())
        await send_order_list(message, self.db, "all", 0)
        self.assertIn("страница 1/2", message.answer.await_args.args[0])
        markup = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("ds:list:all:1", [button.callback_data for row in markup for button in row])

        callback = self.callback("ds:list:new:0")
        await replace_with_order_list(callback, self.db, "new", 0)
        self.assertIn("Новые", callback.message.edit_text.await_args.args[0])
        self.assertNotIn("Готовый заказ", callback.message.edit_text.await_args.args[0])

        today_message = SimpleNamespace(answer=AsyncMock())
        await send_today_list(today_message, self.db, 0)
        self.assertIn("Заказы на сегодня", today_message.answer.await_args.args[0])

    async def test_daily_summary_and_payment_marking_use_event_history(self):
        order = await self.make_order(status="delivered", total_amount=1400, is_paid=False)
        self.db.add(
            OrderEvent(
                order_id=order.id,
                actor_id=22,
                actor_name="Курьер",
                from_status="delivery_en_route",
                to_status="delivered",
            )
        )
        await self.db.commit()
        summary = SimpleNamespace(answer=AsyncMock())

        await show_daily_summary(summary, self.db, 0)

        self.assertIn("Доставлено: 1", summary.answer.await_args.args[0])
        self.assertIn("Сумма к получению: 1400 сом", summary.answer.await_args.args[0])

        callback = self.callback(f"ds:paid:{order.id}")
        await mark_order_paid(callback, self.db)
        await self.db.refresh(order)
        self.assertTrue(order.is_paid)
        self.assertIsNotNone(order.paid_at)


if __name__ == "__main__":
    unittest.main()
