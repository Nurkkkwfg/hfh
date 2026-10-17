import unittest
from datetime import date
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
                self.assertIsInstance(first.pickup_date, date)
        finally:
            await engine.dispose()

    async def test_legacy_pickup_day_is_migrated_to_date_column(self):
        engine, session_factory = create_database("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    """
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_name VARCHAR(255) NOT NULL,
                        address TEXT NOT NULL,
                        pickup_day VARCHAR(64),
                        status VARCHAR(32) NOT NULL DEFAULT 'new',
                        latitude FLOAT,
                        longitude FLOAT,
                        measurements TEXT,
                        pickup_courier_id BIGINT,
                        delivery_courier_id BIGINT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                await connection.exec_driver_sql(
                    "INSERT INTO orders (client_name, address, pickup_day) VALUES (?, ?, ?)",
                    ("Клиент", "Улица 1", "17.10.2026"),
                )

            await initialize_database(engine)
            async with session_factory() as session:
                migrated = await session.get(Order, 1)
                self.assertEqual(migrated.pickup_date, date(2026, 10, 17))
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()