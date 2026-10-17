from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.models import Base
from utils.dates import bishkek_today, parse_pickup_day


def create_database(database_url: str):
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


async def initialize_database(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_migrate_legacy_orders)
        await connection.run_sync(Base.metadata.create_all)


def _migrate_legacy_orders(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    user_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(users)").fetchall()}
    if user_columns and "phone" not in user_columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN phone VARCHAR(32)")

    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(orders)").fetchall()}
    if not columns:
        return
    today = bishkek_today()
    additions = {
        "client_phone": "VARCHAR(32) NOT NULL DEFAULT ''",
        "pickup_date": f"DATE NOT NULL DEFAULT '{today.isoformat()}'",
        "pickup_slot": "VARCHAR(16) NOT NULL DEFAULT 'Любое'",
        "items": "TEXT NOT NULL DEFAULT '[]'",
        "comment": "TEXT",
        "total_amount": "INTEGER NOT NULL DEFAULT 0",
        "is_paid": "BOOLEAN NOT NULL DEFAULT 0",
        "paid_at": "DATETIME",
        "cancel_reason": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE orders ADD COLUMN {name} {declaration}")

    if "pickup_day" in columns:
        for order_id, raw_day, current_date in connection.exec_driver_sql(
            "SELECT id, pickup_day, pickup_date FROM orders"
        ).fetchall():
            value = (raw_day or "").strip()
            parsed_date = None
            try:
                parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                parsed_date = parse_pickup_day(value, today=today)
            if parsed_date is None:
                parsed_date = datetime.strptime(current_date, "%Y-%m-%d").date() if current_date else today
            connection.exec_driver_sql(
                "UPDATE orders SET pickup_date = ? WHERE id = ?",
                (parsed_date.isoformat(), order_id),
            )