import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    database_url: str
    geocoder_url: str
    geocoder_user_agent: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required. Copy .env.example to .env and configure it.")

        raw_admin_ids = os.getenv("ADMIN_IDS", "")
        try:
            admin_ids = frozenset(int(value.strip()) for value in raw_admin_ids.split(",") if value.strip())
        except ValueError as error:
            raise RuntimeError("ADMIN_IDS must be a comma-separated list of Telegram numeric IDs") from error
        if not admin_ids:
            raise RuntimeError("At least one Telegram ID must be configured in ADMIN_IDS")

        return cls(
            bot_token=token,
            admin_ids=admin_ids,
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./database.db"),
            geocoder_url=os.getenv("GEOCODER_URL", "https://nominatim.openstreetmap.org/search"),
            geocoder_user_agent=os.getenv(
                "GEOCODER_USER_AGENT", "hfh-internal-crm/1.0 (contact: ops@example.com)"
            ),
        )