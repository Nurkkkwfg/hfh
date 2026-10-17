import unittest
from datetime import date

from handlers.common import STATUS_LABELS, status_label
from utils.dates import normalize_pickup_day


class CommonTests(unittest.TestCase):
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
            },
        )

    def test_pickup_day_shortcuts_and_future_date(self):
        today = date(2026, 10, 17)
        self.assertEqual(normalize_pickup_day("сЕгОдНя", today=today), "Сегодня")
        self.assertEqual(normalize_pickup_day("ЗАВТРА", today=today), "Завтра")
        self.assertEqual(normalize_pickup_day("18.10.2026", today=today), "18.10.2026")

    def test_invalid_and_past_pickup_days_are_rejected(self):
        today = date(2026, 10, 17)
        self.assertIsNone(normalize_pickup_day("32.10.2026", today=today))
        self.assertIsNone(normalize_pickup_day("16.10.2026", today=today))
        self.assertIsNone(normalize_pickup_day("next week", today=today))


if __name__ == "__main__":
    unittest.main()