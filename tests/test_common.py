import unittest
from datetime import date, datetime, timezone

from handlers.common import ROLE_ADMIN, STATUS_LABELS, role_keyboard, status_label
from utils.dates import format_bishkek_datetime, normalize_pickup_day, parse_pickup_day


class CommonTests(unittest.TestCase):
    def test_admin_keyboard_exposes_all_admin_sections(self):
        keyboard = role_keyboard(ROLE_ADMIN)
        labels = {button.text for row in keyboard.keyboard for button in row}
        self.assertEqual(labels, {"👥 Сотрудники", "⏳ Заявки", "📊 Статистика", "📦 Заказы"})

    def test_order_statuses_have_human_readable_labels(self):
        self.assertEqual(status_label("pickup_en_route"), "Курьер едет за заказом")
        self.assertEqual(status_label("unrecognized"), "unrecognized")

    def test_every_order_status_has_a_human_readable_label(self):
        self.assertEqual(
            set(STATUS_LABELS),
            {
                "new",
                "pickup_en_route",
                "picked_up",
                "processing",
                "ready",
                "delivery_en_route",
                "delivered",
                "cancelled",
                "pickup_failed",
                "delivery_failed",
            },
        )

    def test_pickup_day_shortcuts_and_future_date(self):
        today = date(2026, 10, 17)
        self.assertEqual(normalize_pickup_day("сЕгОдНя", today=today), "17.10.2026")
        self.assertEqual(normalize_pickup_day("ЗАВТРА", today=today), "18.10.2026")
        self.assertEqual(normalize_pickup_day("18.10.2026", today=today), "18.10.2026")
        self.assertEqual(parse_pickup_day("18.10.2026", today=today), date(2026, 10, 18))

    def test_invalid_and_past_pickup_days_are_rejected(self):
        today = date(2026, 10, 17)
        self.assertIsNone(normalize_pickup_day("32.10.2026", today=today))
        self.assertIsNone(normalize_pickup_day("16.10.2026", today=today))
        self.assertIsNone(normalize_pickup_day("next week", today=today))

    def test_created_at_is_displayed_in_bishkek_timezone(self):
        created_at_utc = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(format_bishkek_datetime(created_at_utc), "17.10.2026 18:00")


if __name__ == "__main__":
    unittest.main()