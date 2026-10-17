import unittest
from types import SimpleNamespace

from utils.geo import haversine_km, sort_orders_by_distance


class GeoTests(unittest.TestCase):
    def test_haversine_distance(self):
        distance = haversine_km(42.8746, 74.5698, 42.8746, 74.5698)
        self.assertEqual(distance, 0)

    def test_sort_places_ungeocoded_orders_last(self):
        farther = SimpleNamespace(latitude=42.90, longitude=74.57)
        nearer = SimpleNamespace(latitude=42.88, longitude=74.57)
        unknown = SimpleNamespace(latitude=None, longitude=None)
        sorted_orders = sort_orders_by_distance([farther, unknown, nearer], 42.87, 74.57)
        self.assertEqual(sorted_orders, [nearer, farther, unknown])


if __name__ == "__main__":
    unittest.main()