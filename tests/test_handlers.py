import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.admin import assign_role, change_staff_role, disable_staff, start, admin_cancel_order
from database.models import Order, User
from database.session import create_database, initialize_database
from handlers.common import DatabaseMiddleware, ROLE_COURIER
from handlers.courier import update_order_status
from handlers.laundry import cancel_laundry_flow
from main import create_dispatcher


class HandlerRegressionTests(unittest.IsolatedAsyncioTestCase):
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
                db.add_all([staff, order])
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

    async def test_start_resends_request_for_pending_user(self):
        user = SimpleNamespace(is_approved=False)
        db = SimpleNamespace(get=AsyncMock(return_value=user))
        bot = SimpleNamespace(send_message=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=52, full_name="Staff Member", username="staff"),
            answer=AsyncMock(),
        )
        settings = SimpleNamespace(admin_ids=frozenset({1, 2}))

        await start(message, db, bot, settings, user_role=None)

        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIn("Дождитесь назначения роли", message.answer.await_args.args[0])

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


if __name__ == "__main__":
    unittest.main()