from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.models import Base


def create_database(database_url: str):
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


async def initialize_database(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)