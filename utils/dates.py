from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BISHKEK_TZ = ZoneInfo("Asia/Bishkek")


def bishkek_today() -> date:
    return datetime.now(BISHKEK_TZ).date()


def normalize_pickup_day(value: str, *, today: date | None = None) -> str | None:
    normalized = value.strip().casefold()
    current_date = today or bishkek_today()
    if normalized == "сегодня":
        return current_date.strftime("%d.%m.%Y")
    if normalized == "завтра":
        return (current_date + timedelta(days=1)).strftime("%d.%m.%Y")

    try:
        pickup_date = datetime.strptime(normalized, "%d.%m.%Y").date()
    except ValueError:
        return None

    if pickup_date < current_date:
        return None
    return pickup_date.strftime("%d.%m.%Y")


def parse_pickup_day(value: str, *, today: date | None = None) -> date | None:
    normalized = value.strip().casefold()
    current_date = today or bishkek_today()
    if normalized == "сегодня":
        return current_date
    if normalized == "завтра":
        return current_date + timedelta(days=1)
    try:
        return datetime.strptime(normalized, "%d.%m.%Y").date()
    except ValueError:
        return None


def format_bishkek_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M")