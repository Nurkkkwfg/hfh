import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import Settings
from database.session import create_database, initialize_database
from handlers import admin, courier, dispatcher as dispatcher_handlers, fallback, laundry
from handlers.common import DatabaseMiddleware


def create_dispatcher(settings: Settings, session_factory) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    middleware = DatabaseMiddleware(session_factory, settings.admin_ids)
    for observer in (dp.message, dp.edited_message, dp.callback_query):
        observer.outer_middleware(middleware)
    dp["settings"] = settings
    dp.include_router(admin.router)
    dp.include_router(dispatcher_handlers.router)
    dp.include_router(courier.router)
    dp.include_router(laundry.router)
    dp.include_router(fallback.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    engine, session_factory = create_database(settings.database_url)
    await initialize_database(engine)

    bot = Bot(token=settings.bot_token)
    dp = create_dispatcher(settings, session_factory)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())