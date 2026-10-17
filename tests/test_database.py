import unittest
from datetime import timezone

from database.models import Order
from database.session import create_database, initialize_database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_ids_autoincrement_in_sqlite(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            await initialize_database(engine)
            async with session_factory() as session:
                first = Order(client_name="A", address="Street 1", pickup_day="17.10.2026")
                second = Order(client_name="B", address="Street 2", pickup_day="18.10.2026")
                session.add_all([first, second])
                await session.commit()
                self.assertEqual((first.id, second.id), (1, 2))
                self.assertEqual(first.created_at.utcoffset().total_seconds(), 0)
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()