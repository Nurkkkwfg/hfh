import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.admin import assign_role, start
from handlers.common import ROLE_COURIER


class HandlerRegressionTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()