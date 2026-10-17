from datetime import date, datetime
from zoneinfo import ZoneInfo


def normalize_pickup_day(value: str, *, today: date | None = None) -> str | None:
    normalized = value.strip().casefold()
    if normalized == "сегодня":
        return "Сегодня"
    if normalized == "завтра":
        return "Завтра"

    try:
        pickup_date = datetime.strptime(normalized, "%d.%m.%Y").date()
    except ValueError:
        return None

    current_date = today or datetime.now(ZoneInfo("Asia/Bishkek")).date()
    if pickup_date < current_date:
        return None
    return pickup_date.strftime("%d.%m.%Y")