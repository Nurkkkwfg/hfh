from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from utils.dates import bishkek_today, pickup_date_value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str | None] = mapped_column(String(32), index=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_on_shift: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_phone: Mapped[str] = mapped_column(String(32), default="", nullable=False, index=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    pickup_date: Mapped[date] = mapped_column(Date, default=bishkek_today, nullable=False, index=True)
    pickup_slot: Mapped[str] = mapped_column(String(16), default="Любое", nullable=False)
    items: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    measurements: Mapped[str | None] = mapped_column(Text)
    total_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    pickup_courier_id: Mapped[int | None] = mapped_column(BigInteger)
    delivery_courier_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def pickup_day(self) -> str:
        return self.pickup_date.strftime("%d.%m.%Y")

    @pickup_day.setter
    def pickup_day(self, value: str) -> None:
        try:
            parsed = datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            parsed = pickup_date_value(value)
        if parsed is None:
            raise ValueError("pickup_day must be a valid date")
        self.pickup_date = parsed


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_name: Mapped[str] = mapped_column(String(255), default="Система", nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )