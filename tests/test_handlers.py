import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers.admin import assign_role, change_staff_role, disable_staff, start, admin_cancel_order
from database.models import Order, User
from database.session import create_database, initialize_database
from handlers.common import DatabaseMiddleware, ROLE_COURIER, send_order_card
from handlers.courier import end_shift, save_location, show_tasks, update_order_status
from handlers.dispatcher import (
    OrderForm,
    begin_edit,
    choose_address_type,
    confirm_address,
    cancel_order,
    set_address,
    set_address_details,
    set_pickup_date,
)
from handlers.fallback import ROLE_HELP
from handlers.laundry import cancel_laundry_flow, mark_ready, save_measurement, show_order
from main import USER_ERROR_MESSAGE, create_dispatcher, handle_unhandled_error
from utils.dates import bishkek_today


class MemoryFSM:
    def __init__(self, current_state, data):
        self.current_state = current_state
        self.data = data

    async def get_state(self):
        return self.current_state

    async def set_state(self, state):
        self.current_state = state.state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.current_state = None
        self.data.clear()


class HandlerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_address_search_confirmation_and_apartment_details(self):
        state = MemoryFSM(OrderForm.address.state, {"address_flow": "create"})
        message = SimpleNamespace(text="улица Киевская, 10", answer=AsyncMock())

        await set_address(message, state)

        self.assertEqual(state.current_state, OrderForm.address_confirm.state)
        markup = message.answer.await_args.kwargs["reply_markup"]
        search_button = markup.inline_keyboard[0][0]
        self.assertEqual(search_button.text, "🔎 Проверить адрес в 2ГИС")
        self.assertIn("2gis.kg/bishkek/search/", search_button.url)
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "dispatch:address:confirm")

        callback = SimpleNamespace(
            data="dispatch:address:confirm",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        await confirm_address(callback, state)
        self.assertEqual(state.current_state, OrderForm.address_type.state)

        callback.data = "dispatch:address:type:apartment"
        await choose_address_type(callback, state, db=None, settings=None)
        self.assertEqual(state.current_state, OrderForm.address_details.state)

        details_message = SimpleNamespace(text="подъезд 2, этаж 4, квартира 15", answer=AsyncMock())
        await set_address_details(details_message, state, db=None, settings=None)
        self.assertEqual(state.data["address"], "улица Киевская, 10, подъезд 2, этаж 4, квартира 15")
        self.assertEqual(state.current_state, OrderForm.pickup_day.state)
        self.assertIn("Выберите дату", details_message.answer.await_args.args[0])

    async def test_private_house_address_does_not_request_apartment_details(self):
        state = MemoryFSM(
            OrderForm.address_type.state,
            {"address_flow": "create", "address_base": "улица Садовая, 5"},
        )
        callback = SimpleNamespace(
            data="dispatch:address:type:house",
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )

        await choose_address_type(callback, state, db=None, settings=None)

        self.assertEqual(state.data["address"], "улица Садовая, 5 (частный дом)")
        self.assertEqual(state.current_state, OrderForm.pickup_day.state)
        self.assertIn("Выберите дату", callback.message.answer.await_args.args[0])

    async def test_pickup_date_step_stores_a_date_and_proceeds_to_slot_selection(self):
        state = MemoryFSM(OrderForm.pickup_day.state, {"address": "улица Киевская, 10"})
        message = SimpleNamespace(text="Завтра", answer=AsyncMock())

        await set_pickup_date(message, state)

        self.assertEqual(state.data["pickup_date"], bishkek_today() + timedelta(days=1))
        self.assertEqual(state.current_state, OrderForm.pickup_slot.state)
        self.assertIn("интервал", message.answer.await_args.args[0])

    async def test_database_middleware_is_outermost_for_every_update_type(self):
        settings = SimpleNamespace(admin_ids=frozenset({1}))
        dispatcher = create_dispatcher(settings, session_factory=None)

        for observer in (dispatcher.message, dispatcher.edited_message, dispatcher.callback_query):
            self.assertTrue(
                any(isinstance(middleware, DatabaseMiddleware) for middleware in observer.outer_middleware._middlewares)
            )

    async def test_admin_can_change_role_disable_staff_and_cancel_order(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                staff = User(
                    telegram_id=52,
                    full_name="Staff Member",
                    role=ROLE_COURIER,
                    is_approved=True,
                    is_on_shift=True,
                    latitude=42.8,
                    longitude=74.6,
                )
                order = Order(client_name="Client", address="Street 1", pickup_day="Сегодня")
                pickup_order = Order(
                    client_name="Pickup",
                    address="Street 2",
                    pickup_day="Сегодня",
                    status="pickup_en_route",
                    pickup_courier_id=52,
                )
                delivery_order = Order(
                    client_name="Delivery",
                    address="Street 3",
                    pickup_day="Сегодня",
                    status="delivery_en_route",
                    delivery_courier_id=52,
                )
                db.add_all([staff, order, pickup_order, delivery_order])
                await db.commit()
                bot = SimpleNamespace(send_message=AsyncMock())
                settings = SimpleNamespace(admin_ids=frozenset({1}))

                def make_callback(data):
                    return SimpleNamespace(
                        from_user=SimpleNamespace(id=1),
                        data=data,
                        answer=AsyncMock(),
                        message=SimpleNamespace(
                            edit_reply_markup=AsyncMock(),
                            edit_text=AsyncMock(),
                            answer=AsyncMock(),
                        ),
                    )

                await change_staff_role(make_callback("staff:change:52:dispatcher"), db, bot, settings)
                self.assertEqual(staff.role, "dispatcher")
                bot.send_message.assert_awaited_with(
                    52,
                    "Ваша роль изменена: Диспетчер.",
                    reply_markup=unittest.mock.ANY,
                )

                await disable_staff(make_callback("staff:disable:52"), db, bot, settings)
                self.assertFalse(staff.is_approved)
                self.assertIsNone(staff.role)
                self.assertFalse(staff.is_on_shift)
                self.assertIsNone(staff.latitude)
                self.assertIsNone(staff.longitude)
                self.assertEqual(pickup_order.status, "new")
                self.assertIsNone(pickup_order.pickup_courier_id)
                self.assertEqual(delivery_order.status, "ready")
                self.assertIsNone(delivery_order.delivery_courier_id)

                await admin_cancel_order(
                    make_callback(f"admin:order:cancel:{order.id}"),
                    db,
                    settings,
                )
                self.assertEqual(order.status, "cancelled")
        finally:
            await engine.dispose()

    async def test_laundry_cancel_clears_state_and_restores_role_keyboard(self):
        state = SimpleNamespace(get_state=AsyncMock(return_value="LaundryForm:measurements"), clear=AsyncMock())
        message = SimpleNamespace(answer=AsyncMock())

        await cancel_laundry_flow(message, state)

        state.clear.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "Действие отменено.")
        self.assertIsNotNone(message.answer.await_args.kwargs["reply_markup"])

    async def test_start_does_not_resend_request_for_existing_pending_user(self):
        user = SimpleNamespace(is_approved=False)
        db = SimpleNamespace(get=AsyncMock(return_value=user))
        bot = SimpleNamespace(send_message=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=52, full_name="Staff Member", username="staff"),
            answer=AsyncMock(),
        )
        settings = SimpleNamespace(admin_ids=frozenset({1, 2}))

        await start(message, db, bot, settings, user_role=None)

        bot.send_message.assert_not_awaited()
        self.assertEqual(message.answer.await_args.args[0], "Запрос уже отправлен, ожидайте.")

    async def test_stale_admin_approval_cannot_change_assigned_role(self):
        user = SimpleNamespace(is_approved=True, role=ROLE_COURIER)
        db = SimpleNamespace(get=AsyncMock(return_value=user), commit=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="staff:role:52:dispatcher",
            answer=AsyncMock(),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        settings = SimpleNamespace(admin_ids=frozenset({1}))

        await assign_role(callback, db, bot, settings)

        self.assertEqual(user.role, ROLE_COURIER)
        db.commit.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        callback.answer.assert_awaited_once_with("Пользователь уже одобрен.", show_alert=True)

    async def test_concurrent_admin_approval_does_not_overwrite_role(self):
        pending_user = SimpleNamespace(is_approved=False, role=None)
        db = SimpleNamespace(
            get=AsyncMock(return_value=pending_user),
            execute=AsyncMock(return_value=SimpleNamespace(rowcount=0)),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="staff:role:52:dispatcher",
            answer=AsyncMock(),
            message=None,
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        settings = SimpleNamespace(admin_ids=frozenset({1}))

        await assign_role(callback, db, bot, settings)

        self.assertIsNone(pending_user.role)
        self.assertFalse(pending_user.is_approved)
        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        callback.answer.assert_awaited_once_with("Пользователь уже одобрен.", show_alert=True)

    async def test_super_admin_cannot_be_granted_from_staff_approval_button(self):
        db = SimpleNamespace(get=AsyncMock(), commit=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="staff:role:52:super_admin",
            answer=AsyncMock(),
            message=None,
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        settings = SimpleNamespace(admin_ids=frozenset({1}))

        await assign_role(callback, db, bot, settings)

        db.get.assert_not_awaited()
        db.commit.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        callback.answer.assert_awaited_once_with("Неизвестная роль.", show_alert=True)

    async def test_assign_role_updates_the_loaded_user_record(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                user = User(telegram_id=52, full_name="Pending Staff", is_approved=False)
                db.add(user)
                await db.commit()

                callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=1),
                    data="staff:role:52:dispatcher",
                    answer=AsyncMock(),
                    message=None,
                )
                bot = SimpleNamespace(send_message=AsyncMock())
                settings = SimpleNamespace(admin_ids=frozenset({1}))

                await assign_role(callback, db, bot, settings)

                self.assertTrue(user.is_approved)
                self.assertEqual(user.role, "dispatcher")
        finally:
            await engine.dispose()

    async def test_pickup_claim_updates_the_loaded_order_record(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                courier = User(
                    telegram_id=101,
                    full_name="Courier One",
                    role=ROLE_COURIER,
                    is_approved=True,
                    is_on_shift=True,
                )
                order = Order(client_name="Client", address="Street 1", pickup_day="Сегодня")
                db.add_all([courier, order])
                await db.commit()

                callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=101),
                    data=f"courier:pickup:{order.id}",
                    answer=AsyncMock(),
                    message=SimpleNamespace(edit_text=AsyncMock()),
                )

                await update_order_status(callback, db)

                self.assertEqual(order.status, "pickup_en_route")
                self.assertEqual(order.pickup_courier_id, 101)
        finally:
            await engine.dispose()

    async def test_pickup_claim_is_exclusive_and_requires_active_shift(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                first_courier = User(
                    telegram_id=101,
                    full_name="Courier One",
                    role=ROLE_COURIER,
                    is_approved=True,
                    is_on_shift=True,
                )
                second_courier = User(
                    telegram_id=102,
                    full_name="Courier Two",
                    role=ROLE_COURIER,
                    is_approved=True,
                    is_on_shift=True,
                )
                order = Order(client_name="Client", address="Street 1", pickup_day="Сегодня")
                db.add_all([first_courier, second_courier, order])
                await db.commit()

                def make_callback(courier_id):
                    return SimpleNamespace(
                        from_user=SimpleNamespace(id=courier_id),
                        data=f"courier:pickup:{order.id}",
                        answer=AsyncMock(),
                        message=SimpleNamespace(edit_text=AsyncMock()),
                    )

                await update_order_status(make_callback(101), db)
                await db.refresh(order)
                self.assertEqual(order.status, "pickup_en_route")
                self.assertEqual(order.pickup_courier_id, 101)

                second_callback = make_callback(102)
                await update_order_status(second_callback, db)
                await db.refresh(order)
                self.assertEqual(order.pickup_courier_id, 101)
                second_callback.answer.assert_awaited_once()

                first_courier.is_on_shift = False
                await db.commit()
                inactive_callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=101),
                    data=f"courier:picked:{order.id}",
                    answer=AsyncMock(),
                    message=None,
                )
                await update_order_status(inactive_callback, db)
                await db.refresh(order)
                self.assertEqual(order.status, "pickup_en_route")
                inactive_callback.answer.assert_awaited_once_with(
                    "Смена не активна. Начните смену перед обновлением заказа.", show_alert=True
                )
        finally:
            await engine.dispose()

    async def test_delivery_claim_assigns_an_unassigned_ready_order(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                courier = User(
                    telegram_id=101,
                    full_name="Courier",
                    role=ROLE_COURIER,
                    is_approved=True,
                    is_on_shift=True,
                )
                order = Order(
                    client_name="Client",
                    address="Street 1",
                    pickup_day="17.10.2026",
                    status="ready",
                )
                db.add_all([courier, order])
                await db.commit()
                callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=101),
                    data=f"courier:delivery:{order.id}",
                    answer=AsyncMock(),
                    message=SimpleNamespace(edit_text=AsyncMock()),
                )

                await update_order_status(callback, db)

                self.assertEqual(order.status, "delivery_en_route")
                self.assertEqual(order.delivery_courier_id, 101)
        finally:
            await engine.dispose()

    async def test_live_location_after_shift_is_silently_ignored(self):
        user = SimpleNamespace(is_on_shift=False)
        db = SimpleNamespace(get=AsyncMock(return_value=user), commit=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            location=SimpleNamespace(latitude=42.8, longitude=74.6),
            answer=AsyncMock(),
        )

        await save_location(message, db, notify=False)

        message.answer.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_end_shift_warns_about_assigned_active_orders(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                courier = User(
                    telegram_id=101,
                    full_name="Courier",
                    role=ROLE_COURIER,
                    is_approved=True,
                    is_on_shift=True,
                )
                order = Order(
                    client_name="Client",
                    address="Street 1",
                    pickup_day="17.10.2026",
                    status="delivery_en_route",
                    delivery_courier_id=101,
                )
                db.add_all([courier, order])
                await db.commit()
                message = SimpleNamespace(
                    from_user=SimpleNamespace(id=101),
                    answer=AsyncMock(),
                )

                await end_shift(message, db)

                self.assertFalse(courier.is_on_shift)
                self.assertIn("активных заказов: 1", message.answer.await_args.args[0])
        finally:
            await engine.dispose()

    async def test_tasks_hide_future_pickups_and_use_longitude_first_2gis_url(self):
        today = date(2026, 10, 17)
        due_order = SimpleNamespace(
            id=1,
            status="new",
            pickup_day="17.10.2026",
            pickup_courier_id=None,
            delivery_courier_id=None,
            client_name="Due",
            address="Street 1",
            latitude=42.8,
            longitude=74.6,
        )
        future_order = SimpleNamespace(
            id=2,
            status="new",
            pickup_day="24.10.2026",
            pickup_courier_id=None,
            delivery_courier_id=None,
            client_name="Future",
            address="Street 2",
            latitude=42.9,
            longitude=74.7,
        )
        db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [due_order, future_order])))
        courier = SimpleNamespace(telegram_id=101, latitude=42.7, longitude=74.5)
        message = SimpleNamespace(answer=AsyncMock())

        with patch("handlers.courier.bishkek_today", return_value=today):
            await show_tasks(message, db, courier)

        self.assertEqual(message.answer.await_count, 1)
        task_message = message.answer.await_args
        self.assertIn("Заказ #1", task_message.args[0])
        self.assertNotIn("Заказ #2", task_message.args[0])
        buttons = task_message.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].url, "https://2gis.kg/bishkek/geo/74.6,42.8")

    async def test_dispatcher_restricts_fields_and_cancellation_for_in_route_orders(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                order = Order(
                    client_name="Client",
                    address="Street 1",
                    pickup_day="17.10.2026",
                    status="pickup_en_route",
                )
                db.add(order)
                await db.commit()
                state = MemoryFSM(None, {})
                edit_callback = SimpleNamespace(
                    data=f"dispatch:edit:{order.id}",
                    answer=AsyncMock(),
                    message=SimpleNamespace(answer=AsyncMock()),
                )
                cancel_callback = SimpleNamespace(
                    data=f"dispatch:cancel:{order.id}",
                    answer=AsyncMock(),
                    message=SimpleNamespace(edit_text=AsyncMock()),
                )

                await begin_edit(edit_callback, state, db)
                edit_buttons = edit_callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
                labels = {button.text for row in edit_buttons for button in row}
                self.assertEqual(labels, {"Имя", "Телефон", "Комментарий"})
                await cancel_order(cancel_callback, state, db)

                cancel_callback.answer.assert_awaited_once_with(
                    "Отменить можно только новый или проблемный заказ.", show_alert=True
                )
                await db.refresh(order)
                self.assertEqual(order.status, "pickup_en_route")
        finally:
            await engine.dispose()

    async def test_closed_dispatcher_order_cards_have_no_action_buttons(self):
        for status in ("delivered", "cancelled"):
            message = SimpleNamespace(answer=AsyncMock())
            order = SimpleNamespace(
                id=12,
                client_name="Client",
                address="Street 1",
                pickup_day="17.10.2026",
                status=status,
                created_at=datetime(2026, 10, 17, 12, tzinfo=timezone.utc),
                measurements=None,
            )
            with self.subTest(status=status):
                await send_order_card(message, order, show_dispatcher_actions=True)
                self.assertIsNone(message.answer.await_args.kwargs.get("reply_markup"))

    async def test_laundry_status_writes_are_atomic_and_race_safe(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as db:
                order = Order(
                    client_name="Client",
                    address="Street 1",
                    pickup_day="17.10.2026",
                    status="picked_up",
                )
                db.add(order)
                await db.commit()
                state = MemoryFSM("LaundryForm:measurements", {"order_id": order.id})
                message = SimpleNamespace(
                    text="Ковёр 3x4",
                    answer=AsyncMock(),
                    from_user=SimpleNamespace(id=5, full_name="Цех"),
                )

                await save_measurement(message, state, db)
                await db.refresh(order)
                self.assertEqual(order.status, "processing")
                self.assertEqual(order.measurements, "Ковёр 3x4")

                callback = SimpleNamespace(
                    data=f"laundry:ready:{order.id}",
                    from_user=SimpleNamespace(id=5, full_name="Цех"),
                    answer=AsyncMock(),
                    message=SimpleNamespace(edit_text=AsyncMock()),
                    bot=SimpleNamespace(send_message=AsyncMock()),
                )
                await mark_ready(callback, db)
                self.assertEqual(order.status, "ready")

                stale_state = MemoryFSM("LaundryForm:measurements", {"order_id": order.id})
                stale_message = SimpleNamespace(
                    text="Повторные замеры",
                    answer=AsyncMock(),
                    from_user=SimpleNamespace(id=5, full_name="Цех"),
                )
                await save_measurement(stale_message, stale_state, db)
                stale_message.answer.assert_awaited_once_with("Заказ уже недоступен для изменения.")
                self.assertIsNone(await stale_state.get_state())
                await db.refresh(order)
                self.assertEqual(order.measurements, "Ковёр 3x4")

                repeated_callback = SimpleNamespace(
                    data=f"laundry:ready:{order.id}",
                    answer=AsyncMock(),
                    message=None,
                )
                await mark_ready(repeated_callback, db)
                repeated_callback.answer.assert_awaited_once_with(
                    "Заказ не найден или его статус уже изменился.",
                    show_alert=True,
                )
                await db.refresh(order)
                self.assertEqual(order.status, "ready")
        finally:
            await engine.dispose()

    async def test_laundry_uses_human_readable_order_status(self):
        order = SimpleNamespace(
            id=12,
            status="new",
        )
        db = SimpleNamespace(get=AsyncMock(return_value=order))
        message = SimpleNamespace(answer=AsyncMock())

        await show_order(message, db, "12")

        self.assertIn("статус: Новый", message.answer.await_args.args[0])

    async def test_admin_help_mentions_panel_sections(self):
        self.assertIn("/admin", ROLE_HELP["super_admin"])
        for section in ("Сотрудники", "Заявки", "Статистика", "Заказы"):
            self.assertIn(section, ROLE_HELP["super_admin"])

    async def test_first_start_sends_access_request_once(self):
        db = SimpleNamespace(get=AsyncMock(return_value=None), add=Mock(), commit=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=52, full_name="Staff Member", username="staff"),
            answer=AsyncMock(),
        )
        settings = SimpleNamespace(admin_ids=frozenset({1, 2}))

        await start(message, db, bot, settings, user_role=None)

        self.assertEqual(bot.send_message.await_count, 2)

    async def test_global_error_handler_answers_callback_and_message(self):
        callback = SimpleNamespace(answer=AsyncMock())
        callback_event = SimpleNamespace(
            exception=RuntimeError("callback failure"),
            update=SimpleNamespace(callback_query=callback, message=None, edited_message=None),
        )
        with self.assertLogs("main", level="ERROR"):
            await handle_unhandled_error(callback_event)
        callback.answer.assert_awaited_once_with(USER_ERROR_MESSAGE, show_alert=True)

        message = SimpleNamespace(answer=AsyncMock())
        message_event = SimpleNamespace(
            exception=RuntimeError("message failure"),
            update=SimpleNamespace(callback_query=None, message=message, edited_message=None),
        )
        with self.assertLogs("main", level="ERROR"):
            await handle_unhandled_error(message_event)
        message.answer.assert_awaited_once_with(USER_ERROR_MESSAGE)


if __name__ == "__main__":
    unittest.main()